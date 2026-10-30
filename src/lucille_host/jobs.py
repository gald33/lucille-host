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
holds them before the host acts.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from lucille_host import __version__, _exec, _output
from lucille_host.audit import AuditLog
from lucille_host.containment import contain
from lucille_host.controls import Controls
from lucille_host.ops import CATALOG_VERSION, OPERATIONS, Operation, args_valid
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
    ctx.audit.received(job.job_id, job.seq, job.op, job.args)
    verdict = earlier_code if earlier_code is not None else check(job, ctx)
    if isinstance(verdict, str):
        ctx.audit.decision(job.job_id, "refused", verdict)
        data = encode(
            reply_body(job.job_id, Outcome(status="refused", code=verdict), ctx.policy_digest)
        )
        if decided is not None:
            decided("refused", verdict, data)
    else:
        ctx.audit.decision(job.job_id, "run")
        if decided is not None:
            decided("run", None, None)
        outcome = execute(verdict, ctx)
        data = encode(reply_body(job.job_id, outcome, ctx.policy_digest))
    ctx.audit.sent(job.job_id, hashlib.sha256(data).hexdigest(), len(data))
    return data
