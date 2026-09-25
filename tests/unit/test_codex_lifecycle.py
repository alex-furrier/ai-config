"""Tests for declarative Codex package lifecycle ownership."""

from __future__ import annotations

import json
import os
import signal
import stat
import time
from pathlib import Path

import pytest

from ai_config.adapters.codex import (
    CodexCLI,
    CodexCommandError,
    CodexInstalledPlugin,
    CodexMarketplace,
    CodexPluginInstall,
)
from ai_config.codex_lifecycle import (
    CodexLifecycleExecutionError,
    sync_codex_packages,
    validate_codex_transitions,
)
from ai_config.converters.codex_package import CODEX_OWNERSHIP_FILE, codex_package_spec


def test_expected_plan_drift_fails_before_codex_mutation(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    cli = FakeCodexCLI()
    expected = tuple(
        sync_codex_packages(
            [spec],
            output_dir=tmp_path,
            refreshed_plugin_ids={spec.plugin_id},
            dry_run=True,
            cli=cli,
        )
    )
    cli.marketplaces = [_marketplace(spec)]
    cli.plugins = [_installed(spec)]

    with pytest.raises(ValueError, match="preconditions changed"):
        sync_codex_packages(
            [spec],
            output_dir=tmp_path,
            refreshed_plugin_ids={spec.plugin_id},
            cli=cli,
            expected_actions=expected,
        )

    assert cli.calls == []


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.02)
    return not _pid_exists(pid)


class FakeCodexCLI:
    """Stateful typed fake exposing the lifecycle adapter protocol."""

    def __init__(
        self,
        *,
        marketplaces: list[CodexMarketplace] | None = None,
        plugins: list[CodexInstalledPlugin] | None = None,
        install_version: str = "1.0.0",
    ) -> None:
        self.marketplaces = marketplaces or []
        self.plugins = plugins or []
        self.install_version = install_version
        self.calls: list[tuple[str, ...]] = []

    def list_marketplaces(self) -> list[CodexMarketplace]:
        return self.marketplaces

    def list_plugins(self) -> list[CodexInstalledPlugin]:
        return self.plugins

    def add_marketplace(self, path: str, expected_name: str) -> CodexMarketplace:
        self.calls.append(("add_marketplace", path, expected_name))
        return CodexMarketplace(expected_name, Path(path).resolve(), source_type="local")

    def remove_marketplace(self, name: str) -> None:
        self.calls.append(("remove_marketplace", name))

    def add_plugin(self, plugin_id: str) -> CodexPluginInstall:
        self.calls.append(("add_plugin", plugin_id))
        name, marketplace = plugin_id.split("@", 1)
        return CodexPluginInstall(
            plugin_id=plugin_id,
            name=name,
            marketplace_name=marketplace,
            version=self.install_version,
            installed_path=Path("/installed"),
        )

    def remove_plugin(self, plugin_id: str) -> None:
        self.calls.append(("remove_plugin", plugin_id))


