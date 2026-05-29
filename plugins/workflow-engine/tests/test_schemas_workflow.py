"""
test_schemas_workflow — round-trip tests for WorkflowBase and WorkflowDefinition.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest
from pydantic import ValidationError

from engine.schemas.workflow import WorkflowDefinition, SubgraphInput, SubgraphOutput


MINIMAL_WORKFLOW = {
    "name": "Hello World",
    "description": "Minimal workflow",
    "nodes": [{"id": "step1", "prompt": "Say hello"}],
}


def test_minimal_workflow_parses():
    wf = WorkflowDefinition.model_validate(MINIMAL_WORKFLOW)
    assert wf.name == "Hello World"
    assert wf.description == "Minimal workflow"
    assert len(wf.nodes) == 1


def test_workflow_with_all_base_fields():
    data = {
        **MINIMAL_WORKFLOW,
        "kind": "workflow",
        "provider": "anthropic",
        "model": "claude-opus-4-5",
        "modelReasoningEffort": "high",
        "webSearchMode": "cached",
        "interactive": True,
        "effort": "high",
        "mutates_checkout": False,
        "tags": ["testing", "ci"],
    }
    wf = WorkflowDefinition.model_validate(data)
    assert wf.kind == "workflow"
    assert wf.tags == ["testing", "ci"]


def test_subgraph_kind_accepted():
    data = {
        **MINIMAL_WORKFLOW,
        "kind": "subgraph",
        "id": "my-subgraph",
        "inputs": [{"name": "inputParam", "type": "string", "required": True}],
        "outputs": [{"name": "result", "from": "step1"}],
    }
    wf = WorkflowDefinition.model_validate(data)
    assert wf.kind == "subgraph"
    assert wf.id == "my-subgraph"
    assert wf.inputs is not None and len(wf.inputs) == 1
    assert wf.outputs is not None and len(wf.outputs) == 1


def test_missing_name_raises():
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate({"description": "no name", "nodes": []})


def test_missing_description_raises():
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate({"name": "no desc", "nodes": []})


def test_get_dag_nodes_returns_typed():
    wf = WorkflowDefinition.model_validate(MINIMAL_WORKFLOW)
    nodes, errors = wf.get_dag_nodes()
    assert errors == []
    assert len(nodes) == 1
    from engine.schemas.dag_node import PromptNode
    assert isinstance(nodes[0], PromptNode)


def test_get_dag_nodes_returns_errors_on_bad_node():
    data = {
        "name": "Bad",
        "description": "has a bad node",
        "nodes": [{"id": "x"}],  # no mode field
    }
    wf = WorkflowDefinition.model_validate(data)
    nodes, errors = wf.get_dag_nodes()
    assert len(errors) > 0
    assert nodes == []
