"""
seed_defaults — copy bundled *.yaml to workflow_definitions on first boot.

Idempotent: uses SHA-256 checksum to skip unchanged definitions.
Mirrors TS runtime/seed-defaults.ts.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from engine.store.definition_store import DefinitionStore

logger = logging.getLogger("workflow.seed-defaults")

# Bundled defaults live alongside the plugin package
_BUNDLED_DEFAULTS_DIR = Path(__file__).resolve().parent.parent.parent / "defaults"


def seed_defaults(def_store: "DefinitionStore") -> Dict[str, Any]:
    """
    Seed bundled workflow YAMLs into workflow_definitions.
    Returns {"inserted": N, "skipped": M, "errors": K}.
    """
    result = def_store.seed_bundled(_BUNDLED_DEFAULTS_DIR)
    logger.info(
        "seed_defaults: inserted=%d updated=%d skipped=%d errors=%d from %s",
        result["inserted"],
        result.get("updated", 0),
        result["skipped"],
        result["errors"],
        _BUNDLED_DEFAULTS_DIR,
    )
    return result
