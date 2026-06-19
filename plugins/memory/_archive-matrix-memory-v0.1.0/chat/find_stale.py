from __future__ import annotations


def find_stale(wiki, days: int) -> list[dict]:
    return [
        {"path": page, "age_days": round(wiki.page_age_days(page), 1)}
        for page in wiki.iter_pages()
        if wiki.page_age_days(page) >= days
    ]
