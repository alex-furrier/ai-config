"""Filesystem observation and persistence helpers for sync.

This module owns state access below orchestration so planning and execution modules do not import
the public :mod:`ai_config.operations` entry point.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePath

from ai_config.adapters import claude
from ai_config.converters.codex_package import CodexPackageSpec
from ai_config.path_policy import GENERATED_PYTHON_ENVIRONMENT_DIRECTORY_NAMES
from ai_config.pi_ownership import load_pi_ownership
from ai_config.source_safety import ContainedSource, SourceSafetyError
from ai_config.sync_pipeline import SourceProvenance
from ai_config.types import ClaudeTargetConfig, ConversionConfig, PluginConfig, PluginSource

_CONVERSION_CACHE_VERSION = 9


def conversion_cache_path() -> Path:
    return Path.home() / ".ai-config" / "cache" / "conversion-hashes.json"


def _validated_cached_output_dirs(raw: dict, key: str, cache_path: Path) -> list[str]:
    """Return canonical absolute cached roots or reject the whole cache fail-closed."""
    value = raw.get(key, [])
    label = "Codex" if key == "codex_output_dirs" else "Pi"
    message = f"Invalid conversion cache {label} output roots at {cache_path}; clear it and retry"
    if not isinstance(value, list):
        raise ValueError(message)

    resolved: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or "\0" in item:
            raise ValueError(message)
        try:
            expanded = Path(item).expanduser()
            if not expanded.is_absolute():
                raise ValueError("output root is not absolute")
            canonical = expanded.resolve()
            cursor = Path(canonical.anchor)
            for part in canonical.parts[1:]:
                cursor /= part
                try:
                    if stat.S_ISLNK(cursor.lstat().st_mode):
                        raise ValueError("output root contains an unresolved symlink")
                except FileNotFoundError:
                    break
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError(message) from error
        if not canonical.is_absolute():
            raise ValueError(message)
        resolved.add(str(canonical))
    return sorted(resolved)


def _validate_cache_entries(
    entries: object, cache_path: Path
) -> dict[str, dict[str, dict[str, object]]]:
    message = f"Invalid conversion cache entries at {cache_path}; clear it and retry"
    if not isinstance(entries, dict):
        raise ValueError(message)
    validated: dict[str, dict[str, dict[str, object]]] = {}
    for selector, signatures in entries.items():
        if not isinstance(selector, str) or not selector or not isinstance(signatures, dict):
            raise ValueError(message)
        validated_signatures: dict[str, dict[str, object]] = {}
        for signature, value in signatures.items():
            if not isinstance(signature, str) or not isinstance(value, dict):
                raise ValueError(message)
            value_map = {str(key): item for key, item in value.items()}
            try:
                settings = json.loads(signature)
            except json.JSONDecodeError as error:
                raise ValueError(message) from error
            if not isinstance(settings, dict):
                raise ValueError(message)
            required = ("hash", "source_path", "source_provenance", "updated_at")
            if any(
                not isinstance(value_map.get(key), str) or not value_map[key] for key in required
            ):
                raise ValueError(message)
            output_hash = value_map.get("codex_output_hash")
            if output_hash is not None and (not isinstance(output_hash, str) or not output_hash):
                raise ValueError(message)
            validated_signatures[signature] = value_map
        validated[selector] = validated_signatures
    return validated


def load_conversion_cache() -> dict:
    cache_path = conversion_cache_path()
    empty = {
        "version": _CONVERSION_CACHE_VERSION,
        "entries": {},
        "codex_output_dirs": [],
        "pi_output_dirs": [],
    }
    if not cache_path.exists():
        return empty
    try:
        raw = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Invalid conversion cache at {cache_path}; clear the cache and retry: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid conversion cache object at {cache_path}; clear it and retry")
    codex_dirs = _validated_cached_output_dirs(raw, "codex_output_dirs", cache_path)
    pi_dirs = _validated_cached_output_dirs(raw, "pi_output_dirs", cache_path)
    if raw.get("version") in {7, 8}:
        # v8 content hashes remain valid, but v9 keys entries by configured plugin identity
        # instead of an incidental source path. Ownership cleanup still needs prior roots.
        return {
            "version": _CONVERSION_CACHE_VERSION,
            "entries": {},
            "codex_output_dirs": codex_dirs,
            "pi_output_dirs": pi_dirs,
        }
    if raw.get("version") != _CONVERSION_CACHE_VERSION:
        return empty
    raw["entries"] = _validate_cache_entries(raw.get("entries"), cache_path)
    raw["codex_output_dirs"] = codex_dirs
    raw["pi_output_dirs"] = pi_dirs
    return raw


def save_conversion_cache(cache: dict) -> None:
    cache_path = conversion_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))


def conversion_signature(conversion: ConversionConfig, output_dir: Path) -> str:
    return json.dumps(
        {
            "targets": sorted(conversion.targets),
            "scope": conversion.scope,
            "output_dir": str(output_dir),
        },
        sort_keys=True,
    )


def _is_installed_claude_cache_source(plugin_path: Path, provenance: SourceProvenance) -> bool:
    if provenance != "installed_plugin":
        return False
    configured_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    config_dir = Path(configured_dir).expanduser() if configured_dir else Path.home() / ".claude"
    cache_dir = (config_dir / "plugins" / "cache").absolute()
    try:
        source = plugin_path.absolute()
        if ".." in source.parts or ".." in cache_dir.parts:
            return False
        return bool(source.relative_to(cache_dir).parts)
    except (ValueError, OSError, RuntimeError):
        return False


def _compute_plugin_hash(
    plugin_path: Path,
    *,
    ignored_paths: frozenset[PurePath] = frozenset(),
    provenance: SourceProvenance = "configured_local",
) -> str | None:
    hasher = hashlib.sha256()
    context = "plugin conversion hash" if ignored_paths else "plugin hash"
    try:
        with ContainedSource(
            plugin_path,
            excluded_directory_names=GENERATED_PYTHON_ENVIRONMENT_DIRECTORY_NAMES,
        ) as source:
            files, context_mirrors = source.snapshot_files_and_context_mirrors(
                context=context,
                excluded_directory_names=GENERATED_PYTHON_ENVIRONMENT_DIRECTORY_NAMES,
                # Only Claude's observed installed cache sources have runtime PID markers.
                excluded_root_directory_names=(
                    frozenset({".in_use"})
                    if _is_installed_claude_cache_source(plugin_path, provenance)
                    else frozenset()
                ),
            )
            for relative in files:
                if relative in ignored_paths:
                    continue
                item = source.read_file(relative, context=context)
                hasher.update(os.fsencode(relative.as_posix()))
                hasher.update(b"\0")
                hasher.update(b"x" if item.executable else b"-")
                hasher.update(len(item.content).to_bytes(8, "big"))
                hasher.update(item.content)
            for mirror in context_mirrors:
                target = os.fsencode(mirror.target)
                hasher.update(os.fsencode(mirror.relative_path.as_posix()))
                hasher.update(b"\0l")
                hasher.update(len(target).to_bytes(8, "big"))
                hasher.update(target)
        return hasher.hexdigest()
    except (OSError, SourceSafetyError, UnicodeError):
        return None


def compute_plugin_hash(
    plugin_path: Path, *, provenance: SourceProvenance = "configured_local"
) -> str | None:
    """Hash plugin bytes, omitting Claude's markers only for installed cache sources."""
    return _compute_plugin_hash(plugin_path, provenance=provenance)


