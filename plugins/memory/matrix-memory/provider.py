from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
from utils import atomic_json_write

from .chat.audit import build_audit_report
from .chat.compare import compare_pages
from .chat.find_dead_links import find_dead_links
from .chat.find_duplicates import find_duplicates
from .chat.find_orphans import find_orphans
from .chat.find_stale import find_stale
from .chat.list import list_pages
from .chat.session_mode import requires_session_mode
from .chat.show import show_page
from .chat.trace import trace_links
from .routing import route_recall
from .safety.confirm_token import ConfirmTokenManager
from .safety.dry_run import build_dry_run_preview
from .safety.log_writer import append_log_entry
from .tiers.tier1_episodic import Tier1Store
from .tiers.tier2_wiki import WikiStore
from .tiers.tier3_fts5 import FTSIndex
from .tools import CHAT_SCHEMAS, build_base_tool_schemas

logger = logging.getLogger(__name__)


class MatrixMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._session_id = ""
        self._mode = "normal"
        self._hermes_home = Path(".")
        self._root = Path(".")
        self._config: Dict[str, Any] = {}
        self._tier1: Tier1Store | None = None
        self._wiki: WikiStore | None = None
        self._fts: FTSIndex | None = None
        self._confirm = ConfirmTokenManager()

    @property
    def name(self) -> str:
        return "matrix-memory"

    def is_available(self) -> bool:
        return True

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "wiki_root", "description": "Wiki root relative to HERMES_HOME", "default": "matrix-memory/wiki"},
            {"key": "stale_after_days", "description": "Default stale-page threshold in days", "default": "30"},
            {"key": "chunk_chars", "description": "Approximate chunk size for FTS indexing", "default": "800"},
        ]

    def save_config(self, values, hermes_home):
        path = Path(hermes_home) / "matrix-memory" / "matrix_memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing.update(values)
        atomic_json_write(path, existing, mode=0o600)

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._mode = str(kwargs.get("mode") or "normal")
        self._hermes_home = Path(kwargs.get("hermes_home") or ".")
        self._root = self._hermes_home / "matrix-memory"
        self._root.mkdir(parents=True, exist_ok=True)
        self._config = self._load_config()
        self._tier1 = Tier1Store(self._hermes_home)
        self._wiki = WikiStore(self._root / "wiki")
        self._wiki.ensure_structure()
        self._fts = FTSIndex(self._root / "memory.db", chunk_chars=int(self._config.get("chunk_chars", 800)))
        self._fts.ensure_schema()
        self._fts.reindex_missing(self._wiki.iter_pages(), self._wiki.read_page, self._wiki.chunks_for_page)

    def _load_config(self) -> Dict[str, Any]:
        path = self._root / "matrix_memory.json"
        config: Dict[str, Any] = {"wiki_root": "matrix-memory/wiki", "stale_after_days": 30, "chunk_chars": 800}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config.update(loaded)
            except Exception:
                logger.debug("matrix-memory: failed reading config", exc_info=True)
        return config

    def system_prompt_block(self) -> str:
        return (
            "# Matrix Memory\n"
            f"Active. Session mode: {self._mode}. "
            "Use memory_recall to search across tier1 facts, wiki pages, and the SQLite FTS index. "
            "Use memory_note/memory_ingest to add knowledge and memory_status to inspect health."
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas = build_base_tool_schemas()
        if self._mode == "chat":
            schemas.extend(CHAT_SCHEMAS)
        return schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        handler = getattr(self, tool_name, None)
        if handler is None:
            return json.dumps({"success": False, "error": f"Unknown Matrix Memory tool '{tool_name}'"}, ensure_ascii=False)
        try:
            return json.dumps(handler(args), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.exception("matrix-memory tool failure: %s", tool_name)
            return json.dumps({"success": False, "error": f"Matrix Memory tool '{tool_name}' failed: {exc}"}, ensure_ascii=False)

    def _require_ready(self) -> tuple[Tier1Store, WikiStore, FTSIndex]:
        if not self._tier1 or not self._wiki or not self._fts:
            raise RuntimeError("Matrix Memory provider is not initialized")
        return self._tier1, self._wiki, self._fts

    def _is_chat(self) -> bool:
        return self._mode == "chat"

    def _default_dry_run(self, args: Dict[str, Any]) -> bool:
        if "dry_run" in args:
            return bool(args["dry_run"])
        return self._is_chat()

    def memory_status(self, args: Dict[str, Any]) -> dict:
        tier1, wiki, fts = self._require_ready()
        memory_entries, user_entries = tier1.read_all()
        return {
            "success": True,
            "provider": self.name,
            "mode": self._mode,
            "tier1": {"memory_entries": len(memory_entries), "user_entries": len(user_entries)},
            "wiki": {"pages": len(wiki.iter_pages()), "root": str(wiki.root)},
            "fts": fts.stats(),
        }

    def memory_recall(self, args: Dict[str, Any]) -> dict:
        tier1, wiki, fts = self._require_ready()
        query = str(args.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query is required"}
        top_k = int(args.get("top_k") or 5)
        return {"success": True, "query": query, **route_recall(query, tier1, wiki, fts, top_k=top_k)}

    def memory_note(self, args: Dict[str, Any]) -> dict:
        tier1, wiki, fts = self._require_ready()
        target = str(args.get("target") or "wiki")
        title = str(args.get("title") or "").strip()
        content = str(args.get("content") or "").strip()
        folder = str(args.get("folder") or "queries")
        dry_run = self._default_dry_run(args)
        if not content:
            return {"success": False, "error": "content is required"}
        if target in {"memory", "user"}:
            preview = {"target": target, "content": content, "chars": len(content)}
            if dry_run:
                return build_dry_run_preview("memory_note", preview)
            entry = tier1.add_entry(target, content)
            append_log_entry(wiki.root / "log.md", "note", f"{target}:{entry[:80]}")
            return {"success": True, "target": target, "entry": entry}
        if not title:
            return {"success": False, "error": "title is required for wiki notes"}
        preview = wiki.preview_page(title=title, content=content, folder=folder)
        if dry_run:
            return build_dry_run_preview("memory_note", preview)
        rel = wiki.write_page(title=title, content=content, folder=folder)
        page_content = wiki.read_page(rel)
        fts.index_page(rel, page_content, wiki.chunks_for_page(rel, page_content))
        append_log_entry(wiki.root / "log.md", "note", rel)
        return {"success": True, "target": "wiki", "page": rel, "indexed": True}

    def memory_ingest(self, args: Dict[str, Any]) -> dict:
        _, wiki, fts = self._require_ready()
        source = str(args.get("source") or "").strip()
        title = str(args.get("title") or "").strip()
        content = str(args.get("content") or "").strip()
        folder = str(args.get("folder") or "raw")
        dry_run = self._default_dry_run(args)
        if not (source or content):
            return {"success": False, "error": "source or content is required"}
        preview = wiki.preview_ingest(source=source, title=title, content=content, folder=folder)
        if dry_run:
            return build_dry_run_preview("memory_ingest", preview)
        rel, stored_content = wiki.ingest(source=source, title=title, content=content, folder=folder)
        fts.index_page(rel, stored_content, wiki.chunks_for_page(rel, stored_content))
        append_log_entry(wiki.root / "log.md", "ingest", rel)
        return {"success": True, "page": rel, "indexed": True, "source": source or "inline"}

    def memory_forget(self, args: Dict[str, Any]) -> dict:
        tier1, wiki, fts = self._require_ready()
        kind = str(args.get("kind") or "page")
        target = str(args.get("target") or "").strip()
        dry_run = self._default_dry_run(args)
        if not target:
            return {"success": False, "error": "target is required"}
        preview = {"kind": kind, "target": target}
        if dry_run:
            token = self._confirm.generate("memory_forget", target, preview)
            return build_dry_run_preview("memory_forget", preview, requires_confirmation=True, confirm_token=token)
        if self._is_chat():
            ok, error = self._confirm.verify(str(args.get("confirm_token") or ""), "memory_forget", target)
            if not ok:
                return {"success": False, "error": error}
        if kind in {"memory", "user"}:
            removed = tier1.remove_matching(kind, target)
            append_log_entry(wiki.root / "log.md", "forget", f"{kind}:{target}")
            return {"success": True, "kind": kind, "removed": removed}
        removed = wiki.delete_page(target)
        if removed:
            fts.remove_page(target)
            append_log_entry(wiki.root / "log.md", "forget", target)
        return {"success": True, "kind": "page", "removed": int(bool(removed))}

    def memory_show(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        return show_page(wiki, str(args.get("path") or "").strip())

    def memory_list(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        folder = args.get("folder")
        return list_pages(wiki, str(folder) if folder else None)

    def memory_find_orphans(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        return {"success": True, "orphans": find_orphans(wiki)}

    def memory_find_dead_links(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        return {"success": True, "dead_links": find_dead_links(wiki)}

    def memory_find_stale(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        days = int(args.get("days") or self._config.get("stale_after_days", 30))
        return {"success": True, "days": days, "pages": find_stale(wiki, days)}

    def memory_find_duplicates(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        threshold = float(args.get("threshold") or 0.75)
        return {"success": True, "threshold": threshold, "pairs": find_duplicates(wiki, threshold)}

    def memory_compare(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        return compare_pages(wiki, str(args.get("path_a") or "").strip(), str(args.get("path_b") or "").strip())

    def memory_trace(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        start = str(args.get("start") or "").strip()
        max_depth = int(args.get("max_depth") or 2)
        return {"success": True, "start": start, "trace": trace_links(wiki, start, max_depth=max_depth)}

    def memory_audit(self, args: Dict[str, Any]) -> dict:
        _, wiki, _ = self._require_ready()
        requires_session_mode(self._mode, "chat")
        days = int(args.get("days") or self._config.get("stale_after_days", 30))
        threshold = float(args.get("threshold") or 0.75)
        return {"success": True, "report": build_audit_report(wiki, days=days, threshold=threshold)}
