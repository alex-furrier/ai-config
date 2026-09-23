# Target Compatibility Baseline

This file records the runtime assumptions behind ai-config target conversion.

Last checked: 2026-08-20
Context: first-class Codex plugin packages, source-less Codex marketplace metadata, Codex compatibility, and Claude skill include metadata for shared-resource projection.

## Summary

| Target | ai-config output | Runtime validation |
|---|---|---|
| Claude Code | Source plugin installed through Claude marketplaces | `claude plugin validate/list`, `claude mcp list` |
| Codex | One installable package and local marketplace per source plugin under `.ai-config/codex/marketplaces/` | isolated marketplace/install/enable/discovery/update/remove probe |
| Cursor | `.cursor/skills/`, `.cursor/mcp.json`, `.cursor/hooks.json` | JSON checks and `cursor-agent mcp list` |
| OpenCode | `.opencode/skills/`, `opencode.json`, `opencode.lsp.json` | `opencode debug skill/config`, `opencode mcp list` |
| Pi | project `.pi/` or user `.pi/agent/` skills, prompts, extensions | RPC `get_commands` and extension marker probe |

## Claude 2.1.231 shared-skill metadata evidence

Phase 0 froze the per-skill `x-ai-config-includes` contract only after strict validation and native
plugin loading both accepted it. Run the isolated, credential-free probe with:

```bash
uv run python tests/probes/probe_claude_skill_includes.py
```

Exact output captured on 2026-08-20 (temporary path suffixes vary):

```text
$ /Users/alexfurrier/.local/share/mise/installs/node/24/bin/claude --version
2.1.231 (Claude Code)
exit=0
$ /Users/alexfurrier/.local/share/mise/installs/node/24/bin/claude plugin validate --strict /var/folders/.../ai-config-include-probe-v4xprg3l
Validating plugin manifest: /var/folders/.../ai-config-include-probe-v4xprg3l/.claude-plugin/plugin.json

✔ Validation passed
exit=0
$ /Users/alexfurrier/.local/share/mise/installs/node/24/bin/claude --plugin-dir /var/folders/.../ai-config-include-probe-v4xprg3l plugin details include-probe
include-probe 1.0.0
  Validate ai-config include metadata
  Source: include-probe@inline

Component inventory
  Skills (1)  one
  Agents (0)
  Hooks (0)
  MCP servers (0)
  LSP servers (0)

Projected token cost
  Always-on:   ~23 tok   added to every session

Per-component (rounded)
  component  always-on  on-invoke
  one              ~20        ~40

  On-invoke cost is paid each time a skill or agent fires.
  Token counts are estimates and may differ from actual usage.
exit=0
```

The probe's `SKILL.md` declares exactly:

```yaml
x-ai-config-includes:
  - shared/data.txt
```

This is ai-config build metadata, not a claim that Claude itself materializes the resource. Claude
accepts and loads the source skill; ai-config strips the field while producing self-contained target
skills.

## Codex 0.144.5, 0.145.0, 0.146.0, 0.148.0, 0.149.0, 0.153.3, and 0.156.1 runtime evidence

The latest lane resolved npm's `@openai/codex@latest` tag at execution time on 2026-07-16:

```text
resolved package: @openai/codex@0.144.5
version output:   codex-cli 0.144.5
binary:           <temporary>/install/node_modules/@openai/codex/bin/codex.js
main tarball:      https://registry.npmjs.org/@openai/codex/-/codex-0.144.5.tgz
main integrity:    sha512-jjB+K+OMv572mKhS+2QuLxWXDJNdpwbPenf+V+8bdq7wg4Scqt3cn6WEekD8wPqDVZqck0HSX17K9rD9kbDJQA==
darwin-arm64:      https://registry.npmjs.org/@openai/codex/-/codex-0.144.5-darwin-arm64.tgz
platform integrity: sha512-zcT6NfBCqLFt+BReNSETTZW6v6PdbH0dzNtm9j7l7mDGqwPbKZDGJdnpkBao2389I0ZacyIKgSZoI0vez1d4Dw==
install source:    integrity-verified direct npm registry tarball extraction
```

The same lane resolved and passed the complete package and public-sync probes for Codex 0.145.0 on
2026-07-24:

```text
resolved package: @openai/codex@0.145.0
version output:   codex-cli 0.145.0
main integrity:   sha512-/PSPSFujjjmiyVFvG2yu/grOFhsWdokTH8t2KGWhXSo/M5n/dIDsnbsnO82/7bLtIoDuzQf7ATBUMWqPWQINlQ==
darwin-arm64 integrity: sha512-h6aQ0UxnaP8mIM/9/qPAH9MNkRliJo88toq1T36IxNM2L5JSU0TFamu+MZn7YkFgDsrp0RfiI+97Tm8AVVxqtA==
install source:    integrity-verified direct npm registry tarball extraction
```

