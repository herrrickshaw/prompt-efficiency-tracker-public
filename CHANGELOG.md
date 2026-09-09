# Changelog

## [1.0.0] — 2026-09-09 — Initial release: P0 outcome tracking + P1 promptfoo pilots

### Added
- `hooks/claude-code-usage-hook.py` — fixed the `Stop`/`SubagentStop` feeder for `token_usage.db`: real per-repo `market` (was hardcoded `''`), real `status` from transcript tool-error detection (was hardcoded `"success"`), fixed a dedup-state save/load key mismatch.
- `scripts/heartbeat_check.py` + `launchd/com.umashankar.heartbeat-check.plist` — catches the two failure modes that caused the original tracker to go dark for ~30 days: hooks silently dropped from `settings.json`, and the DB going stale relative to real session activity.
- `promptfoo/market-pipeline/` — pilot prompt-regression suite for `llm_extract.py`. Found and fixed two real bugs on its first run: deprecated `GROQ_MODEL`/`GEMINI_MODEL` in `~/.env.local`, and a citation-grounding gap for array-typed fields in `_verify_citation()`.
- `promptfoo/ship2shore-rag/` — pilot config for the RAG system prompts (prose + checklist mode, anti-hallucination test). Structurally complete; not yet run live (no `ANTHROPIC_API_KEY` configured in that repo).
- `reports/prompt_token_outcome_audit_2026-09-09.md` — the account-wide audit that motivated all of the above.

### Fixed (in other repos, referenced here)
- `market-pipeline/code/python_files/llm_extract.py` — deprecated model names, array-citation grounding.
- `~/.claude/settings.json` — `Stop`/`SubagentStop`/heartbeat `SessionStart` hooks re-wired.
- `~/.env.local` — `GROQ_MODEL`, `GEMINI_MODEL` updated to current models.

### Housekeeping
- `qmd cleanup` — 344 → 8 orphaned embedding chunks, 14 inactive docs removed, index compacted/vacuumed.
- `qmd update -c ruflo` + `qmd embed` — resynced the `ruflo` and `memory` collections.
