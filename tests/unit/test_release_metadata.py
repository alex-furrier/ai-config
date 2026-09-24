"""Release metadata stays truthful until a release is actually published."""

from pathlib import Path

_REPO_ROOT = Path(__file__).parents[2]


def test_0_6_0_release_metadata_is_finalized() -> None:
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text()

    assert "## [0.6.0] - 2026-07-17" in changelog
    assert "[0.6.0]: https://github.com/safurrier/ai-config/compare/v0.5.0...v0.6.0" in changelog


def test_0_6_2_release_metadata_is_finalized() -> None:
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text()
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text()
    package_init = (_REPO_ROOT / "src/ai_config/__init__.py").read_text()
    lockfile = (_REPO_ROOT / "uv.lock").read_text()

    assert 'version = "0.6.2"' in pyproject
    assert '__version__ = "0.6.2"' in package_init
    assert 'name = "ai-config-cli"\nversion = "0.6.2"' in lockfile
    assert "## [0.6.2] - 2026-07-29" in changelog
    assert (
        "[Unreleased]: https://github.com/alex-furrier/ai-config/compare/v0.6.2...HEAD" in changelog
    )
    assert "[0.6.2]: https://github.com/safurrier/ai-config/compare/v0.6.1...v0.6.2" in changelog


def test_current_repository_metadata_uses_renamed_owner() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text()
    assert 'name = "ai-config-cli"' in pyproject
    assert 'Homepage = "https://github.com/alex-furrier/ai-config"' in pyproject
    assert 'Documentation = "https://alex-furrier.github.io/ai-config/"' in pyproject
    assert 'Repository = "https://github.com/alex-furrier/ai-config"' in pyproject
    assert 'Issues = "https://github.com/alex-furrier/ai-config/issues"' in pyproject
    assert (
        'Changelog = "https://github.com/alex-furrier/ai-config/blob/main/CHANGELOG.md"'
        in pyproject
    )
    instructions = (_REPO_ROOT / "AGENTS.md").read_text()
    assert (
        "GitHub repository alex-furrier/ai-config, workflow `publish.yml`, environment `pypi`"
        in instructions
    )


def test_release_checklist_assigns_date_at_release_time() -> None:
    instructions = (_REPO_ROOT / "AGENTS.md").read_text()

    assert (
        "Move `[Unreleased]` entries to a new version section with the actual release date"
        in instructions
    )
