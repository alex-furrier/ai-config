"""Observation and execution boundaries for sync plans."""

from __future__ import annotations

import json
from pathlib import Path

from ai_config import sync_state as state
from ai_config.adapters import claude
from ai_config.sync_conversion import apply_conversion_plan, plan_conversions
from ai_config.sync_pipeline import (
    CacheOwnershipSnapshot,
    DesiredState,
    EmittedArtifactBatch,
    EmittedTargetBatch,
    ExecutionFailure,
    ExecutionReport,
    ObservedMarketplace,
    ObservedPlugin,
    OwnershipSnapshot,
    ParsedPluginBatch,
    PlannedCheckpoint,
    ResolvedPluginSource,
    RuntimeSnapshot,
    SourceBatch,
    SourceProvenance,
    SyncPlan,
    TargetActionBatch,
    UnavailablePluginSource,
    materialize_plan_validation,
    plan_sync,
)
from ai_config.types import (
    ClaudeTargetConfig,
    PluginSource,
    SyncAction,
    SyncResult,
    TargetConfig,
)


def _desired_state(config: ClaudeTargetConfig, force_convert: bool) -> DesiredState:
    return DesiredState(
        marketplaces=tuple(config.marketplaces.items()),
        plugins=config.plugins,
        conversion=config.conversion,
        force_convert=force_convert,
    )


def _observed_plugin(item: claude.InstalledPlugin) -> ObservedPlugin:
    return ObservedPlugin(item.id, item.version, item.scope, item.enabled, item.install_path)


def _ownership_snapshots(
    codex_roots: tuple[Path, ...], pi_roots: tuple[Path, ...]
) -> tuple[OwnershipSnapshot, ...]:
    snapshots = [
        OwnershipSnapshot("codex", root, (root / ".ai-config/codex/ownership.json").read_text())
        for root in codex_roots
    ]
    snapshots.extend(
        OwnershipSnapshot("pi", root, (root / ".ai-config/pi-ownership.json").read_text())
        for root in pi_roots
    )
    return tuple(snapshots)


def _source_batch(
    config: ClaudeTargetConfig,
    installed_plugins: tuple[claude.InstalledPlugin, ...],
    installed_marketplaces: tuple[claude.InstalledMarketplace, ...],
) -> SourceBatch:
    """Resolve source locations without mutating Claude or generated output."""
    installed_by_id = {item.id: item for item in installed_plugins}
    marketplaces_by_name = {item.name: item for item in installed_marketplaces}
    resolved: list[ResolvedPluginSource] = []
    unavailable: list[UnavailablePluginSource] = []
    for plugin in config.plugins:
        if not plugin.enabled:
            continue
        installed = installed_by_id.get(plugin.id)
        configured_marketplace = (
            config.marketplaces.get(plugin.marketplace) if plugin.marketplace is not None else None
        )
        configured_local = (
            configured_marketplace is not None
            and configured_marketplace.source == PluginSource.LOCAL
        )
        path = state.resolve_plugin_conversion_path(config, plugin, installed)
        provenance: SourceProvenance = (
            "configured_local" if configured_local else "installed_plugin"
        )
        if (
            (path is None or not path.is_dir())
            and plugin.marketplace is not None
            and not configured_local
        ):
            observed_marketplace = marketplaces_by_name.get(plugin.marketplace)
            if observed_marketplace is not None and observed_marketplace.install_location:
                path = state.resolve_local_marketplace_plugin_path(
                    Path(observed_marketplace.install_location), plugin.plugin_name
                )
                if path is not None:
                    provenance = "observed_remote_marketplace"
        if path is not None and path.is_dir():
            resolved.append(
                ResolvedPluginSource(
                    plugin.id,
                    path,
                    state.compute_plugin_hash(path, provenance=provenance),
                    provenance,
                )
            )
        else:
            unavailable_provenance: SourceProvenance = (
                "unavailable_configured_local"
                if configured_local
                else "unavailable_remote"
                if plugin.marketplace is not None
                else "unavailable_marketplace_less"
            )
            unavailable.append(
                UnavailablePluginSource(
                    plugin.id,
                    installed.install_path if installed is not None else "<unavailable>",
                    unavailable_provenance,
                )
            )
    return SourceBatch(resolved=tuple(resolved), unavailable=tuple(unavailable))


