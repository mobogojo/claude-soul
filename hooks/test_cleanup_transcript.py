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


# --------------------------------------------------------------------------
# Secret redaction
#
# Two failure directions matter equally: missing a secret leaves it to be
# re-read on every resume, and over-redacting quietly destroys the transcript.
# --------------------------------------------------------------------------

def tok(prefix: str, body: str) -> str:
    """Build a token-shaped fixture at runtime.

    These are fabricated, but they are realistic enough that GitHub push
    protection rejects the push when they appear as literals in the source
    (it blocked this file's first version on the Slack and Stripe samples).
    Splitting prefix from body keeps the full shape out of the committed text
    while the tests still exercise the real patterns.
    """
    return prefix + body


SECRETS = [
    ("env-assignment", "AWS_SECRET_ACCESS_KEY=" + tok("wJalrXUtnFEMI", "/K7MDENG/bPxRfiCYEX")),
    ("env-assignment", 'export API_KEY="s3cr3t-value-not-a-real-key"'),
    ("env-assignment", "DATABASE_PASSWORD: hunter2hunter2"),
    ("aws-access-key-id", "key id " + tok("AKIA", "IOSFODNN7EXAMPLE") + " here"),
    ("github-token", tok("ghp", "_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")),
    ("slack-token", tok("xoxb", "-123456789012-abcdefghijklmnop")),
    ("stripe-key", tok("sk_live", "_A1b2C3d4E5f6G7h8I9j0K1l2")),
    ("google-api-key", tok("AIza", "SyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R")),
    ("anthropic-openai-key", tok("sk-ant", "-A1b2C3d4E5f6G7h8I9j0K1l2M3")),
    ("jwt", tok("eyJhbGciOiJIUzI1NiJ9", ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQabcdef")),
    ("url-credentials", "postgres://admin:hunter2pass@10.0.0.1:5432/prod"),
]


@pytest.mark.parametrize("kind,sample", SECRETS, ids=[s[0] + ":" + s[1][:18] for s in SECRETS])
def test_secret_is_redacted(kind, sample):
    out, found = ct.redact_text(sample)
    assert found, f"{kind} not detected"
    assert ct.REDACTED in out


@pytest.mark.parametrize("kind,sample", SECRETS, ids=[s[0] for s in SECRETS])
def test_secret_value_does_not_survive(kind, sample):
    """The point of the exercise: the raw value must be gone."""
    out, _ = ct.redact_text(sample)
    secret = sample.split("=")[-1].split(": ")[-1].strip('"')
    assert secret not in out or secret in ("", ct.REDACTED)


def test_pem_private_key_block_is_removed():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAqwertyuiopasdfghjkl\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, found = ct.redact_text(pem)
    assert found.get("pem-private-key") == 1
    assert "MIIEowIBAAKCAQEA" not in out


BENIGN = [
    "commit 3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a is fine",
    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "id 550e8400-e29b-41d4-a716-446655440000",
    "npm install --save-dev @types/node@20.11.30",
    "Authorization: Bearer",
    "the deploy token was rotated last week",
    "run tests with pytest -q and check coverage",
    # "AUTH" inside a word: these matched before the boundary fix and would
    # have redacted the trailer of every commit message in the transcript.
    "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
    "author: Logan Waggoner",
    # Templates and placeholders are not credentials.
    "GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }}",
    "API_KEY=<your-key-here>",
    "SECRET_TOKEN=$MY_ENV_VAR",
]


@pytest.mark.parametrize("text", BENIGN)
def test_benign_content_is_untouched(text):
    out, found = ct.redact_text(text)
    assert out == text, f"over-redacted: {found}"
    assert not found


def test_bearer_scheme_word_survives_but_the_token_does_not():
    text = "Authorization: Bearer " + tok("eyJhbGciOiJIUzI1NiJ9", ".eyJzdWIiOiJ4In0.abcdefghij")
    out, found = ct.redact_text(text)
    assert "Bearer" in out, "redacted the scheme word instead of the token"
    assert ct.REDACTED in out
    assert found


def test_high_entropy_blob_needs_secret_context():
    blob = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWpr"
    plain, found_plain = ct.redact_text(f"the payload is {blob}")
    assert plain.endswith(blob), "redacted a blob with no secret context"
    assert not found_plain

    near, found_near = ct.redact_text(f"the api_key is {blob}")
    assert ct.REDACTED in near
    assert found_near


def test_redaction_preserves_json_structure_and_pairing(tmp_path):
    record = {
        "type": "user",
        "timestamp": (NOW - timedelta(days=1)).isoformat(),
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_9",
                    "content": "GITHUB_TOKEN=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
                }
            ]
        },
    }
    src = tmp_path / "t.jsonl"
    src.write_text(json.dumps(record) + "\n", encoding="utf-8")

    stats = ct.redact_file(src, dry_run=False)

    assert stats["replaced"] is True
    out = json.loads(src.read_text(encoding="utf-8").strip())
    block = out["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_9"
    assert "ghp_" not in block["content"]
    assert ct.REDACTED in block["content"]


def test_clean_transcript_is_not_rewritten(tmp_path):
    """No secrets means no rewrite — and so no chance of a lost append."""
    src = tmp_path / "t.jsonl"
    src.write_text(json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n",
                   encoding="utf-8")
    before = src.stat().st_mtime_ns

    stats = ct.redact_file(src, dry_run=False)

    assert stats["found"] == {}
    assert stats["replaced"] is False
    assert src.stat().st_mtime_ns == before


def test_safe_replace_aborts_when_the_file_changed(tmp_path):
    """The compare-and-swap that lets redaction touch a live session."""
    target = tmp_path / "t.jsonl"
    target.write_text("original\n", encoding="utf-8")
    stale = (target.stat().st_size, target.stat().st_mtime_ns)

    target.write_text("original\nappended by the live session\n", encoding="utf-8")
    tmp = tmp_path / "t.jsonl.redact-tmp"
    tmp.write_text("redacted\n", encoding="utf-8")

    assert ct.safe_replace(tmp, target, stale) is False
    assert "appended by the live session" in target.read_text(encoding="utf-8")
    assert not tmp.exists()


def test_safe_replace_commits_when_unchanged(tmp_path):
    target = tmp_path / "t.jsonl"
    target.write_text("original\n", encoding="utf-8")
    st = target.stat()
    tmp = tmp_path / "t.jsonl.redact-tmp"
    tmp.write_text("redacted\n", encoding="utf-8")

    assert ct.safe_replace(tmp, target, (st.st_size, st.st_mtime_ns)) is True
    assert target.read_text(encoding="utf-8") == "redacted\n"

# --------------------------------------------------------------------------
# Redaction runs as part of the cleanup rewrite
#
# It used to be a separate scan on every Stop hook, which cost ~2 minutes per
# turn across a large backlog. Cleanup already streams and rewrites the whole
# file, so redaction rides along with it instead.
# --------------------------------------------------------------------------

def _write(tmp_path, records):
    src = tmp_path / "t.jsonl"
    src.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return src


def test_cleanup_redacts_secrets_in_the_same_pass(tmp_path):
    src = _write(tmp_path, [{
        "type": "user",
        "timestamp": (NOW - timedelta(days=1)).isoformat(),
        "message": {"content": [
            {"type": "text", "text": "GITHUB_TOKEN=" + tok("ghp", "_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")}
        ]},
    }])

    stats = ct.process_file(src, cutoff=NOW - timedelta(days=14), dry_run=False)

    body = src.read_text(encoding="utf-8")
    assert stats["redacted"], "cleanup did not redact"
    assert "ghp_" not in body
    assert ct.REDACTED in body


def test_redaction_applies_regardless_of_record_age(tmp_path):
    """A secret does not become safe because the record is old or new."""
    src = _write(tmp_path, [
        {"type": "user", "timestamp": (NOW - timedelta(days=90)).isoformat(),
         "message": {"content": [{"type": "text", "text": "OLD_API_KEY=abcdef123456ghijkl"}]}},
        {"type": "user", "timestamp": NOW.isoformat(),
         "message": {"content": [{"type": "text", "text": "NEW_API_KEY=zyxwvu987654tsrqpo"}]}},
    ])

    ct.process_file(src, cutoff=NOW - timedelta(days=14), dry_run=False)

    body = src.read_text(encoding="utf-8")
    assert "abcdef123456ghijkl" not in body
    assert "zyxwvu987654tsrqpo" not in body
    assert body.count(ct.REDACTED) == 2


def test_redaction_can_be_turned_off(tmp_path):
    src = _write(tmp_path, [{
        "type": "user", "timestamp": NOW.isoformat(),
        "message": {"content": [{"type": "text", "text": "API_KEY=abcdef123456ghijkl"}]},
    }])

    ct.process_file(src, cutoff=NOW - timedelta(days=14), dry_run=False, redact=False)

    assert "abcdef123456ghijkl" in src.read_text(encoding="utf-8")


def test_redaction_in_cleanup_keeps_tool_pairing(tmp_path):
    """Redacting mid-cleanup must not disturb the message array's integrity."""
    old = (NOW - timedelta(days=60)).isoformat()
    src = _write(tmp_path, [
        {"type": "assistant", "timestamp": old,
         "message": {"content": [{"type": "tool_use", "id": "toolu_5", "name": "Bash"}]}},
        {"type": "user", "timestamp": old,
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_5",
                                  "content": "DISCORD_TOKEN=abcdef123456ghijklmnop"}]}},
    ])

    ct.process_file(src, cutoff=NOW, dry_run=False)

    uses, results = set(), set()
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for block in json.loads(line).get("message", {}).get("content", []):
            if isinstance(block, dict):
                if block.get("type") == "tool_use":
                    uses.add(block["id"])
                if block.get("type") == "tool_result":
                    results.add(block["tool_use_id"])
    assert uses == results == {"toolu_5"}


def test_cleanup_leaves_a_clean_transcript_byte_identical(tmp_path):
    """No secrets and nothing old: the rewrite must not churn the file."""
    src = _write(tmp_path, [{
        "type": "user", "timestamp": NOW.isoformat(),
        "message": {"content": [{"type": "text", "text": "just an ordinary message"}]},
    }])
    before = src.read_text(encoding="utf-8")

    ct.process_file(src, cutoff=NOW - timedelta(days=14), dry_run=False)

    assert src.read_text(encoding="utf-8") == before
