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
    with pytest.raises(ValueError, match="explicit ownership"):
        result.write_to(output)
    assert not output.exists()
    convert_plugin_simple(source, TargetTool.PRIME, output)
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


@pytest.fixture
def prime_sync_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ai_config.adapters.claude import InstalledPlugin
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
    installed = [InstalledPlugin("demo", "1.0.0", "user", True, str(source))]
    monkeypatch.setattr("ai_config.operations.claude.list_installed_marketplaces", lambda: ([], []))
    monkeypatch.setattr(
        "ai_config.operations.claude.list_installed_plugins", lambda: (installed, [])
    )
    return source, output, target, installed


def test_prime_plan_reports_exact_actions_and_ownership_checkpoints(prime_sync_fixture) -> None:
    from ai_config.operations import apply_sync_plan, build_sync_plan

    _source, output, target, _installed = prime_sync_fixture
    planned = build_sync_plan(target)
    assert not planned.is_blocked
    assert not output.exists()
    assert [item.action.action for item in planned.actions] == ["create_prime_output"] * 2
    assert [(batch.target, batch.output_root) for batch in planned.target_batches] == [
        ("prime", output.resolve())
    ]
    observed = planned.runtime.cache_and_ownership.ownership
    assert len(observed) == 1 and observed[0].target == "prime" and observed[0].payload is None
    applied = apply_sync_plan(planned)
    assert applied.errors == ()
    assert applied.completed == tuple(item.action for item in planned.actions)
    assert any(
        item.kind == "ownership" and item.target == str(output.resolve())
        for item in applied.checkpoints_committed
    )
    repeated = build_sync_plan(target)
    assert [item.action.action for item in repeated.actions] == ["noop_prime_output"] * 2
    assert apply_sync_plan(repeated).errors == ()


@pytest.mark.parametrize("ledger_initially_exists", [False, True])
def test_prime_ownership_snapshot_drift_blocks_apply(
    prime_sync_fixture, ledger_initially_exists: bool
) -> None:
    from ai_config.operations import apply_sync_plan, build_sync_plan

    _source, output, target, _installed = prime_sync_fixture
    if ledger_initially_exists:
        assert apply_sync_plan(build_sync_plan(target)).errors == ()
    planned = build_sync_plan(target)
    ledger = output / ".ai-config/prime-ownership.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"schema":1,"files":{}}')
    before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    blocked = apply_sync_plan(planned)
    assert blocked.completed == ()
    assert any("Ownership state changed" in error for error in blocked.errors)
    assert before == {
        p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }


def test_prime_materialized_noop_does_not_recreate_output_deleted_after_plan(
    prime_sync_fixture,
) -> None:
    from ai_config.operations import apply_sync_plan, build_sync_plan

    _source, output, target, _installed = prime_sync_fixture
    assert apply_sync_plan(build_sync_plan(target)).errors == ()
    planned = build_sync_plan(target)
    removed = output / ".prime/agent/skills/demo-review/SKILL.md"
    removed.unlink()
    blocked = apply_sync_plan(planned)
    assert blocked.completed == ()
    assert any("Prime output state changed" in error for error in blocked.errors)
    assert not removed.exists()
    assert apply_sync_plan(build_sync_plan(target)).errors == ()
    assert removed.exists()


def test_prime_sync_owner_survives_source_relocation_and_preserves_mtimes(
    prime_sync_fixture,
) -> None:
    from ai_config.adapters.claude import InstalledPlugin
    from ai_config.operations import apply_sync_plan, build_sync_plan

    source, output, target, installed = prime_sync_fixture
    assert apply_sync_plan(build_sync_plan(target)).errors == ()
    before = {
        p.relative_to(output): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in output.rglob("*")
        if p.is_file()
    }
    relocated = source.with_name("relocated")
    source.rename(relocated)
    installed[:] = [InstalledPlugin("demo", "1.0.0", "user", True, str(relocated))]
    assert apply_sync_plan(build_sync_plan(target)).errors == ()
    assert before == {
        p.relative_to(output): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in output.rglob("*")
        if p.is_file()
    }
    ledger = json.loads((output / ".ai-config/prime-ownership.json").read_text())
    assert {entry["source"] for entry in ledger["files"].values()} == {"sync:demo"}


