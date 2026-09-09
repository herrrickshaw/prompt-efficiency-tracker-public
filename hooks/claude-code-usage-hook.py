#!/usr/bin/env python3
"""
RUFLO — Claude Code token-usage feeder hook.

Wired into Claude Code as a Stop / SubagentStop hook. On each turn end it reads
the session transcript, finds any assistant messages not yet logged, and records
their real token usage into the RUFLO SQLite monitor DB. Uses only the Python
stdlib (sqlite3) — no npm 'sqlite3' native module required.

Input (stdin): the hook event JSON Claude Code provides, e.g.
    {"session_id": "...", "transcript_path": "/abs/....jsonl",
     "hook_event_name": "Stop" | "SubagentStop"}

Tracks:
  - token usage per assistant message (input incl. cache, output, cost)
  - main agent vs subagent (task_id = claude_code / claude_code_subagent)
  - model distribution (opus / sonnet / haiku / ...)
  - daily budget alerts to .ruflo/logs/token_alerts.log
"""
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

RUFLO = os.path.expanduser("~/.ruflo")
DB_PATH = os.environ.get("RUFLO_MONITOR_DB", os.path.join(RUFLO, "data", "token_usage.db"))
ALERT_LOG = os.environ.get("RUFLO_ALERT_LOG", os.path.join(RUFLO, "logs", "token_alerts.log"))
STATE_DIR = os.path.join(RUFLO, "data", "hook-state")
DAILY_LIMIT = 200_000
SOFT_LIMIT = 160_000

# Per-token pricing (USD) by model family. Input rate also used (scaled) for cache.
PRICES = {
    "opus":   {"in": 15e-6, "out": 75e-6},
    "sonnet": {"in": 3e-6,  "out": 15e-6},
    "haiku":  {"in": 1e-6,  "out": 5e-6},
    "fable":  {"in": 15e-6, "out": 75e-6},
}
DEFAULT_PRICE = PRICES["sonnet"]


def _price_for(model: str):
    m = (model or "").lower()
    for key, price in PRICES.items():
        if key in m:
            return price
    return DEFAULT_PRICE


def _estimate_cost(model, input_tokens, output_tokens, cache_read, cache_creation):
    p = _price_for(model)
    return (
        input_tokens * p["in"]
        + cache_creation * p["in"] * 1.25
        + cache_read * p["in"] * 0.10
        + output_tokens * p["out"]
    )


def _state_key(transcript_path):
    # dedup per transcript, NOT per session — the same transcript can be read
    # under multiple session_ids; keying on the file avoids double-counting.
    return hashlib.sha1((transcript_path or "").encode()).hexdigest()[:16]


def _load_state(key):
    path = os.path.join(STATE_DIR, f"{key}.json")
    try:
        with open(path) as fh:
            return set(json.load(fh).get("logged_ids", []))
    except (FileNotFoundError, ValueError, OSError):
        return set()


def _save_state(key, logged_ids):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{key}.json")
    # keep bounded — last 5000 ids is plenty for one session
    ids = list(logged_ids)[-5000:]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"logged_ids": ids}, fh)
    os.replace(tmp, path)


def _has_tool_error(obj):
    """True if this transcript line carries a tool_result marked is_error."""
    msg = obj.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
            return True
    return False


def _iter_assistant_usage(transcript_path):
    """Yield (msg_id, model, usage_dict, ts, had_tool_error) for assistant
    messages with usage. had_tool_error is True if a tool_result marked
    is_error appeared since the previous yielded assistant message — a real
    (if approximate) outcome signal, replacing a hardcoded 'success' literal.
    """
    try:
        fh = open(transcript_path, encoding="utf-8")
    except OSError:
        return
    error_since_last = False
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if _has_tool_error(obj):
                error_since_last = True
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            msg_id = msg.get("id") or obj.get("uuid")
            if not msg_id:
                continue
            # real message time (ISO) → sqlite-friendly 'YYYY-MM-DD HH:MM:SS'
            ts = obj.get("timestamp")
            if ts:
                ts = ts.replace("T", " ").replace("Z", "").split(".")[0]
            yield msg_id, msg.get("model", "unknown"), usage, ts, error_since_last
            error_since_last = False


