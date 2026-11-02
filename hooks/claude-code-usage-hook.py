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

Configuration (environment, all optional):
  RUFLO_HOME        root of the RUFLO tree          (default ~/.ruflo)
  RUFLO_MONITOR_DB  SQLite DB to append to          (default $RUFLO_HOME/data/token_usage.db)
  RUFLO_ALERT_LOG   JSONL budget-alert log          (default $RUFLO_HOME/logs/token_alerts.log)
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

RUFLO = os.environ.get("RUFLO_HOME", os.path.expanduser("~/.ruflo"))
DB_PATH = os.environ.get("RUFLO_MONITOR_DB", os.path.join(RUFLO, "data", "token_usage.db"))
ALERT_LOG = os.environ.get("RUFLO_ALERT_LOG", os.path.join(RUFLO, "logs", "token_alerts.log"))
STATE_DIR = os.path.join(RUFLO, "data", "hook-state")

DAILY_LIMIT = 200_000  # tokens/day before a CRITICAL alert is written
CRITICAL_PCT = 85  # alert thresholds, as % of DAILY_LIMIT
WARNING_PCT = 70
STATE_MAX_IDS = 5000  # per-transcript dedup cache is bounded to this many ids
CACHE_CREATE_MULT = 1.25  # cache-write tokens cost 1.25x the input rate
CACHE_READ_MULT = 0.10  # cache-read tokens cost 0.10x the input rate
GIT_TIMEOUT_S = 3

# Per-token pricing (USD) by model family. Input rate also used (scaled) for cache.
PRICES: dict[str, dict[str, float]] = {
    "opus": {"in": 15e-6, "out": 75e-6},
    "sonnet": {"in": 3e-6, "out": 15e-6},
    "haiku": {"in": 1e-6, "out": 5e-6},
    "fable": {"in": 15e-6, "out": 75e-6},
}
DEFAULT_PRICE = PRICES["sonnet"]

# One DB row: (msg_id, timestamp, task_id, task_name, model, input_tokens,
# output_tokens, total_tokens, cost, market, status)
UsageRow = tuple


def _price_for(model: str) -> dict[str, float]:
    """Return the per-token price dict for a model name (substring match on family)."""
    m = (model or "").lower()
    for key, price in PRICES.items():
        if key in m:
            return price
    return DEFAULT_PRICE


def _estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_creation: int) -> float:
    """Estimated USD cost of one assistant message from its token counts."""
    p = _price_for(model)
    return (
        input_tokens * p["in"]
        + cache_creation * p["in"] * CACHE_CREATE_MULT
        + cache_read * p["in"] * CACHE_READ_MULT
        + output_tokens * p["out"]
    )


def _state_key(transcript_path: str | None) -> str:
    """Dedup-cache key: a short hash of the transcript path (per transcript, NOT per session).

    The same transcript can be read under multiple session_ids; keying on the
    file avoids double-counting.
    """
    return hashlib.sha1((transcript_path or "").encode()).hexdigest()[:16]


def _state_path(key: str, state_dir: str = STATE_DIR) -> str:
    return os.path.join(state_dir, f"{key}.json")


def _load_state(key: str, state_dir: str = STATE_DIR) -> set[str]:
    """Set of message ids already logged for this transcript (empty if no cache)."""
    try:
        with open(_state_path(key, state_dir)) as fh:
            return set(json.load(fh).get("logged_ids", []))
    except (FileNotFoundError, ValueError, OSError):
        return set()


def _save_state(key: str, logged_ids: set[str], state_dir: str = STATE_DIR) -> None:
    """Atomically persist the logged-id set, bounded to STATE_MAX_IDS entries."""
    os.makedirs(state_dir, exist_ok=True)
    path = _state_path(key, state_dir)
    ids = list(logged_ids)[-STATE_MAX_IDS:]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"logged_ids": ids}, fh)
    os.replace(tmp, path)


def _has_tool_error(obj: dict[str, Any]) -> bool:
    """True if this transcript line carries a tool_result marked is_error."""
    msg = obj.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
            return True
    return False


def _normalise_ts(ts: str | None) -> str | None:
    """ISO-8601 message time → sqlite-friendly 'YYYY-MM-DD HH:MM:SS' (None passes through)."""
    if not ts:
        return ts
    return ts.replace("T", " ").replace("Z", "").split(".")[0]