The same lane resolved and passed both complete probes for Codex 0.146.0 on 2026-07-29:

```text
resolved package: @openai/codex@0.146.0
version output:   codex-cli 0.146.0
main integrity:   sha512-yG3sPWNda/2YAIQIDq9MrrjoCTIQ7rxYM5IasrG3VBcuhCLTkgeg/JzqmJq1V98RE4MJ5jCxDXXQlOjrditFRw==
darwin-arm64 integrity: sha512-nb61yX4r5L6Z0dlC4o3u0GAK1YCd4TUvjaB382bajDoh84V+uv2hTBIVZ++fgXWV9yoeuNrNnNcn7GoTGOe2Tg==
install source:    integrity-verified direct npm registry tarball extraction
```

The latest lane resolved and passed both complete auth-free probes for Codex 0.148.0 on 2026-08-20:

```text
resolved package: @openai/codex@0.148.0
version output:   codex-cli 0.148.0
main tarball:      https://registry.npmjs.org/@openai/codex/-/codex-0.148.0.tgz
main integrity:    sha512-bh5kH9+BMrFaHGmLeoSansPdfRksvr4UXzjQInns/KRO7r8VJ+6AAW+SqUsE8XcG3+OW/mI4EEy8Gpo9UDXGvQ==
darwin-arm64:      https://registry.npmjs.org/@openai/codex/-/codex-0.148.0-darwin-arm64.tgz
platform integrity: sha512-xgBPFiF1fHUlRS7HE6wGB56LjBJh16kGD7b4TTbwdVBZNB4QDkTok+vdkAGrfpVkfKcwGNhPSKDgCw+KMZOVug==
install source:    integrity-verified direct npm registry tarball extraction
```

The latest lane resolved and passed both complete auth-free probes for Codex 0.149.0 on 2026-08-20:

```text
resolved package: @openai/codex@0.149.0
version output:   codex-cli 0.149.0
main tarball:      https://registry.npmjs.org/@openai/codex/-/codex-0.149.0.tgz
main integrity:    sha512-i4dryj2Y1j+00Mb5n+0n71EYnTK9/KDc2cdFo/dXD0d1oTog2bhUssKDEIOnKmnEf51P0Z/HJTWvTKw/UHyOvQ==
darwin-arm64:      https://registry.npmjs.org/@openai/codex/-/codex-0.149.0-darwin-arm64.tgz
platform integrity: sha512-GsZJbzBWiD48RETrO8VHGAQNgfSrUVxItXZFeD87wswatPi0+lKuQo8Dx4nMYmOZhZrVtwr3al/feRrZxnDV8Q==
install source:    integrity-verified direct npm registry tarball extraction
```

The latest lane resolved and passed both complete auth-free probes for Codex 0.153.3 on 2026-09-04:

```text
resolved package: @openai/codex@0.153.3
version output:   codex-cli 0.153.3
main tarball:      https://registry.npmjs.org/@openai/codex/-/codex-0.153.3.tgz
main integrity:    sha512-SwQns+YIXvaXV4a6RUd9twgTPJkkfZpuTNEkTtIkwBnfw6fpT61+d6gU1WHZxK6vWFNqJCBoJEP69FIAsoPduA==
darwin-arm64:      https://registry.npmjs.org/@openai/codex/-/codex-0.153.3-darwin-arm64.tgz
platform integrity: sha512-cIJh2xhww3ZBXmgBt6e9gQjrdL2c9xiiq7yimDHtTGdg3wiUOADD/OsmIu/RJOitwM8OgImdHFjR59doBBdo8A==
install source:    integrity-verified direct npm registry tarball extraction
```

The complete auth-free package probe passed for Codex 0.156.1 on 2026-09-23:

```text
resolved package: @openai/codex@0.156.1
version output:   codex-cli 0.156.1
main tarball:      https://registry.npmjs.org/@openai/codex/-/codex-0.156.1.tgz
main integrity:    sha512-nI1iVl/n2SO2lSvlwEsJx63zdSI4C4Me2gR7AG0OWMJiGSakz2tY2hx43E39Zq5aEoeB5bZjJXzp5Sqhog6vyA==
darwin-arm64:      https://registry.npmjs.org/@openai/codex/-/codex-0.156.1-darwin-arm64.tgz
darwin-arm64 integrity: sha512-Jg6wbdV+wmMZczhwE74GSxOYEZlViKXn6KyCw/yfrz3PAKFD14xljuPopmdhWC1+8IKU2WdN5fdmXNPt2q4HPA==
install source:    Dots-managed npm package with isolated auth-free probe state
```

