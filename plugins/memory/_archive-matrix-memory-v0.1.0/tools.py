from __future__ import annotations


def _schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


BASE_SCHEMAS = {
    "memory_recall": _schema(
        "memory_recall",
        "Recall relevant information from Tier 1 facts, the Matrix wiki, and the SQLite FTS index.",
        {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max tier-3 results to return."},
        },
        ["query"],
    ),
    "memory_note": _schema(
        "memory_note",
        "Store a durable note in Tier 1 or create a wiki page in Tier 2.",
        {
            "target": {
                "type": "string",
                "enum": ["memory", "user", "wiki"],
                "description": "Where to store the note.",
            },
            "title": {"type": "string", "description": "Required for wiki notes."},
            "content": {"type": "string", "description": "Note content."},
            "folder": {"type": "string", "description": "Wiki folder for wiki notes."},
            "dry_run": {"type": "boolean", "description": "Preview instead of applying the change."},
        },
        ["content"],
    ),
    "memory_ingest": _schema(
        "memory_ingest",
        "Ingest inline content, a local file, or a URL into the wiki and Tier 3 index.",
        {
            "source": {"type": "string", "description": "Optional URL or local file path."},
            "title": {"type": "string", "description": "Optional title override."},
            "content": {"type": "string", "description": "Optional inline content."},
            "folder": {"type": "string", "description": "Wiki folder to store the page under."},
            "dry_run": {"type": "boolean", "description": "Preview instead of applying the ingest."},
        },
    ),
    "memory_forget": _schema(
        "memory_forget",
        "Remove a Tier 1 entry or a wiki page. In chat mode this defaults to dry-run and requires a confirm token.",
        {
            "kind": {
                "type": "string",
                "enum": ["page", "memory", "user"],
                "description": "What kind of thing to remove.",
            },
            "target": {"type": "string", "description": "Relative page path or substring match for Tier 1 entries."},
            "dry_run": {"type": "boolean", "description": "Preview instead of applying the deletion."},
            "confirm_token": {
                "type": "string",
                "description": "Required in chat mode when applying a destructive action after a preview.",
            },
        },
        ["target"],
    ),
    "memory_status": _schema(
        "memory_status",
        "Return Matrix Memory counts, paths, and index health.",
        {},
    ),
}

CHAT_SCHEMAS = [
    _schema("memory_show", "Show a wiki page by relative path.", {"path": {"type": "string"}}, ["path"]),
    _schema("memory_list", "List wiki pages, optionally restricted to one folder.", {"folder": {"type": "string"}}),
    _schema("memory_find_orphans", "Find pages with no inbound and no outbound links.", {}),
    _schema("memory_find_dead_links", "Find wikilinks that point to missing pages.", {}),
    _schema("memory_find_stale", "Find pages older than a given number of days.", {"days": {"type": "integer"}}),
    _schema("memory_find_duplicates", "Find near-duplicate pages by Jaccard similarity.", {"threshold": {"type": "number"}}),
    _schema(
        "memory_compare",
        "Show a unified diff between two wiki pages.",
        {"path_a": {"type": "string"}, "path_b": {"type": "string"}},
        ["path_a", "path_b"],
    ),
    _schema(
        "memory_trace",
        "Trace wikilinks outward from a start page.",
        {"start": {"type": "string"}, "max_depth": {"type": "integer"}},
        ["start"],
    ),
    _schema(
        "memory_audit",
        "Build a composite audit report over the wiki.",
        {"days": {"type": "integer"}, "threshold": {"type": "number"}},
    ),
]

WRITE_TOOLS = {"memory_note", "memory_ingest", "memory_forget"}


def build_base_tool_schemas() -> list[dict]:
    return [BASE_SCHEMAS[name] for name in ("memory_recall", "memory_note", "memory_ingest", "memory_forget", "memory_status")]
