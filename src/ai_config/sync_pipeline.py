"""Data contracts for the observe-plan-apply sync pipeline.

This module deliberately contains only immutable records and pure transforms.  Filesystem and
runtime inspection belongs to :mod:`ai_config.sync_orchestration`; target-specific emitters and lifecycle
planners remain the authorities for their own actions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from ai_config.converters.codex_package import CodexPackageSpec
from ai_config.pi_ownership import PiDesiredFile
from ai_config.types import (
    ConversionConfig,
    MarketplaceConfig,
    PluginConfig,
    SyncAction,
)

SyncPhase = Literal["marketplace", "plugin", "conversion"]
SourceProvenance = Literal[
    "configured_local",
    "installed_plugin",
    "observed_remote_marketplace",
    "unavailable_configured_local",
    "unavailable_remote",
    "unavailable_marketplace_less",
]


@dataclass(frozen=True)
class DesiredState:
    """Normalized, immutable view of one configured Claude target."""

    marketplaces: tuple[tuple[str, MarketplaceConfig], ...] = ()
    plugins: tuple[PluginConfig, ...] = ()
    conversion: ConversionConfig | None = None
    force_convert: bool = False


@dataclass(frozen=True)
class ObservedMarketplace:
    """One marketplace row observed from Claude before planning."""

    name: str
    source: str
    repository: str
    install_location: str


@dataclass(frozen=True)
class ObservedPlugin:
    """One plugin row observed from Claude before planning."""

    id: str
    version: str
    scope: Literal["user", "project", "local"]
    enabled: bool
    install_path: str


@dataclass(frozen=True)
class OwnershipSnapshot:
    """Exact ownership ledger bytes (or absence) observed for a target root."""

    target: Literal["codex", "pi", "prime"]
    root: Path
    payload: str | None


@dataclass(frozen=True)
class CacheOwnershipSnapshot:
    """Inspectable conversion cache and target ownership state."""

    cache_payload: str = ""
    codex_output_roots: tuple[Path, ...] = ()
    pi_output_roots: tuple[Path, ...] = ()
    ownership: tuple[OwnershipSnapshot, ...] = ()


@dataclass(frozen=True)
class RuntimeSnapshot:
    """External runtime state collected before normal sync mutation begins."""

    marketplaces: tuple[ObservedMarketplace, ...] = ()
    plugins: tuple[ObservedPlugin, ...] = ()
    cache_and_ownership: CacheOwnershipSnapshot = field(default_factory=CacheOwnershipSnapshot)
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedPluginSource:
    """A configured plugin whose conversion source was resolved during observation."""

    config_id: str
    path: Path
    digest: str | None
    provenance: SourceProvenance


@dataclass(frozen=True)
class UnavailablePluginSource:
    """A configured source that could not be inspected without mutation."""

    config_id: str
    install_path: str
    provenance: SourceProvenance


@dataclass(frozen=True)
class ParsedPluginBatch:
    """Identities successfully parsed for target-specific preflight."""

    plugin_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmittedArtifact:
    """One validated generated file, retained in the plan until execution."""

    path: Path
    content: str | bytes
    binary: bool = False
    executable: bool = False


@dataclass(frozen=True)
class EmittedTargetBatch:
    """All generated output for one source/target emitter invocation."""

    source_plugin_id: str
    target: str
    normalized_plugin_id: str
    version: str | None
    files: tuple[EmittedArtifact, ...] = ()
    cleanup_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class EmittedArtifactBatch:
    """Target artifacts and cache-miss decisions validated before writes."""

    batches: tuple[EmittedTargetBatch, ...] = ()
    refresh_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceBatch:
    """Resolved and unavailable sources plus parsed/emitted preflight summaries."""

    resolved: tuple[ResolvedPluginSource, ...] = ()
    unavailable: tuple[UnavailablePluginSource, ...] = ()
    parsed: ParsedPluginBatch = field(default_factory=ParsedPluginBatch)
    emitted: EmittedArtifactBatch = field(default_factory=EmittedArtifactBatch)


@dataclass(frozen=True)
class PlannedAction:
    """One ordered public sync action and its owning execution phase."""

    phase: SyncPhase
    action: SyncAction


@dataclass(frozen=True)
class BlockingDiagnostic:
    """A planning diagnostic that prevents every normal mutation."""

    phase: SyncPhase
    message: str


@dataclass(frozen=True)
class ReportedDiagnostic:
    """A planned error that permits earlier independent actions but skips its phase."""

    phase: SyncPhase
    message: str


@dataclass(frozen=True)
class PlannedCheckpoint:
    """A cache or ownership checkpoint authorized only after prior actions succeed."""

    kind: Literal["cache", "ownership"]
    target: str
    after_phase: SyncPhase = "conversion"


@dataclass(frozen=True)
class TargetActionBatch:
    """Ordered actions owned by one target-specific lifecycle invocation."""

    target: Literal["codex", "pi", "prime"]
    output_root: Path
    actions: tuple[SyncAction, ...]


@dataclass(frozen=True)
class ConversionCandidatePlan:
    """One parsed source and its exact cache/lifecycle decision."""

    source_plugin_id: str
    source_path: Path
    source_digest: str | None
    conversion_digest: str | None
    source_provenance: SourceProvenance
    refresh: bool
    codex_spec: CodexPackageSpec | None = None


@dataclass(frozen=True)
class PiRootPlan:
    """Exact desired Pi state and retained sources for one owned root."""

    output_root: Path
    desired_files: tuple[PiDesiredFile, ...] = ()
    retained_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversionPlan:
    """Complete target-specific conversion decision consumed by the executor."""

    output_root: Path
    scope: Literal["user", "project"]
    targets: tuple[str, ...]
    signature: str
    cache_payload: str
    cache_dirty: bool
    candidates: tuple[ConversionCandidatePlan, ...] = ()
    codex_specs: tuple[CodexPackageSpec, ...] = ()
    retained_codex_ids: tuple[str, ...] = ()
    removal_reasons: tuple[tuple[str, str], ...] = ()
    pi_roots: tuple[PiRootPlan, ...] = ()
    retiring_codex_roots: tuple[Path, ...] = ()
    emissions: tuple[EmittedTargetBatch, ...] = ()
    actions: tuple[SyncAction, ...] = ()
    target_batches: tuple[TargetActionBatch, ...] = ()
    checkpoints: tuple[PlannedCheckpoint, ...] = ()


@dataclass(frozen=True)
class SyncPlan:
    """Complete, inspectable decision consumed by dry-run and real execution."""

    desired: DesiredState
    runtime: RuntimeSnapshot
    sources: SourceBatch
    actions: tuple[PlannedAction, ...] = ()
    diagnostics: tuple[BlockingDiagnostic, ...] = ()
    reported_diagnostics: tuple[ReportedDiagnostic, ...] = ()
    target_batches: tuple[TargetActionBatch, ...] = ()
    checkpoints: tuple[PlannedCheckpoint, ...] = ()
    conversion: ConversionPlan | None = None

    @property
    def is_blocked(self) -> bool:
        return bool(self.diagnostics)


@dataclass(frozen=True)
class ExecutionFailure:
    """One failed planned action or checkpoint."""

    phase: SyncPhase
    action: SyncAction | None
    message: str


@dataclass(frozen=True)
class ExecutionReport:
    """Explicit progress and failures from applying one materialized plan."""

    plan: SyncPlan
    completed: tuple[SyncAction, ...] = ()
    failures: tuple[ExecutionFailure, ...] = ()
    errors: tuple[str, ...] = ()
    checkpoints_committed: tuple[PlannedCheckpoint, ...] = ()


def validate_sync_plan(plan: SyncPlan) -> tuple[BlockingDiagnostic, ...]:
    """Validate structural plan invariants using only materialized data."""
    diagnostics: list[BlockingDiagnostic] = []
    phase_order = {"marketplace": 0, "plugin": 1, "conversion": 2}
    action_phases = tuple(phase_order[item.phase] for item in plan.actions)
    if action_phases != tuple(sorted(action_phases)):
        diagnostics.append(
            BlockingDiagnostic(
                "conversion", "Sync plan actions are not in deterministic phase order"
            )
        )

    marketplace_names = dict(plan.desired.marketplaces)
    for item in plan.actions:
        if item.phase == "marketplace" and (
            item.action.action != "register_marketplace"
            or item.action.target not in marketplace_names
        ):
            diagnostics.append(
                BlockingDiagnostic(
                    "marketplace", f"Invalid planned marketplace action for {item.action.target}"
                )
            )
        if item.phase == "plugin" and item.action.action not in {"install", "enable", "disable"}:
            diagnostics.append(
                BlockingDiagnostic(
                    "plugin", f"Invalid planned Claude plugin action: {item.action.action}"
                )
            )
        if (
            item.phase == "plugin"
            and item.action.action == "install"
            and item.action.scope
            not in {
                "user",
                "project",
                "local",
            }
        ):
            diagnostics.append(
                BlockingDiagnostic(
                    "plugin",
                    f"Planned install for {item.action.target} requires an explicit valid scope",
                )
            )

    conversion_actions = tuple(item.action for item in plan.actions if item.phase == "conversion")
    batched_actions = tuple(action for batch in plan.target_batches for action in batch.actions)
    if plan.target_batches and batched_actions != conversion_actions:
        diagnostics.append(
            BlockingDiagnostic(
                "conversion", "Materialized conversion action batches are inconsistent"
            )
        )

    if plan.conversion is not None and (
        plan.conversion.actions != conversion_actions
        or plan.conversion.target_batches != plan.target_batches
        or plan.conversion.emissions != plan.sources.emitted.batches
        or plan.conversion.checkpoints != plan.checkpoints
    ):
        diagnostics.append(
            BlockingDiagnostic("conversion", "Materialized conversion plan is inconsistent")
        )

    if plan.conversion is not None:
        observed_sources = {item.config_id: item for item in plan.sources.resolved}
        for candidate in plan.conversion.candidates:
            observed = observed_sources.get(candidate.source_plugin_id)
            if observed is None or (
                observed.path != candidate.source_path
                or observed.digest != candidate.source_digest
                or observed.provenance != candidate.source_provenance
            ):
                diagnostics.append(
                    BlockingDiagnostic(
                        "conversion",
                        f"Conversion candidate source snapshot is inconsistent for "
                        f"{candidate.source_plugin_id}",
                    )
                )

    seen_emissions: set[tuple[str, str]] = set()
    for batch in plan.sources.emitted.batches:
        key = (batch.source_plugin_id, batch.target)
        if key in seen_emissions:
            diagnostics.append(
                BlockingDiagnostic(
                    "conversion",
                    f"Duplicate emitted artifact batch for {batch.source_plugin_id}/{batch.target}",
                )
            )
        seen_emissions.add(key)
        unsafe_paths = [
            path
            for path in (*[item.path for item in batch.files], *batch.cleanup_paths)
            if path.is_absolute() or ".." in path.parts
        ]
        if unsafe_paths:
            diagnostics.append(
                BlockingDiagnostic(
                    "conversion",
                    f"Unsafe emitted artifact path in {batch.source_plugin_id}/{batch.target}",
                )
            )

    checkpoint_keys = tuple((item.kind, item.target) for item in plan.checkpoints)
    if len(checkpoint_keys) != len(set(checkpoint_keys)):
        diagnostics.append(BlockingDiagnostic("conversion", "Duplicate planned checkpoint"))
    planned_ownership = {item.target for item in plan.checkpoints if item.kind == "ownership"}
    batch_roots = {str(item.output_root) for item in plan.target_batches}
    if planned_ownership != batch_roots:
        diagnostics.append(
            BlockingDiagnostic(
                "conversion", "Planned ownership checkpoints do not match target action batches"
            )
        )

    try:
        cache = json.loads(plan.runtime.cache_and_ownership.cache_payload or "{}")
    except json.JSONDecodeError:
        cache = None
    if not isinstance(cache, dict):
        diagnostics.append(BlockingDiagnostic("conversion", "Invalid snapshotted conversion cache"))
    return tuple(diagnostics)


def materialize_plan_validation(plan: SyncPlan) -> SyncPlan:
    """Attach structural validation failures to an immutable plan."""
    validation = tuple(
        diagnostic for diagnostic in validate_sync_plan(plan) if diagnostic not in plan.diagnostics
    )
    if not validation:
        return plan
    return replace(plan, diagnostics=(*plan.diagnostics, *validation))


def prerequisite_sync_plan(plan: SyncPlan) -> SyncPlan:
    """Project an immutable plan to its exact Claude prerequisite prefix."""
    sources = replace(
        plan.sources,
        parsed=ParsedPluginBatch(),
        emitted=EmittedArtifactBatch(),
    )
    return materialize_plan_validation(
        replace(
            plan,
            sources=sources,
            actions=tuple(item for item in plan.actions if item.phase != "conversion"),
            diagnostics=tuple(item for item in plan.diagnostics if item.phase != "conversion"),
            reported_diagnostics=(),
            target_batches=(),
            checkpoints=(),
            conversion=None,
        )
    )


def conversion_stage_plan(plan: SyncPlan) -> SyncPlan:
    """Project a re-observed plan to conversion only, blocking unmet prerequisites."""
    prerequisite_actions = tuple(item for item in plan.actions if item.phase != "conversion")
    diagnostics = plan.diagnostics
    if prerequisite_actions:
        diagnostics = (
            *diagnostics,
            BlockingDiagnostic(
                "conversion",
                "Claude prerequisites did not converge after one apply; "
                "no conversion action was executed",
            ),
        )
    return materialize_plan_validation(
        replace(
            plan,
            actions=tuple(item for item in plan.actions if item.phase == "conversion"),
            diagnostics=diagnostics,
        )
    )


def plan_sync(
    desired: DesiredState,
    runtime: RuntimeSnapshot,
    sources: SourceBatch,
    *,
    conversion_actions: tuple[SyncAction, ...] = (),
    conversion_diagnostics: tuple[str, ...] = (),
    conversion_reported_errors: tuple[str, ...] = (),
    target_batches: tuple[TargetActionBatch, ...] = (),
    checkpoints: tuple[PlannedCheckpoint, ...] = (),
    conversion: ConversionPlan | None = None,
) -> SyncPlan:
    """Build and structurally validate a deterministic plan from plain data."""
    actions: list[PlannedAction] = []
    diagnostics = [BlockingDiagnostic("marketplace", message) for message in runtime.diagnostics]
    installed_marketplaces = {item.name for item in runtime.marketplaces}
    for name, marketplace in desired.marketplaces:
        if name not in installed_marketplaces:
            source = marketplace.path if marketplace.path else marketplace.repo
            actions.append(
                PlannedAction(
                    "marketplace",
                    SyncAction(
                        action="register_marketplace",
                        target=name,
                        reason=f"Add marketplace from {source}",
                    ),
                )
            )

    installed_plugins = {item.id: item for item in runtime.plugins}
    for plugin in desired.plugins:
        installed = installed_plugins.get(plugin.id)
        if installed is None and plugin.enabled:
            actions.append(
                PlannedAction(
                    "plugin",
                    SyncAction(
                        action="install",
                        target=plugin.id,
                        scope=plugin.scope,
                        reason="Plugin not installed",
                    ),
                )
            )
        elif installed is not None and plugin.enabled and not installed.enabled:
            actions.append(
                PlannedAction(
                    "plugin",
                    SyncAction(
                        action="enable",
                        target=plugin.id,
                        reason="Plugin should be enabled",
                    ),
                )
            )
        elif installed is not None and not plugin.enabled and installed.enabled:
            actions.append(
                PlannedAction(
                    "plugin",
                    SyncAction(
                        action="disable",
                        target=plugin.id,
                        reason="Plugin should be disabled",
                    ),
                )
            )

    actions.extend(PlannedAction("conversion", action) for action in conversion_actions)
    diagnostics.extend(
        BlockingDiagnostic("conversion", message) for message in conversion_diagnostics
    )
    return materialize_plan_validation(
        SyncPlan(
            desired=desired,
            runtime=runtime,
            sources=sources,
            actions=tuple(actions),
            diagnostics=tuple(diagnostics),
            reported_diagnostics=tuple(
                ReportedDiagnostic("conversion", message) for message in conversion_reported_errors
            ),
            target_batches=target_batches,
            checkpoints=checkpoints,
            conversion=conversion,
        )
    )
