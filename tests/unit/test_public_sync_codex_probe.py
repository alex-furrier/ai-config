"""The public-sync probe only admits observed source-less catalog contracts."""

import importlib
from collections.abc import Callable
from pathlib import Path

import pytest

CatalogContract = Callable[[str, set[tuple[str, str]]], None]


@pytest.fixture
def assert_catalog_contract(monkeypatch: pytest.MonkeyPatch) -> CatalogContract:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "probes"))
    return importlib.import_module(
        "probe_ai_config_sync_codex"
    )._assert_source_less_catalog_contract


@pytest.mark.parametrize(
    "version", ["0.148.0", "0.149.0", "0.153.3", "0.156.1", "0.157.0", "0.159.2"]
)
def test_source_less_catalog_hidden_for_supported_versions(
    assert_catalog_contract: CatalogContract, version: str
) -> None:
    assert_catalog_contract(f"codex-cli {version}", set())
    with pytest.raises(AssertionError, match="unexpectedly exposed"):
        assert_catalog_contract(
            f"codex-cli {version}",
            {("source-less-marketplace", "source-less-plugin@source-less-marketplace")},
        )


@pytest.mark.parametrize("version", ["0.159.0", "0.159.1", "0.159.2-rc.1", "0.159.2+other"])
def test_source_less_catalog_unknown_version_fails_closed(
    assert_catalog_contract: CatalogContract, version: str
) -> None:
    with pytest.raises(AssertionError, match="unsupported Codex public-sync probe version"):
        assert_catalog_contract(f"codex-cli {version}", set())


@pytest.mark.parametrize("version", ["0.159.3", "0.160.0"])
def test_future_catalog_visibility_is_not_assumed(
    assert_catalog_contract: CatalogContract, version: str
) -> None:
    assert_catalog_contract(f"codex-cli {version}", set())
    assert_catalog_contract(
        f"codex-cli {version}",
        {("source-less-marketplace", "source-less-plugin@source-less-marketplace")},
    )


def test_earlier_source_less_catalog_requires_visibility(
    assert_catalog_contract: CatalogContract,
) -> None:
    with pytest.raises(AssertionError, match="did not expose"):
        assert_catalog_contract("codex-cli 0.147.0", set())
    assert_catalog_contract(
        "codex-cli 0.147.0",
        {("source-less-marketplace", "source-less-plugin@source-less-marketplace")},
    )
