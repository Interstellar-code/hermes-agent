"""#251: the memory provider must learn which kind of run it serves (``agent_context``).

``agent_init`` used to hardcode ``agent_context="primary"``, so cron runs initialised the
provider as a primary chat: matrix-memory's ``skip_contexts`` and
``reflect.disabled_for_cron`` never fired and cron preambles landed in working memory.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.agent.test_memory_provider_init import RecordingMemoryProvider


def _patches(cfg, provider):
    return (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    )


def _build(provider, provider_name="recording", **agent_kwargs):
    from contextlib import ExitStack

    cfg = {"memory": {"provider": provider_name}, "agent": {}}
    with ExitStack() as stack:
        for p in _patches(cfg, provider):
            stack.enter_context(p)
        from run_agent import AIAgent

        return AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, **agent_kwargs,
        )


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("cron", "cron"),          # cron/scheduler.py _construct_cron_agent
        ("subagent", "subagent"),  # tools/delegate_tool.py child AIAgent
        ("cli", "primary"),
        ("telegram", "primary"),
        (None, "primary"),
    ],
)
def test_platform_selects_agent_context(platform, expected):
    provider = RecordingMemoryProvider()
    _build(provider, skip_memory=False, platform=platform, session_id="s1")
    assert provider.init_kwargs["agent_context"] == expected


def _construct_cron(provider, provider_name="recording"):
    """Build the agent exactly the way the scheduler does for a fire."""
    from contextlib import ExitStack

    from cron.scheduler import _construct_cron_agent, _CronAgentSetup

    cfg = {"memory": {"provider": provider_name}, "agent": {}}
    setup = _CronAgentSetup(
        model="test/model",
        runtime={"api_key": "test-key-1234567890", "base_url": "https://openrouter.ai/api/v1"},
    )
    with ExitStack() as stack:
        for p in _patches(cfg, provider):
            stack.enter_context(p)
        from run_agent import AIAgent

        return _construct_cron_agent(
            AIAgent, {"id": "job1", "prompt": "x"}, cfg, setup,
            workdir=None, session_id="cron_job1_20261002", session_db=None,
        )


def test_cron_scheduler_agent_initialises_provider_as_cron():
    provider = RecordingMemoryProvider()
    _construct_cron(provider)
    assert provider.init_kwargs["agent_context"] == "cron"


def test_delegate_child_is_subagent_without_provider():
    """delegate_task children are platform="subagent" and never load the external provider."""
    from unittest.mock import MagicMock

    from tests.tools.test_delegate import _make_mock_parent
    from tools.delegate_tool import _build_child_agent

    with patch("run_agent.AIAgent") as MockAgent:
        MockAgent.return_value = MagicMock()
        _build_child_agent(task_index=0, goal="g", context=None, toolsets=None, model=None,
                           max_iterations=3, parent_agent=_make_mock_parent(), task_count=1, role="leaf")
    _, kwargs = MockAgent.call_args
    assert kwargs["platform"] == "subagent"
    assert kwargs["skip_memory"] is True


def test_background_review_fork_never_loads_provider():
    from types import SimpleNamespace

    from agent.background_review import _fork_init_kwargs

    parent = SimpleNamespace(model="m", platform="telegram", provider="p", session_id="s",
                             enabled_toolsets=None, disabled_toolsets=None)
    with patch("agent.background_review._same_model_parity_kwargs", return_value={}):
        kwargs = _fork_init_kwargs(parent, {}, False, 4)
    assert kwargs["skip_memory"] is True


def _working_memory_rows(data_dir: Path) -> int:
    total = 0
    for db in data_dir.rglob("*.db"):
        with sqlite3.connect(db) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='working_memory'").fetchone():
                total += conn.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0]
    return total


def _matrix_memory_turn(build_agent, data_dir, *, explicit_remember=False):
    """One synced turn (plus optionally an explicit mnemosyne_remember) through the real
    matrix-memory provider. Returns (agent_context, rows after the passive sync, tool reply)."""
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("matrix-memory")
    if provider is None:
        pytest.skip("matrix-memory submodule not checked out")
    agent = build_agent(provider)
    try:
        agent._sync_external_memory_for_turn(
            original_user_message="[SYSTEM: scheduled cron job] If there is genuinely nothing new, say so.",
            final_response="Nothing new today from the feed.", interrupted=False,
        )
        agent._memory_manager.flush_pending(timeout=30)
        rows_after_sync = _working_memory_rows(data_dir)
        reply = None
        if explicit_remember:
            reply = agent._memory_manager.handle_tool_call(
                "mnemosyne_remember", {"content": "Weekly B1 recap: Konjunktiv II, Passiv"})
        return provider._agent_context, rows_after_sync, reply
    finally:
        agent.close()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "mnemosyne"
    d.mkdir()
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(d))
    return d


def test_cron_run_skips_passive_writes_but_keeps_explicit_tools(data_dir):
    """Bug repro (#251): a cron fire's turn must not land in working memory via sync_turn,
    but the job's own mnemosyne_remember call must still be stored."""
    context, rows_after_sync, reply = _matrix_memory_turn(
        lambda p: _construct_cron(p, "matrix-memory"), data_dir, explicit_remember=True)
    assert context == "cron"
    assert rows_after_sync == 0
    assert "memory_unavailable" not in reply and '"error"' not in reply, reply
    assert _working_memory_rows(data_dir) == 1


def test_primary_run_does_write_working_memory_control(data_dir):
    """Control: the same turn on a primary agent IS stored under data_dir, so 0 rows above
    means skipped, not "wrote somewhere this test does not look"."""
    context, rows_after_sync, _ = _matrix_memory_turn(
        lambda p: _build(p, "matrix-memory", skip_memory=False, platform="cli", session_id="s-primary"), data_dir)
    assert context == "primary"
    assert rows_after_sync > 0
