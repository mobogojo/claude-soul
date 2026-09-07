/**
 * Builds the `command` strings registered in Claude Code's settings.json.
 *
 * Claude Code hands each command to a shell, and on Windows which shell that
 * is varies by environment -- PowerShell in some sessions, Git Bash in others.
 * They disagree about a command that begins with a quoted path, and no single
 * spelling satisfies both:
 *
 *   "C:/Program Files/Git/bin/bash.exe" "…/write-guard.sh"
 *     bash       -> runs
 *     PowerShell -> At line:1 char:37
 *                   Unexpected token '"…/write-guard.sh"' in expression or
 *                   statement.   (a leading quoted string is an expression,
 *                                 not a command)
 *
 *   & "C:/Program Files/Git/bin/bash.exe" "…/write-guard.sh"
 *     PowerShell -> runs
 *     bash       -> syntax error near unexpected token `&'
 *
 * The quotes are unavoidable -- the default Git install path contains a space
 * -- so the shell is kept out of it entirely: every command starts with a
 * bare `node`, which both shells parse identically, and run-sh.js spawns bash
 * by argv from there.
 */

/** Quote a path for the settings command. Forward slashes only. */
export function quotePath(p: string): string {
  return `"${p.replace(/\\/g, "/")}"`;
}

/** Command for a node hook: `node "<hooksDir>/<script>"`. */
export function nodeHookCommand(hooksDirFwd: string, script: string): string {
  return `node ${quotePath(`${hooksDirFwd}/${script}`)}`;
}

/** Command for a shell hook, routed through the launcher. */
export function shellHookCommand(hooksDirFwd: string, script: string): string {
  return `${nodeHookCommand(hooksDirFwd, "run-sh.js")} ${quotePath(`${hooksDirFwd}/${script}`)}`;
}
