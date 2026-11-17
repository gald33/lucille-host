"""Deciding and running one request whose envelope is already checked, and building its reply.

The refusal order is spec/PROTOCOL.md section 12. The read loop (``loop.py``) runs its first four
steps (dedupe, ``host_too_old``, ``bad_request``, ``expired``; ``envelope.py`` and
``journal.py``), which need the envelope, the journal and the clock. This module runs the other
five, which need the catalog, the local policy and the controls, in the spec's order:

5. ``paused``; 6. ``unknown_op``; 7. ``not_enabled``; 8. ``bad_args``; 9. ``path_outside_root``.

An operation the policy enables but this computer cannot run now (its executable gone since the
policy loaded) is ``not_enabled``, and ``host.describe`` leaves it out, as it would show it. A
folder whose real path cannot be opened is ``path_outside_root``: containment cannot be shown. So
is a path naming something other than a file or a folder, and a path into the host's own folders
(``containment.py``).

:func:`process` takes a request from there to the bytes of its reply, writing each audit line
before the act it records: ``received`` before deciding, ``decision`` before running or replying,
``start`` before the child starts, ``execution`` before the reply is built, and ``sent`` before the
bytes are handed back to be posted. A refusal the read loop already decided is passed in as
``earlier_code``. The read loop's ``decided`` hook is called right after the ``decision`` line and
before anything runs, with the decision and, for a refusal, the reply's bytes, so that its journal
holds them before the host acts. The context's ``observe`` hook, when there is one, is told each
step right after the audit line that records it (``received``, ``decided``, ``started`` and
``finished``): ``run --json-events`` writes its ``request`` objects from it (``loop.py``). It hands
them on without waiting for any reader, so it holds up neither the decision nor the start.

While the host drains (the first SIGTERM, SIGHUP or Ctrl-C), :func:`process` starts nothing: it
raises :class:`Draining` before it records anything, and the read loop stops reading. The pause flag
and the drain are read again just before the child starts, after the ``start`` line's executable is
hashed: a pause or a drain that lands after the decision to run stops the start, and the reply is
``failed interrupted``, as for a child that could not be started. One that lands after that re-read
cannot stop the start; ``kill`` stops the child from its first poll, and a drain lets it finish.

A request's room can be abandoned while it is decided or runs (spec/PROTOCOL.md section 9): the
read loop sets the context's ``stop``. A child not started yet is not started, and a running one has
its process group killed; either way :func:`process` raises :class:`Abandoned` after the
``execution`` line, with no ``sent`` line and no reply, and a user's kill request is left as it was.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from lucille_host import __version__, _exec, _output
from lucille_host.audit import AuditLog
from lucille_host.containment import contain
from lucille_host.controls import Controls
from lucille_host.ops import (
    CATALOG_VERSION,
    OPERATIONS,
    Call,
    Operation,
    Result,
    Stopped,
    TimedOut,
    args_valid,
)
from lucille_host.policy import Effective

__all__ = [
    "PROTOCOL",
    "Abandoned",
    "Context",
    "Draining",
    "Job",
    "Outcome",
    "Ready",
    "check",
    "describe_document",
    "encode",
    "execute",
    "process",
    "reply_body",
]

PROTOCOL: Final = "lucille.exec/1"


@dataclass(frozen=True)
class Job:
    """A request whose envelope the read loop has checked."""

    job_id: str
    op: str
    args: Any
    caps: Mapping[str, int] | None = None
    seq: int | None = None


@dataclass(frozen=True)
class Context:
    """What deciding and running a job reads."""

    os_family: str
    policy: Effective
    policy_digest: str
    controls: Controls
    audit: AuditLog
    # The host's own folders (paths.Dirs.own), which no path may reach. Required, so that no
    # caller can leave them out by accident.
    own_dirs: tuple[str, ...]
    stdout_cap: int = _output.STREAM_CAP
    stderr_cap: int = _output.STREAM_CAP
    # True once the request's room is abandoned: nothing more starts, a running child is killed,
    # and no reply is built.
    stop: Callable[[], bool] | None = None
    # Told each step of the request, right after its audit line: ("received"), ("decided",
    # decision, code), ("started") and ("finished", outcome, reply size).
    observe: Callable[..., None] | None = None


@dataclass(frozen=True)
class Ready:
    """A job that passed every check: what to run, where, and for how long."""

    job: Job
    operation: Operation
    argv: tuple[str, ...] | None
    cwd: str | None
    timeout_s: int


@dataclass(frozen=True)
class Outcome:
    """What the reply reports, before the envelope fields are added."""

    status: str
    code: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stdout_truncated: bool | None = None
    stderr: str | None = None
    stderr_truncated: bool | None = None
    duration_ms: int | None = None


class Draining(Exception):
    """The host is draining: it starts nothing new, and records nothing for a request it got."""


class Abandoned(Exception):
    """The request's room was abandoned: what it was to run did not start, or was killed, and
    no reply is built."""


class _StoppedBeforeStart(Exception):
    """The pause flag was set, or the drain began, between the decision to run and the start."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check(job: Job, ctx: Context) -> str | Ready:
    """Steps 5 to 9 of the refusal order: a refusal code, or what to run."""
    if ctx.controls.paused():
        return "paused"
    operation = OPERATIONS.get(job.op)
    if operation is None:
        return "unknown_op"
    if job.op not in ctx.policy.enabled_ops or not operation.available(ctx.os_family):
        return "not_enabled"
    if not args_valid(operation, job.args):
        return "bad_args"
    cwd: str | None = None
    if operation.uses_root:
        root = ctx.policy.roots.get(job.args["root"])
        if root is None:
            return "path_outside_root"
        cwd = contain(root.path, job.args["path"], own=ctx.own_dirs)
        if cwd is None:
            return "path_outside_root"
    argv = operation.build_argv(ctx.os_family, job.args) if operation.runs_child else None
    if argv is not None and cwd is None:
        # An operation with no root runs in the filesystem root, never in the user's folders.
        cwd = "/"
    return Ready(
        job=job,
        operation=operation,
        argv=argv,
        cwd=cwd,
        timeout_s=operation.timeout(job.args),
    )


