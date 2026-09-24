# Architecture decisions

These records explain lasting choices whose rationale is established in repository history. They complement the current contract in [SPEC.md](https://github.com/alex-furrier/ai-config/blob/main/SPEC.md), the [architecture overview](../architecture.md), and the [project evolution](../project-evolution.md).

| Decision | Status | Effective date |
|---|---|---|
| [Use Claude Code plugins as the source model](0001-claude-plugin-source-model.md) | Accepted | 2026-02-03 |
| [Convert through a target-neutral IR](0002-target-neutral-plugin-ir.md) | Accepted | 2026-02-12 |
| [Allow target-native files to override generated output](0003-target-native-overrides.md) | Accepted | 2026-05-29 |
| [Emit Codex plugin packages instead of loose files](0004-codex-plugin-packages.md) | Accepted | 2026-07-17 |
| [Require proven ownership for destructive cleanup](0005-proven-output-ownership.md) | Accepted | 2026-07-26 |
| [Separate sync observation, planning, and application](0006-observe-plan-apply-sync.md) | Accepted | 2026-07-29 |
| [Materialize shared plugin resources into generated skills](0007-materialize-shared-skill-resources.md) | Accepted | 2026-08-20 |
| [Bound sync convergence across prerequisite and conversion stages](0008-bounded-sync-convergence-stages.md) | Accepted | 2026-08-21 |

Retrospective records use the date the decision entered `main`, not the date this index was written.