The 0.153.3 and 0.156.1 runtime probes confirmed the same stable feature rows, lifecycle mutation
semantics, source-less catalog behavior, and package discovery contract previously observed through
0.149.0. A logged-in catalog probe additionally found remote available rows using typed
`{"source":"remote","id":"..."}` identities instead of URLs, plus a small number of duplicate
remote plugin identities with distinct remote IDs and versions. The adapter validates and preserves
that unowned remote catalog while keeping local and Git duplicate identities fail-closed. Help copy
changed in several commands but preserved the parsed command and JSON surfaces used by the adapter.

The direct extraction in `tests/probes/probe_latest_codex.sh` is intentional. npm's configured
install-date cutoff rejected a normal fresh install because 0.144.5 was newer than the cutoff.
The lane still resolves the live tag, reads each registry URL and integrity value, verifies SHA-512,
and recreates npm's main-plus-platform package layout. It does not call an older installed Codex
"latest."

Exact feature rows:

```text
hooks                                stable             true
plugin_sharing                       stable             true
plugins                              stable             true
remote_plugin                        stable             true
```

This differs from the 0.142.3 spike: `remote_plugin` moved from `under development false` to
`stable true`. ai-config's local package implementation does not depend on remote plugin behavior.

Observed help surfaces:

| Command | First help line / contract |
|---|---|
| `codex plugin --help` | Manage Codex plugins |
| `codex plugin add --help` | Install a plugin from a configured marketplace snapshot. |
| `codex plugin list --help` | List plugins available from configured marketplace snapshots |
| `codex plugin remove --help` | Remove an installed plugin from local config and cache. |
| `codex plugin marketplace --help` | Add, list, upgrade, or remove configured plugin marketplaces |
| `codex plugin marketplace add --help` | Add a local or Git marketplace to the configured marketplace sources |
| `codex plugin marketplace list --help` | List plugin marketplaces Codex is currently considering and their roots |
| `codex plugin marketplace upgrade --help` | Refresh configured Git marketplace snapshots. |
| `codex plugin marketplace remove --help` | Remove a configured marketplace source by name |

Official sources checked through 2026-08-20:

