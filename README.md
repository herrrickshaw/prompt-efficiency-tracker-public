# prompt-efficiency-tracker

Real outcome/attribution tracking for Claude Code usage across this account, plus prompt-regression testing for the LLM calls buried inside a few of the other repos here. Built after a 2026-09-09 audit found the existing token tracker (`~/.ruflo/data/token_usage.db`, fed by a homegrown hook) recorded volume and cost fine but had **zero real outcome signal**: its `status` column was hardcoded to the literal string `"success"`, `market`/`task_name` were blank or generic on every row, and the hook that fed it had been silently dropped from `~/.claude/settings.json` for a month with nothing noticing.

Full narrative report: [`reports/prompt_token_outcome_audit_2026-09-09.md`](reports/prompt_token_outcome_audit_2026-09-09.md).

## What's in here

| Path | What | Status |
|---|---|---|
| `hooks/claude-code-usage-hook.py` | The `Stop`/`SubagentStop` hook that writes `token_usage.db`. Patched: `market` now comes from `git rev-parse --show-toplevel` on the hook's cwd instead of a hardcoded `''`; `status` reflects whether a `tool_result` with `is_error:true` appeared in the transcript since the last logged turn, instead of a hardcoded `"success"`. Also fixed a dedup-state bug (saved under `session_id`, loaded under a transcript-hash key — harmless, since a DB unique index on `msg_id` caught the resulting duplicates, but meant every `Stop` rescanned the whole transcript). | **Live** — deployed to `~/.claude/settings.json` and `~/.ruflo/hooks/`, verified against real Stop-hook traffic from two concurrent sessions the same day. |
| `scripts/heartbeat_check.py` | Checks the two failure modes that actually happened: hooks silently unwired from `settings.json`, and the DB going stale relative to real session activity (compares `MAX(timestamp)` against the newest `.claude/projects/**/*.jsonl` mtime, so a genuinely quiet day doesn't false-alarm). Logs every check to `~/.ruflo/logs/heartbeat.log`; fires a macOS notification only when something's actually wrong. Always exits 0 — never blocks a session. | **Live** — wired as a `SessionStart` hook (fires every session) and a daily `launchd` job. |
| `launchd/com.umashankar.heartbeat-check.plist` | Daily 09:00 run of the heartbeat, for coverage on days without a Claude Code session. | **Live** — loaded via `launchctl`. |
| `promptfoo/market-pipeline/` | Pilot prompt-regression config for `market-pipeline/code/python_files/llm_extract.py` (SEBI DRHP + portfolio structured extraction, with citation-grounding). | **Live, green.** Caught two real bugs on its first run (see below). |
| `promptfoo/ship2shore-rag/` | Pilot config for `ship2shore_rag/rag/pipeline.py`'s two system prompts (prose + checklist mode), including an anti-hallucination test (must refuse an out-of-scope question rather than guess). | **Config complete, not yet live** — that repo's `ANTHROPIC_API_KEY` isn't set locally. Runs the moment one is added: `cd promptfoo/ship2shore-rag && ANTHROPIC_API_KEY=... npx promptfoo eval`. |

`global-stock-screener` (the other repo flagged in the original P1 backlog as a pilot target, based on its high fix:feat commit ratio) turned out to have no LLM-facing prompts to test — it's deterministic screening/scoring code. `ship2shore_rag` was substituted because it actually has real, testable system prompts.

## Bugs the promptfoo pilot caught on its first real run

Piloting isn't just "does the tool run" — the very first live `npx promptfoo eval` against `llm_extract.py` failed both test cases, for real reasons:

1. **Deprecated models, silently swallowed.** `~/.env.local` had `GROQ_MODEL=llama-3.3-70b-versatile` and `GEMINI_MODEL=gemini-2.0-flash` — both retired by their providers. `extract_structured()`'s fallback logic (primary provider → fallback provider) meant every call failed on *both* providers and surfaced one generic stacked error, so the feature (used for SEBI DRHP filing parsing and portfolio broker-statement parsing) had been silently non-functional. Fixed: `GROQ_MODEL=openai/gpt-oss-120b` (Groq's current largest general-purpose model — no Llama chat model remains on this account's Groq catalog, worth revisiting if extraction quality regresses), `GEMINI_MODEL=gemini-3.6-flash` (the exact successor Google's own 404 error named).
2. **Citation-grounding didn't handle array fields.** `_verify_citation()` only accepted a single string span. For an array-typed field (`holdings: [...]`), the model correctly returns *one citation per item* — a list — which the old code rejected outright, marking a perfectly-grounded extraction as `grounded: false`. Fixed in `llm_extract.py`: a list span now verifies element-by-element.

Both fixes are applied in the live `market-pipeline` repo, not just here — this repo holds copies for reference and reuse.

## qmd cleanup (2026-09-09)

`qmd status` was reporting 344 orphaned embedding chunks (43% of the index) and a `ruflo` doc collection 40 days stale against a 2-day-fresh `memory` collection. Ran `qmd cleanup` (344 → 8 orphaned chunks, 14 inactive doc records and content hashes removed, FTS compacted + vacuumed, 7.0MB → 5.8MB) and `qmd update -c ruflo` + `qmd embed` to resync the index. No content changes — this was purely index hygiene.

## Reuse elsewhere

The hooks + heartbeat pattern isn't specific to this machine's exact paths beyond `$HOME`. To wire it into another Claude Code setup:
1. Copy `hooks/claude-code-usage-hook.py` and `scripts/heartbeat_check.py` somewhere stable.
2. Add `Stop`, `SubagentStop`, and a `SessionStart` entry to `~/.claude/settings.json` pointing at them (see the live config for the exact shape — matcher `""` for Stop/SubagentStop, `"startup|resume"` for SessionStart).
3. Optionally add the `launchd` plist (macOS) or an equivalent cron entry for idle-day coverage.
