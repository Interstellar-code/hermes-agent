from __future__ import annotations


def find_orphans(wiki) -> list[str]:
    pages = wiki.iter_pages()
    outbound = {page: set(wiki.extract_links(wiki.read_page(page))) for page in pages}
    inbound_counts = {page: 0 for page in pages}
    for links in outbound.values():
        for link in links:
            resolved = wiki.resolve_link(link)
            if resolved:
                inbound_counts[resolved] = inbound_counts.get(resolved, 0) + 1
    return sorted(page for page in pages if inbound_counts.get(page, 0) == 0 and len(outbound.get(page, set())) == 0)
