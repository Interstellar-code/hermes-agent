"""scripts/check_known_failures.py: a failure not on the known list must fail the check."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_known_failures.py"
_spec = importlib.util.spec_from_file_location("check_known_failures", _PATH)
ckf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ckf)

KNOWN = (
    "# header\n\n"
    "tests/a/test_x.py::test_one  # macOS: something\n"
    "tests/a/test_x.py::test_p[/mnt/my share/db-True]  # id with a space\n"
    "tests/b/test_y.py::test_old  # was broken\n"
)


def _log(*failed, ran=("tests/a/test_x.py", "tests/b/test_y.py")):
    lines = [f"[ 50% | 1/2 ] ✗ {f} (3✓ 1✗, 0.1s)" for f in ran]
    lines += [f"  ║ FAILED {n}" for n in failed]
    return "\n".join(lines) + "\n"


def _run(tmp_path, log_text, *extra):
    (tmp_path / "known.txt").write_text(KNOWN, encoding="utf-8")
    (tmp_path / "run.log").write_text(log_text, encoding="utf-8")
    return ckf.main([str(tmp_path / "run.log"), "--known", str(tmp_path / "known.txt"), *extra])


def test_only_known_failures_passes(tmp_path):
    log = _log("tests/a/test_x.py::test_one", "tests/a/test_x.py::test_p[/mnt/my share/db-True]",
               "tests/b/test_y.py::test_old")
    assert _run(tmp_path, log) == 0


def test_new_failure_blocks(tmp_path, capsys):
    log = _log("tests/a/test_x.py::test_one", "tests/a/test_x.py::test_brand_new")
    assert _run(tmp_path, log) == 1
    assert "NEW    tests/a/test_x.py::test_brand_new" in capsys.readouterr().out


def test_parametrized_id_with_space_is_matched_whole(tmp_path):
    # Splitting on whitespace once turned this id into two bogus entries.
    log = _log("tests/a/test_x.py::test_one", "tests/a/test_x.py::test_p[/mnt/my share/db-True]",
               "tests/b/test_y.py::test_old")
    assert _run(tmp_path, log) == 0


def test_fixed_entry_is_reported_and_strict_fails(tmp_path, capsys):
    log = _log("tests/a/test_x.py::test_one", "tests/a/test_x.py::test_p[/mnt/my share/db-True]")
    assert _run(tmp_path, log) == 0
    assert "FIXED  tests/b/test_y.py::test_old" in capsys.readouterr().out
    assert _run(tmp_path, log, "--strict") == 1


def test_files_that_did_not_run_are_not_reported_fixed(tmp_path, capsys):
    log = _log("tests/a/test_x.py::test_one", "tests/a/test_x.py::test_p[/mnt/my share/db-True]",
               ran=("tests/a/test_x.py",))
    assert _run(tmp_path, log, "--strict") == 0
    assert "FIXED  " not in capsys.readouterr().out  # the per-entry marker, not the summary
