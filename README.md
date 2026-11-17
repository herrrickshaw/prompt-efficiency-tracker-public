# prompt-efficiency-tracker (public snapshot)

Outcome and attribution tracking for Claude Code usage on one developer account, plus
prompt-regression test configs (promptfoo) for the LLM calls inside two sibling repositories.
It produces rows in a local SQLite token-usage database (`~/.ruflo/data/token_usage.db`), a
heartbeat log that says whether that tracking is still alive, and promptfoo eval results.

> Status: archived snapshot. This is a public copy of a private tool, taken 2026-09; the live
> versions of the hook and heartbeat run from `~/.ruflo/` on the owner's machine, and one audit
> report containing personal usage-spend figures was left out of this copy and its history
> (`reports/prompt_token_outcome_audit_2026-09-09.md` is referenced by `CHANGELOG.md` but is
> not here). No data, no secrets; macOS-specific in places (`osascript`, `launchd`).

<!-- how-to-use-with-ai -->
> **New here?** See [How to use this repo with AI (Claude, Gemini, ChatGPT)](HOW_TO_USE_WITH_AI.md).

## What it does
- `hooks/claude-code-usage-hook.py` — a Claude Code `Stop`/`SubagentStop` hook that reads the
  session transcript and appends one row per assistant message (tokens, estimated USD cost, model,
  repo, status) to `token_usage.db`, deduplicated by message id.
