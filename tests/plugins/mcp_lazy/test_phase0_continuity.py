"""Phase 0 baseline logger still captures cache stats correctly under Phase 2."""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_baseline_patch_install_idempotent():
    """baseline_patch.install() can be called multiple times without error."""
    from plugins.mcp_lazy import baseline_patch
    baseline_patch.install()
    baseline_patch.install()  # second call must not raise


def test_cache_report_summarise_empty_log(tmp_path):
    """cache_report.summarise returns 1 on empty log."""
    from plugins.mcp_lazy.scripts.cache_report import summarise
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert summarise(empty) == 1


def test_cache_report_summarise_missing_log(tmp_path):
    """cache_report.summarise returns 1 when file doesn't exist."""
    from plugins.mcp_lazy.scripts.cache_report import summarise
    missing = tmp_path / "no-such-file.jsonl"
    assert summarise(missing) == 1


def test_cache_report_phase2_server_promotions(tmp_path, capsys):
    """cache_report shows per-server promotion counts when rows have promoted_servers."""
    from plugins.mcp_lazy.scripts.cache_report import summarise

    rows = [
        {"cache_read": 1000, "cache_creation": 200, "input_tokens": 800,
         "promoted_servers": ["trek"], "promoted_tools": ["mcp_gmail_send"]},
        {"cache_read": 500, "cache_creation": 100, "input_tokens": 400,
         "promoted_servers": ["trek", "gmail"], "promoted_tools": []},
    ]
    log = tmp_path / "test.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows))

    rc = summarise(log)
    assert rc == 0
    captured = capsys.readouterr()
    assert "trek" in captured.out
    assert "gmail" in captured.out
    assert "server promotions" in captured.out


def test_cache_report_hit_rate_calculation(tmp_path, capsys):
    """cache_report.summarise computes pooled hit rate correctly."""
    from plugins.mcp_lazy.scripts.cache_report import summarise, _hit_rate

    row = {"cache_read": 600, "cache_creation": 200, "input_tokens": 200}
    assert abs(_hit_rate(row) - 0.6) < 1e-9

    log = tmp_path / "test.jsonl"
    log.write_text(json.dumps(row))
    rc = summarise(log)
    assert rc == 0
    out = capsys.readouterr().out
    assert "0.600" in out