def describe_document(ctx: Context) -> dict[str, Any]:
    """What ``host.describe`` returns (spec/PROTOCOL.md section 11): never a path."""
    return {
        "host_version": __version__,
        "protocol": PROTOCOL,
        "os_family": ctx.os_family,
        "catalog_version": CATALOG_VERSION,
        "enabled_ops": [
            name for name in ctx.policy.enabled_ops if OPERATIONS[name].available(ctx.os_family)
        ],
        "root_aliases": sorted(ctx.policy.roots),
        "policy_digest": ctx.policy_digest,
    }


def _caps(job: Job, ctx: Context) -> tuple[int, int]:
    caps = job.caps or {}
    stdout_cap = min(caps.get("stdout_bytes", ctx.stdout_cap), ctx.stdout_cap)
    stderr_cap = min(caps.get("stderr_bytes", ctx.stderr_cap), ctx.stderr_cap)
    return stdout_cap, stderr_cap


def _never() -> bool:
    return False


def execute(ready: Ready, ctx: Context) -> Outcome:
    """Run a job that passed every check.

    Writes the ``start`` audit line before the child starts and the ``execution`` line before
    returning. A child the host decided on and then did not start is ``failed interrupted``: the
    host did not carry out its decision. That is a child that could not be started (its executable
    or its folder went away in between, say), or one a pause or a drain that landed after the
    decision stopped. Raises :class:`Abandoned` when the room was abandoned before the start or
    while the child ran.
    """
    job_id = ready.job.job_id
    stop = ctx.stop if ctx.stop is not None else _never
    if ready.operation.in_process is not None:
        return _call_in_process(ready, ctx, stop)
    if ready.argv is None:
        document = json.dumps(describe_document(ctx), ensure_ascii=True, separators=(",", ":"))
        ctx.audit.execution(job_id, op=ready.operation.name, child=False, stdout_size=len(document))
        return Outcome(
            status="done",
            exit_code=0,
            stdout=document,
            stdout_truncated=False,
            stderr="",
            stderr_truncated=False,
            duration_ms=0,
        )
    assert ready.cwd is not None
    stdout_cap, stderr_cap = _caps(ready.job, ctx)
    command = _exec.Command(
        argv=ready.argv,
        cwd=ready.cwd,
        timeout_s=ready.timeout_s,
        stdout_cap=stdout_cap,
        stderr_cap=stderr_cap,
    )

    def started(executable_sha256: str) -> None:
        if stop():
            raise _StoppedBeforeStart("abandoned")
        if ctx.controls.draining.is_set():
            raise _StoppedBeforeStart("draining")
        if ctx.controls.paused():
            raise _StoppedBeforeStart("paused")
        ctx.audit.start(
            job_id,
            op=ready.operation.name,
            executable=command.argv[0],
            executable_sha256=executable_sha256,
            argv=list(command.argv),
        )
        if ctx.observe is not None:
            ctx.observe("started")

    def kill_requested() -> bool:
        return ctx.controls.kill_requested() or stop()

    try:
        result = _exec.run(command, kill_requested=kill_requested, before_start=started)
    except _exec.NotStarted as exc:
        ctx.audit.execution(job_id, op=ready.operation.name, started=False, errno=exc.errno)
        return Outcome(status="failed", code="interrupted", duration_ms=0)
    except _StoppedBeforeStart as stopped:
        ctx.audit.execution(job_id, op=ready.operation.name, started=False, stopped=stopped.reason)
        if stopped.reason == "abandoned":
            raise Abandoned from None
        return Outcome(status="failed", code="interrupted", duration_ms=0)
    # Killed because the room was abandoned: no reply, and a user's kill request stays as it was.
    abandoned = result.ended == "killed" and stop()
    if result.ended == "killed" and not abandoned:
        ctx.controls.kill_done()
    fields: dict[str, Any] = {
        "op": ready.operation.name,
        "child": True,
        "executable": ready.argv[0],
        "executable_sha256": result.executable_sha256,
        "argv": list(ready.argv),
        "ended": result.ended,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
    }
    if abandoned:
        fields["abandoned"] = True
    if ctx.policy.log_results:
        fields["stdout"] = result.stdout
        fields["stderr"] = result.stderr
    ctx.audit.execution(job_id, **fields)
    if abandoned:
        raise Abandoned
    streams = {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stdout_truncated": result.stdout_truncated,
        "stderr": result.stderr,
        "stderr_truncated": result.stderr_truncated,
        "duration_ms": result.duration_ms,
    }
    if result.ended == "exited":
        return Outcome(status="done", **streams)
    code = "timeout" if result.ended == "timeout" else "killed_locally"
    return Outcome(status="failed", code=code, **streams)


