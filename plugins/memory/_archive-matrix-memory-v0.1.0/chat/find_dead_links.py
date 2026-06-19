from __future__ import annotations


def find_dead_links(wiki) -> list[dict]:
    dead = []
    for page in wiki.iter_pages():
        for link in wiki.extract_links(wiki.read_page(page)):
            if wiki.resolve_link(link) is None:
                dead.append({"page": page, "link": link})
    return dead