def compute_plugin_conversion_hash(
    plugin_path: Path,
    *,
    ignored_paths: frozenset[PurePath],
    provenance: SourceProvenance = "configured_local",
) -> str | None:
    """Hash conversion inputs while omitting parser-confirmed generated artifacts."""
    return _compute_plugin_hash(plugin_path, ignored_paths=ignored_paths, provenance=provenance)


def _lexical_regular_directory(path: Path) -> Path | None:
    """Return an absolute lexical directory path, rejecting a symlink at the selection root."""
    try:
        selected = path.expanduser().absolute()
        selected_stat = selected.lstat()
    except (OSError, RuntimeError, ValueError):
        return None
    if stat.S_ISLNK(selected_stat.st_mode) or not stat.S_ISDIR(selected_stat.st_mode):
        return None
    return selected


def resolve_local_marketplace_plugin_path(marketplace_root: Path, plugin_name: str) -> Path | None:
    for manifest_path in (
        marketplace_root / ".claude-plugin" / "marketplace.json",
        marketplace_root / "marketplace.json",
    ):
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        plugins = manifest.get("plugins", [])
        if not isinstance(plugins, list):
            continue
        for entry in plugins:
            if not isinstance(entry, dict) or entry.get("name") != plugin_name:
                continue
            source = entry.get("source")
            if not isinstance(source, str) or "\0" in source:
                continue
            try:
                source_path = Path(source).expanduser()
            except (RuntimeError, ValueError):
                continue
            if not source_path.is_absolute():
                source_path = marketplace_root / source_path
            selected = _lexical_regular_directory(source_path)
            if selected is not None:
                return selected
    return _lexical_regular_directory(marketplace_root / plugin_name)


