from __future__ import annotations


def show_page(wiki, path: str) -> dict:
    if not path:
        return {"success": False, "error": "path is required"}
    try:
        content = wiki.read_page(path)
    except FileNotFoundError:
        return {"success": False, "error": f"Page not found: {path}"}
    return {"success": True, "path": path, "content": content}
