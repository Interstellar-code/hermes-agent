from __future__ import annotations

from .tiers.tier1_episodic import Tier1Store
from .tiers.tier2_wiki import WikiStore
from .tiers.tier3_fts5 import FTSIndex


def route_recall(query: str, tier1: Tier1Store, wiki: WikiStore, fts: FTSIndex, *, top_k: int = 5) -> dict:
    tier1_hits = tier1.search(query)
    tier2_hits = wiki.search_pages(query)
    tier3_hits = fts.search(query, top_k=top_k)
    return {
        "tier1": tier1_hits,
        "tier2": tier2_hits,
        "tier3": tier3_hits,
        "summary": {
            "tier1_hits": len(tier1_hits),
            "tier2_hits": len(tier2_hits),
            "tier3_hits": len(tier3_hits),
        },
    }
