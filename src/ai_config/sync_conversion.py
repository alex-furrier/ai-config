"""Conversion planning and execution for the materialized sync pipeline."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import cast

from ai_config import sync_state as state
from ai_config.adapters import claude
from ai_config.adapters.codex import CodexCommandError
from ai_config.codex_lifecycle import (
    CodexLifecycleAction,
    CodexLifecycleExecutionError,
    owned_codex_plugin_ids,
    sync_codex_packages,
    validate_codex_transitions,
)
from ai_config.converters import InstallScope, TargetTool, convert_plugin
from ai_config.converters.claude_parser import (
    normalize_portable_name,
    parse_claude_plugin,
    parse_claude_plugin_with_ignored_generated_paths,
)
from ai_config.converters.codex_package import CodexPackageSpec, codex_package_spec
from ai_config.converters.emitters import EmitResult, EmittedFile, PiEmitter, get_emitter
from ai_config.pi_ownership import (
    PiAction,
    PiActionName,
    PiDesiredFile,
    apply_pi_reconciliation,
)
from ai_config.sync_pipeline import (
    ConversionCandidatePlan,
    ConversionPlan,
    EmittedArtifact,
    EmittedTargetBatch,
    PiRootPlan,
    PlannedCheckpoint,
    ResolvedPluginSource,
    TargetActionBatch,
)
from ai_config.types import (
    ClaudeTargetConfig,
    CodexLifecycleActionName,
    SyncAction,
)


@dataclass(frozen=True)
class _ConversionCandidate:
    config_id: str
    plugin_path: Path
    codex_spec: CodexPackageSpec | None
    ignored_generated_paths: frozenset[PurePosixPath]


@dataclass(frozen=True)
class ConversionPlanningResult:
    """Immutable conversion decision produced before normal sync mutation."""

    plan: ConversionPlan
    actions: tuple[SyncAction, ...]
    blocking_errors: tuple[str, ...]
    reported_errors: tuple[str, ...]
    parsed_source_ids: tuple[str, ...]
    refresh_source_ids: tuple[str, ...]
    emissions: tuple[EmittedTargetBatch, ...]
    target_batches: tuple[TargetActionBatch, ...]


def _snapshot_emission(
    source_plugin_id: str,
    target: TargetTool,
    normalized_plugin_id: str,
    version: str | None,
    emitted: EmitResult,
) -> EmittedTargetBatch:
    return EmittedTargetBatch(
        source_plugin_id=source_plugin_id,
        target=target.value,
        normalized_plugin_id=normalized_plugin_id,
        version=version,
        files=tuple(
            EmittedArtifact(file.path, file.content, file.binary, file.executable)
            for file in emitted.files
        ),
        cleanup_paths=tuple(emitted.cleanup_paths),
    )


def _restore_emission(batch: EmittedTargetBatch) -> EmitResult:
    return EmitResult(
        target=TargetTool(batch.target),
        files=[
            EmittedFile(file.path, file.content, file.binary, file.executable)
            for file in batch.files
        ],
        cleanup_paths=list(batch.cleanup_paths),
    )


def _sync_lifecycle_actions(actions: Iterable[CodexLifecycleAction]) -> list[SyncAction]:
    return [
        SyncAction(action=action.action, target=action.target, reason=action.reason)
        for action in actions
    ]


def _apply_codex_lifecycle(
    specs: list[CodexPackageSpec],
    *,
    output_dir: Path,
    refreshed_plugin_ids: set[str],
    retained_plugin_ids: set[str] | None = None,
    removal_reasons: dict[str, str] | None = None,
    default_removal_reason: str = "Source plugin is no longer configured",
    expected_actions: tuple[SyncAction, ...] | None = None,
) -> tuple[list[SyncAction], list[SyncAction], list[str]]:
    """Run one prevalidated lifecycle mutation while preserving partial progress."""
    try:
        completed = sync_codex_packages(
            specs,
            output_dir=output_dir,
            refreshed_plugin_ids=refreshed_plugin_ids,
            retained_plugin_ids=retained_plugin_ids,
            removal_reasons=removal_reasons,
            default_removal_reason=default_removal_reason,
            expected_actions=(
                tuple(
                    CodexLifecycleAction(
                        cast(CodexLifecycleActionName, action.action),
                        action.target,
                        action.reason,
                    )
                    for action in expected_actions
                )
                if expected_actions is not None
                else None
            ),
        )
        return _sync_lifecycle_actions(completed), [], []
    except CodexLifecycleExecutionError as error:
        completed_actions = _sync_lifecycle_actions(error.completed_actions)
        failed_actions = (
            _sync_lifecycle_actions([error.failed_action])
            if error.failed_action is not None
            else []
        )
        return completed_actions, failed_actions, [str(error)]
    except (CodexCommandError, OSError, ValueError) as error:
        return [], [], [str(error)]


def _plan_conversion_pipeline(
    config: ClaudeTargetConfig,
    *,
    force_convert: bool,
    installed_plugins: tuple[claude.InstalledPlugin, ...],
    cache_snapshot: dict,
    resolved_sources: dict[str, ResolvedPluginSource],
    planned_batches: list[TargetActionBatch],
    preflight_summary: dict[str, tuple[str, ...]],
    planned_emissions: list[EmittedTargetBatch],
    reported_errors: list[str],
    prepared_plan: list[ConversionPlan],
) -> tuple[list[SyncAction], list[SyncAction], list[str]]:
    """Materialize the complete conversion decision without mutation."""
    errors: list[str] = []
    try:
        # Planning may normalize root lists, so always detach from the observed snapshot.
        cache = copy.deepcopy(cache_snapshot)
    except ValueError as error:
        return [], [], [str(error)]
    cache_entries = cache.get("entries")
    if not isinstance(cache_entries, dict):
        return [], [], ["Invalid conversion cache entries; clear the cache and retry"]
    tracked_output_dirs = cache.get("codex_output_dirs", [])
    if not isinstance(tracked_output_dirs, list) or any(
        not isinstance(tracked, str) or not tracked for tracked in tracked_output_dirs
    ):
        return [], [], ["Invalid cached Codex output roots; clear the cache and retry"]
    cache["codex_output_dirs"] = tracked_output_dirs
    tracked_pi_output_dirs = cache.get("pi_output_dirs", [])
    if not isinstance(tracked_pi_output_dirs, list) or any(
        not isinstance(tracked, str) or not tracked for tracked in tracked_pi_output_dirs
    ):
        return [], [], ["Invalid cached Pi output roots; clear the cache and retry"]
    cache["pi_output_dirs"] = tracked_pi_output_dirs
    cache_dirty = False

    conversion = config.conversion
    conversion_active = conversion is not None and conversion.enabled
    if conversion_active and conversion is not None:
        targets = [TargetTool(target) for target in conversion.targets]
        output_dir = state.resolve_conversion_output_dir(conversion)
        scope = InstallScope(conversion.scope)
        signature = state.conversion_signature(conversion, output_dir)
    else:
        targets = []
        output_dir = (
            state.resolve_conversion_output_dir(conversion)
            if conversion is not None
            else Path.cwd()
        )
        scope = InstallScope.PROJECT
        signature = ""
    codex_enabled = TargetTool.CODEX in targets
    pi_enabled = TargetTool.PI in targets
    resolved_output_dir = str(output_dir.resolve())
    if pi_enabled and resolved_output_dir not in tracked_pi_output_dirs:
        tracked_pi_output_dirs.append(resolved_output_dir)
        tracked_pi_output_dirs.sort()
        cache_dirty = True
    if codex_enabled and resolved_output_dir not in tracked_output_dirs:
        tracked_output_dirs.append(resolved_output_dir)
        tracked_output_dirs.sort()
        cache_dirty = True
    try:
        prior_output_dirs = state.owned_codex_output_dirs(conversion, cache)
    except ValueError as error:
        return [], [], [str(error)]
    retained_root_strings = {str(root.resolve()) for root in prior_output_dirs}
    if codex_enabled:
        retained_root_strings.add(resolved_output_dir)
    pruned_output_dirs = [
        tracked for tracked in tracked_output_dirs if tracked in retained_root_strings
    ]
    if pruned_output_dirs != tracked_output_dirs:
        tracked_output_dirs[:] = pruned_output_dirs
        cache_dirty = True
    retiring_output_dirs = [
        root
        for root in prior_output_dirs
        if not codex_enabled or root.resolve() != output_dir.resolve()
    ]
    prior_pi_output_dirs = [
        Path(item).expanduser().resolve()
        for item in tracked_pi_output_dirs
        if state.pi_root_has_ownership(Path(item).expanduser().resolve())
    ]
    retiring_pi_output_dirs = [
        root for root in prior_pi_output_dirs if not pi_enabled or root != output_dir.resolve()
    ]

    installed_by_id = (
        {plugin.id: plugin for plugin in installed_plugins} if conversion_active else {}
    )

    codex_specs: list[CodexPackageSpec] = []
    retained_codex_ids: set[str] = set()
    removal_reasons: dict[str, str] = {}
    candidates: list[_ConversionCandidate] = []
    codex_sources: dict[str, str] = {}
    unavailable_pi_sources: set[str] = set()
    has_blocking_errors = False

    if conversion_active:
        for plugin_config in config.plugins:
            configured_identity = normalize_portable_name(plugin_config.plugin_name, "plugin")
            configured_codex_id = f"{configured_identity}@ai-config-{configured_identity}"
            if not plugin_config.enabled:
                if codex_enabled:
                    removal_reasons[configured_codex_id] = "Source plugin is disabled"
                continue

            installed = installed_by_id.get(plugin_config.id)
            resolved_source = resolved_sources.get(plugin_config.id)
            plugin_path = resolved_source.path if resolved_source is not None else None
            if plugin_path is None:
                if codex_enabled:
                    retained_codex_ids.add(configured_codex_id)
                if pi_enabled:
                    unavailable_pi_sources.add(plugin_config.id)
                install_path = (
                    installed.install_path
                    if installed is not None and installed.install_path
                    else "<unavailable>"
                )
                message = (
                    f"Conversion source for {plugin_config.id} is temporarily unavailable "
                    f"(installPath={install_path}); prior owned conversion state was retained"
                )
                errors.append(message)
                reported_errors.append(message)
                continue

            try:
                _metadata_ir, ignored_generated_paths = (
                    parse_claude_plugin_with_ignored_generated_paths(
                        plugin_path, target_native_targets=frozenset(targets)
                    )
                )
            except (OSError, ValueError):
                # Existing guarded conversion paths own user-facing diagnostics.
                # A full fingerprint is the conservative fallback when metadata fails.
                ignored_generated_paths = frozenset()
            spec: CodexPackageSpec | None = None
            if codex_enabled:
                try:
                    ir = parse_claude_plugin(plugin_path)
                except (OSError, ValueError) as error:
                    has_blocking_errors = True
                    errors.append(f"Conversion failed for {plugin_config.id}: {error}")
                    continue
                parse_errors = [
                    diagnostic.message
                    for diagnostic in ir.diagnostics
                    if diagnostic.severity.value == "error"
                ]
                if parse_errors:
                    has_blocking_errors = True
                    errors.extend(
                        f"Conversion failed for {plugin_config.id}: {message}"
                        for message in parse_errors
                    )
                    continue
                if configured_identity != ir.identity.plugin_id:
                    has_blocking_errors = True
                    errors.append(
                        f"Codex identity mismatch for configured plugin '{plugin_config.id}': "
                        f"config normalizes to '{configured_identity}', but source manifest "
                        f"normalizes to '{ir.identity.plugin_id}'. Make the config selector and "
                        "manifest name agree; no package or lifecycle state was changed."
                    )
                    continue
                try:
                    spec = codex_package_spec(
                        configured_identity,
                        ir.identity.version,
                        output_dir,
                        source_plugin_id=plugin_config.id,
                    )
                except ValueError as error:
                    has_blocking_errors = True
                    errors.append(f"Conversion failed for {plugin_config.id}: {error}")
                    continue
                conflicting_source = codex_sources.get(spec.plugin_id)
                if conflicting_source is not None:
                    has_blocking_errors = True
                    errors.append(
                        f"Normalized Codex plugin identity collision for '{spec.plugin_id}' from "
                        f"'{conflicting_source}' and '{plugin_config.id}'. Rename one source plugin; "
                        "no Codex package or lifecycle state was changed."
                    )
                    continue
                codex_sources[spec.plugin_id] = plugin_config.id
                codex_specs.append(spec)
            candidates.append(
                _ConversionCandidate(plugin_config.id, plugin_path, spec, ignored_generated_paths)
            )

    if has_blocking_errors:
        return [], [], errors

    if codex_enabled:
        try:
            validate_codex_transitions(codex_specs, prior_output_dirs)
        except ValueError as error:
            errors.append(str(error))
            return [], [], errors

    # Pi is reconciled as one owned output set, rather than plugin-by-plugin writes.
    # Its parser/emitter diagnostics must be fatal before any lifecycle plan can mutate output.
    pi_desired: list[PiDesiredFile] = []
    pi_diagnostic_errors = False
    if pi_enabled:
        for candidate in candidates:
            try:
                ir = parse_claude_plugin(candidate.plugin_path)
                parse_errors = [
                    diagnostic.message
                    for diagnostic in ir.diagnostics
                    if diagnostic.severity.value == "error"
                ]
                if parse_errors:
                    pi_diagnostic_errors = True
                    errors.extend(
                        f"Pi conversion failed for {candidate.config_id}: {message}"
                        for message in parse_errors
                    )
                    continue
                emitted = PiEmitter(scope).emit(ir)
                emit_errors = [
                    diagnostic.message
                    for diagnostic in emitted.diagnostics
                    if diagnostic.severity.value == "error"
                ]
                if emit_errors:
                    pi_diagnostic_errors = True
                    errors.extend(
                        f"Pi conversion failed for {candidate.config_id}: {message}"
                        for message in emit_errors
                    )
                    continue
                planned_emissions.append(
                    _snapshot_emission(
                        candidate.config_id,
                        TargetTool.PI,
                        ir.identity.plugin_id,
                        ir.identity.version,
                        emitted,
                    )
                )
            except (OSError, ValueError) as error:
                pi_diagnostic_errors = True
                errors.append(f"Pi conversion failed for {candidate.config_id}: {error}")
                continue
            for file in emitted.files:
                content = (
                    file.content.encode("utf-8") if isinstance(file.content, str) else file.content
                )
                pi_desired.append(
                    PiDesiredFile(candidate.config_id, file.path, content, file.executable)
                )
        if pi_diagnostic_errors:
            return [], [], errors
    candidates_to_convert: list[tuple[_ConversionCandidate, str | None]] = []
    candidate_hashes: dict[str, str | None] = {}
    candidate_conversion_hashes: dict[str, str | None] = {}
    for candidate in candidates:
        source = resolved_sources[candidate.config_id]
        plugin_hash = state.compute_plugin_conversion_hash(
            candidate.plugin_path,
            ignored_paths=candidate.ignored_generated_paths,
            provenance=source.provenance,
        )
        candidate_hashes[candidate.config_id] = source.digest
        candidate_conversion_hashes[candidate.config_id] = plugin_hash
        cache_valid = False
        if not force_convert and plugin_hash is not None:
            signature_map = cache_entries.get(candidate.config_id)
            cached = signature_map.get(signature) if isinstance(signature_map, dict) else None
            cache_valid = (
                isinstance(cached, dict)
                and cached.get("hash") == plugin_hash
                and cached.get("source_path") == str(candidate.plugin_path)
                and cached.get("source_provenance") == source.provenance
            )
            if cache_valid and candidate.codex_spec is not None:
                if not isinstance(cached, dict):
                    cache_valid = False
                else:
                    cached_output_hash = cached.get("codex_output_hash")
                    cache_valid = isinstance(
                        cached_output_hash, str
                    ) and cached_output_hash == state.compute_owned_codex_hash(candidate.codex_spec)
        if not cache_valid:
            candidates_to_convert.append((candidate, plugin_hash))
    preflight_summary["refresh"] = tuple(
        candidate.config_id for candidate, _plugin_hash in candidates_to_convert
    )

    # Pi has its own ownership-aware write path; other targets retain existing behavior.
    non_pi_targets = [target for target in targets if target != TargetTool.PI]
    # Validate every emitter result in memory before lifecycle cleanup or generated writes.
    preflight_candidates = candidates_to_convert
    for candidate, _plugin_hash in preflight_candidates:
        try:
            reports = convert_plugin(
                plugin_path=candidate.plugin_path,
                targets=non_pi_targets,
                output_dir=output_dir,
                scope=scope,
                dry_run=True,
                best_effort=True,
            )
        except (OSError, ValueError) as error:
            errors.append(f"Conversion failed for {candidate.config_id}: {error}")
            continue
        report_errors = [
            f"{target.value}: {diagnostic.message}"
            for target, report in reports.items()
            for diagnostic in report.errors
        ]
        errors.extend(
            f"Conversion failed for {candidate.config_id}: {message}" for message in report_errors
        )
        if report_errors:
            continue
        try:
            ir = parse_claude_plugin(candidate.plugin_path)
            for target in non_pi_targets:
                emitted = get_emitter(target, scope).emit(ir)
                emit_errors = [
                    diagnostic.message
                    for diagnostic in emitted.diagnostics
                    if diagnostic.severity.value == "error"
                ]
                if emit_errors:
                    errors.extend(
                        f"Conversion failed for {candidate.config_id}: {target.value}: {message}"
                        for message in emit_errors
                    )
                    continue
                planned_emissions.append(
                    _snapshot_emission(
                        candidate.config_id,
                        target,
                        ir.identity.plugin_id,
                        ir.identity.version,
                        emitted,
                    )
                )
        except (OSError, ValueError) as error:
            errors.append(f"Conversion failed for {candidate.config_id}: {error}")

    preflight_actions: list[SyncAction] = []
    target_removed_reason = "Codex conversion target is disabled or removed"
    try:
        pi_plan_roots = [(output_dir, pi_desired, unavailable_pi_sources)] if pi_enabled else []
        pi_plan_roots.extend((root, [], set()) for root in retiring_pi_output_dirs)
        for pi_root, desired, retained_sources in pi_plan_roots:
            pi_actions = tuple(
                SyncAction(action=action.action, target=str(action.path), reason=action.reason)
                for action in apply_pi_reconciliation(
                    pi_root,
                    desired,
                    dry_run=True,
                    retained_sources=retained_sources,
                    ownership_domain="sync",
                )
            )
            preflight_actions.extend(pi_actions)
            if planned_batches is not None:
                planned_batches.append(TargetActionBatch("pi", pi_root.resolve(), pi_actions))
        for retiring_root in retiring_output_dirs:
            planned = sync_codex_packages(
                [],
                output_dir=retiring_root,
                refreshed_plugin_ids=set(),
                dry_run=True,
                removal_reasons={},
                default_removal_reason=target_removed_reason,
            )
            sync_actions = tuple(_sync_lifecycle_actions(planned))
            preflight_actions.extend(sync_actions)
            if planned_batches is not None:
                planned_batches.append(
                    TargetActionBatch("codex", retiring_root.resolve(), sync_actions)
                )
        if codex_enabled:
            migrating_ids = {
                plugin_id
                for retiring_root in retiring_output_dirs
                for plugin_id in owned_codex_plugin_ids(retiring_root)
            }
            migrating_marketplaces = {
                f"ai-config-{plugin_id.rsplit('@', 1)[0]}" for plugin_id in migrating_ids
            }
            planned = sync_codex_packages(
                codex_specs,
                output_dir=output_dir,
                refreshed_plugin_ids={
                    candidate.codex_spec.plugin_id
                    for candidate, _hash in candidates_to_convert
                    if candidate.codex_spec is not None
                },
                retained_plugin_ids=retained_codex_ids,
                removal_reasons=removal_reasons,
                ignored_runtime_plugin_ids=migrating_ids,
                ignored_runtime_marketplace_names=migrating_marketplaces,
                dry_run=True,
            )
            sync_actions = tuple(_sync_lifecycle_actions(planned))
            preflight_actions.extend(sync_actions)
            if planned_batches is not None:
                planned_batches.append(
                    TargetActionBatch("codex", output_dir.resolve(), sync_actions)
                )
    except (CodexCommandError, OSError, ValueError) as error:
        errors.append(str(error))

    preflight_summary["parsed"] = tuple(candidate.config_id for candidate in candidates)

    blocking_errors = [error for error in errors if error not in reported_errors]
    if blocking_errors:
        return preflight_actions, [], errors
    planned_errors = list(errors)
    batches = tuple(planned_batches or ())
    emissions = tuple(planned_emissions or ())
    checkpoint_roots = sorted(
        {batch.output_root for batch in batches}, key=lambda item: item.as_posix()
    )
    checkpoints = tuple(PlannedCheckpoint("ownership", str(root)) for root in checkpoint_roots) + (
        PlannedCheckpoint("cache", str(state.conversion_cache_path())),
    )
    pi_roots = tuple(
        PiRootPlan(root.resolve(), tuple(desired), tuple(sorted(retained_sources)))
        for root, desired, retained_sources in (
            ([(output_dir, pi_desired, unavailable_pi_sources)] if pi_enabled else [])
            + [(root, [], set()) for root in retiring_pi_output_dirs]
        )
    )
    refresh_ids = {candidate.config_id for candidate, _digest in candidates_to_convert}
    prepared_plan.append(
        ConversionPlan(
            output_root=output_dir.resolve(),
            scope=scope.value,
            targets=tuple(target.value for target in targets),
            signature=signature,
            cache_payload=json.dumps(cache, sort_keys=True),
            cache_dirty=cache_dirty,
            candidates=tuple(
                ConversionCandidatePlan(
                    source_plugin_id=candidate.config_id,
                    source_path=candidate.plugin_path,
                    source_digest=candidate_hashes.get(candidate.config_id),
                    conversion_digest=candidate_conversion_hashes.get(candidate.config_id),
                    source_provenance=resolved_sources[candidate.config_id].provenance,
                    refresh=candidate.config_id in refresh_ids,
                    codex_spec=candidate.codex_spec,
                )
                for candidate in candidates
            ),
            codex_specs=tuple(codex_specs),
            retained_codex_ids=tuple(sorted(retained_codex_ids)),
            removal_reasons=tuple(sorted(removal_reasons.items())),
            pi_roots=pi_roots,
            retiring_codex_roots=tuple(root.resolve() for root in retiring_output_dirs),
            emissions=emissions,
            actions=tuple(preflight_actions),
            target_batches=batches,
            checkpoints=checkpoints,
        )
    )
    return preflight_actions, [], planned_errors


def plan_conversions(
    config: ClaudeTargetConfig,
    *,
    force_convert: bool,
    installed_plugins: tuple[claude.InstalledPlugin, ...],
    cache_snapshot: dict,
    resolved_sources: dict[str, ResolvedPluginSource],
) -> ConversionPlanningResult:
    """Preflight conversion and materialize all emitted and lifecycle decisions."""
    target_batches: list[TargetActionBatch] = []
    summary: dict[str, tuple[str, ...]] = {}
    emissions: list[EmittedTargetBatch] = []
    reported_errors: list[str] = []
    prepared: list[ConversionPlan] = []
    actions, _failures, errors = _plan_conversion_pipeline(
        config,
        force_convert=force_convert,
        installed_plugins=installed_plugins,
        cache_snapshot=cache_snapshot,
        resolved_sources=resolved_sources,
        planned_batches=target_batches,
        preflight_summary=summary,
        planned_emissions=emissions,
        reported_errors=reported_errors,
        prepared_plan=prepared,
    )
    reported = tuple(reported_errors)
    if not prepared:
        # Blocking diagnostics prevent apply, but retain a structurally complete empty decision.
        prepared.append(
            ConversionPlan(
                output_root=Path.cwd().resolve(),
                scope="project",
                targets=(),
                signature="",
                cache_payload=json.dumps(cache_snapshot, sort_keys=True),
                cache_dirty=False,
            )
        )
    return ConversionPlanningResult(
        prepared[0],
        tuple(actions),
        tuple(error for error in errors if error not in reported),
        reported,
        summary.get("parsed", ()),
        summary.get("refresh", ()),
        tuple(emissions),
        tuple(target_batches),
    )


class _TargetBatchCursor:
    """Consume target batches in their planned order without deriving replacements."""

    def __init__(self, batches: tuple[TargetActionBatch, ...]) -> None:
        self._batches = batches
        self._index = 0

    def consume(self, target: str, root: Path) -> tuple[SyncAction, ...]:
        if self._index >= len(self._batches):
            raise ValueError(f"Missing planned {target} action batch for {root}")
        batch = self._batches[self._index]
        self._index += 1
        if batch.target != target or batch.output_root != root.resolve():
            raise ValueError(
                f"Planned target action order changed: expected {batch.target} at "
                f"{batch.output_root}, got {target} at {root.resolve()}"
            )
        return batch.actions

    def ensure_exhausted(self) -> None:
        if self._index != len(self._batches):
            raise ValueError("Materialized target action batches were not fully consumed")


def apply_conversion_plan(
    plan: ConversionPlan,
    *,
    committed_checkpoints: list[PlannedCheckpoint],
) -> tuple[list[SyncAction], list[SyncAction], list[str]]:
    """Apply an exact conversion plan without invoking parsers, emitters, or planners."""
    actions: list[SyncAction] = []
    failed_actions: list[SyncAction] = []
    errors: list[str] = []
    if tuple(action for batch in plan.target_batches for action in batch.actions) != plan.actions:
        return [], [], ["Materialized conversion action batches are inconsistent"]
    try:
        cache = json.loads(plan.cache_payload)
    except json.JSONDecodeError as error:
        return [], [], [f"Invalid materialized conversion cache: {error}"]
    if not isinstance(cache, dict) or not isinstance(cache.get("entries"), dict):
        return [], [], ["Invalid materialized conversion cache entries"]
    cache_entries = cache["entries"]
    cache_dirty = plan.cache_dirty
    cursor = _TargetBatchCursor(plan.target_batches)

    # Pi reconciliation owns its writes and ownership checkpoint as one exact root plan.
    for root_plan in plan.pi_roots:
        try:
            expected = tuple(
                PiAction(cast(PiActionName, action.action), Path(action.target), action.reason)
                for action in cursor.consume("pi", root_plan.output_root)
            )
            completed = apply_pi_reconciliation(
                root_plan.output_root,
                list(root_plan.desired_files),
                retained_sources=set(root_plan.retained_source_ids),
                ownership_domain="sync",
                expected_actions=expected,
            )
        except (OSError, ValueError) as error:
            return actions, failed_actions, [str(error)]
        actions.extend(
            SyncAction(action=item.action, target=str(item.path), reason=item.reason)
            for item in completed
        )
        committed_checkpoints.append(
            PlannedCheckpoint("ownership", str(root_plan.output_root.resolve()))
        )

    tracked_pi_roots = cache.get("pi_output_dirs", [])
    if not isinstance(tracked_pi_roots, list):
        return actions, failed_actions, ["Invalid materialized Pi output roots"]
    retained_pi_roots = sorted(
        root
        for root in tracked_pi_roots
        if state.pi_root_has_ownership(Path(root).expanduser().resolve())
    )
    if retained_pi_roots != tracked_pi_roots:
        cache["pi_output_dirs"] = retained_pi_roots
        cache_dirty = True

    target_removed_reason = "Codex conversion target is disabled or removed"
    tracked_codex_roots = cache.get("codex_output_dirs", [])
    if not isinstance(tracked_codex_roots, list):
        return actions, failed_actions, ["Invalid materialized Codex output roots"]
    for retiring_root in plan.retiring_codex_roots:
        try:
            expected = cursor.consume("codex", retiring_root)
        except ValueError as error:
            return actions, failed_actions, [str(error)]
        completed, failed, lifecycle_errors = _apply_codex_lifecycle(
            [],
            output_dir=retiring_root,
            refreshed_plugin_ids=set(),
            removal_reasons={},
            default_removal_reason=target_removed_reason,
            expected_actions=expected,
        )
        actions.extend(completed)
        failed_actions.extend(failed)
        if lifecycle_errors:
            return actions, failed_actions, lifecycle_errors
        committed_checkpoints.append(PlannedCheckpoint("ownership", str(retiring_root.resolve())))
        root_text = str(retiring_root.resolve())
        if root_text in tracked_codex_roots:
            tracked_codex_roots.remove(root_text)
            cache_dirty = True

    non_pi_targets = {target for target in plan.targets if target != TargetTool.PI.value}
    refreshed_codex_ids: set[str] = set()
    emissions_by_source: dict[str, list[EmittedTargetBatch]] = {}
    for batch in plan.emissions:
        if batch.target != TargetTool.PI.value:
            emissions_by_source.setdefault(batch.source_plugin_id, []).append(batch)
    for candidate in plan.candidates:
        if not candidate.refresh:
            continue
        candidate_batches = emissions_by_source.get(candidate.source_plugin_id, [])
        if {batch.target for batch in candidate_batches} != non_pi_targets:
            errors.append(
                f"Conversion failed for {candidate.source_plugin_id}: materialized emitter batch "
                "does not match planned targets"
            )
            continue
        try:
            for batch in candidate_batches:
                _restore_emission(batch).write_to(plan.output_root)
        except (OSError, ValueError) as error:
            errors.append(f"Conversion failed for {candidate.source_plugin_id}: {error}")
            continue
        if candidate.codex_spec is not None:
            codex_batch = next(
                (batch for batch in candidate_batches if batch.target == TargetTool.CODEX.value),
                None,
            )
            identity = (
                (codex_batch.normalized_plugin_id, codex_batch.version or "0.0.0")
                if codex_batch is not None
                else None
            )
            if identity != (candidate.codex_spec.plugin_name, candidate.codex_spec.version):
                errors.append(
                    f"Conversion failed for {candidate.source_plugin_id}: normalized source "
                    "identity changed between lifecycle preflight and package emission"
                )
                continue
            refreshed_codex_ids.add(candidate.codex_spec.plugin_id)
        if candidate.conversion_digest is not None:
            signature_map = cache_entries.setdefault(candidate.source_plugin_id, {})
            if not isinstance(signature_map, dict):
                signature_map = {}
                cache_entries[candidate.source_plugin_id] = signature_map
            cache_value: dict[str, str] = {
                "hash": candidate.conversion_digest,
                "source_path": str(candidate.source_path),
                "source_provenance": candidate.source_provenance,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if candidate.codex_spec is not None:
                output_hash = state.compute_owned_codex_hash(candidate.codex_spec)
                if output_hash is None:
                    errors.append(
                        f"Conversion failed for {candidate.source_plugin_id}: generated Codex "
                        "output is missing or contains symlinks"
                    )
                    continue
                cache_value["codex_output_hash"] = output_hash
            signature_map[plan.signature] = cache_value
            cache_dirty = True
    if errors:
        return actions, failed_actions, errors

    if TargetTool.CODEX.value in plan.targets:
        try:
            expected = cursor.consume("codex", plan.output_root)
        except ValueError as error:
            return actions, failed_actions, [str(error)]
        completed, failed, lifecycle_errors = _apply_codex_lifecycle(
            list(plan.codex_specs),
            output_dir=plan.output_root,
            refreshed_plugin_ids=refreshed_codex_ids,
            retained_plugin_ids=set(plan.retained_codex_ids),
            removal_reasons=dict(plan.removal_reasons),
            expected_actions=expected,
        )
        actions.extend(completed)
        failed_actions.extend(failed)
        errors.extend(lifecycle_errors)
        ownership_was_checkpointed = not (
            not expected and not plan.codex_specs and plan.retained_codex_ids
        )
        if not lifecycle_errors and ownership_was_checkpointed:
            committed_checkpoints.append(
                PlannedCheckpoint("ownership", str(plan.output_root.resolve()))
            )
    try:
        cursor.ensure_exhausted()
    except ValueError as error:
        errors.append(str(error))
    if cache_dirty and not errors:
        try:
            state.save_conversion_cache(cache)
        except OSError as error:
            errors.append(f"Failed to save conversion cache: {error}")
        else:
            committed_checkpoints.append(
                PlannedCheckpoint("cache", str(state.conversion_cache_path()))
            )
    return actions, failed_actions, errors