def _well_formed(returned: object) -> bool:
    """A Result with an exit code a reply can carry (an in-process call is never a signal's) and
    two strings."""
    return (
        isinstance(returned, Result)
        and type(returned.exit_code) is int
        and 0 <= returned.exit_code <= 255
        and type(returned.stdout) is str
        and type(returned.stderr) is str
    )


def _call_in_process(ready: Ready, ctx: Context, stop: Callable[[], bool]) -> Outcome:
    """Run an in-process entry under a child's rules (see :func:`execute`).

    The pause flag, the drain and the room are read just before the call, as before a child's
    start. The ``start`` line names the function and the entry's definition hash in place of an
    executable and its argv. A kill or an abandoned room reaches the function through its call's
    ``stop``; the host cannot stop a function that does not poll it. A function that raises
    anything else gets a ``done`` reply with exit code 1 and a stderr naming the exception's type
    only: its message can hold anything the function held, so it goes neither in the reply nor in
    the audit log.
    """
    job_id = ready.job.job_id
    operation = ready.operation
    entry = operation.in_process
    assert entry is not None
    reason = (
        "abandoned"
        if stop()
        else "draining"
        if ctx.controls.draining.is_set()
        else "paused"
        if ctx.controls.paused()
        else None
    )
    if reason is not None:
        ctx.audit.execution(job_id, op=operation.name, started=False, stopped=reason)
        if reason == "abandoned":
            raise Abandoned
        return Outcome(status="failed", code="interrupted", duration_ms=0)
    ctx.audit.start(
        job_id,
        op=operation.name,
        in_process=entry.name,
        definition_hash=operation.definition_hash,
    )
    if ctx.observe is not None:
        ctx.observe("started")

    def kill_requested() -> bool:
        return ctx.controls.kill_requested() or stop()

    began = time.monotonic()
    call = Call(
        args=MappingProxyType(dict(ready.job.args)),
        timeout_s=ready.timeout_s,
        deadline=began + ready.timeout_s,
        stop=kill_requested,
    )
    result = Result()
    raised: str | None = None
    try:
        returned = entry.function(call)
        if _well_formed(returned):
            result, ended = returned, "exited"
        else:
            ended, raised = "raised", "TypeError"
    except TimedOut:
        ended = "timeout"
    except Stopped:
        ended = "killed"
    except Exception as exc:
        ended, raised = "raised", type(exc).__name__
    duration_ms = int((time.monotonic() - began) * 1000)
    if raised is not None:
        result = Result(exit_code=1, stderr=f"the operation raised {raised}")
    abandoned = ended == "killed" and stop()
    if ended == "killed" and not abandoned:
        ctx.controls.kill_done()
    stdout_raw = result.stdout.encode("utf-8", "replace")
    stderr_raw = result.stderr.encode("utf-8", "replace")
    fields: dict[str, Any] = {
        "op": operation.name,
        "child": False,
        "in_process": entry.name,
        "ended": ended,
        "exit_code": result.exit_code if ended in ("exited", "raised") else None,
        "duration_ms": duration_ms,
        "stdout_bytes": len(stdout_raw),
        "stderr_bytes": len(stderr_raw),
    }
    if raised is not None:
        fields["raised"] = raised
    if abandoned:
        fields["abandoned"] = True
    if ctx.policy.log_results:
        fields["stdout"] = result.stdout
        fields["stderr"] = result.stderr
    ctx.audit.execution(job_id, **fields)
    if abandoned:
        raise Abandoned
    if ended == "timeout":
        return Outcome(status="failed", code="timeout", duration_ms=duration_ms)
    if ended == "killed":
        return Outcome(status="failed", code="killed_locally", duration_ms=duration_ms)
    stdout_cap, stderr_cap = _caps(ready.job, ctx)
    stdout, stdout_truncated = _output.render(
        stdout_raw[: _output.RAW_CEILING],
        stdout_cap,
        dropped=len(stdout_raw) > _output.RAW_CEILING,
    )
    stderr, stderr_truncated = _output.render(
        stderr_raw[: _output.RAW_CEILING],
        stderr_cap,
        dropped=len(stderr_raw) > _output.RAW_CEILING,
    )
    return Outcome(
        status="done",
        exit_code=result.exit_code,
        stdout=stdout,
        stdout_truncated=stdout_truncated,
        stderr=stderr,
        stderr_truncated=stderr_truncated,
        duration_ms=duration_ms,
    )


