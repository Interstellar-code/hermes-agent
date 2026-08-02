"""Tests for multiplex-aware cron scheduler (cron/scheduler.py)."""

import json
from pathlib import Path
import pytest

from cron.scheduler import tick
from cron.jobs import create_job, load_jobs, save_jobs
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from agent.secret_scope import set_multiplex_active


class TestMultiplexCronScheduler:
    @pytest.mark.asyncio
    async def test_multiplex_cron_tick_serves_all_profiles(self, tmp_path, monkeypatch):
        """When multiplexing is active, tick() must inspect and run due jobs
        across ALL served profiles, each under its own HERMES_HOME."""
        default_home = tmp_path / "default_home"
        default_home.mkdir()
        (default_home / "cron").mkdir()

        coder_home = tmp_path / "coder_home"
        coder_home.mkdir()
        (coder_home / "cron").mkdir()

        # Set up mock profiles_to_serve
        profiles_served = [
            ("default", default_home),
            ("coder", coder_home),
        ]
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex=True: profiles_served,
        )

        past_ts = "2020-01-01T00:00:00Z"

        # Enable multiplexing mode
        set_multiplex_active(True)
        try:
            # Create a due job in default profile
            token = set_hermes_home_override(str(default_home))
            try:
                create_job("default task", "* * * * *", model="test-model")
                jobs = load_jobs()
                jobs[0]["next_run_at"] = past_ts
                save_jobs(jobs)
            finally:
                reset_hermes_home_override(token)

            # Create a due job in coder profile
            token = set_hermes_home_override(str(coder_home))
            try:
                create_job("coder task", "* * * * *", model="test-model")
                jobs = load_jobs()
                jobs[0]["next_run_at"] = past_ts
                save_jobs(jobs)
            finally:
                reset_hermes_home_override(token)

            # Track which profile homes were executed
            executed_homes = []

            def mock_run_one_job(job, *, adapters=None, loop=None, verbose=False):
                executed_homes.append((job["name"], get_hermes_home()))
                return True

            monkeypatch.setattr("cron.scheduler.run_one_job", mock_run_one_job)

            # Run tick
            executed_count = tick(verbose=False, sync=True)

            assert executed_count == 2
            assert len(executed_homes) == 2

            # Verify each job ran under its respective profile home
            executed_resolved = [(name, str(home.resolve())) for name, home in executed_homes]
            assert ("default task", str(default_home.resolve())) in executed_resolved
            assert ("coder task", str(coder_home.resolve())) in executed_resolved
        finally:
            set_multiplex_active(False)
