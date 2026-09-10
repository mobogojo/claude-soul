import { describe, it, expect } from "vitest";
import {
  applyTokenBudget,
  clampToByteCeiling,
  BLOCK_SEPARATOR,
  DEFAULT_MAX_BYTES,
  type ContentBlock,
} from "../src/engine/token-budget.js";
import { mergeConfig } from "../src/util/files.js";
import { DEFAULT_CONFIG } from "../src/types/config-types.js";

const bytes = (s: string) => Buffer.byteLength(s, "utf8");

describe("clampToByteCeiling", () => {
  it("returns content untouched when it fits", () => {
    const blocks = ["alpha", "beta"];
    expect(clampToByteCeiling(blocks, 1000)).toBe(blocks.join(BLOCK_SEPARATOR));
  });

  it("never exceeds the ceiling", () => {
    const blocks = ["a".repeat(500), "b".repeat(500), "c".repeat(500)];
    expect(bytes(clampToByteCeiling(blocks, 600))).toBeLessThanOrEqual(600);
  });

  it("drops whole blocks from the end rather than cutting mid-block", () => {
    const out = clampToByteCeiling(["keep", "b".repeat(5000)], 200);
    expect(out).toContain("keep");
    expect(out).not.toContain("bbbb");
  });

  it("says so when it truncates, so a partial context never reads as complete", () => {
    const out = clampToByteCeiling(["keep", "b".repeat(5000)], 200);
    expect(out).toContain("truncated");
  });

  it("keeps the highest-priority block when only one fits", () => {
    const out = clampToByteCeiling(["first", "second", "third"], 100);
    expect(out.startsWith("first")).toBe(true);
  });

  it("hard-truncates a single oversized block instead of returning it whole", () => {
    const out = clampToByteCeiling(["x".repeat(10000)], 500);
    expect(bytes(out)).toBeLessThanOrEqual(500);
    expect(out).toContain("truncated");
  });

  // Buffer.slice would cut mid-codepoint and emit U+FFFD.
  it("does not split multi-byte characters", () => {
    const out = clampToByteCeiling(["😀".repeat(1000)], 300);
    expect(bytes(out)).toBeLessThanOrEqual(300);
    expect(out).not.toContain("�");
  });

  it("handles an empty block list", () => {
    expect(clampToByteCeiling([], 100)).toBe("");
  });
});

describe("applyTokenBudget byte ceiling", () => {
  // The regression: tier 1 is exempt from the token check, so a tier-1 block
  // that grows over time pushed the payload past the budget unchecked. A
  // 22KB MCP response is large enough to be split across pipe reads, which
  // clients that do not reassemble partial lines fail to decode.
  it("bounds output even when tier 1 alone blows the token budget", () => {
    const blocks: ContentBlock[] = [
      { content: "z".repeat(50000), tier: 1, label: "Framework Vocabulary" },
      { content: "y".repeat(50000), tier: 1, label: "Cognitive Processes" },
    ];
    expect(bytes(applyTokenBudget(blocks, 4500, 16384))).toBeLessThanOrEqual(16384);
  });

  it("applies a default ceiling when the caller supplies none", () => {
    const blocks: ContentBlock[] = [{ content: "z".repeat(500000), tier: 1, label: "big" }];
    expect(bytes(applyTokenBudget(blocks, 4500))).toBeLessThanOrEqual(DEFAULT_MAX_BYTES);
  });

  it("leaves a normally-sized context unchanged", () => {
    const blocks: ContentBlock[] = [
      { content: "identity", tier: 1, label: "SOUL.md" },
      { content: "lessons", tier: 2, label: "Lessons" },
    ];
    const out = applyTokenBudget(blocks, 4500, 16384);
    expect(out).toBe(`identity${BLOCK_SEPARATOR}lessons`);
    expect(out).not.toContain("truncated");
  });
});

describe("mergeConfig", () => {
  // A config written at install predates any field added later. Returning it
  // verbatim left every new setting undefined, silently skipping the limits
  // they guard.
  it("fills in fields missing from an older stored config", () => {
    const stored = { contextBudget: { maxTokens: 4500 } };
    const merged = mergeConfig(stored, DEFAULT_CONFIG);
    expect(merged.contextBudget.maxTokens).toBe(4500);
    expect(merged.contextBudget.maxBytes).toBe(DEFAULT_CONFIG.contextBudget.maxBytes);
    expect(merged.contextBudget.maxFrameworkEntries).toBe(
      DEFAULT_CONFIG.contextBudget.maxFrameworkEntries,
    );
  });

  it("keeps user values rather than overwriting them with defaults", () => {
    const merged = mergeConfig({ contextBudget: { maxBytes: 999 } }, DEFAULT_CONFIG);
    expect(merged.contextBudget.maxBytes).toBe(999);
  });

  it("preserves sections the stored config omits entirely", () => {
    const merged = mergeConfig({ contextBudget: { maxTokens: 1 } }, DEFAULT_CONFIG);
    expect(merged.tensions).toEqual(DEFAULT_CONFIG.tensions);
  });

  it("falls back to defaults for a missing or malformed config", () => {
    expect(mergeConfig(null, DEFAULT_CONFIG)).toEqual(DEFAULT_CONFIG);
    expect(mergeConfig("nonsense", DEFAULT_CONFIG)).toEqual(DEFAULT_CONFIG);
    expect(mergeConfig([1, 2], DEFAULT_CONFIG)).toEqual(DEFAULT_CONFIG);
  });
});
