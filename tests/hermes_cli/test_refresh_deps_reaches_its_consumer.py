"""`hermes update --refresh-deps` must actually refresh, not just parse.

Three pieces have to line up, and each landed in a different unit:
  * the flag exists            (hermes_cli/subcommands/update.py)
  * the dashboard spawns it    (hermes_cli/web_routers/actions.py, strict update)
  * something READS it         (hermes_cli/update_cmd.py -> _run_post_source_refresh)

The third was missing, so the chain was silently dead: the strict-update button
reported success while the new source ran against the OLD dependencies and the
OLD built frontend. Nothing failed -- that is exactly why it needs a test.
"""

from __future__ import annotations

import argparse
import inspect

import pytest


def test_flag_is_defined_with_the_dest_the_consumer_reads():
    """argparse turns --refresh-deps into refresh_deps; a rename breaks the chain."""
    from hermes_cli.subcommands.update import build_update_parser
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(dest="command"), cmd_update=lambda _a: None)
    args = parser.parse_args(["update", "--refresh-deps", "--restart-after-refresh"])
    assert args.refresh_deps is True
    assert args.restart_after_refresh is True


def test_update_impl_consumes_the_flag():
    from hermes_cli import update_cmd
    src = inspect.getsource(update_cmd._cmd_update_impl)
    assert 'getattr(args, "refresh_deps"' in src, "--refresh-deps parses but nothing reads it"
    assert "_run_post_source_refresh" in src


def test_consumer_exists_and_takes_restart_after():
    from hermes_cli.update_cmd_deps import _run_post_source_refresh
    params = inspect.signature(_run_post_source_refresh).parameters
    assert "restart_after" in params


def test_consumer_runs_before_the_managed_install_guards():
    """The strict endpoint already proved this is a git checkout and applied the
    fast-forward; a managed-install guard running first would refuse the very case
    this flag exists for."""
    from hermes_cli import update_cmd
    src = inspect.getsource(update_cmd._cmd_update_impl)
    assert src.index('getattr(args, "refresh_deps"') < src.index("_resolve_update_options")


def test_dashboard_spawns_the_flag_it_expects_to_work():
    """The strict-update action is the only production caller; pin the pairing."""
    import pathlib
    actions = pathlib.Path(update_src()).read_text(encoding="utf-8")
    assert '"--refresh-deps"' in actions and '"--restart-after-refresh"' in actions


def update_src() -> str:
    import hermes_cli.web_routers.actions as mod
    return mod.__file__
