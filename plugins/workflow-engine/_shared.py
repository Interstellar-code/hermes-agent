"""Shared engine factory — single sys.path injection, single create_engine call.

This module is the ONLY place that performs sys.path manipulation.
Both dashboard/plugin_api.py and __init__.py (agent tools) import from here.
daemon.py also imports from here to share the same engine instance within a
single process.

Thread safety: get_engine() uses a threading.Lock to prevent double-construction
in multi-threaded servers. Each engine runs its coroutines on its own loop
thread (see engine/facade.py).
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# These imports resolve because sys.path was just extended above.
from engine import WorkflowEngine, create_engine  # noqa: E402
from hermes_constants import get_hermes_home, get_hermes_home_override  # noqa: E402

# Keyed by HERMES_HOME so a multi-profile process gets one engine (one DB)
# per profile instead of binding whichever profile asked first (L2-23).
_engines: Dict[str, WorkflowEngine] = {}
_engine_lock = threading.Lock()
_llm: Any = None  # host PluginLlm, applied when an engine is first built
# Home active when register() ran (the plugin loader's profile scope). Used
# when a later caller has no explicit override — e.g. the dashboard process
# runs with HERMES_HOME=~/.hermes but loaded this plugin for a profile.
_register_home: Optional[str] = None


def bind_register_home() -> None:
    global _register_home
    _register_home = str(get_hermes_home())


def _home_key() -> str:
    override = get_hermes_home_override()
    if override:
        return override
    return _register_home or str(get_hermes_home())


def set_llm(llm: Any) -> None:
    """Stash the host LLM; applied lazily so plugin load builds no engine (L2-24)."""
    global _llm
    _llm = llm
    # dashboard/plugin_api.py loads a flat copy of this module, so this module's
    # globals are invisible to it; the ``engine`` package is shared by both.
    import engine as _engine_pkg  # noqa: PLC0415
    _engine_pkg.HOST_LLM = llm
    with _engine_lock:
        for eng in _engines.values():
            eng.set_llm(llm)


def _db_path_for(home: str) -> Optional[str]:
    """Explicit DB path for ``home`` unless WORKFLOW_DB_PATH overrides."""
    if os.environ.get("WORKFLOW_DB_PATH"):
        return None
    return str(Path(home) / "switchui-workflows.db")


def get_engine() -> WorkflowEngine:
    """Return this profile's WorkflowEngine, constructing it on first call."""
    key = _home_key()
    eng = _engines.get(key)
    if eng is None:
        with _engine_lock:
            eng = _engines.get(key)
            if eng is None:
                eng = create_engine(_db_path_for(key))
                import engine as _engine_pkg  # noqa: PLC0415
                llm = _llm or getattr(_engine_pkg, "HOST_LLM", None)
                if llm is not None:
                    eng.set_llm(llm)
                _engines[key] = eng
    return eng
