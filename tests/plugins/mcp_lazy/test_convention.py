"""Convention tests: registered tool name and plugin.yaml hook declarations."""
from __future__ import annotations

import importlib
from pathlib import Path

import yaml


def test_meta_tool_registered_name_is_load_mcp_tools():
    """SCHEMA['name'] must be 'load_mcp_tools', not 'mcp_load_tools'."""
    from plugins.mcp_lazy import meta_tool

    assert meta_tool.SCHEMA["name"] == "load_mcp_tools"


def test_plugin_yaml_declares_pre_tool_call_hook():
    """plugin.yaml hooks list must include pre_tool_call."""
    plugin_yaml = Path(__file__).parents[3] / "plugins" / "mcp_lazy" / "plugin.yaml"
    data = yaml.safe_load(plugin_yaml.read_text())
    assert "pre_tool_call" in data["hooks"]
