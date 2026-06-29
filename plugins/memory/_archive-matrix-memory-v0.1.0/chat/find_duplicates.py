from __future__ import annotations


def find_duplicates(wiki, threshold: float) -> list[dict]:
    pages = wiki.iter_pages()
    token_cache = {page: wiki.page_tokens(page) for page in pages}
    results = []
    for idx, page_a in enumerate(pages):
        for page_b in pages[idx + 1 :]:
            a = token_cache[page_a]
            b = token_cache[page_b]
            if not a or not b:
                continue
            score = len(a & b) / len(a | b)
            if score >= threshold:
                results.append({"path_a": page_a, "path_b": page_b, "score": round(score, 3)})
    return sorted(results, key=lambda item: item["score"], reverse=True)
