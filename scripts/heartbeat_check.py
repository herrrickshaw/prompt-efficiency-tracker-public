#!/usr/bin/env python3
"""
RUFLO — token-tracking heartbeat.

Checks the two ways the token/outcome tracker has actually failed before
(2026-09-09 audit): the Stop/SubagentStop hooks silently dropped from
settings.json, and token_usage.db silently going stale. Runs on every
Claude Code SessionStart, and once daily via a launchd job for coverage on
days with no session. Never blocks anything — always exits 0.

Alerts (only when something is actually wrong) go to:
  - ~/.ruflo/logs/heartbeat.log   (always appended, one JSON line per check)
  - a macOS notification (osascript), problems only
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
DB_PATH = os.path.join(HOME, ".ruflo", "data", "token_usage.db")
LOG_PATH = os.path.join(HOME, ".ruflo", "logs", "heartbeat.log")
USAGE_HOOK = os.path.join(HOME, ".ruflo", "hooks", "claude-code-usage-hook.py")
STALE_AFTER_HOURS = 30  # generous: catches "dark for a day+", not every idle evening
HOOK_EVENTS = ("Stop", "SubagentStop")
DB_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _hooks_wired_in(settings: dict[str, Any], usage_hook: str = USAGE_HOOK) -> tuple[bool, str]:
    """Pure check: do Stop AND SubagentStop in parsed settings point at the usage hook?"""
    hooks = settings.get("hooks", {})
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        found = any(usage_hook in h.get("command", "") for entry in entries for h in entry.get("hooks", []))
        if not found:
            return False, f"{event} hook missing from settings.json"
    return True, "ok"


def _hooks_wired(settings_path: str | None = None, usage_hook: str | None = None) -> tuple[bool, str]:
    """True if Stop AND SubagentStop both point at the usage hook script (defaults: module paths)."""
    try:
        with open(settings_path or SETTINGS) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False, "settings.json missing or not valid JSON"
    return _hooks_wired_in(data, usage_hook or USAGE_HOOK)


def _last_row_timestamp(db_path: str) -> tuple[str | None, str | None]:
    """(MAX(timestamp), error) — exactly one of the two is set."""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            row = conn.execute("SELECT MAX(timestamp) FROM token_usage").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return None, f"db error: {exc}"
    return (row[0] if row else None), None


def _newest_transcript_mtime(projects_dir: str) -> float | None:
    """Newest mtime (epoch seconds) of any *.jsonl under projects_dir, or None."""
    newest: float | None = None
    try:
        for root, _, files in os.walk(projects_dir):
            for fn in files:
                if fn.endswith(".jsonl"):
                    mtime = os.path.getmtime(os.path.join(root, fn))
                    if newest is None or mtime > newest:
                        newest = mtime
    except OSError:
        pass  # unreadable tree: treat as no evidence of activity
    return newest


def _db_freshness(
    db_path: str | None = None,
    projects_dir: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """(is_stale, detail) — stale means the last row is older than the threshold
    AND there's evidence of actual Claude Code activity since then (avoids
    false alarms after a genuinely quiet stretch). Defaults: module paths, now."""
    db_path = db_path or DB_PATH
    projects_dir = projects_dir or PROJECTS_DIR
    if not os.path.exists(db_path):
        return True, "token_usage.db does not exist"
    last_ts, err = _last_row_timestamp(db_path)
    if err:
        return True, err
    if not last_ts:
        return True, "token_usage table is empty"

    try:
        last_dt = datetime.strptime(last_ts, DB_TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return True, f"unparseable timestamp: {last_ts}"

    now = now or datetime.now(timezone.utc)
    age = now - last_dt
    threshold = timedelta(hours=STALE_AFTER_HOURS)
    if age < threshold:
        return False, f"last row {age} ago"

    # only flag if a Claude Code transcript was actually touched more
    # recently than the last DB row — otherwise this is just a quiet day
    newest_transcript = _newest_transcript_mtime(projects_dir)
    if newest_transcript is None:
        return False, "no transcripts found, nothing to compare against"

    newest_dt = datetime.fromtimestamp(newest_transcript, tz=timezone.utc)
    if newest_dt - last_dt > threshold:
        return True, f"last DB row {age} ago, but sessions ran as recently as {now - newest_dt} ago"
    return False, f"last row {age} ago, no newer session activity"


def _notify(title: str, message: str) -> None:
    """macOS notification via osascript; silently a no-op if unavailable."""
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Basso"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass  # not macOS, or osascript hung: the log line is the durable record


def _log(entry: dict[str, Any], log_path: str | None = None) -> None:
    """Append one JSON line to the heartbeat log (default: LOG_PATH)."""
    log_path = log_path or LOG_PATH
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_check() -> dict[str, Any]:
    """Run both checks and return the log entry (no side effects)."""
    problems: list[str] = []

    wired, wired_detail = _hooks_wired()
    if not wired:
        problems.append(f"hooks: {wired_detail}")

    stale, stale_detail = _db_freshness()
    if stale:
        problems.append(f"tracker: {stale_detail}")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ok": not problems,
        "problems": problems,
        "hooks_detail": wired_detail,
        "db_detail": stale_detail,
    }


def main() -> None:
    entry = run_check()
    _log(entry)
    if entry["problems"]:
        _notify("Token tracker heartbeat FAILED", "; ".join(entry["problems"]))
    sys.exit(0)  # never block a session or a launchd run


if __name__ == "__main__":
    main()