- Attributes each row to a repo (`market` = `git rev-parse --show-toplevel` of the hook's cwd) and
  derives `status` (`success` / `tool_error`) from whether a `tool_result` with `is_error: true`
  appeared since the previous logged message — replacing a hardcoded `"success"` literal.
- Writes `WARNING` (≥70%) / `CRITICAL` (≥85%) alerts against a 200,000-token daily budget to
  `~/.ruflo/logs/token_alerts.log`.
- `scripts/heartbeat_check.py` — checks that both hooks are still wired in `~/.claude/settings.json`
  and that the DB is not stale (last row older than 30 h while a newer transcript exists); logs every
  check, sends a macOS notification only on failure, always exits 0.
- `launchd/com.umashankar.heartbeat-check.plist` — daily 09:00 run of the heartbeat for days without
  a session.
- `promptfoo/` — two prompt-regression configs: `market-pipeline/` (structured extraction with
  citation grounding, live and green on 2026-09-09; results checked in) and `ship2shore-rag/` (RAG
  system prompts incl. an anti-hallucination test; never run — no `ANTHROPIC_API_KEY` was set).

## Quick start
```bash
git clone https://github.com/herrrickshaw/prompt-efficiency-tracker-public.git
cd prompt-efficiency-tracker-public
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # stdlib only at runtime; pytest + ruff for development
echo '{"transcript_path": "/path/to/session.jsonl", "hook_event_name": "Stop"}' \
  | RUFLO_HOME=/tmp/ruflo python hooks/claude-code-usage-hook.py
```
The hook expects a `token_usage` table to already exist in the DB (it adds the `msg_id` column and
unique index itself); on any DB error it prints to stderr and still exits 0.

## How it works
| Stage | Module | Output |
|---|---|---|
| Claude Code fires `Stop`/`SubagentStop` with event JSON on stdin | `hooks/claude-code-usage-hook.py::main` | — |
| Scan transcript JSONL for assistant messages with `usage`, flag tool errors | `_iter_assistant_usage`, `_has_tool_error` | `(msg_id, model, usage, ts, had_tool_error)` |
| Skip already-logged ids, price tokens by model family | `_build_rows`, `_estimate_cost`, `PRICES` | DB rows |
| `INSERT OR IGNORE` + unique index on `msg_id`; sum today's tokens | `_write_rows`, `_ensure_schema` | `token_usage.db` |
| Persist per-transcript dedup cache; write budget alert if over threshold | `_save_state`, `_alert_level`, `_write_alert` | `hook-state/*.json`, `token_alerts.log` |
| `SessionStart` hook / daily launchd: hooks wired? DB fresh? | `scripts/heartbeat_check.py::run_check` | `heartbeat.log`, notification |

Design decisions:
- Dedup is keyed on a hash of the transcript path, not `session_id`, because one transcript can be
  read under several session ids. A DB unique index on `msg_id` is the hard guarantee; the JSON cache
  only avoids rescanning the whole file on every `Stop`.
- Staleness compares the DB's `MAX(timestamp)` with the newest `~/.claude/projects/**/*.jsonl`
  mtime, so a genuinely quiet day does not false-alarm.
- Cost is an estimate: input rate × 1.25 for cache writes, × 0.10 for cache reads; unknown models
  are priced as Sonnet.
- Both scripts never block a session: every exit path is `exit 0`.

## Repository layout
| Path | Purpose |
|---|---|
| `hooks/claude-code-usage-hook.py` | Stop/SubagentStop feeder for `token_usage.db` |
| `scripts/heartbeat_check.py` | Tracker liveness check (SessionStart hook + launchd) |
| `launchd/com.umashankar.heartbeat-check.plist` | Daily 09:00 launchd job (paths are the owner's `~/.ruflo/`) |
| `promptfoo/market-pipeline/` | `promptfooconfig.yaml`, custom `provider.py` wrapping `llm_extract.extract_structured()` (that module lives in the `market-pipeline` repo, not here), and `results-2026-09-09.json` |
| `promptfoo/ship2shore-rag/` | `promptfooconfig.yaml` for the `ship2shore_rag` system prompts |
| `tests/` | pytest unit + smoke tests for the two scripts and the provider (no `$HOME` access) |
| `graphify-out/` | Generated knowledge-graph output; not hand-edited |
| `CHANGELOG.md`, `HOW_TO_USE_WITH_AI.md` | Release notes; non-programmer guide |

## Configuration
All optional, read by `hooks/claude-code-usage-hook.py`:

| Variable | Default | Meaning |
|---|---|---|
| `RUFLO_HOME` | `~/.ruflo` | Root for DB, logs and the `data/hook-state/` dedup cache |
| `RUFLO_MONITOR_DB` | `$RUFLO_HOME/data/token_usage.db` | SQLite DB to append to |
| `RUFLO_ALERT_LOG` | `$RUFLO_HOME/logs/token_alerts.log` | JSONL budget-alert log |

`scripts/heartbeat_check.py` derives its paths from `$HOME` (`~/.claude/settings.json`,
`~/.claude/projects/`, `~/.ruflo/`). The promptfoo configs need `GROQ_API_KEY` (+ `GEMINI_API_KEY`
fallback) and `ANTHROPIC_API_KEY` respectively, supplied via the environment of the repo they run in;
nothing is committed here.

To wire the hooks into another Claude Code setup: copy the two scripts somewhere stable, add
`Stop`, `SubagentStop` (matcher `""`) and `SessionStart` (matcher `"startup|resume"`) entries to
`~/.claude/settings.json` pointing at them, and optionally load the plist (or a cron line).

## Testing
```bash
pytest -q        # 41 tests, all offline, run in a temp dir
ruff check .
```

## Limitations and known issues
- Snapshot of a private tool: the hook and heartbeat are not maintained here, and the `launchd`
  plist hard-codes the owner's home directory.
- `promptfoo/market-pipeline/provider.py` imports `llm_extract` from a sibling directory in the
  `market-pipeline` repo; it cannot run standalone (tests stub it).
- `promptfoo/ship2shore-rag/` has never been executed.
- Prices in `PRICES` are hard-coded per model family and will drift from vendor pricing.
- `status = tool_error` is an approximate outcome signal (any errored tool result since the last
  assistant message), not a judgement of whether the turn succeeded.
- The pilot's first run found two bugs in the *other* repo (deprecated `GROQ_MODEL`/`GEMINI_MODEL`
  names, and `_verify_citation()` rejecting list-valued citation spans); those fixes live in
  `market-pipeline`, not here — see `CHANGELOG.md`.

## Related repositories
- `market-pipeline` — owns `llm_extract.py`, the target of the `promptfoo/market-pipeline/` config.
- `ship2shore_rag` — owns `rag/pipeline.py`, the target of the `promptfoo/ship2shore-rag/` config.

## Licence
No licence file yet — all rights reserved.
