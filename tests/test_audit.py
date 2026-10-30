"""The audit log: each line written before the act it records, mode 0600, rotation."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from lucille_host import _exec, policy
from lucille_host.audit import AuditLog
from lucille_host.jobs import Job, Ready, check, execute, process
from tests._host import MakeContext


class Crash(Exception):
    """Stands for the host dying right after one audit line was written."""


class CrashAfter(AuditLog):
    """An audit log that writes a line of kind ``kind``, then crashes."""

    def __init__(self, state_dir: Path, kind: str) -> None:
        super().__init__(state_dir)
        self.kind = kind

    def write(self, kind: str, **fields: Any) -> None:
        super().write(kind, **fields)
        if kind == self.kind:
            raise Crash(kind)


def _lines(log: AuditLog) -> list[dict[str, Any]]:
    if not log.path.exists():
        return []
    return [json.loads(line) for line in log.path.read_text().splitlines()]


def _kinds(log: AuditLog) -> list[str]:
    return [line["kind"] for line in _lines(log)]


SLEEP = Job(job_id="a" * 8, op="host.sleep", args={"n": 1}, seq=41)


def test_audit_order(
    state_dir: Path, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[list[str]] = []
    real_run = _exec.run

    def spy(command: Any, **kwargs: Any) -> Any:
        spawned.append(_kinds(AuditLog(state_dir)))
        return real_run(command, **kwargs)

    monkeypatch.setattr("lucille_host._exec.run", spy)

    # A crash right after `received`: nothing is decided, nothing runs.
    ctx = make_context(enabled=["host.sleep"], audit=CrashAfter(state_dir, "received"))
    with pytest.raises(Crash):
        process(SLEEP, ctx)
    assert _kinds(ctx.audit) == ["received"] and spawned == []

    # A crash right after `decision`: the decision is written, and nothing ran.
    ctx.audit.path.unlink()
    ctx = make_context(enabled=["host.sleep"], audit=CrashAfter(state_dir, "decision"))
    with pytest.raises(Crash):
        process(SLEEP, ctx)
    assert _kinds(ctx.audit) == ["received", "decision"] and spawned == []

    # A crash right after `execution`: the run is recorded, and no reply exists to post.
    ctx.audit.path.unlink()
    ctx = make_context(enabled=["host.sleep"], audit=CrashAfter(state_dir, "execution"))
    with pytest.raises(Crash):
        process(SLEEP, ctx)
    assert _kinds(ctx.audit) == ["received", "decision", "start", "execution"]
    # The decision was written when the executor was called.
    assert spawned == [["received", "decision"]]

    # A crash right after `sent`: the reply's hash is recorded, and the bytes never left process().
    ctx.audit.path.unlink()
    ctx = make_context(enabled=["host.sleep"], audit=CrashAfter(state_dir, "sent"))
    with pytest.raises(Crash):
        process(SLEEP, ctx)
    assert _kinds(ctx.audit) == ["received", "decision", "start", "execution", "sent"]


def test_audit_start_line_is_written_before_the_child_starts(
    state_dir: Path, tmp_path: Path, make_context: MakeContext
) -> None:
    marker = tmp_path / "the-child-ran"
    code = f"open({str(marker)!r}, 'w').close()"

    def ready_writing_marker(audit: AuditLog) -> Ready:
        ctx = make_context(enabled=["host.sleep"], audit=audit)
        decided = check(SLEEP, ctx)
        assert isinstance(decided, Ready)
        return dataclasses.replace(decided, argv=(sys.executable, "-c", code))

    # A crash right after `start`: what would run is written, and nothing ran.
    crashing = CrashAfter(state_dir, "start")
    ready = ready_writing_marker(crashing)
    with pytest.raises(Crash):
        execute(ready, make_context(enabled=["host.sleep"], audit=crashing))
    start = _lines(crashing)[-1]
    assert start["kind"] == "start" and start["argv"] == [sys.executable, "-c", code]
    assert len(start["executable_sha256"]) == 64
    assert not marker.exists()

    # The same without the crash runs the child: the test can see a start.
    ready = ready_writing_marker(AuditLog(state_dir))
    assert execute(ready, make_context(enabled=["host.sleep"])).status == "done"
    assert marker.exists()


def test_audit_lines_record_what_the_design_names(
    state_dir: Path, make_context: MakeContext
) -> None:
    ctx = make_context(enabled=["host.sleep"])
    data = process(SLEEP, ctx)
    received, decision, start, execution, sent = _lines(ctx.audit)
    assert received["kind"] == "received"
    assert (received["job_id"], received["seq"], received["op"], received["args"]) == (
        SLEEP.job_id,
        41,
        "host.sleep",
        {"n": 1},
    )
    assert (decision["decision"], decision.get("code")) == ("run", None)
    assert start["kind"] == "start" and start["argv"] == ["/bin/sleep", "1"]
    assert start["executable_sha256"] == hashlib.sha256(Path("/bin/sleep").read_bytes()).hexdigest()
    assert execution["executable"] == "/bin/sleep"
    assert (
        execution["executable_sha256"]
        == hashlib.sha256(Path("/bin/sleep").read_bytes()).hexdigest()
    )
    assert execution["argv"] == ["/bin/sleep", "1"]
    assert (execution["ended"], execution["exit_code"]) == ("exited", 0)
    assert execution["stdout_bytes"] == 0 and "duration_ms" in execution
    assert "stdout" not in execution
    assert (sent["sha256"], sent["size"]) == (hashlib.sha256(data).hexdigest(), len(data))

    refused = Job(job_id="b" * 8, op="files.delete", args={})
    process(refused, ctx)
    assert _lines(ctx.audit)[-2]["decision"] == "refused"
    assert _lines(ctx.audit)[-2]["code"] == "unknown_op"
    assert [line["kind"] for line in _lines(ctx.audit)[-3:]] == ["received", "decision", "sent"]


def test_audit_keeps_output_bodies_only_with_log_results(
    state_dir: Path, make_context: MakeContext, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a")
    for log_results in (False, True):
        ctx = make_context(enabled=["files.sha256"], roots={"docs": root}, log_results=log_results)
        process(Job(job_id="c" * 8, op="files.sha256", args={"root": "docs", "path": "a.txt"}), ctx)
        execution = [line for line in _lines(ctx.audit) if line["kind"] == "execution"][-1]
        assert ("stdout" in execution) is log_results
        assert ("stderr" in execution) is log_results


def test_audit_file_is_0600_even_if_it_was_not(state_dir: Path) -> None:
    log = AuditLog(state_dir)
    log.path.write_text("")
    log.path.chmod(0o644)
    log.decision("d" * 8, "refused", "paused")
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600


def test_audit_rotates_by_size_and_never_edits(state_dir: Path) -> None:
    log = AuditLog(state_dir, max_bytes=400, keep=3)
    for index in range(40):
        log.decision(f"{index:08d}", "refused", "paused")
    files = sorted(p.name for p in state_dir.glob("audit.log*"))
    assert files == ["audit.log", "audit.log.1", "audit.log.2", "audit.log.3"]
    for path in state_dir.glob("audit.log*"):
        assert path.stat().st_size <= 400
    # Every surviving line is whole, and the newest lines are in audit.log.
    ids = []
    for name in ("audit.log.3", "audit.log.2", "audit.log.1", "audit.log"):
        ids += [json.loads(line)["job_id"] for line in (state_dir / name).read_text().splitlines()]
    assert ids == sorted(ids) and ids[-1] == f"{39:08d}"


def test_audit_never_holds_the_digest_key(state_dir: Path, make_context: MakeContext) -> None:
    ctx = make_context(enabled=["host.sleep"])
    process(SLEEP, ctx)
    key = policy.digest_key(state_dir)
    text = ctx.audit.path.read_bytes()
    assert key not in text and key.hex().encode() not in text


def test_each_audit_line_is_synced_before_the_act_it_records(
    state_dir: Path, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Written is not enough for a line to survive the computer stopping: it must be synced. Each
    sync of the log is recorded with how many lines the log then held."""
    synced: list[int] = []
    when_started: list[list[int]] = []

    class Recording(AuditLog):
        def start(self, job_id: str, **fields: Any) -> None:
            # _exec.run starts the child as soon as this returns.
            super().start(job_id, **fields)
            when_started.append(list(synced))

    ctx = make_context(enabled=["host.sleep"], audit=Recording(state_dir))
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        synced_file, log = os.fstat(descriptor), ctx.audit.path.stat()
        if (synced_file.st_dev, synced_file.st_ino) == (log.st_dev, log.st_ino):
            synced.append(len(ctx.audit.path.read_text().splitlines()))

    monkeypatch.setattr("os.fsync", fsync)
    process(SLEEP, ctx)
    # received, decision and start were each synced before the child started; every line was.
    assert when_started == [[1, 2, 3]]
    assert synced == [1, 2, 3, 4, 5]
