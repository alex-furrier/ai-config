"""Fail-closed subprocess adapter for the Codex plugin lifecycle CLI."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ai_config.semver import SemanticVersion

_SUPPORTED_CODEX_MAJOR_MINORS = {
    (0, 144),
    (0, 145),
    (0, 146),
    (0, 147),
    (0, 148),
    (0, 149),
    (0, 153),
    (0, 156),
}
_SUPPORTED_CODEX_CONTRACT = "0.144.x through 0.149.x, 0.153.x, or 0.156.x"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_TERMINATION_GRACE_SECONDS = 0.5
_REAP_TIMEOUT_SECONDS = 0.5
_PROCESS_GROUP_POLL_SECONDS = 0.02
_MAX_ERROR_OUTPUT = 4_000
_KNOWN_SOURCE_TYPES = {"local", "git", "github", "remote"}
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class _DuplicateJSONKey(ValueError):
    """Raised when Codex emits an ambiguous JSON object."""


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"duplicate JSON key '{key}'")
        result[key] = value
    return result


def _as_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _timeout_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sanitize_output(value: str) -> str:
    """Bound and strip terminal control characters from child output."""
    without_ansi = _ANSI_ESCAPE.sub("", value)
    cleaned = "".join(
        character for character in without_ansi if character in "\n\r\t" or ord(character) >= 32
    )
    if len(cleaned) > _MAX_ERROR_OUTPUT:
        return cleaned[:_MAX_ERROR_OUTPUT] + "\n...[truncated]"
    return cleaned


@dataclass(frozen=True)
class CodexMarketplace:
    """One validated marketplace reported by Codex."""

    name: str
    root: Path
    source_type: str | None


@dataclass(frozen=True)
class CodexInstalledPlugin:
    """One validated installed plugin reported by Codex."""

    plugin_id: str
    name: str
    marketplace_name: str
    version: str
    enabled: bool
    source_path: Path | None
    marketplace_root: Path | None


@dataclass(frozen=True)
class CodexPluginInstall:
    """Validated result of installing one Codex plugin."""

    plugin_id: str
    name: str
    marketplace_name: str
    version: str
    installed_path: Path


@dataclass(frozen=True)
class CodexCommandError(RuntimeError):
    """Actionable failure from one Codex lifecycle stage."""

    stage: str
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    remediation: str

    def __str__(self) -> str:
        rendered = shlex.join(self.command)
        detail = self.stderr.strip() or self.stdout.strip() or "no command output"
        return (
            f"Codex plugin lifecycle failed at stage '{self.stage}': {rendered} "
            f"(exit {self.returncode}). {detail}. Remediation: {self.remediation}"
        )


@dataclass(frozen=True)
class _CommandOutput:
    returncode: int
    stdout: str
    stderr: str


class CodexCLI:
    """Typed wrapper around the supported Codex plugin JSON contract."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Codex subprocess timeout must be greater than zero seconds")
        self.executable = executable or shutil.which("codex") or "codex"
        self.timeout_seconds = timeout_seconds
        self._version: str | None = None

    def _error(
        self,
        stage: str,
        args: list[str],
        *,
        returncode: int | None,
        stdout: str,
        stderr: str,
        remediation: str,
    ) -> CodexCommandError:
        return CodexCommandError(
            stage=stage,
            command=(self.executable, *args),
            returncode=returncode,
            stdout=_sanitize_output(stdout),
            stderr=_sanitize_output(stderr),
            remediation=remediation,
        )

    def _stop_process_tree(self, process: subprocess.Popen[str]) -> None:
        """Terminate a timed-out command and every descendant in its process group."""
        if os.name == "posix":
            process_group_id = process.pid
            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass

            deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
            while time.monotonic() < deadline:
                process.poll()
                if not _process_group_exists(process_group_id):
                    break
                time.sleep(_PROCESS_GROUP_POLL_SECONDS)

            if _process_group_exists(process_group_id):
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            process.terminate()
            try:
                process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                process.kill()

        try:
            process.communicate(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=_REAP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass

    def _run(
        self,
        stage: str,
        args: list[str],
        *,
        remediation: str,
    ) -> _CommandOutput:
        command = (self.executable, *args)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            raise self._error(
                stage,
                args,
                returncode=None,
                stdout="",
                stderr=str(error),
                remediation=remediation,
            ) from error
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            self._stop_process_tree(process)
            raise self._error(
                stage,
                args,
                returncode=None,
                stdout=_timeout_text(error.stdout),
                stderr=f"command timed out after {self.timeout_seconds:g} seconds",
                remediation=remediation,
            ) from error
        if process.returncode != 0:
            raise self._error(
                stage,
                args,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                remediation=remediation,
            )
        return _CommandOutput(process.returncode, stdout, stderr)

    def _ensure_supported_version(self) -> str:
        if self._version is not None:
            return self._version
        remediation = (
            f"Install a supported Codex CLI ({_SUPPORTED_CODEX_CONTRACT}) or update "
            "ai-config's Codex adapter and probes for the new CLI contract."
        )
        output = self._run("inspect-version", ["--version"], remediation=remediation)
        match = re.fullmatch(r"codex-cli (\S+)\s*", output.stdout)
        if match is None:
            raise self._error(
                "inspect-version",
                ["--version"],
                returncode=output.returncode,
                stdout=output.stdout,
                stderr="unrecognized Codex version output",
                remediation=remediation,
            )
        version_text = match.group(1)
        try:
            version = SemanticVersion.parse(version_text, context="Codex CLI version")
        except ValueError as error:
            raise self._error(
                "inspect-version",
                ["--version"],
                returncode=output.returncode,
                stdout=output.stdout,
                stderr=str(error),
                remediation=remediation,
            ) from error
        if (version.major, version.minor) not in _SUPPORTED_CODEX_MAJOR_MINORS:
            raise self._error(
                "inspect-version",
                ["--version"],
                returncode=output.returncode,
                stdout=output.stdout,
                stderr=(
                    f"unsupported Codex CLI response contract {version_text}; "
                    f"expected {_SUPPORTED_CODEX_CONTRACT}"
                ),
                remediation=remediation,
            )
        self._version = version_text
        return version_text

    def run_json(
        self,
        stage: str,
        args: list[str],
        *,
        remediation: str,
    ) -> dict[str, object]:
        """Run one JSON command and reject malformed or duplicate-key payloads."""
        output = self._run(stage, args, remediation=remediation)
        try:
            payload: object = json.loads(output.stdout, object_pairs_hook=_json_object)
        except (json.JSONDecodeError, _DuplicateJSONKey) as error:
            raise self._error(
                stage,
                args,
                returncode=output.returncode,
                stdout=output.stdout,
                stderr=f"expected unambiguous JSON output: {error}",
                remediation=remediation,
            ) from error
        if not isinstance(payload, dict):
            raise self._error(
                stage,
                args,
                returncode=output.returncode,
                stdout=output.stdout,
                stderr="expected a JSON object",
                remediation=remediation,
            )
        return payload

    def _schema_error(
        self,
        stage: str,
        args: list[str],
        payload: dict[str, object],
        detail: str,
        remediation: str,
    ) -> CodexCommandError:
        return self._error(
            stage,
            args,
            returncode=0,
            stdout=json.dumps(payload, sort_keys=True),
            stderr=f"invalid supported Codex JSON response: {detail}",
            remediation=remediation,
        )

    def _optional_marketplace_source(
        self,
        entry: dict[str, object],
        *,
        field: str,
        stage: str,
        args: list[str],
        payload: dict[str, object],
        remediation: str,
    ) -> tuple[str, str] | None:
        if "marketplaceSource" not in entry:
            return None
        source = _as_object_dict(entry["marketplaceSource"])
        if source is None:
            detail = f"{field} must be an object when present"
            raise self._schema_error(stage, args, payload, detail, remediation)
        source_type = source.get("sourceType")
        source_value = source.get("source")
        if (
            not isinstance(source_type, str)
            or source_type not in _KNOWN_SOURCE_TYPES
            or not isinstance(source_value, str)
            or not source_value
        ):
            detail = f"{field} has an unknown source type or empty source"
            raise self._schema_error(stage, args, payload, detail, remediation)
        return source_type, source_value

    def list_marketplaces(self) -> list[CodexMarketplace]:
        self._ensure_supported_version()
        args = ["plugin", "marketplace", "list", "--json"]
        remediation = "Run `codex plugin marketplace list --json` and repair Codex config errors."
        payload = self.run_json("list-marketplaces", args, remediation=remediation)
        value = payload.get("marketplaces")
        if not isinstance(value, list):
            raise self._schema_error(
                "list-marketplaces", args, payload, "marketplaces must be an array", remediation
            )
        results: list[CodexMarketplace] = []
        seen: set[str] = set()
        for index, entry in enumerate(value):
            object_entry = _as_object_dict(entry)
            if object_entry is None:
                raise self._schema_error(
                    "list-marketplaces",
                    args,
                    payload,
                    f"marketplaces[{index}] must be an object with string keys",
                    remediation,
                )
            entry = object_entry
            name = entry.get("name")
            root = entry.get("root")
            if not isinstance(name, str) or not name:
                detail = f"marketplaces[{index}].name must be a non-empty string"
                raise self._schema_error("list-marketplaces", args, payload, detail, remediation)
            if name in seen:
                raise self._schema_error(
                    "list-marketplaces",
                    args,
                    payload,
                    f"duplicate marketplace '{name}'",
                    remediation,
                )
            if not isinstance(root, str) or not root:
                detail = f"marketplaces[{index}].root must be a non-empty string"
                raise self._schema_error("list-marketplaces", args, payload, detail, remediation)
            marketplace_source = self._optional_marketplace_source(
                entry,
                field=f"marketplaces[{index}].marketplaceSource",
                stage="list-marketplaces",
                args=args,
                payload=payload,
                remediation=remediation,
            )
            if (
                marketplace_source is not None
                and marketplace_source[0] == "local"
                and Path(marketplace_source[1]).resolve() != Path(root).resolve()
            ):
                detail = f"marketplaces[{index}].marketplaceSource and local root disagree"
                raise self._schema_error("list-marketplaces", args, payload, detail, remediation)
            seen.add(name)
            results.append(
                CodexMarketplace(
                    name=name,
                    root=Path(root).resolve(),
                    source_type=marketplace_source[0] if marketplace_source is not None else None,
                )
            )
        return results

    def add_marketplace(self, path: str, expected_name: str) -> CodexMarketplace:
        self._ensure_supported_version()
        args = ["plugin", "marketplace", "add", path, "--json"]
        remediation = "Validate the generated .agents/plugins/marketplace.json, then retry sync."
        payload = self.run_json("add-marketplace", args, remediation=remediation)
        name = payload.get("marketplaceName")
        root = payload.get("installedRoot")
        already_added = payload.get("alreadyAdded")
        if (
            not isinstance(name, str)
            or name != expected_name
            or not isinstance(root, str)
            or not isinstance(already_added, bool)
        ):
            raise self._schema_error(
                "add-marketplace",
                args,
                payload,
                "marketplaceName, installedRoot, or alreadyAdded did not match the request",
                remediation,
            )
        expected_root = Path(path).resolve()
        if Path(root).resolve() != expected_root:
            raise self._schema_error(
                "add-marketplace",
                args,
                payload,
                f"installedRoot does not match requested path {expected_root}",
                remediation,
            )
        return CodexMarketplace(name=name, root=expected_root, source_type="local")

    def remove_marketplace(self, name: str) -> None:
        self._ensure_supported_version()
        args = ["plugin", "marketplace", "remove", name, "--json"]
        remediation = "Inspect the named ai-config marketplace with Codex, then retry sync."
        payload = self.run_json("remove-marketplace", args, remediation=remediation)
        if (
            set(payload) != {"marketplaceName", "installedRoot"}
            or payload["marketplaceName"] != name
            or payload["installedRoot"] is not None
        ):
            raise self._schema_error(
                "remove-marketplace",
                args,
                payload,
                "response did not confirm removal of the requested marketplace",
                remediation,
            )

    def _validate_available_plugin(
        self,
        entry: object,
        *,
        index: int,
        args: list[str],
        payload: dict[str, object],
        remediation: str,
    ) -> tuple[str, str]:
        object_entry = _as_object_dict(entry)
        if object_entry is None:
            raise self._schema_error(
                "list-plugins",
                args,
                payload,
                f"available[{index}] must be an object with string keys",
                remediation,
            )
        plugin_id = object_entry.get("pluginId")
        name = object_entry.get("name")
        marketplace_name = object_entry.get("marketplaceName")
        version = object_entry.get("version")
        source = _as_object_dict(object_entry.get("source"))
        if (
            not isinstance(plugin_id, str)
            or not plugin_id
            or not isinstance(name, str)
            or not name
            or not isinstance(marketplace_name, str)
            or not marketplace_name
            or not isinstance(version, str)
            or object_entry.get("installed") is not False
            or not isinstance(object_entry.get("enabled"), bool)
            or not isinstance(object_entry.get("installPolicy"), str)
            or not isinstance(object_entry.get("authPolicy"), str)
        ):
            raise self._schema_error(
                "list-plugins",
                args,
                payload,
                f"available[{index}] is missing required typed fields",
                remediation,
            )
        if plugin_id != f"{name}@{marketplace_name}":
            raise self._schema_error(
                "list-plugins",
                args,
                payload,
                f"available[{index}] plugin identity fields disagree",
                remediation,
            )
        try:
            SemanticVersion.parse(version, context=f"available Codex plugin {plugin_id} version")
        except ValueError as error:
            raise self._schema_error(
                "list-plugins", args, payload, f"available[{index}] {error}", remediation
            ) from error
        source_type = source.get("source") if source is not None else None
        source_path = source.get("path") if source is not None else None
        if source_type not in _KNOWN_SOURCE_TYPES:
            detail = f"available[{index}].source has an unknown source type"
            raise self._schema_error("list-plugins", args, payload, detail, remediation)
        assert isinstance(source_type, str)
        if source_type == "local" and (not isinstance(source_path, str) or not source_path):
            detail = f"available[{index}].source.path must be a non-empty string"
            raise self._schema_error("list-plugins", args, payload, detail, remediation)
        if source_type == "remote":
            remote_id = source.get("id") if source is not None else None
            remote_url = source.get("url") if source is not None else None
            if not (
                (isinstance(remote_id, str) and remote_id)
                or (isinstance(remote_url, str) and remote_url)
            ):
                detail = f"available[{index}].source.id or .url must be a non-empty string"
                raise self._schema_error("list-plugins", args, payload, detail, remediation)
        elif source_type != "local" and (
            source is None or not isinstance(source.get("url"), str) or not source.get("url")
        ):
            detail = f"available[{index}].source.url must be a non-empty string"
            raise self._schema_error("list-plugins", args, payload, detail, remediation)
        marketplace_source = self._optional_marketplace_source(
            object_entry,
            field=f"available[{index}].marketplaceSource",
            stage="list-plugins",
            args=args,
            payload=payload,
            remediation=remediation,
        )
        if (
            source_type == "local"
            and marketplace_source is not None
            and marketplace_source[0] == "local"
        ):
            if not isinstance(source_path, str):
                detail = f"available[{index}].source.path must be a non-empty string"
                raise self._schema_error("list-plugins", args, payload, detail, remediation)
            source_root = Path(source_path).resolve()
            marketplace_root = Path(marketplace_source[1]).resolve()
            if marketplace_root not in source_root.parents:
                detail = f"available[{index}] plugin source is outside its marketplaceSource root"
                raise self._schema_error("list-plugins", args, payload, detail, remediation)
        return plugin_id, source_type

    def list_plugins(self) -> list[CodexInstalledPlugin]:
        self._ensure_supported_version()
        args = ["plugin", "list", "--available", "--json"]
        remediation = "Run `codex plugin list --json` and repair Codex config errors."
        payload = self.run_json("list-plugins", args, remediation=remediation)
        installed = payload.get("installed")
        available = payload.get("available")
        if not isinstance(installed, list) or not isinstance(available, list):
            raise self._schema_error(
                "list-plugins",
                args,
                payload,
                "installed and available must both be arrays",
                remediation,
            )
        available_seen: set[str] = set()
        for index, entry in enumerate(available):
            plugin_id, source_type = self._validate_available_plugin(
                entry,
                index=index,
                args=args,
                payload=payload,
                remediation=remediation,
            )
            if plugin_id in available_seen and source_type != "remote":
                raise self._schema_error(
                    "list-plugins",
                    args,
                    payload,
                    f"duplicate available plugin '{plugin_id}'",
                    remediation,
                )
            available_seen.add(plugin_id)

        results: list[CodexInstalledPlugin] = []
        seen: set[str] = set()
        for index, entry in enumerate(installed):
            object_entry = _as_object_dict(entry)
            if object_entry is None:
                raise self._schema_error(
                    "list-plugins",
                    args,
                    payload,
                    f"installed[{index}] must be an object with string keys",
                    remediation,
                )
            entry = object_entry
            plugin_id = entry.get("pluginId")
            name = entry.get("name")
            marketplace_name = entry.get("marketplaceName")
            version = entry.get("version")
            enabled = entry.get("enabled")
            source = entry.get("source")
            if (
                not isinstance(plugin_id, str)
                or not plugin_id
                or not isinstance(name, str)
                or not name
                or not isinstance(marketplace_name, str)
                or not marketplace_name
                or not isinstance(version, str)
                or not isinstance(enabled, bool)
                or entry.get("installed") is not True
                or not isinstance(entry.get("installPolicy"), str)
                or not isinstance(entry.get("authPolicy"), str)
            ):
                raise self._schema_error(
                    "list-plugins",
                    args,
                    payload,
                    f"installed[{index}] is missing required typed fields",
                    remediation,
                )
            if plugin_id != f"{name}@{marketplace_name}":
                raise self._schema_error(
                    "list-plugins",
                    args,
                    payload,
                    f"installed[{index}] plugin identity fields disagree",
                    remediation,
                )
            if plugin_id in available_seen:
                raise self._schema_error(
                    "list-plugins",
                    args,
                    payload,
                    f"plugin '{plugin_id}' appears in both installed and available rows",
                    remediation,
                )
            if plugin_id in seen:
                raise self._schema_error(
                    "list-plugins",
                    args,
                    payload,
                    f"duplicate installed plugin '{plugin_id}'",
                    remediation,
                )
            try:
                SemanticVersion.parse(
                    version, context=f"installed Codex plugin {plugin_id} version"
                )
            except ValueError as error:
                raise self._schema_error(
                    "list-plugins", args, payload, str(error), remediation
                ) from error
            source_object = _as_object_dict(source)
            source_type = source_object.get("source") if source_object is not None else None
            if source_object is None or source_type not in _KNOWN_SOURCE_TYPES:
                detail = f"installed[{index}].source has an unknown source type"
                raise self._schema_error("list-plugins", args, payload, detail, remediation)
            source_path_value = source_object.get("path")
            if source_type == "local" and (
                not isinstance(source_path_value, str) or not source_path_value
            ):
                detail = f"installed[{index}].source.path must be a non-empty string"
                raise self._schema_error("list-plugins", args, payload, detail, remediation)
            resolved_source = (
                Path(source_path_value).resolve() if isinstance(source_path_value, str) else None
            )
            marketplace_source = self._optional_marketplace_source(
                entry,
                field=f"installed[{index}].marketplaceSource",
                stage="list-plugins",
                args=args,
                payload=payload,
                remediation=remediation,
            )
            marketplace_root = (
                Path(marketplace_source[1]).resolve()
                if marketplace_source is not None and marketplace_source[0] == "local"
                else None
            )
            if (
                marketplace_root is not None
                and resolved_source is not None
                and marketplace_root not in resolved_source.parents
            ):
                detail = f"installed[{index}] plugin source is outside its marketplaceSource root"
                raise self._schema_error("list-plugins", args, payload, detail, remediation)
            seen.add(plugin_id)
            results.append(
                CodexInstalledPlugin(
                    plugin_id=plugin_id,
                    name=name,
                    marketplace_name=marketplace_name,
                    version=version,
                    enabled=enabled,
                    source_path=resolved_source,
                    marketplace_root=marketplace_root,
                )
            )
        return results

    def add_plugin(self, plugin_id: str) -> CodexPluginInstall:
        self._ensure_supported_version()
        args = ["plugin", "add", plugin_id, "--json"]
        remediation = (
            "Confirm the generated marketplace is registered and the plugin is available, "
            "then retry sync."
        )
        payload = self.run_json("install-plugin", args, remediation=remediation)
        name, marketplace_name = self._plugin_parts(plugin_id, "install-plugin", args, remediation)
        version = payload.get("version")
        installed_path = payload.get("installedPath")
        if (
            payload.get("pluginId") != plugin_id
            or payload.get("name") != name
            or payload.get("marketplaceName") != marketplace_name
            or not isinstance(version, str)
            or not isinstance(installed_path, str)
            or not installed_path
            or not isinstance(payload.get("authPolicy"), str)
        ):
            raise self._schema_error(
                "install-plugin",
                args,
                payload,
                "response identity or required fields did not match the install request",
                remediation,
            )
        try:
            SemanticVersion.parse(version, context=f"installed Codex plugin {plugin_id} version")
        except ValueError as error:
            raise self._schema_error(
                "install-plugin", args, payload, str(error), remediation
            ) from error
        return CodexPluginInstall(
            plugin_id=plugin_id,
            name=name,
            marketplace_name=marketplace_name,
            version=version,
            installed_path=Path(installed_path).resolve(),
        )

    def remove_plugin(self, plugin_id: str) -> None:
        self._ensure_supported_version()
        args = ["plugin", "remove", plugin_id, "--json"]
        remediation = (
            "Inspect the ai-config-owned plugin entry and cache with Codex, then retry sync."
        )
        payload = self.run_json("remove-plugin", args, remediation=remediation)
        name, marketplace_name = self._plugin_parts(plugin_id, "remove-plugin", args, remediation)
        if (
            payload.get("pluginId") != plugin_id
            or payload.get("name") != name
            or payload.get("marketplaceName") != marketplace_name
        ):
            raise self._schema_error(
                "remove-plugin",
                args,
                payload,
                "response did not confirm removal of the requested plugin",
                remediation,
            )

    def _plugin_parts(
        self,
        plugin_id: str,
        stage: str,
        args: list[str],
        remediation: str,
    ) -> tuple[str, str]:
        parts = plugin_id.rsplit("@", 1)
        if len(parts) != 2 or not all(parts):
            raise self._error(
                stage,
                args,
                returncode=None,
                stdout="",
                stderr=f"invalid plugin selector '{plugin_id}'",
                remediation=remediation,
            )
        return parts[0], parts[1]
