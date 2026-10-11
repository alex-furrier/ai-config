# Conversion

Convert Claude Code plugins to other AI coding tools.

## Targets

| Target | Output |
|---|---|
| `codex` | installable Codex packages and local marketplaces under `.ai-config/codex/marketplaces/` |
| `cursor` | `.cursor/` skills, commands, hooks, and MCP config |
| `opencode` | `.opencode/` skills plus `opencode.json` / `opencode.lsp.json` |
| `pi` | `.pi/` project or `.pi/agent/` user skills, prompts, and extensions |

```bash
ai-config convert ./my-plugin --target codex
ai-config convert ./my-plugin --dry-run
ai-config convert ./my-plugin --target all --report ./report.json
```

## Codex packages (breaking in 0.6.0)

The `codex` target no longer writes loose `.codex/skills`, prompts, hooks, or MCP tables. It emits
one self-contained package and local marketplace for each source plugin:

```text
.ai-config/codex/marketplaces/ai-config-my-plugin/
├── .agents/plugins/marketplace.json
└── plugins/my-plugin/
    ├── .codex-plugin/plugin.json
    ├── skills/
    │   ├── my-skill/SKILL.md
    │   └── command-my-command/SKILL.md
    └── hooks/hooks.json
```

The package manifest contains supported MCP server declarations. Referenced hook support scripts
are copied into the package, and `${CLAUDE_PLUGIN_ROOT}` becomes Codex's `${PLUGIN_ROOT}`.
Target-native files under the source plugin's targets/codex directory are copied into the package root.

`ai-config convert` only generates package sources. A configured `ai-config sync` also registers
each generated local marketplace and installs or refreshes the plugin through `codex plugin`.
Codex owns its installed cache and enablement in `CODEX_HOME`; ai-config does not imitate that
layout or rewrite unrelated Codex settings.

### Migration from 0.5.x

1. Remove `commands_as_skills` from conversion config and `--commands-as-skills` from scripts.
   Commands now always become package skills. Commands with Claude argument variables are reported
   as degraded.
2. Run `ai-config sync --dry-run`, then `ai-config sync --force-convert`.
3. Confirm the generated package with `ai-config doctor --target codex <output-dir>` and
   `codex plugin list --json`.
4. Review old `.codex/skills`, `.codex/prompts`, `.codex/hooks.json`, and generated MCP entries.
   Doctor reports possible stale output, but ai-config does not delete it because it cannot prove
   whether a loose file is user-authored.
5. Remove only legacy files you recognize as old ai-config output.

Each generated marketplace name is `ai-config-<normalized-plugin>` and each installed selector is
`<normalized-plugin>@ai-config-<normalized-plugin>`. The normalized identity from the source
manifest is used consistently for package paths, both manifests, ownership, Codex CLI selectors,
and drift checks. Two configured sources that normalize to the same identity fail before files or
runtime state change.

Source package versions must be valid SemVer 2.0.0 values such as 1.2.3 or 1.2.3-rc.1.
Same-version content refreshes are allowed, upgrades are applied, and ownership/runtime downgrades
fail closed with a remediation message. ai-config records only owned entries in
`.ai-config/codex/ownership.json`. Removal and update are limited to that state. A collision with an
unrelated marketplace or plugin fails without mutation.

## Sync-driven conversion

```yaml
version: 1
targets:
  - type: claude
    config:
      marketplaces:
        my-plugins:
          source: github
          repo: myorg/my-plugins
      plugins:
        - id: my-tool@my-plugins
          scope: user
          enabled: true
      conversion:
        enabled: true
        targets: [codex, cursor]
        scope: user
```

`scope` selects the default output root (`~` for user, the current project for project). A custom
`output_dir` overrides it. Codex package paths remain under that root's `.ai-config/codex/` tree.

## Component mapping

| Component | Codex | Cursor | OpenCode | Pi |
|---|---|---|---|---|
| Skills | package-local native skill | skill | skill | skill |
| Commands | package skill; degraded with Claude variables | command | command | prompt template |
| Hooks | supported command hooks in package | hooks config | unsupported | extension emulation |
| MCP | package manifest `mcpServers` | `.cursor/mcp.json` | `opencode.json` | unsupported |
| LSP | unsupported | unsupported | `opencode.lsp.json` | unsupported |
| Agents | unsupported | unsupported | unsupported | unsupported |

