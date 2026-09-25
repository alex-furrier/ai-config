"""Fail-closed descriptor-contained reads for files below a plugin source root."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class SourceSafetyError(ValueError):
    """A source path is malformed, escapes its root, or is unsafe to read."""


class SourceMissingError(SourceSafetyError):
    """A syntactically safe source path does not exist."""


@dataclass(frozen=True)
class SourceFile:
    """Bytes and mode read from one validated regular source file."""

    relative_path: PurePosixPath
    content: bytes
    executable: bool


@dataclass(frozen=True)
class SourceContextMirror:
    """One validated repository context mirror kept as symlink metadata."""

    relative_path: PurePosixPath
    target: str


def _validate_platform_path(value: str, *, context: str) -> None:
    """Reject values the host cannot safely pass to filesystem APIs."""
    if "\0" in value:
        raise SourceSafetyError(f"{context} path contains an embedded NUL: {value!r}")
    try:
        os.fsencode(value)
    except (UnicodeError, ValueError) as error:
        raise SourceSafetyError(f"{context} path is invalid on this platform: {value!r}") from error
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        invalid = '<>:"|?*'
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{number}" for number in range(1, 10)),
            *(f"LPT{number}" for number in range(1, 10)),
        }
        for part in value.replace("\\", "/").split("/"):
            if any(character in part for character in invalid) or part.endswith((" ", ".")):
                raise SourceSafetyError(f"{context} path is invalid on this platform: {value!r}")
            if part.split(".", 1)[0].upper() in reserved:
                raise SourceSafetyError(f"{context} path is invalid on this platform: {value!r}")


def normalize_source_relative(value: object, *, context: str) -> PurePosixPath:
    """Validate one portable plugin-root-relative path value."""
    if not isinstance(value, str):
        raise SourceSafetyError(f"{context} path must be a string")
    _validate_platform_path(value, context=context)
    if not value or "\\" in value:
        raise SourceSafetyError(
            f"{context} path must be a non-empty POSIX relative path: {value!r}"
        )
    while value.startswith("./"):
        value = value[2:]
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or path == PurePosixPath(".") or ".." in raw_parts:
        raise SourceSafetyError(f"{context} path must stay within the plugin root: {value!r}")
    if any(part in {"", "."} for part in raw_parts):
        raise SourceSafetyError(f"{context} path contains an empty or dot component: {value!r}")
    return path


_OPEN_BASE = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
_OPEN_DIRECTORY = _OPEN_BASE | getattr(os, "O_DIRECTORY", 0)
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_SECURE_DESCRIPTOR_APIS = (
    os.name == "posix"
    and _OPEN_NOFOLLOW != 0
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
)


class ContainedSource:
    """Read source entries through a retained root descriptor without following links.

    POSIX traversal starts at ``/`` and opens every lexical root and source component
    relative to its already-open parent with ``O_NOFOLLOW``. Platforms without the
    required descriptor APIs fail closed rather than falling back to pathname reads.
    """

    def __init__(
        self, root: Path, *, excluded_directory_names: frozenset[str] = frozenset()
    ) -> None:
        self.excluded_directory_names = excluded_directory_names
        try:
            supplied = root.expanduser().absolute()
            _validate_platform_path(os.fspath(supplied), context="Plugin source root")
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            if isinstance(error, SourceSafetyError):
                raise
            raise SourceSafetyError(f"Plugin source root is invalid: {root!s}: {error}") from error
        if not _SECURE_DESCRIPTOR_APIS:
            raise SourceSafetyError(
                "Secure descriptor-relative source reads are unavailable on this platform"
            )
        self.root = supplied
        self._root_fd: int | None = None
        self._root_fd = self._open_absolute_directory(supplied)

    def close(self) -> None:
        """Close the retained root descriptor; repeated calls are harmless."""
        root_fd = getattr(self, "_root_fd", None)
        if root_fd is None:
            return
        self._root_fd = None
        try:
            os.close(root_fd)
        except OSError:
            pass

    def __enter__(self) -> ContainedSource:
        if self._root_fd is None:
            raise SourceSafetyError("Plugin source is closed")
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        """Best-effort fallback for callers that did not close deterministically."""
        self.close()

    @staticmethod
    def _raise_open_error(error: OSError, *, context: str, relative: PurePosixPath) -> None:
        display = relative.as_posix()
        if error.errno == errno.ENOENT:
            raise SourceMissingError(f"{context} path does not exist: {display}") from error
        if error.errno in {errno.ELOOP, errno.EMLINK}:
            raise SourceSafetyError(f"{context} path contains a symlink: {display}") from error
        if error.errno == errno.ENOTDIR:
            raise SourceSafetyError(
                f"{context} path has a symlink or non-directory ancestor: {display}"
            ) from error
        raise SourceSafetyError(f"{context} path is unreadable: {display}: {error}") from error

    def _open_absolute_directory(self, root: Path) -> int:
        current = os.open(os.path.sep, _OPEN_DIRECTORY)
        traversed = PurePosixPath(".")
        try:
            for part in root.parts[1:]:
                traversed /= part
                try:
                    child = os.open(part, _OPEN_DIRECTORY | _OPEN_NOFOLLOW, dir_fd=current)
                except OSError as error:
                    self._raise_open_error(error, context="Plugin source root", relative=traversed)
                os.close(current)
                current = child
            root_stat = os.fstat(current)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise SourceSafetyError(f"Plugin source root is not a directory: {root}")
            return current
        except Exception:
            os.close(current)
            raise

    def relative(self, value: object, *, context: str) -> PurePosixPath:
        return normalize_source_relative(value, context=context)

    def _validated_relative(self, relative: PurePosixPath, *, context: str) -> PurePosixPath:
        """Validate direct public path arguments inside the source authority."""
        if not isinstance(relative, PurePosixPath):
            raise SourceSafetyError(f"{context} path must be a POSIX relative path")
        return normalize_source_relative(relative.as_posix(), context=context)

    def _root_descriptor(self) -> int:
        root_fd = self._root_fd
        if root_fd is None:
            raise SourceSafetyError("Plugin source is closed")
        return root_fd

    def _open_entry(self, relative: PurePosixPath, *, context: str, directory: bool = False) -> int:
        relative = self._validated_relative(relative, context=context)
        current = os.dup(self._root_descriptor())
        try:
            for index, part in enumerate(relative.parts):
                final = index == len(relative.parts) - 1
                flags = _OPEN_BASE | _OPEN_NOFOLLOW
                if not final or directory:
                    flags |= getattr(os, "O_DIRECTORY", 0)
                try:
                    child = os.open(part, flags, dir_fd=current)
                except OSError as error:
                    self._raise_open_error(error, context=context, relative=relative)
                if part in self.excluded_directory_names and stat.S_ISDIR(os.fstat(child).st_mode):
                    os.close(child)
                    raise SourceSafetyError(
                        f"{context} path is excluded generated content: {relative}"
                    )
                os.close(current)
                current = child
            return current
        except Exception:
            os.close(current)
            raise

    def kind(self, relative: PurePosixPath, *, context: str) -> str:
        """Return ``file`` or ``directory`` from an opened, no-follow descriptor."""
        relative = self._validated_relative(relative, context=context)
        fd = self._open_entry(relative, context=context)
        try:
            item_stat = os.fstat(fd)
        finally:
            os.close(fd)
        if stat.S_ISREG(item_stat.st_mode):
            return "file"
        if stat.S_ISDIR(item_stat.st_mode):
            return "directory"
        raise SourceSafetyError(f"{context} path is not a regular file or directory: {relative}")

    def read_file(
        self,
        relative: PurePosixPath,
        *,
        context: str,
        reject_hardlinks: bool = False,
    ) -> SourceFile:
        """Read one regular file from a no-follow descriptor and verify its snapshot."""
        relative = self._validated_relative(relative, context=context)
        fd = self._open_entry(relative, context=context)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise SourceSafetyError(f"{context} path is not a regular file: {relative}")
            if reject_hardlinks and before.st_nlink != 1:
                raise SourceSafetyError(f"{context} path must not be a hardlink: {relative}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(fd)
        except OSError as error:
            raise SourceSafetyError(f"{context} path is unreadable: {relative}: {error}") from error
        finally:
            os.close(fd)

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if identity(before) != identity(after):
            raise SourceSafetyError(f"{context} path changed while being read: {relative}")
        content = b"".join(chunks)
        if len(content) != after.st_size:
            raise SourceSafetyError(f"{context} path changed while being read: {relative}")
        return SourceFile(relative, content, bool(after.st_mode & 0o111))

    def _read_context_mirror(
        self,
        directory_fd: int,
        name: str,
        relative: PurePosixPath,
        *,
        context: str,
    ) -> SourceContextMirror:
        """Validate the repository's exact ``CLAUDE.md -> AGENTS.md`` mirror."""
        if name != "CLAUDE.md":
            raise SourceSafetyError(f"{context} contains a symlink: {relative}")
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            target = os.readlink(name, dir_fd=directory_fd)
            if target != "AGENTS.md":
                raise SourceSafetyError(
                    f"{context} context mirror must target sibling AGENTS.md: {relative}"
                )
            target_fd = os.open(target, _OPEN_BASE | _OPEN_NOFOLLOW, dir_fd=directory_fd)
            try:
                target_stat = os.fstat(target_fd)
                if not stat.S_ISREG(target_stat.st_mode):
                    raise SourceSafetyError(
                        f"{context} context mirror target is not a regular file: {relative}"
                    )
            finally:
                os.close(target_fd)
            after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            after_target = os.readlink(name, dir_fd=directory_fd)
        except OSError as error:
            raise SourceSafetyError(
                f"{context} context mirror is unreadable: {relative}: {error}"
            ) from error

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if identity(before) != identity(after) or target != after_target:
            raise SourceSafetyError(
                f"{context} context mirror changed while being read: {relative}"
            )
        return SourceContextMirror(relative, target)

    def _walk_directory(
        self,
        directory_fd: int,
        directory: PurePosixPath,
        *,
        context: str,
        isolate_errors: bool,
        files: list[PurePosixPath],
        errors: list[str],
        context_mirrors: list[SourceContextMirror] | None = None,
        allow_context_mirrors: bool = False,
        excluded_directory_names: frozenset[str] = frozenset(),
        excluded_root_directory_names: frozenset[str] = frozenset(),
    ) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as error:
            message = f"{context} directory is unreadable: {directory}: {error}"
            if isolate_errors:
                errors.append(message)
                return
            raise SourceSafetyError(message) from error
        for name in names:
            child = directory / name
            try:
                item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(item_stat.st_mode) and (
                    name in excluded_directory_names
                    or (directory == PurePosixPath(".") and name in excluded_root_directory_names)
                ):
                    continue
                if stat.S_ISLNK(item_stat.st_mode):
                    if not allow_context_mirrors or context_mirrors is None:
                        raise SourceSafetyError(f"{context} contains a symlink: {child}")
                    context_mirrors.append(
                        self._read_context_mirror(
                            directory_fd,
                            name,
                            child,
                            context=context,
                        )
                    )
                    continue
                if stat.S_ISDIR(item_stat.st_mode):
                    child_fd = os.open(name, _OPEN_DIRECTORY | _OPEN_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        self._walk_directory(
                            child_fd,
                            child,
                            context=context,
                            isolate_errors=isolate_errors,
                            files=files,
                            errors=errors,
                            context_mirrors=context_mirrors,
                            allow_context_mirrors=allow_context_mirrors,
                            excluded_directory_names=excluded_directory_names,
                            excluded_root_directory_names=excluded_root_directory_names,
                        )
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(item_stat.st_mode):
                    # Open now to ensure the scanned name still identifies a regular no-follow file.
                    file_fd = os.open(name, _OPEN_BASE | _OPEN_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                            raise SourceSafetyError(
                                f"{context} contains a non-regular file: {child}"
                            )
                    finally:
                        os.close(file_fd)
                    files.append(child)
                else:
                    raise SourceSafetyError(f"{context} contains a non-regular file: {child}")
            except (OSError, SourceSafetyError) as error:
                if isinstance(error, OSError):
                    if error.errno in {errno.ELOOP, errno.EMLINK}:
                        message = f"{context} contains a symlink: {child}"
                    else:
                        message = f"{context} entry is unreadable: {child}: {error}"
                else:
                    message = str(error)
                if isolate_errors:
                    errors.append(message)
                    continue
                raise SourceSafetyError(message) from error

    def walk_files(
        self,
        relative: PurePosixPath,
        *,
        context: str,
        excluded_directory_names: frozenset[str] = frozenset(),
    ) -> Iterator[PurePosixPath]:
        """Yield regular files below a descriptor-opened directory."""
        relative = self._validated_relative(relative, context=context)
        directory_fd = self._open_entry(relative, context=context, directory=True)
        files: list[PurePosixPath] = []
        try:
            self._walk_directory(
                directory_fd,
                relative,
                context=context,
                isolate_errors=False,
                files=files,
                errors=[],
                excluded_directory_names=excluded_directory_names,
            )
        finally:
            os.close(directory_fd)
        yield from files

    def scan_files(
        self,
        relative: PurePosixPath,
        *,
        context: str,
        excluded_directory_names: frozenset[str] = frozenset(),
    ) -> tuple[list[PurePosixPath], list[str]]:
        """Collect safe files while isolating unsafe sibling entries."""
        relative = self._validated_relative(relative, context=context)
        directory_fd = self._open_entry(relative, context=context, directory=True)
        files: list[PurePosixPath] = []
        errors: list[str] = []
        try:
            self._walk_directory(
                directory_fd,
                relative,
                context=context,
                isolate_errors=True,
                files=files,
                errors=errors,
                excluded_directory_names=excluded_directory_names,
            )
        finally:
            os.close(directory_fd)
        return files, errors

    def walk_all_files(self, *, context: str) -> Iterator[PurePosixPath]:
        """Yield every regular file below the retained source-root descriptor."""
        files: list[PurePosixPath] = []
        root_fd = os.dup(self._root_descriptor())
        try:
            self._walk_directory(
                root_fd,
                PurePosixPath("."),
                context=context,
                isolate_errors=False,
                files=files,
                errors=[],
            )
        finally:
            os.close(root_fd)
        yield from files

    def snapshot_files_and_context_mirrors(
        self,
        *,
        context: str,
        excluded_directory_names: frozenset[str] = frozenset(),
        excluded_root_directory_names: frozenset[str] = frozenset(),
    ) -> tuple[tuple[PurePosixPath, ...], tuple[SourceContextMirror, ...]]:
        """Collect regular files plus exact repository context mirror metadata."""
        files: list[PurePosixPath] = []
        mirrors: list[SourceContextMirror] = []
        root_fd = os.dup(self._root_descriptor())
        try:
            self._walk_directory(
                root_fd,
                PurePosixPath("."),
                context=context,
                isolate_errors=False,
                files=files,
                errors=[],
                context_mirrors=mirrors,
                allow_context_mirrors=True,
                excluded_directory_names=excluded_directory_names,
                excluded_root_directory_names=excluded_root_directory_names,
            )
        finally:
            os.close(root_fd)
        return tuple(files), tuple(mirrors)
