import { Ollama } from "ollama";
import { spawn } from "node:child_process";

const MODEL = "nomic-embed-text";

let _client: Ollama | null = null;
let _fallbackMode: boolean | null = null; // null = not yet checked
let _fallbackWarned = false;

function getClient(): Ollama {
  if (!_client) {
    _client = new Ollama();
  }
  return _client;
}

function logFallbackWarning(): void {
  if (_fallbackWarned) return;
  _fallbackWarned = true;
  console.error(
    "[claude-soul] Ollama not available — memory will use keyword search.\n" +
      "  For semantic search, install Ollama: https://ollama.com\n" +
      `  Then run: ollama pull ${MODEL}`,
  );
}

export function isUsingFallback(): boolean {
  return _fallbackMode === true;
}

// Probe Ollama with a 2s timeout to avoid hanging hooks.
async function probe(): Promise<boolean> {
  const client = getClient();
  const timeout = new Promise<never>((_, reject) =>
    setTimeout(() => reject(new Error("Ollama timeout")), 2000),
  );
  const models = await Promise.race([client.list(), timeout]);
  return models.models.some((m) => m.name.startsWith(MODEL));
}

async function ensureOllama(): Promise<boolean> {
  if (_fallbackMode === true) return false;
  if (_fallbackMode === false) return true;

  try {
    const hasModel = await probe();
    if (!hasModel) {
      _fallbackMode = true;
      logFallbackWarning();
      return false;
    }
    _fallbackMode = false;
    return true;
  } catch {
    // Not reachable — try to auto-start Ollama, then retry once.
    try {
      spawn("ollama", ["serve"], { detached: true, stdio: "ignore", windowsHide: true }).unref();
    } catch {
      // ollama not on PATH — skip auto-start
    }
    await new Promise<void>((r) => setTimeout(r, 2500));

    try {
      const hasModel = await probe();
      if (hasModel) {
        _fallbackMode = false;
        return true;
      }
    } catch {
      // still not up after auto-start attempt
    }

    _fallbackMode = true;
    logFallbackWarning();
    return false;
  }
}

export async function embed(text: string): Promise<Float32Array | null> {
  const available = await ensureOllama();
  if (!available) return null;

  const client = getClient();
  const response = await client.embed({ model: MODEL, input: text });
  return new Float32Array(response.embeddings[0]);
}

export async function embedMany(texts: string[]): Promise<(Float32Array | null)[]> {
  if (texts.length === 0) return [];

  const available = await ensureOllama();
  if (!available) return texts.map(() => null);

  const client = getClient();
  const response = await client.embed({ model: MODEL, input: texts });
  return response.embeddings.map((e) => new Float32Array(e));
}

export function cosineSimilarity(a: Float32Array, b: Float32Array): number {
  let dot = 0;
  let normA = 0;
  let normB = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }
  const denom = Math.sqrt(normA) * Math.sqrt(normB);
  return denom === 0 ? 0 : dot / denom;
}

export function embeddingToBuffer(embedding: Float32Array): Buffer {
  return Buffer.from(embedding.buffer);
}

export function bufferToEmbedding(buffer: Buffer): Float32Array {
  return new Float32Array(buffer.buffer, buffer.byteOffset, buffer.byteLength / 4);
}

export async function checkOllama(): Promise<{ ok: boolean; error?: string }> {
  try {
    const client = getClient();
    const timeout = new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error("Ollama timeout")), 5000),
    );
    const models = await Promise.race([client.list(), timeout]);
    const hasModel = models.models.some((m) => m.name.startsWith(MODEL));
    if (!hasModel) {
      return {
        ok: false,
        error: `Model '${MODEL}' not found. Run: ollama pull ${MODEL}`,
      };
    }
    return { ok: true };
  } catch (e: any) {
    if (e?.message === "Ollama timeout") {
      return { ok: false, error: "Ollama timed out — is it responding?" };
    }
    return {
      ok: false,
      error: `Ollama not reachable. Is it running? Try: ollama serve`,
    };
  }
}