def _iter_transcript(transcript_path: str) -> Iterator[dict[str, Any]]:
    """Yield each parseable JSON object in a JSONL transcript; unreadable file yields nothing."""
    try:
        fh = open(transcript_path, encoding="utf-8")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _iter_assistant_usage(
    transcript_path: str,
) -> Iterator[tuple[str, str, dict[str, Any], str | None, bool]]:
    """Yield (msg_id, model, usage_dict, ts, had_tool_error) for assistant
    messages with usage. had_tool_error is True if a tool_result marked
    is_error appeared since the previous yielded assistant message — a real
    (if approximate) outcome signal, replacing a hardcoded 'success' literal.
    """
    error_since_last = False
    for obj in _iter_transcript(transcript_path):
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
        yield msg_id, msg.get("model", "unknown"), usage, _normalise_ts(obj.get("timestamp")), error_since_last
        error_since_last = False


def _build_rows(
    transcript_path: str,
    logged: set[str],
    task_id: str,
    task_name: str,
    market: str,
    fallback_ts: str,
) -> list[UsageRow]:
    """Turn unlogged assistant messages into DB rows; adds each new msg_id to `logged`."""
    rows: list[UsageRow] = []
    for msg_id, model, usage, ts, had_tool_error in _iter_assistant_usage(transcript_path):
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
        rows.append((msg_id, ts or fallback_ts, task_id, task_name, model, total_in, out, total, cost, market, status))
    return rows


def _write_alert(level: str, task_id: str, used: int, remaining: int, pct: float, alert_log: str = ALERT_LOG) -> None:
    """Append one JSON budget-alert line to the alert log."""
    os.makedirs(os.path.dirname(alert_log), exist_ok=True)
    alert = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "task_id": task_id,
        "tokens_used": used,
        "tokens_remaining": remaining,
        "percentage_used": round(pct),
        "daily_limit": DAILY_LIMIT,
    }
    with open(alert_log, "a") as fh:
        fh.write(json.dumps(alert) + "\n")


def _alert_level(used: int) -> str | None:
    """'CRITICAL' / 'WARNING' / None for today's token total against DAILY_LIMIT."""
    pct = used / DAILY_LIMIT * 100
    if pct >= CRITICAL_PCT:
        return "CRITICAL"
    if pct >= WARNING_PCT:
        return "WARNING"
    return None


def _infer_market(event: dict[str, Any]) -> str:
    """Best-effort repo/project attribution: git toplevel dir name, else cwd basename."""
    cwd = event.get("cwd") or os.getcwd()
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
        if top.returncode == 0 and top.stdout.strip():
            return os.path.basename(top.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass  # git missing or slow: fall through to the cwd basename
    return os.path.basename(cwd.rstrip("/")) or "unknown"


def _parse_event(raw: str) -> dict[str, Any]:
    """Hook event JSON from stdin; empty/invalid input → {} (never raises)."""
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Add msg_id column + unique index so repeated runs are hard-deduped."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(token_usage)").fetchall()]
    if "msg_id" not in cols:
        conn.execute("ALTER TABLE token_usage ADD COLUMN msg_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_token_usage_msgid ON token_usage(msg_id) WHERE msg_id IS NOT NULL"
    )


def _write_rows(db_path: str, rows: list[UsageRow]) -> int:
    """Insert rows (ignoring duplicates) and return today's total token count."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        _ensure_schema(conn)
        conn.executemany(
            """INSERT OR IGNORE INTO token_usage
               (msg_id, timestamp, task_id, task_name, model, input_tokens, output_tokens,
                total_tokens, cost, market, status, duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            rows,
        )
        conn.commit()
        return conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) FROM token_usage WHERE DATE(timestamp)=DATE('now')"
        ).fetchone()[0]
    finally:
        conn.close()


def main() -> None:
    event = _parse_event(sys.stdin.read())

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
    rows = _build_rows(transcript, logged, task_id, task_name, market, now)
    if not rows:
        sys.exit(0)

    try:
        used = _write_rows(DB_PATH, rows)
    except sqlite3.Error as exc:
        sys.stderr.write(f"[ruflo-usage-hook] db error: {exc}\n")
        sys.exit(0)

    # Saved under the same key it is loaded under (an earlier version saved
    # under session_id, so the dedup cache was never found and every Stop
    # rescanned the whole transcript).
    _save_state(state_key, logged)

    level = _alert_level(used)
    if level:
        _write_alert(level, task_id, used, DAILY_LIMIT - used, used / DAILY_LIMIT * 100)

    sys.exit(0)


if __name__ == "__main__":
    main()