_FIELD_ORDER: Final = (
    "code",
    "exit_code",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
    "duration_ms",
)


def reply_body(job_id: str, outcome: Outcome, policy_digest: str) -> dict[str, Any]:
    """The reply envelope (spec/PROTOCOL.md section 11)."""
    body: dict[str, Any] = {
        "v": PROTOCOL,
        "dir": "reply",
        "job_id": job_id,
        "status": outcome.status,
    }
    for name in _FIELD_ORDER:
        value = getattr(outcome, name)
        if value is not None:
            body[name] = value
    body["policy_digest"] = policy_digest
    body["host_version"] = __version__
    return body


def encode(body: Mapping[str, Any]) -> bytes:
    """The exact bytes of a reply body, as posted."""
    return json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def process(
    job: Job,
    ctx: Context,
    *,
    earlier_code: str | None = None,
    decided: Callable[[str, str | None, bytes | None], None] | None = None,
) -> bytes:
    """Decide one request, run it if it passes, and return the bytes of its reply.

    ``decided(decision, code, data)`` is called right after the ``decision`` line: ``("refused",
    code, the reply's bytes)`` or ``("run", None, None)``, before anything runs. Raises
    :class:`Draining`, having recorded nothing, while the host drains, and :class:`Abandoned`
    when the request's room was abandoned before its reply was built.
    """
    if ctx.controls.draining.is_set():
        raise Draining
    observe = ctx.observe if ctx.observe is not None else _ignore
    ctx.audit.received(job.job_id, job.seq, job.op, job.args)
    observe("received")
    verdict = earlier_code if earlier_code is not None else check(job, ctx)
    if isinstance(verdict, str):
        ctx.audit.decision(job.job_id, "refused", verdict)
        outcome = Outcome(status="refused", code=verdict)
        data = encode(reply_body(job.job_id, outcome, ctx.policy_digest))
        if decided is not None:
            decided("refused", verdict, data)
        observe("decided", "refused", verdict)
    else:
        ctx.audit.decision(job.job_id, "run")
        if decided is not None:
            decided("run", None, None)
        observe("decided", "run", None)
        outcome = execute(verdict, ctx)
        data = encode(reply_body(job.job_id, outcome, ctx.policy_digest))
    ctx.audit.sent(job.job_id, hashlib.sha256(data).hexdigest(), len(data))
    observe("finished", outcome, len(data))
    return data


def _ignore(*_: Any) -> None:
    """No one observes the request."""
