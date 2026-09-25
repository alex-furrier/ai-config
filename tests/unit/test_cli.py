"""Tests for ai_config.cli module."""

import json
import shutil
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ai_config.adapters.claude import CommandResult, InstalledMarketplace, InstalledPlugin
from ai_config.cli import main
from ai_config.converters.ir import PluginIdentity, TargetTool
from ai_config.converters.report import ConversionReport
from ai_config.types import PluginSource, PluginStatus, StatusResult, SyncAction, SyncResult


@pytest.fixture
def runner() -> CliRunner:
    """Click test runner."""
    return CliRunner()


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Create a sample config file."""
    config = tmp_path / "config.yaml"
    config.write_text(
        dedent("""
        version: 1
        targets:
          - type: claude
            config:
              marketplaces:
                my-marketplace:
                  source: github
                  repo: owner/repo
              plugins:
                - id: my-plugin@my-marketplace
                  scope: user
                  enabled: true
        """)
    )
    return config


@pytest.fixture
def minimal_plugin(tmp_path: Path) -> Path:
    """Create a minimal plugin directory for convert command tests."""
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "test-plugin", "version": "1.0.0"}'
    )
    return plugin_dir


def _stub_report(target: TargetTool) -> ConversionReport:
    """Create a minimal conversion report for CLI tests."""
    identity = PluginIdentity(plugin_id="test-plugin", name="test-plugin", version="1.0.0")
    return ConversionReport(source_plugin=identity, target_tool=target)


def test_pi_cli_verify_and_json_report_no_false_drift(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drive sync/status verification through Click with actual Pi output and only CLI inventory patched."""
    marketplace = tmp_path / "marketplace"
    source = marketplace / "dev-tools"
    shutil.copytree(Path(__file__).parents[1] / "fixtures/sample-plugins/complete-plugin", source)
    installed_source = tmp_path / "installed-cache" / "dev-tools"
    shutil.copytree(source, installed_source)
    output = tmp_path / "output"
    config = tmp_path / "config.yaml"
    config.write_text(
        dedent(f"""
        version: 1
        targets:
          - type: claude
            config:
              marketplaces:
                local:
                  source: local
                  path: {marketplace}
              plugins:
                - id: dev-tools@local
                  scope: project
                  enabled: true
              conversion:
                targets: [pi]
                scope: project
                output_dir: {output}
        """)
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    installed = [InstalledPlugin("dev-tools@local", "1", "project", True, str(installed_source))]
    with (
        patch("ai_config.operations.claude.list_installed_plugins", return_value=(installed, [])),
        patch(
            "ai_config.operations.claude.list_installed_marketplaces",
            return_value=(
                [InstalledMarketplace("local", PluginSource.LOCAL, "", str(marketplace))],
                [],
            ),
        ),
    ):
        first = runner.invoke(main, ["sync", "-c", str(config), "--verify"])
        sync_json = runner.invoke(main, ["sync", "-c", str(config), "--verify", "--json"])
        status = runner.invoke(main, ["status", "-c", str(config), "--verify"])
        status_json = runner.invoke(main, ["status", "-c", str(config), "--verify", "--json"])

    assert (
        first.exit_code == sync_json.exit_code == status.exit_code == status_json.exit_code == 0
    ), (
        first.output,
        sync_json.output,
        status.output,
        status_json.output,
    )
    payload = json.loads(sync_json.output)
    assert payload["verification"]["discrepancies"] == []
    assert {item["action"] for item in payload["targets"]["claude"]["completed_actions"]} == {
        "noop_pi_output"
    }
    assert "All in sync" in first.output
    assert "Out of sync" not in status.output
    assert json.loads(status_json.output)["errors"] == []


class TestMainGroup:
    """Tests for main CLI group."""

    def test_version(self, runner: CliRunner) -> None:
        """Shows version info."""
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.6.3" in result.output

    def test_help(self, runner: CliRunner) -> None:
        """Shows help text."""
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "ai-config" in result.output


class TestSyncCommand:
    """Tests for sync command."""

    def test_sync_dry_run(self, runner: CliRunner, config_file: Path) -> None:
        """Dry run shows planned actions without changes."""
        sync_result = SyncResult()
        sync_result.add_success(
            SyncAction(action="install", target="my-plugin@my-marketplace", scope="user")
        )

        with patch("ai_config.cli.sync_config", return_value={"claude": sync_result}):
            result = runner.invoke(main, ["sync", "-c", str(config_file), "--dry-run"])

            assert result.exit_code == 0
            assert "Dry run mode" in result.output
            assert "install" in result.output

    def test_sync_json_reports_action_reason(self, runner: CliRunner, config_file: Path) -> None:
        """Machine output includes lifecycle action and explanation without Rich preamble."""
        sync_result = SyncResult()
        sync_result.add_success(
            SyncAction(
                action="reinstall_codex_plugin",
                target="my-plugin@ai-config-my-plugin",
                reason="Installed generated plugin is disabled",
            )
        )

        with patch("ai_config.cli.sync_config", return_value={"claude": sync_result}):
            result = runner.invoke(main, ["sync", "-c", str(config_file), "--dry-run", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        action = payload["targets"]["claude"]["planned_actions"][0]
        assert action["action"] == "reinstall_codex_plugin"
        assert action["reason"] == "Installed generated plugin is disabled"

    def test_sync_json_distinguishes_completed_and_failed_actions(
        self, runner: CliRunner, config_file: Path
    ) -> None:
        sync_result = SyncResult(success=False, errors=["permission denied"])
        sync_result.add_success(
            SyncAction(action="remove_codex_plugin", target="demo@market", reason="removed")
        )
        sync_result.actions_failed.append(
            SyncAction(action="remove_codex_marketplace", target="market", reason="removed")
        )

        with patch("ai_config.cli.sync_config", return_value={"claude": sync_result}):
            result = runner.invoke(main, ["sync", "-c", str(config_file), "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)["targets"]["claude"]
        assert [item["action"] for item in payload["completed_actions"]] == ["remove_codex_plugin"]
        assert [item["action"] for item in payload["failed_actions"]] == [
            "remove_codex_marketplace"
        ]
        assert payload["planned_actions"] == []

    def test_sync_text_skips_verification_after_apply_failure(
        self, runner: CliRunner, config_file: Path
    ) -> None:
        sync_result = SyncResult(success=False, errors=["apply failed"])
        with (
            patch("ai_config.cli.sync_config", return_value={"claude": sync_result}),
            patch("ai_config.cli.verify_sync") as verify,
        ):
            result = runner.invoke(main, ["sync", "-c", str(config_file), "--verify"])

        assert result.exit_code == 1
        assert "apply failed" in result.output
        verify.assert_not_called()

    def test_sync_json_skips_verification_after_apply_failure(
        self, runner: CliRunner, config_file: Path
    ) -> None:
        sync_result = SyncResult(success=False, errors=["apply failed"])
        with (
            patch("ai_config.cli.sync_config", return_value={"claude": sync_result}),
            patch("ai_config.cli.verify_sync") as verify,
        ):
            result = runner.invoke(main, ["sync", "-c", str(config_file), "--verify", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["verification"] == {
            "requested": True,
            "performed": False,
            "discrepancies": [],
        }
        verify.assert_not_called()

    def test_sync_with_errors(self, runner: CliRunner, config_file: Path) -> None:
        """Shows errors from sync."""
        sync_result = SyncResult(success=False, errors=["Something went wrong"])

        with patch("ai_config.cli.sync_config", return_value={"claude": sync_result}):
            result = runner.invoke(main, ["sync", "-c", str(config_file)])

            assert "Something went wrong" in result.output

    def test_sync_first_action_failure_never_claims_no_changes(
        self, runner: CliRunner, config_file: Path
    ) -> None:
        sync_result = SyncResult()
        sync_result.add_failure(
            SyncAction(action="register_marketplace", target="my-marketplace"),
            "registration failed",
        )

        with patch("ai_config.cli.sync_config", return_value={"claude": sync_result}):
            result = runner.invoke(main, ["sync", "-c", str(config_file)])

        assert result.exit_code == 1
        assert "registration failed" in result.output
        assert "No changes needed" not in result.output

    def test_sync_true_noop_claims_no_changes(self, runner: CliRunner, config_file: Path) -> None:
        with patch("ai_config.cli.sync_config", return_value={"claude": SyncResult()}):
            result = runner.invoke(main, ["sync", "-c", str(config_file)])

        assert result.exit_code == 0
        assert "No changes needed" in result.output

    def test_sync_verification_drift_never_claims_no_changes(
        self, runner: CliRunner, config_file: Path
    ) -> None:
        with (
            patch("ai_config.cli.sync_config", return_value={"claude": SyncResult()}),
            patch("ai_config.cli.verify_sync", return_value=["claude: reinstall required"]),
        ):
            result = runner.invoke(main, ["sync", "-c", str(config_file), "--verify"])

        assert result.exit_code == 1
        assert "reinstall required" in result.output
        assert "No changes needed" not in result.output
        assert "All in sync" not in result.output

    def test_sync_force_convert_flag(self, runner: CliRunner, config_file: Path) -> None:
        """Force-convert flag is passed through to sync_config."""
        sync_result = SyncResult()

        with patch("ai_config.cli.sync_config", return_value={"claude": sync_result}) as mock_sync:
            result = runner.invoke(
                main,
                ["sync", "-c", str(config_file), "--force-convert"],
            )

            assert result.exit_code == 0
            assert mock_sync.called
            kwargs = mock_sync.call_args.kwargs
            assert kwargs["force_convert"] is True

    def test_sync_config_error(self, runner: CliRunner, tmp_path: Path) -> None:
        """Handles config loading errors."""
        invalid_config = tmp_path / "invalid.yaml"
        invalid_config.write_text("version: 2")  # Invalid version

        result = runner.invoke(main, ["sync", "-c", str(invalid_config)])

        assert result.exit_code == 1
        assert "Error loading config" in result.output


class TestStatusCommand:
    """Tests for status command."""

    def test_status_table_output(self, runner: CliRunner) -> None:
        """Shows status as table."""
        status_result = StatusResult(target_type="claude")
        status_result.plugins = [
            PluginStatus(
                id="my-plugin@mp",
                installed=True,
                enabled=True,
                scope="user",
                version="1.0.0",
            )
        ]
        status_result.marketplaces = ["my-marketplace"]

        with patch("ai_config.cli.get_status", return_value=status_result):
            result = runner.invoke(main, ["status"])

            assert result.exit_code == 0
            assert "my-plugin@mp" in result.output
            assert "my-marketplace" in result.output

    def test_status_json_output(self, runner: CliRunner) -> None:
        """Shows status as JSON."""
        status_result = StatusResult(target_type="claude")
        status_result.plugins = [
            PluginStatus(
                id="my-plugin",
                installed=True,
                enabled=True,
                scope="user",
                version="1.0.0",
            )
        ]

        with patch("ai_config.cli.get_status", return_value=status_result):
            result = runner.invoke(main, ["status", "--json"])

            assert result.exit_code == 0
            assert '"id": "my-plugin"' in result.output

    def test_status_json_reports_planned_lifecycle_drift(
        self, runner: CliRunner, config_file: Path
    ) -> None:
        """Status JSON exposes the same planned lifecycle actions and reasons as dry-run."""
        status_result = StatusResult(target_type="claude")
        drift = SyncResult()
        drift.add_success(
            SyncAction(
                action="install_codex_plugin",
                target="my-plugin@ai-config-my-plugin",
                reason="Generated Codex plugin is not installed",
            )
        )
        with (
            patch("ai_config.cli.get_status", return_value=status_result),
            patch("ai_config.cli.sync_config", return_value={"claude": drift}),
        ):
            result = runner.invoke(main, ["status", "--config", str(config_file), "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["planned_actions"] == [
            {
                "action": "install_codex_plugin",
                "target": "my-plugin@ai-config-my-plugin",
                "scope": None,
                "reason": "Generated Codex plugin is not installed",
            }
        ]

    def test_status_verify_uses_lifecycle_drift_for_exit_and_message(
        self, runner: CliRunner, config_file: Path
    ) -> None:
        status_result = StatusResult(target_type="claude")
        drift = SyncResult()
        drift.add_success(
            SyncAction(
                action="reinstall_codex_plugin",
                target="my-plugin@ai-config-my-plugin",
                reason="Generated package integrity drifted",
            )
        )
        with (
            patch("ai_config.cli.get_status", return_value=status_result),
            patch("ai_config.cli.sync_config", return_value={"claude": drift}),
        ):
            result = runner.invoke(main, ["status", "--config", str(config_file), "--verify"])

        assert result.exit_code == 1
        assert "reinstall_codex_plugin" in result.output
        assert "All in sync" not in result.output

    def test_status_terminal_inspection_error_exits_nonzero(self, runner: CliRunner) -> None:
        status_result = StatusResult(target_type="claude", errors=["inspection failed"])
        with patch("ai_config.cli.get_status", return_value=status_result):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 1
        assert "inspection failed" in result.output

    def test_status_drift_inspection_failure_never_claims_sync(
        self, runner: CliRunner, config_file: Path
    ) -> None:
        status_result = StatusResult(target_type="claude")
        drift = SyncResult()
        drift.add_failure(
            SyncAction(action="install_codex_plugin", target="my-plugin@ai-config-my-plugin"),
            "Codex inspection failed",
        )
        with (
            patch("ai_config.cli.get_status", return_value=status_result),
            patch("ai_config.cli.sync_config", return_value={"claude": drift}),
        ):
            result = runner.invoke(main, ["status", "--config", str(config_file), "--verify"])

        assert result.exit_code == 1
        assert "Codex inspection failed" in result.output
        assert "No lifecycle actions needed" not in result.output
        assert "All in sync" not in result.output

    def test_status_true_noop_claims_sync(self, runner: CliRunner, config_file: Path) -> None:
        status_result = StatusResult(target_type="claude")
        with (
            patch("ai_config.cli.get_status", return_value=status_result),
            patch("ai_config.cli.sync_config", return_value={"claude": SyncResult()}),
        ):
            result = runner.invoke(main, ["status", "--config", str(config_file), "--verify"])

        assert result.exit_code == 0
        assert "No lifecycle actions needed" in result.output
        assert "All in sync" in result.output

    def test_status_no_plugins(self, runner: CliRunner) -> None:
        """Shows message when no plugins installed."""
        status_result = StatusResult(target_type="claude")

        with patch("ai_config.cli.get_status", return_value=status_result):
            result = runner.invoke(main, ["status"])

            assert result.exit_code == 0
            assert "No plugins installed" in result.output


class TestUpdateCommand:
    """Tests for update command."""

    def test_update_requires_plugins_or_all(self, runner: CliRunner) -> None:
        """Update requires plugins or --all flag."""
        result = runner.invoke(main, ["update"])

        assert result.exit_code == 1
        assert "Specify plugins" in result.output

    def test_update_all(self, runner: CliRunner) -> None:
        """Updates all plugins with --all."""
        update_result = SyncResult()
        update_result.add_success(SyncAction(action="install", target="plugin1"))

        with patch("ai_config.cli.update_plugins", return_value=update_result):
            result = runner.invoke(main, ["update", "--all"])

            assert result.exit_code == 0
            assert "plugin1" in result.output

    def test_update_specific(self, runner: CliRunner) -> None:
        """Updates specific plugins."""
        update_result = SyncResult()

        with patch("ai_config.cli.update_plugins", return_value=update_result) as mock_update:
            runner.invoke(main, ["update", "plugin1", "plugin2"])

            mock_update.assert_called_once()
            call_args = mock_update.call_args
            assert call_args[1]["plugin_ids"] == ["plugin1", "plugin2"]


class TestConvertCommand:
    """Tests for convert command."""

    def test_convert_scope_user_sets_output_dir(
        self,
        runner: CliRunner,
        minimal_plugin: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Scope user should map output_dir to home when --output not provided."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

        with patch(
            "ai_config.converters.convert_plugin",
            return_value={TargetTool.CODEX: _stub_report(TargetTool.CODEX)},
        ) as mock_convert:
            result = runner.invoke(
                main,
                ["convert", str(minimal_plugin), "--target", "codex", "--scope", "user"],
            )

        assert result.exit_code == 0
        call_args = mock_convert.call_args.kwargs
        assert call_args["output_dir"] == Path(tmp_path / "home")

    def test_convert_pi_records_standalone_ownership(
        self, runner: CliRunner, minimal_plugin: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "output"
        skill = minimal_plugin / "skills" / "thing" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: thing\ndescription: thing\n---\nthing\n")
        result = runner.invoke(
            main, ["convert", str(minimal_plugin), "--target", "pi", "--output", str(output)]
        )

        assert result.exit_code == 0, result.output
        assert (output / ".ai-config/pi-ownership.json").is_file()
        assert "Standalone Pi conversion is disabled" not in result.output

    def test_convert_all_preflights_pi_before_other_target_writes(
        self, runner: CliRunner, minimal_plugin: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "output"
        ledger = output / ".ai-config/pi-ownership.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"version": 1, "files": []}')

        with pytest.raises(ValueError, match="Unsupported Pi ownership state schema"):
            runner.invoke(
                main,
                ["convert", str(minimal_plugin), "-t", "all", "--output", str(output)],
                catch_exceptions=False,
            )

        assert not (output / ".ai-config/codex").exists()
        assert not (output / ".cursor").exists()
        assert not (output / ".opencode").exists()
        assert not (output / "opencode.json").exists()

    def test_convert_pi_dry_run_reports_ownership_actions(
        self, runner: CliRunner, minimal_plugin: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "output"
        skill = minimal_plugin / "skills" / "thing" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: thing\ndescription: thing\n---\nthing\n")
        result = runner.invoke(
            main,
            [
                "convert",
                str(minimal_plugin),
                "--target",
                "pi",
                "--output",
                str(output),
                "--dry-run",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        assert '"action": "create"' in result.output
        assert not output.exists()

    def test_convert_writes_report_file(
        self, runner: CliRunner, minimal_plugin: Path, tmp_path: Path
    ) -> None:
        """Convert with --report should write report to disk."""
        report_path = tmp_path / "report.json"

        with patch(
            "ai_config.converters.convert_plugin",
            return_value={TargetTool.CODEX: _stub_report(TargetTool.CODEX)},
        ):
            result = runner.invoke(
                main,
                [
                    "convert",
                    str(minimal_plugin),
                    "--target",
                    "codex",
                    "--report",
                    str(report_path),
                    "--report-format",
                    "json",
                ],
            )

        assert result.exit_code == 0
        assert report_path.exists()
        assert '"target_tool": "codex"' in report_path.read_text()


class TestCacheCommand:
    """Tests for cache commands."""

    def test_cache_clear_success(self, runner: CliRunner) -> None:
        """Clears cache successfully."""
        with patch(
            "ai_config.adapters.claude.clear_cache",
            return_value=CommandResult(success=True, stdout="", stderr="", returncode=0),
        ):
            result = runner.invoke(main, ["cache", "clear"])

            assert result.exit_code == 0
            assert "cleared successfully" in result.output

    def test_cache_clear_failure(self, runner: CliRunner) -> None:
        """Handles cache clear failure."""
        with patch(
            "ai_config.adapters.claude.clear_cache",
            return_value=CommandResult(
                success=False, stdout="", stderr="Permission denied", returncode=1
            ),
        ):
            result = runner.invoke(main, ["cache", "clear"])

            assert result.exit_code == 1
            assert "Permission denied" in result.output


class TestPluginCommand:
    """Tests for plugin commands."""

    def test_plugin_create(self, runner: CliRunner, tmp_path: Path) -> None:
        """Creates plugin scaffold."""
        result = runner.invoke(main, ["plugin", "create", "test-plugin", "--path", str(tmp_path)])

        assert result.exit_code == 0
        assert "Created plugin scaffold" in result.output
        assert (tmp_path / "test-plugin" / "manifest.yaml").exists()