def build_sync_plan(target: TargetConfig, *, force_convert: bool = False) -> SyncPlan:
    """Observe one target and materialize its complete decision before mutation."""
    if target.type != "claude":
        desired = DesiredState(force_convert=force_convert)
        runtime = RuntimeSnapshot(diagnostics=(f"v1 only supports 'claude', got: {target.type}",))
        return plan_sync(desired, runtime, SourceBatch())

    marketplaces, marketplace_errors = claude.list_installed_marketplaces()
    plugins, plugin_errors = claude.list_installed_plugins()
    installed_plugins = tuple(sorted(plugins, key=lambda item: item.id))
    observation_errors = [*marketplace_errors, *plugin_errors]
    configured_ids = [item.id for item in target.config.plugins]
    duplicate_configured = sorted(
        plugin_id for plugin_id in set(configured_ids) if configured_ids.count(plugin_id) > 1
    )
    installed_ids = [item.id for item in installed_plugins]
    duplicate_installed = sorted(
        plugin_id for plugin_id in set(installed_ids) if installed_ids.count(plugin_id) > 1
    )
    if duplicate_configured:
        observation_errors.append(
            "Duplicate configured plugin selectors: " + ", ".join(duplicate_configured)
        )
    if duplicate_installed:
        observation_errors.append(
            "Claude reported duplicate installed plugin selectors: "
            + ", ".join(duplicate_installed)
        )
    cache: dict = {}
    codex_roots: tuple[Path, ...] = ()
    pi_roots: tuple[Path, ...] = ()
    ownership: tuple[OwnershipSnapshot, ...] = ()
    if not observation_errors:
        try:
            cache = state.load_conversion_cache()
            codex_roots = tuple(state.owned_codex_output_dirs(target.config.conversion, cache))
            pi_roots = tuple(
                root
                for item in cache.get("pi_output_dirs", [])
                for root in (Path(item).expanduser().resolve(),)
                if state.pi_root_has_ownership(root)
            )
            ownership = _ownership_snapshots(codex_roots, pi_roots)
        except (OSError, ValueError) as error:
            observation_errors.append(str(error))
    runtime = RuntimeSnapshot(
        marketplaces=tuple(
            ObservedMarketplace(
                item.name,
                item.source.value,
                item.repo,
                item.install_location,
            )
            for item in sorted(marketplaces, key=lambda item: item.name)
        ),
        plugins=tuple(_observed_plugin(item) for item in installed_plugins),
        cache_and_ownership=CacheOwnershipSnapshot(
            cache_payload=json.dumps(cache, sort_keys=True),
            codex_output_roots=codex_roots,
            pi_output_roots=pi_roots,
            ownership=ownership,
        ),
        diagnostics=tuple(observation_errors),
    )
    sources = _source_batch(
        target.config,
        installed_plugins,
        tuple(sorted(marketplaces, key=lambda item: item.name)),
    )
    conversion_plan = None
    conversion_actions: tuple[SyncAction, ...] = ()
    conversion_errors: tuple[str, ...] = ()
    reported_errors: tuple[str, ...] = ()
    target_batches: tuple[TargetActionBatch, ...] = ()
    parsed_source_ids: tuple[str, ...] = ()
    refresh_source_ids: tuple[str, ...] = ()
    emitted_batches: tuple[EmittedTargetBatch, ...] = ()
    if not runtime.diagnostics:
        conversion_result = plan_conversions(
            target.config,
            force_convert=force_convert,
            installed_plugins=installed_plugins,
            cache_snapshot=cache,
            resolved_sources={item.config_id: item for item in sources.resolved},
        )
        conversion_plan = conversion_result.plan
        conversion_actions = conversion_result.actions
        conversion_errors = conversion_result.blocking_errors
        reported_errors = conversion_result.reported_errors
        target_batches = conversion_result.target_batches
        parsed_source_ids = conversion_result.parsed_source_ids
        refresh_source_ids = conversion_result.refresh_source_ids
        emitted_batches = conversion_result.emissions
    conversion_active = target.config.conversion is not None and target.config.conversion.enabled
    unreadable_sources = tuple(
        item.config_id for item in sources.resolved if conversion_active and item.digest is None
    )
    changed_sources = tuple(
        item.config_id
        for item in sources.resolved
        if item.digest != state.compute_plugin_hash(item.path, provenance=item.provenance)
    )
    if unreadable_sources:
        conversion_errors += (
            "Conversion sources could not be snapshotted safely: " + ", ".join(unreadable_sources),
        )
    if changed_sources:
        conversion_errors += (
            "Conversion sources changed while the sync snapshot was collected: "
            + ", ".join(changed_sources),
        )
    sources = SourceBatch(
        resolved=sources.resolved,
        unavailable=sources.unavailable,
        parsed=ParsedPluginBatch(parsed_source_ids),
        emitted=EmittedArtifactBatch(emitted_batches, refresh_source_ids),
    )
    checkpoint_roots = sorted(
        {batch.output_root for batch in target_batches}, key=lambda item: item.as_posix()
    )
    checkpoints = tuple(PlannedCheckpoint("ownership", str(root)) for root in checkpoint_roots) + (
        PlannedCheckpoint("cache", str(state.conversion_cache_path())),
    )
    return plan_sync(
        _desired_state(target.config, force_convert),
        runtime,
        sources,
        conversion_actions=conversion_actions,
        conversion_diagnostics=conversion_errors,
        conversion_reported_errors=reported_errors,
        target_batches=target_batches,
        checkpoints=checkpoints,
        conversion=conversion_plan,
    )


