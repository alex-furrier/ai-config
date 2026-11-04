"""Subprocess deadline contracts for cold Codex inventory."""

import subprocess
from unittest.mock import Mock

import pytest

from ai_config.adapters.codex import CodexCLI, CodexCommandError


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, [30.0, 120.0, 30.0]),
        ({"timeout_seconds": 7.0, "inventory_timeout_seconds": 90.0}, [7.0, 90.0, 7.0]),
    ],
)
def test_inventory_has_its_own_deadline(monkeypatch, options, expected):
    process = Mock(returncode=0)
    process.communicate.side_effect = [
        ("codex-cli 0.157.0", ""),
        ('{"installed":[],"available":[]}', ""),
        ('{"marketplaces":[]}', ""),
    ]
    monkeypatch.setattr("ai_config.adapters.codex.subprocess.Popen", Mock(return_value=process))
    cli = CodexCLI("codex", **options)
    assert cli.list_plugins() == []
    assert cli.list_marketplaces() == []
    assert [call.kwargs["timeout"] for call in process.communicate.call_args_list] == expected


def test_inventory_timeout_reports_call_and_limit_and_stops_process(monkeypatch):
    process = Mock(returncode=0)
    process.communicate.side_effect = [
        ("codex-cli 0.157.0", ""),
        subprocess.TimeoutExpired("codex", 120.0),
    ]
    monkeypatch.setattr("ai_config.adapters.codex.subprocess.Popen", Mock(return_value=process))
    cli = CodexCLI("codex")
    stop = Mock()
    monkeypatch.setattr(cli, "_stop_process_tree", stop)
    with pytest.raises(CodexCommandError) as caught:
        cli.list_plugins()
    assert "plugin list --available --json" in str(caught.value)
    assert "command timed out after 120 seconds" in str(caught.value)
    stop.assert_called_once_with(process)


@pytest.mark.parametrize("limit", [0, -1])
def test_inventory_deadline_must_be_positive(limit):
    with pytest.raises(ValueError, match="inventory timeout"):
        CodexCLI(inventory_timeout_seconds=limit)
