"""Live read-only contract checks before direct Codex lifecycle mutations."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ai_config.adapters.codex import CodexCLI, CodexCommandError


def _fake_codex(tmp_path: Path, *, version: str = "0.160.0") -> tuple[CodexCLI, Path, Path]:
    config = tmp_path / "responses.json"
    log = tmp_path / "commands.jsonl"
    config.write_text(
        json.dumps(
            {
                "features": "plugins stable true\n",
                "marketplaces": '{"marketplaces":[]}',
                "plugins": '{"installed":[],"available":[]}',
                "mutation": '{"marketplaceName":"market","installedRoot":null}',
            }
        )
    )
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"config = json.loads(Path({str(config)!r}).read_text())\n"
        f"with Path({str(log)!r}).open('a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"if sys.argv[1:] == ['--version']: print('codex-cli {version}')\n"
        "elif sys.argv[1:] == ['features', 'list']: print(config['features'], end='')\n"
        "elif sys.argv[1:] == ['plugin', 'marketplace', 'list', '--json']: "
        "print(config['marketplaces'])\n"
        "elif sys.argv[1:] == ['plugin', 'list', '--available', '--json']: "
        "print(config['plugins'])\n"
        "else: print(config['mutation'])\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return CodexCLI(str(executable)), config, log


def _commands(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize("version", ["0.159.3", "0.160.0"])
def test_future_patch_and_minor_direct_mutation_preflights(tmp_path: Path, version: str) -> None:
    cli, _, log = _fake_codex(tmp_path, version=version)
    cli.remove_marketplace("market")
    assert _commands(log) == [
        ["--version"],
        ["features", "list"],
        ["plugin", "marketplace", "list", "--json"],
        ["plugin", "list", "--available", "--json"],
        ["plugin", "marketplace", "remove", "market", "--json"],
    ]


@pytest.mark.parametrize("version", ["0.158.0", "0.159.1"])
def test_unsupported_old_client_never_mutates(tmp_path: Path, version: str) -> None:
    cli, _, log = _fake_codex(tmp_path, version=version)
    with pytest.raises(CodexCommandError, match="unsupported Codex CLI"):
        cli.remove_plugin("demo@market")
    assert _commands(log) == [["--version"]]


@pytest.mark.parametrize(
    "features",
    [
        "",
        "plugins stable",
        "plugins stable false\n",
        "plugins nonsense true\n",
        "plugins stable true\nplugins stable true\n",
    ],
)
def test_missing_or_malformed_features_block_direct_add(tmp_path: Path, features: str) -> None:
    cli, config, log = _fake_codex(tmp_path)
    data = json.loads(config.read_text())
    data["features"] = features
    config.write_text(json.dumps(data))
    with pytest.raises(CodexCommandError, match="feature row"):
        cli.add_marketplace(str(tmp_path), "market")
    assert _commands(log) == [["--version"], ["features", "list"]]


@pytest.mark.parametrize(
    "field,payload,method",
    [
        ("marketplaces", '{"marketplaces":{}}', "add_plugin"),
        ("marketplaces", '{"marketplaces":[] ,"marketplaces":[]}', "remove_marketplace"),
        ("plugins", '{"installed":[],"available":{}}', "remove_plugin"),
        ("plugins", "not json", "add_marketplace"),
    ],
)
def test_incompatible_list_blocks_direct_mutation(
    tmp_path: Path, field: str, payload: str, method: str
) -> None:
    cli, config, log = _fake_codex(tmp_path)
    data = json.loads(config.read_text())
    data[field] = payload
    config.write_text(json.dumps(data))
    with pytest.raises(CodexCommandError):
        if method == "add_plugin":
            cli.add_plugin("demo@market")
        elif method == "remove_plugin":
            cli.remove_plugin("demo@market")
        elif method == "add_marketplace":
            cli.add_marketplace(str(tmp_path), "market")
        else:
            cli.remove_marketplace("market")
    assert not any("add" in command or "remove" in command for command in _commands(log))


@pytest.mark.parametrize("method", ["add_marketplace", "add_plugin", "remove_plugin"])
def test_future_additive_mutation_fields_keep_required_confirmation(
    tmp_path: Path, method: str
) -> None:
    cli, config, log = _fake_codex(tmp_path)
    responses = {
        "add_marketplace": {
            "marketplaceName": "market",
            "installedRoot": str(tmp_path),
            "alreadyAdded": False,
        },
        "add_plugin": {
            "pluginId": "demo@market",
            "name": "demo",
            "marketplaceName": "market",
            "version": "1.0.0",
            "installedPath": str(tmp_path / "installed"),
            "authPolicy": "ON_INSTALL",
        },
        "remove_plugin": {
            "pluginId": "demo@market",
            "name": "demo",
            "marketplaceName": "market",
        },
    }
    data = json.loads(config.read_text())
    data["mutation"] = json.dumps({**responses[method], "futureMetadata": {"revision": 2}})
    config.write_text(json.dumps(data))

    if method == "add_marketplace":
        assert cli.add_marketplace(str(tmp_path), "market").root == tmp_path
    elif method == "add_plugin":
        assert cli.add_plugin("demo@market").installed_path == tmp_path / "installed"
    else:
        cli.remove_plugin("demo@market")

    commands = _commands(log)
    assert commands[:4] == [
        ["--version"],
        ["features", "list"],
        ["plugin", "marketplace", "list", "--json"],
        ["plugin", "list", "--available", "--json"],
    ]
    assert (
        commands[4]
        == {
            "add_marketplace": ["plugin", "marketplace", "add", str(tmp_path), "--json"],
            "add_plugin": ["plugin", "add", "demo@market", "--json"],
            "remove_plugin": ["plugin", "remove", "demo@market", "--json"],
        }[method]
    )


def test_future_additive_marketplace_removal_keeps_required_confirmation(tmp_path: Path) -> None:
    cli, config, log = _fake_codex(tmp_path)
    data = json.loads(config.read_text())
    data["mutation"] = json.dumps(
        {
            "marketplaceName": "market",
            "installedRoot": None,
            "alreadyAdded": False,
            "futureMetadata": {"revision": 2},
        }
    )
    config.write_text(json.dumps(data))

    cli.remove_marketplace("market")
    assert _commands(log)[-1] == ["plugin", "marketplace", "remove", "market", "--json"]


def test_post_mutation_json_mismatch_reports_possible_partial_state(tmp_path: Path) -> None:
    cli, config, log = _fake_codex(tmp_path)
    data = json.loads(config.read_text())
    data["mutation"] = '{"pluginId":"other@market"}'
    config.write_text(json.dumps(data))
    with pytest.raises(CodexCommandError, match="partial sync state is possible") as caught:
        cli.remove_plugin("demo@market")
    assert caught.value.stage == "remove-plugin"
    assert _commands(log)[-1] == ["plugin", "remove", "demo@market", "--json"]
