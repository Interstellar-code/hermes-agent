"""Tests for the Phase 0 baseline logger and the cache-report summariser."""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import usage_pricing
from agent.usage_pricing import CanonicalUsage, normalize_usage


@pytest.fixture(autouse=True)
def _isolated_observer_slot():
    """Save existing observers, drop them for the duration of the test,
    register the plugin's own observer fresh, then restore on teardown.

    Importing ``plugins.mcp_lazy`` runs its ``__init__`` which calls
    ``baseline_patch.install()`` — so by the time tests run, the package
    may already have registered. We can't rely on call counts from the
    import side-effect, so we explicitly reset and re-register here.
    """
    from plugins.mcp_lazy import baseline_patch
    original = list(usage_pricing._usage_observers)
    usage_pricing._usage_observers.clear()
    yield baseline_patch
    usage_pricing._usage_observers.clear()
    usage_pricing._usage_observers.extend(original)


def _redirect_log(monkeypatch, target: Path) -> None:
    """Point the plugin's log file at a temp path so we don't write to ~/."""
    from plugins.mcp_lazy import baseline_patch
    monkeypatch.setattr(baseline_patch, "_LOG_FILE", target)
    monkeypatch.setattr(baseline_patch, "_LOG_DIR_DEFAULT", target.parent)


def test_install_registers_observer(_isolated_observer_slot, tmp_path, monkeypatch):
    baseline_patch = _isolated_observer_slot
    log_file = tmp_path / "cache-baseline.jsonl"
    _redirect_log(monkeypatch, log_file)
    monkeypatch.setattr(baseline_patch, "_ENABLED", True)

    baseline_patch.install()

    assert baseline_patch._baseline_log in usage_pricing._usage_observers
    assert usage_pricing._usage_observers.count(baseline_patch._baseline_log) == 1


def test_baseline_writes_one_line_per_call(_isolated_observer_slot, tmp_path, monkeypatch):
    baseline_patch = _isolated_observer_slot
    log_file = tmp_path / "cache-baseline.jsonl"
    _redirect_log(monkeypatch, log_file)
    monkeypatch.setattr(baseline_patch, "_ENABLED", True)
    baseline_patch.install()

    fake = SimpleNamespace(
        input_tokens=200,
        output_tokens=10,
        cache_read_input_tokens=100,
        cache_creation_input_tokens=50,
    )
    normalize_usage(fake, provider="anthropic")
    normalize_usage(fake, provider="anthropic")

    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["cache_read"] == 100
    assert row["cache_creation"] == 50
    assert row["input_tokens"] == 200
    assert "ts" in row


def test_baseline_disabled_writes_nothing(_isolated_observer_slot, tmp_path, monkeypatch):
    baseline_patch = _isolated_observer_slot
    log_file = tmp_path / "cache-baseline.jsonl"
    _redirect_log(monkeypatch, log_file)
    monkeypatch.setattr(baseline_patch, "_ENABLED", False)
    baseline_patch.install()

    fake = SimpleNamespace(
        input_tokens=200,
        output_tokens=10,
        cache_read_input_tokens=100,
        cache_creation_input_tokens=50,
    )
    normalize_usage(fake, provider="anthropic")

    assert not log_file.exists() or log_file.read_text() == ""


def test_baseline_swallows_write_errors(_isolated_observer_slot, tmp_path, monkeypatch):
    """A read-only log path must not break the canonicaliser."""
    baseline_patch = _isolated_observer_slot
    # Point at a path inside a non-directory file — open() will fail.
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("file blocking dir creation")
    log_file = blocked / "cache-baseline.jsonl"
    _redirect_log(monkeypatch, log_file)
    monkeypatch.setattr(baseline_patch, "_ENABLED", True)
    baseline_patch.install()

    fake = SimpleNamespace(
        input_tokens=200,
        output_tokens=10,
        cache_read_input_tokens=100,
        cache_creation_input_tokens=50,
    )
    # Should NOT raise.
    result = normalize_usage(fake, provider="anthropic")
    assert result.cache_read_tokens == 100


# -- cache_report summariser --------------------------------------------------

def test_cache_report_pooled_hit_rate(tmp_path, capsys):
    log = tmp_path / "baseline.jsonl"
    rows = [
        {"ts": 1.0, "input_tokens": 100, "output_tokens": 10, "cache_read": 900, "cache_creation": 0},
        {"ts": 2.0, "input_tokens": 100, "output_tokens": 10, "cache_read": 900, "cache_creation": 0},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))

    from plugins.mcp_lazy.scripts import cache_report
    rc = cache_report.summarise(log)

    captured = capsys.readouterr()
    assert rc == 0
    # 1800 read / 2000 total = 0.900
    assert "pooled hit rate:    0.900" in captured.out
    assert "strategy:           deferred" in captured.out


def test_cache_report_immediate_strategy_for_cold_cache(tmp_path, capsys):
    log = tmp_path / "baseline.jsonl"
    rows = [
        {"input_tokens": 1000, "output_tokens": 10, "cache_read": 100, "cache_creation": 0},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))

    from plugins.mcp_lazy.scripts import cache_report
    cache_report.summarise(log)

    captured = capsys.readouterr()
    # 100 / 1100 = 0.0909 → < 0.30 → immediate
    assert "strategy:           immediate" in captured.out


def test_cache_report_missing_file(tmp_path, capsys):
    from plugins.mcp_lazy.scripts import cache_report
    rc = cache_report.summarise(tmp_path / "nope.jsonl")
    assert rc == 1
    out = capsys.readouterr().out
    assert "no log at" in out


def test_cache_report_empty_file(tmp_path, capsys):
    log = tmp_path / "baseline.jsonl"
    log.touch()
    from plugins.mcp_lazy.scripts import cache_report
    rc = cache_report.summarise(log)
    assert rc == 1
    assert "is empty" in capsys.readouterr().out


def test_cache_report_skips_malformed_lines(tmp_path, capsys):
    log = tmp_path / "baseline.jsonl"
    log.write_text(
        json.dumps({"cache_read": 50, "cache_creation": 0, "input_tokens": 50}) + "\n"
        "not-json\n"
        + json.dumps({"cache_read": 50, "cache_creation": 0, "input_tokens": 50}) + "\n"
    )
    from plugins.mcp_lazy.scripts import cache_report
    rc = cache_report.summarise(log)
    assert rc == 0
    out = capsys.readouterr().out
    assert "records: 2" in out


def test_cache_report_zero_denominator_no_divide_error(tmp_path, capsys):
    log = tmp_path / "baseline.jsonl"
    log.write_text(json.dumps({"input_tokens": 0, "cache_read": 0, "cache_creation": 0}) + "\n")
    from plugins.mcp_lazy.scripts import cache_report
    rc = cache_report.summarise(log)
    assert rc == 0
    assert "pooled hit rate:    0.000" in capsys.readouterr().out
