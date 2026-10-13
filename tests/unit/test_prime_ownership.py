"""Owned resource publication preserves outside inodes and recoverable state."""

import os
from pathlib import Path

import pytest

from ai_config.prime_ownership import PrimeDesiredFile, plan_prime_files, write_prime_files


def desired(content=b"old", executable=False):
    return [
        PrimeDesiredFile(
            "sync:test", Path(".prime/agent/skills/demo/SKILL.md"), content, executable
        )
    ]


def test_hardlink_update_preserves_other_name(tmp_path):
    destination = write_prime_files(tmp_path, desired())[0]
    outside = tmp_path / "unrelated"
    os.link(destination, outside)
    write_prime_files(tmp_path, desired(b"new", True))
    assert outside.read_bytes() == b"old"
    assert outside.stat().st_mode & 0o111 == 0
    assert destination.read_bytes() == b"new"
    assert destination.stat().st_ino != outside.stat().st_ino


def test_mode_only_update_and_repeat(tmp_path):
    destination = write_prime_files(tmp_path, desired(executable=True))[0]
    assert plan_prime_files(tmp_path, desired())[0].action == "update_prime_output"
    write_prime_files(tmp_path, desired())
    assert destination.stat().st_mode & 0o111 == 0
    before = destination.stat()
    assert plan_prime_files(tmp_path, desired())[0].action == "noop_prime_output"
    write_prime_files(tmp_path, desired())
    assert destination.stat().st_mtime_ns == before.st_mtime_ns
    assert destination.stat().st_ino == before.st_ino


@pytest.mark.parametrize("failure", ["resource", "ledger"])
def test_late_publish_failure_rolls_back_and_retry_works(tmp_path, monkeypatch, failure):
    files = desired() + [
        PrimeDesiredFile("sync:test", Path(".prime/agent/skills/demo/other.md"), b"old2")
    ]
    write_prime_files(tmp_path, files)
    before = {p: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.rglob("*") if p.is_file()}
    updated = desired(b"new", True) + [
        PrimeDesiredFile("sync:test", files[1].relative_path, b"new2")
    ]
    replace = os.replace
    target = (
        files[1].relative_path if failure == "resource" else Path(".ai-config/prime-ownership.json")
    )
    triggered = False

    def fail_once(source, destination):
        nonlocal triggered
        if Path(destination) == tmp_path / target and not triggered:
            triggered = True
            raise OSError("injected publication failure")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_once)
        with pytest.raises(OSError, match="injected"):
            write_prime_files(tmp_path, updated)
    assert triggered
    assert before == {
        p: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.rglob("*") if p.is_file()
    }
    write_prime_files(tmp_path, updated)
    assert all(
        action.action == "noop_prime_output" for action in plan_prime_files(tmp_path, updated)
    )


def test_staging_failure_publishes_nothing(tmp_path, monkeypatch):
    destination = write_prime_files(tmp_path, desired())[0]
    ledger = tmp_path / ".ai-config/prime-ownership.json"
    before = destination.read_bytes(), ledger.read_bytes()
    fsync = os.fsync
    calls = 0

    def fail_later(descriptor):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected staging failure")
        return fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_later)
        with pytest.raises(OSError, match="staging"):
            write_prime_files(tmp_path, desired(b"new"))
    assert (destination.read_bytes(), ledger.read_bytes()) == before
    assert not list(tmp_path.rglob(".prime-write-*"))


def test_fresh_publish_failure_removes_partial_resources(tmp_path, monkeypatch):
    replace = os.replace

    def fail_ledger(source, destination):
        if Path(destination).name == "prime-ownership.json":
            raise OSError("injected ledger failure")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_ledger)
        with pytest.raises(OSError, match="ledger"):
            write_prime_files(tmp_path, desired())
    assert not (tmp_path / desired()[0].relative_path).exists()
    assert not (tmp_path / ".ai-config/prime-ownership.json").exists()
    write_prime_files(tmp_path, desired())


@pytest.mark.parametrize("executable, expected", [(False, 0o600), (True, 0o711)])
def test_new_resources_preserve_restrictive_umask(tmp_path, executable, expected):
    previous = os.umask(0o077)
    try:
        destination = write_prime_files(tmp_path, desired(executable=executable))[0]
    finally:
        os.umask(previous)
    assert destination.stat().st_mode & 0o777 == expected
