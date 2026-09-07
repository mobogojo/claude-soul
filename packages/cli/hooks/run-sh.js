#!/usr/bin/env node
// Launcher for the shell hooks (session-journal.sh, session-scratchpad.sh,
// write-guard.sh).
//
// Claude Code hands a hook's `command` string to a shell, and on Windows
// which shell that is varies by environment -- PowerShell in some sessions,
// Git Bash in others. The two disagree about a command that starts with a
// quoted path, and there is no spelling that satisfies both:
//
//   "C:/Program Files/Git/bin/bash.exe" "…/write-guard.sh"
//     bash       -> runs
//     PowerShell -> At line:1 char:37
//                   Unexpected token '"…/write-guard.sh"' in expression or
//                   statement.   (a leading quoted string is an expression,
//                                 not a command)
//
//   & "C:/Program Files/Git/bin/bash.exe" "…/write-guard.sh"
//     PowerShell -> runs
//     bash       -> syntax error near unexpected token `&'
//
// Quoting is unavoidable because the default Git install path contains a
// space. So we keep the shell out of it: the registered command is
// `node "<abs>/run-sh.js" "<abs>/<script>.sh"`, whose first token is bare and
// parses identically in both shells, and this launcher spawns bash by argv --
// no quoting, no PATH lookup, no interpreter ambiguity.
//
// Resolution is deliberately duplicated from src/util/bash.ts rather than
// imported: this file is copied verbatim into ~/.soul/hooks and runs outside
// the package, with no node_modules to resolve against.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

function defaultIsFile(p) {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

/**
 * Absolute paths where Git Bash may live, most-preferred first.
 * `options` exists so the Windows branch is exercisable from a Linux CI runner:
 * { platform, env, homedir, isFile } all default to the real environment.
 */
function gitBashCandidates(options = {}) {
  const env = options.env || process.env;
  const home = options.homedir || os.homedir();

  const programFiles = env.ProgramFiles || "C:\\Program Files";
  const programFilesX86 = env["ProgramFiles(x86)"] || "C:\\Program Files (x86)";
  const localAppData = env.LOCALAPPDATA || path.win32.join(home, "AppData", "Local");

  return [
    // Claude Code's own override, when the user has set it.
    env.CLAUDE_CODE_GIT_BASH_PATH,
    path.win32.join(programFiles, "Git", "bin", "bash.exe"),
    path.win32.join(programFiles, "Git", "usr", "bin", "bash.exe"),
    path.win32.join(programFilesX86, "Git", "bin", "bash.exe"),
    path.win32.join(localAppData, "Programs", "Git", "bin", "bash.exe"),
  ].filter(Boolean);
}

/**
 * The bash to spawn: bare `bash` on POSIX, an absolute Git Bash path on
 * Windows. Never a PATH lookup on Windows -- that is how the WSL launcher in
 * System32 gets picked up. Falls back to bare `bash` when no Git Bash is
 * found: it may still work if PATH happens to be right, and that beats
 * hard-failing every hook on a machine without Git for Windows.
 */
function resolveBash(options = {}) {
  const platform = options.platform || process.platform;
  if (platform !== "win32") return "bash";

  const isFile = options.isFile || defaultIsFile;
  for (const candidate of gitBashCandidates(options)) {
    if (isFile(candidate)) return candidate;
  }
  return "bash";
}

function main(argv) {
  const script = argv[2];
  if (!script) {
    console.error("run-sh.js: no script argument");
    return 1;
  }

  // stdio inherit: hooks read the Claude Code payload on stdin and signal
  // through their exit code (2 blocks the tool call), so both pass straight
  // through untouched.
  const result = spawnSync(resolveBash(), [script, ...argv.slice(3)], {
    stdio: "inherit",
    windowsHide: true,
  });

  if (result.error) {
    console.error(`run-sh.js: could not run ${script}: ${result.error.message}`);
    return 1;
  }
  return result.status === null ? 1 : result.status;
}

export { resolveBash, gitBashCandidates };

// Only run when invoked as the hook; stays importable from tests.
if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  process.exit(main(process.argv));
}
