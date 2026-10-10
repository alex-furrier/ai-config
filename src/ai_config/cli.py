"""CLI for ai-config."""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from threading import Event

import click
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ai_config.cli_theme import SYMBOLS, create_console
from ai_config.config import (
    ConfigError,
    find_config_file,
    load_config,
    validate_marketplace_references,
)
from ai_config.operations import (
    get_status,
    sync_config,
    sync_discrepancies,
    update_plugins,
    verify_sync,
)
from ai_config.scaffold import create_plugin
from ai_config.validators import VALIDATORS, run_validators_sync

console = create_console()
error_console = create_console(stderr=True)

# Command order for --help display (logical workflow order)
COMMAND_ORDER = [
    "init",
    "sync",
    "status",
    "watch",
    "update",
    "doctor",
    "plugin",
    "convert",
    "cache",
]


class OrderedGroup(click.Group):
    """Click group that displays commands in a defined order."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Return commands in logical workflow order."""
        commands = super().list_commands(ctx)
        # Sort by COMMAND_ORDER index, unknown commands go to end
        return sorted(commands, key=lambda x: COMMAND_ORDER.index(x) if x in COMMAND_ORDER else 999)


@click.group(
    cls=OrderedGroup,
    context_settings={"max_content_width": 100},
    epilog="\b\nExamples:\n"
    "  ai-config init                          Set up config interactively\n"
    "  ai-config sync                          Install and sync all plugins\n"
    "  ai-config sync --dry-run                Preview what sync would do\n"
    "  ai-config status                        See what's installed\n"
    "  ai-config doctor                        Check for problems\n"
    "  ai-config convert ./plugin -t codex     Convert a plugin to Codex format\n"
    "\n\b\nGetting started:\n"
    "  ai-config init && ai-config sync && ai-config doctor",
)
@click.version_option(package_name="ai-config-cli")
def main() -> None:
    """ai-config - Declarative plugin manager for Claude Code.

    Define plugins, marketplaces, and MCP servers in a YAML config file.
    Run sync to install everything. Also converts Claude Code plugins to
    other AI tools (Codex, Cursor, OpenCode, Pi) via convert.
    """
    pass


@main.command(
    epilog="\b\nExamples:\n"
    "  ai-config sync --dry-run        Preview changes before applying\n"
    "  ai-config sync --dry-run --json Machine-readable actions and reasons\n"
    "  ai-config sync                  Apply changes\n"
    "  ai-config sync --verify         Apply and verify result\n"
    "  ai-config sync --force          Clear Claude plugin cache + reconvert all\n"
    "  ai-config sync --force-convert   Reconvert only (skip cache clear)",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    help="Path to config file. Default: auto-detected .ai-config/config.yaml.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes.")
