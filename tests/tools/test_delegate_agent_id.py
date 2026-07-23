#!/usr/bin/env python3
"""Tests for explicit agent_id propagation through delegation (#194).

The agent_id string is caller-owned identity: preserved verbatim (stripped),
never validated against a registry, and anonymous (None) when absent/invalid.
"""

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _build_child_progress_callback,
    _normalize_agent_id,
    delegate_task,
)


def _make_mock_parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.session_id = "parent-session-1"
    return parent


def _make_child_stub(*_args, **_kwargs):
    """Stand-in for AIAgent so _session_init_model_config is a REAL dict we
    can assert against (a bare MagicMock swallows __setitem__)."""
    child = MagicMock()
    child._session_init_model_config = {}
    child.session_id = "child-session-1"
    return child


def _run_result(i=0):
    return {
        "task_index": i,
        "status": "completed",
        "summary": "ok",
        "api_calls": 1,
        "duration_seconds": 0.1,
    }


class TestNormalizeAgentId(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(_normalize_agent_id(None))

    def test_empty_string(self):
        self.assertIsNone(_normalize_agent_id(""))

    def test_whitespace_only(self):
        self.assertIsNone(_normalize_agent_id("   \t"))

    def test_non_string(self):
        self.assertIsNone(_normalize_agent_id(123))
        self.assertIsNone(_normalize_agent_id(["neo"]))
        self.assertIsNone(_normalize_agent_id({"id": "neo"}))

    def test_verbatim_preserved_stripped(self):
        self.assertEqual(_normalize_agent_id(" neo "), "neo")
        # No registry validation — arbitrary strings pass through verbatim.
        self.assertEqual(_normalize_agent_id("not-a-registered-agent"),
                         "not-a-registered-agent")


class TestSchema(unittest.TestCase):
    def test_agent_id_exposed_top_level_and_per_task(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("agent_id", props)
        self.assertEqual(props["agent_id"]["type"], "string")
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("agent_id", task_props)
        self.assertEqual(task_props["agent_id"]["type"], "string")
        # agent_id must never become required.
        self.assertNotIn("agent_id", DELEGATE_TASK_SCHEMA["parameters"].get("required", []))


class TestDelegateTaskAgentId(unittest.TestCase):
    @patch("tools.delegate_tool._run_single_child")
    @patch("run_agent.AIAgent")
    def test_single_task_propagation_and_persistence(self, mock_agent_cls, mock_run):
        mock_agent_cls.side_effect = _make_child_stub
        mock_run.return_value = _run_result()
        parent = _make_mock_parent()

        result = json.loads(
            delegate_task(goal="do a thing", agent_id="neo", parent_agent=parent)
        )
        self.assertIn("results", result)

        # side_effect built the child; recover it from the run call.
        child = mock_run.call_args.kwargs.get("child")
        if child is None:
            child = mock_run.call_args.args[2]
        self.assertEqual(child._agent_id, "neo")
        self.assertEqual(child._session_init_model_config["_agent_id"], "neo")
        # The lineage marker still rides alongside — same JSON blob.
        self.assertEqual(
            child._session_init_model_config["_delegate_from"], "parent-session-1"
        )

    @patch("tools.delegate_tool._run_single_child")
    @patch("run_agent.AIAgent")
    def test_batch_mixed_agent_ids_stay_per_task(self, mock_agent_cls, mock_run):
        """Guards the loop-scope hazard: a mixed-agent_id batch must never
        share one value across children."""
        children = []

        def _capture(*a, **kw):
            child = _make_child_stub()
            children.append(child)
            return child

        mock_agent_cls.side_effect = _capture
        mock_run.side_effect = [_run_result(0), _run_result(1), _run_result(2)]
        parent = _make_mock_parent()

        tasks = [
            {"goal": "task a", "agent_id": "neo"},
            {"goal": "task b", "agent_id": "trinity"},
            {"goal": "task c"},  # falls back to top-level agent_id
        ]
        json.loads(
            delegate_task(tasks=tasks, agent_id="morpheus", parent_agent=parent)
        )

        self.assertEqual(len(children), 3)
        self.assertEqual(children[0]._agent_id, "neo")
        self.assertEqual(children[1]._agent_id, "trinity")
        self.assertEqual(children[2]._agent_id, "morpheus")
        self.assertEqual(children[0]._session_init_model_config["_agent_id"], "neo")
        self.assertEqual(children[1]._session_init_model_config["_agent_id"], "trinity")
        self.assertEqual(children[2]._session_init_model_config["_agent_id"], "morpheus")

    @patch("tools.delegate_tool._run_single_child")
    @patch("run_agent.AIAgent")
    def test_absent_agent_id_stays_anonymous(self, mock_agent_cls, mock_run):
        mock_agent_cls.side_effect = _make_child_stub
        mock_run.return_value = _run_result()
        parent = _make_mock_parent()

        json.loads(delegate_task(goal="do a thing", parent_agent=parent))

        child = mock_run.call_args.kwargs.get("child")
        if child is None:
            child = mock_run.call_args.args[2]
        self.assertIsNone(child._agent_id)
        self.assertNotIn("_agent_id", child._session_init_model_config)

    @patch("tools.delegate_tool._run_single_child")
    @patch("run_agent.AIAgent")
    def test_empty_agent_id_stays_anonymous(self, mock_agent_cls, mock_run):
        mock_agent_cls.side_effect = _make_child_stub
        mock_run.return_value = _run_result()
        parent = _make_mock_parent()

        json.loads(delegate_task(goal="do a thing", agent_id="   ", parent_agent=parent))

        child = mock_run.call_args.kwargs.get("child")
        if child is None:
            child = mock_run.call_args.args[2]
        self.assertIsNone(child._agent_id)
        self.assertNotIn("_agent_id", child._session_init_model_config)


class TestProgressCallbackIdentityKwargs(unittest.TestCase):
    def _capture_parent(self):
        events = []
        parent = MagicMock()
        parent._delegate_spinner = None

        def _cb(event_type, tool_name=None, preview=None, args=None, **kwargs):
            events.append((event_type, kwargs))

        parent.tool_progress_callback = _cb
        return parent, events

    def test_identity_kwargs_include_agent_id(self):
        parent, events = self._capture_parent()
        cb = _build_child_progress_callback(
            0, "goal", parent, 1, subagent_id="sa-0-abc", agent_id="neo"
        )
        self.assertIsNotNone(cb)
        cb("subagent.start", preview="goal")
        self.assertEqual(len(events), 1)
        event_type, kwargs = events[0]
        self.assertEqual(event_type, "subagent.start")
        self.assertEqual(kwargs.get("agent_id"), "neo")
        self.assertEqual(kwargs.get("subagent_id"), "sa-0-abc")

    def test_no_agent_id_key_when_absent(self):
        parent, events = self._capture_parent()
        cb = _build_child_progress_callback(0, "goal", parent, 1, subagent_id="sa-0-abc")
        self.assertIsNotNone(cb)
        cb("subagent.start", preview="goal")
        _, kwargs = events[0]
        self.assertNotIn("agent_id", kwargs)


if __name__ == "__main__":
    unittest.main()
