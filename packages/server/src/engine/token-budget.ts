import { estimateTokens } from "../util/tokens.js";

export type ContentBlock = {
  content: string;
  tier: 1 | 2 | 3;
  label: string;
  fallback?: string; // compressed version to try if full content doesn't fit
};

export const BLOCK_SEPARATOR = "\n\n---\n\n";

/** Default byte ceiling when a caller does not supply one. */
export const DEFAULT_MAX_BYTES = 16384;

const TRUNCATION_NOTICE =
  "\n\n---\n\n_Context truncated at the byte ceiling; some lower-priority sections were omitted._";

/**
 * Clamp assembled context to a hard byte ceiling.
 *
 * The token budget is an estimate over an assumed characters-per-token ratio;
 * this is the size that actually goes on the wire. An MCP stdio response is a
 * single newline-delimited JSON line, and a client that does not reassemble a
 * message split across pipe reads will fail to decode an oversized one -- so
 * an over-budget context does not merely cost tokens, it can break transport.
 *
 * Whole blocks are dropped from the end (lowest priority first, since blocks
 * arrive in tier order) rather than cutting mid-sentence. If even the first
 * block exceeds the ceiling it is hard-truncated, because returning something
 * oversized would defeat the point.
 */
export function clampToByteCeiling(blocks: string[], maxBytes: number): string {
  const byteLength = (s: string) => Buffer.byteLength(s, "utf8");

  const full = blocks.join(BLOCK_SEPARATOR);
  if (byteLength(full) <= maxBytes) return full;

  const noticeBytes = byteLength(TRUNCATION_NOTICE);
  const kept: string[] = [];
  let used = 0;

  for (const block of blocks) {
    const addition = (kept.length > 0 ? byteLength(BLOCK_SEPARATOR) : 0) + byteLength(block);
    if (used + addition + noticeBytes > maxBytes) break;
    kept.push(block);
    used += addition;
  }

  if (kept.length === 0) {
    // Even one block does not fit. Cut on a character boundary -- slicing a
    // Buffer could split a multi-byte character and emit a replacement char.
    const budget = Math.max(0, maxBytes - noticeBytes);
    let cut = blocks[0] ?? "";
    while (byteLength(cut) > budget && cut.length > 0) {
      cut = cut.slice(0, Math.max(0, Math.floor(cut.length * (budget / byteLength(cut)))));
    }
    return cut + TRUNCATION_NOTICE;
  }

  return kept.join(BLOCK_SEPARATOR) + TRUNCATION_NOTICE;
}

/**
 * Apply the 3-tier token budget. Tier 1 is always included.
 * Tier 2 fills to budget (with fallback support). Tier 3 if room remains.
 * Returns the assembled content within the budget.
 *
 * Tier 1 is exempt from the token check by design -- identity and corrections
 * are not optional -- so the byte ceiling is what ultimately bounds the
 * result. Without it, a tier-1 block that grows over time (the framework
 * lists grow with the store) pushes the output past the budget unchecked.
 */
export function applyTokenBudget(
  blocks: ContentBlock[],
  maxTokens: number,
  maxBytes: number = DEFAULT_MAX_BYTES,
): string {
  const tier1 = blocks.filter((b) => b.tier === 1);
  const tier2 = blocks.filter((b) => b.tier === 2);
  const tier3 = blocks.filter((b) => b.tier === 3);

  const result: string[] = [];
  let usedTokens = 0;

  // Tier 1: always include
  for (const block of tier1) {
    const tokens = estimateTokens(block.content);
    result.push(block.content);
    usedTokens += tokens;
  }

  // Tier 2: fill to budget, try fallback if primary doesn't fit
  for (const block of tier2) {
    const tokens = estimateTokens(block.content);
    if (usedTokens + tokens <= maxTokens) {
      result.push(block.content);
      usedTokens += tokens;
    } else if (block.fallback) {
      const fallbackTokens = estimateTokens(block.fallback);
      if (usedTokens + fallbackTokens <= maxTokens) {
        result.push(block.fallback);
        usedTokens += fallbackTokens;
      }
    }
  }

  // Tier 3: if room remains
  for (const block of tier3) {
    const tokens = estimateTokens(block.content);
    if (usedTokens + tokens <= maxTokens) {
      result.push(block.content);
      usedTokens += tokens;
    }
  }

  return clampToByteCeiling(result, maxBytes);
}