class FailingCodexCLI(FakeCodexCLI):
    """Fail one named mutation after recording the attempted call."""

    def __init__(self, fail_at: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_at = fail_at

    def remove_marketplace(self, name: str) -> None:
        super().remove_marketplace(name)
        if self.fail_at == "remove_marketplace":
            raise OSError("marketplace cleanup failed")


def _package(
    tmp_path: Path,
    name: str = "demo",
    version: str = "1.0.0",
    *,
    source_plugin_id: str | None = None,
):
    spec = codex_package_spec(
        name,
        version,
        tmp_path,
        source_plugin_id=source_plugin_id,
    )
    manifest = spec.marketplace_path / ".agents/plugins/marketplace.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("{}")
    return spec


def _marketplace(spec) -> CodexMarketplace:
    return CodexMarketplace(spec.marketplace_name, spec.marketplace_path, source_type="local")


def _installed(spec, *, enabled: bool = True, version: str | None = None) -> CodexInstalledPlugin:
    source = spec.marketplace_path / "plugins" / spec.plugin_name
    return CodexInstalledPlugin(
        plugin_id=spec.plugin_id,
        name=spec.plugin_name,
        marketplace_name=spec.marketplace_name,
        version=version or spec.version,
        enabled=enabled,
        source_path=source,
        marketplace_root=spec.marketplace_path,
    )


def _establish_ownership(spec, tmp_path: Path) -> None:
    sync_codex_packages(
        [spec],
        output_dir=tmp_path,
        refreshed_plugin_ids={spec.plugin_id},
        cli=FakeCodexCLI(install_version=spec.version),
    )


def test_first_sync_registers_and_installs(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    cli = FakeCodexCLI()

    actions = sync_codex_packages(
        [spec],
        output_dir=tmp_path,
        refreshed_plugin_ids={spec.plugin_id},
        cli=cli,
    )

    assert [action.action for action in actions] == [
        "register_codex_marketplace",
        "install_codex_plugin",
    ]
    assert cli.calls == [
        ("add_marketplace", str(spec.marketplace_path), spec.marketplace_name),
        ("add_plugin", spec.plugin_id),
    ]
    ownership = json.loads((tmp_path / CODEX_OWNERSHIP_FILE).read_text())
    assert set(ownership["packages"]) == {spec.plugin_id}


def test_unchanged_sync_reports_noop_with_reason(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    cli = FakeCodexCLI(marketplaces=[_marketplace(spec)], plugins=[_installed(spec)])

    actions = sync_codex_packages([spec], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=cli)

    assert [action.action for action in actions] == ["noop_codex_plugin"]
    assert "already match" in actions[0].reason
    assert cli.calls == []


def test_refresh_reinstalls_through_codex_cli(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    cli = FakeCodexCLI(marketplaces=[_marketplace(spec)], plugins=[_installed(spec)])

    actions = sync_codex_packages(
        [spec], output_dir=tmp_path, refreshed_plugin_ids={spec.plugin_id}, cli=cli
    )

    assert [action.action for action in actions] == ["update_codex_plugin"]
    assert cli.calls == [("remove_plugin", spec.plugin_id), ("add_plugin", spec.plugin_id)]


def test_drifted_disabled_plugin_reports_reinstall(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    cli = FakeCodexCLI(
        marketplaces=[_marketplace(spec)],
        plugins=[_installed(spec, enabled=False)],
    )

    actions = sync_codex_packages(
        [spec], output_dir=tmp_path, refreshed_plugin_ids=set(), dry_run=True, cli=cli
    )

    assert [action.action for action in actions] == ["reinstall_codex_plugin"]
    assert "disabled" in actions[0].reason
    assert cli.calls == []


def test_missing_runtime_state_reports_register_and_install(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)

    actions = sync_codex_packages(
        [spec],
        output_dir=tmp_path,
        refreshed_plugin_ids=set(),
        dry_run=True,
        cli=FakeCodexCLI(),
    )

    assert [action.action for action in actions] == [
        "register_codex_marketplace",
        "install_codex_plugin",
    ]


def test_version_upgrade_reports_update_and_downgrade_fails_closed(tmp_path: Path) -> None:
    old = _package(tmp_path, version="1.0.0")
    _establish_ownership(old, tmp_path)
    upgraded = codex_package_spec("demo", "1.1.0", tmp_path)
    cli = FakeCodexCLI(
        marketplaces=[_marketplace(upgraded)],
        plugins=[_installed(upgraded, version="1.0.0")],
        install_version="1.1.0",
    )

    actions = sync_codex_packages(
        [upgraded], output_dir=tmp_path, refreshed_plugin_ids=set(), dry_run=True, cli=cli
    )
    assert [action.action for action in actions] == ["update_codex_plugin"]

    downgraded = codex_package_spec("demo", "0.9.0", tmp_path)
    with pytest.raises(ValueError, match="would downgrade"):
        sync_codex_packages(
            [downgraded],
            output_dir=tmp_path,
            refreshed_plugin_ids=set(),
            dry_run=True,
            cli=cli,
        )


def test_prerelease_to_release_transition_is_an_upgrade(tmp_path: Path) -> None:
    prerelease = _package(tmp_path, version="1.0.0-rc.1")
    _establish_ownership(prerelease, tmp_path)
    release = codex_package_spec("demo", "1.0.0", tmp_path)
    cli = FakeCodexCLI(
        marketplaces=[_marketplace(release)],
        plugins=[_installed(release, version="1.0.0-rc.1")],
        install_version="1.0.0",
    )

    actions = sync_codex_packages(
        [release], output_dir=tmp_path, refreshed_plugin_ids=set(), dry_run=True, cli=cli
    )

    assert [action.action for action in actions] == ["update_codex_plugin"]


def test_cross_output_downgrade_fails_before_migration(tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    old = _package(old_root, version="2.0.0")
    _establish_ownership(old, old_root)
    downgraded = codex_package_spec("demo", "1.9.0", tmp_path / "new")

    with pytest.raises(ValueError, match="would downgrade"):
        validate_codex_transitions([downgraded], [old_root])

    assert old.marketplace_path.is_dir()
    assert (old_root / CODEX_OWNERSHIP_FILE).is_file()


def test_normalized_identity_collision_fails_before_cli(tmp_path: Path) -> None:
    first = _package(tmp_path, source_plugin_id="My Plugin@one")
    duplicate = codex_package_spec("demo", "1.0.0", tmp_path, source_plugin_id="demo@two")
    cli = FakeCodexCLI()

    with pytest.raises(ValueError, match="identity collision"):
        sync_codex_packages(
            [first, duplicate],
            output_dir=tmp_path,
            refreshed_plugin_ids=set(),
            cli=cli,
        )
    assert cli.calls == []


def test_temporarily_unavailable_source_retains_owned_state(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    cli = FakeCodexCLI(marketplaces=[_marketplace(spec)], plugins=[_installed(spec)])

    actions = sync_codex_packages(
        [],
        output_dir=tmp_path,
        refreshed_plugin_ids=set(),
        retained_plugin_ids={spec.plugin_id},
        cli=cli,
    )

    assert actions == []
    assert cli.calls == []
    ownership = json.loads((tmp_path / CODEX_OWNERSHIP_FILE).read_text())
    assert set(ownership["packages"]) == {spec.plugin_id}
    assert spec.marketplace_path.is_dir()


def test_incomplete_marketplace_removal_confirmation_retains_generated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    responses = iter(
        [
            {
                "marketplaces": [
                    {
                        "name": spec.marketplace_name,
                        "root": str(spec.marketplace_path),
                        "marketplaceSource": {
                            "sourceType": "local",
                            "source": str(spec.marketplace_path),
                        },
                    }
                ]
            },
            {"installed": [], "available": []},
            {"marketplaceName": spec.marketplace_name},
        ]
    )
    monkeypatch.setattr(cli, "run_json", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(CodexLifecycleExecutionError) as caught:
        sync_codex_packages([], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=cli)

    assert caught.value.failed_action is not None
    assert caught.value.failed_action.action == "remove_codex_marketplace"
    ownership = json.loads((tmp_path / CODEX_OWNERSHIP_FILE).read_text())
    assert set(ownership["packages"]) == {spec.plugin_id}
    assert (spec.marketplace_path / ".agents/plugins/marketplace.json").is_file(), (
        "generated marketplace must remain retryable"
    )


def test_partial_cleanup_failure_reports_progress_and_retains_ownership(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    cli = FailingCodexCLI(
        "remove_marketplace",
        marketplaces=[_marketplace(spec)],
        plugins=[_installed(spec)],
    )

    with pytest.raises(CodexLifecycleExecutionError) as caught:
        sync_codex_packages([], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=cli)

    error = caught.value
    assert [action.action for action in error.planned_actions] == [
        "remove_codex_plugin",
        "remove_codex_marketplace",
        "remove_codex_package",
    ]
    assert [action.action for action in error.completed_actions] == ["remove_codex_plugin"]
    assert error.failed_action.action == "remove_codex_marketplace"
    ownership = json.loads((tmp_path / CODEX_OWNERSHIP_FILE).read_text())
    assert set(ownership["packages"]) == {spec.plugin_id}
    assert spec.marketplace_path.is_dir()

    retry = FakeCodexCLI(marketplaces=[_marketplace(spec)])
    actions = sync_codex_packages([], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=retry)
    assert [action.action for action in actions] == [
        "remove_codex_marketplace",
        "remove_codex_package",
    ]
    assert not spec.marketplace_path.exists()
    assert not (tmp_path / CODEX_OWNERSHIP_FILE).exists()


def test_symlinked_marketplace_ancestor_cannot_redirect_lifecycle_cleanup(
    tmp_path: Path,
) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    owned_path = spec.marketplace_path
    marketplaces = owned_path.parent
    external = tmp_path / "external"
    external.mkdir()
    external_package = external / spec.marketplace_name
    owned_path.rename(external_package)
    marketplaces.rmdir()
    marketplaces.symlink_to(external, target_is_directory=True)
    sentinel = external_package / "sentinel"
    sentinel.write_text("outside")
    cli = FakeCodexCLI(
        marketplaces=[
            CodexMarketplace(
                spec.marketplace_name,
                external_package.resolve(),
                source_type="local",
            )
        ]
    )

    with pytest.raises(ValueError, match="symlink"):
        sync_codex_packages([], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=cli)

    assert cli.calls == []
    assert sentinel.read_text() == "outside"
    assert (tmp_path / CODEX_OWNERSHIP_FILE).is_file()

    marketplaces.unlink()
    marketplaces.mkdir()
    external_package.rename(owned_path)
    retry = FakeCodexCLI(marketplaces=[_marketplace(spec)])
    actions = sync_codex_packages([], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=retry)

    assert [action.action for action in actions] == [
        "remove_codex_marketplace",
        "remove_codex_package",
    ]
    assert not (tmp_path / CODEX_OWNERSHIP_FILE).exists()


def test_filesystem_cleanup_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)

    def fail_cleanup(_path: Path) -> None:
        raise OSError("generated package cleanup failed")

    monkeypatch.setattr("ai_config.codex_lifecycle.shutil.rmtree", fail_cleanup)
    with pytest.raises(CodexLifecycleExecutionError) as caught:
        sync_codex_packages([], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=FakeCodexCLI())

    assert caught.value.failed_action.action == "remove_codex_package"
    ownership = json.loads((tmp_path / CODEX_OWNERSHIP_FILE).read_text())
    assert set(ownership["packages"]) == {spec.plugin_id}


def test_ownership_checkpoint_failure_is_reported_as_failed_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    cli = FakeCodexCLI(marketplaces=[_marketplace(spec)], plugins=[_installed(spec)])

    def fail_ownership(*_args, **_kwargs) -> None:
        raise OSError("ownership checkpoint failed")

    monkeypatch.setattr("ai_config.codex_lifecycle._write_ownership", fail_ownership)
    with pytest.raises(CodexLifecycleExecutionError) as caught:
        sync_codex_packages([spec], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=cli)

    assert caught.value.failed_action is not None
    assert caught.value.failed_action.action == "write_codex_ownership"
    assert [action.action for action in caught.value.completed_actions] == ["noop_codex_plugin"]


def test_removal_touches_only_owned_state(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    _establish_ownership(spec, tmp_path)
    unrelated = tmp_path / ".codex/config.toml"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('model = "keep"\n')
    user_root = Path("/user/market").resolve()
    cli = FakeCodexCLI(
        marketplaces=[
            _marketplace(spec),
            CodexMarketplace("user-market", user_root, source_type="local"),
        ],
        plugins=[
            _installed(spec),
            CodexInstalledPlugin(
                plugin_id="user@user-market",
                name="user",
                marketplace_name="user-market",
                version="1.0.0",
                enabled=False,
                source_path=user_root / "plugins/user",
                marketplace_root=None,
            ),
        ],
    )

    actions = sync_codex_packages([], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=cli)

    assert [action.action for action in actions] == [
        "remove_codex_plugin",
        "remove_codex_marketplace",
        "remove_codex_package",
    ]
    assert cli.calls == [
        ("remove_plugin", spec.plugin_id),
        ("remove_marketplace", spec.marketplace_name),
    ]
    assert unrelated.read_text() == 'model = "keep"\n'


def test_sourceless_plugin_collision_fails_before_mutation(tmp_path: Path) -> None:
    spec = _package(tmp_path)
    installed = _installed(spec)
    sourceless_collision = CodexInstalledPlugin(
        plugin_id=installed.plugin_id,
        name=installed.name,
        marketplace_name=installed.marketplace_name,
        version=installed.version,
        enabled=installed.enabled,
        source_path=installed.source_path,
        marketplace_root=None,
    )
    cli = FakeCodexCLI(
        marketplaces=[_marketplace(spec)],
        plugins=[sourceless_collision],
    )

    with pytest.raises(ValueError, match="identity collision"):
        sync_codex_packages(
            [spec],
            output_dir=tmp_path,
            refreshed_plugin_ids={spec.plugin_id},
            cli=cli,
        )

    assert cli.calls == []


@pytest.mark.parametrize("owned", [False, True])
def test_sourceless_marketplace_collision_fails_before_mutation(
    tmp_path: Path, owned: bool
) -> None:
    spec = _package(tmp_path)
    desired = [spec]
    if owned:
        _establish_ownership(spec, tmp_path)
        desired = []
    cli = FakeCodexCLI(
        marketplaces=[
            CodexMarketplace(
                name=spec.marketplace_name,
                root=spec.marketplace_path,
                source_type=None,
            )
        ]
    )

    with pytest.raises(ValueError, match="marketplace (name collision|ownership changed)"):
        sync_codex_packages(
            desired,
            output_dir=tmp_path,
            refreshed_plugin_ids={spec.plugin_id} if desired else set(),
            cli=cli,
        )

    assert cli.calls == []


def test_validated_output_migration_plans_replacement_state(tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old = _package(old_root)
    _establish_ownership(old, old_root)
    new = _package(new_root)
    cli = FakeCodexCLI(marketplaces=[_marketplace(old)], plugins=[_installed(old)])

    actions = sync_codex_packages(
        [new],
        output_dir=new_root,
        refreshed_plugin_ids={new.plugin_id},
        dry_run=True,
        ignored_runtime_plugin_ids={old.plugin_id},
        ignored_runtime_marketplace_names={old.marketplace_name},
        cli=cli,
    )

    assert [action.action for action in actions] == [
        "register_codex_marketplace",
        "install_codex_plugin",
    ]
    with pytest.raises(ValueError, match="only during a validated dry-run migration"):
        sync_codex_packages(
            [new],
            output_dir=new_root,
            refreshed_plugin_ids={new.plugin_id},
            ignored_runtime_plugin_ids={old.plugin_id},
            ignored_runtime_marketplace_names={old.marketplace_name},
            cli=cli,
        )


@pytest.mark.parametrize("root", [Path("/unrelated"), Path("/unknown")])
def test_marketplace_name_collision_fails_closed(tmp_path: Path, root: Path) -> None:
    spec = _package(tmp_path)
    cli = FakeCodexCLI(
        marketplaces=[CodexMarketplace(spec.marketplace_name, root, source_type="local")]
    )
    with pytest.raises(ValueError, match="will not modify"):
        sync_codex_packages(
            [spec], output_dir=tmp_path, refreshed_plugin_ids={spec.plugin_id}, cli=cli
        )
    assert cli.calls == []


def test_ambiguous_ownership_json_fails_before_cli(tmp_path: Path) -> None:
    ownership = tmp_path / CODEX_OWNERSHIP_FILE
    ownership.parent.mkdir(parents=True)
    ownership.write_text(
        '{"version":1,"packages":{"demo@ai-config-demo":'
        '{"marketplace_name":"ai-config-demo","marketplace_path":"/tmp/ai-config-demo",'
        '"version":"1.0.0"},"demo@ai-config-demo":'
        '{"marketplace_name":"ai-config-demo","marketplace_path":"/tmp/ai-config-demo",'
        '"version":"1.0.0"}}}'
    )
    cli = FakeCodexCLI()

    with pytest.raises(ValueError, match="duplicate key"):
        sync_codex_packages([], output_dir=tmp_path, refreshed_plugin_ids=set(), cli=cli)
    assert cli.calls == []


def test_invalid_semver_fails_at_package_boundary(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Semantic Versioning"):
        codex_package_spec("demo", "1.0", tmp_path)


def test_cli_list_schema_rejects_partial_duplicate_and_inconsistent_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")

    monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: {"installed": []})
    with pytest.raises(CodexCommandError, match="installed and available"):
        cli.list_plugins()

    duplicate = {
        "installed": [
            {
                "pluginId": "demo@market",
                "name": "demo",
                "marketplaceName": "market",
                "version": "1.0.0",
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": "/market/plugins/demo"},
                "marketplaceSource": {"sourceType": "local", "source": "/market"},
                "installPolicy": "AVAILABLE",
                "authPolicy": "ON_INSTALL",
            }
        ]
        * 2,
        "available": [],
    }
    monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: duplicate)
    with pytest.raises(CodexCommandError, match="duplicate installed plugin"):
        cli.list_plugins()

    duplicate["installed"] = [{**duplicate["installed"][0], "pluginId": "wrong@market"}]
    monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: duplicate)
    with pytest.raises(CodexCommandError, match="identity fields disagree"):
        cli.list_plugins()


def test_cli_available_rows_require_complete_typed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    captured_args: list[str] = []
    available = {
        "pluginId": "demo@market",
        "name": "demo",
        "marketplaceName": "market",
        "version": "1.0.0",
        "installed": False,
        "enabled": False,
        "source": {"source": "local", "path": "/market/plugins/demo"},
        "marketplaceSource": {"sourceType": "local", "source": "/market"},
        "installPolicy": "AVAILABLE",
        "authPolicy": "ON_INSTALL",
    }

    def valid_response(_stage, args, **_kwargs):
        captured_args.extend(args)
        return {"installed": [], "available": [available]}

    monkeypatch.setattr(cli, "run_json", valid_response)
    assert cli.list_plugins() == []
    assert "--available" in captured_args

    monkeypatch.setattr(
        cli,
        "run_json",
        lambda *_args, **_kwargs: {"installed": [], "available": [available, available]},
    )
    with pytest.raises(CodexCommandError, match="duplicate available plugin"):
        cli.list_plugins()

    for mutation in (
        {"pluginId": "other@market"},
        {"version": "latest"},
        {"installed": True},
        {"enabled": "false"},
        {"source": {"source": "local"}},
    ):
        malformed = {**available, **mutation}
        monkeypatch.setattr(
            cli,
            "run_json",
            lambda *_args, malformed=malformed, **_kwargs: {
                "installed": [],
                "available": [malformed],
            },
        )
        with pytest.raises(CodexCommandError, match=r"available\[0\]"):
            cli.list_plugins()


def test_cli_accepts_remote_available_source_id_and_rejects_missing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.153.3")
    available = {
        "pluginId": "demo@openai-curated-remote",
        "name": "demo",
        "marketplaceName": "openai-curated-remote",
        "version": "1.0.0",
        "installed": False,
        "enabled": False,
        "source": {"source": "remote", "id": "plugin_remote_123"},
        "installPolicy": "AVAILABLE",
        "authPolicy": "ON_INSTALL",
    }
    monkeypatch.setattr(
        cli,
        "run_json",
        lambda *args, **kwargs: {"installed": [], "available": [available]},
    )

    assert cli.list_plugins() == []

    duplicate = {
        **available,
        "version": "2.0.0",
        "source": {"source": "remote", "id": "plugin_remote_456"},
    }
    monkeypatch.setattr(
        cli,
        "run_json",
        lambda *args, **kwargs: {"installed": [], "available": [available, duplicate]},
    )
    assert cli.list_plugins() == []

    malformed = {**available, "source": {"source": "remote", "id": ""}}
    monkeypatch.setattr(
        cli,
        "run_json",
        lambda *args, **kwargs: {"installed": [], "available": [malformed]},
    )
    with pytest.raises(CodexCommandError, match=r"source\.id or \.url"):
        cli.list_plugins()


def test_cli_accepts_absent_marketplace_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    responses = iter(
        [
            {
                "marketplaces": [
                    {
                        "name": "openai-curated",
                        "root": "/cache/openai-curated",
                    }
                ]
            },
            {
                "installed": [
                    {
                        "pluginId": "installed@openai-curated",
                        "name": "installed",
                        "marketplaceName": "openai-curated",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                        "source": {
                            "source": "local",
                            "path": "/cache/openai-curated/plugins/installed",
                        },
                        "installPolicy": "AVAILABLE",
                        "authPolicy": "ON_INSTALL",
                    }
                ],
                "available": [
                    {
                        "pluginId": "available@openai-curated",
                        "name": "available",
                        "marketplaceName": "openai-curated",
                        "version": "2.0.0",
                        "installed": False,
                        "enabled": False,
                        "source": {
                            "source": "local",
                            "path": "/cache/openai-curated/plugins/available",
                        },
                        "installPolicy": "AVAILABLE",
                        "authPolicy": "ON_INSTALL",
                    }
                ],
            },
        ]
    )
    monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: next(responses))

    marketplace = cli.list_marketplaces()[0]
    installed = cli.list_plugins()[0]

    assert marketplace == CodexMarketplace(
        name="openai-curated",
        root=Path("/cache/openai-curated"),
        source_type=None,
    )
    assert installed.source_path == Path("/cache/openai-curated/plugins/installed")
    assert installed.marketplace_root is None


@pytest.mark.parametrize(
    "marketplace_source",
    [
        None,
        [],
        "local",
        {},
        {"sourceType": "unknown", "source": "/market"},
        {"sourceType": "local", "source": ""},
        {"sourceType": "local", "source": "/other-market"},
    ],
)
@pytest.mark.parametrize("row_kind", ["marketplace", "available", "installed"])
def test_cli_rejects_present_malformed_marketplace_source(
    monkeypatch: pytest.MonkeyPatch,
    marketplace_source: object,
    row_kind: str,
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    plugin_row = {
        "pluginId": "demo@market",
        "name": "demo",
        "marketplaceName": "market",
        "version": "1.0.0",
        "installed": row_kind == "installed",
        "enabled": row_kind == "installed",
        "source": {"source": "local", "path": "/market/plugins/demo"},
        "marketplaceSource": marketplace_source,
        "installPolicy": "AVAILABLE",
        "authPolicy": "ON_INSTALL",
    }
    if row_kind == "marketplace":
        payload = {
            "marketplaces": [
                {
                    "name": "market",
                    "root": "/market",
                    "marketplaceSource": marketplace_source,
                }
            ]
        }
        monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: payload)
        operation = cli.list_marketplaces
    else:
        payload = {
            "installed": [plugin_row] if row_kind == "installed" else [],
            "available": [plugin_row] if row_kind == "available" else [],
        }
        monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: payload)
        operation = cli.list_plugins

    with pytest.raises(CodexCommandError, match="marketplaceSource"):
        operation()


def test_cli_preserves_typed_nonlocal_unrelated_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented Git/remote rows are validated without being mistaken for owned local state."""
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    responses = iter(
        [
            {
                "marketplaces": [
                    {
                        "name": "git-market",
                        "root": "/cache/git-market",
                        "marketplaceSource": {
                            "sourceType": "git",
                            "source": "https://example.test/market.git",
                        },
                    }
                ]
            },
            {
                "installed": [
                    {
                        "pluginId": "remote-plugin@git-market",
                        "name": "remote-plugin",
                        "marketplaceName": "git-market",
                        "version": "2.0.0",
                        "installed": True,
                        "enabled": False,
                        "source": {
                            "source": "git",
                            "url": "https://example.test/plugin.git",
                        },
                        "marketplaceSource": {
                            "sourceType": "git",
                            "source": "https://example.test/market.git",
                        },
                        "installPolicy": "AVAILABLE",
                        "authPolicy": "ON_INSTALL",
                    }
                ],
                "available": [],
            },
        ]
    )
    monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: next(responses))

    assert cli.list_marketplaces()[0].name == "git-market"
    installed = cli.list_plugins()[0]
    assert installed.plugin_id == "remote-plugin@git-market"
    assert installed.source_path is None
    assert installed.marketplace_root is None


@pytest.mark.parametrize(
    "payload",
    [
        {"installed": [], "available": []},
        {"marketplaces": [{"name": "demo"}]},
    ],
)
def test_cli_marketplace_list_schema_rejects_wrong_shape(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: payload)

    with pytest.raises(CodexCommandError, match="invalid supported Codex JSON response"):
        cli.list_marketplaces()


def test_cli_json_rejects_malformed_and_duplicate_keys(tmp_path: Path) -> None:
    for index, payload in enumerate(('{"marketplaces":', '{"marketplaces":[],"marketplaces":[]}')):
        executable = tmp_path / f"codex-json-{index}"
        executable.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo \'codex-cli 0.144.5\'; exit 0; fi\n'
            f"printf '%s' '{payload}'\n"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(CodexCommandError, match="unambiguous JSON"):
            CodexCLI(str(executable)).list_marketplaces()


@pytest.mark.parametrize(
    "payload",
    [
        {"marketplaceName": "market"},
        {"marketplaceName": "market", "installedRoot": "/still/installed"},
        {"marketplaceName": "other", "installedRoot": None},
        {"marketplaceName": "market", "installedRoot": None, "alreadyAdded": False},
    ],
)
def test_cli_marketplace_removal_requires_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    monkeypatch.setattr(cli, "run_json", lambda *args, **kwargs: payload)

    with pytest.raises(CodexCommandError, match="did not confirm removal"):
        cli.remove_marketplace("market")


def test_cli_marketplace_removal_accepts_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    monkeypatch.setattr(
        cli,
        "run_json",
        lambda *args, **kwargs: {"marketplaceName": "market", "installedRoot": None},
    )

    cli.remove_marketplace("market")


def test_cli_marketplace_removal_rejects_duplicate_confirmation_key(tmp_path: Path) -> None:
    executable = tmp_path / "codex-duplicate-removal"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo \'codex-cli 0.144.5\'; exit 0; fi\n'
        "printf '%s' "
        '\'{"marketplaceName":"market","installedRoot":null,"installedRoot":null}\'\n'
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(CodexCommandError, match="duplicate JSON key"):
        CodexCLI(str(executable)).remove_marketplace("market")


def test_cli_mutation_schema_rejects_semantically_wrong_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = CodexCLI("/bin/codex")
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    monkeypatch.setattr(
        cli,
        "run_json",
        lambda *args, **kwargs: {
            "pluginId": "other@market",
            "name": "other",
            "marketplaceName": "market",
            "version": "unknown",
            "installedPath": "/tmp/plugin",
            "authPolicy": "ON_INSTALL",
        },
    )
    with pytest.raises(CodexCommandError, match="did not match"):
        cli.add_plugin("demo@market")

    monkeypatch.setattr(
        cli,
        "run_json",
        lambda *args, **kwargs: {
            "pluginId": "demo@market",
            "name": "demo",
            "marketplaceName": "market",
            "version": "unknown",
            "installedPath": "/tmp/plugin",
            "authPolicy": "ON_INSTALL",
        },
    )
    with pytest.raises(CodexCommandError, match="Semantic Versioning"):
        cli.add_plugin("demo@market")

    monkeypatch.setattr(
        cli,
        "run_json",
        lambda *args, **kwargs: {
            "pluginId": "other@market",
            "name": "other",
            "marketplaceName": "market",
        },
    )
    with pytest.raises(CodexCommandError, match="did not confirm removal"):
        cli.remove_plugin("demo@market")


@pytest.mark.parametrize(
    "version",
    [
        "0.144.5",
        "0.145.0",
        "0.146.0",
        "0.147.0",
        "0.148.0",
        "0.149.0",
        "0.153.3",
        "0.156.1",
        "0.157.0",
    ],
)
def test_cli_supported_versions_accept_observed_contract(tmp_path: Path, version: str) -> None:
    executable = tmp_path / f"codex-version-{version}"
    executable.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo \'codex-cli {version}\'; exit 0; fi\n'
        "printf '%s' '{\"marketplaces\":[]}'\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    assert CodexCLI(str(executable)).list_marketplaces() == []


@pytest.mark.parametrize(
    "version", ["0.150.0", "0.151.0", "0.152.0", "0.154.0", "0.155.0", "0.158.0"]
)
def test_cli_unknown_version_fails_closed(tmp_path: Path, version: str) -> None:
    executable = tmp_path / "codex-version"
    executable.write_text(f"#!/bin/sh\necho 'codex-cli {version}'\n")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(CodexCommandError, match="unsupported Codex CLI response contract"):
        CodexCLI(str(executable)).list_marketplaces()


def test_cli_failure_sanitizes_and_bounds_output(tmp_path: Path) -> None:
    executable = tmp_path / "codex-fail"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo \'codex-cli 0.144.5\'; exit 0; fi\n'
        "printf '\\033[31mbroken\\033[0m\\000' >&2\n"
        "exit 7\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(CodexCommandError) as caught:
        CodexCLI(str(executable)).list_marketplaces()
    message = str(caught.value)
    assert "stage 'list-marketplaces'" in message
    assert "\x1b" not in message
    assert "broken" in message
    assert "Remediation:" in message


@pytest.mark.skipif(os.name != "posix", reason="process-group descendant test requires POSIX")
def test_cli_timeout_kills_forked_descendant_after_parent_closes_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descendant_pid_path = tmp_path / "descendant.pid"
    descendant_code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(descendant_pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    executable = tmp_path / "codex-timeout.py"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, subprocess, sys, time\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli 0.144.5')\n"
        "    raise SystemExit(0)\n"
        f"descendant_code = {descendant_code!r}\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', descendant_code],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        "    close_fds=True,\n"
        "    preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN),\n"
        ")\n"
        f"open({str(descendant_pid_path)!r}, 'w').write(str(descendant.pid))\n"
        "time.sleep(30)\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    cli = CodexCLI(str(executable), timeout_seconds=0.5)
    monkeypatch.setattr(cli, "_ensure_supported_version", lambda: "0.144.5")
    descendant_pid: int | None = None
    try:
        with pytest.raises(CodexCommandError, match="timed out"):
            cli.list_marketplaces()
        deadline = time.monotonic() + 1.0
        while not descendant_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert descendant_pid_path.exists(), "hostile descendant did not start"
        descendant_pid = int(descendant_pid_path.read_text())
        assert _wait_for_pid_exit(descendant_pid, 1.0), "timed-out descendant survived cleanup"
    finally:
        if descendant_pid is not None and _pid_exists(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)
