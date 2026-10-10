"""Emitters for converting IR to target tool formats.

Each emitter takes a PluginIR and produces files for a specific tool.
Emitters follow the Protocol pattern (structural typing) - any class with
the right shape satisfies the Emitter interface.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ai_config.converters.claude_parser import (
    CODEX_SUPPORTED_HOOK_EVENTS,
    normalize_portable_name,
)
from ai_config.converters.codex_package import codex_package_spec
from ai_config.converters.ir import (
    Command,
    Diagnostic,
    Hook,
    InstallScope,
    LspServer,
    MappingStatus,
    McpServer,
    McpTransport,
    PluginIR,
    Severity,
    Skill,
    TargetTool,
    TextFile,
)
from ai_config.converters.report import IncludeResult
from ai_config.converters.skill_projection import project_skill
from ai_config.output_safety import validated_output_path
from ai_config.path_policy import GENERATED_PYTHON_ENVIRONMENT_DIRECTORY_NAMES
from ai_config.source_safety import ContainedSource, SourceMissingError, SourceSafetyError
from ai_config.validators.target.skill_invariants import generated_skill_bytes_invariant_errors

_ENV_VAR_PATTERN = re.compile(
    r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}|\{env:([A-Za-z_][A-Za-z0-9_]*)\}|\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
)


def _is_safe_relative_path(path: Path) -> bool:
    """Return True when path is safe to emit relative to an output root."""
    return not path.is_absolute() and ".." not in path.parts


def _paths_conflict(left: Path, right: Path) -> bool:
    """Return True when two relative output paths cannot coexist on disk."""
    return left == right or left in right.parents or right in left.parents


@dataclass
class EmittedFile:
    """A file to be written by the emitter."""

    path: Path  # Relative path from output root
    content: str | bytes
    binary: bool = False
    executable: bool = False


@dataclass
class ComponentMapping:
    """Record of how a component was mapped."""

    component_kind: str
    component_name: str
    status: MappingStatus
    target_path: Path | None = None
    notes: str | None = None
    lost_features: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _EmittedMarkdownRewriteEvidence:
    """Internal converter-rewrite evidence after applying a target output root."""

    include_target_path: Path
    markdown_target_path: Path
    direct_rewrite_count: int


@dataclass
class EmitResult:
    """Result of emitting a plugin to a target format."""

    target: TargetTool
    prime_source: str | None = None
    files: list[EmittedFile] = field(default_factory=list)
    mappings: list[ComponentMapping] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    cleanup_paths: list[Path] = field(default_factory=list)
    include_evidence: list[IncludeResult] = field(default_factory=list)
    _markdown_rewrite_evidence: list[_EmittedMarkdownRewriteEvidence] = field(
        default_factory=list, init=False, repr=False, compare=False
    )

    def add_file(self, path: Path | str, content: str, executable: bool = False) -> None:
        """Add a file to emit."""
        self.files.append(EmittedFile(path=Path(path), content=content, executable=executable))

    def add_binary_file(self, path: Path | str, content: bytes, executable: bool = False) -> None:
        """Add a binary file to emit."""
        self.files.append(
            EmittedFile(path=Path(path), content=content, binary=True, executable=executable)
        )

    def add_cleanup_path(self, path: Path | str) -> None:
        """Remove a legacy emitted path before writing new output."""
        cleanup_path = Path(path)
        if cleanup_path.is_absolute() or ".." in cleanup_path.parts:
            self.add_diagnostic(
                Severity.WARN,
                f"Ignoring unsafe cleanup path: {cleanup_path}",
            )
            return
        if cleanup_path not in self.cleanup_paths:
            self.cleanup_paths.append(cleanup_path)

    def add_mapping(
        self,
        kind: str,
        name: str,
        status: MappingStatus,
        target_path: Path | None = None,
        notes: str | None = None,
        lost_features: list[str] | None = None,
    ) -> None:
        """Record a component mapping."""
        self.mappings.append(
            ComponentMapping(
                component_kind=kind,
                component_name=name,
                status=status,
                target_path=target_path,
                notes=notes,
                lost_features=lost_features or [],
            )
        )

    def add_diagnostic(
        self,
        severity: Severity,
        message: str,
        component_ref: str | None = None,
    ) -> None:
        """Add a diagnostic message."""
        self.diagnostics.append(
            Diagnostic(
                severity=severity,
                message=message,
                component_ref=component_ref,
            )
        )

    def write_to(self, output_dir: Path, dry_run: bool = False) -> list[Path]:
        """Write all files to the output directory.

        Args:
            output_dir: Directory to write files to
            dry_run: If True, don't actually write files

        Returns list of file paths that were/would be written.
        """
        if self.target == TargetTool.PRIME:
            from ai_config.prime_ownership import write_prime_files

            return write_prime_files(
                output_dir, self.files, source=self.prime_source, dry_run=dry_run
            )

        cleanup_targets = [
            validated_output_path(output_dir, cleanup_path) for cleanup_path in self.cleanup_paths
        ]
        file_targets = [validated_output_path(output_dir, emitted.path) for emitted in self.files]
        written: list[Path] = []
        if not dry_run:
            for cleanup_path, full_cleanup_path in zip(
                self.cleanup_paths, cleanup_targets, strict=True
            ):
                full_cleanup_path = validated_output_path(output_dir, cleanup_path)
                if full_cleanup_path.is_dir():
                    shutil.rmtree(full_cleanup_path)
                elif full_cleanup_path.exists():
                    full_cleanup_path.unlink()

        for emitted, full_path in zip(self.files, file_targets, strict=True):
            if not dry_run:
                full_path = validated_output_path(output_dir, emitted.path)
                full_path.parent.mkdir(parents=True, exist_ok=True)
                if emitted.binary:
                    full_path.write_bytes(emitted.content)
                else:
                    full_path.write_text(emitted.content)
                if emitted.executable:
                    full_path.chmod(full_path.stat().st_mode | 0o111)
            written.append(full_path)
        return written

    def _recompute_include_rewrite_counts(self) -> None:
        """Refresh public additive totals from final converter-authored Markdown evidence."""
        totals: dict[Path, int] = {}
        for evidence in self._markdown_rewrite_evidence:
            totals[evidence.include_target_path] = (
                totals.get(evidence.include_target_path, 0) + evidence.direct_rewrite_count
            )
        self.include_evidence = [
            replace(
                evidence,
                direct_rewrite_count=totals.get(evidence.target_path, 0),
            )
            for evidence in self.include_evidence
        ]

    def apply_target_native_files(
        self,
        *,
        plugin_root: Path | None,
        target: TargetTool,
        base_dir: Path,
    ) -> None:
        """Copy target-native plugin files into the emitted output.

        Plugins can provide files under ``targets/<target>/`` when a target needs
        hand-written config that should be copied verbatim instead of generated.
        Native files are rooted at the target's natural config directory. If a
        native file conflicts with generated output, the native file wins.
        """
        if plugin_root is None:
            return

        target_relative = Path("targets") / target.value
        original_files = list(self.files)
        original_evidence = list(self.include_evidence)
        original_rewrite_evidence = list(self._markdown_rewrite_evidence)
        original_mapping_count = len(self.mappings)
        try:
            with ContainedSource(
                plugin_root,
                excluded_directory_names=GENERATED_PYTHON_ENVIRONMENT_DIRECTORY_NAMES,
            ) as source_root:
                target_source = source_root.relative(
                    target_relative.as_posix(), context="target-native"
                )
                source_root.kind(target_source, context="target-native")
                source_paths = list(
                    source_root.walk_files(
                        target_source,
                        context="target-native",
                        excluded_directory_names=GENERATED_PYTHON_ENVIRONMENT_DIRECTORY_NAMES,
                    )
                )
                sources = []
                for source_relative in source_paths:
                    try:
                        sources.append(
                            source_root.read_file(source_relative, context="target-native")
                        )
                    except SourceSafetyError as error:
                        self.add_diagnostic(
                            Severity.ERROR,
                            str(error),
                            component_ref=f"file:{source_relative.as_posix()}",
                        )
        except SourceMissingError:
            return
        except SourceSafetyError as error:
            message = str(error)
            if "symlink" in message:
                message = f"Ignoring symlinked target-native directory: {message}"
            self.add_diagnostic(
                Severity.ERROR,
                message,
                component_ref=f"file:targets/{target.value}",
            )
            return

        for source in sources:
            source_relative = source.relative_path
            relpath = Path(*source_relative.parts[len(target_relative.parts) :])
            target_path = base_dir / relpath
            if not _is_safe_relative_path(target_path):
                self.add_diagnostic(
                    Severity.ERROR,
                    f"Unsafe target-native output path: {target_path.as_posix()}",
                    component_ref=f"file:{source_relative.as_posix()}",
                )
                continue
            content_bytes = source.content
            executable = source.executable

            conflicts = [
                (index, emitted)
                for index, emitted in enumerate(self.files)
                if _paths_conflict(emitted.path, target_path)
            ]
            replaced = bool(conflicts)
            exact_conflict = next(
                (index for index, emitted in conflicts if emitted.path == target_path), None
            )
            if conflicts and (exact_conflict is None or len(conflicts) != 1):
                self.add_diagnostic(
                    Severity.ERROR,
                    "Target-native path conflicts with a generated directory; only exact file "
                    f"overrides are allowed: {target_path.as_posix()}",
                    component_ref=f"file:{source_relative.as_posix()}",
                )
                continue
            try:
                native_file = EmittedFile(
                    path=target_path,
                    content=content_bytes.decode("utf-8"),
                    executable=executable,
                )
            except UnicodeDecodeError:
                native_file = EmittedFile(
                    path=target_path,
                    content=content_bytes,
                    binary=True,
                    executable=executable,
                )
            if exact_conflict is not None:
                self.files[exact_conflict] = native_file
                self.include_evidence = [
                    evidence
                    for evidence in self.include_evidence
                    if evidence.target_path != target_path
                ]
                self._markdown_rewrite_evidence = [
                    evidence
                    for evidence in self._markdown_rewrite_evidence
                    if evidence.include_target_path != target_path
                    and evidence.markdown_target_path != target_path
                ]
                self._recompute_include_rewrite_counts()
            else:
                self.files.append(native_file)

            if replaced:
                self.add_diagnostic(
                    Severity.INFO,
                    f"Target-native file overrides generated output: {target_path.as_posix()}",
                    component_ref=f"file:{source_relative.as_posix()}",
                )
            self.add_mapping(
                "file",
                source_relative.as_posix(),
                MappingStatus.NATIVE,
                target_path=target_path,
                notes="Target-native file copied verbatim into converted output"
                + (" and overrides generated output" if replaced else ""),
            )

        invalid_skill_dirs = self._validate_final_generated_skills()
        if invalid_skill_dirs:
            self._restore_generated_skills_after_unsafe_override(
                invalid_skill_dirs,
                original_files=original_files,
                original_evidence=original_evidence,
                original_rewrite_evidence=original_rewrite_evidence,
                original_mapping_count=original_mapping_count,
            )
            # The restored generated projection must still satisfy both the byte
            # invariants and its include-evidence correspondence.
            self._validate_final_generated_skills()

    def _restore_generated_skills_after_unsafe_override(
        self,
        invalid_skill_dirs: set[Path],
        *,
        original_files: list[EmittedFile],
        original_evidence: list[IncludeResult],
        original_rewrite_evidence: list[_EmittedMarkdownRewriteEvidence],
        original_mapping_count: int,
    ) -> None:
        """Roll back every target-native change below an invalid generated skill."""

        def belongs_to_invalid_skill(path: Path) -> bool:
            return any(
                path == skill_dir or skill_dir in path.parents for skill_dir in invalid_skill_dirs
            )

        self.files = [
            emitted for emitted in self.files if not belongs_to_invalid_skill(emitted.path)
        ] + [emitted for emitted in original_files if belongs_to_invalid_skill(emitted.path)]
        self.include_evidence = [
            evidence
            for evidence in self.include_evidence
            if not belongs_to_invalid_skill(evidence.target_path)
        ] + [
            evidence
            for evidence in original_evidence
            if belongs_to_invalid_skill(evidence.target_path)
        ]
        self._markdown_rewrite_evidence = [
            evidence
            for evidence in self._markdown_rewrite_evidence
            if not belongs_to_invalid_skill(evidence.include_target_path)
        ] + [
            evidence
            for evidence in original_rewrite_evidence
            if belongs_to_invalid_skill(evidence.include_target_path)
        ]
        self._recompute_include_rewrite_counts()
        self.mappings = self.mappings[:original_mapping_count] + [
            mapping
            for mapping in self.mappings[original_mapping_count:]
            if mapping.target_path is None or not belongs_to_invalid_skill(mapping.target_path)
        ]

    def _validate_final_generated_skills(self) -> set[Path]:
        """Recheck generated skill invariants and evidence after native precedence is final."""
        skill_paths = {
            mapping.target_path
            for mapping in self.mappings
            if mapping.target_path is not None
            and mapping.target_path.name == "SKILL.md"
            and mapping.component_kind in {"skill", "command"}
        }
        invalid_skill_dirs: set[Path] = set()
        emitted_paths = {emitted.path for emitted in self.files}
        for skill_path in sorted(skill_paths, key=lambda path: path.as_posix()):
            skill_dir = skill_path.parent
            files: dict[PurePosixPath, bytes] = {}
            for emitted in self.files:
                if emitted.path != skill_path and skill_dir not in emitted.path.parents:
                    continue
                relative = PurePosixPath(emitted.path.relative_to(skill_dir).as_posix())
                files[relative] = (
                    emitted.content.encode("utf-8")
                    if isinstance(emitted.content, str)
                    else emitted.content
                )
            skill_bytes = files.get(PurePosixPath("SKILL.md"))
            metadata: dict[str, Any] = {}
            if skill_bytes is not None:
                try:
                    text = skill_bytes.decode("utf-8")
                    parts = text.split("---", 2)
                    parsed = yaml.safe_load(parts[1]) if len(parts) == 3 else None
                    if isinstance(parsed, dict):
                        metadata = parsed
                except (UnicodeDecodeError, yaml.YAMLError):
                    pass
            errors = generated_skill_bytes_invariant_errors(files, metadata)
            for evidence in self.include_evidence:
                if skill_dir not in evidence.target_path.parents:
                    continue
                if evidence.target_path not in emitted_paths:
                    errors.append(
                        "Generated skill include evidence references a missing copy: "
                        f"{evidence.target_path.relative_to(skill_dir).as_posix()}"
                    )
            for error in errors:
                invalid_skill_dirs.add(skill_dir)
                self.add_diagnostic(
                    Severity.ERROR,
                    error,
                    component_ref=f"file:{skill_path.as_posix()}",
                )
        return invalid_skill_dirs

    def preview(self, output_dir: Path | None = None) -> str:
        """Generate preview of what would be written.

        Args:
            output_dir: Optional base directory for path display

        Returns formatted string showing files and sizes.
        """
        lines = [f"Files to write ({len(self.files)} total):"]
        lines.append("")

        total_bytes = 0
        for f in self.files:
            if f.binary:
                size = len(f.content)
            else:
                size = len(f.content.encode("utf-8"))  # type: ignore[arg-type]
            total_bytes += size

            if output_dir:
                display_path = output_dir / f.path
            else:
                display_path = f.path

            action = "[CREATE]"
            if output_dir and (output_dir / f.path).exists():
                action = "[UPDATE]"

            exec_flag = " (exec)" if f.executable else ""
            bin_flag = " (bin)" if f.binary else ""
            lines.append(f"  {action} {display_path}{exec_flag}{bin_flag}")
            lines.append(f"         {size:,} bytes")

        lines.append("")
        lines.append(f"Total: {total_bytes:,} bytes")

        # Add mapping summary
        if self.mappings:
            lines.append("")
            lines.append("Component mappings:")
            for m in self.mappings:
                status_icon = {
                    MappingStatus.NATIVE: "✓",
                    MappingStatus.TRANSFORM: "~",
                    MappingStatus.FALLBACK: "↓",
                    MappingStatus.EMULATE: "≈",
                    MappingStatus.UNSUPPORTED: "✗",
                }.get(m.status, "?")
                lines.append(
                    f"  {status_icon} {m.component_kind}:{m.component_name} → {m.status.value}"
                )

        # Add diagnostics
        errors = [d for d in self.diagnostics if d.severity == Severity.ERROR]
        warnings = [d for d in self.diagnostics if d.severity == Severity.WARN]

        if errors:
            lines.append("")
            lines.append(f"Errors ({len(errors)}):")
            for e in errors:
                lines.append(f"  ✗ {e.message}")

        if warnings:
            lines.append("")
            lines.append(f"Warnings ({len(warnings)}):")
            for w in warnings:
                lines.append(f"  ⚠ {w.message}")

        return "\n".join(lines)

    def has_errors(self) -> bool:
        """Check if any error-level diagnostics exist."""
        return any(d.severity == Severity.ERROR for d in self.diagnostics)


# Module-level helper function (extracted from BaseEmitter for Protocol pattern)
def skill_to_markdown(
    skill: Skill, strip_claude_fields: bool = True, *, name_override: str | None = None
) -> str:
    """Convert a skill to SKILL.md format.

    Args:
        skill: The skill to convert.
        strip_claude_fields: If True, remove Claude-specific fields like
            allowed-tools, model, context, agent, etc.
        name_override: Optional emitted Agent Skills name. Use when target tools
            require frontmatter name to match a namespaced output directory.

    Returns:
        Markdown string with YAML frontmatter.
    """
    # Build frontmatter
    meta: dict[str, Any] = {
        "name": name_override or skill.name,
    }
    if skill.description:
        meta["description"] = skill.description

    # Include portable fields only when not stripping
    if not strip_claude_fields:
        if skill.allowed_tools:
            meta["allowed-tools"] = skill.allowed_tools
        if skill.model:
            meta["model"] = skill.model
        if skill.context:
            meta["context"] = skill.context
        if skill.agent:
            meta["agent"] = skill.agent
        if not skill.user_invocable:
            meta["user-invocable"] = False
        if skill.disable_model_invocation:
            meta["disable-model-invocation"] = True

    # Find SKILL.md content
    body = ""
    for f in skill.files:
        if f.relpath == "SKILL.md" and isinstance(f, TextFile):
            # Extract body from content (TextFile only)
            file_content = f.content
            if file_content.startswith("---"):
                parts = file_content.split("---", 2)
                if len(parts) >= 3:
                    body = parts[2].strip()
            else:
                body = file_content
            break

    # Build markdown
    frontmatter = yaml.dump(meta, default_flow_style=False, sort_keys=False)
    return f"---\n{frontmatter}---\n\n{body}"


def _emit_projected_skill(
    result: EmitResult,
    skill: Skill,
    skill_dir: Path,
    generated_markdown: str,
) -> bool:
    """Emit one target-neutral self-contained skill projection."""
    projection = project_skill(skill, generated_markdown)
    if projection.errors:
        for error in projection.errors:
            result.add_diagnostic(Severity.ERROR, error, component_ref=f"skill:{skill.name}")
        return False
    for item in projection.files:
        target = skill_dir / Path(item.relative_path.as_posix())
        if isinstance(item.content, bytes):
            result.add_binary_file(target, item.content, item.executable)
        else:
            result.add_file(target, item.content, item.executable)
    for evidence in projection.include_evidence:
        include_target = skill_dir / evidence.projected_path
        result.include_evidence.append(
            IncludeResult(
                source_relative_path=evidence.source_relative_path,
                consumer_skill=skill.name,
                target_path=include_target,
                copy_count=1,
                duplicated_bytes=evidence.duplicated_bytes,
                direct_rewrite_count=evidence.direct_rewrite_count,
            )
        )
        result._markdown_rewrite_evidence.extend(
            _EmittedMarkdownRewriteEvidence(
                include_target_path=include_target,
                markdown_target_path=skill_dir / item.markdown_relative_path.as_posix(),
                direct_rewrite_count=item.direct_rewrite_count,
            )
            for item in evidence.markdown_rewrites
        )
    return True


def _transform_env_value(value: str, target: TargetTool) -> str:
    """Transform env var syntax to target format."""

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1) or match.group(2) or match.group(3)
        if target == TargetTool.CURSOR:
            return f"${{env:{var_name}}}"
        if target == TargetTool.OPENCODE:
            return f"{{env:{var_name}}}"
        return f"${{{var_name}}}"

    return _ENV_VAR_PATTERN.sub(replacer, value)


def _resolve_claude_plugin_root(command: str, plugin_root: Path | None) -> str:
    """Resolve Claude hook commands that reference ${CLAUDE_PLUGIN_ROOT}."""
    if "${CLAUDE_PLUGIN_ROOT}" not in command:
        return command
    if plugin_root is None:
        return command
    return command.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))


class CodexEmitter:
    """Emit one self-contained package and local marketplace for Codex."""

    target = TargetTool.CODEX

    def __init__(self, scope: InstallScope = InstallScope.PROJECT) -> None:
        self.scope = scope

    def emit(self, ir: PluginIR) -> EmitResult:
        """Emit IR using Codex's installable plugin package contract."""
        result = EmitResult(target=self.target)
        try:
            spec = codex_package_spec(ir.identity.plugin_id, ir.identity.version, Path("."))
        except ValueError as error:
            result.add_diagnostic(
                Severity.ERROR,
                str(error),
                component_ref=f"package:{ir.identity.plugin_id}",
            )
            return result
        package_root = spec.package_relative_path
        command_skill_names = self._command_skill_names(result, ir)
        if command_skill_names is None:
            return result

        result.add_cleanup_path(spec.marketplace_relative_path)
        manifest: dict[str, object] = {
            "name": spec.plugin_name,
            "version": spec.version,
            "description": ir.identity.description
            or f"Converted Codex package for {ir.identity.name}",
        }

        if ir.skills() or ir.commands():
            manifest["skills"] = "./skills/"
        for skill in ir.skills():
            self._emit_skill(result, skill, package_root)
        for command, skill_name in zip(ir.commands(), command_skill_names, strict=True):
            self._emit_command(result, command, skill_name, package_root)

        hooks_data = self._build_hooks(result, ir.hooks(), package_root, ir.source_path)
        if hooks_data:
            hooks_path = package_root / "hooks" / "hooks.json"
            result.add_file(hooks_path, json.dumps({"hooks": hooks_data}, indent=2) + "\n")
            manifest["hooks"] = "./hooks/hooks.json"

        mcp_servers = self._build_mcp_servers(
            result, ir.mcp_servers(), package_root, ir.source_path
        )
        if mcp_servers:
            manifest["mcpServers"] = mcp_servers

        for agent in ir.agents():
            result.add_mapping(
                "agent",
                agent.name,
                MappingStatus.UNSUPPORTED,
                notes="Codex plugin packages do not support Claude agent definitions",
            )
        for lsp in ir.lsp_servers():
            result.add_mapping(
                "lsp",
                lsp.name,
                MappingStatus.UNSUPPORTED,
                notes="Codex plugin packages do not support custom LSP servers",
            )

        manifest_path = package_root / ".codex-plugin" / "plugin.json"
        result.add_file(manifest_path, json.dumps(manifest, indent=2) + "\n")
        result.add_mapping(
            "package",
            ir.identity.plugin_id,
            MappingStatus.NATIVE,
            target_path=manifest_path,
            notes="Generated installable Codex plugin package",
        )
        marketplace = {
            "name": spec.marketplace_name,
            "interface": {"displayName": f"ai-config: {ir.identity.name}"},
            "plugins": [
                {
                    "name": spec.plugin_name,
                    "source": {
                        "source": "local",
                        "path": f"./plugins/{spec.plugin_name}",
                    },
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Developer Tools",
                }
            ],
        }
        marketplace_path = (
            spec.marketplace_relative_path / ".agents" / "plugins" / "marketplace.json"
        )
        result.add_file(marketplace_path, json.dumps(marketplace, indent=2) + "\n")
        result.add_mapping(
            "marketplace",
            spec.marketplace_name,
            MappingStatus.NATIVE,
            target_path=marketplace_path,
            notes=f"Install as {spec.plugin_id} through the Codex plugin CLI",
        )
        result.apply_target_native_files(
            plugin_root=ir.source_path,
            target=self.target,
            base_dir=package_root,
        )
        result.add_diagnostic(
            Severity.INFO,
            f"Generated Codex package {spec.plugin_id}; sync registers and installs it through `codex plugin`.",
            component_ref=f"package:{ir.identity.plugin_id}",
        )
        result.add_diagnostic(
            Severity.WARN,
            "Legacy loose Codex output is preserved because ownership cannot be proven; "
            "`doctor --target codex` reports stale .codex skills, prompts, hooks, and MCP config.",
            component_ref="codex:legacy-output",
        )
        return result

    def _command_skill_names(self, result: EmitResult, ir: PluginIR) -> list[str] | None:
        """Validate the combined package skill namespace before creating any output."""
        occupied: dict[str, tuple[str, str]] = {}
        for skill in ir.skills():
            existing = occupied.get(skill.name)
            if existing is not None:
                result.add_diagnostic(
                    Severity.ERROR,
                    f"Codex package skill namespace collision for '{skill.name}': multiple "
                    "source skills normalize to the same identity. Rename one source component; "
                    "no package output was emitted.",
                    component_ref=f"skill:{skill.name}",
                )
                return None
            occupied[skill.name] = ("skill", skill.name)

        command_skill_names: list[str] = []
        for command in ir.commands():
            normalized = normalize_portable_name(command.name, "command", max_len=56)
            skill_name = f"command-{normalized}"
            existing = occupied.get(skill_name)
            if existing is not None:
                existing_kind, existing_name = existing
                result.add_diagnostic(
                    Severity.ERROR,
                    "Codex package skill namespace collision for "
                    f"'{skill_name}': {existing_kind} '{existing_name}' conflicts with "
                    f"command '{command.name}'. Rename one source component; no package output "
                    "was emitted.",
                    component_ref=f"command:{command.name}",
                )
                return None
            occupied[skill_name] = ("command", command.name)
            command_skill_names.append(skill_name)
        return command_skill_names

    def _emit_skill(self, result: EmitResult, skill: Skill, package_root: Path) -> None:
        skill_dir = package_root / "skills" / skill.name
        skill_path = skill_dir / "SKILL.md"
        if not _emit_projected_skill(
            result,
            skill,
            skill_dir,
            skill_to_markdown(skill, strip_claude_fields=True),
        ):
            return
        lost = (
            ["Claude-only skill execution metadata"]
            if skill.allowed_tools or skill.model or skill.context or skill.agent
            else []
        )
        transformed = bool(lost or skill.includes)
        result.add_mapping(
            "skill",
            skill.name,
            MappingStatus.TRANSFORM if transformed else MappingStatus.NATIVE,
            target_path=skill_path,
            notes=(
                "Package-local self-contained skill with shared resources materialized"
                if skill.includes
                else "Package-local Agent Skill"
                if not lost
                else "Package-local skill with Claude-only fields removed"
            ),
            lost_features=lost,
        )

    def _emit_command(
        self,
        result: EmitResult,
        command: Command,
        skill_name: str,
        package_root: Path,
    ) -> None:
        skill_path = package_root / "skills" / skill_name / "SKILL.md"
        description = command.description or f"Converted Claude command: {command.name}"
        if command.argument_hint:
            description = f"{description} Arguments: {command.argument_hint}"
        frontmatter = yaml.dump({"name": skill_name, "description": description}, sort_keys=False)
        result.add_file(skill_path, f"---\n{frontmatter}---\n\n{command.markdown}\n")
        degraded = command.has_arguments_var or command.has_positional_vars
        result.add_mapping(
            "command",
            command.name,
            MappingStatus.FALLBACK if degraded else MappingStatus.TRANSFORM,
            target_path=skill_path,
            notes=(
                "Converted to a package skill; Claude argument variables remain literal instructions"
                if degraded
                else "Converted to a package-local Agent Skill"
            ),
            lost_features=["Claude slash-command argument substitution"] if degraded else [],
        )
        if degraded:
            result.add_diagnostic(
                Severity.WARN,
                f"Command '{command.name}' uses Claude argument variables; "
                "the Codex package skill keeps them as literal instructions.",
                component_ref=f"command:{command.name}",
            )

    def _build_hooks(
        self,
        result: EmitResult,
        hooks: list[Hook],
        package_root: Path,
        source_root: Path | None,
    ) -> dict[str, list[dict[str, object]]]:
        converted: dict[str, list[dict[str, object]]] = {}
        for hook in hooks:
            for event in hook.events:
                if event.name not in CODEX_SUPPORTED_HOOK_EVENTS:
                    result.add_mapping(
                        "hook",
                        event.name,
                        MappingStatus.UNSUPPORTED,
                        notes="No documented Codex package hook equivalent",
                    )
                    continue
                handlers: list[dict[str, object]] = []
                lost: list[str] = []
                for handler in event.handlers:
                    if handler.type.value != "command" or not handler.command:
                        lost.append(f"{handler.type.value} handler")
                        continue
                    if not self._copy_referenced_support_files(
                        result, package_root, source_root, handler.command
                    ):
                        lost.append("missing package support file")
                        continue
                    command = handler.command.replace("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}")
                    converted_handler: dict[str, object] = {
                        "type": "command",
                        "command": command,
                    }
                    if handler.timeout_sec is not None:
                        converted_handler["timeout"] = handler.timeout_sec
                    if handler.is_async:
                        lost.append("async execution")
                    handlers.append(converted_handler)
                if not handlers:
                    result.add_mapping(
                        "hook",
                        event.name,
                        MappingStatus.UNSUPPORTED,
                        notes="No Codex-compatible command handlers",
                        lost_features=lost,
                    )
                    continue
                group: dict[str, object] = {"hooks": handlers}
                if event.matcher and event.name in {
                    "PreToolUse",
                    "PermissionRequest",
                    "PostToolUse",
                }:
                    group["matcher"] = event.matcher
                elif event.matcher:
                    lost.append("matcher")
                converted.setdefault(event.name, []).append(group)
                result.add_mapping(
                    "hook",
                    event.name,
                    MappingStatus.TRANSFORM if not lost else MappingStatus.FALLBACK,
                    target_path=package_root / "hooks" / "hooks.json",
                    notes="Converted to package-native Codex hooks",
                    lost_features=lost,
                )
        return converted

    def _copy_referenced_support_files(
        self,
        result: EmitResult,
        package_root: Path,
        source_root: Path | None,
        command: str,
    ) -> bool:
        """Copy files referenced through CLAUDE_PLUGIN_ROOT into the package."""
        references = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\s'\";|&]+)", command)
        if not references:
            return True
        if source_root is None:
            return False
        try:
            with ContainedSource(
                source_root,
                excluded_directory_names=GENERATED_PYTHON_ENVIRONMENT_DIRECTORY_NAMES,
            ) as contained:
                return self._copy_referenced_support_files_from_source(
                    result, package_root, contained, references
                )
        except SourceSafetyError as error:
            result.add_diagnostic(Severity.ERROR, str(error), component_ref="hook:source-root")
            return False

    def _copy_referenced_support_files_from_source(
        self,
        result: EmitResult,
        package_root: Path,
        contained: ContainedSource,
        references: list[str],
    ) -> bool:
        """Copy referenced support files while the source descriptor is open."""
        valid = True
        for reference in references:
            try:
                relative = contained.relative(reference, context="Codex support file")
                source = contained.read_file(relative, context="Codex support file")
            except SourceMissingError as error:
                result.add_diagnostic(
                    Severity.WARN,
                    str(error),
                    component_ref=f"hook:file:{reference}",
                )
                valid = False
                continue
            except SourceSafetyError as error:
                result.add_diagnostic(
                    Severity.ERROR,
                    str(error),
                    component_ref=f"hook:file:{reference}",
                )
                valid = False
                continue
            target = package_root / Path(relative.as_posix())
            if any(file.path == target for file in result.files):
                continue
            try:
                result.add_file(target, source.content.decode("utf-8"), source.executable)
            except UnicodeDecodeError:
                result.add_binary_file(target, source.content, source.executable)
        return valid

    def _build_mcp_servers(
        self,
        result: EmitResult,
        servers: list[McpServer],
        package_root: Path,
        source_root: Path | None,
    ) -> dict[str, dict[str, object]]:
        converted: dict[str, dict[str, object]] = {}
        for server in servers:
            referenced_values = [
                value
                for value in [server.command, server.cwd, *server.args, *server.env.values()]
                if value
            ]
            support_results = [
                self._copy_referenced_support_files(
                    result, package_root, source_root, referenced_value
                )
                for referenced_value in referenced_values
            ]
            if not all(support_results):
                result.add_mapping(
                    "mcp_server",
                    server.name,
                    MappingStatus.UNSUPPORTED,
                    notes="Referenced package support file is missing or unsafe",
                )
                continue
            config: dict[str, object] = {}
            if server.command:
                config["command"] = server.command.replace(
                    "${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}"
                )
            if server.args:
                config["args"] = [
                    value.replace("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}")
                    for value in server.args
                ]
            if server.url:
                config["url"] = server.url
            if server.env:
                config["env"] = {
                    key: _transform_env_value(value, TargetTool.CODEX)
                    for key, value in server.env.items()
                }
            if server.cwd:
                config["cwd"] = server.cwd.replace("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}")
            converted[server.name] = config
            result.add_mapping(
                "mcp_server",
                server.name,
                MappingStatus.TRANSFORM,
                target_path=package_root / ".codex-plugin" / "plugin.json",
                notes="Converted to package manifest mcpServers entry",
            )
        return converted


