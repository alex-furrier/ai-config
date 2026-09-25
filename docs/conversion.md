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
cleanup there. The adapter accepts the repository-supported Codex 0.144.x through 0.149.x,
0.153.x, 0.156.x, and 0.157.x JSON contracts. Unverified 0.150.x through 0.152.x, 0.154.x, and
0.155.x releases remain fail-closed. Runtime probe evidence in the compatibility baseline currently
includes 0.153.3, 0.156.1, and 0.157.0.
Malformed, partial, inconsistent, or unknown-version responses fail closed. Duplicate local and Git
identities also fail closed; duplicate available identities in Codex's unowned remote catalog are
validated but tolerated because 0.153.x can report distinct remote IDs and versions for one display
identity. Lifecycle failures retain ownership for retry, sanitize child output, name the exact stage and command,
include remediation, and report completed and failed actions.
