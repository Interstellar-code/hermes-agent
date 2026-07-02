"""Shared engine factory — single sys.path injection, single create_engine call.

This module is the ONLY place that performs sys.path manipulation.
Both dashboard/plugin_api.py and __init__.py (agent tools) import from here.
daemon.py also imports from here to share the same engine instance within a
single process.

Thread safety: get_engine() uses a threading.Lock to prevent double-construction
in multi-threaded servers.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Optional

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# These imports resolve because sys.path was just extended above.
from engine import WorkflowEngine, create_engine  # noqa: E402

_engine: Optional[WorkflowEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> WorkflowEngine:
    """Return the process-wide WorkflowEngine singleton, constructing it on first call."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = create_engine()
    return _engine
