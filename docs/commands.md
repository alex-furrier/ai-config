# Commands

Complete reference for all ai-config commands.

## Overview

| Command | What it does |
|---------|--------------|
| `init` | Interactive config generator |
| `sync` | Install/uninstall plugins to match config |
| `status` | Show what's currently installed |
| `watch` | Auto-sync on file changes |
| `update` | Update plugins to latest versions |
| `doctor` | Validate setup and show fix hints |
| `convert` | Convert plugins to other AI tools |
| `plugin create` | Scaffold a new plugin |
| `cache clear` | Clear the plugin cache |

## Global Options

```bash
ai-config [OPTIONS] COMMAND

Options:
  -c, --config PATH  Path to config file
  --version          Show version
  --help             Show help message
```

## init

Interactive wizard for creating or updating your config file.

```bash
ai-config init
```

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output PATH` | Output path for the config file |
| `--non-interactive` | Create minimal config without prompts |

The wizard walks you through:

1. Adding marketplaces (GitHub repos or local directories with plugins)
2. Selecting plugins from those marketplaces
3. Choosing install scope (user or project)

Creates `.ai-config/config.yaml` in the current directory (or the path specified with `-o`).

## sync

Make installed plugins match your config file.

```bash
ai-config sync
```

**Options:**

| Option | Description |
|--------|-------------|
| `-c, --config PATH` | Path to config file |
| `--dry-run` | Show what would change without doing it |
| `--fresh`, `--force` | Clear Claude's plugin cache and reconvert configured outputs; preserve target homes and ownership ledgers |
| `--force-convert` | Reconvert only (without clearing plugin cache) |
| `--verify` | Verify sync state after completion |
| `--json` | Output planned/completed actions and reasons as JSON |

What it does:

- Installs plugins listed in config but not installed
- Uninstalls plugins installed but not in config
- Updates plugin configurations
- Runs conversion if `conversion` section is configured (see [Conversion](conversion.md))

Codex conversion reports register, install, update, reinstall/repair, remove, and no-op actions with
reasons. JSON distinguishes `planned_actions`, `completed_actions`, and `failed_actions`. Sync exits
non-zero if any target fails or post-sync lifecycle verification finds drift, and terminal output only
prints `No changes needed` after a clean result.

## status

Show current state of marketplaces and plugins.

```bash
ai-config status
```

**Options:**

| Option | Description |
|--------|-------------|
| `-c, --config PATH` | Path to config file; also computes lifecycle drift |
| `--verify` | Verify current state matches config |
| `--json` | Output as JSON, including planned lifecycle actions and reasons |

Displays:

- Configured marketplaces and their status
- Installed plugins (from config and extra)
- Any sync issues

With `--config` or `--verify`, status uses the same lifecycle planner as sync and exits non-zero for
any non-no-op action or inspection error. Terminal output only prints `No lifecycle actions needed`
or `All in sync` after clean inspection and planning; it never derives those claims from Claude-only
state while Codex package drift remains.

## watch

Auto-sync when config or plugin files change.

```bash
ai-config watch
```

**Options:**

| Option | Description |
|--------|-------------|
| `-c, --config PATH` | Path to config file |
| `--debounce SECONDS` | Wait time before syncing (default: 1.5) |
| `--dry-run` | Show changes without syncing |
| `-v, --verbose` | Show all file events |

Useful during plugin development. Watches:

- `.ai-config/config.yaml`
- Plugin directories

Press Ctrl+C to stop.

!!! warning "Claude Code reload required"

    Claude Code only loads plugins at session start. After `watch` syncs your changes, you must restart Claude Code for them to take effect.

    To continue your previous session after restarting:

    ```bash
    claude --resume
    ```

## update

Update plugins to their latest versions.

```bash
ai-config update --all
ai-config update PLUGIN1 PLUGIN2
```

**Options:**

| Option | Description |
|--------|-------------|
| `--all` | Update all plugins |
| `--fresh` | Clear cache before updating |

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PLUGINS` | Specific plugin IDs to update (positional, space-separated) |

You must specify either `--all` or one or more plugin names.

## doctor

Validate your setup and find problems.

```bash
ai-config doctor
```

**Options:**

| Option | Description |
|--------|-------------|
| `-c, --config PATH` | Path to config file |
| `--category CATEGORY` | Run only specific validation categories (can be repeated) |
| `-t, --target TARGET` | Validate converted output: `codex`, `cursor`, `opencode`, `pi`, `prime`, or `all` |
| `--json` | Output as JSON |
| `-v, --verbose` | Show all checks including passed |

**Arguments (target mode only):**

| Argument | Description |
|----------|-------------|
| `OUTPUT_DIR` | Directory containing converted output (default: current dir) |

### Default mode

Checks:

- Marketplace URLs are reachable
- Plugins are properly installed
- Skills have required fields
- Hooks are executable
- MCP server configs are valid

### Target validation mode

When `--target` is specified, validates converted output instead of plugin config:

```bash
ai-config doctor --target codex ./output-dir
ai-config doctor --target all ./output-dir
```

Checks target-specific output directory structure, SKILL.md files, MCP/hooks/LSP config validity.

## convert

Convert a Claude Code plugin to other AI coding tools.

```bash
ai-config convert PLUGIN_PATH
```

**Options:**

| Option | Description |
|--------|-------------|
| `-t, --target TARGET` | Target tool(s): `codex`, `cursor`, `opencode`, `pi`, `all` (default: `all`) |
| `-o, --output DIR` | Output directory (default: based on `--scope`) |
| `--scope SCOPE` | `user` or `project` — controls default output path |
| `--dry-run` | Preview changes without writing files |
| `--best-effort` | Continue even if some components fail to convert |
| `--format FORMAT` | Console output: `summary`, `markdown`, or `json` |
| `--report PATH` | Write conversion report to a file |
| `--report-format FORMAT` | Report file format: `json` (default) or `markdown` |

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PLUGIN_PATH` | Path to the Claude Code plugin directory to convert |

Supported targets:

- **codex** — OpenAI Codex (installable packages and local marketplaces under `.ai-config/codex/`; configured sync manages the Codex CLI lifecycle)
- **cursor** — Cursor (`.cursor/` dir with skills, commands, hooks, and MCP config)
- **opencode** — OpenCode (`opencode.json` + `.opencode/` skills dir)
- **pi** — Pi (`.pi/` dir with skills, prompt templates, and extensions)

Multiple targets can be specified: `-t codex -t cursor`

See [Conversion](conversion.md) for a full guide.

## plugin create

Scaffold a new plugin.

```bash
ai-config plugin create NAME
```

**Options:**

| Option | Description |
|--------|-------------|
| `--path PATH` | Base path for plugin directory |

Creates a plugin directory with:

- `manifest.yaml` — Plugin metadata
- skills directory — Skill files
- hooks directory — Hook files

## cache clear

Clear the plugin cache.

```bash
ai-config cache clear
```

Forces fresh downloads on next sync. Use when plugins seem stale.
