"""Tests for ACP Registry metadata shipped with Hermes."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "acp_registry" / "agent.json"
ICON = ROOT / "acp_registry" / "icon.svg"
FORBIDDEN_MANIFEST_KEYS = {"schema_version", "display_name"}
ALLOWED_DISTRIBUTIONS = {"binary", "npx", "uvx"}


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_agent_json_matches_official_registry_required_fields():
    data = _manifest()

    assert FORBIDDEN_MANIFEST_KEYS.isdisjoint(data)
    assert data["id"] == "hermes-agent"
    assert re.fullmatch(r"[a-z][a-z0-9-]*", data["id"])
    assert data["name"] == "Hermes Agent"
    assert data["description"]
    assert data["repository"] == "https://github.com/NousResearch/hermes-agent"
    assert data["website"].startswith("https://hermes-agent.nousresearch.com/")
    assert data["authors"] == ["Nous Research"]
    assert data["license"] == "MIT"
    assert set(data["distribution"]) <= ALLOWED_DISTRIBUTIONS


def test_agent_json_uses_uvx_distribution_without_local_command_fields():
    data = _manifest()

    assert set(data["distribution"]) == {"uvx"}
    uvx = data["distribution"]["uvx"]
    # Schema allows {package, args, env}; we use {package, args}.
    assert set(uvx) <= {"package", "args", "env"}
    assert "package" in uvx
    assert uvx["package"] == f"hermes-agent[acp]=={data['version']}"
    assert uvx["args"] == ["hermes-acp"]
    # Old command-shape fields must not leak back in.
    assert "type" not in data["distribution"]
    assert "command" not in data["distribution"]


def test_agent_json_version_is_decoupled_from_pyproject():
    """The manifest describes upstream's PyPI release, not this fork's version.

    It used to be bumped in lockstep with pyproject.toml by release.py. That
    was only correct while this repo could publish ``hermes-agent`` to PyPI —
    it cannot. The package on PyPI belongs to upstream Nous Research, so a
    trusted-publishing exchange from this fork fails with ``invalid-publisher``
    and the PyPI workflow has been removed. Lockstep bumping therefore pinned
    the manifest to versions that never reached the index (0.19.10, 0.19.11),
    breaking ``uvx`` for anyone who resolved it.

    The manifest is now left alone by the release tool. This test asserts the
    decoupling so a future change cannot silently reintroduce the lockstep.
    """
    assert _manifest()["version"] != _pyproject_version(), (
        "acp_registry/agent.json is tracking pyproject.toml again — release.py "
        "must not bump it while this fork does not publish to PyPI."
    )


def test_agent_json_pins_uvx_package_to_an_exact_published_version():
    """The registry CI rejects ``@latest`` and floating pins, so the spec must
    stay an exact ``==`` pin — and it must match the manifest's own version."""
    manifest = _manifest()
    package = manifest["distribution"]["uvx"]["package"]

    assert package == f"hermes-agent[acp]=={manifest['version']}"
    assert "==" in package
    assert "latest" not in package
    assert re.fullmatch(r"hermes-agent\[acp\]==\d+\.\d+\.\d+", package)


def test_icon_svg_is_16x16_current_color():
    root = ET.fromstring(ICON.read_text(encoding="utf-8"))

    assert root.attrib["viewBox"] == "0 0 16 16"
    assert root.attrib["width"] == "16"
    assert root.attrib["height"] == "16"


def test_icon_svg_has_no_hardcoded_colors_or_gradients():
    text = ICON.read_text(encoding="utf-8")

    assert "linearGradient" not in text
    assert "radialGradient" not in text
    assert "url(#" not in text
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text)

    root = ET.fromstring(text)
    for element in root.iter():
        for attr in ("fill", "stroke"):
            value = element.attrib.get(attr)
            if value is not None:
                assert value in {"currentColor", "none"}