class CursorEmitter:
    """Emit plugins in Cursor format.

    Satisfies the Emitter protocol with target, scope, and emit() method.
    """

    target = TargetTool.CURSOR

    def __init__(self, scope: InstallScope = InstallScope.PROJECT) -> None:
        self.scope = scope

    def emit(self, ir: PluginIR) -> EmitResult:
        """Emit IR to Cursor format."""
        result = EmitResult(target=self.target)
        plugin_id = ir.identity.plugin_id

        # Emit skills
        for skill in ir.skills():
            self._emit_skill(result, skill, plugin_id)

        # Emit commands (plain markdown, no variables)
        for cmd in ir.commands():
            self._emit_command(result, cmd, plugin_id)

        # Emit hooks (Cursor supports them!)
        hooks = ir.hooks()
        if hooks:
            self._emit_hooks(result, hooks, plugin_id)

        # Emit MCP servers
        mcp_servers = ir.mcp_servers()
        if mcp_servers:
            self._emit_mcp_config(result, mcp_servers, plugin_id)

        # Agents not supported
        for agent in ir.agents():
            result.add_mapping(
                "agent",
                agent.name,
                MappingStatus.UNSUPPORTED,
                notes="Cursor does not support custom agent definitions",
            )

        # LSP not supported (Cursor handles LSP internally)
        for lsp in ir.lsp_servers():
            result.add_mapping(
                "lsp",
                lsp.name,
                MappingStatus.UNSUPPORTED,
                notes="Cursor handles LSP internally",
            )

        result.apply_target_native_files(
            plugin_root=ir.source_path,
            target=self.target,
            base_dir=Path(".cursor"),
        )

        return result

    def _emit_skill(self, result: EmitResult, skill: Skill, plugin_id: str) -> None:
        """Emit a skill to Cursor format."""
        dir_name = f"{plugin_id}-{skill.name}"
        skill_dir = Path(".cursor") / "skills" / dir_name
        skill_path = skill_dir / "SKILL.md"
        content = skill_to_markdown(skill, strip_claude_fields=True, name_override=dir_name)
        if not _emit_projected_skill(result, skill, skill_dir, content):
            return
        result.add_mapping(
            "skill",
            skill.name,
            MappingStatus.TRANSFORM if skill.includes else MappingStatus.NATIVE,
            target_path=skill_path,
            notes="Shared resources materialized into a self-contained skill"
            if skill.includes
            else None,
        )

    def _emit_command(self, result: EmitResult, cmd: Command, plugin_id: str) -> None:
        """Emit a command to Cursor format."""
        # Cursor commands are plain markdown, no variables
        cmd_path = Path(".cursor") / "commands" / f"{plugin_id}-{cmd.name}.md"

        # Strip variable references since Cursor doesn't support them
        content = cmd.markdown
        if cmd.has_arguments_var or cmd.has_positional_vars:
            # Add a note about lost functionality
            result.add_diagnostic(
                Severity.WARN,
                f"Command '{cmd.name}' uses template variables which Cursor doesn't support",
                component_ref=f"command:{cmd.name}",
            )
            # Replace $ARGUMENTS with a placeholder note
            content = re.sub(
                r"\$ARGUMENTS|\$\{ARGUMENTS\}",
                "[user arguments will be appended]",
                content,
            )
            content = re.sub(
                r"\$[1-9]|\$\{[1-9]\}",
                "[positional arg]",
                content,
            )

        result.add_file(cmd_path, content)

        status = (
            MappingStatus.NATIVE
            if not (cmd.has_arguments_var or cmd.has_positional_vars)
            else MappingStatus.TRANSFORM
        )
        result.add_mapping(
            "command",
            cmd.name,
            status,
            target_path=cmd_path,
            notes="Cursor commands don't support variable substitution"
            if status == MappingStatus.TRANSFORM
            else None,
            lost_features=["Template variable substitution"]
            if status == MappingStatus.TRANSFORM
            else None,
        )

    def _emit_hooks(self, result: EmitResult, hooks: list[Hook], plugin_id: str) -> None:
        """Emit hooks configuration for Cursor."""
        # Map Claude events to Cursor events
        event_map = {
            "PreToolUse": ["beforeShellExecution", "beforeMCPExecution", "beforeReadFile"],
            "PostToolUse": ["afterShellExecution", "afterMCPExecution", "afterFileEdit"],
            "UserPromptSubmit": ["beforeSubmitPrompt"],
            "Stop": ["stop"],
        }

        cursor_hooks: dict[str, list[dict[str, Any]]] = {}

        for hook in hooks:
            for event in hook.events:
                cursor_events = event_map.get(event.name, [])
                if not cursor_events:
                    result.add_diagnostic(
                        Severity.WARN,
                        f"Hook event '{event.name}' has no Cursor equivalent",
                        component_ref=f"hook:{event.name}",
                    )
                    continue

                for cursor_event in cursor_events:
                    if cursor_event not in cursor_hooks:
                        cursor_hooks[cursor_event] = []

                    for handler in event.handlers:
                        if handler.type.value == "command" and handler.command:
                            cursor_hooks[cursor_event].append({"command": handler.command})
                        else:
                            result.add_diagnostic(
                                Severity.WARN,
                                f"Hook handler type '{handler.type}' not supported in Cursor",
                                component_ref=f"hook:{event.name}",
                            )

        if cursor_hooks:
            hooks_config = {"version": 1, "hooks": cursor_hooks}
            hooks_path = Path(".cursor") / "hooks.json"
            result.add_file(hooks_path, json.dumps(hooks_config, indent=2))
            result.add_mapping(
                "hook",
                "hooks",
                MappingStatus.TRANSFORM,
                target_path=hooks_path,
                notes="Event names mapped to Cursor equivalents",
            )

    def _emit_mcp_config(
        self, result: EmitResult, servers: list[McpServer], plugin_id: str
    ) -> None:
        """Emit MCP configuration for Cursor."""
        mcp_servers: dict[str, dict[str, Any]] = {}

        for server in servers:
            name = f"{plugin_id}-{server.name}"
            config: dict[str, Any] = {}

            if server.transport == McpTransport.STDIO:
                config["type"] = "stdio"
                if server.command:
                    config["command"] = server.command
                if server.args:
                    config["args"] = server.args
            else:
                # HTTP/SSE
                if server.url:
                    config["url"] = server.url

            if server.env:
                config["env"] = {
                    k: _transform_env_value(v, TargetTool.CURSOR) for k, v in server.env.items()
                }

            mcp_servers[name] = config

            result.add_mapping(
                "mcp_server",
                server.name,
                MappingStatus.TRANSFORM,
                notes="Converted to Cursor MCP format",
            )

        mcp_path = Path(".cursor") / "mcp.json"
        result.add_file(mcp_path, json.dumps({"mcpServers": mcp_servers}, indent=2))


