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
import threading
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


# How long an in-process function told to stop (its deadline passed, the user killed it, or its
# room was abandoned) has to return before the host replies without it; and how often the worker
# looks while it runs.
IN_PROCESS_GRACE: Final = 2.0
_IN_PROCESS_POLL: Final = 0.05


# The call of each in-process entry that was left running after the host replied without it, by
# entry name. While one is alive, no other call of that entry starts: at most one stray call per
# entry, never two calls of one entry at once. Process-wide, as the threads are.
_DETACHED: dict[str, threading.Thread] = {}
_DETACHED_LOCK = threading.Lock()


def _detached_running(name: str) -> bool:
    with _DETACHED_LOCK:
        thread = _DETACHED.get(name)
        if thread is not None and not thread.is_alive():
            del _DETACHED[name]
            thread = None
        return thread is not None


# Python's own exception classes an in-process call's exception is named for, each before any class
# it derives from.
_NAMED_EXCEPTIONS: Final = (
    RecursionError,
    NotImplementedError,
    KeyError,
    IndexError,
    LookupError,
    FileNotFoundError,
    PermissionError,
    TimeoutError,
    OSError,
    UnicodeError,
    ValueError,
    TypeError,
    ZeroDivisionError,
    OverflowError,
    ArithmeticError,
    AttributeError,
    ImportError,
    AssertionError,
    RuntimeError,
    MemoryError,
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
    Exception,
)


def _exception_name(exc: BaseException) -> str:
    """The name of the first of :data:`_NAMED_EXCEPTIONS` the exception's type derives from, else
    ``BaseException``.

    The name of a class the function made is the function's to choose, and so is as unbounded and
    as unredacted as its output: it goes nowhere. Every name returned is one of Python's own, so
    it is short and holds no secret, and it is written to the audit line with ``log_results`` off.

    The exception object is the function's too, and Python asks it for its ``__class__`` when
    ``isinstance`` is given an instance, which can run the function's code and raise anything,
    with a message that holds a secret. So this asks the type, which asks the object nothing, and
    any exception it raises anyway is dropped: the call is then named ``BaseException``.
    """
    try:
        kind = type(exc)
        for cls in _NAMED_EXCEPTIONS:
            if issubclass(kind, cls):
                return cls.__name__
    except Exception:  # noqa: BLE001 - nothing it holds may reach the thread's excepthook
        pass
    return "BaseException"


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
    """Run an in-process entry (see :func:`execute`), bounded by the host whether or not the
    function cooperates.

    The pause flag, the drain and the room are read just before the call, as before a child's
    start. The ``start`` line names the function and the entry's definition hash in place of an
    executable and its argv. The function runs on a thread of its own while the worker watches it:

    - at its deadline, or when the user kills it or its room is abandoned, the call's ``stop``
      turns true and the function has :data:`IN_PROCESS_GRACE` seconds to return;
    - after that the host replies without it (``failed timeout``, ``failed killed_locally``, or no
      reply for an abandoned room) and its audit line says ``detached``. A thread cannot be
      killed: a detached function runs on, its result is dropped, and whatever it does after the
      reply happens anyway. The worker, the drain and the other rooms do not wait for it. While
      it runs, a new call of the same entry is refused before it starts (``failed interrupted``,
      ``stopped: detached_call_running`` in the audit log): at most one stray call per entry, and
      never two calls of one entry at once. Its thread is a daemon: when the process exits, a
      stray call is cut off wherever it is;
    - a call that returns after its deadline is ``failed timeout``, and one that returns after a
      kill or an abandonment is ``killed``, whatever it returned, as for a child; a kill is then
      consumed, so it does not reach the next request.

    A function that raises anything else, ``BaseException`` included, gets a ``done`` reply with
    exit code 1 and a stderr naming the exception's nearest built-in class only (see
    :func:`_exception_name`): its message and any name the function gave its own class can hold
    anything the function held, so neither goes in the reply or in the audit log.
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
        else "detached_call_running"
        if _detached_running(operation.name)
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
    deadline = began + ready.timeout_s
    told = threading.Event()

    def call_stop() -> bool:
        return told.is_set() or kill_requested() or time.monotonic() >= deadline

    call = Call(
        args=MappingProxyType(dict(ready.job.args)),
        timeout_s=ready.timeout_s,
        deadline=deadline,
        stop=call_stop,
    )
    box: dict[str, Any] = {}
    finished = threading.Event()

    def body() -> None:
        try:
            box["returned"] = entry.function(call)
        except TimedOut:
            box["said"] = "timeout"
        except Stopped:
            box["said"] = "stopped"
        except BaseException as exc:  # noqa: BLE001 - reported by type, as any other raise
            box["raised"] = _exception_name(exc)
        finally:
            box["at"] = time.monotonic()
            finished.set()

    thread = threading.Thread(target=body, name=f"lucille-host {operation.name}", daemon=True)
    thread.start()
    watched: str | None = None
    while not finished.wait(_IN_PROCESS_POLL):
        if kill_requested():
            watched = "killed"
            break
        if time.monotonic() >= deadline:
            watched = "timeout"
            break
    if watched is not None:
        told.set()
        finished.wait(IN_PROCESS_GRACE)
    detached = not finished.is_set()
    if detached:
        with _DETACHED_LOCK:
            _DETACHED[operation.name] = thread
    duration_ms = int((time.monotonic() - began) * 1000)
    result = Result()
    raised: str | None = None
    if watched == "killed" or kill_requested():
        ended = "killed"
    elif watched == "timeout" or box.get("said") == "timeout" or box["at"] > deadline:
        ended = "timeout"
    elif "raised" in box:
        ended, raised = "raised", box["raised"]
    elif box.get("said") == "stopped":
        # Stopped with nothing telling it to stop: a fault of the function's own.
        ended, raised = "raised", "Stopped"
    elif _well_formed(box.get("returned")):
        ended, result = "exited", box["returned"]
    else:
        ended, raised = "raised", "TypeError"
    if raised is not None:
        result = Result(exit_code=1, stderr=f"the operation raised {raised}")
    abandoned = ended == "killed" and stop()
    if ended == "killed" and not abandoned:
        ctx.controls.kill_done()
    stdout_raw = result.stdout.encode("utf-8", "replace")
    stderr_raw = result.stderr.encode("utf-8", "replace")
    # Rendered as a child's output is (decoded, redacted, capped), before anything is written or
    # sent: the audit line, the reply and the logs only ever see the rendered strings.
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
    if detached:
        fields["detached"] = True
    if ctx.policy.log_results:
        fields["stdout"] = stdout
        fields["stderr"] = stderr
    ctx.audit.execution(job_id, **fields)
    if abandoned:
        raise Abandoned
    if ended == "timeout":
        return Outcome(status="failed", code="timeout", duration_ms=duration_ms)
    if ended == "killed":
        return Outcome(status="failed", code="killed_locally", duration_ms=duration_ms)
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