- [Codex changelog](https://developers.openai.com/codex/changelog)
- [Codex 0.156.1 release](https://github.com/openai/codex/releases/tag/rust-v0.156.1)
- [Codex 0.153.3 release](https://github.com/openai/codex/releases/tag/rust-v0.153.3)
- [Codex 0.149.0 release](https://github.com/openai/codex/releases/tag/rust-v0.149.0)
- [Codex 0.148.0 release](https://github.com/openai/codex/releases/tag/rust-v0.148.0)
- [Codex 0.146.0 release](https://github.com/openai/codex/releases/tag/rust-v0.146.0)
- [Codex 0.145.0 release](https://github.com/openai/codex/releases/tag/rust-v0.145.0)
- [Plugins](https://learn.chatgpt.com/docs/plugins)
- [Build plugins](https://learn.chatgpt.com/docs/build-plugins)
- [Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Hooks](https://learn.chatgpt.com/docs/hooks)
- [Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)

## Codex package contract used by ai-config

- `.codex-plugin/plugin.json` owns package metadata, `skills`, `hooks`, and `mcpServers`.
- `.agents/plugins/marketplace.json` points to a local package source.
- Package skills are native Agent Skills. Claude commands become package skills; commands using
  `$ARGUMENTS` or positional variables are reported degraded because Codex skills do not provide
  Claude slash-command substitution.
- Supported command hooks become package `hooks/hooks.json`. `${CLAUDE_PLUGIN_ROOT}` becomes
  `${PLUGIN_ROOT}`, and referenced support scripts are copied into the package. Missing support
  files cause the handler to be omitted and diagnosed.
- MCP servers become manifest `mcpServers` entries. Agents and LSP servers remain unsupported.
- `targets/codex/**` files land inside the package root and can override generated package files.
- No new `.codex/skills`, `.codex/prompts`, `.codex/hooks.json`, or direct generated
  `[mcp_servers.*]` tables are emitted.

`ai-config sync` uses the Codex CLI for marketplace and installed-cache state. ai-config owns only
its generated marketplace directories and `.ai-config/codex/ownership.json`; Codex owns
`$CODEX_HOME/config.toml`, enablement, and cache. Parsed source identity is the sole normalized key
for package/marketplace manifests, ownership, CLI selectors, and drift checks. Normalized collisions,
duplicate runtime records, source/path mismatches, and SemVer downgrades fail before mutation.
Removal is limited to entries recorded in the ownership file. Possible old loose output is reported
by `doctor` and never removed without proof of ownership.

The adapter accepts Codex 0.144.x through 0.149.x, 0.153.x, and 0.156.x contracts. Captured
isolated runtime evidence in this document includes 0.153.3 and 0.156.1. Unverified 0.150.x through
0.152.x, 0.154.x, and 0.155.x releases remain fail-closed. The adapter validates the CLI version plus typed schemas and
semantic identity for marketplace list/add/remove and plugin list/add/remove responses. Malformed
JSON, duplicate keys/records, partial output, unknown versions,
and inconsistent success responses are errors. Every call uses a finite timeout and bounds/strips
control characters from error output. POSIX calls start a separate process group; after a bounded
SIGTERM grace period, timeout cleanup inspects and kills any remaining group even if the direct child
already exited, then performs a bounded direct-child reap. Non-POSIX cleanup is limited to the direct
child; 0.6.0 does not advertise descendant cleanup on those platforms.

Codex 0.144.5 can omit `marketplaceSource` from resolved marketplace and plugin catalog rows when no
configured source corresponds to the resolved marketplace root. The adapter accepts only absence:
a present value must remain a typed object with a known source type and non-empty source. Installed
rows without this metadata retain their plugin source but have no inferred marketplace root, so
desired or previously owned identity collisions still fail before mutation. Codex 0.148.0, 0.149.0, and 0.153.3 do not list the probe's directly seeded
`$CODEX_HOME/.tmp/plugins` source-less catalog. The public-sync probe
therefore records its initial CLI visibility and exact files, then requires both to remain unchanged;
configured marketplace and plugin add/list/remove schemas and semantics remain compatible.

## Isolated runtime probe

```bash
tests/probes/probe_latest_codex.sh
```

The latest shell lane runs both `probe_codex_plugin_package.py` and
`probe_ai_config_sync_codex.py`. Each creates fresh `HOME` and `CODEX_HOME`, removes
`OPENAI_API_KEY`, `CODEX_API_KEY`, `CHATGPT_API_KEY`, `OPENAI_ORG_ID`, and
`OPENAI_PROJECT_ID`.

The isolated generated-package probe proves:

1. generated package and marketplace validation;
2. marketplace add/list and plugin available/install/list;
3. enabled, disabled, and re-enabled skill discovery through `debug prompt-input`;
4. package hooks copied to the installed cache and package MCP visible through `mcp list`;
5. content update followed by CLI remove/reinstall and updated discovery;
6. repeated sync leaves Codex config byte-identical;
7. managed removal leaves an unrelated marketplace, plugin, disabled/enabled state, and scalar config;
8. strict Codex doctor still loads the resulting config.

The public-command probe invokes `python -m ai_config sync --config <isolated> --json` against a
disposable source marketplace and real Codex binary. It proves first registration/install,
unchanged no-op, automatic repair of tampered generated output, SemVer refresh/update and
discovery, status drift reporting, disabled-plugin reinstall, missing-plugin repair, owned removal,
and preservation of unrelated marketplace, plugin enablement, scalar config, and source-less
marketplace/plugin catalog state. The pinned all-tools E2E lane runs both probes.

The Docker all-tools image pins `@openai/codex@0.145.0`. The separate workflow latest lane resolves
and probes `@latest` so reproducibility and drift detection remain independent.

## Other target assumptions

### Pi

- Project skills/prompts/extensions use `.pi/`; user scope uses `.pi/agent/`.
- Hook commands are emulated with TypeScript extensions.
- RPC `get_commands` proves skill discovery without credentials.

### Cursor

- Output includes `.cursor/skills/`, `.cursor/mcp.json`, and `.cursor/hooks.json`.
- `cursor-agent mcp list` is the available real-tool MCP check; skill validation remains file based.

### OpenCode

- Output includes `.opencode/skills/`, `opencode.json`, and `opencode.lsp.json`.
- `opencode debug skill/config/paths` and `opencode mcp list` are preferred auth-free probes.

## Update checklist

1. Resolve current versions and record exact binaries/install sources.
2. Confirm named help/probe surfaces before using them.
3. Isolate runtime homes and credentials.
4. Update emitters, validators, docs, and real-tool tests together.
5. Pin a reproducible E2E version and retain an explicit latest lane.
