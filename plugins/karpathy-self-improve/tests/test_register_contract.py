"""
test_register_contract.py — verify register(ctx) contract.

Checks:
  - exactly 1 CLI command registered, named "karpathy"
  - no AttributeError / include_router calls (ctx has no such method)
  - register() never raises
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock


@dataclass
class FakeCtx:
    tools: List[Dict[str, Any]] = field(default_factory=list)
    hooks: List[Dict[str, Any]] = field(default_factory=list)
    cli_commands: List[Dict[str, Any]] = field(default_factory=list)
    commands: List[Dict[str, Any]] = field(default_factory=list)
    llm: Any = field(default_factory=MagicMock)

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        **kwargs: Any,
    ) -> None:
        self.tools.append({"name": name, "toolset": toolset})

    def register_hook(self, hook_name: str, callback: Callable) -> None:
        self.hooks.append({"hook": hook_name})

    def register_cli_command(
        self,
        name: str,
        help: str,
        setup_fn: Callable,
        handler_fn: Optional[Callable] = None,
        description: str = "",
    ) -> None:
        self.cli_commands.append({"name": name})

    def register_command(
        self,
        name: str,
        handler: Callable,
        description: str = "",
        args_hint: str = "",
    ) -> None:
        self.commands.append({"name": name})

    # Deliberately absent: include_router, register_dashboard_router, etc.
    # If register() tries to call one of these, the test will raise AttributeError.


def test_register_cli_command_karpathy() -> None:
    """register() must register exactly 1 CLI command named 'karpathy'."""
    import plugins.karpathy_self_improve as p

    ctx = FakeCtx()
    p.register(ctx)

    cli_names = [c["name"] for c in ctx.cli_commands]
    assert "karpathy" in cli_names, f"Expected CLI command 'karpathy', got: {cli_names}"
    assert len(ctx.cli_commands) == 1, (
        f"Expected exactly 1 CLI command, got {len(ctx.cli_commands)}: {cli_names}"
    )


def test_register_slash_command_karpathy() -> None:
    """register() must register the '/karpathy' slash command."""
    import plugins.karpathy_self_improve as p

    ctx = FakeCtx()
    p.register(ctx)

    slash_names = [c["name"] for c in ctx.commands]
    assert "karpathy" in slash_names, (
        f"Expected slash command 'karpathy', got: {slash_names}"
    )


def test_register_never_raises() -> None:
    """register() must not raise even if ctx is minimal."""
    import plugins.karpathy_self_improve as p

    ctx = FakeCtx()
    # Should not raise
    p.register(ctx)


def test_register_no_include_router() -> None:
    """register() must not call ctx.include_router (it doesn't exist on ctx)."""
    import plugins.karpathy_self_improve as p

    # FakeCtx has no include_router; if register() calls it, AttributeError fires.
    ctx = FakeCtx()
    p.register(ctx)  # passes means no include_router call was made