def requires_source_reobservation(plan: SyncPlan) -> bool:
    """Return whether one Claude apply may reveal a deferred non-local source."""
    conversion = plan.desired.conversion
    eligible = any(
        item.provenance in {"unavailable_remote", "unavailable_marketplace_less"}
        for item in plan.sources.unavailable
    )
    return bool(
        conversion is not None
        and conversion.enabled
        and eligible
        and plan.reported_diagnostics
        and any(item.phase in {"marketplace", "plugin"} for item in plan.actions)
    )


def _apply_marketplace_action(
    action: SyncAction,
    desired: DesiredState,
    known_names: set[str],
) -> tuple[bool, str | None, set[str]]:
    marketplace = dict(desired.marketplaces)[action.target]
    pre_add_names = set(known_names)
    result = claude.add_marketplace(
        repo=marketplace.repo if marketplace.source == PluginSource.GITHUB else None,
        name=action.target,
        path=marketplace.path if marketplace.source == PluginSource.LOCAL else None,
    )
    if not result.success:
        return False, f"Failed to add marketplace '{action.target}': {result.stderr}", known_names
    post_add, post_errors = claude.list_installed_marketplaces()
    if post_errors:
        return True, "; ".join(post_errors), known_names
    post_names = {item.name for item in post_add}
    if action.target not in post_names:
        new_names = post_names - pre_add_names
        if len(new_names) == 1:
            actual = next(iter(new_names))
            return (
                True,
                (
                    f"Marketplace registered as '{actual}' (from marketplace.json), "
                    f"but config uses '{action.target}'. Update your config key from "
                    f"'{action.target}' to '{actual}' to match."
                ),
                post_names,
            )
        if new_names:
            actual_names = ", ".join(sorted(new_names))
            return (
                True,
                (
                    f"Marketplace registration for '{action.target}' added ambiguous names: "
                    f"{actual_names}. Update the config key to the manifest name."
                ),
                post_names,
            )
    return True, None, post_names


def _apply_plugin_action(action: SyncAction) -> str | None:
    if action.action == "install":
        if action.scope not in {"user", "project", "local"}:
            return f"Planned install for {action.target} requires an explicit valid scope"
        command = claude.install_plugin(action.target, action.scope)
        label = "install"
    elif action.action == "enable":
        command = claude.enable_plugin(action.target)
        label = "enable"
    elif action.action == "disable":
        command = claude.disable_plugin(action.target)
        label = "disable"
    else:
        return f"Unsupported planned Claude plugin action: {action.action}"
    if command.success:
        return None
    return f"Failed to {label} '{action.target}': {command.stderr}"


def _precondition_error(plan: SyncPlan) -> str | None:
    """Verify observed runtime and ownership state without deriving new actions."""
    marketplaces, marketplace_errors = claude.list_installed_marketplaces()
    plugins, plugin_errors = claude.list_installed_plugins()
    errors = [*marketplace_errors, *plugin_errors]
    if errors:
        return "; ".join(errors)
    current_marketplaces = tuple(
        ObservedMarketplace(item.name, item.source.value, item.repo, item.install_location)
        for item in sorted(marketplaces, key=lambda item: item.name)
    )
    current_plugins = tuple(
        _observed_plugin(item) for item in sorted(plugins, key=lambda item: item.id)
    )
    if current_marketplaces != plan.runtime.marketplaces or current_plugins != plan.runtime.plugins:
        return "Claude runtime state changed after sync planning; no action was executed"
    for snapshot in plan.runtime.cache_and_ownership.ownership:
        relative = (
            Path(".ai-config/codex/ownership.json")
            if snapshot.target == "codex"
            else Path(".ai-config/pi-ownership.json")
        )
        try:
            payload = (snapshot.root / relative).read_text()
        except OSError:
            return "Ownership state changed after sync planning; no action was executed"
        if payload != snapshot.payload:
            return "Ownership state changed after sync planning; no action was executed"
    return None


