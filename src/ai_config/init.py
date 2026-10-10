"""Interactive init wizard for ai-config.

This module provides the `ai-config init` command that creates a new
.ai-config/config.yaml file through an interactive wizard experience.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import questionary
import requests
import yaml
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from ai_config.cli_theme import SYMBOLS


class GoBack(Exception):
    """Raised when user presses Escape to go back one step."""

    pass


# Sentinel return value for go-back
GO_BACK: object = object()


@dataclass(frozen=True)
class _ResolvedPath:
    """A resolved path paired with the raw user input for config portability.

    ``resolved`` is used for filesystem operations (exists, discovery).
    ``raw`` is preserved in config.yaml (e.g. ``$DOTS_REPO/plugins``).
    """

    resolved: Path
    raw: str


# ---------------------------------------------------------------------------
# Prompter protocol — injectable interface for all user prompts
# ---------------------------------------------------------------------------


class Prompter(Protocol):
    """Interface for prompting the user during the init wizard.

    Production code uses ``QuestionaryPrompter`` (the default).
    Tests inject a ``ScriptedPrompter`` that returns canned answers.
    All methods return ``GO_BACK`` on Escape, ``None`` on Ctrl+C.
    """

    def select(
        self, message: str, choices: list[str], default: str | None = None
    ) -> str | None | object: ...

    def checkbox(
        self,
        message: str,
        choices: list[tuple[str, str]],
        checked_by_default: bool = True,
    ) -> list[str] | None | object: ...

    def text(self, message: str, default: str = "") -> str | None | object: ...

    def confirm(self, message: str, default: bool = True) -> bool | None | object: ...


# Scope choices with descriptions for user selection
SCOPE_CHOICES: dict[str, str] = {
    "user": "Available in all projects (~/.claude/plugins/)",
    "project": "Only in this project (.claude/plugins/)",
}


def parse_github_repo(input_str: str) -> str | None:
    """Parse a GitHub repo from various input formats.

    Accepts:
    - owner/repo (simple slug)
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo/tree/main/...
    - git@github.com:owner/repo.git

    Args:
        input_str: User input that might be a GitHub repo.

    Returns:
        Normalized owner/repo string, or None if invalid.
    """
    if not input_str:
        return None

    input_str = input_str.strip()

    # Handle simple owner/repo format
    if "/" in input_str and not input_str.startswith(("http", "git@")):
        parts = input_str.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            return input_str
        return None

    # Handle HTTPS URLs: https://github.com/owner/repo[.git][/...]
    if input_str.startswith("https://github.com/"):
        path = input_str.replace("https://github.com/", "")
        path = path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return f"{parts[0]}/{parts[1]}"
        return None

    # Handle SSH URLs: git@github.com:owner/repo.git
    if input_str.startswith("git@github.com:"):
        path = input_str.replace("git@github.com:", "")
        if path.endswith(".git"):
            path = path[:-4]
        parts = path.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[0]}/{parts[1]}"
        return None

    return None


@dataclass(frozen=True)
class PluginInfo:
    """Information about a discovered plugin."""

    id: str
    description: str = ""


@dataclass
class MarketplaceChoice:
    """A marketplace selection during init."""

    name: str
    source: Literal["github", "local"]
    repo: str = ""
    path: str = ""


@dataclass
class PluginChoice:
    """A plugin selection during init."""

    id: str
    marketplace: str
    enabled: bool = True
    scope: str = "user"


@dataclass
class ConversionChoice:
    """Conversion target selection during init."""

    enabled: bool = False
    targets: list[str] = field(default_factory=list)
    scope: str = "project"  # "user" or "project"
    custom_output_dir: Path | None = None  # Override canonical location if set

    def get_output_dir(self, target: str) -> Path:
        """Get the output directory for a specific target.

        Args:
            target: Target tool name (codex, cursor, opencode)

        Returns:
            Path to output directory for this target.
        """
        if self.custom_output_dir:
            return self.custom_output_dir

        # Canonical locations per target and scope
        if self.scope == "user":
            return Path.home()
        else:
            return Path.cwd()

    # For backwards compatibility
    @property
    def output_dir(self) -> Path | None:
        """Get the output directory (for backwards compatibility)."""
        return self.custom_output_dir


def _conversion_output_paths(target: str, base_dir: Path, scope: str) -> list[Path]:
    """Display paths that a conversion target writes under a base output directory."""
    if target == "codex":
        return [base_dir / ".ai-config" / "codex" / "marketplaces"]
    if target == "prime":
        return [base_dir / ".prime" / "agent"]
    if target == "pi" and scope == "user":
        return [base_dir / ".pi" / "agent"]
    return [base_dir / f".{target}"]


@dataclass
class InitConfig:
    """Collected user choices during init wizard."""

    config_path: Path
    marketplaces: list[MarketplaceChoice] = field(default_factory=list)
    plugins: list[PluginChoice] = field(default_factory=list)
    conversion: ConversionChoice | None = None
    run_sync: bool = False


def check_claude_cli() -> tuple[bool, str]:
    """Check if Claude CLI is installed and get version.

    Returns:
        Tuple of (is_installed, version_or_error_message).
    """
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            return True, version
        return False, result.stderr.strip() or "Unknown error"
    except FileNotFoundError:
        return False, "Claude CLI not found"
    except subprocess.TimeoutExpired:
        return False, "Claude CLI timed out"
    except OSError as e:
        return False, str(e)


def get_marketplace_name(path: Path) -> str | None:
    """Get the marketplace name from its marketplace.json file.

    Claude CLI uses the name from marketplace.json, not user-provided names.
    This function reads that name so we can use it correctly.

    Args:
        path: Path to the marketplace directory.

    Returns:
        The marketplace name, or None if it can't be read.
    """
    marketplace_json = path / ".claude-plugin" / "marketplace.json"

    if not marketplace_json.exists():
        return None

    try:
        data = json.loads(marketplace_json.read_text())
        return data.get("name")
    except (json.JSONDecodeError, OSError):
        return None


def get_marketplace_name_from_github(repo: str) -> str | None:
    """Get the marketplace name from a GitHub repo's marketplace.json.

    Fetches .claude-plugin/marketplace.json from the repo and reads the name field.
    Tries 'main' branch first, then 'master'.

    Args:
        repo: GitHub repo in owner/repo format.

    Returns:
        The marketplace name, or None if it can't be fetched.
    """
    branches = ["main", "master"]

    for branch in branches:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/.claude-plugin/marketplace.json"

        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get("name")
        except Exception:
            continue

    return None


def discover_plugins_from_local(path: Path) -> list[PluginInfo]:
    """Discover plugins from a local marketplace directory.

    Reads the .claude-plugin/marketplace.json file to find available plugins.

    Args:
        path: Path to the marketplace directory.

    Returns:
        List of PluginInfo for each plugin found, empty list on error.
    """
    marketplace_json = path / ".claude-plugin" / "marketplace.json"

    if not marketplace_json.exists():
        return []

    try:
        data = json.loads(marketplace_json.read_text())
        plugins_data = data.get("plugins", [])

        return [
            PluginInfo(
                id=p.get("name", ""),
                description=p.get("description", ""),
            )
            for p in plugins_data
            if p.get("name")
        ]
    except (json.JSONDecodeError, OSError):
        return []


def discover_plugins_from_github(repo: str) -> list[PluginInfo]:
    """Discover plugins from a GitHub marketplace repository.

    Fetches the .claude-plugin/marketplace.json file from the repo.
    Tries 'main' branch first, then 'master'.

    Args:
        repo: GitHub repo in owner/repo format.

    Returns:
        List of PluginInfo for each plugin found, empty list on error.
    """
    branches = ["main", "master"]

    for branch in branches:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/.claude-plugin/marketplace.json"

        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                plugins_data = data.get("plugins", [])

                return [
                    PluginInfo(
                        id=p.get("name", ""),
                        description=p.get("description", ""),
                    )
                    for p in plugins_data
                    if p.get("name")
                ]
        except Exception:
            continue

    return []


def find_local_marketplaces(search_path: Path, max_depth: int = 4) -> list[Path]:
    """Search for local marketplace directories.

    Looks for directories containing .claude-plugin/marketplace.json.

    Args:
        search_path: Directory to search from.
        max_depth: Maximum directory depth to search.

    Returns:
        List of paths to marketplace directories (parent of .claude-plugin).
    """
    results: list[Path] = []

    def search_recursive(current: Path, depth: int) -> None:
        if depth > max_depth:
            return

        marketplace_json = current / ".claude-plugin" / "marketplace.json"
        if marketplace_json.exists():
            results.append(current)
            return  # Don't search inside a marketplace

        try:
            for child in current.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    search_recursive(child, depth + 1)
        except PermissionError:
            pass

    search_recursive(search_path, 0)
    return results


def fetch_marketplace_plugins(
    source: Literal["github", "local"],
    repo: str = "",
    path: str = "",
) -> list[PluginInfo]:
    """Fetch available plugins from a marketplace.

    Uses the new discovery functions to read marketplace.json directly.

    Args:
        source: Either 'github' or 'local'.
        repo: GitHub repo in owner/repo format (for github source).
        path: Local filesystem path (for local source).

    Returns:
        List of PluginInfo for each plugin found, empty if fetch fails.
    """
    if source == "github":
        return discover_plugins_from_github(repo)
    else:
        return discover_plugins_from_local(Path(path))


def _add_escape_binding(question: questionary.Question) -> questionary.Question:
    """Add ESC key binding to a questionary prompt.

    Pressing ESC will raise GoBack (go back one step).
    Ctrl+C still raises KeyboardInterrupt (cancel wizard).
    """
    extra = KeyBindings()

    @extra.add(Keys.Escape, eager=True)
    def _escape(event):
        event.app.exit(exception=GoBack(), style="class:aborting")

    original = question.application.key_bindings
    if original is not None:
        question.application.key_bindings = merge_key_bindings([original, extra])
    else:
        question.application.key_bindings = extra
    return question


class QuestionaryPrompter:
    """Production prompter that uses questionary for interactive terminal prompts."""

    def select(
        self, message: str, choices: list[str], default: str | None = None
    ) -> str | None | object:
        question = questionary.select(message, choices=choices, default=default)
        _add_escape_binding(question)
        try:
            return question.ask()
        except GoBack:
            return GO_BACK

    def checkbox(
        self,
        message: str,
        choices: list[tuple[str, str]],
        checked_by_default: bool = True,
    ) -> list[str] | None | object:
        q_choices = [
            questionary.Choice(title=label, value=value, checked=checked_by_default)
            for value, label in choices
        ]
        question = questionary.checkbox(message, choices=q_choices)
        _add_escape_binding(question)
        try:
            return question.ask()
        except GoBack:
            return GO_BACK

    def text(self, message: str, default: str = "") -> str | None | object:
        question = questionary.text(message, default=default)
        _add_escape_binding(question)
        try:
            return question.ask()
        except GoBack:
            return GO_BACK

    def confirm(self, message: str, default: bool = True) -> bool | None | object:
        question = questionary.confirm(message, default=default)
        _add_escape_binding(question)
        try:
            return question.ask()
        except GoBack:
            return GO_BACK


# Module-level convenience functions for backwards compatibility
def prompt_select(
    message: str, choices: list[str], default: str | None = None
) -> str | None | object:
    """Interactive select prompt using questionary."""
    return QuestionaryPrompter().select(message, choices, default)


def prompt_checkbox(
    message: str,
    choices: list[tuple[str, str]],
    checked_by_default: bool = True,
) -> list[str] | None | object:
    """Interactive checkbox prompt using questionary."""
    return QuestionaryPrompter().checkbox(message, choices, checked_by_default)


def prompt_text(message: str, default: str = "") -> str | None | object:
    """Interactive text prompt using questionary."""
    return QuestionaryPrompter().text(message, default)


def prompt_confirm(message: str, default: bool = True) -> bool | None | object:
    """Interactive confirm prompt using questionary."""
    return QuestionaryPrompter().confirm(message, default)


def prompt_path_with_search(
    console: Console,
    prompter: Prompter,
    search_from: Path | None = None,
) -> Path | None | object:
    """Prompt for a local path with optional marketplace search.

    Offers to search for existing marketplace.json files.

    Args:
        console: Rich console for output.
        prompter: Prompter implementation to use.
        search_from: Directory to search from (defaults to cwd).

    Returns:
        Selected path, None if cancelled (Ctrl+C), or GO_BACK if Escape pressed.
    """
    search_path = search_from or Path.cwd()

    # Search for marketplaces in current directory first
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Searching for marketplaces...", total=None)
        found = find_local_marketplaces(search_path)

    # Build choices: found marketplaces + manual entry option
    choices: list[str] = [str(p) for p in found]
    choices.append("Enter a different path (local path, env var like $MY_REPO, etc.)")

    if found:
        console.print(f"  Found {len(found)} marketplace(s) in {search_path}")
        console.print()

    selected = prompter.select(
        "Select a marketplace or enter a different path:",
        choices,
    )

    if selected is GO_BACK:
        return GO_BACK
    if selected is None:
        return None

    assert isinstance(selected, str)
    if not selected.startswith("Enter a different path"):
        return Path(selected)

    # Manual path entry
    console.print()
    console.print("  [dim]Tip: You can use env vars ($MY_REPO) or ~ in paths.[/dim]")
    console.print("  [dim]Env vars are preserved in the config for portability.[/dim]")
    raw_path = prompter.text("Enter local path:")

    if raw_path is GO_BACK:
        return GO_BACK
    if raw_path is None:
        return None

    assert isinstance(raw_path, str)
    resolved = Path(os.path.expandvars(raw_path)).expanduser().resolve()
    return _ResolvedPath(resolved, raw_path)


def generate_config_yaml(init_config: InitConfig) -> str:
    """Generate YAML string from InitConfig.

    Args:
        init_config: The collected configuration choices.

    Returns:
        YAML string ready to write to file.
    """
    # Build marketplaces dict
    marketplaces: dict[str, dict[str, str]] = {}
    for mp in init_config.marketplaces:
        if mp.source == "github":
            marketplaces[mp.name] = {
                "source": "github",
                "repo": mp.repo,
            }
        else:
            marketplaces[mp.name] = {
                "source": "local",
                "path": mp.path,
            }

    # Build plugins list
    plugins: list[dict[str, str | bool]] = []
    for plugin in init_config.plugins:
        plugins.append(
            {
                "id": f"{plugin.id}@{plugin.marketplace}",
                "scope": plugin.scope,
                "enabled": plugin.enabled,
            }
        )

    # Build config structure
    config: dict[str, object] = {
        "version": 1,
        "targets": [
            {
                "type": "claude",
                "config": {
                    "marketplaces": marketplaces,
                    "plugins": plugins,
                },
            }
        ],
    }

    # Add conversion settings if enabled
    if init_config.conversion and init_config.conversion.enabled:
        conversion_cfg: dict[str, object] = {
            "enabled": True,
            "targets": init_config.conversion.targets,
            "scope": init_config.conversion.scope,
        }
        if init_config.conversion.custom_output_dir:
            conversion_cfg["output_dir"] = str(init_config.conversion.custom_output_dir)
        targets = cast(list[dict[str, Any]], config["targets"])
        target_entry = targets[0]
        target_config = cast(dict[str, Any], target_entry["config"])
        target_config["conversion"] = conversion_cfg

    # Generate YAML with nice formatting
    return yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)


def write_config(init_config: InitConfig) -> Path:
    """Write config file, creating directories as needed.

    Args:
        init_config: The configuration to write.

    Returns:
        Path to the written config file.
    """
    config_path = init_config.config_path

    # Create parent directories
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate and write YAML
    yaml_content = generate_config_yaml(init_config)
    config_path.write_text(yaml_content)

    return config_path


def prompt_conversion_targets(
    console: Console,
    prompter: Prompter | None = None,
    default_scope: str = "user",
) -> ConversionChoice | None | object:
    """Prompt user for conversion target selection.

    Args:
        console: Rich console for output.
        prompter: Prompter implementation (defaults to QuestionaryPrompter).
        default_scope: Default scope from plugin selection ("user" or "project").

    Returns:
        ConversionChoice with user selections, None if cancelled (Ctrl+C),
        or GO_BACK if Escape pressed.
    """
    if prompter is None:
        prompter = QuestionaryPrompter()
    console.print()
    console.print("[subheader]Plugin Conversion[/subheader]")
    console.print(
        "ai-config can auto-generate config for other AI coding tools"
        " so they share your Claude plugins."
    )
    console.print()

    # Ask if user wants to convert — GO_BACK here propagates to caller
    wants_conversion = prompter.confirm(
        "Also generate config for Codex, Cursor, OpenCode, or Pi?",
        default=False,
    )

    if wants_conversion is GO_BACK:
        return GO_BACK
    if wants_conversion is None:
        return None  # Cancelled

    if not wants_conversion:
        return ConversionChoice(enabled=False)

    # Sub-steps loop — GO_BACK within these re-prompts conversion, not the caller
    target_choices = [
        ("codex", "Codex (OpenAI) - installable plugin packages and local marketplaces"),
        ("cursor", "Cursor - .cursor/ directory"),
        ("opencode", "OpenCode - .opencode/ directory"),
        ("pi", "Pi - .pi/ skills, prompts, and extensions"),
        ("prime", "Prime Agent Rust - Markdown skills only"),
    ]

    conv_step = 0
    selected_targets: list[str] = []
    scope = default_scope

    while True:
        if conv_step == 0:
            result = prompter.checkbox(
                "Which tools should get the converted config?"
                " (↑↓ move, space toggles, a toggles all):",
                target_choices,
                checked_by_default=True,
            )

            if result is GO_BACK:
                # Go back to the "want conversion?" question — re-enter from the top
                return GO_BACK
            if result is None:
                return None

            assert isinstance(result, list)
            selected_targets = cast(list[str], result)
            if not selected_targets:
                return ConversionChoice(enabled=False)

            # Show where files will be written
            console.print()
            console.print("[info]Converted files will be written to:[/info]")
            base_dir = Path.home() if scope == "user" else Path.cwd()
            for target in selected_targets:
                paths = ", ".join(
                    str(path) for path in _conversion_output_paths(target, base_dir, scope)
                )
                console.print(f"  {SYMBOLS['bullet']} {target}: {paths}")

            conv_step = 1
            continue

        if conv_step == 1:
            use_custom = prompter.confirm(
                "Use custom output directory instead?",
                default=False,
            )

            if use_custom is GO_BACK:
                conv_step = 0
                continue
            if use_custom is None:
                return None

            if not use_custom:
                break  # done

            conv_step = 2
            continue

        if conv_step == 2:
            output_dir_str = prompter.text(
                "Output directory for all converted files:",
                default=".",
            )
            if output_dir_str is GO_BACK:
                conv_step = 1
                continue
            if output_dir_str is None:
                return None
            assert isinstance(output_dir_str, str)
            break  # done

    custom_output_dir = None
    if conv_step == 2:
        assert isinstance(output_dir_str, str)
        custom_output_dir = Path(output_dir_str)

    return ConversionChoice(
        enabled=True,
        targets=selected_targets,
        scope=scope,
        custom_output_dir=custom_output_dir,
    )


def _run_marketplace_loop(
    console: Console,
    prompter: Prompter,
    init_config: InitConfig,
) -> bool | object | None:
    """Run the marketplace collection loop with go-back support.

    Handles the inner sub-steps of marketplace addition:
      sub-step 0: "Add a marketplace?" (GitHub/Local/Skip)
      sub-step 1: Repo URL or local path entry
      sub-step 2: Plugin selection (checkbox)
      sub-step 3: Scope selection
      sub-step 4: "Add another marketplace?"

    Args:
        console: Rich console for output.
        prompter: Prompter implementation to use.
        init_config: Config being built (marketplaces/plugins mutated in place).

    Returns:
        True when the loop completes normally (user chose Skip or declined adding more).
        GO_BACK when the user presses Escape from the first sub-step with no marketplaces yet.
        None when the user presses Ctrl+C to cancel the wizard.
    """
    while True:
        # --- sub-step 0: marketplace source selection ---
        mp_source = prompter.select(
            "Add a marketplace? (marketplaces contain plugins you can install)",
            choices=[
                "GitHub repository",
                "Local directory",
                "Skip (no more marketplaces)",
            ],
            default="GitHub repository",
        )

        if mp_source is GO_BACK:
            if not init_config.marketplaces:
                # Nothing added yet → go back to previous main step
                return GO_BACK
            else:
                # Undo last marketplace + its plugins and re-ask "add another?"
                last_mp = init_config.marketplaces.pop()
                init_config.plugins = [
                    p for p in init_config.plugins if p.marketplace != last_mp.name
                ]
                continue
        if mp_source is None or mp_source == "Skip (no more marketplaces)":
            return True

        console.print()

        # --- sub-step 1: get marketplace details ---
        marketplace: MarketplaceChoice | None = None

        if mp_source == "GitHub repository":
            repo_input = prompter.text("GitHub repo (owner/repo or full URL):")

            if repo_input is GO_BACK:
                continue  # back to sub-step 0
            if repo_input is None:
                return None

            assert isinstance(repo_input, str)
            repo = parse_github_repo(repo_input)
            if repo is None:
                console.print("[warning]Invalid format. Examples:[/warning]")
                console.print("  - owner/repo")
                console.print("  - https://github.com/owner/repo")
                continue

            console.print()
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
                transient=True,
            ) as progress:
                progress.add_task("Reading marketplace name...", total=None)
                actual_name = get_marketplace_name_from_github(repo)

            if actual_name:
                console.print(
                    f"  [info]Found marketplace name in manifest:[/info] [key]{actual_name}[/key]"
                )
                name = actual_name
            else:
                console.print("  [warning]Could not read marketplace name from manifest[/warning]")
                suggested_name = repo.replace("/", "-")
                name = prompter.text("Marketplace name:", default=suggested_name)
                if name is GO_BACK:
                    continue  # back to sub-step 0
                if name is None:
                    return None
                assert isinstance(name, str)

            marketplace = MarketplaceChoice(name=name, source="github", repo=repo)

        else:  # Local directory
            path = prompt_path_with_search(console, prompter)

            if path is GO_BACK:
                continue  # back to sub-step 0
            if path is None:
                return None

            # Unpack result: _ResolvedPath from manual entry, Path from search
            if isinstance(path, _ResolvedPath):
                raw_input = path.raw
                path = path.resolved
            else:
                assert isinstance(path, Path)
                raw_input = str(path)

            if not path.exists():
                console.print(f"[warning]Path does not exist: {path}[/warning]")
                add_anyway = prompter.confirm("Add anyway?", default=True)
                if add_anyway is GO_BACK:
                    continue
                if not add_anyway:
                    continue

            # Auto-discover: if path is not itself a marketplace, search inside
            marketplace_path = path
            config_path_str = raw_input  # what goes into config.yaml
            if path.exists() and not (path / ".claude-plugin" / "marketplace.json").exists():
                nested = find_local_marketplaces(path)
                if len(nested) == 1:
                    marketplace_path = nested[0]
                    console.print(f"  [info]Found marketplace at:[/info] {marketplace_path}")
                elif len(nested) > 1:
                    console.print(f"  Found {len(nested)} marketplace(s) in {path.name}")
                    console.print()
                    mp_choices = [str(p) for p in nested]
                    mp_selected = prompter.select("Select a marketplace:", mp_choices)
                    if mp_selected is GO_BACK:
                        continue  # back to sub-step 0
                    if mp_selected is None:
                        return None
                    assert isinstance(mp_selected, str)
                    marketplace_path = Path(mp_selected)

                # Reconstruct config path: replace resolved prefix with raw input
                # e.g. raw="$DOTS_REPO", resolved="/home/alex/dots",
                #   found="/home/alex/dots/config/plugins" → "$DOTS_REPO/config/plugins"
                try:
                    sub = marketplace_path.relative_to(path)
                    config_path_str = str(Path(raw_input) / sub)
                except ValueError:
                    config_path_str = str(marketplace_path)

            actual_name = get_marketplace_name(marketplace_path)

            if actual_name:
                console.print(
                    f"  [info]Found marketplace name in manifest:[/info] [key]{actual_name}[/key]"
                )
                name = actual_name
            else:
                console.print("  [warning]Could not read marketplace name from manifest[/warning]")
                suggested_name = marketplace_path.name
                name = prompter.text("Marketplace name:", default=suggested_name)
                if name is GO_BACK:
                    continue
                if name is None:
                    return None
                assert isinstance(name, str)

            marketplace = MarketplaceChoice(name=name, source="local", path=config_path_str)

        init_config.marketplaces.append(marketplace)

        # Show marketplace added confirmation
        console.print()
        console.print(
            f"[success]{SYMBOLS['pass']}[/success] Added marketplace: [key]{marketplace.name}[/key]"
        )
        if marketplace.source == "github":
            console.print(f"    Source: github ({marketplace.repo})")
        else:
            console.print(f"    Source: local ({marketplace.path})")

        # --- sub-step 2: plugin selection ---
        # Use resolved path for filesystem operations (marketplace.path may contain env vars)
        resolved_mp_path = (
            str(Path(os.path.expandvars(marketplace.path)).expanduser().resolve())
            if marketplace.source == "local"
            else ""
        )
        while True:  # loop to allow go-back from scope → plugin selection
            console.print()
            console.print("Discovering plugins...")
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
                transient=True,
            ) as progress:
                progress.add_task(f"Fetching plugins from {marketplace.name}...", total=None)
                plugins = fetch_marketplace_plugins(
                    marketplace.source,
                    marketplace.repo,
                    resolved_mp_path,
                )

            if not plugins:
                console.print(
                    f"  [warning]{SYMBOLS['warn']}[/warning] No plugins found in marketplace.json"
                )
                console.print("    (The marketplace was added but contains no plugins yet)")
                break

            console.print(f"  [success]{SYMBOLS['pass']}[/success] Found {len(plugins)} plugin(s):")
            for p in plugins:
                desc = f" - {p.description}" if p.description else ""
                console.print(f"    {SYMBOLS['bullet']} {p.id}{desc}")
            console.print()

            choices = [
                (p.id, f"{p.id} - {p.description}" if p.description else p.id) for p in plugins
            ]

            selected = prompter.checkbox(
                "Select plugins to enable (↑↓ move, space toggles, a toggles all, enter confirms):",
                choices,
                checked_by_default=False,
            )

            if selected is GO_BACK:
                # Remove the marketplace we just added and go back to sub-step 0
                init_config.marketplaces.remove(marketplace)
                break  # breaks inner while, outer while re-prompts sub-step 0
            if selected is None:
                return None

            assert isinstance(selected, list)
            selected = cast(list[str], selected)
            if not selected:
                break  # no plugins selected, move on

            # --- sub-step 3: scope selection ---
            console.print()
            scope_choices = [f"{scope} - {desc}" for scope, desc in SCOPE_CHOICES.items()]
            scope_selection = prompter.select(
                "Where should plugins be installed?",
                choices=scope_choices,
                default=scope_choices[0],
            )

            if scope_selection is GO_BACK:
                # Go back to plugin selection (re-loop inner while)
                continue
            if scope_selection is None:
                return None

            assert isinstance(scope_selection, str)
            selected_scope = scope_selection.split(" - ")[0]

            for plugin_id in selected:
                init_config.plugins.append(
                    PluginChoice(
                        id=plugin_id,
                        marketplace=marketplace.name,
                        enabled=True,
                        scope=selected_scope,
                    )
                )
            break  # done with this marketplace's plugins

        # If marketplace was removed by go-back from plugin selection, restart loop
        if marketplace not in init_config.marketplaces:
            continue

        # --- sub-step 4: add another? ---
        console.print()
        add_another = prompter.confirm("Add another marketplace?", default=False)

        if add_another is GO_BACK:
            # Remove this iteration's plugins, go back to plugin selection
            init_config.plugins = [
                p for p in init_config.plugins if p.marketplace != marketplace.name
            ]
            # Keep marketplace in list but re-do plugin selection
            # Actually, per plan: remove plugins, re-prompt plugin selection
            # We need to re-enter the plugin sub-step for this marketplace
            # Simplest: remove marketplace too and continue (re-prompt sub-step 0)
            init_config.marketplaces.remove(marketplace)
            continue
        if add_another is None or not add_another:
            return True

        console.print()

    # Should not reach here, but just in case
    return True


def run_init_wizard(
    console: Console,
    output_path: Path | None = None,
    prompter: Prompter | None = None,
) -> InitConfig | None:
    """Run the interactive init wizard.

    Uses a step-based state machine so Escape goes back one step
    while Ctrl+C cancels the entire wizard.

    Steps:
        0: Config location
        1: Overwrite check (conditional)
        2: Marketplace loop
        3: Conversion targets (conditional — only if plugins selected)
        4: Preview + confirm write
        5: Run sync? (conditional — only if conversion enabled)

    Args:
        console: Rich console for output.
        output_path: Optional explicit output path.
        prompter: Prompter implementation (defaults to QuestionaryPrompter).

    Returns:
        InitConfig with collected choices, or None if cancelled.
    """
    if prompter is None:
        prompter = QuestionaryPrompter()
    # Header
    console.print()
    console.print(Panel.fit("[header]ai-config init[/header]", border_style="cyan"))
    console.print()

    # Check prerequisites
    console.print("Checking prerequisites...")
    cli_installed, cli_version = check_claude_cli()

    if cli_installed:
        console.print(
            f"  [success]{SYMBOLS['pass']}[/success] Claude CLI installed ({cli_version})"
        )
    else:
        console.print(f"  [error]{SYMBOLS['fail']}[/error] Claude CLI not found")
        console.print()
        console.print("[hint]Install Claude Code: npm install -g @anthropic-ai/claude-code[/hint]")
        return None

    console.print()

    # State accumulated across steps
    config_path: Path | None = None
    init_config: InitConfig | None = None

    step = 0
    while step <= 5:
        # ── Step 0: Config location ──────────────────────────────────
        if step == 0:
            if output_path:
                config_path = output_path
                console.print(f"Config will be created at: {config_path}")
                step += 1
                continue

            location = prompter.select(
                "Where should the config be created?",
                choices=[
                    ".ai-config/config.yaml (this project)",
                    "~/.ai-config/config.yaml (global)",
                ],
                default=".ai-config/config.yaml (this project)",
            )

            if location is GO_BACK:
                return None  # nothing before step 0
            if location is None:
                return None

            assert isinstance(location, str)
            if "this project" in location:
                config_path = Path.cwd() / ".ai-config" / "config.yaml"
            else:
                config_path = Path.home() / ".ai-config" / "config.yaml"

            step += 1
            continue

        # ── Step 1: Overwrite check (conditional) ────────────────────
        if step == 1:
            assert config_path is not None
            if config_path.exists():
                console.print()
                console.print(f"[warning]Config already exists at {config_path}[/warning]")
                overwrite = prompter.confirm("Overwrite existing config?", default=False)
                if overwrite is GO_BACK:
                    step = 0
                    continue
                if not overwrite:
                    return None
            # Either no existing file or user confirmed overwrite
            step += 1
            continue

        # ── Step 2: Marketplace loop ─────────────────────────────────
        if step == 2:
            assert config_path is not None
            console.print()
            console.print("━" * 40)
            console.print()

            init_config = InitConfig(config_path=config_path)

            result = _run_marketplace_loop(console, prompter, init_config)
            if result is GO_BACK:
                step = 0 if not output_path else 1
                continue
            if result is None:
                return None

            step += 1
            continue

        # ── Step 3: Conversion targets (conditional) ─────────────────
        if step == 3:
            assert init_config is not None
            if init_config.plugins:
                console.print()
                console.print("━" * 40)

                default_scope = init_config.plugins[0].scope if init_config.plugins else "user"

                conversion_choice = prompt_conversion_targets(console, prompter, default_scope)
                if conversion_choice is GO_BACK:
                    # Re-prompt conversion (preserves marketplace state)
                    continue
                if conversion_choice is None:
                    return None

                init_config.conversion = conversion_choice
            step += 1
            continue

        # ── Step 4: Preview + confirm write ──────────────────────────
        if step == 4:
            assert init_config is not None
            console.print()
            console.print("━" * 40)
            console.print()

            console.print("[subheader]Config preview:[/subheader]")
            console.print()
            yaml_preview = generate_config_yaml(init_config)
            console.print(yaml_preview)

            if init_config.conversion and init_config.conversion.enabled:
                console.print()
                console.print("[subheader]Conversion preview:[/subheader]")
                console.print(f"  Targets: {', '.join(init_config.conversion.targets)}")
                console.print(f"  Scope: {init_config.conversion.scope}")
                if init_config.conversion.custom_output_dir:
                    console.print(f"  Output: {init_config.conversion.custom_output_dir}")
                else:
                    for target in init_config.conversion.targets:
                        base_dir = init_config.conversion.get_output_dir(target)
                        paths = ", ".join(
                            str(path)
                            for path in _conversion_output_paths(
                                target, base_dir, init_config.conversion.scope
                            )
                        )
                        console.print(f"  {SYMBOLS['bullet']} {target}: {paths}")

            write_ok = prompter.confirm(f"Write config to {init_config.config_path}?", default=True)
            if write_ok is GO_BACK:
                # Go back to conversion if plugins exist, else marketplace loop
                step = 3 if init_config.plugins else 2
                continue
            if not write_ok:
                return None

            step += 1
            continue

        # ── Step 5: Run sync? (conditional) ──────────────────────────
        if step == 5:
            assert init_config is not None
            if init_config.conversion and init_config.conversion.enabled:
                run_sync = prompter.confirm("Run ai-config sync now?", default=True)
                if run_sync is GO_BACK:
                    step = 4
                    continue
                if run_sync is None:
                    return None
                init_config.run_sync = bool(run_sync)

            step += 1
            continue

    return init_config


def create_minimal_config(output_path: Path | None = None) -> InitConfig:
    """Create a minimal config without prompts.

    Args:
        output_path: Optional explicit output path.

    Returns:
        InitConfig with minimal/empty configuration.
    """
    if output_path:
        config_path = output_path
    else:
        config_path = Path.cwd() / ".ai-config" / "config.yaml"

    return InitConfig(config_path=config_path)
