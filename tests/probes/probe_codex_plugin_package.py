#!/usr/bin/env python3
"""Probe generated Codex packages and lifecycle with no ambient credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ai_config.codex_lifecycle import sync_codex_packages
from ai_config.converters import TargetTool, convert_plugin
from ai_config.converters.codex_package import codex_package_spec
from ai_config.semver import SemanticVersion
from ai_config.validators.target.codex import CodexOutputValidator

SENSITIVE_ENV_VARS = {
    "CHATGPT_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
}
FEATURE_ROWS = {
    "hooks": ("stable", "true"),
    "plugin_sharing": ("stable", "true"),
    "plugins": ("stable", "true"),
    "remote_plugin": ("stable", "true"),
}


def run(
    codex: str,
    args: list[str],
    env: dict[str, str],
    *,
    cwd: Path | None = None,
    expected_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    """Run one isolated Codex command and fail with complete evidence."""
    result = subprocess.run(
        [codex, *args],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in expected_codes:
        raise RuntimeError(
            f"command failed ({result.returncode}): {codex} {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def load_json(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    value: object = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise AssertionError("expected JSON object")
    return value


def make_unrelated_marketplace(root: Path) -> tuple[str, str]:
    """Create state that ai-config must never mutate."""
    marketplace = root / "unrelated"
    plugin = marketplace / "plugins" / "user-plugin"
    (marketplace / ".agents/plugins").mkdir(parents=True)
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills/user-skill").mkdir(parents=True)
    (marketplace / ".agents/plugins/marketplace.json").write_text(
        json.dumps(
            {
                "name": "user-marketplace",
                "plugins": [
                    {
                        "name": "user-plugin",
                        "source": {"source": "local", "path": "./plugins/user-plugin"},
                    }
                ],
            }
        )
    )
    (plugin / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "user-plugin",
                "version": "1.0.0",
                "description": "unrelated",
                "skills": "./skills/",
            }
        )
    )
    (plugin / "skills/user-skill/SKILL.md").write_text(
        "---\nname: user-skill\ndescription: unrelated marker\n---\nunrelated marker\n"
    )
    return str(marketplace), "user-plugin@user-marketplace"


def set_enabled(config_path: Path, plugin_id: str, enabled: bool) -> None:
    old = f'[plugins."{plugin_id}"]\nenabled = {str(not enabled).lower()}'
    new = f'[plugins."{plugin_id}"]\nenabled = {str(enabled).lower()}'
    content = config_path.read_text()
    if content.count(old) != 1:
        raise AssertionError(f"expected one plugin toggle for {plugin_id}")
    config_path.write_text(content.replace(old, new))


def probe(codex: str, expected_version: str | None = None) -> dict[str, object]:
    """Exercise generation, validation, discovery, update, idempotence, and removal."""
    repo_root = Path(__file__).resolve().parents[2]
    source = repo_root / "tests/fixtures/sample-plugins/complete-plugin"
    with tempfile.TemporaryDirectory(prefix="ai-config-codex-package-") as temporary:
        root = Path(temporary)
        home = root / "home"
        codex_home = home / ".codex"
        output = root / "output"
        codex_home.mkdir(parents=True)
        (codex_home / "config.toml").write_text('model = "unrelated-model"\n')
        env = {key: value for key, value in os.environ.items() if key not in SENSITIVE_ENV_VARS}
        env.update({"HOME": str(home), "CODEX_HOME": str(codex_home)})

        version_output = run(codex, ["--version"], env).stdout.strip()
        version = version_output.removeprefix("codex-cli ")
        if expected_version is not None and version != expected_version:
            raise AssertionError(f"expected Codex {expected_version}, got {version_output}")
        features_output = run(codex, ["features", "list"], env).stdout
        feature_evidence: dict[str, str] = {}
        parsed_version = SemanticVersion.parse(version, context="Codex CLI version")
        expected_features = (
            {"plugins": FEATURE_ROWS["plugins"]}
            if parsed_version >= SemanticVersion(0, 159, 2)
            else FEATURE_ROWS
        )
        for name, (stage, value) in expected_features.items():
            row = next(
                (line for line in features_output.splitlines() if line.split()[:1] == [name]), None
            )
            if row is None or stage not in row or not row.rstrip().endswith(value):
                raise AssertionError(f"missing exact feature state for {name}: {stage} {value}")
            feature_evidence[name] = row.strip()

        help_surfaces = {}
        for args in (
            ["plugin", "--help"],
            ["plugin", "add", "--help"],
            ["plugin", "list", "--help"],
            ["plugin", "remove", "--help"],
            ["plugin", "marketplace", "--help"],
            ["plugin", "marketplace", "add", "--help"],
            ["plugin", "marketplace", "list", "--help"],
            ["plugin", "marketplace", "upgrade", "--help"],
            ["plugin", "marketplace", "remove", "--help"],
        ):
            key = " ".join(args[:-1])
            help_surfaces[key] = run(codex, args, env).stdout.splitlines()[0]

        unrelated_path, unrelated_id = make_unrelated_marketplace(root)
        load_json(run(codex, ["plugin", "marketplace", "add", unrelated_path, "--json"], env))
        load_json(run(codex, ["plugin", "add", unrelated_id, "--json"], env))

        reports = convert_plugin(source, [TargetTool.CODEX], output_dir=output)
        report = reports[TargetTool.CODEX]
        if not report.success:
            raise AssertionError(report.to_json())
        validation = CodexOutputValidator().validate_all(output)
        failures = [result.message for result in validation if result.status == "fail"]
        if failures:
            raise AssertionError(f"generated package validation failed: {failures}")

        spec = codex_package_spec("dev-tools", "1.0.0", output)
        with_env = {key: os.environ.get(key) for key in SENSITIVE_ENV_VARS}
        os.environ.clear()
        os.environ.update(env)
        try:
            sync_codex_packages(
                [spec],
                output_dir=output,
                refreshed_plugin_ids={spec.plugin_id},
            )
            installed = load_json(run(codex, ["plugin", "list", "--json"], env))["installed"]
            managed = next(item for item in installed if item["pluginId"] == spec.plugin_id)
            if managed["enabled"] is not True:
                raise AssertionError("generated plugin was not enabled")

            prompt = run(codex, ["-C", str(output), "debug", "prompt-input", "probe"], env).stdout
            if "dev-tools:code-review" not in prompt:
                raise AssertionError("enabled generated package skill was not discovered")
            mcp = run(codex, ["mcp", "list"], env).stdout
            if "database" not in mcp or "github" not in mcp:
                raise AssertionError("package MCP servers were not ingested")
            installed_manifests = list(
                (codex_home / "plugins" / "cache").glob(
                    f"{spec.marketplace_name}/{spec.plugin_name}/*/.codex-plugin/plugin.json"
                )
            )
            if len(installed_manifests) != 1:
                raise AssertionError(
                    f"expected one installed generated package, got {installed_manifests}"
                )
            installed_path = installed_manifests[0].parents[1]
            if not (installed_path / "hooks/hooks.json").is_file():
                raise AssertionError("package hooks were not copied into Codex cache")

            config_path = codex_home / "config.toml"
            set_enabled(config_path, spec.plugin_id, False)
            disabled_prompt = run(
                codex, ["-C", str(output), "debug", "prompt-input", "probe"], env
            ).stdout
            if "dev-tools:code-review" in disabled_prompt:
                raise AssertionError("disabled generated package skill was discovered")
            set_enabled(config_path, spec.plugin_id, True)

            before = hashlib.sha256(config_path.read_bytes()).hexdigest()
            sync_codex_packages([spec], output_dir=output, refreshed_plugin_ids=set())
            after = hashlib.sha256(config_path.read_bytes()).hexdigest()
            if before != after:
                raise AssertionError("idempotent sync changed Codex config")

            skill_path = spec.marketplace_path / "plugins/dev-tools/skills/code-review/SKILL.md"
            skill_path.write_text(
                skill_path.read_text().replace(
                    "description: Review code for best practices, security issues, and style violations.",
                    "description: LATEST_UPDATE_MARKER.",
                )
            )
            sync_codex_packages(
                [spec],
                output_dir=output,
                refreshed_plugin_ids={spec.plugin_id},
            )
            updated_prompt = run(
                codex, ["-C", str(output), "debug", "prompt-input", "probe"], env
            ).stdout
            if "LATEST_UPDATE_MARKER" not in updated_prompt:
                raise AssertionError("updated generated package was not reinstalled")

            sync_codex_packages([], output_dir=output, refreshed_plugin_ids=set())
        finally:
            os.environ.clear()
            os.environ.update(env)
            for key, value in with_env.items():
                if value is not None:
                    os.environ[key] = value

        final_plugins = load_json(run(codex, ["plugin", "list", "--json"], env))["installed"]
        ids = {item["pluginId"] for item in final_plugins}
        if spec.plugin_id in ids or unrelated_id not in ids:
            raise AssertionError(f"removal did not preserve unrelated plugin: {ids}")
        marketplaces = load_json(run(codex, ["plugin", "marketplace", "list", "--json"], env))[
            "marketplaces"
        ]
        names = {item["name"] for item in marketplaces}
        if spec.marketplace_name in names or "user-marketplace" not in names:
            raise AssertionError(f"removal did not preserve unrelated marketplace: {names}")
        config = config_path.read_text()
        if 'model = "unrelated-model"' not in config:
            raise AssertionError("Codex lifecycle clobbered unrelated scalar config")

        doctor = load_json(
            run(codex, ["--strict-config", "doctor", "--json"], env, expected_codes=(0, 1))
        )
        config_check = doctor.get("checks", {}).get("config.load", {})
        if config_check.get("status") != "ok":
            raise AssertionError("strict Codex doctor could not load preserved config")

    return {
        "result": "passed",
        "version": version_output,
        "binary": str(Path(codex).resolve()),
        "install_source": "caller-provided Codex binary",
        "credentials_removed": sorted(SENSITIVE_ENV_VARS),
        "features": feature_evidence,
        "plugin_help_surfaces": help_surfaces,
        "lifecycle": [
            "generate",
            "validate",
            "marketplace-add/list/remove",
            "plugin-install/list/remove",
            "enabled-disabled-reenabled-discovery",
            "hooks-and-mcp-ingestion",
            "update-reinstall",
            "idempotence",
            "unrelated-state-preservation",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    if not args.codex:
        raise SystemExit("codex executable not found")
    print(json.dumps(probe(args.codex, args.expected_version), indent=2))


if __name__ == "__main__":
    main()
