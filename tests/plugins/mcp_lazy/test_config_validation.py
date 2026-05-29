"""Tests for Phase 2 config validation in validate_config_structure."""
from __future__ import annotations

import pytest


def _validate(config: dict) -> list:
    from hermes_cli.config import validate_config_structure
    return validate_config_structure(config)


def _errors(issues) -> list:
    return [i for i in issues if i.severity == "error"]


def test_valid_discovery_mode_tool():
    issues = _validate({"mcp": {"discovery_mode": "tool"}})
    assert not _errors(issues)


def test_valid_discovery_mode_server():
    issues = _validate({"mcp": {"discovery_mode": "server"}})
    assert not _errors(issues)


def test_valid_discovery_mode_both():
    issues = _validate({"mcp": {"discovery_mode": "both"}})
    assert not _errors(issues)


def test_invalid_discovery_mode_rejected():
    issues = _validate({"mcp": {"discovery_mode": "lazy"}})
    errs = _errors(issues)
    assert any("discovery_mode" in e.message for e in errs)


def test_invalid_discovery_mode_int_rejected():
    issues = _validate({"mcp": {"discovery_mode": 42}})
    errs = _errors(issues)
    assert any("discovery_mode" in e.message for e in errs)


def test_lazy_stub_max_desc_negative_rejected():
    issues = _validate({"mcp": {"lazy_stub_max_desc": -1}})
    errs = _errors(issues)
    assert any("lazy_stub_max_desc" in e.message for e in errs)


def test_server_stub_max_desc_float_rejected():
    issues = _validate({"mcp": {"server_stub_max_desc": 1.5}})
    errs = _errors(issues)
    assert any("server_stub_max_desc" in e.message for e in errs)


def test_server_eager_token_threshold_zero_ok():
    issues = _validate({"mcp": {"server_eager_token_threshold": 0}})
    assert not _errors(issues)


def test_mcp_servers_description_non_string_rejected():
    issues = _validate({"mcp_servers": {"trek": {"description": 42}}})
    errs = _errors(issues)
    assert any("description" in e.message for e in errs)


def test_mcp_servers_description_string_ok():
    issues = _validate({"mcp_servers": {"trek": {"description": "Trip planning tools"}}})
    assert not _errors(issues)


def test_mcp_empty_block_is_fine():
    issues = _validate({"mcp": {}})
    assert not _errors(issues)


def test_no_mcp_block_is_fine():
    issues = _validate({})
    assert not _errors(issues)
