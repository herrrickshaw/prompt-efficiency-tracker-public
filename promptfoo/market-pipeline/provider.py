"""promptfoo custom Python provider wrapping llm_extract.extract_structured().

promptfoo calls call_api(prompt, options, context) for every test case and
grades the returned {"output": ...}. `prompt` here is the raw document text
(the promptfooconfig.yaml prompt is just `{{document_text}}`); the actual
prompt construction happens inside extract_structured() itself, in
llm_extract.py -- that's the file this pilot is meant to catch regressions
in, so we call the real function rather than re-implementing its prompt.

This file is meant to live at market-pipeline/code/python_files/promptfoo/
(next to llm_extract.py's directory); llm_extract is NOT part of this repo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_extract import extract_structured  # noqa: E402

DEFAULT_PROVIDER = "groq"
DEFAULT_FALLBACK = "gemini"


def parse_extract_config(context: dict[str, Any] | None) -> dict[str, Any]:
    """Per-test extraction config from `vars.extract_config` (JSON string or dict).

    Per-test config travels through `vars` (JSON-encoded) since promptfoo's
    provider-level `options.config` is static across all tests, but each
    test case here needs its own extraction schema.
    """
    raw_cfg = (context or {}).get("vars", {}).get("extract_config", "{}")
    return json.loads(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg


def call_api(prompt: str, options: Any, context: dict[str, Any] | None) -> dict[str, str]:
    """promptfoo provider entry point: returns {"output": json} or {"output": "", "error": msg}."""
    cfg = parse_extract_config(context)
    try:
        result = extract_structured(
            prompt,
            cfg["schema"],
            instructions=cfg.get("instructions", ""),
            provider=cfg.get("provider", DEFAULT_PROVIDER),
            fallback_provider=cfg.get("fallback_provider", DEFAULT_FALLBACK),
            require_citations=cfg.get("require_citations", True),
        )
        return {"output": json.dumps(result)}
    except Exception as exc:  # promptfoo contract: surface the error as a graded failure
        return {"output": "", "error": str(exc)}
