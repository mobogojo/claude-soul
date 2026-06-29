#!/usr/bin/env node

/**
 * Reflection background worker — spawned detached by on-stop.ts so Claude Code
 * is never blocked waiting for the LLM reflection call to complete.
 *
 * Usage: node reflect-worker.js <tier>
 */

import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { runReflection } from "../engine/reflection-runner.js";
import type { ReflectionTier } from "../types/learning-types.js";

const tier = process.argv[2] as ReflectionTier;
if (tier !== "quick" && tier !== "deep") process.exit(1);

const logPath = path.join(os.tmpdir(), "soul-hook.log");
const log = (msg: string) =>
  fs.appendFile(logPath, `[soul] ${msg}\n`, "utf-8").catch(() => {});

const lockPath = path.join(os.tmpdir(), "soul-reflect.lock");

async function tryAcquireLock(): Promise<boolean> {
  const direct = await fs
    .writeFile(lockPath, String(process.pid), { flag: "wx" })
    .then(() => true)
    .catch(() => false);
  if (direct) return true;

  const ownerPid = parseInt(
    await fs.readFile(lockPath, "utf-8").catch(() => ""),
    10,
  );
  if (!Number.isFinite(ownerPid)) return false;

  const ownerAlive = await new Promise<boolean>((resolve) => {
    try {
      process.kill(ownerPid, 0);
      resolve(true);
    } catch (e: any) {
      resolve(e.code !== "ESRCH");
    }
  });
  if (ownerAlive) return false;

  await fs.unlink(lockPath).catch(() => {});
  return fs
    .writeFile(lockPath, String(process.pid), { flag: "wx" })
    .then(() => true)
    .catch(() => false);
}

async function main() {
  const lockAcquired = await tryAcquireLock();
  if (!lockAcquired) {
    await log("Reflection skipped — lock held by another process.");
    return;
  }
  try {
    const result = await runReflection(tier);
    await log(
      `${tier} reflection complete: ${result.frameworksUpdated} updated, ` +
        `${result.newFrameworks} new, ${result.retired} retired, ` +
        `${result.lessonsGenerated} lessons.`,
    );
  } catch (err) {
    await log(`${tier} reflection failed: ${err}`);
  } finally {
    await fs.unlink(lockPath).catch(() => {});
  }
}

main().then(() => process.exit(0)).catch(() => process.exit(1));
