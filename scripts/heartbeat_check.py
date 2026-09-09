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
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
DB_PATH = os.path.join(HOME, ".ruflo", "data", "token_usage.db")
LOG_PATH = os.path.join(HOME, ".ruflo", "logs", "heartbeat.log")
USAGE_HOOK = os.path.join(HOME, ".ruflo", "hooks", "claude-code-usage-hook.py")
STALE_AFTER_HOURS = 30  # generous: catches "dark for a day+", not every idle evening


def _hooks_wired():
    """True if Stop AND SubagentStop both point at the usage hook script."""
    try:
        with open(SETTINGS) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False, "settings.json missing or not valid JSON"

    hooks = data.get("hooks", {})
    for event in ("Stop", "SubagentStop"):
        entries = hooks.get(event, [])
        found = any(
            USAGE_HOOK in h.get("command", "")
            for entry in entries
            for h in entry.get("hooks", [])
        )
        if not found:
            return False, f"{event} hook missing from settings.json"
    return True, "ok"


def _db_freshness():
    """(is_stale, detail) — stale means the last row is older than the threshold
    AND there's evidence of actual Claude Code activity since then (avoids
    false alarms after a genuinely quiet stretch)."""
    if not os.path.exists(DB_PATH):
        return True, "token_usage.db does not exist"
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        row = conn.execute("SELECT MAX(timestamp) FROM token_usage").fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return True, f"db error: {exc}"

    last_ts = row[0] if row else None
    if not last_ts:
        return True, "token_usage table is empty"

    try:
        last_dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return True, f"unparseable timestamp: {last_ts}"

    age = datetime.now(timezone.utc) - last_dt
    if age < timedelta(hours=STALE_AFTER_HOURS):
        return False, f"last row {age} ago"

    # only flag if a Claude Code transcript was actually touched more
    # recently than the last DB row — otherwise this is just a quiet day
    projects_dir = os.path.join(HOME, ".claude", "projects")
    newest_transcript = None
    try:
        for root, _, files in os.walk(projects_dir):
            for fn in files:
                if fn.endswith(".jsonl"):
                    mtime = os.path.getmtime(os.path.join(root, fn))
                    if newest_transcript is None or mtime > newest_transcript:
                        newest_transcript = mtime
    except OSError:
        pass

    if newest_transcript is None:
        return False, "no transcripts found, nothing to compare against"

    newest_dt = datetime.fromtimestamp(newest_transcript, tz=timezone.utc)
    if newest_dt - last_dt > timedelta(hours=STALE_AFTER_HOURS):
        return True, f"last DB row {age} ago, but sessions ran as recently as {datetime.now(timezone.utc) - newest_dt} ago"
    return False, f"last row {age} ago, no newer session activity"


def _notify(title, message):
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Basso"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def _log(entry):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    problems = []

    wired, wired_detail = _hooks_wired()
    if not wired:
        problems.append(f"hooks: {wired_detail}")

    stale, stale_detail = _db_freshness()
    if stale:
        problems.append(f"tracker: {stale_detail}")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ok": not problems,
        "problems": problems,
        "hooks_detail": wired_detail,
        "db_detail": stale_detail,
    }
    _log(entry)

    if problems:
        _notify("Token tracker heartbeat FAILED", "; ".join(problems))

    sys.exit(0)  # never block a session or a launchd run


if __name__ == "__main__":
    main()
