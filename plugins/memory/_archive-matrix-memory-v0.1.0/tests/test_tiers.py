from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "hermes_plugins.matrix_memory"


def _ensure_parent_namespace() -> None:
    if "hermes_plugins" not in sys.modules:
        parent = importlib.util.module_from_spec(
            importlib.machinery.ModuleSpec("hermes_plugins", loader=None, is_package=True)
        )
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent


def _load_package():
    _ensure_parent_namespace()
    module = sys.modules.get(PKG_NAME)
    if module is not None and getattr(module, "__file__", None):
        return module
    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_submodule(name: str):
    _load_package()
    fullname = f"{PKG_NAME}.{name}"
    module = sys.modules.get(fullname)
    if module is not None and getattr(module, "__file__", None):
        return module
    path = ROOT / (name.replace(".", "/") + ".py")
    spec = importlib.util.spec_from_file_location(fullname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


tier1_mod = _load_submodule("tiers.tier1_episodic")
tier2_mod = _load_submodule("tiers.tier2_wiki")
tier3_mod = _load_submodule("tiers.tier3_fts5")
Tier1Store = tier1_mod.Tier1Store
WikiStore = tier2_mod.WikiStore
FTSIndex = tier3_mod.FTSIndex


def test_tier1_round_trip(tmp_path: Path):
    store = Tier1Store(tmp_path)
    store.add_entry("memory", "User prefers dark mode")
    store.add_entry("user", "Calls project Atlas")
    memory_entries, user_entries = store.read_all()
    assert memory_entries == ["User prefers dark mode"]
    assert user_entries == ["Calls project Atlas"]
    assert store.search("dark")[0]["target"] == "memory"


def test_tier2_wiki_structure_and_links(tmp_path: Path):
    wiki = WikiStore(tmp_path / "wiki")
    wiki.ensure_structure()
    rel = wiki.write_page(title="Project Atlas", content="Depends on [[EU Team]].", folder="entities")
    assert rel == "entities/project-atlas.md"
    assert wiki.resolve_link("Project Atlas") == rel
    assert wiki.extract_links(wiki.read_page(rel)) == ["EU Team"]
    assert (tmp_path / "wiki" / "index.md").exists()


def test_tier3_index_and_search(tmp_path: Path):
    wiki = WikiStore(tmp_path / "wiki")
    wiki.ensure_structure()
    rel = wiki.write_page(title="Atlas Rollout", content="Atlas rollout is scheduled for Q3.", folder="queries")
    content = wiki.read_page(rel)
    index = FTSIndex(tmp_path / "memory.db", chunk_chars=200)
    index.ensure_schema()
    index.index_page(rel, content, wiki.chunks_for_page(rel, content))
    hits = index.search("Atlas rollout", top_k=5)
    assert hits
    assert hits[0]["path"] == rel
