from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)
_PLUGIN_DIR = Path(__file__).resolve().parent
_VERSION = "0.1.0"


def register(ctx) -> None:
    """Register the Matrix Memory provider and bundled skill."""
    from .provider import MatrixMemoryProvider

    ctx.register_memory_provider(MatrixMemoryProvider())

    if hasattr(ctx, "register_skill"):
        skill_path = _PLUGIN_DIR / "skills" / "matrix-memory" / "SKILL.md"
        if skill_path.exists():
            try:
                ctx.register_skill(
                    name="matrix-memory",
                    path=skill_path,
                    description="Operate Matrix Memory's wiki and recall tools safely.",
                )
            except Exception:  # noqa: BLE001 - optional additive surface
                log.debug("matrix-memory: register_skill failed", exc_info=True)
