#!/usr/bin/env node
/**
 * Stop-hook wrapper for cleanup-transcript.py.
 * Claude Code runs hooks via bash on Windows, which eats backslashes in
 * unquoted C:\ paths. Other hooks already invoke node with quoted paths;
 * this file then launches Windows Python with a real Win32 script path.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";

const WIN_PY = "C:\\Python314\\python.exe";
const WSL_PY = "/mnt/c/Python314/python.exe";
const SCRIPT = "C:\\Users\\logan\\.soul\\hooks\\cleanup-transcript.py";

const py = process.platform === "win32"
  ? WIN_PY
  : existsSync(WSL_PY)
    ? WSL_PY
    : "python3";

const result = spawnSync(py, [SCRIPT, ...process.argv.slice(2)], {
  stdio: "inherit",
  timeout: 110_000,
  windowsHide: true,
});

if (result.error) {
  console.error(`cleanup-transcript: ${result.error.message}`);
  process.exit(0);
}
process.exit(result.status ?? 0);
