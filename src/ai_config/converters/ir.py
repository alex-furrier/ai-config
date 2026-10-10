"""Intermediate Representation (IR) for plugin conversion.

This module defines Pydantic models that serve as the canonical representation
of plugin components during conversion between AI coding tools.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_config.source_safety import normalize_source_relative


class TargetTool(str, Enum):
    """Supported AI coding tools."""

    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"
    OPENCODE = "opencode"
    PI = "pi"
    PRIME = "prime"


class InstallScope(str, Enum):
    """Installation scope for plugin components."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"  # uncommitted machine-local where supported


class ComponentKind(str, Enum):
    """Types of plugin components."""

    SKILL = "skill"
    COMMAND = "command"
    HOOK = "hook"
    MCP_SERVER = "mcp_server"
    AGENT = "agent"
    LSP_SERVER = "lsp_server"
    FILE = "file"


class MappingStatus(str, Enum):
    """How a component maps to a target tool."""

    NATIVE = "native"  # Direct representation exists
    TRANSFORM = "transform"  # Config/schema conversion required
    EMULATE = "emulate"  # Implement as wrapper mechanism
    FALLBACK = "fallback"  # Degrade to prompt/command
    UNSUPPORTED = "unsupported"  # No equivalent


class Severity(str, Enum):
    """Diagnostic severity levels."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class Diagnostic(BaseModel):
    """A diagnostic message from parsing or conversion."""

    severity: Severity
    message: str
    component_ref: str | None = None  # e.g. "skill:my-skill"
    source_path: Path | None = None


class PluginIdentity(BaseModel):
    """Plugin identification and metadata."""

    plugin_id: str = Field(..., description="Stable ID for namespacing")
    name: str
    version: str | None = None
    description: str | None = None

    @field_validator("plugin_id")
    @classmethod
    def validate_plugin_id(cls, v: str) -> str:
        """Ensure plugin_id is kebab-case."""
        import re

        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", v):
            raise ValueError(f"plugin_id must be kebab-case: {v}")
        return v


# --- File Types ---


class TextFile(BaseModel):
    """A text file to include in the plugin."""

    relpath: str
    content: str
    executable: bool = False


class BinaryFile(BaseModel):
    """A binary file (base64 encoded) to include in the plugin."""

    relpath: str
    content_b64: str
    executable: bool = False


AnyFile = TextFile | BinaryFile


class SkillInclude(BaseModel):
    """Immutable plugin-root file captured for one generated skill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_relative_path: str
    content: bytes
    executable: bool = False

    @field_validator("source_relative_path")
    @classmethod
    def validate_source_relative_path(cls, value: str) -> str:
        """Require a canonical safe plugin-relative path in direct IR construction."""
        normalized = normalize_source_relative(value, context="skill include").as_posix()
        if normalized != value:
            raise ValueError(f"skill include path must be canonical: {value!r}")
        return normalized

    @property
    def projected_path(self) -> str:
        """Derive the reserved per-skill projection path from the safe source path."""
        return f"_shared/{self.source_relative_path}"


# --- Component Types ---


