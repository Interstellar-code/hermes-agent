"""
ManifestWriter — writes HERMES_HOME/workflows-manifest.json on boot.

Mirrors TS runtime/manifest.ts: lists all known workflow definitions
and writes a compact JSON manifest for Hermes chat-based launch routing.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from hermes_constants import get_hermes_home
from typing import Any, Dict, List, Optional

from engine.store.definition_store import DefinitionStore

logger = logging.getLogger("workflow.manifest")

_MANIFEST_PATH = get_hermes_home() / "workflows-manifest.json"


class ManifestWriter:
    def __init__(self, def_store: DefinitionStore) -> None:
        self._def_store = def_store

    def write(self, *, out_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Enumerate workflow definitions, write manifest JSON.
        Returns {"entries_written": N, "path": str}.
        """
        path = out_path or _MANIFEST_PATH
        try:
            rows = self._def_store.list_definitions(kind="workflow")
            entries: List[Dict[str, Any]] = []
            for row in rows:
                entries.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "description": row.get("description") or "",
                        "source": row.get("source", "user"),
                        "kind": row.get("kind", "workflow"),
                    }
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"workflows": entries}, indent=2),
                encoding="utf-8",
            )
            logger.info("ManifestWriter: wrote %d entries to %s", len(entries), path)
            return {"entries_written": len(entries), "path": str(path)}
        except Exception as exc:
            logger.warning("ManifestWriter: failed to write manifest: %s", exc)
            return {"entries_written": 0, "path": str(path), "error": str(exc)}
