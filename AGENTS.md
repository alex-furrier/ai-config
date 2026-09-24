# ai-config

Declarative plugin manager for Claude Code — with cross-tool plugin conversion.

## Why This Exists

Claude Code plugins let you extend Claude with custom skills, hooks, and MCP servers. Managing them manually (`claude plugin install`, `claude plugin marketplace add`) doesn't scale across machines or teams.

ai-config provides a YAML config file for declarative plugin management. Define what you want, run `ai-config sync`, done. It also converts Claude plugins to other AI coding tools (Codex, Cursor, OpenCode, Pi) via `ai-config convert`.

## Repo Map

```
src/ai_config/
├── cli.py           # Click CLI entry point
├── config.py        # Config file parsing
├── operations.py    # sync, update, status, sync-driven conversion
├── init.py          # Interactive setup wizard
├── types.py         # Frozen dataclasses for config schema
├── watch.py         # File watcher for dev mode
├── adapters/        # Tool-specific adapters (claude.py)
├── converters/      # Plugin conversion pipeline (parse → IR → emit)
│   ├── ir.py            # Tool-agnostic intermediate representation
│   ├── claude_parser.py # Claude plugin → PluginIR
│   ├── emitters.py      # PluginIR → target files (Codex, Cursor, OpenCode, Pi)
│   ├── convert.py       # Orchestrator (ties parse + emit + report)
│   └── report.py        # Conversion reports (JSON, Markdown)
└── validators/      # Validation framework
    ├── component/   # skill, hook, mcp validators
    ├── marketplace/ # marketplace validators
    ├── plugin/      # plugin validators
    └── target/      # converted output validators (codex, cursor, opencode, pi)
tests/
├── unit/            # Fast unit tests (577 tests)
├── integration/     # Integration tests (8 tests, marked)
├── e2e/             # Docker-based E2E tests (79 tests)
│   ├── conftest.py             # Docker fixtures + exec_in_container()
│   ├── tmux_helper.py          # TmuxTestSession for interactive CLI testing
│   ├── test_conversion.py      # Conversion CLI + per-target output
│   ├── test_fresh_install.py   # Sync + config validation
│   ├── test_integration_smoke.py # Full workflow smoke test
│   └── test_tool_validation.py # Interactive CLI introspection via tmux
├── docker/          # Docker test infrastructure
│   ├── Dockerfile.claude-only  # Fast image with Claude Code only
│   ├── Dockerfile.all-tools    # Full image with 5 AI tools
│   └── test_in_docker.py       # CLI for local Docker testing
└── fixtures/
    ├── sample-plugins/complete-plugin/  # Full plugin fixture (skills, hooks, MCP, LSP)
    └── test-marketplace/                # Local marketplace for sync E2E tests
.agents/skills/      # Repo-local agent workflows for ai-config-specific tasks
```

## How to Work Here

1. **Setup**: `uv sync --all-extras`
2. **Make changes**
3. **Validate**: `uv run ruff check src/ && uv run ty check src/ && uv run pytest tests/unit/ -v`
4. **Commit** when all checks pass

## Commands

All commands use `uv run` (or `just` if installed).

| Task | Command |
|------|---------|
| Install deps | `uv sync --all-extras` |
| Lint | `uv run ruff check src/` |
| Format | `uv run ruff format src/` |
| Fix lint | `uv run ruff check src/ --fix` |
| Type check | `uv run ty check src/` |
| All checks | `uv run ruff check src/ && uv run ty check src/ && uv run pytest tests/ -v` |
| Unit tests | `uv run pytest tests/unit/ -v` |
| Single test | `uv run pytest tests/unit/test_foo.py -v` |
| Coverage | `uv run pytest tests/ -v --cov=src/ai_config --cov-report=term-missing` |
| Docs serve | `uv run mkdocs serve` |
| E2E tests (Claude) | `uv run pytest tests/e2e/ -m "e2e and docker" -v` |
| E2E tests (all tools) | `uv run pytest tests/e2e/ -m "e2e and docker and slow" -v` |
| Smoke test | `uv run pytest tests/e2e/test_integration_smoke.py -v` |
| Docker shell | `python tests/docker/test_in_docker.py --shell` |
| Docker build | `python tests/docker/test_in_docker.py --build-only` |

If `just` is available, use `just check` (runs lint + ty + test).

## Code Style

Enforced by tooling—don't memorize rules, run the tools:

- **Formatting**: `ruff format` (line-length 100)
- **Linting**: `ruff check` with autofix
- **Types**: `ty check` with strict mode

Conventions:
- Python 3.10+ syntax
- Type annotations required
- Imports: stdlib → third-party → local (enforced by ruff isort)

## Testing

- Unit tests in `tests/unit/` - fast, no external deps
- Integration tests marked with `@pytest.mark.integration`
- E2E tests in `tests/e2e/` - Docker-based, validates real tool interactions
- Run single test: `uv run pytest tests/unit/test_config.py::test_name -v`

### E2E Testing with Docker

E2E tests run in Docker containers to validate ai-config works with real AI coding tools.

