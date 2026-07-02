"""workflow_list — list available workflow definitions."""
from __future__ import annotations

import json
from typing import Any, Dict, List

SCHEMA: Dict[str, Any] = {
    "name": "workflow_list",
    "description": "List available workflow definitions. Optionally filter by tags or source.",
    "parameters": {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter to definitions that carry all of the given tags.",
            },
            "source": {
                "type": "string",
                "enum": ["bundled", "user", "project"],
                "description": "Filter to definitions from the given source.",
            },
        },
        "required": [],
    },
}


def check() -> bool:
    """Always allow — read-only tool."""
    return True


async def handler(args: Dict[str, Any], **kwargs: Any) -> str:  # noqa: ARG001
    return json.dumps(await _handler_impl(args, **kwargs), ensure_ascii=False, default=str)


async def _handler_impl(args: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:  # noqa: ARG001
    tags: List[str] | None = args.get("tags")
    source: str | None = args.get("source")
    from .._shared import get_engine  # noqa: PLC0415

    engine = get_engine()
    definitions = await engine.list_definitions()

    if source is not None:
        definitions = [d for d in definitions if d.get("source") == source]

    if tags:
        tag_set = set(tags)
        definitions = [
            d for d in definitions
            if tag_set.issubset(set(d.get("tags") or []))
        ]

    return {
        "definitions": [
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "description": d.get("description"),
                "source": d.get("source"),
                "tags": d.get("tags") or [],
            }
            for d in definitions
        ],
        "count": len(definitions),
    }
