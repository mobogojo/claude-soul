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