def _write_alert(level, task_id, used, remaining, pct):
    os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
    alert = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "task_id": task_id,
        "tokens_used": used,
        "tokens_remaining": remaining,
        "percentage_used": round(pct),
        "daily_limit": DAILY_LIMIT,
    }
    with open(ALERT_LOG, "a") as fh:
        fh.write(json.dumps(alert) + "\n")


def _infer_market(event):
    """Best-effort repo/project attribution: git toplevel dir name, else cwd basename."""
    cwd = event.get("cwd") or os.getcwd()
    try:
        import subprocess
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if top.returncode == 0 and top.stdout.strip():
            return os.path.basename(top.stdout.strip())
    except Exception:
        pass
    return os.path.basename(cwd.rstrip("/")) or "unknown"


def main():
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except ValueError:
        event = {}

    session_id = event.get("session_id") or "unknown-session"
    transcript = event.get("transcript_path")
    is_subagent = event.get("hook_event_name") == "SubagentStop"
    task_id = "claude_code_subagent" if is_subagent else "claude_code"
    task_name = "Claude Code subagent turn" if is_subagent else "Claude Code turn"
    market = _infer_market(event)

    if not transcript or not os.path.exists(transcript):
        sys.exit(0)  # nothing to log; never block the turn

    state_key = _state_key(transcript)
    logged = _load_state(state_key)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for msg_id, model, usage, ts, had_tool_error in _iter_assistant_usage(transcript):
        if msg_id in logged:
            continue
        logged.add(msg_id)
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        cread = int(usage.get("cache_read_input_tokens", 0) or 0)
        ccreate = int(usage.get("cache_creation_input_tokens", 0) or 0)
        total_in = inp + cread + ccreate
        total = total_in + out
        if total == 0:
            continue
        cost = _estimate_cost(model, inp, out, cread, ccreate)
        status = "tool_error" if had_tool_error else "success"
        rows.append((msg_id, ts or now, task_id, task_name, model, total_in, out, total, cost, market, status))

    if not rows:
        sys.exit(0)

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        # msg_id column + unique index give a hard dedup guarantee even if the
        # per-transcript cursor is lost. Safe to run repeatedly.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(token_usage)").fetchall()]
        if "msg_id" not in cols:
            conn.execute("ALTER TABLE token_usage ADD COLUMN msg_id TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_token_usage_msgid "
            "ON token_usage(msg_id) WHERE msg_id IS NOT NULL"
        )
        conn.executemany(
            """INSERT OR IGNORE INTO token_usage
               (msg_id, timestamp, task_id, task_name, model, input_tokens, output_tokens,
                total_tokens, cost, market, status, duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            rows,
        )
        conn.commit()
        used = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) FROM token_usage WHERE DATE(timestamp)=DATE('now')"
        ).fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        sys.stderr.write(f"[ruflo-usage-hook] db error: {exc}\n")
        sys.exit(0)

    # was buggy: saved under session_id but loaded under state_key (transcript
    # hash), so the dedup cache was never actually found on the next Stop —
    # harmless (a DB-level unique index on msg_id caught the duplicates) but
    # meant every Stop rescanned the whole transcript from line 1.
    _save_state(state_key, logged)

    pct = used / DAILY_LIMIT * 100
    if pct >= 85:
        _write_alert("CRITICAL", task_id, used, DAILY_LIMIT - used, pct)
    elif pct >= 70:
        _write_alert("WARNING", task_id, used, DAILY_LIMIT - used, pct)

    sys.exit(0)


if __name__ == "__main__":
    main()
