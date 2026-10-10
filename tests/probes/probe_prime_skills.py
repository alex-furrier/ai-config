"""Auth-free Prime Rust discovery proof in disposable project and user homes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from ai_config.converters.convert import convert_plugin
from ai_config.converters.ir import InstallScope, TargetTool


def probe(binary: Path) -> dict[str, object]:
    binary = binary.resolve()
    version = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=True, timeout=5
    ).stdout.strip()
    if version != "0.10.0":
        raise ValueError(
            f"Only audited Prime Rust 0.10.0 is supported by this probe; got {version}"
        )
    with tempfile.TemporaryDirectory(prefix="ai-config-prime-") as temporary:
        root = Path(temporary).resolve()
        source = root / "source"
        (source / ".claude-plugin").mkdir(parents=True)
        (source / ".claude-plugin/plugin.json").write_text('{"name":"probe"}')
        (source / "skills/discovery").mkdir(parents=True)
        (source / "skills/discovery/SKILL.md").write_text(
            "---\nname: discovery\ndescription: PRIME_ADAPTER_DISCOVERY_SENTINEL\n---\nRead fixture."
        )
        observed = []
        for scope in (InstallScope.PROJECT, InstallScope.USER):
            home = root / scope.value / "home"
            project = root / scope.value / "project"
            home.mkdir(parents=True)
            project.mkdir(parents=True)
            output = home if scope == InstallScope.USER else project
            report = convert_plugin(source, [TargetTool.PRIME], output, scope=scope)[
                TargetTool.PRIME
            ]
            if report.errors:
                raise ValueError(f"Conversion failed: {report.errors}")
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home),
                "TMPDIR": str(root),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "PRIME_AGENT_CODING_AGENT_DIR": str(home / ".prime/agent"),
            }
            result = subprocess.run(
                [str(binary), "prompt", "--cwd", str(project), "--json"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
            payload = json.loads(result.stdout)
            serialized = json.dumps(payload)
            if (
                "PRIME_ADAPTER_DISCOVERY_SENTINEL" not in serialized
                or "probe-discovery" not in serialized
            ):
                raise ValueError(f"Prime did not discover {scope.value} generated skill")
            observed.append(scope.value)
        return {"version": version, "discovered_scopes": observed, "inference_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prime-bin", type=Path, required=True)
    print(json.dumps(probe(parser.parse_args().prime_bin), sort_keys=True))
