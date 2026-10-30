"""Core operations for ai-config: sync, status, update."""

from __future__ import annotations

from ai_config.adapters import claude
from ai_config.sync_orchestration import (
    apply_sync_plan,
    build_sync_plan,
    requires_source_reobservation,
    sync_result_from_execution,
)
from ai_config.sync_pipeline import conversion_stage_plan, prerequisite_sync_plan
from ai_config.types import (
    AIConfig,
    PluginStatus,
    StatusResult,
    SyncAction,
    SyncResult,
    TargetConfig,
)


def sync_target(
    target: TargetConfig,
    dry_run: bool = False,
    fresh: bool = False,
    force_convert: bool = False,
) -> SyncResult:
    """Observe, plan, and optionally apply one target configuration."""
    if target.type != "claude":
        return SyncResult(
            success=False,
            errors=[f"v1 only supports 'claude', got: {target.type}"],
        )

    fresh_errors: list[str] = []
    # Fresh is intentionally outside the pure pipeline: clearing Claude's cache must precede
    # observation. Dry-run never crosses this mutation boundary.
    if fresh and not dry_run:
        cache_result = claude.clear_cache()
        if not cache_result.success:
            fresh_errors.append(f"Failed to clear cache: {cache_result.stderr}")

    plan = build_sync_plan(target, force_convert=force_convert)
    if dry_run:
        plan_errors = [item.message for item in (*plan.diagnostics, *plan.reported_diagnostics)]
        rendered_plan = (
            prerequisite_sync_plan(plan) if requires_source_reobservation(plan) else plan
        )
        result = SyncResult(
            success=not plan_errors,
            actions_taken=[item.action for item in rendered_plan.actions],
            errors=plan_errors,
        )
    elif requires_source_reobservation(plan):
        prerequisite = sync_result_from_execution(apply_sync_plan(prerequisite_sync_plan(plan)))
        if not prerequisite.success or prerequisite.actions_failed or prerequisite.errors:
            result = prerequisite
        else:
            reobserved = build_sync_plan(target, force_convert=force_convert)
            converged = sync_result_from_execution(
                apply_sync_plan(conversion_stage_plan(reobserved))
            )
            result = SyncResult(
                success=converged.success,
                actions_taken=[*prerequisite.actions_taken, *converged.actions_taken],
                actions_failed=[*prerequisite.actions_failed, *converged.actions_failed],
                errors=[*prerequisite.errors, *converged.errors],
            )
    else:
        result = sync_result_from_execution(apply_sync_plan(plan))
    if fresh_errors:
        result.errors[:0] = fresh_errors
        result.success = False
    return result


def sync_config(
    config: AIConfig,
    dry_run: bool = False,
    fresh: bool = False,
    force_convert: bool = False,
) -> dict[str, SyncResult]:
    """Sync all targets in a config.

    Args:
        config: Configuration to sync.
        dry_run: If True, only report what would be done.
        fresh: If True, clear Claude's plugin cache before syncing.
        force_convert: If True, bypass conversion hash cache.

    Returns:
        Dict mapping target type to SyncResult.
    """
    results: dict[str, SyncResult] = {}

    for target in config.targets:
        results[target.type] = sync_target(target, dry_run, fresh, force_convert)

    return results


def get_status(target_type: str = "claude") -> StatusResult:
    """Get current status of plugins and marketplaces.

    Args:
        target_type: Target to get status for (only "claude" supported).

    Returns:
        StatusResult with current state.
    """
    if target_type != "claude":
        return StatusResult(
            target_type="claude",
            errors=[f"v1 only supports 'claude', got: {target_type}"],
        )

    result = StatusResult(target_type="claude")

    # Get plugins
    plugins, plugin_errors = claude.list_installed_plugins()
    result.errors.extend(plugin_errors)

    for plugin in plugins:
        result.plugins.append(
            PluginStatus(
                id=plugin.id,
                installed=True,
                enabled=plugin.enabled,
                scope=plugin.scope,
                version=plugin.version,
            )
        )

    # Get marketplaces
    marketplaces, mp_errors = claude.list_installed_marketplaces()
    result.errors.extend(mp_errors)

    for mp in marketplaces:
        result.marketplaces.append(mp.name)

    return result


def update_plugins(
    plugin_ids: list[str] | None = None,
    fresh: bool = False,
) -> SyncResult:
    """Update plugins to latest versions.

    Args:
        plugin_ids: Specific plugins to update, or None for all.
        fresh: If True, clear cache before updating.

    Returns:
        SyncResult with update actions.
    """
    result = SyncResult()

    # Clear cache if fresh mode
    if fresh:
        cache_result = claude.clear_cache()
        if not cache_result.success:
            result.errors.append(f"Failed to clear cache: {cache_result.stderr}")

    # Get installed plugins
    installed, errors = claude.list_installed_plugins()
    if errors:
        result.errors.extend(errors)
        result.success = False
        return result

    # Determine which plugins to update
    if plugin_ids is None:
        plugins_to_update = [p.id for p in installed]
    else:
        installed_ids = {p.id for p in installed}
        plugins_to_update = [pid for pid in plugin_ids if pid in installed_ids]

        # Warn about plugins that aren't installed
        for pid in plugin_ids:
            if pid not in installed_ids:
                result.errors.append(f"Plugin '{pid}' is not installed, skipping")

    # Update each plugin
    for plugin_id in plugins_to_update:
        update_result = claude.update_plugin(plugin_id)
        action = SyncAction(
            action="install",  # update is like reinstall
            target=plugin_id,
            reason="Update to latest version",
        )

        if update_result.success:
            result.add_success(action)
        else:
            result.add_failure(action, update_result.stderr)

    return result


def sync_discrepancies(results: dict[str, SyncResult]) -> list[str]:
    """Translate dry-run lifecycle plans and inspection failures into verification truth."""
    discrepancies: list[str] = []
    for target_type, result in results.items():
        discrepancies.extend(f"{target_type}: {error}" for error in result.errors)
        discrepancies.extend(
            f"{target_type}: failed to inspect {action.action} for {action.target}"
            for action in result.actions_failed
        )
        discrepancies.extend(
            f"{target_type}: {action.action} required for {action.target}: {action.reason}"
            for action in result.actions_taken
            if action.action not in {"noop_codex_plugin", "noop_pi_output", "noop_prime_output"}
        )
    return discrepancies


def verify_sync(config: AIConfig) -> list[str]:
    """Verify all configured targets using the same dry-run planner as sync."""
    return sync_discrepancies(sync_config(config, dry_run=True))