@click.option(
    "--fresh",
    "--force",
    is_flag=True,
    help="Clear Claude's plugin cache and reconvert all configured outputs.",
)
@click.option(
    "--force-convert",
    is_flag=True,
    help="Force conversion only (without clearing plugin cache).",
)
@click.option("--verify", is_flag=True, help="Verify installed state matches config after sync.")
@click.option("--json", "as_json", is_flag=True, help="Output actions and reasons as JSON.")
def sync(
    config_path: Path | None,
    dry_run: bool,
    fresh: bool,
    force_convert: bool,
    verify: bool,
    as_json: bool,
) -> None:
    """Install, enable, and disable plugins to match your config file.

    Reads .ai-config/config.yaml (or the path given by -c) and makes
    Claude Code's installed state match. Actions include installing from
    marketplaces, enabling/disabling plugins, and running conversions
    for other tools if configured.
    """
    try:
        config = load_config(config_path)
    except ConfigError as e:
        error_console.print(f"[error]Error loading config:[/error] {e}")
        sys.exit(1)

    # Validate marketplace references
    ref_errors = validate_marketplace_references(config)
    if ref_errors:
        error_console.print("[error]Config validation errors:[/error]")
        for error in ref_errors:
            error_console.print(f"  {SYMBOLS['bullet']} {error}")
        sys.exit(1)

    # --fresh/--force implies --force-convert (full clean rebuild)
    if fresh:
        force_convert = True

    if dry_run:
        if not as_json:
            console.print("[warning]Dry run mode - no changes will be made[/warning]")
        results = sync_config(config, dry_run=dry_run, fresh=fresh, force_convert=force_convert)
    elif as_json:
        results = sync_config(config, dry_run=False, fresh=fresh, force_convert=force_convert)
    else:
        # Use spinner for actual sync operations
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Syncing plugins...", total=None)
            results = sync_config(config, dry_run=dry_run, fresh=fresh, force_convert=force_convert)

    apply_succeeded = all(
        result.success and not result.actions_failed and not result.errors
        for result in results.values()
    )

    if as_json:
        verification_performed = verify and not dry_run and apply_succeeded
        discrepancies = verify_sync(config) if verification_performed else []
        output = {
            "dry_run": dry_run,
            "targets": {
                target_type: {
                    "success": result.success,
                    "planned_actions": [
                        {
                            "action": action.action,
                            "target": action.target,
                            "scope": action.scope,
                            "reason": action.reason,
                        }
                        for action in result.actions_taken
                    ]
                    if dry_run
                    else [],
                    "completed_actions": [
                        {
                            "action": action.action,
                            "target": action.target,
                            "scope": action.scope,
                            "reason": action.reason,
                        }
                        for action in result.actions_taken
                    ]
                    if not dry_run
                    else [],
                    "failed_actions": [
                        {
                            "action": action.action,
                            "target": action.target,
                            "scope": action.scope,
                            "reason": action.reason,
                        }
                        for action in result.actions_failed
                    ],
                    "errors": result.errors,
                }
                for target_type, result in results.items()
            },
            "verification": {
                "requested": verify,
                "performed": verification_performed,
                "discrepancies": discrepancies,
            },
        }
        console.print_json(json.dumps(output))
        if (
            any(
                not result.success or bool(result.actions_failed) or bool(result.errors)
                for result in results.values()
            )
            or discrepancies
        ):
            raise click.exceptions.Exit(1)
        return

    for target_type, result in results.items():
        console.print(f"\n[subheader]Target: {target_type}[/subheader]")

        if result.actions_taken:
            action_state = "Planned" if dry_run else "Completed"
            console.print(f"[subheader]{action_state} actions:[/subheader]")
            table = Table(show_header=True, header_style="bold", box=None)
            table.add_column("Action", style="key")
            table.add_column("Target")
            table.add_column("Scope", style="info")
            table.add_column("Reason", style="dim")

            for action in result.actions_taken:
                table.add_row(
                    action.action,
                    action.target,
                    action.scope or "-",
                    action.reason or "-",
                )
            console.print(table)
        elif (
            result.success
            and not result.actions_failed
            and not result.errors
            and (not verify or dry_run)
        ):
            console.print(f"  [success]{SYMBOLS['pass']}[/success] No changes needed")

        if result.actions_failed:
            console.print("[error]Failed actions:[/error]")
            for action in result.actions_failed:
                console.print(
                    f"  {SYMBOLS['fail']} {action.action} {action.target}: {action.reason}"
                )

        if result.errors:
            console.print("[error]Errors:[/error]")
            for error in result.errors:
                console.print(f"  {SYMBOLS['fail']} {error}")

    # Exit non-zero if any target had errors or failed actions.
    if any(
        not result.success or bool(result.actions_failed) or bool(result.errors)
        for result in results.values()
    ):
        sys.exit(1)

    # Verify if requested
    if verify and not dry_run:
        console.print("\n[subheader]Verification:[/subheader]")
        discrepancies = verify_sync(config)
        if discrepancies:
            console.print("[error]Out of sync:[/error]")
            for d in discrepancies:
                console.print(f"  {SYMBOLS['fail']} {d}")
            sys.exit(1)
        else:
            console.print(f"[success]{SYMBOLS['pass']} All in sync![/success]")


