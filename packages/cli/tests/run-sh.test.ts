import { describe, it, expect } from "vitest";
import { resolveBash, gitBashCandidates } from "../hooks/run-sh.js";

const WIN_ENV = {
  ProgramFiles: "C:\\Program Files",
  "ProgramFiles(x86)": "C:\\Program Files (x86)",
  LOCALAPPDATA: "C:\\Users\\jane\\AppData\\Local",
} as NodeJS.ProcessEnv;

/** Only the listed paths "exist". */
const only =
  (...paths: string[]) =>
  (p: string) =>
    paths.includes(p);

const win = (over: Record<string, unknown> = {}) =>
  resolveBash({
    platform: "win32",
    env: WIN_ENV,
    homedir: "C:\\Users\\jane",
    isFile: () => false,
    ...over,
  });

describe("resolveBash — POSIX", () => {
  it("returns bare bash on linux regardless of what exists on disk", () => {
    expect(resolveBash({ platform: "linux", isFile: () => true, env: WIN_ENV })).toBe("bash");
  });

  it("returns bare bash on darwin", () => {
    expect(resolveBash({ platform: "darwin", isFile: () => true })).toBe("bash");
  });
});

describe("resolveBash — Windows", () => {
  it("returns the absolute Git Bash path when it exists", () => {
    expect(win({ isFile: only("C:\\Program Files\\Git\\bin\\bash.exe") })).toBe(
      "C:\\Program Files\\Git\\bin\\bash.exe",
    );
  });

  // The path is handed to spawn() as argv[0], never to a shell, so it must
  // come back raw: no quoting, no escaping, no slash rewriting. Adding any of
  // that would make spawn look for a file whose name contains quote marks.
  it("returns the path unquoted and unescaped for direct spawn", () => {
    const bash = win({ isFile: only("C:\\Program Files\\Git\\bin\\bash.exe") });
    expect(bash.startsWith('"')).toBe(false);
    expect(bash).not.toContain('"');
    expect(bash).toContain("Program Files");
  });

  it("prefers CLAUDE_CODE_GIT_BASH_PATH over the standard install locations", () => {
    const override = "D:\\tools\\git\\bin\\bash.exe";
    expect(
      win({
        env: { ...WIN_ENV, CLAUDE_CODE_GIT_BASH_PATH: override },
        isFile: only(override, "C:\\Program Files\\Git\\bin\\bash.exe"),
      }),
    ).toBe(override);
  });

  it("skips CLAUDE_CODE_GIT_BASH_PATH when it points at nothing", () => {
    expect(
      win({
        env: { ...WIN_ENV, CLAUDE_CODE_GIT_BASH_PATH: "D:\\gone\\bash.exe" },
        isFile: only("C:\\Program Files\\Git\\bin\\bash.exe"),
      }),
    ).toBe("C:\\Program Files\\Git\\bin\\bash.exe");
  });

  it("falls back to usr/bin when only that layout is present", () => {
    expect(win({ isFile: only("C:\\Program Files\\Git\\usr\\bin\\bash.exe") })).toBe(
      "C:\\Program Files\\Git\\usr\\bin\\bash.exe",
    );
  });

  it("finds a per-user (non-admin) Git install under LOCALAPPDATA", () => {
    const p = "C:\\Users\\jane\\AppData\\Local\\Programs\\Git\\bin\\bash.exe";
    expect(win({ isFile: only(p) })).toBe(p);
  });

  it("falls back to bare bash when no Git Bash is installed", () => {
    expect(win()).toBe("bash");
  });

  // The bug this launcher exists to prevent: the WSL launcher lives on PATH as
  // `bash` but cannot open a C:/ path, and fails with a misleading
  // "No such file or directory" naming a script that is plainly there.
  it("never selects the WSL launcher in System32", () => {
    const bash = win({
      isFile: only("C:\\Windows\\System32\\bash.exe", "C:\\Program Files\\Git\\bin\\bash.exe"),
    });
    expect(bash).not.toContain("System32");
    expect(bash).toBe("C:\\Program Files\\Git\\bin\\bash.exe");
  });

  it("does not fall back to System32 even when Git Bash is absent", () => {
    expect(win({ isFile: only("C:\\Windows\\System32\\bash.exe") })).toBe("bash");
  });
});

describe("gitBashCandidates", () => {
  it("honours ProgramFiles relocation", () => {
    expect(
      gitBashCandidates({ env: { ...WIN_ENV, ProgramFiles: "E:\\Apps" }, homedir: "C:\\Users\\jane" }),
    ).toContain("E:\\Apps\\Git\\bin\\bash.exe");
  });

  it("derives LOCALAPPDATA from homedir when the env var is unset", () => {
    expect(gitBashCandidates({ env: {}, homedir: "C:\\Users\\jane" })).toContain(
      "C:\\Users\\jane\\AppData\\Local\\Programs\\Git\\bin\\bash.exe",
    );
  });

  it("omits CLAUDE_CODE_GIT_BASH_PATH when unset rather than yielding undefined", () => {
    const candidates = gitBashCandidates({ env: WIN_ENV, homedir: "C:\\Users\\jane" });
    expect(candidates.every((c) => typeof c === "string" && c.length > 0)).toBe(true);
    expect(candidates[0]).toBe("C:\\Program Files\\Git\\bin\\bash.exe");
  });
});