class OpenCodeEmitter:
    """Emit plugins in OpenCode format.

    Satisfies the Emitter protocol with target, scope, and emit() method.
    """

    target = TargetTool.OPENCODE

    def __init__(self, scope: InstallScope = InstallScope.PROJECT) -> None:
        self.scope = scope

    def emit(self, ir: PluginIR) -> EmitResult:
        """Emit IR to OpenCode format."""
        result = EmitResult(target=self.target)
        plugin_id = ir.identity.plugin_id

        # Emit skills
        for skill in ir.skills():
            self._emit_skill(result, skill, plugin_id)

        # Emit commands
        for cmd in ir.commands():
            self._emit_command(result, cmd, plugin_id)

        # Hooks not natively supported
        for _hook in ir.hooks():
            result.add_mapping(
                "hook",
                "hooks",
                MappingStatus.EMULATE,
                notes="OpenCode doesn't have hooks - consider using plugins",
            )
            result.add_diagnostic(
                Severity.WARN,
                "Hooks are not natively supported in OpenCode",
                component_ref="hook:*",
            )

        # Emit MCP servers
        mcp_servers = ir.mcp_servers()
        if mcp_servers:
            self._emit_mcp_config(result, mcp_servers, plugin_id)

        # Emit LSP servers (OpenCode supports them!)
        lsp_servers = ir.lsp_servers()
        if lsp_servers:
            self._emit_lsp_config(result, lsp_servers, plugin_id)

        # Agents not supported
        for agent in ir.agents():
            result.add_mapping(
                "agent",
                agent.name,
                MappingStatus.UNSUPPORTED,
                notes="OpenCode does not support custom agent definitions",
            )

        result.apply_target_native_files(
            plugin_root=ir.source_path,
            target=self.target,
            base_dir=Path("."),
        )

        return result

    def _emit_skill(self, result: EmitResult, skill: Skill, plugin_id: str) -> None:
        """Emit a skill to OpenCode format."""
        dir_name = f"{plugin_id}-{skill.name}"
        skill_dir = Path(".opencode") / "skills" / dir_name
        skill_path = skill_dir / "SKILL.md"
        content = skill_to_markdown(skill, strip_claude_fields=True, name_override=dir_name)
        if not _emit_projected_skill(result, skill, skill_dir, content):
            return
        result.add_mapping(
            "skill",
            skill.name,
            MappingStatus.TRANSFORM if skill.includes else MappingStatus.NATIVE,
            target_path=skill_path,
            notes="Shared resources materialized into a self-contained skill"
            if skill.includes
            else None,
        )

    def _emit_command(self, result: EmitResult, cmd: Command, plugin_id: str) -> None:
        """Emit a command to OpenCode format.

        OpenCode commands support:
        - Markdown with YAML frontmatter (description, agent, model)
        - Placeholders using $NAME syntax (uppercase)
        - Located in .opencode/commands/ (project) or ~/.config/opencode/commands/ (user)
        """
        cmd_path = Path(".opencode") / "commands" / f"{plugin_id}-{cmd.name}.md"

        # Build frontmatter
        meta: dict[str, Any] = {}
        if cmd.description:
            meta["description"] = cmd.description

        # Transform Claude's $ARGUMENTS to OpenCode's $ARGS placeholder
        # and $1, $2 etc to $ARG1, $ARG2 (OpenCode uses uppercase)
        content = cmd.markdown
        if cmd.has_arguments_var:
            content = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", "$ARGS", content)
        if cmd.has_positional_vars:
            # Convert $1 to $ARG1, $2 to $ARG2, etc.
            content = re.sub(r"\$([1-9])|\$\{([1-9])\}", r"$ARG\1\2", content)

        if meta:
            frontmatter = yaml.dump(meta, default_flow_style=False)
            full_content = f"---\n{frontmatter}---\n\n{content}"
        else:
            full_content = content

        result.add_file(cmd_path, full_content)

        # Determine mapping status
        if cmd.has_arguments_var or cmd.has_positional_vars:
            status = MappingStatus.TRANSFORM
            notes = "Variables transformed: $ARGUMENTS→$ARGS, $N→$ARGN"
        else:
            status = MappingStatus.NATIVE
            notes = None

        result.add_mapping(
            "command",
            cmd.name,
            status,
            target_path=cmd_path,
            notes=notes,
        )

    def _emit_mcp_config(
        self, result: EmitResult, servers: list[McpServer], plugin_id: str
    ) -> None:
        """Emit MCP configuration for OpenCode."""
        mcp_config: dict[str, dict[str, Any]] = {}

        for server in servers:
            name = f"{plugin_id}-{server.name}"
            config: dict[str, Any] = {"enabled": True}

            if server.transport == McpTransport.STDIO:
                config["type"] = "local"
                cmd_parts = []
                if server.command:
                    cmd_parts.append(server.command)
                cmd_parts.extend(server.args)
                if cmd_parts:
                    config["command"] = cmd_parts
            else:
                config["type"] = "remote"
                if server.url:
                    config["url"] = server.url

            if server.env:
                config["environment"] = {
                    k: _transform_env_value(v, TargetTool.OPENCODE) for k, v in server.env.items()
                }
            if server.timeout_ms:
                config["timeout"] = server.timeout_ms

            mcp_config[name] = config

            result.add_mapping(
                "mcp_server",
                server.name,
                MappingStatus.TRANSFORM,
                notes="Converted to OpenCode MCP format",
            )

        # Emit to opencode.json at output root
        opencode_config = {"mcp": mcp_config}
        config_path = Path("opencode.json")
        result.add_file(config_path, json.dumps(opencode_config, indent=2))

        result.add_diagnostic(
            Severity.INFO,
            f"MCP config written to {config_path}",
            component_ref="mcp:*",
        )

    def _emit_lsp_config(
        self, result: EmitResult, lsp_servers: list[LspServer], plugin_id: str
    ) -> None:
        """Emit LSP configuration for OpenCode."""
        lsp_entries: dict[str, dict[str, Any]] = {}

        for lsp in lsp_servers:
            name = f"{plugin_id}-{lsp.name}"

            config: dict[str, Any] = {}
            if lsp.command:
                cmd_parts = [lsp.command] + lsp.args
                config["command"] = cmd_parts
            if lsp.extensions:
                config["extensions"] = lsp.extensions
            if lsp.env:
                config["env"] = lsp.env
            if lsp.initialization_options:
                config["initialization"] = lsp.initialization_options

            lsp_entries[name] = config

            result.add_mapping(
                "lsp",
                lsp.name,
                MappingStatus.TRANSFORM,
                target_path=Path("opencode.lsp.json"),
                notes="Converted to OpenCode LSP format",
            )

        config_path = Path("opencode.lsp.json")
        result.add_file(config_path, json.dumps({"lsp": lsp_entries}, indent=2))


