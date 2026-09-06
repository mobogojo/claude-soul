import { describe, it, expect } from "vitest";
import { resolveBashCommand, gitBashCandidates } from "../src/util/bash.js";

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

const win = (over: Partial<Parameters<typeof resolveBashCommand>[0]> = {}) =>
  resolveBashCommand({
    platform: "win32",
    env: WIN_ENV,
    homedir: "C:\\Users\\jane",
    isFile: () => false,
    ...over,
  });

describe("resolveBashCommand — POSIX", () => {
  it("returns bare bash on linux regardless of what exists on disk", () => {
    expect(
      resolveBashCommand({ platform: "linux", isFile: () => true, env: WIN_ENV }),
    ).toBe("bash");
  });

  it("returns bare bash on darwin", () => {
    expect(resolveBashCommand({ platform: "darwin", isFile: () => true })).toBe("bash");
  });
});

describe("resolveBashCommand — Windows", () => {
  it("returns a quoted, forward-slashed absolute path when Git Bash exists", () => {
    const cmd = win({ isFile: only("C:\\Program Files\\Git\\bin\\bash.exe") });
    expect(cmd).toBe('"C:/Program Files/Git/bin/bash.exe"');
  });

  it("quotes the path so a directory with spaces survives the shell", () => {
    const cmd = win({ isFile: only("C:\\Program Files\\Git\\bin\\bash.exe") });
    expect(cmd.startsWith('"')).toBe(true);
    expect(cmd.endsWith('"')).toBe(true);
    expect(cmd).toContain("Program Files");
  });

  it("emits no backslashes — they would be eaten as escapes inside bash quotes", () => {
    const cmd = win({ isFile: only("C:\\Program Files\\Git\\bin\\bash.exe") });
    expect(cmd).not.toContain("\\");
  });

  it("prefers CLAUDE_CODE_GIT_BASH_PATH over the standard install locations", () => {
    const override = "D:\\tools\\git\\bin\\bash.exe";
    const cmd = win({
      env: { ...WIN_ENV, CLAUDE_CODE_GIT_BASH_PATH: override },
      isFile: only(override, "C:\\Program Files\\Git\\bin\\bash.exe"),
    });
    expect(cmd).toBe('"D:/tools/git/bin/bash.exe"');
  });

  it("skips CLAUDE_CODE_GIT_BASH_PATH when it points at nothing", () => {
    const cmd = win({
      env: { ...WIN_ENV, CLAUDE_CODE_GIT_BASH_PATH: "D:\\gone\\bash.exe" },
      isFile: only("C:\\Program Files\\Git\\bin\\bash.exe"),
    });
    expect(cmd).toBe('"C:/Program Files/Git/bin/bash.exe"');
  });

  it("falls back to usr/bin when only that layout is present", () => {
    const cmd = win({ isFile: only("C:\\Program Files\\Git\\usr\\bin\\bash.exe") });
    expect(cmd).toBe('"C:/Program Files/Git/usr/bin/bash.exe"');
  });

  it("finds a per-user (non-admin) Git install under LOCALAPPDATA", () => {
    const cmd = win({
      isFile: only("C:\\Users\\jane\\AppData\\Local\\Programs\\Git\\bin\\bash.exe"),
    });
    expect(cmd).toBe('"C:/Users/jane/AppData/Local/Programs/Git/bin/bash.exe"');
  });

  it("falls back to bare bash when no Git Bash is installed", () => {
    expect(win()).toBe("bash");
  });

  // The bug this module exists to prevent: the WSL launcher lives on PATH as
  // `bash` but cannot open a C:/ path. It must never be selected.
  it("never selects the WSL launcher in System32", () => {
    const cmd = win({
      isFile: only("C:\\Windows\\System32\\bash.exe", "C:\\Program Files\\Git\\bin\\bash.exe"),
    });
    expect(cmd).not.toContain("System32");
    expect(cmd).toBe('"C:/Program Files/Git/bin/bash.exe"');
  });

  it("does not fall back to System32 even when Git Bash is absent", () => {
    const cmd = win({ isFile: only("C:\\Windows\\System32\\bash.exe") });
    expect(cmd).toBe("bash");
  });
});

describe("gitBashCandidates", () => {
  it("honours ProgramFiles relocation", () => {
    const candidates = gitBashCandidates({
      env: { ...WIN_ENV, ProgramFiles: "E:\\Apps" },
      homedir: "C:\\Users\\jane",
    });
    expect(candidates).toContain("E:\\Apps\\Git\\bin\\bash.exe");
  });

  it("derives LOCALAPPDATA from homedir when the env var is unset", () => {
    const candidates = gitBashCandidates({ env: {}, homedir: "C:\\Users\\jane" });
    expect(candidates).toContain(
      "C:\\Users\\jane\\AppData\\Local\\Programs\\Git\\bin\\bash.exe",
    );
  });

  it("omits CLAUDE_CODE_GIT_BASH_PATH when unset rather than yielding undefined", () => {
    const candidates = gitBashCandidates({ env: WIN_ENV, homedir: "C:\\Users\\jane" });
    expect(candidates.every((c) => typeof c === "string" && c.length > 0)).toBe(true);
    expect(candidates[0]).toBe("C:\\Program Files\\Git\\bin\\bash.exe");
  });
});
