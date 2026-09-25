"""Plain-data characterization tests for sync planning."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_config.adapters.claude import CommandResult, InstalledMarketplace, InstalledPlugin
from ai_config.operations import apply_sync_plan, build_sync_plan, sync_target
from ai_config.sync_orchestration import _source_batch
from ai_config.sync_pipeline import (
    CacheOwnershipSnapshot,
    DesiredState,
    EmittedArtifact,
    EmittedArtifactBatch,
    EmittedTargetBatch,
    ObservedMarketplace,
    ObservedPlugin,
    OwnershipSnapshot,
    PlannedAction,
    RuntimeSnapshot,
    SourceBatch,
    plan_sync,
    validate_sync_plan,
)
from ai_config.types import (
    ClaudeTargetConfig,
    ConversionConfig,
    MarketplaceConfig,
    PluginConfig,
    PluginSource,
    SyncAction,
    TargetConfig,
)


@pytest.mark.parametrize(
    ("plugins", "observed", "expected"),
    [
        ((PluginConfig("new@market"),), (), ("install",)),
        (
            (PluginConfig("demo@market"),),
            (ObservedPlugin("demo@market", "1.0.0", "user", False, "/demo"),),
            ("enable",),
        ),
        (
            (PluginConfig("demo@market", enabled=False),),
            (ObservedPlugin("demo@market", "1.0.0", "user", True, "/demo"),),
            ("disable",),
        ),
        (
            (PluginConfig("demo@market"),),
            (ObservedPlugin("demo@market", "1.0.0", "user", True, "/demo"),),
            (),
        ),
    ],
)
def test_plugin_decisions_are_plain_data_and_table_driven(
    plugins: tuple[PluginConfig, ...],
    observed: tuple[ObservedPlugin, ...],
    expected: tuple[str, ...],
) -> None:
    plan = plan_sync(
        DesiredState(plugins=plugins), RuntimeSnapshot(plugins=observed), SourceBatch()
    )

    assert tuple(item.action.action for item in plan.actions) == expected


def test_marketplaces_precede_plugins_and_conversion_actions() -> None:
    desired = DesiredState(
        marketplaces=(("market", MarketplaceConfig(PluginSource.GITHUB, repo="owner/repo")),),
        plugins=(PluginConfig("demo@market"),),
    )
    conversion = SyncAction("create_pi_output", ".pi/skills/demo/SKILL.md", reason="missing")

    plan = plan_sync(
        desired,
        RuntimeSnapshot(),
        SourceBatch(),
        conversion_actions=(conversion,),
    )

    assert tuple(item.phase for item in plan.actions) == (
        "marketplace",
        "plugin",
        "conversion",
    )
    assert tuple(item.action.action for item in plan.actions) == (
        "register_marketplace",
        "install",
        "create_pi_output",
    )


def test_observed_marketplace_and_noop_plugin_produce_no_actions() -> None:
    desired = DesiredState(
        marketplaces=(("market", MarketplaceConfig(PluginSource.LOCAL, path="/market")),),
        plugins=(PluginConfig("demo@market"),),
    )
    runtime = RuntimeSnapshot(
        marketplaces=(ObservedMarketplace("market", "local", "", "/market"),),
        plugins=(ObservedPlugin("demo@market", "1", "user", True, "/demo"),),
    )

    assert plan_sync(desired, runtime, SourceBatch()).actions == ()


def test_blocking_diagnostics_are_materialized_without_discarding_decisions() -> None:
    desired = DesiredState(plugins=(PluginConfig("demo"),))

    plan = plan_sync(
        desired,
        RuntimeSnapshot(diagnostics=("runtime unavailable",)),
        SourceBatch(),
        conversion_diagnostics=("invalid ownership",),
    )

    assert plan.is_blocked
    assert [item.message for item in plan.diagnostics] == [
        "runtime unavailable",
        "invalid ownership",
    ]
    assert [item.action.action for item in plan.actions] == ["install"]


def test_plan_records_are_immutable() -> None:
    plan = plan_sync(DesiredState(), RuntimeSnapshot(), SourceBatch())

    with pytest.raises(FrozenInstanceError):
        plan.actions = ()  # type: ignore[misc]


def test_plan_validation_rejects_unsafe_materialized_artifact_paths() -> None:
    sources = SourceBatch(
        emitted=EmittedArtifactBatch(
            (
                EmittedTargetBatch(
                    "demo",
                    "cursor",
                    "demo",
                    "1.0.0",
                    files=(EmittedArtifact(Path("../outside"), "unsafe"),),
                ),
            )
        )
    )

    plan = plan_sync(DesiredState(), RuntimeSnapshot(), sources)

    assert plan.is_blocked
    assert any("Unsafe emitted artifact path" in item.message for item in plan.diagnostics)
    assert validate_sync_plan(plan)


def test_real_execution_consumes_the_materialized_action_order() -> None:
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(plugins=(PluginConfig("demo"),)),
    )
    command = type("Command", (), {"success": True, "stderr": ""})()
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([], [])),
        patch(
            "ai_config.sync_state.load_conversion_cache",
            return_value={"version": 7, "entries": {}, "codex_output_dirs": []},
        ),
        patch("ai_config.operations.claude.install_plugin", return_value=command) as install,
    ):
        plan = build_sync_plan(target)
        report = apply_sync_plan(plan)

    assert tuple(item.action for item in plan.actions) == report.completed
    install.assert_called_once_with("demo", "user")


def test_runtime_drift_blocks_every_planned_action() -> None:
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(plugins=(PluginConfig("demo"),)),
    )
    cache = {"version": 7, "entries": {}, "codex_output_dirs": []}
    changed = InstalledPlugin("unrelated", "1.0.0", "user", True, "/unrelated")
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch(
            "ai_config.operations.claude.list_installed_plugins",
            side_effect=[([], []), ([changed], [])],
        ),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch("ai_config.operations.claude.install_plugin") as install,
    ):
        plan = build_sync_plan(target)
        report = apply_sync_plan(plan)

    assert report.completed == ()
    assert report.errors == (
        "Claude runtime state changed after sync planning; no action was executed",
    )
    install.assert_not_called()


def test_ownership_drift_blocks_every_planned_action(tmp_path: Path) -> None:
    root = tmp_path / "output"
    ledger = root / ".ai-config/pi-ownership.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("before")
    runtime = RuntimeSnapshot(
        cache_and_ownership=CacheOwnershipSnapshot(
            cache_payload="{}", ownership=(OwnershipSnapshot("pi", root, "before"),)
        )
    )
    plan = plan_sync(DesiredState(), runtime, SourceBatch())
    ledger.write_text("after")

    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([], [])),
        patch("ai_config.sync_state.load_conversion_cache", return_value={}),
    ):
        report = apply_sync_plan(plan)

    assert report.completed == ()
    assert report.errors == ("Ownership state changed after sync planning; no action was executed",)


def test_source_batch_hashes_local_in_use_but_ignores_installed_cache_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-profile"))
    local_marketplace = tmp_path / "marketplace"
    local_plugin = local_marketplace / "demo"
    local_plugin.mkdir(parents=True)
    cached_plugin = tmp_path / "claude-profile/plugins/cache/remote/demo/1.0.0"
    cached_plugin.mkdir(parents=True)
    for plugin_path in (local_plugin, cached_plugin):
        (plugin_path / "source.md").write_text("original")
        (plugin_path / ".in_use").mkdir()
        (plugin_path / ".in_use/12345").write_text("first")

    local_config = ClaudeTargetConfig(
        marketplaces={"local": MarketplaceConfig(PluginSource.LOCAL, path=str(local_marketplace))},
        plugins=(PluginConfig("demo@local"),),
    )
    cached_config = ClaudeTargetConfig(plugins=(PluginConfig("demo"),))
    installed = InstalledPlugin("demo", "1.0.0", "user", True, str(cached_plugin))
    local_before = _source_batch(local_config, (), ()).resolved[0]
    cached_before = _source_batch(cached_config, (installed,), ()).resolved[0]
    assert local_before.provenance == "configured_local"
    assert cached_before.provenance == "installed_plugin"
    (local_plugin / ".in_use/12345").write_text("second")
    (cached_plugin / ".in_use/12345").write_text("second")
    assert _source_batch(local_config, (), ()).resolved[0].digest != local_before.digest
    assert _source_batch(cached_config, (installed,), ()).resolved[0].digest == cached_before.digest


def test_apply_writes_materialized_emitter_artifacts_without_replanning(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text('{"name":"demo","version":"1.0.0"}')
    (source / "skills/demo").mkdir(parents=True)
    (source / "skills/demo/SKILL.md").write_text("---\nname: demo\n---\nBody\n")
    output = tmp_path / "output"
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            plugins=(PluginConfig("demo"),),
            conversion=ConversionConfig(targets=("cursor",), output_dir=str(output)),
        ),
    )
    installed = InstalledPlugin("demo", "1.0.0", "user", True, str(source))
    cache = {
        "version": 7,
        "entries": {},
        "codex_output_dirs": [],
        "pi_output_dirs": [],
    }
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([installed], [])),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch("ai_config.sync_state.save_conversion_cache"),
    ):
        plan = build_sync_plan(target)
        assert plan.sources.emitted.batches
        with (
            patch("ai_config.sync_conversion.convert_plugin") as convert,
            patch("ai_config.sync_conversion.get_emitter") as emitter,
        ):
            report = apply_sync_plan(plan)

    assert report.errors == ()
    planned_files = [file for batch in plan.sources.emitted.batches for file in batch.files]
    assert planned_files
    assert all((output / file.path).is_file() for file in planned_files)
    assert report.checkpoints_committed == tuple(
        checkpoint for checkpoint in plan.checkpoints if checkpoint.kind == "cache"
    )
    convert.assert_not_called()
    emitter.assert_not_called()


def test_unavailable_conversion_source_allows_planned_plugin_install() -> None:
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            plugins=(PluginConfig("demo"),),
            conversion=ConversionConfig(targets=("cursor",)),
        ),
    )
    cache = {"version": 7, "entries": {}, "codex_output_dirs": []}
    command = type("Command", (), {"success": True, "stderr": ""})()
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([], [])),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch("ai_config.operations.claude.install_plugin", return_value=command) as install,
    ):
        plan = build_sync_plan(target)
        report = apply_sync_plan(plan)

    assert not plan.is_blocked
    assert plan.reported_diagnostics
    assert [item.action for item in report.completed] == ["install"]
    assert any("temporarily unavailable" in error for error in report.errors)
    install.assert_called_once_with("demo", "user")


def test_remote_source_converges_after_one_bounded_reobservation(tmp_path: Path) -> None:
    source = tmp_path / "installed-plugin"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text('{"name":"demo","version":"1.0.0"}')
    (source / "skills/demo").mkdir(parents=True)
    (source / "skills/demo/SKILL.md").write_text("---\nname: demo\n---\nBody\n")
    output = tmp_path / "output"
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            marketplaces={"remote": MarketplaceConfig(PluginSource.GITHUB, repo="owner/plugins")},
            plugins=(PluginConfig("demo@remote"),),
            conversion=ConversionConfig(targets=("cursor",), output_dir=str(output)),
        ),
    )
    marketplace = InstalledMarketplace(
        "remote", PluginSource.GITHUB, "owner/plugins", str(tmp_path / "marketplace")
    )
    installed = InstalledPlugin("demo@remote", "1.0.0", "user", True, str(source))
    cache = {"version": 9, "entries": {}, "codex_output_dirs": [], "pi_output_dirs": []}
    command = CommandResult(True, "", "", 0)
    with (
        patch(
            "ai_config.operations.claude.list_installed_marketplaces",
            return_value=([marketplace], []),
        ),
        patch(
            "ai_config.operations.claude.list_installed_plugins",
            side_effect=[([], []), ([], []), ([installed], []), ([installed], [])],
        ),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch("ai_config.operations.claude.install_plugin", return_value=command) as install,
        patch("ai_config.sync_state.save_conversion_cache"),
    ):
        result = sync_target(target)

    assert result.success
    assert [action.action for action in result.actions_taken][0] == "install"
    assert (output / ".cursor/skills/demo-demo/SKILL.md").is_file()
    install.assert_called_once_with("demo@remote", "user")


def test_remote_prerequisite_runs_before_unrelated_conversion_blocker(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "malformed-plugin"
    (malformed / ".claude-plugin").mkdir(parents=True)
    (malformed / ".claude-plugin/plugin.json").write_text("{not-json")
    deferred = tmp_path / "deferred-plugin"
    (deferred / ".claude-plugin").mkdir(parents=True)
    (deferred / ".claude-plugin/plugin.json").write_text('{"name":"deferred","version":"1.0.0"}')
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            marketplaces={"remote": MarketplaceConfig(PluginSource.GITHUB, repo="owner/plugins")},
            plugins=(PluginConfig("malformed@remote"), PluginConfig("deferred@remote")),
            conversion=ConversionConfig(targets=("cursor",), output_dir=str(tmp_path / "output")),
        ),
    )
    marketplace = InstalledMarketplace("remote", PluginSource.GITHUB, "owner/plugins", "")
    malformed_installed = InstalledPlugin("malformed@remote", "1.0.0", "user", True, str(malformed))
    deferred_installed = InstalledPlugin("deferred@remote", "1.0.0", "user", True, str(deferred))
    cache = {"version": 9, "entries": {}, "codex_output_dirs": [], "pi_output_dirs": []}
    command = CommandResult(True, "", "", 0)
    with (
        patch(
            "ai_config.operations.claude.list_installed_marketplaces",
            return_value=([marketplace], []),
        ),
        patch(
            "ai_config.operations.claude.list_installed_plugins",
            side_effect=[
                ([malformed_installed], []),
                ([malformed_installed], []),
                ([malformed_installed, deferred_installed], []),
            ],
        ),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch("ai_config.operations.claude.install_plugin", return_value=command) as install,
    ):
        result = sync_target(target)

    assert not result.success
    assert [action.action for action in result.actions_taken] == ["install"]
    assert any("Conversion failed for malformed@remote" in error for error in result.errors)
    install.assert_called_once_with("deferred@remote", "user")


def test_deferred_remote_dry_run_reports_only_exact_prerequisite_actions(
    tmp_path: Path,
) -> None:
    available = tmp_path / "available"
    (available / ".claude-plugin").mkdir(parents=True)
    (available / ".claude-plugin/plugin.json").write_text('{"name":"available","version":"1.0.0"}')
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            marketplaces={"remote": MarketplaceConfig(PluginSource.GITHUB, repo="owner/plugins")},
            plugins=(PluginConfig("available@remote"), PluginConfig("deferred@remote")),
            conversion=ConversionConfig(targets=("cursor",), output_dir=str(tmp_path / "output")),
        ),
    )
    marketplace = InstalledMarketplace("remote", PluginSource.GITHUB, "owner/plugins", "")
    installed = InstalledPlugin("available@remote", "1.0.0", "user", True, str(available))
    cache = {"version": 9, "entries": {}, "codex_output_dirs": [], "pi_output_dirs": []}
    with (
        patch(
            "ai_config.operations.claude.list_installed_marketplaces",
            return_value=([marketplace], []),
        ),
        patch(
            "ai_config.operations.claude.list_installed_plugins",
            return_value=([installed], []),
        ),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
    ):
        result = sync_target(target, dry_run=True)

    assert not result.success
    assert [action.action for action in result.actions_taken] == ["install"]
    assert result.actions_taken[0].target == "deferred@remote"
    assert any("temporarily unavailable" in error for error in result.errors)


def test_missing_configured_local_source_never_triggers_reobservation(tmp_path: Path) -> None:
    marketplace_root = tmp_path / "local-marketplace"
    marketplace_root.mkdir()
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            marketplaces={
                "local": MarketplaceConfig(PluginSource.LOCAL, path=str(marketplace_root))
            },
            plugins=(PluginConfig("demo@local"),),
            conversion=ConversionConfig(targets=("cursor",), output_dir=str(tmp_path / "output")),
        ),
    )
    marketplace = InstalledMarketplace("local", PluginSource.LOCAL, "", str(marketplace_root))
    command = CommandResult(True, "", "", 0)
    cache = {"version": 9, "entries": {}, "codex_output_dirs": [], "pi_output_dirs": []}
    with (
        patch(
            "ai_config.operations.claude.list_installed_marketplaces",
            return_value=([marketplace], []),
        ),
        patch(
            "ai_config.operations.claude.list_installed_plugins", return_value=([], [])
        ) as observe_plugins,
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch("ai_config.operations.claude.install_plugin", return_value=command) as install,
    ):
        result = sync_target(target)

    assert not result.success
    assert any("temporarily unavailable" in error for error in result.errors)
    assert observe_plugins.call_count == 2
    install.assert_called_once_with("demo@local", "user")


def test_duplicate_configured_selectors_block_before_mutation() -> None:
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            plugins=(PluginConfig("demo"), PluginConfig("demo")),
        ),
    )
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([], [])),
        patch("ai_config.operations.claude.install_plugin") as install,
    ):
        report = apply_sync_plan(build_sync_plan(target))

    assert report.errors == ("Duplicate configured plugin selectors: demo",)
    install.assert_not_called()


def test_cache_checkpoint_failure_is_reported_and_not_committed(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text('{"name":"demo","version":"1.0.0"}')
    output = tmp_path / "output"
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            plugins=(PluginConfig("demo"),),
            conversion=ConversionConfig(targets=("cursor",), output_dir=str(output)),
        ),
    )
    installed = InstalledPlugin("demo", "1.0.0", "user", True, str(source))
    cache = {"version": 7, "entries": {}, "codex_output_dirs": [], "pi_output_dirs": []}
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([installed], [])),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch(
            "ai_config.sync_state.save_conversion_cache",
            side_effect=OSError("checkpoint unavailable"),
        ),
    ):
        report = apply_sync_plan(build_sync_plan(target))

    assert report.completed == ()
    assert report.checkpoints_committed == ()
    assert report.errors == ("Failed to save conversion cache: checkpoint unavailable",)


def test_blocking_observation_diagnostic_prevents_every_planned_mutation() -> None:
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(plugins=(PluginConfig("demo"),)),
    )
    with (
        patch(
            "ai_config.operations.claude.list_installed_marketplaces",
            return_value=([], ["inspection failed"]),
        ),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([], [])),
        patch("ai_config.operations.claude.install_plugin") as install,
    ):
        report = apply_sync_plan(build_sync_plan(target))

    assert report.errors == ("inspection failed",)
    install.assert_not_called()


def test_apply_validates_unmaterialized_plan_before_any_mutation() -> None:
    marketplace = MarketplaceConfig(PluginSource.GITHUB, repo="owner/repo")
    valid = plan_sync(
        DesiredState(
            marketplaces=(("market", marketplace),),
            plugins=(PluginConfig("demo"),),
        ),
        RuntimeSnapshot(),
        SourceBatch(),
    )
    malformed = replace(
        valid,
        actions=(
            PlannedAction(
                "marketplace",
                SyncAction("register_marketplace", "market", reason="missing"),
            ),
            PlannedAction(
                "plugin",
                SyncAction("install", "demo", scope=None, reason="malformed"),
            ),
        ),
        diagnostics=(),
    )

    with (
        patch("ai_config.operations.claude.list_installed_marketplaces") as observe_marketplaces,
        patch("ai_config.operations.claude.list_installed_plugins") as observe_plugins,
        patch("ai_config.operations.claude.add_marketplace") as add_marketplace,
        patch("ai_config.operations.claude.install_plugin") as install,
    ):
        report = apply_sync_plan(malformed)

    assert report.errors == ("Planned install for demo requires an explicit valid scope",)
    assert report.completed == ()
    assert any("explicit valid scope" in item.message for item in report.plan.diagnostics)
    observe_marketplaces.assert_not_called()
    observe_plugins.assert_not_called()
    add_marketplace.assert_not_called()
    install.assert_not_called()


def test_marketplace_postconditions_use_each_immediate_prior_state() -> None:
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            marketplaces={
                "first": MarketplaceConfig(PluginSource.GITHUB, repo="owner/first"),
                "second": MarketplaceConfig(PluginSource.GITHUB, repo="owner/second"),
            }
        ),
    )
    first = InstalledMarketplace("first", PluginSource.GITHUB, "owner/first", "/first")
    alias = InstalledMarketplace("second-alias", PluginSource.GITHUB, "owner/second", "/second")
    marketplace_observations = [
        ([], []),  # planning
        ([], []),  # apply precondition
        ([first], []),  # first postcondition
        ([first, alias], []),  # second postcondition
    ]
    command = CommandResult(True, "", "", 0)
    cache = {"version": 7, "entries": {}, "codex_output_dirs": []}
    with (
        patch(
            "ai_config.operations.claude.list_installed_marketplaces",
            side_effect=marketplace_observations,
        ),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([], [])),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch("ai_config.operations.claude.add_marketplace", return_value=command),
    ):
        report = apply_sync_plan(build_sync_plan(target))

    assert [item.action for item in report.completed] == [
        "register_marketplace",
        "register_marketplace",
    ]
    assert report.errors == (
        "Marketplace registered as 'second-alias' (from marketplace.json), but config uses "
        "'second'. Update your config key from 'second' to 'second-alias' to match.",
    )


def test_conversion_apply_never_invokes_planning_functions(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text('{"name":"demo","version":"1.0.0"}')
    output = tmp_path / "output"
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            plugins=(PluginConfig("demo"),),
            conversion=ConversionConfig(targets=("cursor",), output_dir=str(output)),
        ),
    )
    installed = InstalledPlugin("demo", "1.0.0", "user", True, str(source))
    cache = {"version": 7, "entries": {}, "codex_output_dirs": [], "pi_output_dirs": []}
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([installed], [])),
        patch("ai_config.sync_state.load_conversion_cache", return_value=cache),
        patch("ai_config.sync_state.save_conversion_cache"),
    ):
        plan = build_sync_plan(target)
        with (
            patch("ai_config.sync_conversion.parse_claude_plugin") as parser,
            patch("ai_config.sync_conversion.convert_plugin") as converter,
            patch("ai_config.sync_conversion.get_emitter") as emitter,
            patch("ai_config.sync_conversion._plan_conversion_pipeline") as planner,
        ):
            report = apply_sync_plan(plan)

    assert report.errors == ()
    parser.assert_not_called()
    converter.assert_not_called()
    emitter.assert_not_called()
    planner.assert_not_called()