@main.command(
    epilog="\b\nExamples:\n"
    "  ai-config status                List installed plugins\n"
    "  ai-config status --verify       Compare installed state with config\n"
    "  ai-config status --json         Machine-readable installed state\n"
    "  ai-config status -c config.yaml --json  Include planned lifecycle drift",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    help="Path to config file (enables lifecycle drift planning and --verify).",
)
@click.option("--verify", is_flag=True, help="Compare installed state against config file.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def status(
    config_path: Path | None,
    verify: bool,
    as_json: bool,
) -> None:
    """Show installed plugins and registered marketplaces.

    Lists every plugin Claude Code knows about with its version, scope,
    and enabled/disabled state. Add --verify to compare against your
    config and flag anything out of sync.
    """
    result = get_status()
    drift_results = None
    loaded_config = None
    if config_path is not None or verify:
        try:
            loaded_config = load_config(config_path)
            ref_errors = validate_marketplace_references(loaded_config)
            if ref_errors:
                raise ConfigError("; ".join(ref_errors))
            drift_results = sync_config(loaded_config, dry_run=True)
        except ConfigError as error:
            if not as_json:
                error_console.print(f"[error]Cannot inspect drift - config error:[/error] {error}")
                raise click.exceptions.Exit(1) from error
            result.errors.append(f"Cannot inspect drift: {error}")

    drift_discrepancies = sync_discrepancies(drift_results or {})

    if as_json:
        output = {
            "target": result.target_type,
            "plugins": [
                {
                    "id": p.id,
                    "installed": p.installed,
                    "enabled": p.enabled,
                    "scope": p.scope,
                    "version": p.version,
                }
                for p in result.plugins
            ],
            "marketplaces": result.marketplaces,
            "planned_actions": [
                {
                    "action": action.action,
                    "target": action.target,
                    "scope": action.scope,
                    "reason": action.reason,
                }
                for drift_result in (drift_results or {}).values()
                for action in drift_result.actions_taken
                if action.action not in {"noop_codex_plugin", "noop_pi_output"}
            ],
            "errors": result.errors
            + [
                error
                for drift_result in (drift_results or {}).values()
                for error in drift_result.errors
            ],
        }
        console.print_json(json.dumps(output))
        if output["errors"] or drift_discrepancies:
            raise click.exceptions.Exit(1)
        return

    # Display plugins table
    console.print("\n[subheader]Installed Plugins:[/subheader]")
    if result.plugins:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style="key")
        table.add_column("Version")
        table.add_column("Scope")
        table.add_column("Enabled")

        for plugin in result.plugins:
            if plugin.enabled:
                enabled_str = f"[success]{SYMBOLS['pass']}[/success]"
            else:
                enabled_str = f"[error]{SYMBOLS['fail']}[/error]"
            table.add_row(
                plugin.id,
                plugin.version or "-",
                plugin.scope or "-",
                enabled_str,
            )
        console.print(table)
    else:
        console.print("  No plugins installed")

    # Display marketplaces
    console.print("\n[subheader]Registered Marketplaces:[/subheader]")
    if result.marketplaces:
        for mp in result.marketplaces:
            console.print(f"  {SYMBOLS['bullet']} {mp}")
    else:
        console.print("  No marketplaces registered")

    # Show errors if any
    if result.errors:
        console.print("\n[error]Errors:[/error]")
        for error in result.errors:
            console.print(f"  {SYMBOLS['fail']} {error}")

    if drift_results is not None:
        console.print("\n[subheader]Planned lifecycle actions:[/subheader]")
        planned_actions = [
            action
            for drift_result in drift_results.values()
            for action in drift_result.actions_taken
            if action.action not in {"noop_codex_plugin", "noop_pi_output"}
        ]
        if planned_actions:
            table = Table(show_header=True, header_style="bold", box=None)
            table.add_column("Action", style="key")
            table.add_column("Target")
            table.add_column("Reason", style="dim")
            for action in planned_actions:
                table.add_row(action.action, action.target, action.reason or "-")
            console.print(table)
        elif not result.errors and not drift_discrepancies:
            console.print(f"  [success]{SYMBOLS['pass']}[/success] No lifecycle actions needed")
        for drift_result in drift_results.values():
            for action in drift_result.actions_failed:
                console.print(
                    f"  [error]{SYMBOLS['fail']} Failed to inspect "
                    f"{action.action} for {action.target}[/error]"
                )
            for error in drift_result.errors:
                console.print(f"  [error]{SYMBOLS['fail']} {error}[/error]")

    # Verify against config if requested
    if verify:
        console.print("\n[subheader]Verification:[/subheader]")
        try:
            if loaded_config is None:
                load_config(config_path)
            discrepancies = drift_discrepancies
            if result.errors:
                console.print("[error]Cannot verify because status inspection failed.[/error]")
                sys.exit(1)
            if discrepancies:
                console.print("[error]Out of sync:[/error]")
                for d in discrepancies:
                    console.print(f"  {SYMBOLS['fail']} {d}")
                sys.exit(1)
            console.print(f"[success]{SYMBOLS['pass']} All in sync![/success]")
        except ConfigError as e:
            error_console.print(f"[error]Cannot verify - config error:[/error] {e}")
            sys.exit(1)

    if result.errors or drift_discrepancies:
        raise click.exceptions.Exit(1)


