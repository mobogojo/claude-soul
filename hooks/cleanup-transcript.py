#!/usr/bin/env python3
"""Shrink Claude Code session JSONL so it never hits Node's ~512MB string cap.

Streaming (never loads the whole file as one string):
  - Strip all user image blocks (base64 screenshots)
  - Drop file-history-snapshot records older than --keep-days
  - Stub user tool_result payloads older than --keep-days (keep the event/uuid)

Runs when either:
  - the file is at least --threshold-mb (500), or
  - --interval-days (14) have passed since the last successful cleanup of that file.

Keep-days matches the interval (last 14 days of snapshots/tool results). Images are
always stripped. Stop hook and a biweekly scheduled task both invoke this script.

Never rewrites a session that is still open. Cleanup reads the file, builds a
replacement alongside it, then renames over the original -- so anything the
owning session appends in between is silently lost, and an already-open handle
keeps writing to the unlinked inode. The Stop hook fires while its own session
is very much alive and about to append again, so the session named on stdin is
*excluded* rather than targeted, and any transcript touched within
--active-grace-min is left alone as presumed live. --force overrides the size
and interval triggers, not this; use --allow-active for that, and only when no
session is running.

Usage:
  python cleanup-transcript.py PATH.jsonl
  python cleanup-transcript.py --force PATH.jsonl
  python cleanup-transcript.py            # stdin Stop-hook JSON, or scan ~/.claude/projects
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_THRESHOLD_MB = 500
DEFAULT_KEEP_DAYS = 14
DEFAULT_INTERVAL_DAYS = 14
# A transcript touched more recently than this is presumed to belong to an open
# session. Claude Code appends on every turn, so idle-for-30-minutes is a
# conservative stand-in for "nobody is writing to this".
DEFAULT_ACTIVE_GRACE_MIN = 30
STATE_FILE = Path.home() / ".soul" / "data" / "transcript-cleanup.json"
IMAGE_PLACEHOLDER = "[image removed by transcript cleanup]"
TOOL_PLACEHOLDER = "[trimmed by transcript cleanup: tool result older than keep-days]"
REDACTED = "xxxx-REDACTED-xxxx"

# ---------------------------------------------------------------------------
# Secret redaction
#
# Printing an env dump into a session puts live credentials into the transcript,
# where they are read again on every --resume and by anything that indexes the
# file. Redaction removes that ongoing exposure. It does NOT undo the original
# exposure -- the value was already sent to the API when it entered context --
# so a hit here still means rotate.
#
# Two tiers, because over-redaction quietly destroys the transcript:
#   1. Shapes that are secrets by construction (AKIA…, ghp_…, PEM blocks).
#      Always redacted.
#   2. High-entropy blobs, which look identical to git SHAs, checksums, base64
#      payloads and minified JS. Only redacted when a secret-ish word appears
#      just before them.
# ---------------------------------------------------------------------------

_SECRET_WORD = (
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY"
    # AUTH must not run into more letters, or "Co-Authored-By" and "author"
    # match and the commit trailer gets redacted.
    r"|CREDENTIAL|CONNECTION[_-]?STRING|AUTH(?![A-Za-z])|BEARER"
    r"|SESSION[_-]?KEY|CLIENT[_-]?SECRET)"
)

# NAME=value / NAME: value — the env-dump case.
ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_.\-]*" + _SECRET_WORD + r"[A-Z0-9_.\-]*)"  # 1: key (kept)
    r"(\s*[=:]\s*)"                                              # 2: separator
    r"([\"']?)"                                                  # 3: optional quote
    r"([^\s\"'`,;]{6,})"                                         # 4: value (dropped)
    r"\3"
)

# (name, pattern, group to replace; 0 = whole match)
SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]", int]] = [
    ("pem-private-key",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), 0),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("github-token",
     re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"), 0),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    ("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), 0),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("anthropic-openai-key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}"), 0),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"), 0),
    # user:password@host — replace only the password group.
    ("url-credentials", re.compile(r"(://[^/\s:@\"']+:)([^/\s:@\"']{4,})(@)"), 2),
]

# Values that match the assignment shape but carry no secret.
_NOT_SECRET_VALUES = {
    "bearer", "basic", "digest", "none", "null", "true", "false", "undefined",
    "changeme", "password", "secret", "redacted", "xxxxx", "example",
}

HIGH_ENTROPY_RE = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")
SECRET_CONTEXT_RE = re.compile(r"(?i)" + _SECRET_WORD)
ENTROPY_CONTEXT_CHARS = 120
ENTROPY_THRESHOLD = 4.0


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    from collections import Counter
    from math import log2

    n = len(s)
    return -sum((c / n) * log2(c / n) for c in Counter(s).values())


def _redact_high_entropy(text: str, bump) -> str:
    """Redact long high-entropy runs, but only near secret-ish wording.

    Entropy alone cannot tell an API key from a commit SHA, so the surrounding
    text has to vouch for it.
    """
    out: list[str] = []
    last = 0
    for m in HIGH_ENTROPY_RE.finditer(text):
        blob = m.group(0)
        if REDACTED in blob or shannon_entropy(blob) < ENTROPY_THRESHOLD:
            continue
        context = text[max(0, m.start() - ENTROPY_CONTEXT_CHARS):m.start()]
        if not SECRET_CONTEXT_RE.search(context):
            continue
        out.append(text[last:m.start()])
        out.append(REDACTED)
        last = m.end()
        bump("high-entropy-near-secret")
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Return the text with secrets replaced, plus a count by kind."""
    found: dict[str, int] = {}

    def bump(kind: str) -> None:
        found[kind] = found.get(kind, 0) + 1

    def _assignment(m: "re.Match[str]") -> str:
        value = m.group(4)
        # "Authorization: Bearer <jwt>" would otherwise redact the word Bearer
        # and leave the token to a later pattern. Scheme words and placeholders
        # are not secrets; skipping them keeps the transcript readable.
        if (
            value.lower() in _NOT_SECRET_VALUES
            or REDACTED in value
            # Docs and templates, not credentials: <your-token>, ${VAR}, YOUR_KEY.
            or value.startswith(("<", "{", "$", "%", "your", "YOUR", "***", "xxx"))
        ):
            return m.group(0)
        bump("env-assignment")
        # Keep the key so the transcript still reads sensibly.
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}"

    text = ASSIGNMENT_RE.sub(_assignment, text)

    for kind, pattern, group in SECRET_PATTERNS:
        def _sub(m: "re.Match[str]", kind=kind, group=group) -> str:
            bump(kind)
            if group == 0:
                return REDACTED
            whole, base = m.group(0), m.start(0)
            return whole[: m.start(group) - base] + REDACTED + whole[m.end(group) - base :]

        text = pattern.sub(_sub, text)

    return _redact_high_entropy(text, bump), found


