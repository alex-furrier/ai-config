# ai-config

Declarative plugin manager for Claude Code — with cross-tool conversion to Codex, Cursor, OpenCode, and Pi.

## Why This Exists

You've spent time customizing your AI coding setup — skills, MCP servers, hooks, workflows. Then you want to try a different tool, and you're starting from scratch. Or you set up a new machine and can't remember what you installed.

ai-config solves both problems:

- **Reproducible setup** — define your plugins in one YAML file, run `ai-config sync`, done. Works the same on every machine.
- **No tool lock-in** — your customizations convert automatically to Codex, Cursor, OpenCode, and Pi. Try a new tool without re-doing your config.
- **Version-controlled** — check your `.ai-config/config.yaml` into git and share it with your team.

Or more simply, run `ai-config init` and it writes the config for you.

## Installation

```bash
uv tool install git+https://github.com/alex-furrier/ai-config
```

This installs `ai-config` globally. Run `ai-config --help` to verify.

### For Development

```bash
git clone https://github.com/alex-furrier/ai-config.git
cd ai-config
just setup    # Install dependencies
just check    # Run lint, type check, tests
```

## Quick Start

**1. Create your config**

```bash
ai-config init
```

Interactive wizard walks you through adding marketplaces and plugins. Creates `.ai-config/config.yaml`.

**2. Sync to install plugins**

```bash
ai-config sync
```

Installs/uninstalls plugins to match your config. Run this after editing `config.yaml`.

**3. Convert to other tools** (optional)

```bash
ai-config convert ./my-plugin --target codex
```

Converts a Claude Code plugin to work with Codex, Cursor, OpenCode, or Pi. You can also set up automatic conversion on sync via the `conversion` config section.

**4. Check status**

```bash
ai-config status
```

Shows what's installed vs what's in config.

## The idea

You define your setup once. `ai-config sync` installs your Claude plugins and generates equivalent config for every other tool:

```
ai-config sync
  → Claude Code: plugins installed
  → Codex:       ~/.ai-config/codex/ packages, installed via codex plugin CLI
  → Cursor:      ~/.cursor/skills/, ~/.cursor/mcp.json, ~/.cursor/hooks.json
  → OpenCode:    ~/.opencode/skills/, ~/opencode.json
  → Pi:          ~/.pi/agent/skills/, ~/.pi/agent/prompts/, ~/.pi/agent/extensions/
```

Check your `.ai-config/config.yaml` into your dotfiles. Run `ai-config sync` on any machine. Want to try a new tool? Add it to `conversion.targets` and re-sync — your skills are already there.

## What's Next

- [Commands](commands.md) — Full command reference
- [Configuration](config.md) — Config file format and examples
- [Conversion](conversion.md) — Converting plugins to other AI tools