@main.command(
    epilog="\b\nExamples:\n"
    "  ai-config update --all              Update everything\n"
    "  ai-config update my-plugin          Update one plugin\n"
    "  ai-config update --all --fresh      Clear cache, then update",
)
@click.option("--all", "update_all", is_flag=True, help="Update all plugins.")
@click.option("--fresh", "--force", is_flag=True, help="Clear cache before updating.")
@click.argument("plugins", nargs=-1)
def update(
    update_all: bool,
    fresh: bool,
    plugins: tuple[str, ...],
) -> None:
    """Update plugins to their latest versions.

    Fetches newest versions from marketplaces and re-installs. Name
    specific plugins to update selectively, or use --all for everything.
    """
    if not update_all and not plugins:
        error_console.print("[error]Specify plugins to update or use --all[/error]")
        sys.exit(1)

    plugin_ids = None if update_all else list(plugins)

    # Use spinner for update operation
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Updating plugins...", total=None)
        result = update_plugins(plugin_ids=plugin_ids, fresh=fresh)

    if result.actions_taken:
        console.print(f"[success]{SYMBOLS['pass']} Updated plugins:[/success]")
        for action in result.actions_taken:
            console.print(f"  {SYMBOLS['arrow']} {action.target}")

    if result.errors:
        console.print("[error]Errors:[/error]")
        for error in result.errors:
            console.print(f"  {SYMBOLS['fail']} {error}")

    if not result.success:
        sys.exit(1)


@main.group()
def cache() -> None:
    """Manage the plugin cache."""
    pass


@cache.command(
    name="clear",
    epilog="\b\nExample:\n  ai-config cache clear && ai-config sync",
)
def cache_clear() -> None:
    """Delete cached plugin data.

    Forces the next sync to re-download everything from marketplaces.
    Use when plugins seem stale or after changing marketplace URLs.
    """
    from ai_config.adapters import claude

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Clearing cache...", total=None)
        result = claude.clear_cache()

    if result.success:
        console.print(f"[success]{SYMBOLS['pass']} Cache cleared successfully[/success]")
    else:
        error_console.print(f"[error]Failed to clear cache:[/error] {result.stderr}")
        sys.exit(1)


@main.group()
def plugin() -> None:
    """Create and manage plugins."""
    pass


@plugin.command(
    name="create",
    epilog="\b\nExamples:\n"
    "  ai-config plugin create my-plugin\n"
    "  ai-config plugin create my-plugin --path ~/plugins",
)
@click.argument("name")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    help="Base directory to create the plugin in. Defaults to current directory.",
)
def plugin_create(name: str, path: Path | None) -> None:
    """Scaffold a new plugin directory.

    Creates NAME/ with a manifest.yaml, skills/, and hooks/
    subdirectories. Add the plugin to your config as a local
    marketplace, then run sync to start using it.
    """
    plugin_dir = create_plugin(name, path)
    console.print(f"[success]{SYMBOLS['pass']} Created plugin scaffold at:[/success] {plugin_dir}")
    console.print("\n[subheader]Next steps:[/subheader]")
    console.print(f"  1. Edit {plugin_dir}/manifest.yaml")
    console.print(f"  2. Add skills to {plugin_dir}/skills/")
    console.print(f"  3. Add hooks to {plugin_dir}/hooks/")


@main.command(
    epilog="\b\nExamples:\n"
    "  ai-config init                       Interactive setup wizard\n"
    "  ai-config init -o my-config.yaml     Custom output path\n"
    "  ai-config init --non-interactive     Empty config, no prompts",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output path for config file. Default: .ai-config/config.yaml.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Create a minimal empty config without prompts.",
)
def init(output: Path | None, non_interactive: bool) -> None:
    """Set up a new ai-config configuration.

    Walks you through adding marketplaces and selecting plugins, then
    writes .ai-config/config.yaml. Use --non-interactive to create a
    minimal empty config you can edit by hand.
    """
    from ai_config.init import create_minimal_config, run_init_wizard, write_config

    if non_interactive:
        # Generate minimal config without prompts
        init_config = create_minimal_config(output)
        path = write_config(init_config)
        console.print(f"[success]{SYMBOLS['pass']} Created minimal config at {path}[/success]")
        console.print("\n[subheader]Next steps:[/subheader]")
        console.print("  1. Edit the config file to add marketplaces and plugins")
        console.print("  2. Run: ai-config sync")
        return

    result = run_init_wizard(console, output)
    if result is None:
        console.print("[warning]Cancelled[/warning]")
        sys.exit(1)

    assert result is not None  # Type narrowing for type checker
    path = write_config(result)
    console.print()
    console.print(f"[success]{SYMBOLS['pass']} Config created at {path}[/success]")
    console.print("\n[subheader]Next steps:[/subheader]")
    console.print("  ai-config sync      # Install plugins")
    console.print("  ai-config doctor    # Verify setup")

    if result.run_sync:
        console.print("\n[subheader]Running sync now...[/subheader]")
        try:
            config = load_config(path)
        except ConfigError as e:
            error_console.print(f"[error]Error loading config:[/error] {e}")
            sys.exit(1)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Syncing plugins...", total=None)
            results = sync_config(config, dry_run=False, fresh=False)

        for target_type, result in results.items():
            console.print(f"\n[subheader]Target: {target_type}[/subheader]")

            if result.actions_taken:
                table = Table(show_header=True, header_style="bold", box=None)
                table.add_column("Action", style="key")
                table.add_column("Target")
                table.add_column("Scope", style="info")

                for action in result.actions_taken:
                    table.add_row(
                        action.action,
                        action.target,
                        action.scope or "-",
                    )
                console.print(table)
            else:
                console.print(f"  [success]{SYMBOLS['pass']}[/success] No changes needed")

            if result.errors:
                console.print("[error]Errors:[/error]")
                for error in result.errors:
                    console.print(f"  {SYMBOLS['fail']} {error}")


