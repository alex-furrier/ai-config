"""Prime's public conversion boundary and conservative ownership behavior."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from ai_config.cli import main
from ai_config.converters.claude_parser import parse_claude_plugin
from ai_config.converters.convert import convert_plugin, convert_plugin_simple
from ai_config.converters.emitters import PrimeEmitter
from ai_config.converters.ir import InstallScope, TargetTool
from ai_config.validators.target import get_output_validator


def plugin(root: Path, *, body: str = "Review carefully", name: str = "demo") -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}))
    skill = root / "skills/review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review source\nallowed-tools: [Read]\n"
        "context: fork\n---\n" + body
    )
    (skill / "reference.txt").write_text("Supporting material")
    return root


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_prime_scope_discovery_resources_and_diagnostics(
    tmp_path: Path, scope: InstallScope
) -> None:
    source = plugin(tmp_path / "source")
    commands = source / "commands"
    commands.mkdir()
    (commands / "commit.md").write_text("Commit $ARGUMENTS")
    (source / ".mcp.json").write_text('{"mcpServers":{"demo":{"command":"false"}}}')
    result = PrimeEmitter(scope).emit(parse_claude_plugin(source))
    assert result.target == TargetTool.PRIME
    assert {f.path for f in result.files} == {
        Path(".prime/agent/skills/demo-review/SKILL.md"),
        Path(".prime/agent/skills/demo-review/reference.txt"),
    }
    assert result.mappings[0].lost_features == ["allowed-tools", "context", "user-invocable"]
    assert all(m.status.value == "unsupported" for m in result.mappings[1:])
    output = tmp_path / "output"
    result.write_to(output)
    assert all(v.status == "pass" for v in get_output_validator("prime").validate_all(output))
    assert not (output / ".prime/agent/settings.json").exists()


def test_prime_dry_run_idempotence_owned_update_and_preserved_stale(tmp_path: Path) -> None:
    source = plugin(tmp_path / "source")
    output = tmp_path / "output"
    convert_plugin(source, [TargetTool.PRIME], output, dry_run=True)
    assert not output.exists()
    convert_plugin(source, [TargetTool.PRIME], output)
    snapshot = {
        p.relative_to(output): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in output.rglob("*")
        if p.is_file()
    }
    convert_plugin(source, [TargetTool.PRIME], output)
    assert snapshot == {
        p.relative_to(output): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in output.rglob("*")
        if p.is_file()
    }
    (source / "skills/review/SKILL.md").write_text(
        "---\nname: review\ndescription: Review source\n---\nUpdated"
    )
    convert_plugin_simple(source, "prime", output)
    generated = output / ".prime/agent/skills/demo-review/SKILL.md"
    assert "Updated" in generated.read_text()
    (source / "skills/review/reference.txt").unlink()
    convert_plugin(source, [TargetTool.PRIME], output)
    assert (
        output / ".prime/agent/skills/demo-review/reference.txt"
    ).read_text() == "Supporting material"


@pytest.mark.parametrize("kind", ["unowned", "edited", "symlink", "different-source"])
def test_prime_preserves_conflicts_before_writes(tmp_path: Path, kind: str) -> None:
    source = plugin(tmp_path / "source")
    output = tmp_path / "output"
    path = output / ".prime/agent/skills/demo-review/SKILL.md"
    if kind in {"edited", "different-source"}:
        convert_plugin(source, [TargetTool.PRIME], output)
        if kind == "edited":
            path.write_text("USER CONTENT")
        else:
            source = plugin(tmp_path / "another")
    else:
        path.parent.mkdir(parents=True)
        if kind == "symlink":
            sentinel = tmp_path / "sentinel"
            sentinel.write_text("USER CONTENT")
            path.symlink_to(sentinel)
        else:
            path.write_text("USER CONTENT")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        convert_plugin(source, [TargetTool.PRIME], output, dry_run=True)
    with pytest.raises(ValueError):
        convert_plugin(source, [TargetTool.PRIME], output)
    assert path.read_bytes() == original


def test_prime_python_skill_and_missing_description_diagnosed(tmp_path: Path) -> None:
    source = plugin(tmp_path / "source")
    (source / "skills/review/pyproject.toml").write_text("[project]\nname='review'")
    result = PrimeEmitter().emit(parse_claude_plugin(source))
    assert not result.files and result.mappings[0].status.value == "unsupported"
    (source / "skills/review/pyproject.toml").unlink()
    (source / "skills/review/SKILL.md").write_text("---\nname: review\n---\nBody")
    assert PrimeEmitter().emit(parse_claude_plugin(source)).has_errors()


def test_prime_cli_convert_and_doctor(tmp_path: Path) -> None:
    source = plugin(tmp_path / "source")
    output = tmp_path / "output"
    runner = CliRunner()
    converted = runner.invoke(
        main, ["convert", str(source), "--target", "prime", "--output", str(output)]
    )
    assert converted.exit_code == 0, converted.output
    checked = runner.invoke(main, ["doctor", "--target", "prime", str(output)])
    assert checked.exit_code == 0, checked.output


def test_prime_sync_revalidates_output_and_preserves_user_changes(tmp_path: Path) -> None:
    from unittest.mock import patch

    from ai_config.adapters.claude import InstalledPlugin
    from ai_config.operations import apply_sync_plan, build_sync_plan
    from ai_config.types import ClaudeTargetConfig, ConversionConfig, PluginConfig, TargetConfig

    source = plugin(tmp_path / "source")
    output = tmp_path / "output"
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            plugins=(PluginConfig("demo"),),
            conversion=ConversionConfig(targets=("prime",), output_dir=str(output)),
        ),
    )
    installed = InstalledPlugin("demo", "1.0.0", "user", True, str(source))
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=([installed], [])),
    ):
        first = apply_sync_plan(build_sync_plan(target))
        assert first.errors == ()
        second = apply_sync_plan(build_sync_plan(target))
        assert second.errors == ()
        path = output / ".prime/agent/skills/demo-review/SKILL.md"
        path.unlink()
        assert apply_sync_plan(build_sync_plan(target)).errors == ()
        assert path.exists()
        path.write_text("USER EDIT")
        blocked = build_sync_plan(target)
        assert blocked.is_blocked
        assert apply_sync_plan(blocked).errors
        assert path.read_text() == "USER EDIT"


def test_prime_sync_source_collision_blocks_all_output(tmp_path: Path) -> None:
    from unittest.mock import patch

    from ai_config.adapters.claude import InstalledPlugin
    from ai_config.operations import apply_sync_plan, build_sync_plan
    from ai_config.types import ClaudeTargetConfig, ConversionConfig, PluginConfig, TargetConfig

    one = plugin(tmp_path / "one")
    two = plugin(tmp_path / "two", body="Other source")
    output = tmp_path / "output"
    target = TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            plugins=(PluginConfig("demo@one"), PluginConfig("demo@two")),
            conversion=ConversionConfig(targets=("prime",), output_dir=str(output)),
        ),
    )
    installed = [
        InstalledPlugin("demo@one", "1.0.0", "user", True, str(one)),
        InstalledPlugin("demo@two", "1.0.0", "user", True, str(two)),
    ]
    with (
        patch("ai_config.operations.claude.list_installed_marketplaces", return_value=([], [])),
        patch("ai_config.operations.claude.list_installed_plugins", return_value=(installed, [])),
    ):
        plan = build_sync_plan(target)
        assert any("Prime output collision" in e for e in [d.message for d in plan.diagnostics])
        assert apply_sync_plan(plan).errors
    assert not output.exists()


@pytest.mark.parametrize("conflict", ["ancestor-file", "python-package", "mixed-target"])
def test_prime_preflights_before_any_generated_write(tmp_path: Path, conflict: str) -> None:
    source = plugin(tmp_path / "source")
    output = tmp_path / "output"
    directory = output / ".prime/agent/skills/demo-review"
    directory.parent.mkdir(parents=True)
    targets = [TargetTool.PRIME]
    if conflict == "ancestor-file":
        directory.write_text("USER FILE")
    else:
        directory.mkdir()
        if conflict == "python-package":
            (directory / "pyproject.toml").write_text("[project]\nname='user'")
        else:
            (directory / "SKILL.md").write_text("USER SKILL")
            targets = [TargetTool.CODEX, TargetTool.PRIME]
    before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        convert_plugin(source, targets, output)
    assert before == {
        p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }
    assert not (output / ".ai-config").exists()
