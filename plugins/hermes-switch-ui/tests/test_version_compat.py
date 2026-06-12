"""test_version_compat.py — tests for hermes-switch-ui _version_compat.py.

Covers:
 - in-range versions (1.0.0, 1.5.2, 2.3.43) -> compatible=True, warn=None
 - out-of-range versions (0.9.0, 3.0.0) -> compatible=False, warn non-null
 - None / garbage -> compatible=False, warn non-null, no raise
 - fallback comparator (_check_fallback) is exercised directly so both code paths tested
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load _version_compat.py via spec_from_file_location
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _load_compat() -> object:
    """Load _version_compat fresh."""
    mod_path = _PLUGIN_DIR / "_version_compat.py"
    spec = importlib.util.spec_from_file_location("_version_compat_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.pop("_version_compat_test", None)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Public API: check()
# ---------------------------------------------------------------------------

def test_in_range_1_0_0():
    vc = _load_compat()
    result = vc.check("1.0.0")
    assert result["compatible"] is True, f"1.0.0 should be compatible: {result}"
    assert result["warn"] is None, f"1.0.0 should have no warning: {result['warn']}"


def test_in_range_1_5_2():
    vc = _load_compat()
    result = vc.check("1.5.2")
    assert result["compatible"] is True, f"1.5.2 should be compatible: {result}"
    assert result["warn"] is None, f"1.5.2 should have no warning: {result['warn']}"


def test_out_of_range_below_0_9_0():
    vc = _load_compat()
    result = vc.check("0.9.0")
    assert result["compatible"] is False, f"0.9.0 should not be compatible: {result}"
    assert result["warn"] is not None and result["warn"].strip(), (
        f"0.9.0 should have a non-empty warn string: {result['warn']}"
    )


def test_in_range_2_3_43():
    """Current SwitchUI 2.x line is in range since the bump to <3.0.0."""
    vc = _load_compat()
    result = vc.check("2.3.43")
    assert result["compatible"] is True, f"2.3.43 should be compatible: {result}"
    assert result["warn"] is None, f"2.3.43 should have no warning: {result['warn']}"


def test_out_of_range_at_upper_bound_3_0_0():
    """3.0.0 is excluded by <3.0.0."""
    vc = _load_compat()
    result = vc.check("3.0.0")
    assert result["compatible"] is False, f"3.0.0 should not be compatible: {result}"
    assert result["warn"] is not None and result["warn"].strip(), (
        f"3.0.0 should have a non-empty warn string: {result['warn']}"
    )


def test_none_version():
    """None version -> compatible=False, warn non-null, no exception."""
    vc = _load_compat()
    result = vc.check(None)
    assert result["compatible"] is False
    assert result["warn"] is not None and result["warn"].strip()


def test_garbage_version():
    """Garbage string -> compatible=False, warn non-null, no exception."""
    vc = _load_compat()
    result = vc.check("not-a-version!!")
    assert result["compatible"] is False
    assert result["warn"] is not None and result["warn"].strip()


def test_empty_string_version():
    """Empty string -> compatible=False, warn non-null."""
    vc = _load_compat()
    result = vc.check("")
    assert result["compatible"] is False
    assert result["warn"] is not None and result["warn"].strip()


def test_result_always_has_required_keys():
    """check() result must always contain compatible, warn, plugin_range, frontend_version."""
    vc = _load_compat()
    for v in ["1.0.0", "0.9.0", "2.0.0", None, "garbage"]:
        result = vc.check(v)
        for key in ("compatible", "warn", "plugin_range", "frontend_version"):
            assert key in result, f"Key {key!r} missing from check({v!r}) result: {result}"


# ---------------------------------------------------------------------------
# Fallback comparator: exercise directly so both packaging + fallback paths covered
# ---------------------------------------------------------------------------

def test_fallback_in_range_1_0_0():
    vc = _load_compat()
    result = vc._check_fallback("1.0.0")
    assert result["compatible"] is True, f"fallback: 1.0.0 should be compatible: {result}"
    assert result["warn"] is None


def test_fallback_in_range_1_5_2():
    vc = _load_compat()
    result = vc._check_fallback("1.5.2")
    assert result["compatible"] is True
    assert result["warn"] is None


def test_fallback_out_of_range_0_9_0():
    vc = _load_compat()
    result = vc._check_fallback("0.9.0")
    assert result["compatible"] is False
    assert result["warn"] is not None and result["warn"].strip()


def test_fallback_out_of_range_3_0_0():
    vc = _load_compat()
    result = vc._check_fallback("3.0.0")
    assert result["compatible"] is False
    assert result["warn"] is not None and result["warn"].strip()


def test_fallback_garbage():
    vc = _load_compat()
    result = vc._check_fallback("not-semver")
    assert result["compatible"] is False
    assert result["warn"] is not None and result["warn"].strip()


def test_packaging_path_agrees_with_fallback():
    """Both paths must agree on compatible for a representative sample."""
    vc = _load_compat()
    try:
        import packaging.specifiers  # noqa: F401
    except ImportError:
        pytest.skip("packaging not installed — skipping cross-path comparison")

    for version in ("1.0.0", "1.5.2", "0.9.0", "2.0.0", "3.0.0"):
        pkg = vc._check_with_packaging(version)
        fb = vc._check_fallback(version)
        assert pkg["compatible"] == fb["compatible"], (
            f"packaging and fallback disagree for {version!r}: "
            f"packaging={pkg['compatible']}, fallback={fb['compatible']}"
        )
