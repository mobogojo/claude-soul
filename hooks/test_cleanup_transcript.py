"""Tests for cleanup-transcript.py.

Two things are pinned here:

1. The active-session guard. Cleanup rewrites by read-then-rename, so touching
   an open session loses whatever it appends mid-rewrite.
2. tool_use / tool_result pairing. A transcript is a message array the API
   replays on --resume, and every tool_use must keep a matching tool_result.
   Trimming may empty a result's content; it may never drop the block.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Hyphenated filename, so load it by path rather than by import.
_SPEC = importlib.util.spec_from_file_location(
    "cleanup_transcript", Path(__file__).with_name("cleanup-transcript.py")
)
ct = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ct)


NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def touch(path: Path, *, minutes_ago: int) -> Path:
    path.write_text("{}\n", encoding="utf-8")
    stamp = (NOW - timedelta(minutes=minutes_ago)).timestamp()
    import os

    os.utime(path, (stamp, stamp))
    return path


# --------------------------------------------------------------------------
# Active-session guard
# --------------------------------------------------------------------------

def test_live_session_is_never_rewritten(tmp_path):
    live = touch(tmp_path / "live.jsonl", minutes_ago=999)
    reason = ct.active_reason(live, live, NOW, 30)
    assert reason is not None
    assert "live session" in reason


def test_recently_modified_transcript_is_treated_as_open(tmp_path):
    recent = touch(tmp_path / "recent.jsonl", minutes_ago=5)
    reason = ct.active_reason(recent, None, NOW, 30)
    assert reason is not None
    assert "may still be open" in reason


def test_idle_transcript_is_safe_to_clean(tmp_path):
    idle = touch(tmp_path / "idle.jsonl", minutes_ago=120)
    assert ct.active_reason(idle, None, NOW, 30) is None


def test_other_sessions_are_still_cleanable_while_one_is_live(tmp_path):
    """The guard must not become an excuse to never clean anything."""
    live = touch(tmp_path / "live.jsonl", minutes_ago=0)
    other = touch(tmp_path / "other.jsonl", minutes_ago=120)
    assert ct.active_reason(live, live, NOW, 30) is not None
    assert ct.active_reason(other, live, NOW, 30) is None


def test_guard_matches_the_same_file_spelled_differently(tmp_path):
    live = touch(tmp_path / "live.jsonl", minutes_ago=999)
    alias = tmp_path / "sub" / ".." / "live.jsonl"
    (tmp_path / "sub").mkdir()
    assert ct.active_reason(alias, live, NOW, 30) is not None


def test_grace_window_is_configurable(tmp_path):
    p = touch(tmp_path / "t.jsonl", minutes_ago=45)
    assert ct.active_reason(p, None, NOW, 30) is None
    assert ct.active_reason(p, None, NOW, 60) is not None


def test_missing_file_does_not_raise(tmp_path):
    assert ct.active_reason(tmp_path / "gone.jsonl", None, NOW, 30) is None


# --------------------------------------------------------------------------
# tool_use / tool_result pairing
# --------------------------------------------------------------------------

def test_stub_keeps_the_tool_result_block_and_its_id():
    obj = {
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_abc", "content": "x" * 5000}
            ]
        }
    }
    ct.stub_tool_results(obj)
    block = obj["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_abc"
    assert block["content"] == ct.TOOL_PLACEHOLDER


def test_stub_shrinks_content_but_preserves_block_count():
    obj = {
        "message": {
            "content": [
                {"type": "text", "text": "keep me"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "y" * 9000},
            ]
        }
    }
    ct.stub_tool_results(obj)
    assert len(obj["message"]["content"]) == 2
    assert obj["message"]["content"][0]["text"] == "keep me"


def test_every_tool_use_still_has_a_result_after_a_full_trim(tmp_path):
    """The invariant that makes selective trimming resume-safe."""
    old = (NOW - timedelta(days=60)).isoformat()
    lines = [
        {
            "type": "assistant",
            "timestamp": old,
            "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash"}]},
        },
        {
            "type": "user",
            "timestamp": old,
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "z" * 4000}
                ]
            },
        },
    ]
    src = tmp_path / "t.jsonl"
    src.write_text("".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8")

    ct.process_file(src, cutoff=NOW, dry_run=False)

    uses, results = set(), set()
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for block in json.loads(line).get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.add(block["id"])
            if block.get("type") == "tool_result":
                results.add(block["tool_use_id"])
    assert uses == {"toolu_1"}
    assert uses == results, "a tool_use lost its tool_result — resume would replay malformed"


def test_trim_actually_reduces_size(tmp_path):
    old = (NOW - timedelta(days=60)).isoformat()
    line = {
        "type": "user",
        "timestamp": old,
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "z" * 50000},
                {"type": "image", "source": {"data": "A" * 50000}},
            ]
        },
    }
    src = tmp_path / "t.jsonl"
    src.write_text(json.dumps(line) + "\n", encoding="utf-8")
    before = src.stat().st_size

    stats = ct.process_file(src, cutoff=NOW, dry_run=False)

    assert src.stat().st_size < before / 10
    assert stats["replaced"] is True


def test_recent_tool_results_are_left_intact(tmp_path):
    recent = (NOW - timedelta(days=1)).isoformat()
    payload = "keep this recent output"
    line = {
        "type": "user",
        "timestamp": recent,
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": payload}]
        },
    }
    src = tmp_path / "t.jsonl"
    src.write_text(json.dumps(line) + "\n", encoding="utf-8")

    ct.process_file(src, cutoff=NOW - timedelta(days=14), dry_run=False)

    assert payload in src.read_text(encoding="utf-8")


def test_dry_run_leaves_the_original_untouched(tmp_path):
    old = (NOW - timedelta(days=60)).isoformat()
    line = {
        "type": "user",
        "timestamp": old,
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "z" * 9000}]
        },
    }
    src = tmp_path / "t.jsonl"
    src.write_text(json.dumps(line) + "\n", encoding="utf-8")
    before = src.read_bytes()

    stats = ct.process_file(src, cutoff=NOW, dry_run=True)

    assert src.read_bytes() == before
    assert stats["replaced"] is False
    assert not (tmp_path / "t.jsonl.cleanup-tmp").exists()


def test_line_count_is_preserved(tmp_path):
    """Trimming shrinks records; it must not delete conversation turns."""
    old = (NOW - timedelta(days=60)).isoformat()
    lines = [
        {
            "type": "user",
            "timestamp": old,
            "message": {"content": [{"type": "text", "text": f"turn {i}"}]},
        }
        for i in range(5)
    ]
    src = tmp_path / "t.jsonl"
    src.write_text("".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8")

    ct.process_file(src, cutoff=NOW, dry_run=False)

    kept = [l for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(kept) == 5
    assert "turn 4" in kept[-1]