class Skill(BaseModel):
    """A skill component (SKILL.md based)."""

    kind: Literal[ComponentKind.SKILL] = ComponentKind.SKILL
    name: str
    description: str | None = None
    scope_hint: InstallScope = InstallScope.USER
    entrypoint: str = "SKILL.md"
    files: list[AnyFile] = Field(default_factory=list)
    includes: tuple[SkillInclude, ...] = ()

    # Claude-specific fields that may not convert
    allowed_tools: list[str] | None = None
    model: str | None = None
    context: str | None = None  # "fork" for subagent
    agent: str | None = None  # subagent type
    user_invocable: bool = True
    disable_model_invocation: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate skill name constraints."""
        import re

        # Most restrictive: OpenCode requires lowercase kebab-case, max 64 chars
        if len(v) > 64:
            raise ValueError(f"Skill name too long (max 64): {v}")
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", v):
            raise ValueError(f"Skill name must be lowercase kebab-case: {v}")
        return v


class Command(BaseModel):
    """A command component (slash command)."""

    kind: Literal[ComponentKind.COMMAND] = ComponentKind.COMMAND
    name: str
    description: str | None = None
    scope_hint: InstallScope = InstallScope.USER
    markdown: str
    argument_hint: str | None = None

    # Template variables present in markdown
    has_arguments_var: bool = False
    has_positional_vars: bool = False


class HookHandlerType(str, Enum):
    """Types of hook handlers."""

    COMMAND = "command"
    PROMPT = "prompt"
    AGENT = "agent"


class HookHandler(BaseModel):
    """A single hook handler definition."""

    type: HookHandlerType
    command: str | None = None
    prompt: str | None = None
    timeout_sec: int | None = None
    is_async: bool = False


class HookEvent(BaseModel):
    """A hook event with its handlers."""

    name: str  # e.g. "PreToolUse", "PostToolUse"
    matcher: str | None = None  # e.g. "Bash", "Write|Edit"
    handlers: list[HookHandler] = Field(default_factory=list)


class Hook(BaseModel):
    """Hook configuration component."""

    kind: Literal[ComponentKind.HOOK] = ComponentKind.HOOK
    scope_hint: InstallScope = InstallScope.USER
    events: list[HookEvent] = Field(default_factory=list)


class McpTransport(str, Enum):
    """MCP server transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


class McpServer(BaseModel):
    """MCP server configuration component."""

    kind: Literal[ComponentKind.MCP_SERVER] = ComponentKind.MCP_SERVER
    name: str
    scope_hint: InstallScope = InstallScope.USER
    transport: McpTransport = McpTransport.STDIO
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    timeout_ms: int | None = None


class Agent(BaseModel):
    """Agent definition component (Claude-specific)."""

    kind: Literal[ComponentKind.AGENT] = ComponentKind.AGENT
    name: str
    description: str | None = None
    scope_hint: InstallScope = InstallScope.USER
    markdown: str
    capabilities: list[str] = Field(default_factory=list)


class LspServer(BaseModel):
    """LSP server configuration component."""

    kind: Literal[ComponentKind.LSP_SERVER] = ComponentKind.LSP_SERVER
    name: str
    scope_hint: InstallScope = InstallScope.USER
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)  # e.g. [".go", ".py"]
    env: dict[str, str] = Field(default_factory=dict)
    initialization_options: dict[str, Any] = Field(default_factory=dict)


Component = Skill | Command | Hook | McpServer | Agent | LspServer


class PluginIR(BaseModel):
    """Complete Intermediate Representation of a plugin."""

    identity: PluginIdentity
    components: list[Component] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    source_path: Path | None = None

    def skills(self) -> list[Skill]:
        """Get all skill components."""
        return [c for c in self.components if isinstance(c, Skill)]

    def commands(self) -> list[Command]:
        """Get all command components."""
        return [c for c in self.components if isinstance(c, Command)]

    def hooks(self) -> list[Hook]:
        """Get all hook components."""
        return [c for c in self.components if isinstance(c, Hook)]

    def mcp_servers(self) -> list[McpServer]:
        """Get all MCP server components."""
        return [c for c in self.components if isinstance(c, McpServer)]

    def agents(self) -> list[Agent]:
        """Get all agent components."""
        return [c for c in self.components if isinstance(c, Agent)]

    def lsp_servers(self) -> list[LspServer]:
        """Get all LSP server components."""
        return [c for c in self.components if isinstance(c, LspServer)]

    def add_diagnostic(
        self,
        severity: Severity,
        message: str,
        component_ref: str | None = None,
        source_path: Path | None = None,
    ) -> None:
        """Add a diagnostic message."""
        self.diagnostics.append(
            Diagnostic(
                severity=severity,
                message=message,
                component_ref=component_ref,
                source_path=source_path,
            )
        )

    def has_errors(self) -> bool:
        """Check if any error-level diagnostics exist."""
        return any(d.severity == Severity.ERROR for d in self.diagnostics)
