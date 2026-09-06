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
// wins, and that process is not always Claude Code itself -- an MCP server
// hosted inside WSL, for example, hands its children a PATH where System32
// comes first. The hook then runs under WSL's bash, where `C:/Users/...` is
// not a path at all, and fails with:
//
//   /bin/bash: C:/Users/<user>/.soul/hooks/write-guard.sh: No such file or directory
//
// The hook file is fine; only the interpreter was wrong. So on Windows we
// bake an absolute Git Bash path into the settings command instead of
// trusting PATH. On POSIX, plain `bash` is correct and unambiguous.
//
// Exported for tests.
export function gitBashCandidates(): string[] {
  const programFiles = process.env.ProgramFiles ?? "C:\\Program Files";
  const programFilesX86 = process.env["ProgramFiles(x86)"] ?? "C:\\Program Files (x86)";
  const localAppData = process.env.LOCALAPPDATA ?? path.join(os.homedir(), "AppData", "Local");

  return [
    // Claude Code's own override, when the user has set it.
    process.env.CLAUDE_CODE_GIT_BASH_PATH,
    path.join(programFiles, "Git", "bin", "bash.exe"),
    path.join(programFiles, "Git", "usr", "bin", "bash.exe"),
    path.join(programFilesX86, "Git", "bin", "bash.exe"),
    path.join(localAppData, "Programs", "Git", "bin", "bash.exe"),
  ].filter((p): p is string => Boolean(p));
}

/**
 * The command prefix that invokes bash for a hook, e.g. `bash` on POSIX or
 * `"C:/Program Files/Git/bin/bash.exe"` on Windows. Always safe to
 * concatenate with an already-quoted script path.
 */
export function resolveBashCommand(): string {
  if (process.platform !== "win32") return "bash";

  for (const candidate of gitBashCandidates()) {
    try {
      if (fsSync.statSync(candidate).isFile()) {
        // Forward slashes so the path survives both cmd and bash quoting.
        return `"${candidate.replace(/\\/g, "/")}"`;
      }
    } catch {
      /* not installed at this location */
    }
  }

  // No Git Bash found. Fall back to bare `bash` -- it may still work if the
  // PATH happens to be right, and it keeps `init`/`upgrade` from hard-failing
  // on a machine where the shell hooks simply aren't usable.
  return "bash";
}
