# Sync and Conversion Pipelines

## Sync orchestration

`ai-config sync` uses an explicit data boundary before normal mutation:

```text
fresh cache clear (real `--fresh` only)
    -> observe Claude, cache, ownership, and source state
    -> build immutable RuntimeSnapshot and SourceBatch records
    -> plan_sync(DesiredState, RuntimeSnapshot, SourceBatch)
    -> validate blocking diagnostics and target preflight
    -> apply_sync_plan(SyncPlan)
    -> report ExecutionReport and commit ownership/cache checkpoints
```

`sync_pipeline.py` owns the frozen, tool-independent records, the pure `plan_sync()` transform, and
structural plan validation. `sync_orchestration.py` owns observation and the narrow executor;
`sync_conversion.py` exposes separate typed conversion planning and application boundaries while
retaining target-specific preflight and lifecycle logic; `sync_state.py` owns cache, source, output-root,
and ownership observation helpers below orchestration; `operations.py` retains only stable command
entry points. A `SyncPlan` contains phase-tagged, ordered public actions, blocking diagnostics, exact
ownership-ledger snapshots, and a target-specific `ConversionPlan`. That conversion record carries
resolved candidates and digests, package specifications, retained identities, retiring roots, Pi
desired files, emitted bytes, target action batches, cache update intent, and planned checkpoints.
Marketplace actions always precede dependent plugin actions; conversion actions follow
them. Before any normal action, the executor verifies the observed Claude, cache, ownership, and
source preconditions. It reports completed and failed actions explicitly and does not authorize cache
or ownership checkpoints after a conversion error.

Planning never performs filesystem writes, cache writes, destructive cleanup, or state-changing tool
calls. Conversion observation may parse sources, emit artifacts in memory, and ask the Codex and Pi
lifecycle authorities for dry-run actions. Those decisions are then included in the same materialized
plan used by CLI dry-run rendering and real execution. During apply, target-specific lifecycle code
may validate current preconditions, but the materialized action sequence remains the authorization
boundary. The conversion executor accepts only `ConversionPlan`; it never invokes source parsers,
emitters, cache-choice logic, retention logic, output-root discovery, or conversion planning.

`--fresh` is intentionally outside the pure transform. For a real sync, Claude's cache is cleared
before observation because that ordering changes what can be observed. A fresh dry-run does not clear
the cache and remains mutation-free.

Unavailable configured sources are represented separately from resolved sources. Their diagnostic is
materialized as a reported (non-blocking) conversion error, so earlier independent marketplace and
plugin actions retain historical progress while conversion mutation is skipped and prior proven-owned
state is retained. Disabled or removed sources can request cleanup only through the existing Codex or
Pi ownership ledgers. Cache version 8 invalidates content entries while preserving validated tracked
Codex and Pi output roots so cleanup can still discover prior custom roots; ownership formats are
unchanged.

## Plugin conversion

ai-config converts Claude plugins through a target-independent IR:

```text
Claude plugin directory -> contained source reader -> ClaudePluginParser -> PluginIR
                        -> pure skill projection -> target emitter -> EmitResult/report
```

## Ownership boundaries

`PluginIR` carries identity, skills, commands, hooks, MCP servers, agents, LSP servers, source paths,
and diagnostics. Skill include records are immutable and contain the plugin-relative source path,
canonically derived projected `_shared/` path, captured bytes, and executable mode. Rewrite counts and
duplication bytes belong to projection/report evidence rather than IR. Emitters do not mutate the IR
or reopen include sources. Each target owns an independent `EmitResult`, component mappings,
diagnostics, and output paths.

`source_safety.py` is the canonical authority for conversion source reads. It validates portable
plugin-relative paths, rejects final and in-root ancestor symlinks, traversal, special files, and
resolved escape before reading. Manifests, components, skill assets, includes, Codex support files,
and target-native files use this descriptor-relative, no-follow boundary. `compute_plugin_hash()`
walks the same regular-file universe and permits one metadata-only exception: the exact sibling
`CLAUDE.md -> AGENTS.md` repository context mirror whose target opens as a no-follow regular file.
The digest includes the link path and target text without reading through it; every other symlink
remains a hard failure. Sync rehashes that universe before conversion as a stale plan precondition,
but digesting and conversion remain separate contained passes. Standalone `convert` does not compute
a digest. Cache version 8 invalidates older content signatures while preserving validated tracked
Codex and Pi output roots.

