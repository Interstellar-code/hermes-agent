"""test_library.py — persona store loader: parity, validation, skip, dup-id.

Loads _library via spec_from_file_location (flat plugin load convention).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # plugins/personas/


def _load_library() -> Any:
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    spec = importlib.util.spec_from_file_location(
        "personas_library", _PLUGIN_DIR / "_library.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Expected migration parity — guards a lossy copy.
_EXPECTED_TOTAL = 20
_EXPECTED_CATEGORIES = {
    "design": 2, "devops": 2, "engineering": 4, "leadership": 4,
    "product": 2, "research": 2, "testing": 2, "writing": 2,
}


def test_library_total_count():
    lib = _load_library()
    lib.reload()
    assert lib.count() == _EXPECTED_TOTAL, f"expected {_EXPECTED_TOTAL} personas, got {lib.count()}"


def test_category_parity():
    lib = _load_library()
    lib.reload()
    counts: dict[str, int] = {}
    for p in lib.list_personas():
        counts[p["category"]] = counts.get(p["category"], 0) + 1
    assert counts == _EXPECTED_CATEGORIES, f"category mismatch: {counts}"


def test_list_is_metadata_only():
    """list_personas must NOT leak full system_prompt; only a preview."""
    lib = _load_library()
    lib.reload()
    for p in lib.list_personas():
        assert "system_prompt" not in p, f"{p['id']} leaked full system_prompt in list"
        assert "system_prompt_preview" in p


def test_list_sorted_by_category_then_name():
    lib = _load_library()
    lib.reload()
    items = lib.list_personas()
    keys = [(p["category"], p["name"]) for p in items]
    assert keys == sorted(keys), "list_personas not sorted by (category, name)"


def test_category_filter():
    lib = _load_library()
    lib.reload()
    eng = lib.list_personas(category="engineering")
    assert len(eng) == 4
    assert all(p["category"] == "engineering" for p in eng)


def test_get_persona_full_prompt():
    lib = _load_library()
    lib.reload()
    p = lib.get_persona("engineering-security-engineer")
    assert p is not None
    assert p["system_prompt"].strip(), "system_prompt must be non-empty"
    assert p["name"] and p["glyph"] and p["category"] == "engineering"


def test_get_unknown_returns_none():
    lib = _load_library()
    lib.reload()
    assert lib.get_persona("does-not-exist") is None


def test_all_personas_have_nonempty_prompt():
    lib = _load_library()
    lib.reload()
    for meta in lib.list_personas():
        full = lib.get_persona(meta["id"])
        assert full is not None and full["system_prompt"].strip(), f"{meta['id']} empty prompt"


def test_skip_malformed_no_crash(tmp_path):
    """A malformed file is skipped (warning), not fatal; valid files still load."""
    lib = _load_library()
    (tmp_path / "good.md").write_text(
        "---\nid: x\ncategory: c\nglyph: GX\nname: X\n---\nbody", encoding="utf-8"
    )
    (tmp_path / "bad.md").write_text("no frontmatter here", encoding="utf-8")
    loaded = lib._load(tmp_path)
    assert set(loaded.keys()) == {"x"}, "malformed file should be skipped, good kept"


def test_duplicate_id_skipped_first_wins(tmp_path):
    """Duplicate id must NOT crash startup — first file wins, second skipped."""
    lib = _load_library()
    (tmp_path / "a.md").write_text(
        "---\nid: dup\ncategory: c\nglyph: G1\nname: A\n---\na", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\nid: dup\ncategory: c\nglyph: G2\nname: B\n---\nb", encoding="utf-8"
    )
    loaded = lib._load(tmp_path)
    assert set(loaded.keys()) == {"dup"}, "duplicate id must not raise; keep one entry"
    assert loaded["dup"]["name"] == "A", "first file (sorted) must win"
