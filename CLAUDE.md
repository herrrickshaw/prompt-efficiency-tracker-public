# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this repo is
Public snapshot (2026-09) of a private tool: a Claude Code `Stop`/`SubagentStop` hook that feeds
`~/.ruflo/data/token_usage.db`, a heartbeat that checks the tracking is alive, and two promptfoo
prompt-regression configs. See `README.md` for the pipeline table; keep both files consistent.

## Commands
```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
pytest -q          # offline; every test redirects $HOME-derived paths to a temp dir
ruff check .       # config in pyproject.toml, target py39
```

## Rules
- Behaviour-preserving edits only: both scripts must keep exiting 0 on every path, and the hook's
  dedup (transcript-hash state file + unique index on `msg_id`) must not change.
- `graphify-out/` and `promptfoo/market-pipeline/results-*.json` are generated; never hand-edit.
- `promptfoo/market-pipeline/provider.py` imports `llm_extract` from the `market-pipeline` repo; it
  is not importable here — stub it in tests (see `tests/test_provider.py`).
- Never commit keys. `GROQ_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` come from the environment
  of the repo the promptfoo configs run in.

<!-- source-fetch-toolkit:v1 (managed by ~/bin/add_source_fetch_section.py — edit there, not here) -->
## Source fetching for data collection & literature surveys

When a task needs a primary document (statute, regulator data, government release, paper, filing),
fetch the document itself — never cite a search snippet as if it had been read.

- **On the owner's Mac:** `fetch_source.py "<url | doi:… | celex:… | arxiv:…>" --out ./sources`
  (in `~/bin`; full guide `~/bin/SOURCE_FETCH_TOOLKIT.md`). Scheduled/unattended runs use only
  `bash /Users/umashankar/bin/fetch_source.sh "<target>"` (allowlisted) and then Read the `.txt` it reports.
- **Elsewhere (cloud sessions, CI):** follow the same ladder by hand — direct GET with a browser User-Agent →
  headless browser only if the page needs JavaScript → official alternates (DOI → OpenAlex
  `api.openalex.org/works/doi:<doi>` locations → repository copy; EUR-Lex → EU Publications Office
  `publications.europa.eu/resource/celex/<CELEX>`; arXiv pdf/abs) → Wayback Machine `id_` snapshot.
- **Hard rule:** a CAPTCHA, robot check, Cloudflare/Akamai/WAF challenge or login wall means stop on that
  host. Do not bypass it (no TLS impersonation, stealth browsers, identity rotation or challenge solving);
  look for the same document via another route, or report it as unavailable.
- **Record** for every figure: URL read, title/date, page/table, fetch route; mark abstract-only or
  snippet-only sources. Fetch politely (~1 request/second per host, no bulk crawling).
<!-- /source-fetch-toolkit -->