`skill_projection.py` is pure and shared by all four emitters. It rewrites only exact declared
`${CLAUDE_PLUGIN_ROOT}/<path>` occurrences in instruction Markdown, always to a skill-root-relative
`_shared/<path>` even when the Markdown is nested. It copies captured bytes into each consumer,
blocks remaining undeclared placeholders and projected collisions, and returns additive per-copy
evidence. A zero direct rewrite count is valid for a transitive dependency.

| Emitter | Primary output | Shared-state behavior |
|---|---|---|
| `CodexEmitter` | `.ai-config/codex/marketplaces/<name>/` package + marketplace | emits owned sources only; sync uses Codex CLI for install/cache/config |
| `CursorEmitter` | `.cursor/` plus MCP/hooks JSON | writes target files |
| `OpenCodeEmitter` | `.opencode/`, `opencode.json`, `opencode.lsp.json` | writes target files |
| `PiEmitter` | project `.pi/` or user `.pi/agent/` | writes target files/extensions |

`EmitResult` contains emitted files, independent component mappings, diagnostics, additive include
evidence, and proven-owned cleanup paths. `write_to()` removes only explicit owned cleanup paths
before writing. Target-native files under `targets/<target>/` override generated files at the target's
natural root. Pi's normal desired-file ledger owns included copies. Codex keeps them under its owned
package. Cursor and OpenCode do not add cleanup paths for removed includes because they have no
provenance ledger.

## Codex package and lifecycle seam

`CodexEmitter` emits one marketplace per package so conversion is deterministic and does not need to
merge a global marketplace file:

```text
.ai-config/codex/marketplaces/ai-config-<plugin>/
├── .agents/plugins/marketplace.json
└── plugins/<plugin>/
    ├── .codex-plugin/plugin.json
    ├── skills/**/SKILL.md
    └── hooks/hooks.json
```

`converters/codex_package.py` is the shared naming/path source of truth. The emitter never writes
`CODEX_HOME`. `codex_lifecycle.py` reads ai-config's ownership file and delegates marketplace/plugin
add/list/remove to `adapters/codex.py`. Codex remains responsible for cache and enablement.

Lifecycle convergence receives all desired package specs plus the set whose source changed:

- new package: add marketplace, install plugin;
- changed or disabled managed package: remove/add through Codex CLI;
- unchanged package: no mutation;
- removed source: remove only the recorded plugin and marketplace, then delete only its generated
  marketplace root;
- unrelated marketplace name collision: fail closed;
- command failure: include stage, exact command, output, and remediation.

Conversion cache versioning invalidates old loose-output signatures. The ownership file is separate
from the content hash cache because it defines the destructive boundary.

## Fidelity and reports

Each component produces its own mapping:

- native: package skill, package/marketplace structure;
- transform: command-to-skill without variables, package hooks, package MCP;
- fallback/degraded: command variables, partial hook semantics;
- unsupported: agents, LSP, or hook handlers/events without a Codex package equivalent.

Reports and dry runs list package paths. Sync dry runs additionally list planned Codex marketplace
and plugin lifecycle actions without invoking Codex.

## Validation

`CodexOutputValidator` validates marketplace JSON, local source containment, manifest identity and
version, package skill frontmatter, hook structure/root variables, and MCP declarations. It also
reports possible legacy loose output without deleting it.

Real runtime proof uses `tests/probes/probe_codex_plugin_package.py`: generation, doctor validation,
CLI installation, enabled/disabled discovery, hook/MCP ingestion, update, idempotence, removal, and
unrelated-state preservation in isolated homes.


## Prime Markdown ownership

Prime follows the target-specific write-boundary convention: emitters produce
bytes and diagnostics, standalone conversion supplies its source-path identity,
and sync uses `EmittedTargetBatch.source_plugin_id`. `PrimeDesiredFile` attaches
that identity only to the aggregate owned root plan. Shared conversion arguments
and emitted records do not carry a separate Prime source value.

Prime actions and checkpoints are materialized alongside Pi/Codex actions.
Observation snapshots its ledger even when absent. Apply validates that snapshot
and create/update/noop intent before writing, including a repeat check under the
Prime root lock. Empty projections have no ownership checkpoint. Prime retains
stale output deliberately; Pi reconciliation may remove its proven-owned stale
files. The low-level generic emitter writer rejects Prime output without this
ownership boundary.