def redact_obj(obj: object) -> tuple[object, dict[str, int]]:
    """Redact string values in a parsed record, leaving structure untouched.

    Walking the parsed object rather than the raw line keeps JSON escaping
    valid and cannot disturb tool_use/tool_result pairing.
    """
    found: dict[str, int] = {}

    def merge(counts: dict[str, int]) -> None:
        for k, v in counts.items():
            found[k] = found.get(k, 0) + v

    def walk(node: object) -> object:
        if isinstance(node, str):
            new, counts = redact_text(node)
            merge(counts)
            return new
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    return walk(obj), found


def parse_ts(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_old(ts: datetime | None, cutoff: datetime) -> bool:
    return ts is not None and ts < cutoff


def strip_images(content: object) -> tuple[object, bool]:
    if not isinstance(content, list):
        return content, False
    kept = []
    removed = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            removed = True
            continue
        kept.append(block)
    if removed and not kept:
        return IMAGE_PLACEHOLDER, True
    return kept, removed


def has_tool_result(content: object) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def stub_tool_results(obj: dict) -> None:
    msg = obj.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                block["content"] = TOOL_PLACEHOLDER
    if "toolUseResult" in obj:
        obj["toolUseResult"] = TOOL_PLACEHOLDER


def process_file(path: Path, cutoff: datetime, dry_run: bool) -> dict:
    stats = {
        "in_bytes": path.stat().st_size,
        "out_bytes": 0,
        "in_lines": 0,
        "out_lines": 0,
        "dropped_snapshots": 0,
        "kept_snapshots": 0,
        "stripped_images": 0,
        "stubbed_tools": 0,
        "user_kept": 0,
        "replaced": False,
    }
    tmp_path = path.with_name(path.name + ".cleanup-tmp")
    try:
        with path.open("rb") as src, tmp_path.open("wb") as dst:
            for raw in src:
                stats["in_lines"] += 1
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    dst.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    stats["out_lines"] += 1
                    continue
                if not isinstance(obj, dict):
                    dst.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    stats["out_lines"] += 1
                    continue

                kind = obj.get("type")
                if kind == "file-history-snapshot":
                    ts = parse_ts((obj.get("snapshot") or {}).get("timestamp"))
                    if is_old(ts, cutoff):
                        stats["dropped_snapshots"] += 1
                        continue
                    stats["kept_snapshots"] += 1
                    dst.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    stats["out_lines"] += 1
                    continue

                if kind == "user":
                    msg = obj.get("message")
                    content = msg.get("content") if isinstance(msg, dict) else None
                    new_content, img_removed = strip_images(content)
                    changed = False
                    if img_removed and isinstance(msg, dict):
                        stats["stripped_images"] += 1
                        msg["content"] = new_content
                        content = new_content
                        changed = True
                    ts = parse_ts(obj.get("timestamp"))
                    if has_tool_result(content) and is_old(ts, cutoff):
                        stub_tool_results(obj)
                        stats["stubbed_tools"] += 1
                        changed = True
                    if changed:
                        out = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
                        dst.write(out.encode("utf-8"))
                    else:
                        dst.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    stats["out_lines"] += 1
                    continue

                dst.write(raw if raw.endswith(b"\n") else raw + b"\n")
                stats["out_lines"] += 1

        stats["out_bytes"] = tmp_path.stat().st_size if tmp_path.exists() else 0
        if dry_run:
            tmp_path.unlink(missing_ok=True)
            return stats
        os.replace(tmp_path, path)
        stats["replaced"] = True
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return stats


def safe_replace(tmp_path: Path, path: Path, expected: tuple[int, int]) -> bool:
    """Rename tmp over path only if path has not changed since it was read.

    Trimming avoids the live session entirely, but redaction cannot wait for a
    session to close -- a secret sitting in an open transcript is read again on
    every resume. So instead of assuming the file is idle, verify it: if the
    session appended while we were building the replacement, throw the
    replacement away and try again next run. Losing a redaction pass is
    recoverable; losing appended conversation is not.
    """
    try:
        stat = path.stat()
    except OSError:
        tmp_path.unlink(missing_ok=True)
        return False
    if (stat.st_size, stat.st_mtime_ns) != expected:
        tmp_path.unlink(missing_ok=True)
        return False
    try:
        os.replace(tmp_path, path)
        return True
    except OSError:
        # Windows can refuse the rename while another process holds the file.
        tmp_path.unlink(missing_ok=True)
        return False


def scan_for_secrets(path: Path, start: int) -> dict[str, int]:
    """Cheap detection pass over bytes appended since the last scan.

    Transcripts only grow, so rescanning from zero on every Stop would mean
    re-reading hundreds of MB to find nothing. Scan the new tail; a full
    rewrite only happens when this finds something.
    """
    found: dict[str, int] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            if start:
                fh.seek(start)
                fh.readline()  # align to a line boundary
            for line in fh:
                _, counts = redact_text(line)
                for k, v in counts.items():
                    found[k] = found.get(k, 0) + v
    except OSError:
        return {}
    return found


def redact_file(path: Path, dry_run: bool) -> dict:
    """Rewrite the transcript with secrets replaced. Structure is preserved."""
    stats: dict = {"found": {}, "replaced": False, "lines": 0}
    try:
        before = path.stat()
    except OSError:
        return stats
    expected = (before.st_size, before.st_mtime_ns)

    tmp_path = path.with_name(path.name + ".redact-tmp")
    try:
        with path.open("rb") as src, tmp_path.open("wb") as dst:
            for raw in src:
                stats["lines"] += 1
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    text, counts = redact_text(raw.decode("utf-8", "replace"))
                    out = text if text.endswith("\n") else text + "\n"
                    dst.write(out.encode("utf-8"))
                else:
                    obj, counts = redact_obj(obj)
                    out = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
                    dst.write(out.encode("utf-8"))
                for k, v in counts.items():
                    stats["found"][k] = stats["found"].get(k, 0) + v

        if dry_run or not stats["found"]:
            tmp_path.unlink(missing_ok=True)
            return stats
        stats["replaced"] = safe_replace(tmp_path, path, expected)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return stats


def load_state() -> dict:
    if not STATE_FILE.is_file():
        return {"files": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except Exception:
        pass
    return {"files": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def file_key(path: Path) -> str:
    return str(path.resolve())


def last_run_at(state: dict, path: Path) -> datetime | None:
    entry = (state.get("files") or {}).get(file_key(path)) or {}
    return parse_ts(entry.get("last_run"))


def mark_redact_offset(state: dict, path: Path, offset: int) -> None:
    """Remember how far the secret scan has read, so it only sees new bytes."""
    entry = state.setdefault("files", {}).setdefault(file_key(path), {})
    entry["redact_offset"] = offset


def mark_ran(state: dict, path: Path, bytes_after: int) -> None:
    files = state.setdefault("files", {})
    files[file_key(path)] = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "bytes_after": bytes_after,
    }
    save_state(state)


def due_for_cleanup(path: Path, size: int, threshold: int, interval_days: int, state: dict, force: bool) -> str | None:
    """Return why we should run, or None to skip."""
    if force:
        return "force"
    if size >= threshold:
        return f"size {format_mb(size)} >= {format_mb(threshold)}"
    prev = last_run_at(state, path)
    if prev is None:
        return None
    due = prev + timedelta(days=interval_days)
    if datetime.now(timezone.utc) >= due:
        return f"interval {interval_days}d since {prev.date()}"
    return None


def format_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f}MB"


def print_stats(path: Path, stats: dict) -> None:
    saved = stats["in_bytes"] - stats["out_bytes"]
    print(
        f"{path}: {format_mb(stats['in_bytes'])} -> {format_mb(stats['out_bytes'])} "
        f"(saved {format_mb(saved)}) lines {stats['in_lines']} -> {stats['out_lines']} "
        f"images={stats['stripped_images']} old_snapshots={stats['dropped_snapshots']} "
        f"old_tools={stats['stubbed_tools']} replaced={stats['replaced']}",
        file=sys.stderr,
    )


def read_hook_path() -> Path | None:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    p = data.get("transcript_path") or ""
    return Path(p) if p else None


def find_transcripts() -> list[Path]:
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    return [p for p in root.glob("*/*.jsonl") if p.is_file()]


def same_path(a: Path, b: Path) -> bool:
    """Compare by resolved path so a symlink or ..\\ spelling still matches."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def active_reason(
    path: Path, live: Path | None, now: datetime, grace_minutes: int
) -> str | None:
    """Why `path` must not be rewritten right now, or None when it is safe.

    Rewriting is a read-then-rename, so anything appended in between is lost.
    That makes an open session the one file we must never touch.
    """
    if live is not None and same_path(path, live):
        return "live session — it triggered this run and will append again"
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None
    idle = now - mtime
    if idle < timedelta(minutes=grace_minutes):
        return f"modified {int(idle.total_seconds())}s ago; session may still be open"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="Session .jsonl path")
    parser.add_argument("--force", action="store_true", help="Run even if under the size/date triggers")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not replace the file")
    parser.add_argument("--mark-ran", action="store_true", help="Record last-run now without rewriting the file")
    parser.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    parser.add_argument("--interval-days", type=int, default=DEFAULT_INTERVAL_DAYS)
    parser.add_argument("--threshold-mb", type=int, default=DEFAULT_THRESHOLD_MB)
    parser.add_argument(
        "--active-grace-min",
        type=int,
        default=DEFAULT_ACTIVE_GRACE_MIN,
        help="Treat a transcript touched within this many minutes as a live session",
    )
    parser.add_argument(
        "--no-redact", action="store_true", help="Skip the secret-redaction pass"
    )
    parser.add_argument(
        "--redact-only",
        action="store_true",
        help="Redact secrets and skip size/interval trimming entirely",
    )
    parser.add_argument(
        "--allow-active",
        action="store_true",
        help="Rewrite even a live transcript. Loses whatever that session appends "
        "mid-rewrite; only safe with no session running.",
    )
    args = parser.parse_args()

    threshold = args.threshold_mb * 1024 * 1024
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)
    state = load_state()

    # Stdin names the session that triggered this run. It is the one file that
    # is guaranteed to be open, so it marks what to skip -- not what to clean.
    live = read_hook_path()

    paths: list[Path] = []
    if args.path:
        paths = [Path(args.path)]
    else:
        paths = find_transcripts()

    if not paths:
        return 0

    now = datetime.now(timezone.utc)

    for path in paths:
        if not path.is_file():
            continue

        # Redaction runs on every invocation, including on a live session --
        # a secret is re-read on every resume, so it should not wait for the
        # size or interval trigger. safe_replace makes that safe.
        if not args.no_redact:
            key = file_key(path)
            offset = int(state.get("files", {}).get(key, {}).get("redact_offset", 0))
            size_now = path.stat().st_size
            if size_now < offset:
                offset = 0  # file shrank (trimmed elsewhere) -- rescan in full
            if scan_for_secrets(path, offset):
                stats = redact_file(path, dry_run=args.dry_run)
                if stats["found"]:
                    kinds = ", ".join(f"{k}={v}" for k, v in sorted(stats["found"].items()))
                    verb = "would redact" if args.dry_run else (
                        "redacted" if stats["replaced"] else "redaction deferred (file changed)"
                    )
                    print(f"{path}: {verb} — {kinds}", file=sys.stderr)
                    print(
                        "  These credentials were already sent to the API before this ran. "
                        "Redaction stops them being re-read; rotate them.",
                        file=sys.stderr,
                    )
                if not args.dry_run and stats["replaced"]:
                    size_now = path.stat().st_size
            if not args.dry_run:
                mark_redact_offset(state, path, size_now)

        if args.redact_only:
            continue

        if not args.allow_active:
            blocked = active_reason(path, live, now, args.active_grace_min)
            if blocked:
                print(f"{path}: skipped ({blocked})", file=sys.stderr)
                continue
        size = path.stat().st_size
        if args.mark_ran:
            mark_ran(state, path, size)
            print(f"{path}: marked last-run now ({format_mb(size)})", file=sys.stderr)
            continue
        reason = due_for_cleanup(path, size, threshold, args.interval_days, state, args.force)
        if not reason:
            continue
        print(f"{path}: running ({reason})", file=sys.stderr)
        stats = process_file(path, cutoff, dry_run=args.dry_run)
        print_stats(path, stats)
        if stats.get("replaced"):
            mark_ran(state, path, stats["out_bytes"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as e:
        print(f"cleanup-transcript: {e}", file=sys.stderr)
        raise SystemExit(0)
