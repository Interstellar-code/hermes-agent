"""Matrix Memory v0.2 — thin registration shim over the Mnemosyne fork.

The actual engine + contract layer live in the pinned git submodule at
``plugins/memory/_matrix-memory-mnemosyne`` (fork ``main``; pinned SHA is
recorded in ``.gitmodules`` + the tracked gitlink — run
``git submodule update --init`` on a fresh clone):

    _matrix-memory-mnemosyne/
      mnemosyne/                  # ~30k LOC engine (vector + KG + temporal + sync)
      hermes_memory_provider/     # contract layer
        __init__.py               # MnemosyneMemoryProvider + register_memory_provider(ctx)
        tier1.py                  # MEMORY.md/USER.md passthrough (read + delete + migration)
        wiki_bridge.py            # Tier 3 markdown -> Tier 2 + 60s mtime poll thread
        safety.py                 # dry_run + confirm_token gate (opt-in: MNEMOSYNE_MATRIX_SAFETY=1)
      skills/matrix-memory/SKILL.md   # 3-tier discipline skill

The submodule dir is ``_``-prefixed so Hermes' bundled-provider discovery
(``plugins/memory/__init__.py``: skips ``_``/``.`` names) does not surface it
as a phantom provider. Only this ``matrix-memory`` dir is discoverable, and
it is the provider named by ``memory.provider`` in config.

Why a shim instead of pointing the config at the submodule directly: the
fork's engine imports ``mnemosyne`` as a top-level package at module load
(``from mnemosyne.core... import ...``), so the submodule repo root must be
on ``sys.path`` *before* ``hermes_memory_provider`` is imported. This shim
guarantees that ordering, then delegates registration to the fork.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent
# Sibling submodule (underscore-prefixed so discovery skips it).
_FORK_ROOT = _PLUGIN_DIR.parent / "_matrix-memory-mnemosyne"


def _ensure_fork_importable() -> None:
    """Put the submodule repo root on sys.path.

    Both ``mnemosyne`` (engine) and ``hermes_memory_provider`` (contract)
    are top-level packages inside the submodule root, and the contract
    package imports the engine at module load. Prepending the root makes
    ``import mnemosyne`` and ``import hermes_memory_provider`` resolve to
    the pinned fork.
    """
    root = str(_FORK_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def register(ctx) -> None:
    """Register the Mnemosyne-backed matrix-memory provider + discipline skill.

    Called by Hermes' memory-provider discovery (``load_memory_provider``)
    with a ctx exposing ``register_memory_provider`` (and optionally
    ``register_skill`` / ``register_cli_command``).
    """
    if not _FORK_ROOT.exists():
        raise RuntimeError(
            "matrix-memory: submodule missing at %s — run "
            "`git submodule update --init plugins/memory/_matrix-memory-mnemosyne`"
            % _FORK_ROOT
        )

    _ensure_fork_importable()

    # Primary: register the memory provider (carries the 20 Mnemosyne tools +
    # 3 wiki tools + Tier 1 passthrough via its schema injection).
    from hermes_memory_provider import register_memory_provider as _reg_provider

    _reg_provider(ctx)

    # Bonus: register the `mnemosyne` CLI + tools/hooks when the loader's ctx
    # supports it. Self-degrades when ctx is the minimal provider-only ctx.
    try:
        from hermes_memory_provider import register as _fork_register

        _fork_register(ctx)
    except Exception:  # noqa: BLE001 - optional additive surface
        log.debug("matrix-memory: fork register(ctx) (CLI/tools) skipped", exc_info=True)

    # Discipline skill (3-tier model + dry_run/confirm_token discipline).
    if hasattr(ctx, "register_skill"):
        skill_path = _FORK_ROOT / "skills" / "matrix-memory" / "SKILL.md"
        if skill_path.exists():
            try:
                ctx.register_skill(
                    name="matrix-memory",
                    path=skill_path,
                    description="Operate matrix-memory (Mnemosyne) 3-tier memory safely: "
                    "Tier 1 passthrough, Tier 2 recall, Tier 3 wiki bridge, "
                    "dry_run + confirm_token deletes.",
                )
            except Exception:  # noqa: BLE001 - optional additive surface
                log.debug("matrix-memory: register_skill failed", exc_info=True)


def register_memory_provider(ctx) -> None:
    """Alias entrypoint — delegates to :func:`register`.

    Mirrors the fork's dual-entrypoint convention so either discovery path
    lands on the same provider registration.
    """
    register(ctx)