@main.command(
    epilog="\b\nExamples:\n"
    "  ai-config doctor                             Check plugin health\n"
    "  ai-config doctor --verbose                   Show passing checks too\n"
    "  ai-config doctor --category component        Run only component checks\n"
    "  ai-config doctor --target codex ./out        Validate Codex conversion\n"
    "  ai-config doctor --json                      Machine-readable output",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    help="Path to config file. Default: auto-detected .ai-config/config.yaml.",
)
@click.option(
    "--category",
    type=click.Choice(list(VALIDATORS.keys())),
    multiple=True,
    help="Run only specific validation categories (repeatable).",
)
@click.option(
    "--target",
    "-t",
    type=click.Choice(["codex", "cursor", "opencode", "pi", "prime", "all"]),
    help="Validate converted output for a target tool instead of plugin health.",
)
@click.argument("output_dir", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--verbose", "-v", is_flag=True, help="Show all checks, including passed ones.")
def doctor(
    config_path: Path | None,
    category: tuple[str, ...],
    target: str | None,
    output_dir: Path | None,
    as_json: bool,
    verbose: bool,
) -> None:
    """Check for problems with plugins, marketplaces, and components.

    Runs validation checks and reports pass/fail/warn for each. Failed
    checks include a fix_hint with remediation steps.

    \b
    Default mode checks marketplace registration, plugin install state,
    skill file validity, hook configuration, and MCP server setup.

    \b
    With --target, validates converted output instead: directory
    structure, skill files, and tool-specific config files. Pass the
    output directory as an argument (defaults to current directory).
    """
    # Handle target validation mode
    if target:
        _doctor_target_mode(target, output_dir, as_json, verbose)
        return

    # Original config validation mode
    from ai_config.cli_render import render_doctor_output

    try:
        actual_config_path = find_config_file(config_path)
        config = load_config(config_path)
    except ConfigError as e:
        error_console.print(f"[error]Error loading config:[/error] {e}")
        sys.exit(1)

    # Run validators
    categories_to_run = list(category) if category else None
    reports = run_validators_sync(config, actual_config_path, categories_to_run)

    if as_json:
        output = {
            "reports": {
                cat: {
                    "target": report.target,
                    "passed": report.passed,
                    "has_warnings": report.has_warnings,
                    "results": [
                        {
                            "check_name": r.check_name,
                            "status": r.status,
                            "message": r.message,
                            "details": r.details,
                            "fix_hint": r.fix_hint,
                        }
                        for r in report.results
                    ],
                }
                for cat, report in reports.items()
            }
        }
        console.print_json(json.dumps(output))
        return

    console.print()
    console.print(Panel.fit("[header]ai-config doctor[/header]", border_style="cyan"))
    console.print()

    _total_pass, _total_warn, total_fail = render_doctor_output(reports, config, console, verbose)

    if total_fail > 0:
        sys.exit(1)


def _doctor_target_mode(
    target: str,
    output_dir: Path | None,
    as_json: bool,
    verbose: bool,
) -> None:
    """Run doctor in target validation mode.

    Args:
        target: Target tool to validate (codex, cursor, opencode, all)
        output_dir: Directory containing converted output
        as_json: Output as JSON
        verbose: Show all checks including passed
    """
    from ai_config.validators.base import ValidationResult
    from ai_config.validators.target import get_output_validator

    if output_dir is None:
        output_dir = Path.cwd()

    # Determine which targets to validate
    if target == "all":
        targets = ["codex", "cursor", "opencode", "pi", "prime"]
    else:
        targets = [target]

    # Collect all results
    all_results: dict[str, list[ValidationResult]] = {}
    total_pass = 0
    total_warn = 0
    total_fail = 0

    for t in targets:
        validator = get_output_validator(t)
        results = validator.validate_all(output_dir)
        all_results[t] = results

        for r in results:
            if r.status == "pass":
                total_pass += 1
            elif r.status == "warn":
                total_warn += 1
            else:
                total_fail += 1

    # Output as JSON
    if as_json:
        output = {
            "reports": {
                t: {
                    "target": t,
                    "passed": all(r.status != "fail" for r in results),
                    "has_warnings": any(r.status == "warn" for r in results),
                    "results": [
                        {
                            "check_name": r.check_name,
                            "status": r.status,
                            "message": r.message,
                            "details": r.details,
                            "fix_hint": r.fix_hint,
                        }
                        for r in results
                    ],
                }
                for t, results in all_results.items()
            }
        }
        console.print_json(json.dumps(output))
        if total_fail > 0:
            sys.exit(1)
        return

    # Render output
    console.print()
    console.print(
        Panel.fit(
            f"[header]ai-config doctor --target {target}[/header]",
            border_style="cyan",
        )
    )
    console.print()
    console.print(f"[subheader]Validating: {output_dir}[/subheader]")
    console.print()

    for t, results in all_results.items():
        console.print(f"[header]{t.upper()} Validation[/header]")
        console.print()

        for r in results:
            if r.status == "pass" and not verbose:
                continue

            if r.status == "pass":
                icon = SYMBOLS["pass"]
                style = "success"
            elif r.status == "warn":
                icon = SYMBOLS["warn"]
                style = "warning"
            else:
                icon = SYMBOLS["fail"]
                style = "error"

            console.print(f"  [{style}]{icon} {r.message}[/{style}]")

            if r.details and (r.status != "pass" or verbose):
                console.print(f"    [dim]{r.details}[/dim]")
            if r.fix_hint and r.status == "fail":
                console.print(f"    [hint]Fix: {r.fix_hint}[/hint]")

        console.print()

    # Summary
    console.print("[subheader]Summary:[/subheader]")
    console.print(f"  [success]{SYMBOLS['pass']} Passed: {total_pass}[/success]")
    if total_warn > 0:
        console.print(f"  [warning]{SYMBOLS['warn']} Warnings: {total_warn}[/warning]")
    if total_fail > 0:
        console.print(f"  [error]{SYMBOLS['fail']} Failed: {total_fail}[/error]")

    if total_fail > 0:
        sys.exit(1)


@main.command(
    epilog="\b\nExamples:\n"
    "  ai-config convert ./my-plugin                    Convert to all targets\n"
    "  ai-config convert ./my-plugin -t codex           Codex only\n"
    "  ai-config convert ./my-plugin -t cursor -o ./out Custom output directory\n"
    "  ai-config convert ./my-plugin --dry-run          Preview without writing\n"
    "  ai-config convert ./my-plugin --scope user       Write to ~/ instead of ./",
)
@click.argument("plugin_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--target",
    "-t",
    "targets",
    multiple=True,
    type=click.Choice(["codex", "cursor", "opencode", "pi", "prime", "all"]),
    default=["all"],
    show_default=True,
    help="Target tool(s) to convert to (repeatable).",
)
@click.option(
    "--output",
    "-o",
    "output_dir",
    type=click.Path(path_type=Path),
    help="Output directory. Default: ~/ for user scope, ./ for project scope.",
)
@click.option(
    "--scope",
    type=click.Choice(["user", "project"]),
    default="project",
    show_default=True,
    help="Where to write output when -o is not set.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview what would be written without creating files.",
)
@click.option(
    "--best-effort",
    is_flag=True,
    help="Keep going even if some components fail to convert.",
)
@click.option(
    "--format",
    "report_format",
    type=click.Choice(["summary", "markdown", "json"]),
    default="summary",
    show_default=True,
    help="Output format for the conversion report.",
)
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path),
    help="Write conversion report to a file.",
)
@click.option(
    "--report-format",
    "report_file_format",
    type=click.Choice(["json", "markdown"]),
    default="json",
    show_default=True,
    help="Format for the report file (used with --report).",
)
def convert(
    plugin_path: Path,
    targets: tuple[str, ...],
    output_dir: Path | None,
    scope: str,
    dry_run: bool,
    best_effort: bool,
    report_format: str,
    report_path: Path | None,
    report_file_format: str,
) -> None:
    """Convert a Claude Code plugin to other AI coding tools.

    Reads PLUGIN_PATH and emits equivalent config for the target
    tool(s). Components that can't convert exactly are flagged as
    degraded or skipped in the report.

    \b
    Targets:
      codex      OpenAI Codex (installable plugin package with skills, hooks, MCP)
      cursor     Cursor (skills, commands, hooks, MCP)
      opencode   OpenCode (skills, commands, MCP, LSP)
      pi         Pi (skills, prompt templates, supported hook extensions)
      prime      Prime Agent Rust (Markdown skills only)
      all        All of the above (default)
    """
    from ai_config.converters import InstallScope, TargetTool, convert_plugin

    # Resolve targets
    if "all" in targets:
        target_list = [
            TargetTool.CODEX,
            TargetTool.CURSOR,
            TargetTool.OPENCODE,
            TargetTool.PI,
            TargetTool.PRIME,
        ]
    else:
        target_list = [TargetTool(t) for t in targets]

    # Use scope-based output resolution if no output specified
    if output_dir is None:
        output_dir = Path.home() if scope == "user" else Path.cwd()
    install_scope = InstallScope(scope)

    # Perform conversion or report its ownership-aware plan.
    console.print()
    title = "ai-config convert (preview)" if dry_run else "ai-config convert"
    console.print(Panel.fit(f"[header]{title}[/header]", border_style="cyan"))
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Converting plugin...", total=None)
        reports = convert_plugin(
            plugin_path=plugin_path,
            targets=target_list,
            output_dir=output_dir,
            scope=install_scope,
            dry_run=dry_run,
            best_effort=best_effort,
        )

    # Display results
    any_errors = False
    for target, report in reports.items():
        console.print(f"\n[subheader]═══ {target.value.upper()} ═══[/subheader]")

        if report_format == "json":
            console.print_json(report.to_json())
        elif report_format == "markdown":
            console.print(report.to_markdown())
        else:
            # Summary format
            console.print(report.summary())

            # Show component details
            if report.components_converted:
                console.print(
                    f"\n[success]Converted ({len(report.components_converted)}):[/success]"
                )
                for comp in report.components_converted:
                    notes = f" - {comp.notes}" if comp.notes else ""
                    console.print(f"  {SYMBOLS['pass']} {comp.kind}:{comp.name}{notes}")

            if report.components_degraded:
                console.print(f"\n[warning]Degraded ({len(report.components_degraded)}):[/warning]")
                for comp in report.components_degraded:
                    notes = f" - {comp.notes}" if comp.notes else ""
                    console.print(f"  {SYMBOLS['warn']} {comp.kind}:{comp.name}{notes}")

            if report.components_skipped:
                console.print(f"\n[error]Skipped ({len(report.components_skipped)}):[/error]")
                for comp in report.components_skipped:
                    notes = f" - {comp.notes}" if comp.notes else ""
                    console.print(f"  {SYMBOLS['fail']} {comp.kind}:{comp.name}{notes}")

            # Show files
            if report.files_written:
                label = "Files planned" if dry_run else "File actions"
                console.print(f"\n[info]{label} ({len(report.files_written)}):[/info]")
                for f in report.files_written:
                    reason = f" - {f.reason}" if f.reason else ""
                    console.print(f"  {SYMBOLS['arrow']} {f.action}: {f.path}{reason}")

        if report.has_errors():
            any_errors = True

    # Write report files if requested
    if report_path:
        multi_target = len(reports) > 1
        for target, report in reports.items():
            report_file = (
                _resolve_report_path(report_path, target.value, report_file_format)
                if multi_target
                else report_path
            )
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report.write_to_file(report_file, format=report_file_format)

    console.print()
    if any_errors and not best_effort:
        console.print("[error]Conversion completed with errors[/error]")
        sys.exit(1)
    else:
        console.print(f"[success]{SYMBOLS['pass']} Conversion complete![/success]")


