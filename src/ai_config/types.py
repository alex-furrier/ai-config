"""Type definitions for ai-config."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypeAlias

CodexLifecycleActionName: TypeAlias = Literal[
    "register_codex_marketplace",
    "install_codex_plugin",
    "update_codex_plugin",
    "reinstall_codex_plugin",
    "noop_codex_plugin",
    "remove_codex_plugin",
    "remove_codex_marketplace",
    "remove_codex_package",
    "write_codex_ownership",
]
SyncActionName: TypeAlias = (
    Literal[
        "install",
        "uninstall",
        "enable",
        "disable",
        "register_marketplace",
    ]
    | CodexLifecycleActionName
    | Literal[
        "create_pi_output",
        "update_pi_output",
        "remove_pi_output",
        "noop_pi_output",
        "preserve_pi_output",
    ]
)


class PluginSource(str, Enum):
    """Source type for plugin marketplaces."""

    GITHUB = "github"
    LOCAL = "local"


@dataclass(frozen=True)
class MarketplaceConfig:
    """Configuration for a plugin marketplace."""

    source: PluginSource
    repo: str = ""
    path: str = ""

    def __post_init__(self) -> None:
        if self.source == PluginSource.GITHUB:
            if not self.repo:
                raise ValueError("Marketplace repo cannot be empty for github source")
            if "/" not in self.repo:
                raise ValueError(
                    f"Marketplace repo must be in 'owner/repo' format, got: {self.repo}"
                )
        elif self.source == PluginSource.LOCAL:
            if not self.path:
                raise ValueError("Marketplace path cannot be empty for local source")


@dataclass(frozen=True)
class PluginConfig:
    """Configuration for a single plugin."""

    id: str
    scope: Literal["user", "project", "local"] = "user"
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Plugin id cannot be empty")
        if self.id.count("@") > 1 or "@" in self.id and not all(self.id.split("@", 1)):
            raise ValueError(
                f"Plugin id must be 'plugin-name' or 'plugin-name@marketplace-name', got: {self.id}"
            )

    @property
    def marketplace(self) -> str | None:
        """Extract marketplace name from plugin id (format: plugin@marketplace)."""
        if "@" in self.id:
            return self.id.split("@", 1)[1]
        return None

    @property
    def plugin_name(self) -> str:
        """Extract plugin name without marketplace suffix."""
        if "@" in self.id:
            return self.id.split("@", 1)[0]
        return self.id


@dataclass(frozen=True)
class ConversionConfig:
    """Configuration for plugin conversion targets."""

    enabled: bool = True
    targets: tuple[Literal["codex", "cursor", "opencode", "pi", "prime"], ...] = field(
        default_factory=tuple
    )
    scope: Literal["user", "project"] = "project"
    output_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not self.targets:
            raise ValueError("Conversion targets cannot be empty")
        valid_targets = {"codex", "cursor", "opencode", "pi", "prime"}
        for target in self.targets:
            if target not in valid_targets:
                raise ValueError(f"Invalid conversion target: {target}")
        if self.scope not in ("user", "project"):
            raise ValueError(f"Conversion scope must be 'user' or 'project', got: {self.scope}")


@dataclass(frozen=True)
class ClaudeTargetConfig:
    """Configuration specific to Claude Code target."""

    marketplaces: dict[str, MarketplaceConfig] = field(default_factory=dict)
    plugins: tuple[PluginConfig, ...] = field(default_factory=tuple)
    conversion: ConversionConfig | None = None


@dataclass(frozen=True)
class TargetConfig:
    """Configuration for a target AI tool."""

    type: Literal["claude"]
    config: ClaudeTargetConfig

    def __post_init__(self) -> None:
        if self.type != "claude":
            raise ValueError(f"v1 only supports 'claude', got: {self.type}")


@dataclass(frozen=True)
class AIConfig:
    """Root configuration for ai-config."""

    version: int
    targets: tuple[TargetConfig, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError(f"Only version 1 is supported, got: {self.version}")


@dataclass
class PluginStatus:
    """Status of a single plugin."""

    id: str
    installed: bool = False
    enabled: bool = False
    scope: Literal["user", "project", "local"] | None = None
    version: str | None = None


@dataclass(frozen=True)
class SyncAction:
    """A single action to be taken during sync."""

    action: SyncActionName
    target: str  # plugin id or marketplace name
    scope: Literal["user", "project", "local"] | None = None
    reason: str = ""


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool = True
    actions_taken: list[SyncAction] = field(default_factory=list)
    actions_failed: list[SyncAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_success(self, action: SyncAction) -> None:
        """Record a successful action."""
        self.actions_taken.append(action)

    def add_failure(self, action: SyncAction, error: str) -> None:
        """Record a failed action."""
        self.actions_failed.append(action)
        self.errors.append(error)
        self.success = False


@dataclass
class StatusResult:
    """Result of a status check."""

    target_type: Literal["claude"]
    plugins: list[PluginStatus] = field(default_factory=list)
    marketplaces: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
