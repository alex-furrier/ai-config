"""Repeated public sync must verify without mutating unrelated native targets."""

import json
from pathlib import Path

import pytest

from ai_config.adapters.claude import InstalledPlugin
from ai_config.adapters.codex import CodexInstalledPlugin, CodexMarketplace, CodexPluginInstall
from ai_config.operations import apply_sync_plan, build_sync_plan, sync_target, verify_sync
from ai_config.types import (
    AIConfig,
    ClaudeTargetConfig,
    ConversionConfig,
    PluginConfig,
    TargetConfig,
)


class CodexRuntime:
    """Stateful native boundary; use the real conversion and lifecycle implementations."""

    def __init__(self):
        self.marketplaces = {}
        self.plugins = {}
        self.calls = []

    def list_marketplaces(self):
        return list(self.marketplaces.values())

    def list_plugins(self):
        return list(self.plugins.values())

    def add_marketplace(self, path, expected_name):
        item = CodexMarketplace(expected_name, Path(path).resolve(), "local")
        self.marketplaces[expected_name] = item
        self.calls.append(("add_marketplace", expected_name))
        return item

    def remove_marketplace(self, name):
        self.marketplaces.pop(name, None)
        self.calls.append(("remove_marketplace", name))

    def add_plugin(self, plugin_id):
        name, marketplace = plugin_id.split("@", 1)
        root = self.marketplaces[marketplace].root
        source = root / "plugins" / name
        version = json.loads((source / ".codex-plugin/plugin.json").read_text())["version"]
        self.plugins[plugin_id] = CodexInstalledPlugin(
            plugin_id, name, marketplace, version, True, source, root
        )
        self.calls.append(("add_plugin", plugin_id))
        return CodexPluginInstall(plugin_id, name, marketplace, version, source)

    def remove_plugin(self, plugin_id):
        self.plugins.pop(plugin_id, None)
        self.calls.append(("remove_plugin", plugin_id))


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "source"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text('{"name":"demo","version":"1.0.0"}')
    skill = source / "skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: Review carefully\n---\nCheck the source.\n")
    installed = [InstalledPlugin("demo", "1.0.0", "user", True, str(source))]
    monkeypatch.setattr("ai_config.operations.claude.list_installed_marketplaces", lambda: ([], []))
    monkeypatch.setattr(
        "ai_config.operations.claude.list_installed_plugins", lambda: (installed, [])
    )
    native = CodexRuntime()
    monkeypatch.setattr("ai_config.codex_lifecycle.CodexCLI", lambda: native)
    return source, tmp_path / "output", native


def target_for(output, targets):
    return TargetConfig(
        type="claude",
        config=ClaudeTargetConfig(
            plugins=(PluginConfig("demo"),),
            conversion=ConversionConfig(targets=targets, output_dir=str(output)),
        ),
    )


def test_prime_public_verification_accepts_noop_but_still_rejects_edits(runtime):
    _source, output, _native = runtime
    target = target_for(output, ("prime",))
    assert sync_target(target).success
    assert sync_target(target).success
    config = AIConfig(version=1, targets=(target,))
    assert verify_sync(config) == []
    skill = output / ".prime/agent/skills/demo-review/SKILL.md"
    skill.write_text("USER EDIT")
    assert verify_sync(config)
    assert not sync_target(target).success
    assert skill.read_text() == "USER EDIT"


def test_prime_revalidation_does_not_reinstall_an_unchanged_codex_package(runtime):
    source, output, native = runtime
    target = target_for(output, ("prime", "codex"))
    first = sync_target(target)
    assert first.success, first.errors
    before = list(native.calls)
    repeat = build_sync_plan(target)
    assert not repeat.is_blocked
    assert not any(item.action.action == "update_codex_plugin" for item in repeat.actions)
    assert apply_sync_plan(repeat).errors == ()
    assert native.calls == before
    assert verify_sync(AIConfig(version=1, targets=(target,))) == []

    forced = sync_target(target, force_convert=True)
    assert forced.success, forced.errors
    assert native.calls[len(before) :] == [
        ("remove_plugin", "demo@ai-config-demo"),
        ("add_plugin", "demo@ai-config-demo"),
    ]
    before = list(native.calls)
    skill = source / "skills/review/SKILL.md"
    skill.write_text(skill.read_text() + "Check recovery too.\n")
    changed = sync_target(target)
    assert changed.success, changed.errors
    assert native.calls[len(before) :] == [
        ("remove_plugin", "demo@ai-config-demo"),
        ("add_plugin", "demo@ai-config-demo"),
    ]
    assert verify_sync(AIConfig(version=1, targets=(target,))) == []
