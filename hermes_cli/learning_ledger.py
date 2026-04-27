"""Learning ledger: read-only index of how Hermes has grown for this profile."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


@dataclass
class LedgerItem:
    type: str
    name: str
    summary: str
    source: str
    count: int = 0
    learned_from: str | None = None
    last_used_at: float | None = None
    learned_at: float | None = None
    via: str | None = None


def build_learning_ledger(db: Any = None, *, limit: int = 80) -> dict[str, Any]:
    """Build a compact, read-only ledger from existing Hermes artifacts."""
    skill_inventory = _skill_inventory()
    items = [
        *_memory_items(),
        *_tool_usage_items(db),
        *_integration_items(),
    ]
    items.sort(
        key=lambda i: (i.last_used_at or i.learned_at or 0, i.type, i.name),
        reverse=True,
    )

    counts: dict[str, int] = {}
    for item in items:
        counts[item.type] = counts.get(item.type, 0) + 1

    return {
        "generated_at": time.time(),
        "home": str(get_hermes_home()),
        "counts": counts,
        "items": [asdict(item) for item in items[: max(1, limit)]],
        "inventory": {"skills": skill_inventory},
        "total": len(items),
    }


def _memory_items() -> list[LedgerItem]:
    try:
        from tools.memory_tool import MemoryStore, get_memory_dir

        mem_dir = get_memory_dir()
        pairs = [
            ("memory", "MEMORY.md", "agent note"),
            ("user", "USER.md", "user profile"),
        ]
        items: list[LedgerItem] = []
        for item_type, filename, label in pairs:
            path = mem_dir / filename
            for idx, entry in enumerate(MemoryStore._read_file(path), 1):
                items.append(
                    LedgerItem(
                        type=item_type,
                        name=f"{label} {idx}",
                        summary=_one_line(entry),
                        source=str(path),
                        learned_at=_mtime(path),
                    )
                )
        return items
    except Exception:
        return []


def _skill_inventory() -> int:
    try:
        from tools.skills_tool import _find_all_skills

        return len(_find_all_skills())
    except Exception:
        return 0


def _tool_usage_items(db: Any) -> list[LedgerItem]:
    if db is None or not getattr(db, "_conn", None):
        return []

    usage: dict[tuple[str, str], LedgerItem] = {}

    def bump(
        item_type: str,
        name: str,
        summary: str,
        ts: float | None,
        *,
        learned_from: str | None = None,
        via: str | None = None,
    ):
        key = (item_type, name)
        item = usage.get(key)
        if not item:
            item = usage[key] = LedgerItem(
                type=item_type,
                name=name,
                summary=summary,
                source="state.db",
                learned_from=learned_from,
                via=via,
            )
        item.count += 1
        if ts and (not item.last_used_at or ts > item.last_used_at):
            item.last_used_at = ts
            item.learned_from = learned_from or item.learned_from
            item.via = via or item.via

    try:
        with db._lock:
            rows = db._conn.execute(
                """
                SELECT m.role, m.content, m.tool_calls, m.tool_name, m.timestamp,
                       m.session_id, s.title, s.source AS session_source
                FROM messages m
                LEFT JOIN sessions s ON s.id = m.session_id
                WHERE m.tool_name IS NOT NULL OR m.tool_calls IS NOT NULL
                ORDER BY m.timestamp DESC
                LIMIT 5000
                """
            ).fetchall()
    except Exception:
        return []

    for row in rows:
        ts = _float(row["timestamp"])
        tool_name = row["tool_name"]
        content = row["content"] or ""
        learned_from = row["title"] or row["session_source"] or row["session_id"]
        if tool_name == "memory":
            target = _json(content).get("target") or "memory"
            bump(str(target), f"{target} writes", "Durable memory updates", ts, learned_from=learned_from, via="memory")
        elif tool_name == "session_search":
            event = learning_event_from_tool(tool_name, {}, content)
            if event:
                bump("recall", event["title"], event["summary"], ts, learned_from=learned_from, via="session_search")
        elif tool_name in {"skill_view", "skill_manage"}:
            data = _json(content)
            name = str(data.get("name") or data.get("skill") or tool_name)
            bump("skill-use", name, _skill_summary(tool_name, data), ts, learned_from=learned_from, via=tool_name)

        for call in _tool_calls(row["tool_calls"]):
            name, args = call
            if name == "session_search":
                event = learning_event_from_tool(name, args, content)
                if event:
                    bump("recall", event["title"], event["summary"], ts, learned_from=learned_from, via=name)
            elif name in {"skill_view", "skill_manage"}:
                skill_name = str(
                    args.get("name") or args.get("skill") or args.get("query") or name
                )
                bump("skill-use", skill_name, _skill_summary(name, args), ts, learned_from=learned_from, via=name)
            elif name == "memory":
                target = str(args.get("target") or "memory")
                bump(target, f"{target} writes", "Durable memory updates", ts, learned_from=learned_from, via=name)

    return list(usage.values())


def learning_event_from_tool(
    tool_name: str,
    args: dict[str, Any] | None = None,
    result: str | None = None,
) -> dict[str, Any] | None:
    args = args or {}
    data = _json(result)

    if tool_name == "memory":
        target = str(args.get("target") or data.get("target") or "memory")
        content = str(args.get("content") or "").strip()
        return {
            "type": target if target in {"memory", "user"} else "memory",
            "verb": "remembered",
            "title": _one_line(content, max_len=120) if content else f"{target} updated",
            "summary": "Durable memory updated",
            "source": "memory",
            "via": "memory",
        }

    if tool_name == "session_search":
        title = _recall_title(data) or str(args.get("query") or "").strip() or "past sessions"
        return {
            "type": "recall",
            "verb": "recalled",
            "title": _one_line(title, max_len=120),
            "summary": "Past conversations recalled",
            "source": "state.db",
            "via": "session_search",
        }

    if tool_name in {"skill_view", "skill_manage"}:
        action = str(args.get("action") or data.get("action") or "").strip().lower()
        name = str(args.get("name") or args.get("query") or data.get("name") or "skill").strip()
        verb = "updated skill" if tool_name == "skill_manage" and action in {"create", "patch", "update", "install"} else "applied skill"
        return {
            "type": "skill-use",
            "verb": verb,
            "title": _one_line(name, max_len=120),
            "summary": _skill_summary(tool_name, {**args, **(data if isinstance(data, dict) else {})}),
            "source": "skills",
            "via": tool_name,
        }

    return None


def _skill_summary(tool_name: str, data: dict[str, Any]) -> str:
    action = str(data.get("action") or "").strip().lower()
    if tool_name == "skill_manage" and action:
        return f"Skill {action.replace('_', ' ')}"
    if tool_name == "skill_manage":
        return "Skill managed"
    return "Skill reused"


def _recall_title(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return str(data.get("query") or "").strip()
    first = results[0] if isinstance(results[0], dict) else {}
    return str(first.get("title") or first.get("preview") or data.get("query") or "").strip()


def _integration_items() -> list[LedgerItem]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        return []

    items: list[LedgerItem] = []
    provider = ((cfg.get("memory") or {}) if isinstance(cfg, dict) else {}).get(
        "provider"
    )
    if provider:
        items.append(
            LedgerItem(
                type="integration",
                name=f"{provider} memory provider",
                summary="External memory provider is configured",
                source="config.yaml",
            )
        )

    for server in (
        sorted(((cfg.get("mcp") or {}).get("servers") or {}).keys())
        if isinstance(cfg, dict)
        else []
    ):
        items.append(
            LedgerItem(
                type="integration",
                name=f"{server} MCP server",
                summary="MCP server is configured",
                source="config.yaml",
            )
        )

    return items


def _tool_calls(raw: str | None) -> list[tuple[str, dict[str, Any]]]:
    calls = _json(raw)
    if not isinstance(calls, list):
        return []

    parsed = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = call.get("name") or fn.get("name")
        args = fn.get("arguments") or call.get("arguments") or call.get("args") or {}
        if isinstance(args, str):
            args = _json(args)
        if name:
            parsed.append((str(name), args if isinstance(args, dict) else {}))
    return parsed


def _json(raw: Any) -> Any:
    if not raw:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _one_line(text: str, *, max_len: int = 180) -> str:
    line = " ".join(str(text).split())
    return line[: max_len - 1] + "…" if len(line) > max_len else line
