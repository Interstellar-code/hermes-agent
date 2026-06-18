from __future__ import annotations

from .find_dead_links import find_dead_links
from .find_duplicates import find_duplicates
from .find_orphans import find_orphans
from .find_stale import find_stale


def build_audit_report(wiki, *, days: int, threshold: float) -> dict:
    pages = wiki.iter_pages()
    return {
        "counts": {"pages": len(pages)},
        "orphans": find_orphans(wiki),
        "dead_links": find_dead_links(wiki),
        "stale": find_stale(wiki, days),
        "duplicates": find_duplicates(wiki, threshold),
    }
