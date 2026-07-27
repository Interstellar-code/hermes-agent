#!/usr/bin/env python3
"""Prune invalid session keys from sessions.json where platform is not a valid Platform enum value.

Derived dynamically from gateway.config.Platform.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

# Ensure repository root is on sys.path when executed directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gateway.config import Platform
from hermes_constants import get_hermes_home


def is_valid_platform(platform_name: str | None) -> bool:
    """Return True if platform_name is a valid member of gateway.config.Platform."""
    if not platform_name:
        return False
    try:
        p = Platform(platform_name)
        return p is not None
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def extract_platform_segment(key: str, entry: object) -> str | None:
    """Extract the platform segment from a session key or entry object.

    Metadata keys starting with '_' return None.
    Standard keys like 'agent:main:<platform>:...' return the third colon-separated field.
    """
    if key.startswith("_"):
        return None
    parts = key.split(":")
    if len(parts) >= 3 and parts[0] == "agent" and parts[1] == "main":
        return parts[2]
    if isinstance(entry, dict):
        if entry.get("platform"):
            return str(entry["platform"])
        origin = entry.get("origin")
        if isinstance(origin, dict) and origin.get("platform"):
            return str(origin["platform"])
    return None


def resolve_sessions_file(profile_path_arg: str | Path | None = None) -> Path:
    """Resolve the path to sessions.json from an optional profile or file argument."""
    if profile_path_arg:
        p = Path(profile_path_arg).expanduser().resolve()
        if p.is_file():
            return p
        if (p / "sessions" / "sessions.json").exists():
            return p / "sessions" / "sessions.json"
        if (p / "sessions.json").exists():
            return p / "sessions.json"
        return p / "sessions" / "sessions.json"
    return _active_profile_home() / "sessions" / "sessions.json"


def _active_profile_home() -> Path:
    """Home directory of the profile this script should target by default.

    ``get_hermes_home()`` is NOT the right default on its own: with HERMES_HOME
    unset it returns ``~/.hermes``, which is the *default* profile — so a bare
    invocation would silently target a different profile's data than the active
    one (``~/.hermes/profiles/<name>``). Since this script deletes keys, aiming
    at the wrong profile is the worst failure mode it has.

    An explicitly set HERMES_HOME wins: that means the caller pinned a profile.
    Otherwise fall back to the sticky active profile on disk.
    """
    if os.environ.get("HERMES_HOME"):
        return get_hermes_home()

    from hermes_cli.profiles import get_active_profile, get_profile_dir

    name = get_active_profile()
    if name in ("", "default"):
        return get_hermes_home()
    return get_profile_dir(name)


def prune_invalid_session_keys(
    sessions_file: Path, dry_run: bool = False
) -> tuple[int, int, int]:
    """Prune session keys whose platform is not a valid Platform enum value.

    Returns tuple of (before_count, deleted_count, surviving_count).
    """
    if not sessions_file.exists():
        raise FileNotFoundError(f"sessions.json not found at {sessions_file}")

    with open(sessions_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    before_count = len(data)
    surviving: dict[str, object] = {}
    deleted: list[tuple[str, str | None]] = []

    for key, val in data.items():
        if key.startswith("_"):
            surviving[key] = val
            continue
        seg = extract_platform_segment(key, val)
        if seg and is_valid_platform(seg):
            surviving[key] = val
        else:
            deleted.append((key, seg))

    # Safety assertions — refuse to run if any key with a valid platform would be lost
    for key, seg in deleted:
        if seg and is_valid_platform(seg):
            raise RuntimeError(
                f"Safety assertion failure: key {key!r} has valid platform {seg!r} "
                f"but was marked for deletion!"
            )

    for key, val in data.items():
        if not key.startswith("_"):
            seg = extract_platform_segment(key, val)
            if seg and is_valid_platform(seg):
                if key not in surviving:
                    raise RuntimeError(
                        f"Safety assertion failure: valid platform key {key!r} ({seg!r}) "
                        f"was lost from surviving set!"
                    )

    surviving_count = len(surviving)
    deleted_count = len(deleted)

    print(f"Target file: {sessions_file}")
    print(f"Total keys before: {before_count}")
    print(f"Keys to delete:    {deleted_count}")
    print(f"Surviving keys:   {surviving_count}")

    if deleted:
        print(f"\nDeleted keys sample (up to 5 of {deleted_count}):")
        for key, seg in deleted[:5]:
            print(f"  - {key} (segment: {seg})")

    if dry_run:
        print("\n[DRY RUN] No changes written to disk.")
        return (before_count, deleted_count, surviving_count)

    if deleted_count == 0:
        print("\nNo invalid keys found to prune. File left untouched.")
        return (before_count, deleted_count, surviving_count)

    # Back up the file first (timestamped copy alongside it)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = sessions_file.with_name(f"sessions.json.bak.{timestamp}")
    shutil.copy2(sessions_file, backup_path)
    print(f"\nBackup created: {backup_path}")

    # Atomic write (temp file + os.replace)
    temp_file = sessions_file.with_name(f"sessions.json.tmp.{os.getpid()}")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(surviving, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp_file, sessions_file)
    print(f"Successfully pruned invalid keys and updated {sessions_file}")

    return (before_count, deleted_count, surviving_count)


def run_self_tests() -> None:
    """Exercise and verify all helper functions and edge cases on temporary files."""
    # Test is_valid_platform
    assert is_valid_platform("telegram") is True
    assert is_valid_platform("a2a_fleet") is False
    assert is_valid_platform("") is False
    assert is_valid_platform(None) is False

    # Test extract_platform_segment
    assert extract_platform_segment("_README", {}) is None
    assert extract_platform_segment("agent:main:telegram:dm:123", {}) == "telegram"
    assert extract_platform_segment("agent:main:a2a_fleet:dm:456", {}) == "a2a_fleet"
    assert extract_platform_segment("custom", {"platform": "discord"}) == "discord"
    assert (
        extract_platform_segment("custom2", {"origin": {"platform": "slack"}})
        == "slack"
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        test_file = (sessions_dir / "sessions.json").resolve()

        sample_data = {
            "_README": "doc key",
            "agent:main:telegram:dm:100": {"platform": "telegram"},
            "agent:main:a2a_fleet:dm:200": {"platform": "a2a_fleet"},
            "custom_valid": {"platform": "discord"},
            "custom_invalid": {"platform": "invalid_junk"},
        }
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump(sample_data, f, indent=2)

        # Test resolve_sessions_file variations
        assert resolve_sessions_file(tmp_path) == test_file
        assert resolve_sessions_file(test_file) == test_file

        # Test dry-run
        b, d, s = prune_invalid_session_keys(test_file, dry_run=True)
        assert b == 5
        assert d == 2
        assert s == 3
        with open(test_file, "r", encoding="utf-8") as f:
            raw_after_dry = json.load(f)
        assert len(raw_after_dry) == 5

        # Test real run
        b, d, s = prune_invalid_session_keys(test_file, dry_run=False)
        assert b == 5
        assert d == 2
        assert s == 3
        with open(test_file, "r", encoding="utf-8") as f:
            raw_after_real = json.load(f)
        assert len(raw_after_real) == 3
        assert "_README" in raw_after_real
        assert "agent:main:telegram:dm:100" in raw_after_real
        assert "custom_valid" in raw_after_real
        assert "agent:main:a2a_fleet:dm:200" not in raw_after_real
        assert "custom_invalid" not in raw_after_real

        # Verify backup file exists
        backups = list(sessions_dir.glob("sessions.json.bak.*"))
        assert len(backups) == 1

        # Test zero invalid keys path
        b2, d2, s2 = prune_invalid_session_keys(test_file, dry_run=False)
        assert b2 == 3
        assert d2 == 0
        assert s2 == 3

    print("Self-tests passed successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prune session keys with invalid platforms from sessions.json."
    )
    parser.add_argument(
        "profile_path",
        nargs="?",
        default=None,
        help="Path to profile directory or sessions.json file (default: active profile)",
    )
    parser.add_argument(
        "--profile-path",
        dest="profile_path_opt",
        default=None,
        help="Alternative flag for profile path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry-run without writing changes to disk",
    )
    args = parser.parse_args()

    # Self-tests always run: this script deletes keys from a live data file, and
    # they cost milliseconds against a temp dir. There is deliberately no flag to
    # skip them — a --self-test flag existed here but was never read, which is
    # worse than no flag at all.
    run_self_tests()

    profile_arg = args.profile_path_opt or args.profile_path
    sessions_file = resolve_sessions_file(profile_arg)
    prune_invalid_session_keys(sessions_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