Reports classify each component independently as native, transform, emulate, fallback/degraded, or
unsupported. One target's mapping never changes another target's report.

## Shared skill resources

Keep plugin-wide resources DRY and declare each exact consumer in its `SKILL.md`:

```yaml
---
name: analyze
description: Analyze data with the shared helper
x-ai-config-includes:
  - shared/analyze.py
  - shared/schema.json
---

Run `${CLAUDE_PLUGIN_ROOT}/shared/analyze.py`.
```

Conversion captures each declared regular file once in IR and materializes byte-preserved copies
inside that generated skill at these illustrative locations:

```text
_shared/shared/analyze.py
_shared/shared/schema.json
```

Exact declared root references in instruction Markdown become skill-root-relative paths beneath the
generated `_shared` directory.
The generated `SKILL.md` omits `x-ai-config-includes`. If two skills consume the same source, each
receives an independent regular-file copy; this intentional distribution-time duplication keeps every
skill self-contained on Codex, Cursor, OpenCode, and Pi.

V1 accepts exact plugin-root-relative files only. It rejects globs, directory or recursive declarations,
absolute paths, empty/dot/dotdot components, backslashes, symlinks, hardlinks, special files, duplicate
declarations, projected path collisions, and undeclared `${CLAUDE_PLUGIN_ROOT}` references. Declared
transitive dependencies need no direct Markdown reference and are reported with a zero rewrite count,
not as unused. Included scripts and binaries are byte-preserved and should locate siblings through
language mechanisms such as `__file__`.

Reports add one record per include and consumer with its plugin-relative logical source,
target-relative destination, copy count, duplicated bytes, and direct rewrite count. Pi copies use the
normal digest ownership ledger. Codex copies stay under the generated package root. Cursor and
OpenCode write desired copies but do not delete removed copies because containment is not provenance.

## Source safety

Conversion reads manifests, component files, skill assets, includes, Codex support files, and
target-native files through descriptor-relative, no-follow traversal rooted at one retained plugin
source descriptor on POSIX. A platform without the required descriptor APIs fails closed before a
source read rather than claiming equivalent race resistance. Absolute/traversing paths, final or
in-root ancestor symlinks, resolved escapes,
and non-regular files fail closed before bytes are read. Unsafe components may be isolated with
`--best-effort`; unrelated safe components can continue. During sync, the cache digest covers every
safely readable plugin file, including shared and target-native bytes, and is rechecked as an execution
precondition. Standalone `convert` does not compute a digest, and the sync digest and later conversion
reads are separate contained passes rather than one immutable filesystem snapshot.

## Target-native files

Put hand-written files under `targets/<target>/`. They are copied into the target's natural output
root and override a generated regular file at the exact same path. File/directory conflicts are
rejected. Final generated skills are rechecked after overrides, so native files cannot reintroduce
`x-ai-config-includes` or unresolved `${CLAUDE_PLUGIN_ROOT}` references. When an override replaces
`SKILL.md` or an included `_shared` copy, report evidence describes only the final emitted projection.
For Codex, the natural root is the generated plugin package—not shared `.codex` config.

## Options

| Option | Description |
|---|---|
| `-t, --target` | repeatable `codex`, `cursor`, `opencode`, `pi`, or `all` |
| `-o, --output` | output root |
| `--scope` | `user` or `project` default output root |
| `--dry-run` | report package/files without writing or invoking lifecycle commands |
| `--best-effort` | continue other target conversion after component errors |
| `--format` | `summary`, `markdown`, or `json` |
| `--report`, `--report-format` | write JSON or Markdown report |

## Validation and cache

```bash
ai-config doctor --target codex ./output-dir
ai-config doctor --target all ./output-dir
ai-config sync --force-convert
```

Sync keys conversion cache entries by configured plugin selector and conversion settings. Source
provenance, physical path, complete safe source digest, and owned generated Codex bytes remain
required observations for a cache hit. A changed source path refreshes output because target files
may contain resolved plugin-root paths. Legacy path-keyed entries are discarded while validated
tracked output roots remain available for ownership cleanup. Normal sync therefore repairs deleted
or tampered output without treating an incidental path as logical plugin identity.

