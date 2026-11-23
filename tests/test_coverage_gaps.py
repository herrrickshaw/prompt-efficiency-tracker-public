"""Edge cases not covered by the main suites: notify, git failure, id-less rows, racing files."""

from __future__ import annotations

import json
import os
import subprocess

from conftest import REPO, _load


def test_iter_assistant_usage_skips_rows_without_any_id(usage_hook, tmp_path):
    t = tmp_path / "t.jsonl"
    rows = [
        {"type": "assistant", "message": {"usage": {"input_tokens": 1}}},  # no id, no uuid -> skipped
        {"type": "assistant", "uuid": "u1", "message": {"usage": {"input_tokens": 2}}},  # uuid fallback
        {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 3}}},
    ]
    t.write_text("".join(json.dumps(r) + "\n" for r in rows))
    got = [(i, u["input_tokens"]) for i, _m, u, _ts, _e in usage_hook._iter_assistant_usage(str(t))]
    assert got == [("u1", 2), ("m1", 3)]


def test_infer_market_falls_back_to_cwd_basename_when_git_is_missing(usage_hook, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(usage_hook.subprocess, "run", boom)
    assert usage_hook._infer_market({"cwd": "/work/my-proj/"}) == "my-proj"


def test_infer_market_falls_back_when_git_times_out(usage_hook, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("git", 3)

    monkeypatch.setattr(usage_hook.subprocess, "run", slow)
    assert usage_hook._infer_market({"cwd": "/work/other"}) == "other"


def test_newest_transcript_mtime_ignores_file_that_vanishes(heartbeat, tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    (proj / "a").mkdir()
    (proj / "a" / "x.jsonl").write_text("{}")

    def gone(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(heartbeat.os.path, "getmtime", gone)
    assert heartbeat._newest_transcript_mtime(str(proj)) is None


def test_newest_transcript_mtime_picks_the_newest_jsonl_only(heartbeat, tmp_path):
    proj = tmp_path / "p2"
    (proj / "a").mkdir(parents=True)
    old, new, other = proj / "a" / "old.jsonl", proj / "a" / "new.jsonl", proj / "a" / "note.txt"
    for f in (old, new, other):
        f.write_text("x")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    os.utime(other, (9000, 9000))  # not a transcript: must not count
    near = proj / "a" / "notes.yaml.l"  # shares the trailing "l" but is not .jsonl
    near.write_text("x")
    os.utime(near, (9500, 9500))
    assert heartbeat._newest_transcript_mtime(str(proj)) == 2000


def _real_heartbeat():
    # the shared fixture stubs _notify; load a pristine copy
    return _load("heartbeat_real", REPO / "scripts" / "heartbeat_check.py")


def test_notify_invokes_osascript_with_json_quoted_text(monkeypatch):
    hb = _real_heartbeat()
    calls = []
    monkeypatch.setattr(hb.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    hb._notify('ti"tle', "mess\nage")
    (cmd, kw), = calls
    assert cmd[:2] == ["osascript", "-e"]
    assert cmd[2] == 'display notification "mess\\nage" with title "ti\\"tle" sound name "Basso"'
    assert kw["timeout"] == 5 and kw["capture_output"] is True


def test_notify_swallows_missing_osascript_and_timeout(monkeypatch):
    hb = _real_heartbeat()
    for exc in (FileNotFoundError("osascript"), subprocess.TimeoutExpired("osascript", 5)):

        def raiser(*a, _e=exc, **k):
            raise _e

        monkeypatch.setattr(hb.subprocess, "run", raiser)
        assert hb._notify("t", "m") is None
