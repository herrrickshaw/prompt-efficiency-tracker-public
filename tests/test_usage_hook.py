from __future__ import annotations

import io
import json
import sqlite3

import pytest

SCHEMA = """CREATE TABLE token_usage (
    id INTEGER PRIMARY KEY, timestamp TEXT, task_id TEXT, task_name TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, cost REAL,
    market TEXT, status TEXT, duration_seconds REAL)"""


def _line(obj):
    return json.dumps(obj) + "\n"


def _assistant(msg_id, model="claude-sonnet-5", ts="2026-09-09T10:00:00.123Z", **usage):
    return {"type": "assistant", "timestamp": ts, "message": {"id": msg_id, "model": model, "usage": usage}}


def _tool_error():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True}]}}


def write_transcript(path, lines):
    path.write_text("".join(_line(obj) for obj in lines) + "not json\n\n")


# ---- pure functions -------------------------------------------------------


@pytest.mark.parametrize(
    "model,key",
    [
        ("claude-opus-4", "opus"),
        ("claude-sonnet-5", "sonnet"),
        ("claude-haiku-3", "haiku"),
        ("claude-fable-5-1", "fable"),
        ("unknown", "sonnet"),
        ("", "sonnet"),
        (None, "sonnet"),
    ],
)
def test_price_for_matches_family(usage_hook, model, key):
    assert usage_hook._price_for(model) == usage_hook.PRICES[key]


def test_estimate_cost_formula(usage_hook):
    cost = usage_hook._estimate_cost("sonnet", 1000, 100, 2000, 400)
    expected = 1000 * 3e-6 + 400 * 3e-6 * 1.25 + 2000 * 3e-6 * 0.10 + 100 * 15e-6
    assert cost == pytest.approx(expected)


def test_state_key_is_stable_and_short(usage_hook):
    assert usage_hook._state_key("/a/b.jsonl") == usage_hook._state_key("/a/b.jsonl")
    assert usage_hook._state_key("/a/b.jsonl") != usage_hook._state_key("/a/c.jsonl")
    assert len(usage_hook._state_key(None)) == 16


def test_normalise_ts(usage_hook):
    assert usage_hook._normalise_ts("2026-09-09T10:00:00.123Z") == "2026-09-09 10:00:00"
    assert usage_hook._normalise_ts(None) is None
    assert usage_hook._normalise_ts("") == ""


def test_has_tool_error(usage_hook):
    assert usage_hook._has_tool_error(_tool_error())
    ok = {"message": {"content": [{"type": "tool_result", "is_error": False}]}}
    assert not usage_hook._has_tool_error(ok)
    assert not usage_hook._has_tool_error({"message": {"content": "plain string"}})
    assert not usage_hook._has_tool_error({})


@pytest.mark.parametrize("used,level", [(0, None), (139_999, None), (140_000, "WARNING"), (170_000, "CRITICAL")])
def test_alert_level_thresholds(usage_hook, used, level):
    assert usage_hook._alert_level(used) == level


def test_parse_event(usage_hook):
    assert usage_hook._parse_event("") == {}
    assert usage_hook._parse_event("   ") == {}
    assert usage_hook._parse_event("{bad") == {}
    assert usage_hook._parse_event('{"a": 1}') == {"a": 1}


# ---- transcript scanning --------------------------------------------------


