from __future__ import annotations


def list_pages(wiki, folder: str | None = None) -> dict:
    return {"success": True, "pages": wiki.list_pages(folder)}
