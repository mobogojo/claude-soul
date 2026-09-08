import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import type { SoulConfig } from "../types/config-types.js";
import { DEFAULT_CONFIG } from "../types/config-types.js";

export const SOUL_DIR = path.join(os.homedir(), ".soul");
export const DATA_DIR = path.join(SOUL_DIR, "data");
export const FILES_DIR = path.join(SOUL_DIR, "files");
export const SNAPSHOTS_DIR = path.join(DATA_DIR, "snapshots");
export const REFLECTIONS_DIR = path.join(SOUL_DIR, "reflections");

export const FRAMEWORKS_PATH = path.join(DATA_DIR, "frameworks.json");
export const SESSION_LOG_PATH = path.join(DATA_DIR, "session-log.jsonl");
export const STATE_PATH = path.join(DATA_DIR, "state.json");
export const TENSIONS_PATH = path.join(DATA_DIR, "tensions.json");
export const EXEMPLARS_PATH = path.join(DATA_DIR, "exemplars.json");
export const LESSONS_PATH = path.join(DATA_DIR, "lessons.json");
export const META_PATH = path.join(DATA_DIR, "meta.json");
export const CONFIG_PATH = path.join(SOUL_DIR, "config.json");

export async function ensureDirs(): Promise<void> {
  await fs.mkdir(DATA_DIR, { recursive: true });
  await fs.mkdir(FILES_DIR, { recursive: true });
  await fs.mkdir(SNAPSHOTS_DIR, { recursive: true });
  await fs.mkdir(REFLECTIONS_DIR, { recursive: true });
}

export async function readJsonSafe<T>(filePath: string, fallback: T): Promise<T> {
  try {
    const data = await fs.readFile(filePath, "utf-8");
    return JSON.parse(data) as T;
  } catch {
    return fallback;
  }
}

/**
 * Atomic text-content file write. Writes to a `.tmp.{pid}-{uuid}` sidecar in
 * the same directory, then renames into place — fs.rename is atomic on every
 * platform Node supports, including overwriting an existing target on Windows
 * (libuv MoveFileExW with MOVEFILE_REPLACE_EXISTING since Node 12.x).
 *
 * The PID + UUID combination prevents collisions between concurrent same-PID
 * callers (e.g. two `appendSignals` calls racing inside one node process).
 *
 * `content: string` is a deliberate constraint — this util always encodes UTF-8.
 * Callers needing Buffer/binary writes should use `fs.writeFile` directly.
 */
export async function writeFileAtomic(filePath: string, content: string): Promise<void> {
  const dir = path.dirname(filePath);
  await fs.mkdir(dir, { recursive: true });
  const tmpPath = `${filePath}.tmp.${process.pid}-${crypto.randomUUID()}`;
  try {
    await fs.writeFile(tmpPath, content, "utf-8");
    await fs.rename(tmpPath, filePath);
  } catch (err) {
    await fs.unlink(tmpPath).catch(() => {});
    throw err;
  }
}

export async function writeJsonAtomic(filePath: string, data: unknown): Promise<void> {
  await writeFileAtomic(filePath, JSON.stringify(data, null, 2));
}

export async function readFileSafe(filePath: string): Promise<string> {
  try {
    return await fs.readFile(filePath, "utf-8");
  } catch {
    return "";
  }
}

/**
 * Merge a stored config over the defaults, one level into each section.
 *
 * Configs are written once at install and then left alone, so a config on
 * disk is generally older than the code reading it and will be missing any
 * field added since. Returning it verbatim makes every new setting `undefined`
 * on existing installs -- silently skipping whatever it guards. Defaults fill
 * only the gaps; anything the user set wins.
 */
export function mergeConfig(stored: unknown, defaults: SoulConfig): SoulConfig {
  if (!stored || typeof stored !== "object" || Array.isArray(stored)) return defaults;

  const merged = { ...defaults } as Record<string, unknown>;
  for (const [key, value] of Object.entries(stored as Record<string, unknown>)) {
    if (value === undefined) continue;
    const base = (defaults as Record<string, unknown>)[key];
    const bothPlainObjects =
      base && typeof base === "object" && !Array.isArray(base) &&
      value && typeof value === "object" && !Array.isArray(value);
    merged[key] = bothPlainObjects
      ? { ...(base as object), ...(value as object) }
      : value;
  }
  return merged as SoulConfig;
}

export async function loadConfig(): Promise<SoulConfig> {
  const stored = await readJsonSafe<unknown>(CONFIG_PATH, null);
  return mergeConfig(stored, DEFAULT_CONFIG);
}

export function soulFilePath(name: string): string {
  return path.join(FILES_DIR, name);
}
