"""Shared skill resource projection and contained-source safety contracts."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_config.converters.claude_parser import (
    parse_claude_plugin,
    parse_claude_plugin_with_ignored_generated_paths,
)
from ai_config.converters.convert import convert_plugin, convert_plugin_simple
from ai_config.converters.emitters import CodexEmitter, CursorEmitter, OpenCodeEmitter, PiEmitter
from ai_config.converters.ir import MappingStatus, McpServer, PluginIdentity, PluginIR, TargetTool
from ai_config.pi_ownership import load_pi_ownership
from ai_config.sync_state import (
    compute_plugin_conversion_hash,
    compute_plugin_hash,
    load_conversion_cache,
)
from ai_config.validators.target.codex import CodexOutputValidator
from ai_config.validators.target.cursor import CursorOutputValidator
from ai_config.validators.target.opencode import OpenCodeOutputValidator
from ai_config.validators.target.pi import PiOutputValidator
from ai_config.validators.target.skill_invariants import generated_skill_invariant_errors

FIXTURE = Path(__file__).parents[2] / "fixtures" / "sample-plugins" / "shared-includes"


def _plugin(tmp_path: Path, includes: object, body: str = "body") -> Path:
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "safe-plugin", "skills": "./skills"})
    )
    for name in ("good", "bad"):
        directory = plugin / "skills" / name
        directory.mkdir(parents=True)
        metadata = includes if name == "bad" else []
        (directory / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {name}\n"
            f"x-ai-config-includes: {json.dumps(metadata)}\n"
            "---\n\n"
            f"{body if name == 'bad' else 'safe body'}\n"
        )
    return plugin


def _remove_include_metadata(plugin: Path) -> None:
    for name in ("alpha", "beta"):
        (plugin / f"skills/{name}/SKILL.md").write_text(
            f"---\nname: {name}\ndescription: no includes\n---\n\nplain body\n"
        )


def _target_native_alpha_skill(target: TargetTool) -> Path:
    if target == TargetTool.CODEX:
        return Path("targets/codex/skills/alpha")
    if target == TargetTool.CURSOR:
        return Path("targets/cursor/skills/shared-includes-alpha")
    if target == TargetTool.OPENCODE:
        return Path("targets/opencode/.opencode/skills/shared-includes-alpha")
    return Path("targets/pi/skills/shared-includes-alpha")


def _skill_roots(target: TargetTool) -> tuple[Path, Path]:
    if target == TargetTool.CODEX:
        root = Path(
            ".ai-config/codex/marketplaces/ai-config-shared-includes/plugins/shared-includes/skills"
        )
        return root / "alpha", root / "beta"
    prefix = {
        TargetTool.CURSOR: Path(".cursor/skills"),
        TargetTool.OPENCODE: Path(".opencode/skills"),
        TargetTool.PI: Path(".pi/skills"),
    }[target]
    return prefix / "shared-includes-alpha", prefix / "shared-includes-beta"


def test_parser_captures_immutable_byte_exact_include_records() -> None:
    ir = parse_claude_plugin(FIXTURE)
    assert not ir.has_errors()
    alpha = next(skill for skill in ir.skills() if skill.name == "alpha")
    assert alpha.includes[0].source_relative_path == "shared/data.txt"
    assert alpha.includes[0].projected_path == "_shared/shared/data.txt"
    assert alpha.includes[0].content == (FIXTURE / "shared/data.txt").read_bytes()
    assert alpha.includes[2].executable
    assert alpha.includes[3].content == (FIXTURE / "shared/blob.bin").read_bytes()
    with pytest.raises(ValidationError):
        alpha.includes[0].content = b"changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("target", "emitter"),
    [
        (TargetTool.CODEX, CodexEmitter()),
        (TargetTool.CURSOR, CursorEmitter()),
        (TargetTool.OPENCODE, OpenCodeEmitter()),
        (TargetTool.PI, PiEmitter()),
    ],
)
def test_all_emitters_materialize_independent_self_contained_skill_copies(
    target: TargetTool, emitter: object, tmp_path: Path
) -> None:
    ir = parse_claude_plugin(FIXTURE)
    result = emitter.emit(ir)  # type: ignore[union-attr]
    assert not result.has_errors()
    result.write_to(tmp_path)

    alpha, beta = _skill_roots(target)
    for root in (alpha, beta):
        skill_md = (tmp_path / root / "SKILL.md").read_text()
        assert "x-ai-config-includes" not in skill_md
        assert "CLAUDE_PLUGIN_ROOT" not in skill_md
        assert "_shared/shared/data.txt" in skill_md
        assert (tmp_path / root / "_shared/shared/data.txt").read_bytes() == (
            FIXTURE / "shared/data.txt"
        ).read_bytes()
        assert (tmp_path / root / "_shared/shared/blob.bin").read_bytes() == (
            FIXTURE / "shared/blob.bin"
        ).read_bytes()
        assert os.access(tmp_path / root / "_shared/shared/run.sh", os.X_OK)
    assert (tmp_path / alpha / "_shared/shared/data.txt") != (
        tmp_path / beta / "_shared/shared/data.txt"
    )
    assert all(
        mapping.status == MappingStatus.TRANSFORM
        for mapping in result.mappings
        if mapping.component_kind == "skill"
    )
    assert len(result.include_evidence) == 8
    dependency = next(
        item
        for item in result.include_evidence
        if item.consumer_skill == "alpha" and item.source_relative_path.endswith("dependency.json")
    )
    assert dependency.direct_rewrite_count == 0
    assert dependency.copy_count == 1


@pytest.mark.parametrize(
    ("target", "emitter"),
    [
        (TargetTool.CODEX, CodexEmitter()),
        (TargetTool.CURSOR, CursorEmitter()),
        (TargetTool.OPENCODE, OpenCodeEmitter()),
        (TargetTool.PI, PiEmitter()),
    ],
)
def test_target_native_skill_override_rechecks_invariants_and_updates_evidence(
    target: TargetTool, emitter: object, tmp_path: Path
) -> None:
    plugin = tmp_path / "plugin"
    shutil.copytree(FIXTURE, plugin)
    nested = plugin / "skills/alpha/references/guide.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("`${CLAUDE_PLUGIN_ROOT}/shared/data.txt`")
    original = emitter.emit(parse_claude_plugin(plugin))  # type: ignore[union-attr]

    native_skill = plugin / _target_native_alpha_skill(target) / "SKILL.md"
    native_skill.parent.mkdir(parents=True)
    native_skill.write_text(
        "---\nname: "
        + ("alpha" if target == TargetTool.CODEX else "shared-includes-alpha")
        + "\ndescription: native safe override\n---\n\nRead `_shared/shared/data.txt`.\n"
    )
    result = emitter.emit(parse_claude_plugin(plugin))  # type: ignore[union-attr]
    assert not result.has_errors()
    alpha_evidence = {
        item.source_relative_path: item
        for item in result.include_evidence
        if item.consumer_skill == "alpha"
    }
    assert alpha_evidence["shared/data.txt"].direct_rewrite_count == 1
    assert alpha_evidence["shared/run.sh"].direct_rewrite_count == 0

    native_nested = plugin / _target_native_alpha_skill(target) / "references/guide.md"
    native_nested.parent.mkdir(parents=True, exist_ok=True)
    native_nested.write_text("Authored `_shared/shared/data.txt` reference")
    native_skill.write_text(
        "---\nname: unsafe\ndescription: unsafe\nx-ai-config-includes: [shared/data.txt]\n"
        "---\n\n${CLAUDE_PLUGIN_ROOT}/shared/data.txt\n"
    )
    unsafe = emitter.emit(parse_claude_plugin(plugin))  # type: ignore[union-attr]
    assert unsafe.has_errors()
    assert any("build metadata" in item.message for item in unsafe.diagnostics)
    assert any("CLAUDE_PLUGIN_ROOT" in item.message for item in unsafe.diagnostics)
    restored_skill = next(
        item for item in unsafe.files if item.path == _skill_roots(target)[0] / "SKILL.md"
    )
    assert "x-ai-config-includes" not in restored_skill.content
    assert "CLAUDE_PLUGIN_ROOT" not in restored_skill.content
    restored_nested = next(
        item
        for item in unsafe.files
        if item.path == _skill_roots(target)[0] / "references/guide.md"
    )
    assert restored_nested.content == "`_shared/shared/data.txt`"
    assert sorted(unsafe.include_evidence, key=lambda item: item.target_path.as_posix()) == sorted(
        original.include_evidence, key=lambda item: item.target_path.as_posix()
    )
    assert sorted(
        unsafe._markdown_rewrite_evidence,
        key=lambda item: (item.include_target_path, item.markdown_target_path),
    ) == sorted(
        original._markdown_rewrite_evidence,
        key=lambda item: (item.include_target_path, item.markdown_target_path),
    )


@pytest.mark.parametrize("target", [TargetTool.CURSOR, TargetTool.PI])
@pytest.mark.parametrize("simple", [False, True], ids=["best-effort", "simple"])
def test_mutating_conversion_never_writes_unsafe_target_native_skill(
    target: TargetTool, simple: bool, tmp_path: Path
) -> None:
    plugin = tmp_path / "plugin"
    shutil.copytree(FIXTURE, plugin)
    native_skill = plugin / _target_native_alpha_skill(target) / "SKILL.md"
    native_skill.parent.mkdir(parents=True)
    native_skill.write_text(
        "---\nname: unsafe\ndescription: unsafe\nx-ai-config-includes: [shared/data.txt]\n"
        "---\n\n${CLAUDE_PLUGIN_ROOT}/shared/data.txt\n"
    )
    output = tmp_path / "output"

    if simple:
        result = convert_plugin_simple(plugin, target, output)
        assert result.has_errors()
    else:
        report = convert_plugin(
            plugin,
            [target],
            output_dir=output,
            best_effort=True,
        )[target]
        assert report.errors

    alpha, beta = _skill_roots(target)
    assert (output / alpha / "SKILL.md").is_file()
    assert (output / beta / "SKILL.md").is_file()
    emitted_bytes = b"\n".join(
        path.read_bytes() for path in sorted(output.rglob("*")) if path.is_file()
    )
    assert b"x-ai-config-includes" not in emitted_bytes
    assert b"CLAUDE_PLUGIN_ROOT" not in emitted_bytes


def test_target_native_shared_override_removes_original_copy_evidence(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    shutil.copytree(FIXTURE, plugin)
    native = plugin / "targets/pi/skills/shared-includes-alpha/_shared/shared/data.txt"
    native.parent.mkdir(parents=True)
    native.write_text("native payload")
    result = PiEmitter().emit(parse_claude_plugin(plugin))
    target = Path(".pi/skills/shared-includes-alpha/_shared/shared/data.txt")
    emitted = next(item for item in result.files if item.path == target)
    assert emitted.content == "native payload"
    assert not any(item.target_path == target for item in result.include_evidence)


@pytest.mark.parametrize(
    ("target", "emitter"),
    [
        (TargetTool.CODEX, CodexEmitter()),
        (TargetTool.CURSOR, CursorEmitter()),
        (TargetTool.OPENCODE, OpenCodeEmitter()),
        (TargetTool.PI, PiEmitter()),
    ],
)
def test_target_native_nested_markdown_override_removes_only_its_rewrite_evidence(
    target: TargetTool, emitter: object, tmp_path: Path
) -> None:
    plugin = tmp_path / "plugin"
    shutil.copytree(FIXTURE, plugin)
    first = plugin / "skills/alpha/references/first.md"
    first.parent.mkdir(parents=True)
    first.write_text("`${CLAUDE_PLUGIN_ROOT}/shared/data.txt`")
    second = plugin / "skills/alpha/references/second.markdown"
    second.write_text(
        "`${CLAUDE_PLUGIN_ROOT}/shared/data.txt` and `${CLAUDE_PLUGIN_ROOT}/shared/data.txt`"
    )
    native = plugin / _target_native_alpha_skill(target) / "references/first.md"
    native.parent.mkdir(parents=True)
    native.write_text("Authored `_shared/shared/data.txt` and `_shared/shared/data.txt` references")

    result = emitter.emit(parse_claude_plugin(plugin))  # type: ignore[union-attr]

    assert not result.has_errors()
    data_evidence = next(
        item
        for item in result.include_evidence
        if item.consumer_skill == "alpha" and item.source_relative_path == "shared/data.txt"
    )
    assert data_evidence.direct_rewrite_count == 3
    alpha_root = _skill_roots(target)[0]
    positive_by_markdown = {
        item.markdown_target_path.relative_to(alpha_root).as_posix(): item.direct_rewrite_count
        for item in result._markdown_rewrite_evidence
        if item.include_target_path == data_evidence.target_path and item.direct_rewrite_count > 0
    }
    assert positive_by_markdown == {"SKILL.md": 1, "references/second.markdown": 2}


def test_nested_markdown_rewrites_from_skill_root_not_nested_file(tmp_path: Path) -> None:
    plugin = _plugin(
        tmp_path,
        ["shared/data.txt"],
        "body",
    )
    (plugin / "shared").mkdir()
    (plugin / "shared/data.txt").write_text("data")
    nested = plugin / "skills/bad/references/guide.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("`${CLAUDE_PLUGIN_ROOT}/shared/data.txt`")
    result = PiEmitter().emit(parse_claude_plugin(plugin))
    emitted = next(item for item in result.files if item.path.name == "guide.md")
    assert emitted.content == "`_shared/shared/data.txt`"


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        ".",
        "../outside.txt",
        "/tmp/outside.txt",
        "shared\\data.txt",
        "./shared/data.txt",
        "shared//data.txt",
        "shared/./data.txt",
        "shared/missing.txt",
        "shared/*.txt",
        "shared/bad\0name",
        3,
    ],
)
def test_invalid_include_paths_fail_and_best_effort_isolates_safe_skill(
    invalid: object, tmp_path: Path
) -> None:
    plugin = _plugin(tmp_path, [invalid])
    ir = parse_claude_plugin(plugin)
    assert ir.has_errors()
    assert [skill.name for skill in ir.skills()] == ["good"]

    output = tmp_path / "out"
    report = convert_plugin(
        plugin,
        [TargetTool.PI],
        output_dir=output,
        best_effort=True,
    )[TargetTool.PI]
    assert report.errors
    assert (output / ".pi/skills/safe-plugin-good/SKILL.md").is_file()
    assert not (output / ".pi/skills/safe-plugin-bad").exists()


def test_duplicate_collision_and_undeclared_placeholder_are_blocking(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, ["shared/data.txt", "shared/data.txt"])
    (plugin / "shared").mkdir()
    (plugin / "shared/data.txt").write_text("data")
    assert any(
        "duplicate include" in item.message for item in parse_claude_plugin(plugin).diagnostics
    )

    bad_skill = plugin / "skills/bad/SKILL.md"
    bad_skill.write_text(
        "---\nname: bad\ndescription: bad\nx-ai-config-includes: [shared/data.txt]\n---\n\n"
        "`${CLAUDE_PLUGIN_ROOT}/shared/other.txt`"
    )
    assert any(
        "reference remains" in item.message for item in parse_claude_plugin(plugin).diagnostics
    )

    bad_skill.write_text(
        "---\nname: bad\ndescription: bad\nx-ai-config-includes: [shared/data.txt]\n---\nbody"
    )
    collision = plugin / "skills/bad/_shared/shared/data.txt"
    collision.parent.mkdir(parents=True)
    collision.write_text("collision")
    assert any("collides" in item.message for item in parse_claude_plugin(plugin).diagnostics)


def test_include_directory_is_rejected(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, ["shared"])
    (plugin / "shared").mkdir()
    assert any(
        "not a regular file" in item.message for item in parse_claude_plugin(plugin).diagnostics
    )


def test_include_hardlink_is_rejected(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, ["shared/data.txt"])
    (plugin / "shared").mkdir()
    original = plugin / "shared/original.txt"
    original.write_text("data")
    os.link(original, plugin / "shared/data.txt")
    assert any("hardlink" in item.message for item in parse_claude_plugin(plugin).diagnostics)


def test_component_absolute_traversal_and_malformed_paths_are_rejected(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    for value in ("../outside", "/tmp/outside", "skills/bad\0name", 7):
        (plugin / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": "unsafe", "commands": value})
        )
        ir = parse_claude_plugin(plugin)
        assert ir.has_errors()
        assert not ir.commands()


def test_final_and_ancestor_symlinks_are_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: bad\ndescription: bad\n---\noutside")
    plugin = _plugin(tmp_path, [])
    (plugin / "skills/bad/SKILL.md").unlink()
    (plugin / "skills/bad/SKILL.md").symlink_to(outside)
    ir = parse_claude_plugin(plugin)
    assert ir.has_errors()
    assert [skill.name for skill in ir.skills()] == ["good"]

    shutil.rmtree(plugin / "skills/bad")
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (external_dir / "SKILL.md").write_text("---\nname: bad\ndescription: bad\n---\noutside")
    (plugin / "skills/bad").symlink_to(external_dir, target_is_directory=True)
    ir = parse_claude_plugin(plugin)
    assert ir.has_errors()
    assert [skill.name for skill in ir.skills()] == ["good"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_special_file_in_skill_is_rejected(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, [])
    os.mkfifo(plugin / "skills/bad/pipe")
    ir = parse_claude_plugin(plugin)
    assert ir.has_errors()
    assert [skill.name for skill in ir.skills()] == ["good"]


@pytest.mark.skipif(os.name != "posix", reason="raw-byte filenames require POSIX")
def test_hash_handles_non_utf8_posix_filename_deterministically(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    raw_path = os.path.join(os.fsencode(plugin), b"raw-\xff")
    try:
        descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as error:
        pytest.skip(f"filesystem rejects non-UTF-8 filenames: {error}")
    try:
        os.write(descriptor, b"payload")
    finally:
        os.close(descriptor)

    first = compute_plugin_hash(plugin)
    assert first is not None
    assert compute_plugin_hash(plugin) == first


def test_conversion_hash_ignores_only_parser_excluded_python_artifacts(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path, ["skills/bad/intentional.pyc"])
    generated = plugin / "skills/good/__pycache__/tool.cpython-313.pyc"
    generated.parent.mkdir()
    generated.write_bytes(b"first generated bytes")
    loose_generated = plugin / "skills/good/loose.pyc"
    loose_generated.write_bytes(b"first loose generated bytes")
    intentional = plugin / "targets/pi/intentional.pyc"
    intentional.parent.mkdir(parents=True)
    intentional.write_bytes(b"first intentional bytes")
    included = plugin / "skills/bad/intentional.pyc"
    included.write_bytes(b"first included bytes")
    referenced = plugin / "skills/good/__pycache__/referenced.pyc"
    referenced.write_bytes(b"first referenced bytes")
    manifest_path = plugin / ".claude-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["hooks"] = "./hooks/hooks.json"
    manifest_path.write_text(json.dumps(manifest))
    hooks = plugin / "hooks/hooks.json"
    hooks.parent.mkdir()
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PLUGIN_ROOT}/skills/good/__pycache__/referenced.pyc",
                                }
                            ]
                        }
                    ],
                    "Notification": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PLUGIN_ROOT}/skills/good/__pycache__/tool.cpython-313.pyc",
                                }
                            ]
                        }
                    ],
                }
            }
        )
    )

    _ir, ignored = parse_claude_plugin_with_ignored_generated_paths(plugin)
    full_before = compute_plugin_hash(plugin)
    conversion_before = compute_plugin_conversion_hash(plugin, ignored_paths=ignored)
    generated.write_bytes(b"second generated bytes")
    loose_generated.write_bytes(b"second loose generated bytes")

    assert compute_plugin_hash(plugin) != full_before
    assert compute_plugin_conversion_hash(plugin, ignored_paths=ignored) == conversion_before

    intentional.write_bytes(b"second intentional bytes")
    after_target_native_change = compute_plugin_conversion_hash(plugin, ignored_paths=ignored)
    assert after_target_native_change != conversion_before

    included.write_bytes(b"second included bytes")
    after_include_change = compute_plugin_conversion_hash(plugin, ignored_paths=ignored)
    assert after_include_change != after_target_native_change

    referenced.write_bytes(b"second referenced bytes")
    assert compute_plugin_conversion_hash(plugin, ignored_paths=ignored) != after_include_change

    _ir, cursor_ignored = parse_claude_plugin_with_ignored_generated_paths(
        plugin, target_native_targets=frozenset({TargetTool.CURSOR})
    )
    referenced_relative = referenced.relative_to(plugin)
    assert referenced_relative in cursor_ignored
    cursor_before = compute_plugin_conversion_hash(plugin, ignored_paths=cursor_ignored)
    referenced.write_bytes(b"third referenced bytes")
    assert compute_plugin_conversion_hash(plugin, ignored_paths=cursor_ignored) == cursor_before


def test_conversion_hash_tracks_independent_inputs_also_selected_as_skill_files(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "overlapping-skills",
                "skills": [
                    "./skills/main/SKILL.md",
                    "./skills/main/__pycache__/child/SKILL.md",
                    "./targets/codex/SKILL.md",
                ],
                "commands": "./skills/main/__pycache__/hello.md",
            }
        )
    )
    for skill_path, name in (
        (plugin / "skills/main/SKILL.md", "main"),
        (plugin / "targets/codex/SKILL.md", "native"),
    ):
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\nbody\n")
    generated = plugin / "skills/main/__pycache__/generated.pyc"
    generated.parent.mkdir()
    generated.write_bytes(b"first generated bytes")
    command = plugin / "skills/main/__pycache__/hello.md"
    command.write_text("first command contents")
    child_skill = plugin / "skills/main/__pycache__/child/SKILL.md"
    child_skill.parent.mkdir()
    child_skill.write_text(
        "---\nname: child\ndescription: nested child skill\n---\n\nfirst child contents\n"
    )
    target_native = plugin / "targets/codex/__pycache__/helper.pyc"
    target_native.parent.mkdir(parents=True)
    target_native.write_bytes(b"first target-native bytes")

    _ir, ignored = parse_claude_plugin_with_ignored_generated_paths(plugin)
    generated_relative = generated.relative_to(plugin)
    command_relative = command.relative_to(plugin)
    child_skill_relative = child_skill.relative_to(plugin)
    target_native_relative = target_native.relative_to(plugin)
    assert generated_relative in ignored
    assert command_relative not in ignored
    assert child_skill_relative not in ignored
    assert target_native_relative not in ignored

    conversion_before = compute_plugin_conversion_hash(plugin, ignored_paths=ignored)
    generated.write_bytes(b"second generated bytes")
    assert compute_plugin_conversion_hash(plugin, ignored_paths=ignored) == conversion_before

    command.write_text("second command contents")
    after_command_change = compute_plugin_conversion_hash(plugin, ignored_paths=ignored)
    assert after_command_change != conversion_before

    child_skill.write_text(
        "---\nname: child\ndescription: nested child skill\n---\n\nsecond child contents\n"
    )
    after_child_change = compute_plugin_conversion_hash(plugin, ignored_paths=ignored)
    assert after_child_change != after_command_change

    target_native.write_bytes(b"second target-native bytes")
    assert compute_plugin_conversion_hash(plugin, ignored_paths=ignored) != after_child_change

    _ir, cursor_ignored = parse_claude_plugin_with_ignored_generated_paths(
        plugin, target_native_targets=frozenset({TargetTool.CURSOR})
    )
    assert target_native_relative in cursor_ignored
    cursor_before = compute_plugin_conversion_hash(plugin, ignored_paths=cursor_ignored)
    target_native.write_bytes(b"third target-native bytes")
    assert compute_plugin_conversion_hash(plugin, ignored_paths=cursor_ignored) == cursor_before


def test_hash_accepts_exact_agent_context_mirror_and_tracks_it(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, [])
    agents = plugin / "AGENTS.md"
    agents.write_text("instructions\n")
    mirror = plugin / "CLAUDE.md"
    mirror.symlink_to("AGENTS.md")

    with_mirror = compute_plugin_hash(plugin)
    assert with_mirror is not None
    assert compute_plugin_hash(plugin) == with_mirror

    mirror.unlink()
    without_mirror = compute_plugin_hash(plugin)
    assert without_mirror is not None
    assert without_mirror != with_mirror


def test_hash_rejects_context_mirror_with_wrong_or_escaping_target(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, [])
    (plugin / "AGENTS.md").write_text("instructions\n")
    (plugin / "OTHER.md").write_text("other\n")
    mirror = plugin / "CLAUDE.md"
    mirror.symlink_to("OTHER.md")
    assert compute_plugin_hash(plugin) is None

    mirror.unlink()
    mirror.symlink_to(tmp_path / "outside")
    assert compute_plugin_hash(plugin) is None


def test_hash_ignores_only_installed_claude_cache_in_use_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-profile"))
    cache = tmp_path / "claude-profile/plugins/cache/marketplace/demo/1.0.0"
    cache.mkdir(parents=True)
    plugin = _plugin(cache, [])
    source = plugin / "commands.md"
    source.write_text("original")
    baseline = compute_plugin_hash(plugin, provenance="installed_plugin")
    assert baseline is not None
    conversion_baseline = compute_plugin_conversion_hash(
        plugin, ignored_paths=frozenset(), provenance="installed_plugin"
    )
    assert conversion_baseline == baseline

    markers = plugin / ".in_use"
    markers.mkdir()
    marker = markers / "12345"
    marker.write_text("")
    assert compute_plugin_hash(plugin, provenance="installed_plugin") == baseline
    marker.write_text("updated runtime marker")
    assert compute_plugin_hash(plugin, provenance="installed_plugin") == baseline
    assert (
        compute_plugin_conversion_hash(
            plugin, ignored_paths=frozenset(), provenance="installed_plugin"
        )
        == conversion_baseline
    )
    marker.unlink()
    assert compute_plugin_hash(plugin, provenance="installed_plugin") == baseline
    markers.rmdir()
    markers.symlink_to(tmp_path)
    assert compute_plugin_hash(plugin, provenance="installed_plugin") is None
    markers.unlink()

    source.write_text("changed")
    changed = compute_plugin_hash(plugin, provenance="installed_plugin")
    assert changed is not None and changed != baseline
    source.chmod(source.stat().st_mode | 0o100)
    executable = compute_plugin_hash(plugin, provenance="installed_plugin")
    assert executable is not None and executable != changed
    source.rename(plugin / "renamed.md")
    renamed = compute_plugin_hash(plugin, provenance="installed_plugin")
    assert renamed is not None and renamed != executable

    nested = plugin / "nested/.in_use"
    nested.mkdir(parents=True)
    nested_marker = nested / "12345"
    nested_marker.write_text("plugin data")
    assert compute_plugin_hash(plugin, provenance="installed_plugin") != renamed
    nested_marker.unlink()
    nested_marker.symlink_to(tmp_path)
    assert compute_plugin_hash(plugin, provenance="installed_plugin") is None


def test_hash_keeps_local_and_non_cache_in_use_significant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-profile"))
    cache = tmp_path / "claude-profile/plugins/cache/demo"
    cache.mkdir(parents=True)
    plugin = _plugin(cache, [])
    baseline = compute_plugin_hash(plugin, provenance="configured_local")
    assert baseline is not None
    markers = plugin / ".in_use"
    markers.mkdir()
    marker = markers / "12345"
    marker.write_text("meaningful plugin data")
    assert compute_plugin_hash(plugin, provenance="configured_local") != baseline
    assert compute_plugin_hash(plugin, provenance="observed_remote_marketplace") != baseline
    assert (
        compute_plugin_conversion_hash(
            plugin, ignored_paths=frozenset(), provenance="configured_local"
        )
        != baseline
    )
    marker.unlink()
    marker.symlink_to(tmp_path)
    assert compute_plugin_hash(plugin, provenance="configured_local") is None
    marker.unlink()
    markers.rmdir()

    outside = _plugin(tmp_path, [])
    outside_baseline = compute_plugin_hash(outside, provenance="installed_plugin")
    (outside / ".in_use").mkdir()
    (outside / ".in_use/12345").write_text("meaningful plugin data")
    assert compute_plugin_hash(outside, provenance="installed_plugin") != outside_baseline
    (outside / ".in_use/12345").unlink()
    (outside / ".in_use").rmdir()
    (outside / ".in_use").symlink_to(tmp_path)
    assert compute_plugin_hash(outside, provenance="installed_plugin") is None


def test_hash_does_not_ignore_unsafe_or_non_directory_in_use(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, [])
    baseline = compute_plugin_hash(plugin)
    assert baseline is not None
    marker_path = plugin / ".in_use"
    marker_path.write_text("plugin data")
    assert compute_plugin_hash(plugin) != baseline
    marker_path.unlink()
    marker_path.symlink_to(tmp_path)
    assert compute_plugin_hash(plugin) is None
    marker_path.unlink()

    # Exclusion cannot make a symlink in the source-root ancestry safe.
    parent_link = tmp_path / "linked"
    parent_link.symlink_to(plugin, target_is_directory=True)
    assert compute_plugin_hash(parent_link) is None


def test_hash_includes_shared_bytes_and_fails_closed_on_symlink(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, ["shared/data.txt"])
    (plugin / "shared").mkdir()
    shared = plugin / "shared/data.txt"
    shared.write_text("one")
    before = compute_plugin_hash(plugin)
    shared.write_text("two")
    assert compute_plugin_hash(plugin) != before
    (plugin / "unsafe").symlink_to(tmp_path / "outside")
    assert compute_plugin_hash(plugin) is None


@pytest.mark.parametrize("legacy_version", [7, 8])
def test_legacy_cache_entries_are_invalidated_for_logical_source_identity(
    legacy_version: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".ai-config/cache/conversion-hashes.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "version": legacy_version,
                "entries": {"old": {}},
                "codex_output_dirs": ["/custom/codex"],
                "pi_output_dirs": ["/custom/pi"],
            }
        )
    )
    loaded = load_conversion_cache()
    assert loaded == {
        "version": 9,
        "entries": {},
        "codex_output_dirs": ["/custom/codex"],
        "pi_output_dirs": ["/custom/pi"],
    }


def test_cache_v9_rejects_malformed_logical_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".ai-config/cache/conversion-hashes.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "version": 9,
                "entries": {"demo@market": {"not-json": {"hash": "digest"}}},
                "codex_output_dirs": [],
                "pi_output_dirs": [],
            }
        )
    )
    with pytest.raises(ValueError, match="Invalid conversion cache entries"):
        load_conversion_cache()


@pytest.mark.parametrize("version", [7, 8, 9])
@pytest.mark.parametrize("invalid_root", ["relative/root", "~ai_config_no_such_user/root"])
def test_cache_rejects_nonabsolute_or_unexpandable_output_roots(
    version: int,
    invalid_root: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".ai-config/cache/conversion-hashes.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "version": version,
                "entries": {},
                "codex_output_dirs": [invalid_root],
                "pi_output_dirs": [],
            }
        )
    )
    with pytest.raises(ValueError, match="Invalid conversion cache Codex output roots"):
        load_conversion_cache()


@pytest.mark.parametrize("version", [7, 8, 9])
def test_cache_rejects_symlink_loop_output_root(
    version: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b.name)
    loop_b.symlink_to(loop_a.name)
    cache = tmp_path / ".ai-config/cache/conversion-hashes.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "version": version,
                "entries": {},
                "codex_output_dirs": [str(loop_a / "output")],
                "pi_output_dirs": [],
            }
        )
    )
    with pytest.raises(ValueError, match="Invalid conversion cache Codex output roots"):
        load_conversion_cache()


@pytest.mark.parametrize("version", [7, 8, 9])
def test_cache_normalizes_and_deduplicates_preserved_output_roots(
    version: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".ai-config/cache/conversion-hashes.json"
    cache.parent.mkdir(parents=True)
    canonical = (tmp_path / "outputs").resolve()
    cache.write_text(
        json.dumps(
            {
                "version": version,
                "entries": {},
                "codex_output_dirs": [str(canonical), str(canonical / ".." / "outputs")],
                "pi_output_dirs": [],
            }
        )
    )
    assert load_conversion_cache()["codex_output_dirs"] == [str(canonical)]


def test_cache_v7_rejects_invalid_preserved_output_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".ai-config/cache/conversion-hashes.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "version": 7,
                "entries": {},
                "codex_output_dirs": ["/valid"],
                "pi_output_dirs": ["bad\0root"],
            }
        )
    )
    with pytest.raises(ValueError, match="Invalid conversion cache Pi output roots"):
        load_conversion_cache()


def test_codex_support_file_rejects_ancestor_symlink(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.sh").write_text("#!/bin/sh\n")
    (plugin / "scripts").symlink_to(outside, target_is_directory=True)
    ir = PluginIR(
        identity=PluginIdentity(plugin_id="unsafe", name="unsafe"),
        source_path=plugin,
        components=[
            McpServer(
                name="unsafe",
                command="sh",
                args=["${CLAUDE_PLUGIN_ROOT}/scripts/run.sh"],
            )
        ],
    )
    result = CodexEmitter().emit(ir)
    assert result.has_errors()
    assert not any(item.path.as_posix().endswith("scripts/run.sh") for item in result.files)


def test_pi_ledger_owns_copies_and_preserves_tampered_stale_include(tmp_path: Path) -> None:
    plugin = tmp_path / "source"
    shutil.copytree(FIXTURE, plugin)
    output = tmp_path / "output"
    convert_plugin(plugin, [TargetTool.PI], output_dir=output)
    alpha, beta = _skill_roots(TargetTool.PI)
    alpha_copy = alpha / "_shared/shared/data.txt"
    beta_copy = beta / "_shared/shared/data.txt"
    ownership = load_pi_ownership(output)
    assert alpha_copy in ownership
    assert beta_copy in ownership

    (output / alpha_copy).write_text("local edit")
    _remove_include_metadata(plugin)
    convert_plugin(plugin, [TargetTool.PI], output_dir=output)
    assert (output / alpha_copy).read_text() == "local edit"
    assert not (output / beta_copy).exists()


@pytest.mark.parametrize("target", [TargetTool.CURSOR, TargetTool.OPENCODE])
def test_unowned_targets_do_not_delete_removed_include_copies(
    target: TargetTool, tmp_path: Path
) -> None:
    plugin = tmp_path / "source"
    shutil.copytree(FIXTURE, plugin)
    output = tmp_path / "output"
    convert_plugin(plugin, [target], output_dir=output)
    alpha, _beta = _skill_roots(target)
    old_copy = output / alpha / "_shared/shared/data.txt"
    assert old_copy.is_file()
    _remove_include_metadata(plugin)
    convert_plugin(plugin, [target], output_dir=output)
    assert old_copy.is_file()


def test_codex_removed_include_copy_stays_within_rebuilt_package_root(tmp_path: Path) -> None:
    plugin = tmp_path / "source"
    shutil.copytree(FIXTURE, plugin)
    output = tmp_path / "output"
    unrelated = output / "keep.txt"
    unrelated.parent.mkdir()
    unrelated.write_text("keep")
    convert_plugin(plugin, [TargetTool.CODEX], output_dir=output)
    alpha, _beta = _skill_roots(TargetTool.CODEX)
    old_copy = output / alpha / "_shared/shared/data.txt"
    assert old_copy.is_file()
    _remove_include_metadata(plugin)
    convert_plugin(plugin, [TargetTool.CODEX], output_dir=output)
    assert not old_copy.exists()
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize(
    "target",
    [TargetTool.CODEX, TargetTool.CURSOR, TargetTool.OPENCODE, TargetTool.PI],
)
def test_reports_match_final_projection_after_native_markdown_override(
    target: TargetTool, tmp_path: Path
) -> None:
    plugin = tmp_path / "plugin"
    shutil.copytree(FIXTURE, plugin)
    nested = plugin / "skills/alpha/references/guide.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("`${CLAUDE_PLUGIN_ROOT}/shared/data.txt`")
    native_skill = plugin / _target_native_alpha_skill(target) / "SKILL.md"
    native_skill.parent.mkdir(parents=True)
    native_skill.write_text(
        "---\nname: "
        + ("alpha" if target == TargetTool.CODEX else "shared-includes-alpha")
        + "\ndescription: native safe override\n---\n\nAuthored `_shared/shared/data.txt`.\n"
    )

    report = convert_plugin(plugin, [target], output_dir=tmp_path / "output")[target]

    assert not report.errors
    payload = next(
        item
        for item in report.to_dict()["includes"]
        if item["consumer_skill"] == "alpha" and item["source"] == "shared/data.txt"
    )
    assert payload["direct_rewrite_count"] == 1
    expected_line = (
        f"`shared/data.txt` → `{payload['target_path']}` for `alpha` "
        f"(1 copy, {payload['duplicated_bytes']:,} bytes, 1 direct rewrites)"
    )
    assert expected_line in report.to_markdown()
    alpha_root = tmp_path / "output" / _skill_roots(target)[0]
    assert "Authored `_shared/shared/data.txt`" in (alpha_root / "SKILL.md").read_text()
    assert (alpha_root / "references/guide.md").read_text() == "`_shared/shared/data.txt`"


def test_report_contains_additive_relative_include_evidence(tmp_path: Path) -> None:
    report = convert_plugin(FIXTURE, [TargetTool.CURSOR], output_dir=tmp_path)[TargetTool.CURSOR]
    assert len(report.includes) == 8
    payload = report.to_dict()["includes"]
    assert payload[0]["source"] == "shared/data.txt"
    assert not Path(payload[0]["source"]).is_absolute()
    assert payload[0]["copy_count"] == 1
    assert payload[0]["duplicated_bytes"] == len((FIXTURE / "shared/data.txt").read_bytes())
    markdown = report.to_markdown()
    assert f"{payload[0]['copy_count']} copy" in markdown
    assert f"{payload[0]['duplicated_bytes']:,} bytes" in markdown
    assert f"{payload[0]['direct_rewrite_count']} direct rewrites" in markdown


def test_target_validator_invariants_reject_build_metadata_and_root_placeholder(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("`${CLAUDE_PLUGIN_ROOT}/shared/data.txt`")
    errors = generated_skill_invariant_errors(
        skill, {"name": "skill", "x-ai-config-includes": ["shared/data.txt"]}
    )
    assert any("build metadata" in message for message in errors)
    assert any("CLAUDE_PLUGIN_ROOT" in message for message in errors)


@pytest.mark.parametrize(
    ("target", "validator"),
    [
        (TargetTool.CODEX, CodexOutputValidator()),
        (TargetTool.CURSOR, CursorOutputValidator()),
        (TargetTool.OPENCODE, OpenCodeOutputValidator()),
        (TargetTool.PI, PiOutputValidator()),
    ],
)
def test_shared_resource_output_passes_each_target_validator(
    target: TargetTool, validator: object, tmp_path: Path
) -> None:
    report = convert_plugin(FIXTURE, [target], output_dir=tmp_path)[target]
    assert not report.errors
    results = validator.validate_all(tmp_path)  # type: ignore[union-attr]
    assert not [result for result in results if result.status == "fail"]


def test_skill_without_include_metadata_retains_native_projection() -> None:
    ir = parse_claude_plugin(Path(__file__).parents[2] / "fixtures/sample-plugins/complete-plugin")
    result = CursorEmitter().emit(ir)
    assert not result.include_evidence
    assert all("_shared" not in item.path.parts for item in result.files)
    assert all(
        item.status == MappingStatus.NATIVE
        for item in result.mappings
        if item.component_kind == "skill"
    )