class PiEmitter:
    """Emit plugins in Pi format.

    Pi implements the Agent Skills standard. Skills map natively.
    Commands map to Pi prompt templates. Hooks are emulated with TypeScript extensions.
    """

    target = TargetTool.PI

    def __init__(self, scope: InstallScope = InstallScope.PROJECT) -> None:
        self.scope = scope
        # Pi user-scope resources live under ~/.pi/agent/, project-scope under .pi/
        self._base_dir = Path(".pi") / "agent" if scope == InstallScope.USER else Path(".pi")

    def emit(self, ir: PluginIR) -> EmitResult:
        result = EmitResult(target=self.target)
        plugin_id = ir.identity.plugin_id

        for skill in ir.skills():
            self._emit_skill(result, skill, plugin_id)

        for cmd in ir.commands():
            self._emit_command(result, cmd, plugin_id)

        hooks = ir.hooks()
        if hooks:
            self._emit_hooks_extension(result, hooks, plugin_id, plugin_root=ir.source_path)

        for server in ir.mcp_servers():
            result.add_mapping(
                "mcp_server",
                server.name,
                MappingStatus.UNSUPPORTED,
                notes="Pi does not support MCP; use CLI tools exposed as skills",
            )

        for agent in ir.agents():
            result.add_mapping(
                "agent",
                agent.name,
                MappingStatus.UNSUPPORTED,
                notes="Pi does not support agent definitions",
            )

        for lsp in ir.lsp_servers():
            result.add_mapping(
                "lsp",
                lsp.name,
                MappingStatus.UNSUPPORTED,
                notes="Pi does not support custom LSP servers",
            )

        result.apply_target_native_files(
            plugin_root=ir.source_path,
            target=self.target,
            base_dir=self._base_dir,
        )

        return result

    def _emit_skill(self, result: EmitResult, skill: Skill, plugin_id: str) -> None:
        """Emit a skill to Pi format (Agent Skills standard)."""
        # Pi requires frontmatter name to match the parent directory name
        dir_name = f"{plugin_id}-{skill.name}"
        skill_dir = self._base_dir / "skills" / dir_name
        skill_path = skill_dir / "SKILL.md"

        # Build frontmatter — Pi supports the Agent Skills standard fields
        meta: dict[str, Any] = {"name": dir_name}
        if skill.description:
            meta["description"] = skill.description
        if skill.disable_model_invocation:
            meta["disable-model-invocation"] = True

        # Extract body from SKILL.md
        body = ""
        for f in skill.files:
            if f.relpath == "SKILL.md" and isinstance(f, TextFile):
                file_content = f.content
                if file_content.startswith("---"):
                    parts = file_content.split("---", 2)
                    if len(parts) >= 3:
                        body = parts[2].strip()
                else:
                    body = file_content
                break

        frontmatter = yaml.dump(meta, default_flow_style=False, sort_keys=False)
        content = f"---\n{frontmatter}---\n\n{body}"
        if not _emit_projected_skill(result, skill, skill_dir, content):
            return

        result.add_mapping(
            "skill",
            skill.name,
            MappingStatus.TRANSFORM if skill.includes else MappingStatus.NATIVE,
            target_path=skill_path,
            notes="Shared resources materialized into a self-contained skill"
            if skill.includes
            else None,
        )

    def _emit_command(self, result: EmitResult, cmd: Command, plugin_id: str) -> None:
        """Emit a command as a Pi prompt template."""
        prompt_name = f"{plugin_id}-{cmd.name}"
        prompt_path = self._base_dir / "prompts" / f"{prompt_name}.md"

        # Build frontmatter
        meta: dict[str, Any] = {}
        if cmd.description:
            meta["description"] = cmd.description

        body = cmd.markdown
        if cmd.argument_hint:
            body = f"{body}\n\nArguments: {cmd.argument_hint}"

        if meta:
            frontmatter = yaml.dump(meta, default_flow_style=False)
            content = f"---\n{frontmatter}---\n\n{body}"
        else:
            content = body

        result.add_file(prompt_path, content)

        # Pi prompt templates support $1, $2, $@ natively
        lost = []
        if cmd.has_arguments_var:
            result.add_diagnostic(
                Severity.INFO,
                f"Command '{cmd.name}' uses $ARGUMENTS — Pi uses $@ for the same purpose",
                component_ref=f"command:{cmd.name}",
            )

        result.add_mapping(
            "command",
            cmd.name,
            MappingStatus.TRANSFORM,
            target_path=prompt_path,
            notes=f"Invoke with /{prompt_name}",
            lost_features=lost,
        )

    def _emit_hooks_extension(
        self,
        result: EmitResult,
        hooks: list[Hook],
        plugin_id: str,
        *,
        plugin_root: Path | None = None,
    ) -> None:
        """Emit Claude command hooks as a Pi TypeScript extension."""
        event_map = {
            "SessionStart": "session_start",
            "UserPromptSubmit": "input",
            "PreToolUse": "tool_call",
            "PostToolUse": "tool_result",
            "Stop": "agent_end",
            "SessionEnd": "session_shutdown",
            "PreCompact": "session_before_compact",
            "PostCompact": "session_compact",
        }
        handlers_by_event: dict[str, list[str]] = {}

        for hook in hooks:
            for event in hook.events:
                pi_event = event_map.get(event.name)
                if pi_event is None:
                    result.add_mapping(
                        "hook",
                        event.name,
                        MappingStatus.UNSUPPORTED,
                        notes="Pi extension event mapping is not known for this Claude hook event",
                    )
                    result.add_diagnostic(
                        Severity.WARN,
                        f"Hook event '{event.name}' has no Pi extension equivalent",
                        component_ref=f"hook:{event.name}",
                    )
                    continue

                if event.matcher:
                    result.add_diagnostic(
                        Severity.WARN,
                        f"Pi extension hook for {event.name} does not preserve Claude matcher '{event.matcher}'",
                        component_ref=f"hook:{event.name}",
                    )

                for handler in event.handlers:
                    if handler.type.value == "command" and handler.command:
                        command = _resolve_claude_plugin_root(handler.command, plugin_root)
                        if command != handler.command:
                            result.add_diagnostic(
                                Severity.INFO,
                                "Resolved ${CLAUDE_PLUGIN_ROOT} in Pi extension hook command",
                                component_ref=f"hook:{event.name}",
                            )
                        handlers_by_event.setdefault(pi_event, []).append(command)
                    else:
                        result.add_diagnostic(
                            Severity.WARN,
                            f"Hook handler type '{handler.type.value}' is not supported in Pi extension conversion",
                            component_ref=f"hook:{event.name}",
                        )

        if not handlers_by_event:
            result.add_mapping(
                "hook",
                "hooks",
                MappingStatus.UNSUPPORTED,
                notes="No Pi-compatible command hooks found",
            )
            return

        lines = [
            'import { spawnSync } from "node:child_process";',
            "",
            "function runHook(command: string, payload: unknown) {",
            "  const result = spawnSync(command, {",
            "    shell: true,",
            "    input: JSON.stringify(payload),",
            '    encoding: "utf8",',
            '    stdio: ["pipe", "inherit", "inherit"],',
            "  });",
            "  if ((result.status ?? 1) !== 0) {",
            "    throw new Error(`Pi hook failed: ${command}`);",
            "  }",
            "}",
            "",
            "export default function (pi: any) {",
        ]

        for pi_event, commands in sorted(handlers_by_event.items()):
            lines.append(f"  pi.on({json.dumps(pi_event)}, async (event: unknown) => {{")
            for command in commands:
                lines.append(f"    runHook({json.dumps(command)}, event);")
            lines.append("  });")

        lines.append("}")
        lines.append("")

        extension_path = self._base_dir / "extensions" / f"{plugin_id}-hooks.ts"
        result.add_file(extension_path, "\n".join(lines))
        result.add_mapping(
            "hook",
            "hooks",
            MappingStatus.EMULATE,
            target_path=extension_path,
            notes="Converted supported Claude command hooks to a Pi TypeScript extension",
        )


