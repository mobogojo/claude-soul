import fsSync from "node:fs";
import path from "node:path";
import os from "node:os";

// Resolve the `bash` used to run the shell hooks (`session-journal.sh`,
// `session-scratchpad.sh`, `write-guard.sh`).
//
// On Windows a bare `bash` is ambiguous and resolves off PATH. Two very
// different binaries commonly answer to that name:
//
//   C:\Program Files\Git\bin\bash.exe   -- Git Bash; understands `C:/Users/...`
//   C:\Windows\System32\bash.exe        -- the WSL launcher; does NOT
//
// Whichever appears first on the PATH of the process that spawns the hook
// wins, and that process is not always Claude Code -- an MCP server hosted
// inside WSL, for example, hands its children a PATH where System32 comes
// first. The hook then runs under WSL's bash, where `C:/Users/...` is not a
// path at all, and fails with:
//
//   /bin/bash: C:/Users/<user>/.soul/hooks/write-guard.sh: No such file or directory
//
// The hook file is fine; only the interpreter was wrong. So on Windows we
// bake an absolute Git Bash path into the settings command instead of
// trusting PATH. On POSIX, plain `bash` is correct and unambiguous.

/** Injection seams — defaulted to the real environment, overridden in tests. */
export interface BashResolveOptions {
  platform?: NodeJS.Platform;
  env?: NodeJS.ProcessEnv;
  homedir?: string;
  /** Returns true when `p` exists and is a regular file. */
  isFile?: (p: string) => boolean;
}

function defaultIsFile(p: string): boolean {
  try {
    return fsSync.statSync(p).isFile();
  } catch {
    return false;
  }
}

/**
 * Absolute paths to try for Git Bash on Windows, in priority order.
 * Exported for tests.
 */
export function gitBashCandidates(options: BashResolveOptions = {}): string[] {
  const env = options.env ?? process.env;
  const home = options.homedir ?? os.homedir();

  const programFiles = env.ProgramFiles ?? "C:\\Program Files";
  const programFilesX86 = env["ProgramFiles(x86)"] ?? "C:\\Program Files (x86)";
  const localAppData = env.LOCALAPPDATA ?? path.win32.join(home, "AppData", "Local");

  return [
    // Claude Code's own override, when the user has set it.
    env.CLAUDE_CODE_GIT_BASH_PATH,
    path.win32.join(programFiles, "Git", "bin", "bash.exe"),
    path.win32.join(programFiles, "Git", "usr", "bin", "bash.exe"),
    path.win32.join(programFilesX86, "Git", "bin", "bash.exe"),
    path.win32.join(localAppData, "Programs", "Git", "bin", "bash.exe"),
  ].filter((p): p is string => Boolean(p));
}

/**
 * The command prefix that invokes bash for a hook: `bash` on POSIX, or a
 * quoted absolute Git Bash path on Windows. Always safe to concatenate with
 * an already-quoted script path.
 *
 * Falls back to bare `bash` when no Git Bash is found -- it may still work if
 * the PATH happens to be right, and it keeps `init`/`upgrade` from hard-
 * failing on a machine where the shell hooks simply aren't usable.
 */
export function resolveBashCommand(options: BashResolveOptions = {}): string {
  const platform = options.platform ?? process.platform;
  if (platform !== "win32") return "bash";

  const isFile = options.isFile ?? defaultIsFile;

  for (const candidate of gitBashCandidates(options)) {
    if (isFile(candidate)) {
      // Forward slashes so the path survives both cmd and bash quoting.
      return `"${candidate.replace(/\\/g, "/")}"`;
    }
  }

  return "bash";
}
