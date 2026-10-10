"""Conservative Prime skill writes: digest-owned updates, no automatic removal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from ai_config.output_safety import validated_output_path

if TYPE_CHECKING:
    from ai_config.converters.emitters import EmittedFile


def _write_prime_files(
    root: Path, files: list[EmittedFile], *, source: str | None, dry_run: bool = False
) -> list[Path]:
    """Preflight the entire batch before writing; preserve stale and edited files.

    The ledger only grants update authority over files this adapter created.
    Identical unowned files are collisions too. No settings, tokens or context
    files belong to this adapter. Live writes are serialized by the outer file lock.
    """
    ledger = validated_output_path(root, Path(".ai-config/prime-ownership.json"))
    _check_ancestors(root, ledger)
    owned: dict[str, dict[str, str]] = {}
    if not source:
        raise ValueError("Prime writes require source identity")
    if ledger.exists():
        if not ledger.is_file():
            raise ValueError("Prime ownership ledger is not a regular file")
        payload = json.loads(ledger.read_text())
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != 1
            or not isinstance(payload.get("files"), dict)
        ):
            raise ValueError("Invalid Prime ownership ledger")
        owned = payload["files"]
        if any(
            not isinstance(k, str)
            or not isinstance(v, dict)
            or set(v) != {"source", "sha256"}
            or not all(isinstance(value, str) for value in v.values())
            for k, v in owned.items()
        ):
            raise ValueError("Invalid Prime ownership entries")
    prepared: list[tuple[Path, bytes, bool]] = []
    next_owned = dict(owned)
    seen: set[Path] = set()
    for file in files:
        parts = file.path.parts
        if not (parts[:3] == (".prime", "agent", "skills") and len(parts) >= 5) or ".." in parts:
            raise ValueError(f"Prime adapter cannot own this path: {file.path}")
        destination = validated_output_path(root, file.path)
        _check_ancestors(root, destination)
        if any(
            destination == prior or destination in prior.parents or prior in destination.parents
            for prior in seen
        ):
            raise ValueError(f"Conflicting Prime output paths: {file.path}")
        seen.add(destination)
        skill_directory = validated_output_path(root, Path(*parts[:4]))
        if (skill_directory / "pyproject.toml").exists() or (
            skill_directory / "pyproject.toml"
        ).is_symlink():
            raise ValueError(
                f"Prime Markdown destination contains Python package metadata: {skill_directory}"
            )
        if file.path.name == "pyproject.toml" and len(parts) == 5:
            raise ValueError("Prime adapter cannot emit Python skill package metadata")
        key = file.path.as_posix()
        data = file.content.encode("utf-8") if isinstance(file.content, str) else file.content
        if key in next_owned and key not in owned:
            raise ValueError(f"Duplicate Prime output path: {key}")
        if destination.exists():
            if not destination.is_file() or owned.get(key) != {
                "source": source,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }:
                raise ValueError(f"Prime output is unowned or modified; preserving {destination}")
        if key in owned and owned[key]["source"] != source:
            raise ValueError(f"Prime output belongs to another source; preserving {destination}")
        next_owned[key] = {"source": source, "sha256": hashlib.sha256(data).hexdigest()}
        prepared.append((destination, data, file.executable))
    if not dry_run and files:
        for destination, data, executable in prepared:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists() or destination.read_bytes() != data:
                destination.write_bytes(data)
            if executable:
                destination.chmod(destination.stat().st_mode | 0o111)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"schema": 1, "files": next_owned}, indent=2, sort_keys=True) + "\n"
        if not ledger.exists() or ledger.read_text() != content:
            ledger.write_text(content)
    return [destination for destination, _data, _executable in prepared]


def write_prime_files(
    root: Path, files: list[EmittedFile], *, source: str | None, dry_run: bool = False
) -> list[Path]:
    """Serialize live ownership updates on POSIX; dry runs create no state."""
    if dry_run or not files:
        return _write_prime_files(root, files, source=source, dry_run=dry_run)
    try:
        import fcntl
    except ImportError as error:
        raise ValueError("Prime ownership writes currently require POSIX file locking") from error
    # Preflight before creating even the lock directory; repeat under the lock.
    _write_prime_files(root, files, source=source, dry_run=True)
    lock = validated_output_path(root, Path(".ai-config/prime-ownership.lock"))
    if lock.exists() and not lock.is_file():
        raise ValueError("Prime ownership lock is not a regular file")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            return _write_prime_files(root, files, source=source)
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _check_ancestors(root: Path, destination: Path) -> None:
    """Reject user file/dir conflicts before the first write, including ledger parents."""
    resolved_root = root.expanduser().resolve()
    for parent in destination.parents:
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"Prime output ancestor is not a directory: {parent}")
        if parent == resolved_root:
            break
