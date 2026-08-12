import inspect

from tui_gateway import slash_worker


def test_is_orphaned_true_when_ppid_changes():
    # Our parent went away and we were reparented to a subreaper/init.
    assert slash_worker._is_orphaned(1234, getppid=lambda: 999999) is True


def test_is_orphaned_false_when_direct_parent_is_unchanged():
    original_ppid = 1234
    assert slash_worker._is_orphaned(original_ppid, getppid=lambda: original_ppid) is False


def test_parent_death_watchdog_contract_has_no_create_time_plumbing():
    assert list(inspect.signature(slash_worker._is_orphaned).parameters) == [
        "original_ppid",
        "getppid",
    ]
    assert list(inspect.signature(slash_worker._start_parent_death_watchdog).parameters) == [
        "original_ppid",
    ]


def test_worker_marks_its_cli_non_interactive_for_confirms():
    """Issue #220 — confirm prompts must never reach input() in the worker.

    The worker's stdin is the JSON-RPC line protocol, so a bare input() both
    wedges the process until the gateway's timeout kills it and swallows the
    next request line. ``main()`` flags the CLI so the prompt helpers cancel.
    """
    src = inspect.getsource(slash_worker.main)
    assert "cli._noninteractive_confirm = True" in src


def test_noninteractive_confirm_is_not_keyed_off_hermes_interactive():
    """The flag must stay separate from HERMES_INTERACTIVE.

    ``tools/approval.py`` reads HERMES_INTERACTIVE to decide whether dangerous
    commands may prompt or must fail closed; the worker still sets it to "1".
    Reusing it as the confirm signal would silently change approval semantics.
    """
    src = inspect.getsource(slash_worker.main)
    assert 'os.environ["HERMES_INTERACTIVE"] = "1"' in src
