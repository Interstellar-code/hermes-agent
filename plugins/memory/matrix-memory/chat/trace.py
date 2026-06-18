from __future__ import annotations

from collections import deque


def trace_links(wiki, start: str, *, max_depth: int = 2) -> list[dict]:
    if not start:
        return []
    resolved = wiki.resolve_link(start) or start
    pages = set(wiki.iter_pages())
    if resolved not in pages:
        return []
    seen = {resolved}
    queue = deque([(resolved, 0)])
    traced = []
    while queue:
        page, depth = queue.popleft()
        traced.append({"page": page, "depth": depth})
        if depth >= max_depth:
            continue
        for link in wiki.extract_links(wiki.read_page(page)):
            nxt = wiki.resolve_link(link)
            if nxt and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, depth + 1))
    return traced
