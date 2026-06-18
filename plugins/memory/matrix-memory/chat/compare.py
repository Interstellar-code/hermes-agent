from __future__ import annotations

import difflib


def compare_pages(wiki, path_a: str, path_b: str) -> dict:
    if not path_a or not path_b:
        return {"success": False, "error": "path_a and path_b are required"}
    try:
        a_lines = wiki.read_page(path_a).splitlines()
        b_lines = wiki.read_page(path_b).splitlines()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}
    diff = "\n".join(difflib.unified_diff(a_lines, b_lines, fromfile=path_a, tofile=path_b, lineterm=""))
    return {"success": True, "path_a": path_a, "path_b": path_b, "diff": diff}