**Two Docker images available:**

| Image | Tools | Use Case |
|-------|-------|----------|
| `all-tools` | Claude, Codex, OpenCode, Cursor, Pi | Default - full multi-tool validation |
| `claude-only` | Claude Code | Fast local testing |

**Running E2E tests locally:**

```bash
# Default: All tools tests
python tests/docker/test_in_docker.py

# Fast: Claude-only tests (quicker builds)
python tests/docker/test_in_docker.py --claude-only

# Debug: Interactive shell
python tests/docker/test_in_docker.py --shell

# Rebuild image from scratch
python tests/docker/test_in_docker.py --rebuild
```

**Tmux requirement (tool validation):**
- `tests/e2e/test_tool_validation.py` uses tmux to exercise interactive CLIs and will **fail loudly** if tmux is missing.
- Install tmux on your host (`brew install tmux` or `apt-get install tmux`) or run via the Docker test runner (tmux is preinstalled in the images).

**Pytest markers:**
- `@pytest.mark.e2e` - All E2E tests
- `@pytest.mark.docker` - Tests requiring Docker
- `@pytest.mark.slow` - Tests using all-tools image

**Multi-tool support:**

ai-config is designed to support multiple AI coding tools. Current installation methods:

| Tool | Install Command | Binary |
|------|-----------------|--------|
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude` |
| OpenAI Codex | `npm install -g @openai/codex` | `codex` |
| OpenCode | `npm install -g opencode-ai` | `opencode` |
| Cursor CLI | `curl -fsSL https://cursor.com/install \| bash` | `cursor-agent` |
| Pi | `npm install -g @earendil-works/pi-coding-agent` | `pi` |

## Releases

**Before merging a PR with user-facing changes:**
1. Update `CHANGELOG.md` under `[Unreleased]` section
2. Use Keep a Changelog format: Added, Changed, Deprecated, Removed, Fixed, Security

**To release a new version:**
1. Move `[Unreleased]` entries to a new version section with the actual release date
2. Update version in `pyproject.toml`
3. Update comparison links at bottom of CHANGELOG.md
4. Create PR, merge to main
5. Create GitHub release with tag `vX.Y.Z` (this auto-publishes to PyPI)

Version is in `pyproject.toml` (currently `0.3.0`). Tags use `v` prefix (e.g., `v0.3.0`).

### PyPI Publishing

Publishing is automated via GitHub Actions (`.github/workflows/publish.yml`):
- Triggered when a GitHub Release is published
- Uses trusted publishing (no API tokens needed)
- Requires PyPI environment configured in repo settings

**One-time setup (repo admin):**
1. Go to PyPI → Account settings → Publishing
2. Add trusted publisher: GitHub repository alex-furrier/ai-config, workflow `publish.yml`, environment `pypi`
3. Go to GitHub repo → Settings → Environments → Create `pypi` environment

## Repo-local Skills

Repo-specific agent workflows live in `.agents/skills/`. Use `ai-config-target-refresh` when auditing or updating target-tool compatibility for Claude, Codex, Cursor, OpenCode, Pi, or future AI coding CLIs.

## Nested Docs

Module-specific docs (auto-discovered by Claude Code):
- `src/AGENTS.md` — Code patterns, module overview, extension guides

Cross-cutting docs in `ai_agent_docs/`:
- `ai_agent_docs/conversion-pipeline.md` — Converter architecture (Parse → IR → Emit)
- `ai_agent_docs/e2e-testing.md` — Docker E2E infrastructure, fixtures, tmux helpers
- `ai_agent_docs/adding-a-target.md` — Step-by-step guide for adding a new conversion target
- `ai_agent_docs/target-compatibility-baseline.md` — Current target-runtime assumptions, version baseline, and validation probes

Durable context contracts and explanations:
- `SPEC.md` — Current correctness envelope and acceptance evidence
- `docs/architecture.md` — Current system boundaries and data flow
- `docs/project-evolution.md` — Evidence-backed project phases and supersession
- `docs/adr/README.md` — Architecture decision index

## Gotchas

- **Claude Code reloads plugins at session start** — after `ai-config sync`, restart Claude Code (use `claude --resume` to continue)
- **Config locations**: `.ai-config/config.yaml` (project) or `~/.ai-config/config.yaml` (global)
- **Scopes**: `user` scope installs to `~/.claude/plugins/`, `project` scope to `.claude/plugins/`
- **Docker E2E tests require Docker** — use `docker info` to verify Docker is running
- **Cursor CLI binary is `cursor-agent`** — not `cursor` (the desktop app uses `cursor`)
- **E2E tests are class-scoped** — tests in the same class share a container, different classes get fresh containers
- **E2E sync configs need absolute paths** — config written to `~/.ai-config/` resolves relative paths from `~`, not the repo. Use `/home/testuser/ai-config/...` in Docker tests.
- **Conversion artifacts are gitignored** — `.codex/`, `.cursor/`, `.opencode/`, `.pi/`, `opencode.json`, `opencode.lsp.json` are local output, not committed