class PrimeEmitter:
    """Project Markdown skills for Prime Rust 0.10.0; never emit executable plugins."""

    target = TargetTool.PRIME

    def __init__(self, scope: InstallScope = InstallScope.PROJECT) -> None:
        self.scope = scope
        self._base_dir = Path(".prime/agent")

    def emit(self, ir: PluginIR) -> EmitResult:
        result = EmitResult(
            target=self.target,
            prime_source=f"standalone:{ir.source_path.resolve()}"
            if ir.source_path
            else f"ir:{ir.identity.plugin_id}",
        )
        for skill in ir.skills():
            # Prime interprets a pyproject + src/<name>/__init__.py as an executable
            # Python skill. This adapter only promises Markdown resource projection.
            if any(file.relpath == "pyproject.toml" for file in skill.files):
                result.add_mapping(
                    "skill",
                    skill.name,
                    MappingStatus.UNSUPPORTED,
                    notes="Prime Python skill packages require native authoring",
                )
                continue
            if not skill.description or not skill.description.strip():
                result.add_diagnostic(
                    Severity.ERROR,
                    "Prime skills require a description",
                    component_ref=f"skill:{skill.name}",
                )
                continue
            name = f"{ir.identity.plugin_id}-{skill.name}"
            directory = self._base_dir / "skills" / name
            body = next(
                (
                    f.content
                    for f in skill.files
                    if f.relpath == "SKILL.md" and isinstance(f, TextFile)
                ),
                "",
            )
            if body.startswith("---"):
                body = body.split("---", 2)[-1].strip()
            meta: dict[str, Any] = {"name": name, "description": skill.description}
            if skill.disable_model_invocation:
                meta["disable-model-invocation"] = True
            content = "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n" + body
            if _emit_projected_skill(result, skill, directory, content):
                result.add_mapping(
                    "skill",
                    skill.name,
                    MappingStatus.TRANSFORM,
                    target_path=directory / "SKILL.md",
                    notes="Namespaced Markdown skill; supporting resources preserved",
                    lost_features=[
                        field
                        for field, value in {
                            "allowed-tools": skill.allowed_tools,
                            "model": skill.model,
                            "context": skill.context,
                            "agent": skill.agent,
                            "user-invocable": skill.user_invocable,
                        }.items()
                        if value is not None and value != []
                    ],
                )
        for component in ir.components:
            if not isinstance(component, Skill):
                result.add_mapping(
                    component.kind.value,
                    getattr(component, "name", "hooks"),
                    MappingStatus.UNSUPPORTED,
                    notes="Prime adapter supports Markdown skills only; no settings writes",
                )
        return result


# Factory function
def get_emitter(
    target: TargetTool,
    scope: InstallScope = InstallScope.PROJECT,
) -> CodexEmitter | CursorEmitter | OpenCodeEmitter | PiEmitter | PrimeEmitter:
    """Get an emitter for the specified target tool and scope."""
    if target == TargetTool.CODEX:
        return CodexEmitter(scope)
    elif target == TargetTool.CURSOR:
        return CursorEmitter(scope)
    elif target == TargetTool.OPENCODE:
        return OpenCodeEmitter(scope)
    elif target == TargetTool.PRIME:
        return PrimeEmitter(scope)
    elif target == TargetTool.PI:
        return PiEmitter(scope)
    else:
        raise ValueError(f"No emitter for target: {target}")