def _conversion_precondition_error(plan: SyncPlan, expected_cache: dict) -> str | None:
    try:
        current_cache = state.load_conversion_cache()
    except ValueError as error:
        return str(error)
    if current_cache != expected_cache:
        return "Conversion cache changed after sync planning; no conversion action was executed"
    for snapshot in plan.runtime.cache_and_ownership.ownership:
        relative = (
            Path(".ai-config/codex/ownership.json")
            if snapshot.target == "codex"
            else Path(".ai-config/pi-ownership.json")
        )
        try:
            if (snapshot.root / relative).read_text() != snapshot.payload:
                return (
                    "Ownership state changed after sync planning; no conversion action was executed"
                )
        except OSError:
            return "Ownership state changed after sync planning; no conversion action was executed"
    changed_sources = tuple(
        item.config_id
        for item in plan.sources.resolved
        if item.digest != state.compute_plugin_hash(item.path, provenance=item.provenance)
    )
    if changed_sources:
        return (
            "Conversion source changed after sync planning; no conversion action was executed: "
            + ", ".join(changed_sources)
        )
    return None


def apply_sync_plan(plan: SyncPlan) -> ExecutionReport:
    """Validate preconditions, then execute a plan without rebuilding its decisions."""
    plan = materialize_plan_validation(plan)
    if plan.is_blocked:
        errors = tuple(item.message for item in plan.diagnostics)
        return ExecutionReport(plan=plan, errors=errors)

    runtime_error = _precondition_error(plan)
    if runtime_error is not None:
        return ExecutionReport(plan=plan, errors=(runtime_error,))

    expected_cache = json.loads(plan.runtime.cache_and_ownership.cache_payload or "{}")
    conversion_precondition = _conversion_precondition_error(plan, expected_cache)
    if conversion_precondition is not None:
        return ExecutionReport(plan=plan, errors=(conversion_precondition,))

    completed: list[SyncAction] = []
    failures: list[ExecutionFailure] = []
    errors: list[str] = []
    known_marketplaces = {item.name for item in plan.runtime.marketplaces}
    for planned in plan.actions:
        if planned.phase == "conversion":
            continue
        if planned.phase == "marketplace":
            action_completed, error, known_marketplaces = _apply_marketplace_action(
                planned.action, plan.desired, known_marketplaces
            )
        else:
            error = _apply_plugin_action(planned.action)
            action_completed = error is None
        if action_completed:
            completed.append(planned.action)
        if error is not None:
            failures.append(ExecutionFailure(planned.phase, planned.action, error))
            errors.append(error)

    if plan.reported_diagnostics:
        errors.extend(item.message for item in plan.reported_diagnostics)
        return ExecutionReport(
            plan=plan,
            completed=tuple(completed),
            failures=tuple(failures),
            errors=tuple(errors),
        )

    conversion_precondition = _conversion_precondition_error(plan, expected_cache)
    if conversion_precondition is not None:
        failures.append(ExecutionFailure("conversion", None, conversion_precondition))
        errors.append(conversion_precondition)
        return ExecutionReport(
            plan=plan,
            completed=tuple(completed),
            failures=tuple(failures),
            errors=tuple(errors),
        )

    committed_checkpoints: list[PlannedCheckpoint] = []
    if plan.conversion is None:
        conversion_actions, conversion_failures, conversion_errors = [], [], []
    else:
        conversion_actions, conversion_failures, conversion_errors = apply_conversion_plan(
            plan.conversion,
            committed_checkpoints=committed_checkpoints,
        )
    completed.extend(conversion_actions)
    failures.extend(
        ExecutionFailure(
            "conversion", action, next(iter(conversion_errors), "planned action failed")
        )
        for action in conversion_failures
    )
    errors.extend(conversion_errors)
    return ExecutionReport(
        plan=plan,
        completed=tuple(completed),
        failures=tuple(failures),
        errors=tuple(errors),
        checkpoints_committed=tuple(committed_checkpoints),
    )


def sync_result_from_execution(report: ExecutionReport) -> SyncResult:
    # Claude marketplace/plugin command failures historically surface as errors only. Preserve
    # SyncResult.actions_failed for target lifecycle partial-progress evidence.
    failed_actions = [
        item.action
        for item in report.failures
        if item.phase == "conversion" and item.action is not None
    ]
    return SyncResult(
        success=not report.errors and not failed_actions,
        actions_taken=list(report.completed),
        actions_failed=failed_actions,
        errors=list(report.errors),
    )
