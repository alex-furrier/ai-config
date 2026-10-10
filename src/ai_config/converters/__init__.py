"""Plugin conversion module for ai-config.

This module provides functionality to convert Claude Code plugins
to equivalent artifacts for other AI coding tools (Codex, Cursor, OpenCode, Pi).
"""

from ai_config.converters.claude_parser import parse_claude_plugin
from ai_config.converters.convert import (
    convert_plugin,
    convert_plugin_simple,
    preview_conversion,
)
from ai_config.converters.emitters import (
    CodexEmitter,
    CursorEmitter,
    EmitResult,
    OpenCodeEmitter,
    PiEmitter,
    PrimeEmitter,
    get_emitter,
)
from ai_config.converters.ir import (
    Component,
    ComponentKind,
    Diagnostic,
    InstallScope,
    MappingStatus,
    PluginIdentity,
    PluginIR,
    Severity,
    TargetTool,
)
from ai_config.converters.report import ConversionReport

__all__ = [
    # High-level API
    "convert_plugin",
    "convert_plugin_simple",
    "preview_conversion",
    # Parser
    "parse_claude_plugin",
    # Emitters
    "get_emitter",
    "CodexEmitter",
    "CursorEmitter",
    "OpenCodeEmitter",
    "PiEmitter",
    "PrimeEmitter",
    "EmitResult",
    # Report
    "ConversionReport",
    # IR types
    "Component",
    "ComponentKind",
    "Diagnostic",
    "InstallScope",
    "MappingStatus",
    "PluginIdentity",
    "PluginIR",
    "Severity",
    "TargetTool",
]
