"""Conservative Prime skill writes: digest-owned updates, no automatic removal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

from ai_config.output_safety import validated_output_path

if TYPE_CHECKING:
    from ai_config.converters.emitters import EmittedFile


PrimeActionName: TypeAlias = Literal[
    "create_prime_output", "update_prime_output", "noop_prime_output"
]


@dataclass(frozen=True)
class PrimeDesiredFile:
    source_plugin: str
    relative_path: Path
    content: bytes
    executable: bool = False


def prime_desired_files(files: list[EmittedFile], source_plugin: str) -> list[PrimeDesiredFile]:
    """Attach ownership only where emitted bytes enter Prime reconciliation."""
    return [
        PrimeDesiredFile(
            source_plugin,
            item.path,
            item.content.encode("utf-8") if isinstance(item.content, str) else item.content,
            item.executable,
        )
        for item in files
    ]


@dataclass(frozen=True)
class PrimeAction:
    action: PrimeActionName
    path: Path
    reason: str


def standalone_prime_source_identity(plugin_path: Path) -> str:
    """Preserve the standalone ledger identity at the conversion boundary."""
    return f"standalone:{plugin_path.resolve()}"


def read_prime_ownership(root: Path) -> str | None:
    """Observe exact ledger contents, including absence, without following symlinks."""
    ledger = validated_output_path(root, Path(".ai-config/prime-ownership.json"))
    _check_ancestors(root, ledger)
    try:
        return ledger.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None


def _write_prime_files(
    root: Path,
    files: list[PrimeDesiredFile],
    *,
    dry_run: bool = False,
    expected_actions: tuple[PrimeAction, ...] | None = None,
) -> tuple[list[Path], tuple[PrimeAction, ...]]:
    """Preflight the entire batch before writing; preserve stale and edited files.

    The ledger only grants update authority over files this adapter created.
    Identical unowned files are collisions too. No settings, tokens or context
    files belong to this adapter. Live writes are serialized by the outer file lock.
    """
    ledger = validated_output_path(root, Path(".ai-config/prime-ownership.json"))
    _check_ancestors(root, ledger)
    owned: dict[str, dict[str, str]] = {}
    observed = read_prime_ownership(root)
    if observed is not None:
        payload = json.loads(observed)
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
    actions: list[PrimeAction] = []
    for file in files:
        source = file.source_plugin
        if not source:
            raise ValueError("Prime writes require source identity")
        parts = file.relative_path.parts
        if not (parts[:3] == (".prime", "agent", "skills") and len(parts) >= 5) or ".." in parts:
            raise ValueError(f"Prime adapter cannot own this path: {file.relative_path}")
        destination = validated_output_path(root, file.relative_path)
        _check_ancestors(root, destination)
        if any(
            destination == prior or destination in prior.parents or prior in destination.parents
            for prior in seen
        ):
            raise ValueError(f"Conflicting Prime output paths: {file.relative_path}")
        seen.add(destination)
        skill_directory = validated_output_path(root, Path(*parts[:4]))
        if (skill_directory / "pyproject.toml").exists() or (
            skill_directory / "pyproject.toml"
        ).is_symlink():
            raise ValueError(
                f"Prime Markdown destination contains Python package metadata: {skill_directory}"
            )
        if file.relative_path.name == "pyproject.toml" and len(parts) == 5:
            raise ValueError("Prime adapter cannot emit Python skill package metadata")
        key = file.relative_path.as_posix()
        data = file.content
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
        if not destination.exists():
            action = PrimeAction(
                "create_prime_output", file.relative_path, f"Create owned Prime output for {source}"
            )
        elif destination.read_bytes() == data:
            action = PrimeAction(
                "noop_prime_output",
                file.relative_path,
                f"Owned Prime output is unchanged for {source}",
            )
        else:
            action = PrimeAction(
                "update_prime_output", file.relative_path, f"Update owned Prime output for {source}"
            )
        actions.append(action)
        next_owned[key] = {"source": source, "sha256": hashlib.sha256(data).hexdigest()}
        prepared.append((destination, data, file.executable))
    if expected_actions is not None and tuple(actions) != expected_actions:
        raise ValueError("Prime output state changed after planning; no Prime output was written")
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
    return [destination for destination, _data, _executable in prepared], tuple(actions)


def plan_prime_files(root: Path, files: list[PrimeDesiredFile]) -> tuple[PrimeAction, ...]:
    """Inspect the exact create/update/noop intent without output or ledger mutation."""
    return _write_prime_files(root, files, dry_run=True)[1]


def write_prime_files(
    root: Path,
    files: list[PrimeDesiredFile],
    *,
    dry_run: bool = False,
    expected_actions: tuple[PrimeAction, ...] | None = None,
) -> list[Path]:
    """Serialize live ownership updates on POSIX; dry runs create no state."""
    if dry_run or not files:
        return _write_prime_files(root, files, dry_run=dry_run, expected_actions=expected_actions)[
            0
        ]
    try:
        import fcntl
    except ImportError as error:
        raise ValueError("Prime ownership writes currently require POSIX file locking") from error
    # Preflight before creating even the lock directory; repeat under the lock.
    _write_prime_files(root, files, dry_run=True, expected_actions=expected_actions)
    lock = validated_output_path(root, Path(".ai-config/prime-ownership.lock"))
    if lock.exists() and not lock.is_file():
        raise ValueError("Prime ownership lock is not a regular file")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            return _write_prime_files(root, files, expected_actions=expected_actions)[0]
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