Configured local marketplaces are strict conversion-source authorities, including after Claude
installs a cached copy. If that configured tree is missing or unsafe, sync retains prior ownership
and reports it unavailable rather than converting stale installed bytes. Remote and
marketplace-less plugins may use safely observed installed sources.

A fresh remote plugin may require Claude installation before its source can be inspected. Real sync
applies one immutable Claude prerequisite plan, re-observes once, and then applies a separately
validated conversion-only plan. It never retries Claude actions or recursively syncs until quiet.
Dry-run reports the exact prerequisite actions and deferred source without speculating about the
second stage. If post-install parsing or conversion fails, completed Claude actions remain visible,
conversion exits non-zero, and verification is skipped.

Dry-run and JSON output distinguish planned, completed, and failed actions. `sync --verify` performs
one read-only verification only after all apply stages succeed; an empty isolated `CODEX_HOME` is a
supported initial state. `status --config ... --json` exits non-zero when lifecycle planning finds a
non-no-op action or an inspection error. A temporarily unavailable source retains prior ownership;
a disabled/removed source is cleaned up. Removing or disabling the Codex target also reconciles
prior owned roots, including a prior custom `output_dir` recorded in the conversion cache.

Every Codex subprocess has a finite timeout. On POSIX, each command starts in a separate process
group; after a bounded SIGTERM grace period, timeout cleanup inspects and kills any remaining group
even when the direct child exited first, then performs a bounded reap of the direct child. Non-POSIX
platforms receive direct-child timeout cleanup only; ai-config 0.6.0 does not claim descendant
cleanup there. The adapter accepts Codex 0.144.x through 0.149.x, 0.153.x, 0.156.x, 0.157.x,
and >=0.159.2 (stable releases). The intervening unverified releases remain blocked.
Before each mutation, including direct adapter add/remove, it checks the enabled `plugins` feature
and validates both marketplace and plugin list JSON read-only. These checks cannot prove future
mutation responses or prevent upstream behavior changing between preflight and mutation. Malformed
or inconsistent responses fail with an explicit possible partial-state warning; inspect live Codex
state and ai-config ownership before retrying. No remote catalog or authenticated behavior is
certified by the local 0.159.2 probe. Duplicate local and Git
identities also fail closed; duplicate available identities in Codex's unowned remote catalog are
validated but tolerated because 0.153.x can report distinct remote IDs and versions for one display
identity. Lifecycle failures retain ownership for retry, sanitize child output, name the exact stage and command,
include remediation, and report completed and failed actions.

## Prime Agent Rust (bounded Markdown support)

`--target prime` and `conversion.targets: [prime]` project Claude Markdown
skills into `.prime/agent/skills/<plugin>-<skill>/`. Project scope uses the
project output root; user scope uses the selected home output root. Both paths
are intentional for the Rust runtime. `doctor --target prime` validates this
output. This adapter was checked against **Rust 0.10.0**, not the retired
TypeScript implementation.

Names and descriptions are preserved with a plugin namespace. Supporting files
and declared shared resources are materialized into each skill. The
`disable-model-invocation` field is retained. Claude `allowed-tools`, model,
context/fork, agent and user-invocable semantics are reported as lost features;
Markdown guidance does not enforce tool permissions. Skills with a root
`pyproject.toml` are skipped, since Prime can treat them as executable Python
packages.

| Surface | Runtime evidence | Adapter behavior |
| --- | --- | --- |
| Markdown skills | Rust discovery and real prompt assembly | Converted and validated |
| Global instructions | State-root `AGENTS.md`/`CLAUDE.md` discovery | User-owned; no writes to AGENTS.md or SYSTEM.md |
| Prompt templates and themes | Rust resource loader | Deferred; commands reported unsupported |
| MCP/tool configuration | Native `mcpServers` settings schema | Deferred; no settings/env/token copying |
| Hooks, Python packages, Pi extensions | No established Pi extension loader | Reported unsupported; no generated hooks |
| Agents and LSP | No mapping established by this adapter | Reported unsupported |

### Ownership and preview

