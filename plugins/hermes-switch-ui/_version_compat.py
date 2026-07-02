"""hermes-switch-ui — version compatibility check.

PLUGIN_RANGE declares the SwitchUI semver range this plugin understands.
check(version) -> dict with compatible, warn, plugin_range, frontend_version.

Uses packaging.specifiers.SpecifierSet when available; ships a minimal
major.minor.patch fallback so no new hard dependency is required.
None / unparseable version -> compatible=False with a warn string, never raises.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Mirrored from plugin.yaml: compatible_switchui
PLUGIN_RANGE: str = ">=1.0.0,<3.0.0"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_version(version_str: str) -> Optional[tuple]:
    """Parse 'major.minor.patch' into a tuple of ints.

    Returns None if parsing fails.
    """
    try:
        parts = version_str.strip().split(".")
        if len(parts) < 1:
            return None
        # Accept 1, 1.0, or 1.0.0 — pad to 3 components
        padded = (parts + ["0", "0"])[:3]
        return tuple(int(p) for p in padded)
    except (ValueError, AttributeError):
        return None


def _check_with_packaging(version_str: str) -> dict:
    """Use packaging.specifiers.SpecifierSet for the comparison."""
    from packaging.specifiers import SpecifierSet, InvalidSpecifier  # type: ignore[import]
    from packaging.version import Version, InvalidVersion  # type: ignore[import]

    try:
        spec = SpecifierSet(PLUGIN_RANGE)
    except InvalidSpecifier:
        # Should never happen with a hard-coded range, but be safe
        return {
            "compatible": False,
            "warn": f"Plugin range {PLUGIN_RANGE!r} could not be parsed.",
            "plugin_range": PLUGIN_RANGE,
            "frontend_version": version_str,
        }

    try:
        ver = Version(version_str)
    except InvalidVersion:
        return {
            "compatible": False,
            "warn": (
                f"SwitchUI version {version_str!r} could not be parsed; "
                f"expected semver in range {PLUGIN_RANGE}."
            ),
            "plugin_range": PLUGIN_RANGE,
            "frontend_version": version_str,
        }

    compatible = ver in spec
    warn: Optional[str] = None
    if not compatible:
        warn = (
            f"SwitchUI {version_str} is outside supported range {PLUGIN_RANGE}; "
            f"some features may not sync."
        )
    return {
        "compatible": compatible,
        "warn": warn,
        "plugin_range": PLUGIN_RANGE,
        "frontend_version": version_str,
    }


def _check_fallback(version_str: str) -> dict:
    """Minimal semver range check without packaging dependency.

    Supports constraints of the form >=X.Y.Z and <X.Y.Z (AND-combined).
    """
    parsed = _parse_version(version_str)
    if parsed is None:
        return {
            "compatible": False,
            "warn": (
                f"SwitchUI version {version_str!r} could not be parsed; "
                f"expected semver in range {PLUGIN_RANGE}."
            ),
            "plugin_range": PLUGIN_RANGE,
            "frontend_version": version_str,
        }

    # Parse PLUGIN_RANGE constraints
    import re
    constraint_re = re.compile(r"(>=|<=|>|<|==|!=)\s*(\d+\.\d+(?:\.\d+)?)")
    constraints = constraint_re.findall(PLUGIN_RANGE)

    compatible = True
    for op, ver_str in constraints:
        bound = _parse_version(ver_str)
        if bound is None:
            compatible = False
            break
        if op == ">=":
            if not (parsed >= bound):
                compatible = False
                break
        elif op == ">":
            if not (parsed > bound):
                compatible = False
                break
        elif op == "<=":
            if not (parsed <= bound):
                compatible = False
                break
        elif op == "<":
            if not (parsed < bound):
                compatible = False
                break
        elif op == "==":
            if not (parsed == bound):
                compatible = False
                break
        elif op == "!=":
            if not (parsed != bound):
                compatible = False
                break

    warn: Optional[str] = None
    if not compatible:
        warn = (
            f"SwitchUI {version_str} is outside supported range {PLUGIN_RANGE}; "
            f"some features may not sync."
        )
    return {
        "compatible": compatible,
        "warn": warn,
        "plugin_range": PLUGIN_RANGE,
        "frontend_version": version_str,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(version: Optional[str]) -> dict:
    """Check if the given SwitchUI version is compatible with this plugin.

    Args:
        version: SwitchUI version string (e.g. "1.0.0") or None.

    Returns:
        dict with keys:
            compatible      — bool
            warn            — str or None (human-readable mismatch message)
            plugin_range    — str: PLUGIN_RANGE constant
            frontend_version — str: the version passed in (or "<unknown>")
    """
    if version is None:
        return {
            "compatible": False,
            "warn": (
                f"SwitchUI version not reported; "
                f"expected semver in range {PLUGIN_RANGE}."
            ),
            "plugin_range": PLUGIN_RANGE,
            "frontend_version": "<unknown>",
        }

    if not isinstance(version, str):
        version = str(version)

    version = version.strip()
    if not version:
        return {
            "compatible": False,
            "warn": (
                f"SwitchUI version is empty; "
                f"expected semver in range {PLUGIN_RANGE}."
            ),
            "plugin_range": PLUGIN_RANGE,
            "frontend_version": "<empty>",
        }

    # Try packaging first; fall back to minimal comparator
    try:
        import packaging.specifiers  # noqa: F401 — probe only
        return _check_with_packaging(version)
    except ImportError:
        log.debug("hermes-switch-ui: packaging not available, using fallback comparator")
        return _check_fallback(version)
    except Exception as exc:  # noqa: BLE001
        log.debug("hermes-switch-ui: packaging check failed (%s), using fallback", exc)
        return _check_fallback(version)