def test_iter_assistant_usage_flags_error_since_last_message(usage_hook, tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(
        t,
        [
            _assistant("m1", input_tokens=10, output_tokens=5),
            _tool_error(),
            {"type": "assistant", "message": {"id": "no-usage"}},
            _assistant("m2", input_tokens=1),
            _assistant("m3", input_tokens=1),
            {"type": "assistant", "uuid": "u4", "message": {"usage": {"input_tokens": 2}}},
        ],
    )
    got = list(usage_hook._iter_assistant_usage(str(t)))
    assert [(g[0], g[4]) for g in got] == [("m1", False), ("m2", True), ("m3", False), ("u4", False)]
    assert got[0][3] == "2026-09-09 10:00:00"


def test_iter_assistant_usage_missing_file_yields_nothing(usage_hook, tmp_path):
    assert list(usage_hook._iter_assistant_usage(str(tmp_path / "nope.jsonl"))) == []


def test_build_rows_skips_logged_and_zero_token(usage_hook, tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(
        t,
        [
            _assistant("seen", input_tokens=5),
            _assistant("zero", input_tokens=0, output_tokens=0),
            _assistant("new", input_tokens=3, output_tokens=2, cache_read_input_tokens=10, ts=None),
        ],
    )
    logged = {"seen"}
    rows = usage_hook._build_rows(str(t), logged, "claude_code", "Claude Code turn", "repo", "FALLBACK")
    assert len(rows) == 1
    msg_id, ts, task_id, task_name, model, total_in, out, total, cost, market, status = rows[0]
    assert (msg_id, ts, total_in, out, total, market, status) == ("new", "FALLBACK", 13, 2, 15, "repo", "success")
    assert cost == pytest.approx(usage_hook._estimate_cost(model, 3, 2, 10, 0))
    assert logged == {"seen", "zero", "new"}


def test_state_roundtrip_bounded(usage_hook, tmp_path):
    ids = {f"id{i}" for i in range(6000)}
    usage_hook._save_state("k", ids, str(tmp_path / "state"))
    back = usage_hook._load_state("k", str(tmp_path / "state"))
    assert len(back) == usage_hook.STATE_MAX_IDS and back <= ids
    assert usage_hook._load_state("missing", str(tmp_path / "state")) == set()


def test_infer_market_falls_back_to_cwd(usage_hook, tmp_path):
    assert usage_hook._infer_market({"cwd": str(tmp_path / "proj")}) == "proj"


# ---- end-to-end smoke -----------------------------------------------------


def test_main_writes_rows_dedups_and_alerts(usage_hook, tmp_path, monkeypatch):
    db = tmp_path / "ruflo" / "data" / "token_usage.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).executescript(SCHEMA)
    today = usage_hook.datetime.now(usage_hook.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t = tmp_path / "t.jsonl"
    write_transcript(
        t,
        [
            _assistant("m1", ts=today, input_tokens=100_000, output_tokens=1),
            _tool_error(),
            _assistant("m2", ts=today, input_tokens=100_000, output_tokens=1),
        ],
    )
    event = json.dumps({"transcript_path": str(t), "hook_event_name": "SubagentStop", "cwd": str(tmp_path)})

    monkeypatch.setattr("sys.stdin", io.StringIO(event))
    with pytest.raises(SystemExit) as exc:
        usage_hook.main()
    assert exc.value.code == 0

    rows = (
        sqlite3.connect(db)
        .execute("SELECT msg_id, task_id, status, market, total_tokens FROM token_usage ORDER BY msg_id")
        .fetchall()
    )
    assert rows == [
        ("m1", "claude_code_subagent", "success", tmp_path.name, 100_001),
        ("m2", "claude_code_subagent", "tool_error", tmp_path.name, 100_001),
    ]
    alerts = [json.loads(line) for line in (tmp_path / "ruflo" / "logs" / "token_alerts.log").read_text().splitlines()]
    assert [a["level"] for a in alerts] == ["CRITICAL"]
    assert usage_hook._load_state(usage_hook._state_key(str(t))) == {"m1", "m2"}

    # second run: dedup cache means no new rows and no new alert
    monkeypatch.setattr("sys.stdin", io.StringIO(event))
    with pytest.raises(SystemExit):
        usage_hook.main()
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 2
    assert len((tmp_path / "ruflo" / "logs" / "token_alerts.log").read_text().splitlines()) == 1


def test_main_without_transcript_exits_zero(usage_hook, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit) as exc:
        usage_hook.main()
    assert exc.value.code == 0


def test_main_db_error_is_swallowed(usage_hook, tmp_path, monkeypatch, capsys):
    # DB exists but has no token_usage table -> sqlite3.Error -> stderr + exit 0
    db = tmp_path / "ruflo" / "data" / "token_usage.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    t = tmp_path / "t.jsonl"
    write_transcript(t, [_assistant("m1", input_tokens=1)])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(t)})))
    with pytest.raises(SystemExit) as exc:
        usage_hook.main()
    assert exc.value.code == 0
    assert "db error" in capsys.readouterr().err