def resolve_plugin_conversion_path(
    config: ClaudeTargetConfig,
    plugin_config: PluginConfig,
    installed: claude.InstalledPlugin | None,
) -> Path | None:
    """Resolve the configured source authority, using installed state only as fallback.

    A configured local marketplace remains authoritative after Claude installs a cached copy.
    Remote and marketplace-less plugins depend on Claude's installed source instead.
    """
    marketplace_name = plugin_config.marketplace
    marketplace = (
        config.marketplaces.get(marketplace_name) if marketplace_name is not None else None
    )
    if marketplace is not None and marketplace.source == PluginSource.LOCAL:
        return resolve_local_marketplace_plugin_path(
            Path(marketplace.path), plugin_config.plugin_name
        )

    installed_path = (
        Path(installed.install_path) if installed is not None and installed.install_path else None
    )
    return installed_path if installed_path is not None and installed_path.is_dir() else None


def compute_owned_codex_hash(spec: CodexPackageSpec) -> str | None:
    root = spec.marketplace_path
    if not root.is_dir() or root.is_symlink():
        return None
    hasher = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                return None
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            hasher.update(relative.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(b"x" if path.stat().st_mode & 0o111 else b"-")
            hasher.update(len(data).to_bytes(8, "big"))
            hasher.update(data)
        return hasher.hexdigest()
    except OSError:
        return None


def resolve_conversion_output_dir(conversion: ConversionConfig) -> Path:
    if conversion.output_dir:
        return Path(conversion.output_dir)
    return Path.home() if conversion.scope == "user" else Path.cwd()


def owned_codex_output_dirs(conversion: ConversionConfig | None, cache: dict) -> list[Path]:
    candidates: set[Path] = {Path.cwd().resolve(), Path.home().resolve()}
    if conversion is not None:
        candidates.add(resolve_conversion_output_dir(conversion).resolve())
    tracked_output_dirs = cache.get("codex_output_dirs", [])
    if not isinstance(tracked_output_dirs, list) or any(
        not isinstance(output_dir, str) or not output_dir for output_dir in tracked_output_dirs
    ):
        raise ValueError("Invalid cached Codex output roots; clear the cache and retry")
    candidates.update(Path(output_dir).expanduser().resolve() for output_dir in tracked_output_dirs)
    entries = cache.get("entries")
    if isinstance(entries, dict):
        for signature_map in entries.values():
            if not isinstance(signature_map, dict):
                raise ValueError("Invalid conversion cache entry table; clear the cache and retry")
            for signature in signature_map:
                if not isinstance(signature, str):
                    raise ValueError(
                        "Invalid conversion cache signature; clear the cache and retry"
                    )
                try:
                    settings = json.loads(signature)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "Invalid conversion cache signature JSON; clear the cache and retry"
                    ) from error
                if not isinstance(settings, dict):
                    raise ValueError("Invalid conversion cache settings; clear the cache and retry")
                targets_value = settings.get("targets")
                output_dir_value = settings.get("output_dir")
                if (
                    isinstance(targets_value, list)
                    and "codex" in targets_value
                    and isinstance(output_dir_value, str)
                ):
                    candidates.add(Path(output_dir_value).expanduser().resolve())
    return sorted(
        (
            candidate
            for candidate in candidates
            if (candidate / ".ai-config" / "codex" / "ownership.json").is_file()
        ),
        key=lambda item: item.as_posix(),
    )


def pi_root_has_ownership(root: Path) -> bool:
    try:
        return bool(load_pi_ownership(root))
    except ValueError:
        return True
