"""Matrix Coder configuration defaults.

Pure stdlib.  Phase 0 returns hard-coded defaults; later phases will overlay
values resolved from Hermes config (``config.yaml`` / env).  Keep this module
free of Hermes-runtime imports so it stays unit-testable in isolation.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


# Dispatch categories map to the model a specialist runs on.  ``None`` means
# "inherit the parent session's model" — the real mapping is resolved from
# Hermes config in a later phase, so these are placeholders only.
DISPATCH_CATEGORY_MODEL: Dict[str, Optional[str]] = {
    "deep": None,   # heavyweight reasoning roles (plan, review, verify)
    "quick": None,  # lightweight passes (explore, simplify)
}


def load_config() -> Dict[str, Any]:
    """Return Matrix Coder defaults.

    Phase 0: static defaults only.  The shape is intentionally a plain dict so
    later phases can deep-merge Hermes config without changing call sites.
    """
    return {
        "enabled": True,
        # Default intake behaviour: route ambiguous/complex work through the
        # matrix rather than answering directly.
        "default_verdict": "MATRIX",
        "dispatch_category_model": dict(DISPATCH_CATEGORY_MODEL),
        # Single-writer-per-file guardrail is enforced at orchestration time;
        # this flag exists so later phases can toggle the bookkeeping.
        "single_writer_per_file": True,
        # Phase 2 audit-mirror: mirror each matrix invocation as a Hermes Kanban
        # card for live observability on the Switch UI. Purely an audit layer,
        # never control flow. Set False to disable mirroring entirely.
        "KANBAN_AUDIT_ENABLED": True,
        # Phase 5 implicit routing kill-switch.  Set to False (or set the env
        # var MATRIX_CODER_IMPLICIT_ROUTING=0) to disable all implicit intent
        # routing; explicit "matrix ..." triggers are unaffected.
        "implicit_routing_enabled": os.environ.get(
            "MATRIX_CODER_IMPLICIT_ROUTING", "1"
        ) != "0",
    }
