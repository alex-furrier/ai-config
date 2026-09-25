#!/usr/bin/env python3
"""Exercise the public ``ai-config sync`` Codex lifecycle in isolated homes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from probe_codex_plugin_package import (
    SENSITIVE_ENV_VARS,
    load_json,
    make_unrelated_marketplace,
    run,
    set_enabled,
)

from ai_config.semver import SemanticVersion

_MANAGED_PLUGIN_ID = "dev-tools@ai-config-dev-tools"
_SOURCE_LESS_MARKETPLACE = "source-less-marketplace"
_SOURCE_LESS_PLUGIN_ID = f"source-less-plugin@{_SOURCE_LESS_MARKETPLACE}"


def _write_fake_claude(path: Path, plugin_path: Path, marketplace_path: Path) -> None:
    script = f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

plugin_path = Path({str(plugin_path)!r})
marketplace_path = Path({str(marketplace_path)!r})
args = sys.argv[1:]
if args == ["plugin", "marketplace", "list", "--json"]:
    print(json.dumps([{{
        "name": "probe-market",
        "source": "directory",
        "path": str(marketplace_path),
        "installLocation": str(marketplace_path),
    }}]))
elif args == ["plugin", "list", "--json"]:
    manifest = json.loads((plugin_path / ".claude-plugin/plugin.json").read_text())
    print(json.dumps([{{
        "id": "dev-tools@probe-market",
        "version": manifest["version"],
        "scope": "user",
        "enabled": True,
        "installPath": str(plugin_path),
    }}]))
else:
    print(f"unexpected fake Claude command: {{args}}", file=sys.stderr)
    raise SystemExit(2)
"""
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_config(path: Path, marketplace_path: Path, output_path: Path, *, enabled: bool) -> None:
    plugins = (
        [{"id": "dev-tools@probe-market", "scope": "user", "enabled": True}] if enabled else []
    )
    payload = {
        "version": 1,
        "targets": [
            {
                "type": "claude",
                "config": {
                    "marketplaces": {
                        "probe-market": {
                            "source": "local",
                            "path": str(marketplace_path),
                        }
                    },
                    "plugins": plugins,
                    "conversion": {
                        "enabled": True,
                        "targets": ["codex"],
                        "scope": "user",
                        "output_dir": str(output_path),
                    },
                },
            }
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def _write_source_less_catalog(codex_home: Path) -> Path:
    marketplace = codex_home / ".tmp/plugins"
    plugin = marketplace / "plugins/source-less-plugin"
    (marketplace / ".agents/plugins").mkdir(parents=True)
    (plugin / ".codex-plugin").mkdir(parents=True)
    (marketplace / ".agents/plugins/marketplace.json").write_text(
        json.dumps(
            {
                "name": _SOURCE_LESS_MARKETPLACE,
                "plugins": [
                    {
                        "name": "source-less-plugin",
                        "source": {
                            "source": "local",
                            "path": "./plugins/source-less-plugin",
                        },
                    }
                ],
            }
        )
    )
    (plugin / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "source-less-plugin",
                "version": "1.0.0",
                "description": "unrelated source-less catalog state",
            }
        )
    )
    return marketplace


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run_ai_config(config: Path, env: dict[str, str], *extra: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "ai_config", "sync", "--config", str(config), "--json", *extra],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
        start_new_session=os.name == "posix",
    )
    if result.returncode != 0:
        raise AssertionError(
            f"public ai-config sync failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    payload: object = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("public ai-config sync did not return a JSON object")
    return payload


def _run_status(
    config: Path, env: dict[str, str], *, expected_returncode: int = 0
) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "ai_config", "status", "--config", str(config), "--json"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
        start_new_session=os.name == "posix",
    )
    if result.returncode != expected_returncode:
        raise AssertionError(
            f"public ai-config status returned {result.returncode}, expected "
            f"{expected_returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    payload: object = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("public ai-config status did not return a JSON object")
    return payload


def _actions(payload: dict[str, object]) -> dict[str, str]:
    targets = payload.get("targets")
    if not isinstance(targets, dict):
        raise AssertionError(f"missing targets in sync result: {payload}")
    claude = targets.get("claude")
    if not isinstance(claude, dict):
        raise AssertionError(f"missing Claude target in sync result: {payload}")
    actions = claude.get("completed_actions")
    if not isinstance(actions, list):
        raise AssertionError(f"missing completed actions in sync result: {payload}")
    parsed: dict[str, str] = {}
    for action in actions:
        if not isinstance(action, dict):
            raise AssertionError(f"invalid action in sync result: {action}")
        name = action.get("action")
        reason = action.get("reason")
        if not isinstance(name, str) or not isinstance(reason, str):
            raise AssertionError(f"invalid action fields in sync result: {action}")
        parsed[name] = reason
    return parsed


def _assert_prompt_marker(codex: str, env: dict[str, str], output: Path, marker: str) -> None:
    prompt = run(codex, ["-C", str(output), "debug", "prompt-input", "probe"], env).stdout
    if marker not in prompt:
        raise AssertionError(f"public sync package marker was not discovered: {marker}")


def _source_less_catalog_state(codex: str, env: dict[str, str]) -> set[tuple[str, str]]:
    marketplaces = load_json(run(codex, ["plugin", "marketplace", "list", "--json"], env)).get(
        "marketplaces"
    )
    if not isinstance(marketplaces, list):
        raise AssertionError("Codex source-less marketplace probe did not return an array")
    marketplace_names = {
        entry["name"]
        for entry in marketplaces
        if isinstance(entry, dict)
        and "marketplaceSource" not in entry
        and isinstance(entry.get("name"), str)
    }
    available = load_json(run(codex, ["plugin", "list", "--available", "--json"], env)).get(
        "available"
    )
    if not isinstance(available, list):
        raise AssertionError("Codex source-less plugin probe did not return an available array")
    catalog_pairs = {
        (entry["marketplaceName"], entry["pluginId"])
        for entry in available
        if isinstance(entry, dict)
        and "marketplaceSource" not in entry
        and entry.get("marketplaceName") in marketplace_names
        and isinstance(entry.get("marketplaceName"), str)
        and isinstance(entry.get("pluginId"), str)
    }
    return catalog_pairs


def probe(codex: str) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[2]
    source_fixture = repo_root / "tests/fixtures/sample-plugins/complete-plugin"
    with tempfile.TemporaryDirectory(prefix="ai-config-public-sync-") as temporary:
        root = Path(temporary)
        home = root / "home"
        codex_home = home / ".codex"
        output = root / "output"
        marketplace = root / "claude-marketplace"
        plugin = marketplace / "dev-tools"
        installed_plugin = root / "claude-cache" / "dev-tools"
        config = root / "config.yaml"
        bin_dir = root / "bin"
        codex_home.mkdir(parents=True)
        bin_dir.mkdir()
        shutil.copytree(source_fixture, plugin)
        local_skill = plugin / "skills/code-review/SKILL.md"
        local_skill.write_text(
            local_skill.read_text().replace(
                "description: Review code for best practices, security issues, and style violations.",
                "description: Review code for best practices, security issues, and style violations. LOCAL_AUTHORITY_MARKER.",
            )
        )
        shutil.copytree(plugin, installed_plugin)
        installed_skill = installed_plugin / "skills/code-review/SKILL.md"
        installed_skill.write_text(
            installed_skill.read_text().replace(
                "LOCAL_AUTHORITY_MARKER", "STALE_INSTALLED_COPY_MARKER"
            )
        )
        (marketplace / ".claude-plugin").mkdir()
        (marketplace / ".claude-plugin/marketplace.json").write_text(
            json.dumps(
                {
                    "name": "probe-market",
                    "plugins": [{"name": "dev-tools", "source": "./dev-tools"}],
                }
            )
        )
        _write_fake_claude(bin_dir / "claude", installed_plugin, marketplace)
        _write_config(config, marketplace, output, enabled=True)
        (codex_home / "config.toml").write_text('model = "preserve-public-sync"\n')

        env = {key: value for key, value in os.environ.items() if key not in SENSITIVE_ENV_VARS}
        env.update(
            {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "PATH": f"{bin_dir}{os.pathsep}{Path(codex).parent}{os.pathsep}{env.get('PATH', '')}",
            }
        )

        version_output = run(codex, ["--version"], env).stdout.strip()
        version = SemanticVersion.parse(
            version_output.removeprefix("codex-cli "), context="Codex CLI version"
        )

        unrelated_path, unrelated_id = make_unrelated_marketplace(root)
        load_json(run(codex, ["plugin", "marketplace", "add", unrelated_path, "--json"], env))
        load_json(run(codex, ["plugin", "add", unrelated_id, "--json"], env))
        set_enabled(codex_home / "config.toml", unrelated_id, False)
        source_less_catalog = _write_source_less_catalog(codex_home)
        source_less_files = _tree_snapshot(source_less_catalog)
        source_less_catalog_state = _source_less_catalog_state(codex, env)
        source_less_catalog_entry = (_SOURCE_LESS_MARKETPLACE, _SOURCE_LESS_PLUGIN_ID)
        if (version.major, version.minor) in {(0, 144), (0, 145), (0, 146), (0, 147)}:
            if source_less_catalog_entry not in source_less_catalog_state:
                raise AssertionError(
                    "Codex did not expose the constructed source-less catalog state: "
                    f"{source_less_catalog_entry}"
                )
        elif (version.major, version.minor) in {(0, 148), (0, 149), (0, 153), (0, 156), (0, 157)}:
            if source_less_catalog_state:
                raise AssertionError(
                    f"Codex {version.major}.{version.minor} unexpectedly exposed directly seeded "
                    "source-less catalog state"
                )
        else:
            raise AssertionError(f"unsupported Codex public-sync probe version: {version_output}")

        first_payload = _run_ai_config(config, env, "--force", "--verify")
        first = _actions(first_payload)
        if not {"register_codex_marketplace", "install_codex_plugin"} <= set(first):
            raise AssertionError(f"first public sync actions were incomplete: {first}")
        verification = first_payload.get("verification")
        if not isinstance(verification, dict) or verification.get("performed") is not True:
            raise AssertionError(f"first public sync did not perform verification: {verification}")
        if verification.get("discrepancies") != []:
            raise AssertionError(f"first public sync reported false drift: {verification}")
        _assert_prompt_marker(codex, env, output, "dev-tools:code-review")
        _assert_prompt_marker(codex, env, output, "LOCAL_AUTHORITY_MARKER")
        prompt = run(codex, ["-C", str(output), "debug", "prompt-input", "probe"], env).stdout
        if "STALE_INSTALLED_COPY_MARKER" in prompt:
            raise AssertionError("public sync converted Claude's stale installed copy")

        unchanged = _actions(_run_ai_config(config, env))
        if set(unchanged) != {"noop_codex_plugin"}:
            raise AssertionError(f"unchanged public sync did not converge to no-op: {unchanged}")

        generated_skill = (
            output
            / ".ai-config/codex/marketplaces/ai-config-dev-tools/plugins/dev-tools"
            / "skills/code-review/SKILL.md"
        )
        generated_bytes = generated_skill.read_bytes()
        generated_skill.write_text("tampered generated output\n")
        repaired_output = _actions(_run_ai_config(config, env))
        if "update_codex_plugin" not in repaired_output:
            raise AssertionError(
                f"generated-output tampering did not trigger repair: {repaired_output}"
            )
        if generated_skill.read_bytes() != generated_bytes:
            raise AssertionError("normal sync did not restore tampered generated output")

        manifest_path = plugin / ".claude-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "1.1.0"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        skill_path = plugin / "skills/code-review/SKILL.md"
        skill_path.write_text(
            skill_path.read_text().replace(
                "description: Review code for best practices, security issues, and style violations. LOCAL_AUTHORITY_MARKER.",
                "description: PUBLIC_SYNC_UPDATE_MARKER.",
            )
        )
        updated = _actions(_run_ai_config(config, env))
        if "update_codex_plugin" not in updated:
            raise AssertionError(f"source update did not report a public update: {updated}")
        _assert_prompt_marker(codex, env, output, "PUBLIC_SYNC_UPDATE_MARKER")

        config_path = codex_home / "config.toml"
        set_enabled(config_path, _MANAGED_PLUGIN_ID, False)
        status = _run_status(config, env, expected_returncode=1)
        planned = status.get("planned_actions")
        if not isinstance(planned, list) or not any(
            isinstance(action, dict) and action.get("action") == "reinstall_codex_plugin"
            for action in planned
        ):
            raise AssertionError(f"status did not report disabled-plugin drift: {status}")
        repaired_enabled = _actions(_run_ai_config(config, env))
        if "reinstall_codex_plugin" not in repaired_enabled:
            raise AssertionError(f"sync did not repair disabled-plugin drift: {repaired_enabled}")

        load_json(run(codex, ["plugin", "remove", _MANAGED_PLUGIN_ID, "--json"], env))
        repaired_missing = _actions(_run_ai_config(config, env))
        if "install_codex_plugin" not in repaired_missing:
            raise AssertionError(f"sync did not repair missing-plugin drift: {repaired_missing}")

        _write_config(config, marketplace, output, enabled=False)
        removed = _actions(_run_ai_config(config, env))
        if not {"remove_codex_plugin", "remove_codex_marketplace"} <= set(removed):
            raise AssertionError(f"public removal actions were incomplete: {removed}")

        final_plugins = load_json(run(codex, ["plugin", "list", "--json"], env))["installed"]
        if not isinstance(final_plugins, list):
            raise AssertionError("Codex installed list was not an array")
        final_ids = {
            entry["pluginId"]
            for entry in final_plugins
            if isinstance(entry, dict) and isinstance(entry.get("pluginId"), str)
        }
        if _MANAGED_PLUGIN_ID in final_ids or unrelated_id not in final_ids:
            raise AssertionError(f"public removal did not preserve unrelated plugin: {final_ids}")
        marketplaces = load_json(run(codex, ["plugin", "marketplace", "list", "--json"], env))[
            "marketplaces"
        ]
        if not isinstance(marketplaces, list):
            raise AssertionError("Codex marketplace list was not an array")
        names = {
            entry["name"]
            for entry in marketplaces
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        if "ai-config-dev-tools" in names or "user-marketplace" not in names:
            raise AssertionError(f"public removal did not preserve unrelated marketplace: {names}")
        if _source_less_catalog_state(codex, env) != source_less_catalog_state:
            raise AssertionError("public sync changed source-less catalog visibility")
        if _tree_snapshot(source_less_catalog) != source_less_files:
            raise AssertionError("public sync changed unrelated source-less catalog files")
        final_config = config_path.read_text()
        if 'model = "preserve-public-sync"' not in final_config:
            raise AssertionError("public sync clobbered unrelated scalar config")
        if f'[plugins."{unrelated_id}"]\nenabled = false' not in final_config:
            raise AssertionError("public sync changed unrelated plugin enablement")

    return {
        "result": "passed",
        "version": version_output,
        "binary": str(Path(codex).resolve()),
        "public_command": f"{sys.executable} -m ai_config sync --config <isolated> --json",
        "lifecycle": [
            "first-sync-force-verify-local-authority",
            "unchanged-noop",
            "generated-output-integrity-repair",
            "source-refresh-update",
            "status-drift-reporting",
            "disabled-drift-reinstall",
            "missing-drift-install",
            "owned-removal",
            "unrelated-state-preservation",
            "source-less-catalog-preservation",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default=shutil.which("codex"))
    args = parser.parse_args()
    if not args.codex:
        raise SystemExit("codex executable not found")
    print(json.dumps(probe(args.codex), indent=2))


if __name__ == "__main__":
    main()