@pytest.mark.parametrize("first_owner", ["sync", "standalone"])
def test_prime_standalone_and_sync_ownership_do_not_take_over_each_other(
    prime_sync_fixture, first_owner: str
) -> None:
    from ai_config.operations import apply_sync_plan, build_sync_plan

    source, output, target, _installed = prime_sync_fixture
    if first_owner == "sync":
        assert apply_sync_plan(build_sync_plan(target)).errors == ()
        with pytest.raises(ValueError):
            convert_plugin(source, [TargetTool.PRIME], output)
    else:
        convert_plugin_simple(source, TargetTool.PRIME, output)
        blocked = build_sync_plan(target)
        assert blocked.is_blocked
        assert apply_sync_plan(blocked).completed == ()


def test_prime_expected_actions_checked_under_lock_before_any_mutation(tmp_path: Path) -> None:
    import fcntl
    from unittest.mock import patch

    from ai_config.prime_ownership import PrimeDesiredFile, plan_prime_files, write_prime_files

    output = tmp_path / "output"
    path = Path(".prime/agent/skills/demo-review/SKILL.md")
    desired = [PrimeDesiredFile("sync:demo", path, b"fixture")]
    expected = plan_prime_files(output, desired)
    original_flock = fcntl.flock

    def concurrent_file_appears(stream, operation):
        original_flock(stream, operation)
        if operation == fcntl.LOCK_EX:
            destination = output / path
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"USER FILE")

    with patch("fcntl.flock", side_effect=concurrent_file_appears):
        with pytest.raises(ValueError, match="unowned or modified"):
            write_prime_files(output, desired, expected_actions=expected)
    assert (output / path).read_bytes() == b"USER FILE"
    assert not (output / ".ai-config/prime-ownership.json").exists()


def test_prime_empty_projection_has_no_ownership_checkpoint(prime_sync_fixture) -> None:
    from ai_config.operations import apply_sync_plan, build_sync_plan

    source, output, target, _installed = prime_sync_fixture
    (source / "skills/review/pyproject.toml").write_text("[project]\nname='review'")
    planned = build_sync_plan(target)
    assert not planned.is_blocked and planned.target_batches == ()
    assert all(item.kind != "ownership" for item in planned.checkpoints)
    assert apply_sync_plan(planned).errors == ()
    assert not output.exists()


@pytest.mark.parametrize("ledger_kind", ["directory", "symlink"])
def test_prime_ledger_observation_rejects_unsafe_state(
    prime_sync_fixture, ledger_kind: str
) -> None:
    from ai_config.operations import apply_sync_plan, build_sync_plan

    _source, output, target, _installed = prime_sync_fixture
    ledger = output / ".ai-config/prime-ownership.json"
    ledger.parent.mkdir(parents=True)
    if ledger_kind == "directory":
        ledger.mkdir()
    else:
        sentinel = output.parent / "sentinel"
        sentinel.write_text('{"schema":1,"files":{}}')
        ledger.symlink_to(sentinel)
    planned = build_sync_plan(target)
    assert planned.is_blocked
    assert apply_sync_plan(planned).completed == ()
    assert not (output / ".prime").exists()


def test_prime_aggregate_guard_checks_every_source_before_writing(tmp_path: Path) -> None:
    from ai_config.prime_ownership import PrimeDesiredFile, write_prime_files

    output = tmp_path / "output"
    one = Path(".prime/agent/skills/one-review/SKILL.md")
    two = Path(".prime/agent/skills/two-review/SKILL.md")
    destination = output / two
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"USER FILE")
    with pytest.raises(ValueError, match="unowned or modified"):
        write_prime_files(
            output,
            [
                PrimeDesiredFile("sync:one", one, b"one"),
                PrimeDesiredFile("sync:two", two, b"two"),
            ],
        )
    assert not (output / one).exists()
    assert destination.read_bytes() == b"USER FILE"
    assert not (output / ".ai-config").exists()