Prime writes use `.ai-config/prime-ownership.json` under the output root. Each
entry records a source identity and content digest. Sync identities are the
configured plugin ID; standalone identities are the resolved source path.
Only matching, unedited, owned files can be updated. Unowned collisions,
symlinks, user edits, file/directory conflicts, and adjacent Python package
metadata block conversion before generated writes. Live POSIX writes hold a
stable file lock. Windows ownership writes are not supported in this slice.

Dry runs make no files. Identical repeat syncs preserve file bytes and mtimes.
Prime emitters produce files and diagnostics only. Standalone conversion attaches
`standalone:<source-path>` ownership at the write boundary; sync derives
`sync:<configured-plugin-id>` from its existing materialized source identity.
No target-specific source argument is threaded through shared conversion APIs
or emitter records. Low-level generic `EmitResult.write_to` rejects Prime output;
use the public conversion APIs or the explicit Prime ownership writer.

Sync plans include inspectable create/update/noop Prime actions, exact ownership
ledger contents (including absence), and an ownership checkpoint for nonempty
projections. Apply rechecks snapshots before normal actions and verifies expected
output actions under the root lock. A deleted file after a no-op plan blocks
that plan; a newly built plan can explicitly authorize its recreation.

Prime checks ownership on every sync, including source-cache hits; a missing
owned file can be regenerated. Stale files, disabled plugins, removed targets
and old output roots are **retained**, including their ledger entries. Review
and remove retired projections explicitly; this slice grants no deletion
permission. Standalone and sync projections cannot take over each other's
files. Lost or corrupt ledgers require explicit recovery, never silent adoption.

Conversion never copies authentication, provider/model settings, or whole
configuration homes. `PRIME_AGENT_CODING_AGENT_DIR` is a runtime state override,
not an implicit conversion destination. For a custom profile, project a reviewed
skill into a temporary project first or supply it using Prime's `--skill` option.
A live profile projection needs a separate approved destination scope.

```sh
sandbox=$(mktemp -d)
ai-config convert /absolute/path/to/plugin --target prime --output "$sandbox" --dry-run
ai-config convert /absolute/path/to/plugin --target prime --output "$sandbox"
ai-config doctor --target prime "$sandbox"
# Auth-free introspection; use the actual approved Rust binary.
# Keep HOME/state/XDG/TMPDIR isolated as in the repository probe below.
uv run python tests/probes/probe_prime_skills.py --prime-bin /absolute/path/to/prime-agent
```

The probe builds project and user fixtures in temporary homes, clears ambient
credentials and selectors, and inspects `prompt --json`. It starts no login,
provider change, subscription, model call or worker. It does not validate native
worker completion or permission enforcement.

### Sources and acceptance evidence

Checked 2026-10-10 at upstream commit
`763094e1b6a7c17450ee8399b06cda1853847ca5`:

- [Rust skill loader](https://github.com/PrimeIntellect-ai/prime-agent/blob/763094e1b6a7c17450ee8399b06cda1853847ca5/crates/pa-core/src/skills/loader.rs): both scopes use `.prime/agent/skills`.
- [Skill discovery](https://github.com/PrimeIntellect-ai/prime-agent/blob/763094e1b6a7c17450ee8399b06cda1853847ca5/crates/pa-core/src/skills/discovery.rs): Markdown frontmatter, description requirement, Python package detection.
- [Resource loader](https://github.com/PrimeIntellect-ai/prime-agent/blob/763094e1b6a7c17450ee8399b06cda1853847ca5/crates/pa-core/src/resources/mod.rs): context files and prompt resources.
- [Settings schema](https://github.com/PrimeIntellect-ai/prime-agent/blob/763094e1b6a7c17450ee8399b06cda1853847ca5/crates/pa-core/src/settings/types.rs): native MCP support; no schema equivalence assumed.
- [State directory override](https://github.com/PrimeIntellect-ai/prime-agent/blob/763094e1b6a7c17450ee8399b06cda1853847ca5/crates/pa-cli/src/config.rs): `PRIME_AGENT_CODING_AGENT_DIR` and default state root.

The implementation slice pairs emitter/validator/CLI tests with ownership
conflict, no-deletion, idempotence, dry-run, mixed-target preflight, source
collision and full sync tests. The runtime probe verifies actual discovery.
Existing Codex/Cursor/OpenCode/Pi adapters keep their own mappings and ownership
rules. Docker all-tools testing remains separate from this local Rust probe.
