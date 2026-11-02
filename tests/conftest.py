"""Load the hook/heartbeat scripts as modules without touching $HOME paths."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def usage_hook(tmp_path, monkeypatch):
    """The usage hook module with RUFLO_HOME redirected to a temp dir."""
    monkeypatch.setenv("RUFLO_HOME", str(tmp_path / "ruflo"))
    monkeypatch.delenv("RUFLO_MONITOR_DB", raising=False)
    monkeypatch.delenv("RUFLO_ALERT_LOG", raising=False)
    return _load("usage_hook", REPO / "hooks" / "claude-code-usage-hook.py")


@pytest.fixture
def heartbeat(tmp_path, monkeypatch):
    """The heartbeat module with every $HOME-derived path pointed at a temp dir."""
    mod = _load("heartbeat_check", REPO / "scripts" / "heartbeat_check.py")
    monkeypatch.setattr(mod, "SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mod, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "token_usage.db"))
    monkeypatch.setattr(mod, "LOG_PATH", str(tmp_path / "logs" / "heartbeat.log"))
    monkeypatch.setattr(mod, "USAGE_HOOK", str(tmp_path / "hook.py"))
    monkeypatch.setattr(mod, "_notify", lambda *a, **k: None)
    os.makedirs(tmp_path / "projects", exist_ok=True)
    return mod
