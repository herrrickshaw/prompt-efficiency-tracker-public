"""promptfoo custom Python provider wrapping llm_extract.extract_structured().

promptfoo calls call_api(prompt, options, context) for every test case and
grades the returned {"output": ...}. `prompt` here is the raw document text
(the promptfooconfig.yaml prompt is just `{{document_text}}`); the actual
prompt construction happens inside extract_structured() itself, in
llm_extract.py -- that's the file this pilot is meant to catch regressions
in, so we call the real function rather than re-implementing its prompt.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_extract import extract_structured  # noqa: E402


def call_api(prompt, options, context):
    # per-test config travels through `vars` (JSON-encoded) since promptfoo's
    # provider-level `options.config` is static across all tests, but each
    # test case here needs its own extraction schema.
    raw_cfg = (context or {}).get("vars", {}).get("extract_config", "{}")
    cfg = json.loads(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg
    schema = cfg["schema"]
    instructions = cfg.get("instructions", "")
    provider = cfg.get("provider", "groq")
    fallback = cfg.get("fallback_provider", "gemini")
    require_citations = cfg.get("require_citations", True)

    try:
        result = extract_structured(
            prompt,
            schema,
            instructions=instructions,
            provider=provider,
            fallback_provider=fallback,
            require_citations=require_citations,
        )
        return {"output": json.dumps(result)}
    except Exception as exc:
        return {"output": "", "error": str(exc)}
