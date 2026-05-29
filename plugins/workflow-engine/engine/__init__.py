"""
Workflow Engine — core Python package.

Public API::

    from engine import WorkflowEngine, create_engine

Import paths for sub-packages::

    from engine.db.client import open_db
    from engine.db.migrate import ensure_schema
    from engine.schemas.workflow import WorkflowDefinition
    from engine.discovery.loader import parse_workflow
    from engine.discovery.validator import validate_workflow_yaml
    from engine.store.run_store import RunStore
    from engine.store.definition_store import DefinitionStore
    from engine.emitter.bus import EventBus
    from engine.runtime.runner import WorkflowRunner
    from engine.facade import WorkflowEngine
    from engine.wiring import create_engine
"""
from engine.facade import WorkflowEngine
from engine.wiring import create_engine

__all__ = ["WorkflowEngine", "create_engine"]
