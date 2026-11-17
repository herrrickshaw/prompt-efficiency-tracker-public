from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


def _settings(hook, events=("Stop", "SubagentStop")):
    return {
        "hooks": {e: [{"matcher": "", "hooks": [{"type": "command", "command": f"python3 {hook}"}]}] for e in events}
    }


def _db(path, last_ts):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE token_usage (timestamp TEXT)")
    if last_ts is not None:
        conn.execute("INSERT INTO token_usage VALUES (?)", (last_ts,))
    conn.commit()
    conn.close()


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---- hooks wired ----------------------------------------------------------


def test_hooks_wired_in_ok(heartbeat):
    assert heartbeat._hooks_wired_in(_settings("/x/hook.py"), "/x/hook.py") == (True, "ok")


@pytest.mark.parametrize("present,missing", [(("Stop",), "SubagentStop"), ((), "Stop")])
def test_hooks_wired_in_missing_event(heartbeat, present, missing):
    ok, detail = heartbeat._hooks_wired_in(_settings("/x/hook.py", present), "/x/hook.py")
    assert not ok and detail == f"{missing} hook missing from settings.json"


def test_hooks_wired_in_wrong_script(heartbeat):
    assert heartbeat._hooks_wired_in(_settings("/other.py"), "/x/hook.py")[0] is False


def test_hooks_wired_reads_file(heartbeat, tmp_path):
    assert heartbeat._hooks_wired()[1] == "settings.json missing or not valid JSON"
    (tmp_path / "settings.json").write_text("{not json")
    assert heartbeat._hooks_wired()[0] is False
    (tmp_path / "settings.json").write_text(json.dumps(_settings(heartbeat.USAGE_HOOK)))
    assert heartbeat._hooks_wired() == (True, "ok")


# ---- db freshness ---------------------------------------------------------


def test_db_freshness_missing_empty_unparseable(heartbeat, tmp_path):
    assert heartbeat._db_freshness() == (True, "token_usage.db does not exist")
    _db(heartbeat.DB_PATH, None)
    assert heartbeat._db_freshness() == (True, "token_usage table is empty")
    os.remove(heartbeat.DB_PATH)
    _db(heartbeat.DB_PATH, "yesterday-ish")
    assert heartbeat._db_freshness() == (True, "unparseable timestamp: yesterday-ish")


def test_db_freshness_db_error(heartbeat, tmp_path):
    sqlite3.connect(heartbeat.DB_PATH).close()  # no token_usage table
    stale, detail = heartbeat._db_freshness()
    assert stale and detail.startswith("db error:")


def test_db_freshness_recent_row_is_fresh(heartbeat):
    now = datetime.now(timezone.utc)
    _db(heartbeat.DB_PATH, _fmt(now - timedelta(hours=2)))
    stale, detail = heartbeat._db_freshness(now=now)
    assert not stale and detail.startswith("last row ")


def test_db_freshness_old_row_no_transcripts(heartbeat):
    now = datetime.now(timezone.utc)
    _db(heartbeat.DB_PATH, _fmt(now - timedelta(days=3)))
    assert heartbeat._db_freshness(now=now) == (False, "no transcripts found, nothing to compare against")


def test_db_freshness_old_row_but_newer_session_is_stale(heartbeat, tmp_path):
    now = datetime.now(timezone.utc)
    _db(heartbeat.DB_PATH, _fmt(now - timedelta(days=3)))
    t = tmp_path / "projects" / "p" / "s.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text("{}\n")  # mtime = now, > 30h after the last row
    stale, detail = heartbeat._db_freshness(now=now)
    assert stale and "sessions ran as recently as" in detail


def test_db_freshness_old_row_quiet_since(heartbeat, tmp_path):
    now = datetime.now(timezone.utc)
    last = now - timedelta(days=3)
    _db(heartbeat.DB_PATH, _fmt(last))
    t = tmp_path / "projects" / "s.jsonl"
    t.write_text("{}\n")
    mt = (last + timedelta(hours=1)).timestamp()
    os.utime(t, (mt, mt))
    stale, detail = heartbeat._db_freshness(now=now)
    assert not stale and detail.endswith("no newer session activity")


def test_newest_transcript_mtime(heartbeat, tmp_path):
    assert heartbeat._newest_transcript_mtime(str(tmp_path / "none")) is None
    a = tmp_path / "projects" / "a.jsonl"
    b = tmp_path / "projects" / "b.txt"
    a.write_text("")
    b.write_text("")
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    assert heartbeat._newest_transcript_mtime(str(tmp_path / "projects")) == 1000


# ---- smoke ----------------------------------------------------------------


def test_main_logs_and_exits_zero(heartbeat, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps(_settings(heartbeat.USAGE_HOOK)))
    _db(heartbeat.DB_PATH, _fmt(datetime.now(timezone.utc)))
    notified = []
    heartbeat._notify = lambda title, msg: notified.append(msg)
    with pytest.raises(SystemExit) as exc:
        heartbeat.main()
    assert exc.value.code == 0
    entry = json.loads(open(heartbeat.LOG_PATH).read().splitlines()[-1])
    assert entry["ok"] is True and entry["problems"] == []
    assert notified == []


def test_main_notifies_on_problems(heartbeat, tmp_path):
    notified = []
    heartbeat._notify = lambda title, msg: notified.append(msg)
    with pytest.raises(SystemExit) as exc:
        heartbeat.main()
    assert exc.value.code == 0
    assert notified == ["hooks: settings.json missing or not valid JSON; tracker: token_usage.db does not exist"]
