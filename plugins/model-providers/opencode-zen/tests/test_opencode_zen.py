"""test_opencode_zen.py — verify OpenCode provider profiles (Zen & Go)."""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from __init__ import (
    OpenCodeGoProfile,
    _flat_model_name,
    _is_glm_5_2_model,
    opencode_go,
    opencode_zen,
)


def test_flat_model_name():
    assert _flat_model_name("provider/model-a") == "model-a"
    assert _flat_model_name("model-b") == "model-b"
    assert _flat_model_name(None) == ""


def test_is_glm_5_2_model():
    assert _is_glm_5_2_model("glm-5.2")
    assert _is_glm_5_2_model("opencode/glm-5-2")
    assert _is_glm_5_2_model("glm-5p2-pro")
    assert not _is_glm_5_2_model("glm-5")


def test_opencode_go_max_tokens_mimo():
    profile = OpenCodeGoProfile(name="opencode-go")
    assert profile.get_max_tokens("mimo-v2.5-pro") == 131072
    assert profile.get_max_tokens("provider/mimo-v2.5-pro") == 131072
    assert profile.get_max_tokens("other-model") is None


def test_opencode_go_glm_5_2_reasoning():
    profile = OpenCodeGoProfile(name="opencode-go")

    # Disabled reasoning -> empty
    extra, top = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": False}, model="glm-5.2"
    )
    assert top == {}

    # High effort
    extra, top = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "high"}, model="glm-5.2"
    )
    assert top.get("reasoning_effort") == "high"

    # Ultra effort -> max
    extra, top = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "ultra"}, model="glm-5.2"
    )
    assert top.get("reasoning_effort") == "max"