def _resolve_report_path(base: Path, target: str, report_format: str) -> Path:
    """Resolve report output path for multi-target conversions."""
    suffix = ".md" if report_format == "markdown" else ".json"
    if base.is_dir():
        return base / f"conversion-{target}{suffix}"
    if target:
        stem = base.stem
        ext = base.suffix if base.suffix else suffix
        if len(stem) == 0:
            return Path(f"conversion-{target}{suffix}")
        return base.with_name(f"{stem}-{target}{ext}")
    return base


@main.command(
    epilog="\b\nExamples:\n"
    "  ai-config watch                 Start watching with defaults\n"
    "  ai-config watch --dry-run       See what would sync, don't apply\n"
    "  ai-config watch --verbose       Show individual file events\n"
    "  ai-config watch --debounce 3    Wait 3 seconds between syncs",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    help="Path to config file. Default: auto-detected .ai-config/config.yaml.",
)
@click.option(
    "--debounce",
    type=float,
    default=1.5,
    show_default=True,
    help="Seconds to wait after a change before syncing.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report changes without syncing.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show individual file change events.",
)
def watch(
    config_path: Path | None,
    debounce: float,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Watch for file changes and auto-sync.

    Monitors your config file and local plugin directories. When a file
    changes, waits for the debounce period, then runs sync. Useful
    during plugin development. Press Ctrl+C to stop.

    \b
    Note: Claude Code loads plugins at session start. After watch syncs
    new changes, restart Claude Code for them to take effect. Use
    "claude --resume" to keep your session.
    """
    from ai_config.watch import FileChange, collect_watch_paths, run_watch_loop

    try:
        actual_config_path = find_config_file(config_path)
        config = load_config(config_path)
    except ConfigError as e:
        error_console.print(f"[error]Error loading config:[/error] {e}")
        sys.exit(1)

    watch_config = collect_watch_paths(config, actual_config_path)

    # Display watch info
    console.print()
    console.print(Panel.fit("[header]ai-config watch[/header]", border_style="cyan"))
    console.print()

    console.print("[subheader]Watching:[/subheader]")
    console.print(f"  {SYMBOLS['bullet']} Config: {watch_config.config_path}")
    for plugin_dir in watch_config.plugin_directories:
        console.print(f"  {SYMBOLS['bullet']} Plugin: {plugin_dir}")

    if not watch_config.plugin_directories:
        console.print("  [info](no local plugin directories)[/info]")

    console.print()
    if dry_run:
        console.print("[warning]Dry run: true[/warning]")
    console.print("[info]Press Ctrl+C to stop[/info]")
    console.print()
    console.print(
        "[dim]Note: Claude Code loads plugins at session start. "
        "After changes sync, restart Claude Code to apply them.[/dim]"
    )
    console.print("[dim]Tip: Use 'claude --resume' to continue your previous session.[/dim]")
    console.print()

    # Track sync count
    sync_count = 0

    def on_changes(changes: list[FileChange]) -> None:
        """Handle detected changes."""
        nonlocal sync_count
        sync_count += 1

        config_changes = [c for c in changes if c.change_type == "config"]
        plugin_changes = [c for c in changes if c.change_type == "plugin_directory"]

        console.print(f"[subheader]Changes detected (batch #{sync_count}):[/subheader]")
        if config_changes:
            console.print(f"  Config: {len(config_changes)} change(s)")
            if verbose:
                for c in config_changes:
                    console.print(f"    {SYMBOLS['arrow']} {c.event_type}: {c.path}")
        if plugin_changes:
            console.print(f"  Plugins: {len(plugin_changes)} change(s)")
            if verbose:
                for c in plugin_changes:
                    console.print(f"    {SYMBOLS['arrow']} {c.event_type}: {c.path}")

        # Reload config if it changed
        try:
            current_config = load_config(config_path)
        except ConfigError as e:
            error_console.print(f"[error]Config error:[/error] {e}")
            return

        if dry_run:
            console.print("[warning]Dry run - no sync performed[/warning]")
            return

        # Sync
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Syncing...", total=None)
            results = sync_config(current_config, dry_run=False, fresh=False)

        total_actions = sum(len(r.actions_taken) for r in results.values())
        if total_actions > 0:
            console.print(f"[success]{SYMBOLS['pass']} Synced {total_actions} action(s)[/success]")
        else:
            console.print(f"[info]{SYMBOLS['pass']} No sync needed[/info]")

        for result in results.values():
            if result.errors:
                for error in result.errors:
                    error_console.print(f"  [error]{SYMBOLS['fail']}[/error] {error}")

        console.print()

    # Setup signal handler for graceful shutdown
    stop_event = Event()

    def signal_handler(signum: int, frame: object) -> None:
        console.print("\n[info]Stopping...[/info]")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run the watch loop
    run_watch_loop(
        watch_config=watch_config,
        on_changes=on_changes,
        stop_event=stop_event,
        debounce_seconds=debounce,
    )

    console.print(f"[success]{SYMBOLS['pass']} Watch stopped[/success]")


if __name__ == "__main__":
    main()
