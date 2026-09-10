import { describe, it, expect } from "vitest";
import { quotePath, nodeHookCommand, shellHookCommand } from "../src/util/hook-command.js";

const HOOKS = "C:/Users/jane/.soul/hooks";

describe("quotePath", () => {
  it("quotes so a directory with spaces survives the shell", () => {
    expect(quotePath("C:\\Program Files\\Git\\bin\\bash.exe")).toBe(
      '"C:/Program Files/Git/bin/bash.exe"',
    );
  });

  it("emits forward slashes only — backslashes are escapes inside bash quotes", () => {
    expect(quotePath("C:\\Users\\jane\\.soul\\hooks\\write-guard.sh")).not.toContain("\\");
  });
});

describe("hook command shape", () => {
  const commands = [
    nodeHookCommand(HOOKS, "session-agency.js"),
    shellHookCommand(HOOKS, "session-journal.sh"),
    shellHookCommand(HOOKS, "session-scratchpad.sh"),
    shellHookCommand(HOOKS, "write-guard.sh"),
  ];

  // The regression this pins. Claude Code hands the command to a shell, and on
  // Windows that is PowerShell in some environments and Git Bash in others.
  // PowerShell treats a leading quoted string as an expression, not a command,
  // and fails to parse the argument that follows it:
  //   At line:1 char:37
  //   Unexpected token '"…/write-guard.sh"' in expression or statement.
  // A bare first token is the only spelling both shells agree on.
  it.each(commands)("starts with a bare, unquoted command token: %s", (command) => {
    expect(command.startsWith('"')).toBe(false);
    expect(command.split(" ")[0]).toBe("node");
  });

  // The other half of the same trap: `& "…"` fixes PowerShell and breaks bash
  // with `syntax error near unexpected token '&'`.
  it.each(commands)("uses no PowerShell call operator: %s", (command) => {
    expect(command).not.toContain("&");
  });

  // The original bug: a bare `bash` is resolved off PATH, where the WSL
  // launcher in System32 answers to that name and cannot open a C:/ path.
  it.each(commands)("never invokes bash through PATH: %s", (command) => {
    expect(command).not.toMatch(/(^|\s)bash(\s|$)/);
  });
});

describe("shellHookCommand", () => {
  it("routes the script through the launcher rather than invoking it directly", () => {
    expect(shellHookCommand(HOOKS, "write-guard.sh")).toBe(
      `node "${HOOKS}/run-sh.js" "${HOOKS}/write-guard.sh"`,
    );
  });

  it("passes the script as a separate quoted argument, not concatenated", () => {
    const command = shellHookCommand(HOOKS, "session-journal.sh");
    expect(command).toContain(`"${HOOKS}/run-sh.js" "${HOOKS}/session-journal.sh"`);
  });
});
