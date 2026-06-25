"""_library.py — canonical persona store loader for the personas plugin.

Python port of SwitchUI's personas-browser.ts. Reads the flat library/ directory
of markdown persona files, parses YAML frontmatter + markdown body, validates the
required keys, skips malformed files with a logged warning (never crashes), and
raises on a duplicate persona id (a real data error worth failing loud).

Loaded flat by the plugin loader / plugin_api via spec_from_file_location, so this
module uses NO relative imports.

Persona file format (preserve every key — do not narrow):
  YAML frontmatter: id, category, glyph, name, description, tags[],
                    default_model, default_memory_provider,
                    suggested_mcps[], suggested_toolsets[]
  Markdown body:    system_prompt (the persona overlay text)
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml  # PyYAML — available in the gateway runtime
except Exception:  # pragma: no cover - yaml is a hard dep of the runtime
    yaml = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# library/ lives next to this module: plugins/personas/library/
_LIBRARY_DIR = Path(__file__).resolve().parent / "library"

# Required frontmatter keys (mirror personas-browser.ts parsePersonaFile).
_REQUIRED_KEYS = ("id", "category", "glyph", "name")

# ^---\n(yaml)\n---\n(body)$  — tolerant of CRLF, mirrors the TS regex.
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)

# Module-level cache. First load populates; subsequent calls reuse.
_cache: Optional[Dict[str, Dict[str, Any]]] = None
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> Optional[tuple[Dict[str, Any], str]]:
    """Split a persona file into (frontmatter_dict, body). None if malformed."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    if yaml is None:  # pragma: no cover
        return None
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data, (m.group(2) or "")


def _parse_persona_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parse one persona file into a normalized dict. None (with warning) if invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        log.warning("[personas] Could not read %s", path)
        return None

    parsed = _parse_frontmatter(text)
    if parsed is None:
        log.warning("[personas] Skipping %s — no/invalid YAML frontmatter", path)
        return None
    data, body = parsed

    # Validate required keys are present and string-typed.
    if not all(isinstance(data.get(k), str) for k in _REQUIRED_KEYS):
        log.warning(
            "[personas] Skipping %s — missing required keys %s",
            path, ", ".join(_REQUIRED_KEYS),
        )
        return None

    def _str_list(v: Any) -> List[str]:
        return [str(x) for x in v] if isinstance(v, list) else []

    return {
        "id": data["id"],
        "category": data["category"],
        "glyph": data["glyph"],
        "name": data["name"],
        "description": data.get("description") if isinstance(data.get("description"), str) else "",
        "tags": _str_list(data.get("tags")),
        "system_prompt": body.strip(),
        "default_model": data.get("default_model") if isinstance(data.get("default_model"), str) else None,
        "default_memory_provider": data.get("default_memory_provider") if isinstance(data.get("default_memory_provider"), str) else None,
        "suggested_mcps": _str_list(data.get("suggested_mcps")),
        "suggested_toolsets": _str_list(data.get("suggested_toolsets")),
        "path": str(path),
    }


# ---------------------------------------------------------------------------
# Loading + cache
# ---------------------------------------------------------------------------

def _load(library_dir: Path = _LIBRARY_DIR) -> Dict[str, Dict[str, Any]]:
    """Load + validate every persona file into an id->persona dict.

    Raises ValueError on a duplicate id (real data error). Malformed individual
    files are skipped with a warning, not fatal.
    """
    personas: Dict[str, Dict[str, Any]] = {}
    if yaml is None:
        log.error("[personas] PyYAML unavailable — persona library will be empty")
        return personas
    if not library_dir.is_dir():
        log.warning("[personas] library dir missing: %s", library_dir)
        return personas

    for path in sorted(library_dir.glob("*.md")):
        persona = _parse_persona_file(path)
        if persona is None:
            continue
        pid = persona["id"]
        if pid in personas:
            # Operational resilience: a duplicate id must not brick plugin
            # startup (this runs inside register()). First file wins; the
            # later duplicate is skipped with a loud error, mirroring the
            # non-fatal handling of malformed files.
            log.error(
                "[personas] Duplicate persona id '%s' — keeping %s, skipping %s",
                pid, personas[pid]["path"], persona["path"],
            )
            continue
        personas[pid] = persona
    return personas


def _get_cache() -> Dict[str, Dict[str, Any]]:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = _load()
    return _cache


def reload() -> None:
    """Drop the cache so the next access re-reads from disk (tests / hot edits)."""
    global _cache
    with _cache_lock:
        _cache = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _metadata(p: Dict[str, Any], *, preview: bool = True) -> Dict[str, Any]:
    """Persona dict without the full system_prompt (metadata-only view)."""
    body = p["system_prompt"]
    out = {k: v for k, v in p.items() if k not in ("system_prompt", "path")}
    if preview:
        out["system_prompt_preview"] = body[:280]
        out["has_more_prompt"] = len(body) > 280
    return out


def list_personas(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return persona METADATA (no full prompt), sorted by (category, name).

    Optional category filter. Safe to call repeatedly (cached).
    """
    items = list(_get_cache().values())
    if category:
        items = [p for p in items if p["category"] == category]
    items.sort(key=lambda p: (p["category"], p["name"]))
    return [_metadata(p) for p in items]


def get_persona(persona_id: str) -> Optional[Dict[str, Any]]:
    """Return the full persona (incl. system_prompt) by id, or None if unknown."""
    p = _get_cache().get(persona_id)
    return dict(p) if p is not None else None


def count() -> int:
    """Number of valid personas loaded."""
    return len(_get_cache())
