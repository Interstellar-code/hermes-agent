"""Tests for /personality none — clearing personality overlay."""
import pytest
from unittest.mock import MagicMock, patch
import yaml


# ── CLI tests ──────────────────────────────────────────────────────────────

class TestCLIPersonalityNone:

    def _make_cli(self, personalities=None, base_system_prompt=""):
        from cli import HermesCLI
        cli = HermesCLI.__new__(HermesCLI)
        cli.personalities = personalities or {
            "helpful": "You are helpful.",
            "concise": "You are concise.",
        }
        cli.base_system_prompt = base_system_prompt
        cli.personality = "kawaii"
        cli._personality_session_override = False
        cli.system_prompt = "You are kawaii~"
        cli.agent = MagicMock()
        cli.console = MagicMock()
        return cli

    def test_none_clears_system_prompt(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality none")
        assert cli.system_prompt == ""

    def test_default_clears_system_prompt(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality default")
        assert cli.system_prompt == ""

    def test_neutral_clears_system_prompt(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality neutral")
        assert cli.system_prompt == ""

    def test_none_forces_agent_reinit(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality none")
        assert cli.agent is None

    def test_none_saves_to_config_only_with_global(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True) as mock_save:
            cli._handle_personality_command("/personality none --global")
        mock_save.assert_called_once_with("agent.personality", "")

    def test_known_personality_still_works(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality helpful")
        assert cli.system_prompt == "You are helpful."

    def test_unknown_personality_shows_none_in_available(self, capsys):
        cli = self._make_cli()
        cli._handle_personality_command("/personality nonexistent")
        output = capsys.readouterr().out
        assert "none" in output.lower()

    def test_list_shows_none_option(self):
        cli = self._make_cli()
        with patch("builtins.print") as mock_print:
            cli._handle_personality_command("/personality")
        output = " ".join(str(c) for c in mock_print.call_args_list)
        assert "none" in output.lower()


class TestPersonalityScope:
    """Regressions for #223 — /personality must not clobber agent.system_prompt.

    Before the fix, /personality wrote the resolved preset text straight into
    the global ``agent.system_prompt`` key (and ``/personality none`` blanked
    it), destroying any hand-written prompt with no backup.
    """

    def _make_cli(self, base_system_prompt="", personality=""):
        from cli import HermesCLI
        cli = HermesCLI.__new__(HermesCLI)
        cli.personalities = {"helpful": "You are helpful.", "concise": "You are concise."}
        cli.base_system_prompt = base_system_prompt
        cli.personality = personality
        cli._personality_session_override = False
        cli.system_prompt = cli._compose_system_prompt()
        cli.agent = MagicMock()
        cli.console = MagicMock()
        return cli

    # ── scope ──────────────────────────────────────────────────────────────
    def test_set_without_global_does_not_write_config(self):
        cli = self._make_cli()
        with patch("cli.save_config_value") as mock_save:
            cli._handle_personality_command("/personality helpful")
        mock_save.assert_not_called()
        assert cli.system_prompt == "You are helpful."
        assert cli.personality == "helpful"

    def test_clear_without_global_does_not_write_config(self):
        cli = self._make_cli(personality="helpful")
        with patch("cli.save_config_value") as mock_save:
            cli._handle_personality_command("/personality none")
        mock_save.assert_not_called()
        assert cli.personality == ""

    def test_global_writes_personality_name_not_prompt_text(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True) as mock_save:
            cli._handle_personality_command("/personality helpful --global")
        mock_save.assert_called_once_with("agent.personality", "helpful")

    def test_session_flag_is_accepted_as_noop(self):
        cli = self._make_cli()
        with patch("cli.save_config_value") as mock_save:
            cli._handle_personality_command("/personality concise --session")
        mock_save.assert_not_called()
        assert cli.system_prompt == "You are concise."

    # ── the hand-written prompt survives ───────────────────────────────────
    def test_global_leaves_existing_system_prompt_intact(self):
        cli = self._make_cli(base_system_prompt="MY OWN PROMPT")
        with patch("cli.save_config_value", return_value=True) as mock_save:
            cli._handle_personality_command("/personality helpful --global")
        assert mock_save.call_args_list == [(("agent.personality", "helpful"),)]
        assert cli.base_system_prompt == "MY OWN PROMPT"

    def test_none_does_not_blank_the_hand_written_prompt(self):
        cli = self._make_cli(base_system_prompt="MY OWN PROMPT", personality="helpful")
        with patch("cli.save_config_value", return_value=True) as mock_save:
            cli._handle_personality_command("/personality none --global")
        mock_save.assert_called_once_with("agent.personality", "")
        assert cli.base_system_prompt == "MY OWN PROMPT"
        # Clearing the personality falls back to the user's own prompt.
        assert cli.system_prompt == "MY OWN PROMPT"

    def test_config_yaml_is_untouched_without_global(self, tmp_path, monkeypatch):
        """End-to-end against a real config.yaml, no save_config_value patch."""
        import cli as cli_mod

        config_file = tmp_path / "config.yaml"
        original = "agent:\n  system_prompt: MY OWN PROMPT\n"
        config_file.write_text(original)
        monkeypatch.setattr(cli_mod, "_hermes_home", tmp_path)

        cli = self._make_cli(base_system_prompt="MY OWN PROMPT")
        cli._handle_personality_command("/personality helpful")
        assert config_file.read_text() == original

        cli._handle_personality_command("/personality helpful --global")
        saved = yaml.safe_load(config_file.read_text())
        assert saved["agent"]["personality"] == "helpful"
        assert saved["agent"]["system_prompt"] == "MY OWN PROMPT"

    # ── precedence ─────────────────────────────────────────────────────────
    def test_hand_written_prompt_beats_stored_personality(self):
        """Both keys set in config.yaml → the hand-written prompt wins."""
        cli = self._make_cli(base_system_prompt="MY OWN PROMPT", personality="helpful")
        assert cli.system_prompt == "MY OWN PROMPT"

    def test_stored_personality_applies_when_no_hand_written_prompt(self):
        cli = self._make_cli(personality="concise")
        assert cli.system_prompt == "You are concise."

    def test_live_command_beats_hand_written_prompt_for_the_session(self):
        cli = self._make_cli(base_system_prompt="MY OWN PROMPT")
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality helpful")
        assert cli.system_prompt == "You are helpful."

    def test_global_warns_when_system_prompt_shadows_the_personality(self, capsys):
        cli = self._make_cli(base_system_prompt="MY OWN PROMPT")
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality helpful --global")
        out = capsys.readouterr().out
        assert "agent.system_prompt" in out
        assert "precedence" in out.lower()


# ── Gateway tests ──────────────────────────────────────────────────────────

class TestGatewayPersonalityNone:

    def _make_event(self, args=""):
        event = MagicMock()
        event.get_command.return_value = "personality"
        event.get_command_args.return_value = args
        return event

    def _make_runner(self, personalities=None):
        from gateway.run import GatewayRunner
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._ephemeral_system_prompt = "You are kawaii~"
        runner.config = {
            "agent": {
                "personalities": personalities or {"helpful": "You are helpful."}
            }
        }
        return runner

    @pytest.mark.asyncio
    async def test_none_clears_ephemeral_prompt(self, tmp_path):
        runner = self._make_runner()
        config_data = {"agent": {"personalities": {"helpful": "You are helpful."}, "system_prompt": "kawaii"}}
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        with patch("gateway.run._hermes_home", tmp_path):
            event = self._make_event("none")
            result = await runner._handle_personality_command(event)

        assert runner._ephemeral_system_prompt == ""
        assert "cleared" in result.lower()

    @pytest.mark.asyncio
    async def test_default_clears_ephemeral_prompt(self, tmp_path):
        runner = self._make_runner()
        config_data = {"agent": {"personalities": {"helpful": "You are helpful."}}}
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        with patch("gateway.run._hermes_home", tmp_path):
            event = self._make_event("default")
            result = await runner._handle_personality_command(event)

        assert runner._ephemeral_system_prompt == ""

    @pytest.mark.asyncio
    async def test_list_includes_none(self, tmp_path):
        runner = self._make_runner()
        config_data = {"agent": {"personalities": {"helpful": "You are helpful."}}}
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        with patch("gateway.run._hermes_home", tmp_path):
            event = self._make_event("")
            result = await runner._handle_personality_command(event)

        assert "none" in result.lower()

    @pytest.mark.asyncio
    async def test_unknown_shows_none_in_available(self, tmp_path):
        runner = self._make_runner()
        config_data = {"agent": {"personalities": {"helpful": "You are helpful."}}}
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        with patch("gateway.run._hermes_home", tmp_path):
            event = self._make_event("nonexistent")
            result = await runner._handle_personality_command(event)

        assert "none" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_personality_list_uses_profile_display_path(self, tmp_path):
        runner = self._make_runner(personalities={})
        (tmp_path / "config.yaml").write_text(yaml.dump({"agent": {"personalities": {}}}))

        with patch("gateway.run._hermes_home", tmp_path), \
             patch("hermes_constants.display_hermes_home", return_value="~/.hermes/profiles/coder"):
            event = self._make_event("")
            result = await runner._handle_personality_command(event)

        assert result == "No personalities configured in `~/.hermes/profiles/coder/config.yaml`"


class TestPersonalityDictFormat:
    """Test dict-format custom personalities with description, tone, style."""

    def _make_cli(self, personalities):
        from cli import HermesCLI
        cli = HermesCLI.__new__(HermesCLI)
        cli.personalities = personalities
        cli.system_prompt = ""
        cli.agent = None
        cli.console = MagicMock()
        return cli

    def test_dict_personality_uses_system_prompt(self):
        cli = self._make_cli({
            "coder": {
                "description": "Expert programmer",
                "system_prompt": "You are an expert programmer.",
                "tone": "technical",
                "style": "concise",
            }
        })
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality coder")
        assert "You are an expert programmer." in cli.system_prompt

    def test_dict_personality_includes_tone(self):
        cli = self._make_cli({
            "coder": {
                "system_prompt": "You are an expert programmer.",
                "tone": "technical and precise",
            }
        })
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality coder")
        assert "Tone: technical and precise" in cli.system_prompt

    def test_dict_personality_includes_style(self):
        cli = self._make_cli({
            "coder": {
                "system_prompt": "You are an expert programmer.",
                "style": "use code examples",
            }
        })
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality coder")
        assert "Style: use code examples" in cli.system_prompt

    def test_string_personality_still_works(self):
        cli = self._make_cli({"helper": "You are helpful."})
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality helper")
        assert cli.system_prompt == "You are helpful."

    def test_resolve_prompt_dict_no_tone_no_style(self):
        from cli import HermesCLI
        result = HermesCLI._resolve_personality_prompt({
            "description": "A helper",
            "system_prompt": "You are helpful.",
        })
        assert result == "You are helpful."

    def test_resolve_prompt_string(self):
        from cli import HermesCLI
        result = HermesCLI._resolve_personality_prompt("You are helpful.")
        assert result == "You are helpful."


class TestPersonalityReachesOtherSurfaces:
    """A --global personality must apply beyond the CLI (#223).

    The old, destructive behaviour wrote the preset text into
    agent.system_prompt, so a personality reached the messaging gateway and
    cron runs for free. Storing a NAME under agent.personality fixes the data
    loss but would silently narrow the feature unless the other surfaces
    resolve that name too — so these pin the shared resolver and, crucially,
    the precedence at the call sites.
    """

    def test_resolves_string_personality(self):
        from hermes_cli.config import resolve_personality_prompt

        cfg = {"agent": {"personality": "concise", "personalities": {"concise": "Be brief."}}}
        assert resolve_personality_prompt(cfg) == "Be brief."

    def test_resolves_dict_personality_with_tone_and_style(self):
        from hermes_cli.config import resolve_personality_prompt

        cfg = {
            "agent": {
                "personality": "x",
                "personalities": {"x": {"system_prompt": "Base.", "tone": "warm", "style": "terse"}},
            }
        }
        assert resolve_personality_prompt(cfg) == "Base.\nTone: warm\nStyle: terse"

    def test_unknown_or_cleared_name_resolves_empty_not_raising(self):
        from hermes_cli.config import resolve_personality_prompt

        personalities = {"concise": "Be brief."}
        for name in ("ghost", "none", "default", "neutral", "off", ""):
            cfg = {"agent": {"personality": name, "personalities": personalities}}
            assert resolve_personality_prompt(cfg) == "", name
        assert resolve_personality_prompt({}) == ""

    def test_gateway_prefers_explicit_system_prompt_over_personality(self, monkeypatch):
        """Precedence lives at the call site, not in the resolver."""
        from gateway.run import GatewayRunner

        cfg = {
            "agent": {
                "system_prompt": "My own prompt.",
                "personality": "concise",
                "personalities": {"concise": "Be brief."},
            }
        }
        monkeypatch.delenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", raising=False)
        monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: cfg)
        assert GatewayRunner._load_ephemeral_system_prompt() == "My own prompt."

    def test_gateway_falls_back_to_personality_when_no_system_prompt(self, monkeypatch):
        from gateway.run import GatewayRunner

        cfg = {"agent": {"personality": "concise", "personalities": {"concise": "Be brief."}}}
        monkeypatch.delenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", raising=False)
        monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: cfg)
        assert GatewayRunner._load_ephemeral_system_prompt() == "Be brief."
