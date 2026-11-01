"""``lucille-host run``: listening for Lucille's requests in the work rooms, and answering them.

spec/PROTOCOL.md sections 5, 7, 9 and 10 to 16. The host is reachable whenever ``run`` runs, and
only then; nothing starts it at login. A profile runs one ``run`` at a time: it holds
``state/.run.lock`` (``os.lockf``) for as long as it runs, and a second ``run`` of the profile
refuses, changing nothing, since two would each answer every request. The threads:

- **Main.** Takes the signals (the first SIGTERM, SIGHUP or Ctrl-C drains), and makes every
  pairing and sharing call, through the :class:`link.Session` that keeps the pair and takes the
  work rooms Lucille offers over it: the session's rules decide when to resolve and touch, and
  what to accept, leave or abandon. Main feeds it the other threads' events (a note on the pair
  channel, a hub error, a room that failed its checks, a verdict on the local policy) and a
  wake from sleep, which it finds by the wall clock jumping ahead of the monotonic one, which
  stops while the computer sleeps. It starts a reader for each room the session says may be
  read, writes ``state/status.json``, and, for ``run --json-events``, the ``ready`` and ``state``
  objects (spec/CLI.md section 13). It never loads the local policy, which looks at each of its
  folders: it stats the file once a tick, checks a file that changed without looking at any
  folder (``policy.check_file``), holds the offers until a load of the changed file says it is
  valid, and asks the loader to load it. The session looks again, through main, just before it
  accepts a room (``Session.offers_held``): a step can wait long on the hub, so the offers then wait
  too when the last load began :data:`RECONCILE_EVERY` seconds ago or more, for a new one.
- **The heartbeat.** While main is in a step, where it can wait on a hub that does not answer
  (each call given up after ``_switchboard.CLIENT_TIMEOUT``) or on the record lock, it writes
  ``state/status.json`` and the ``state`` object again as main last wrote them, at least every
  :data:`STATUS_EVERY` seconds, for up to :data:`WAIT_MOST` seconds of one step. It decides
  nothing.
- **The pair reader.** One long-poll on the ``pair`` channel of pair:<name>, at the generation
  the last resolve found, from sequence number 0 at each new generation. A library note there
  is only a wake for main (spec/PROTOCOL.md section 5); anything else is logged and ignored.
- **A room reader per work room** the session judged usable: accepted, passing checks 3 and 4, and
  named by its list as the current pair's. Before every read it checks the room again, with no hub
  call, and stops when the room fails, telling main unless the records gave the room up (below).
  Its read is a peek past the room's cursor, ``since`` always given, waiting up to 25 s for 200
  messages at most. A message the room's key cannot open, or whose body is not JSON or nests more
  than 64 levels deep (``_switchboard.NEST_MOST``), fails the whole peek: the reader then reads the
  channel's newest 1000 messages, hands on those above the cursor up to the first unopenable one,
  logs that one and moves the cursor to it; with none above the cursor in that window it logs
  ``read_stuck``, and tries again later without moving. After a page is handled, the cursor moves
  to the lowest last sequence number among the channels whose page came back full, or else to the
  highest returned (:func:`next_cursor`). The readers and the pair reader each long-poll at the
  same time.
- **The worker.** One request at a time, in the order they were read, so in sequence order within
  a room: the messages that get no reply are logged (``envelope.ignored``); a job id already in
  the journal gets nothing (dedupe, across rooms); the local policy is loaded, main is told
  whether it is valid, and while it is invalid the request waits, unwritten; the journal entry is
  written; steps 2 to 4 of the refusal order (``envelope.early_code``) and then
  ``jobs.process``, in a context built from that load, so that a change of the local policy
  applies to the next request; the decision goes to the journal as soon as it is made. Each
  message's cursor is written to ``state/cursors.json`` only after its audit lines and its
  journal entry. For ``run --json-events`` it writes each request's ``request`` objects, right
  after the audit lines they mirror.
- **The loader.** Loads the local policy each time main asks (when the file changed, and every
  :data:`RECONCILE_EVERY` seconds), and tells main whether it is valid, so that main's asks are
  answered while the worker runs an operation. It decides nothing. A load held up on a folder of a
  stalled mount holds up only the thread that makes it, never main; main takes each verdict only
  from a load that began after the last one it took.
- **The poster.** Each reply is posted as a note with no thread and an explicit TTL
  (``envelope.reply_ttl``), after the room is checked again; a failed post is tried again with a
  back-off until its TTL would have run out, and a transport failure never becomes a ``failed``
  reply. The worker does not wait for it: a reply retried through a hub outage holds up no other
  request.

**Abandoning a room** (the session's callback): it is recorded first, by the session, in the
audit log and the room record; then queued requests and replies from the room are dropped, a
running child of a request from it is killed with no reply (the context's ``stop``), its reader
stops, and its journal entries are marked; the session then leaves the room. When the pair ends
(``run`` then stops, exiting 0), every room ``run`` still reads is abandoned this way too: the
session has left the rooms it held already, and when another command forgot the pair
(``lucille-host forget`` while ``run`` runs) that command left them.

**Another command's abandon** does not wait for main. ``forget`` marks every room abandoned in the
room record first, and then, still holding the record lock, leaves them and tells Lucille: hub
calls, each of which can take up to the client's timeout. Main waits for that lock at its next
step. So the other threads read the room record and the gate's record themselves, with no lock and
no hub call: the worker before each request it takes, each time it polls a running child
(``_exec.POLL_S``, 0.05 s) and before it queues a reply, the readers before each read, and the
poster before each post. A room the records give up (marked abandoned, no longer held, or the gate
record not the one the session serves) is halted there and then, as above but for the leave:
nothing more starts from it, and a running child of a request from it is killed with no reply.
``run`` itself stops at its first step after the other command is done, by the pair's gate record,
whether or not Lucille was told, and touches nothing of what the profile holds, which may already
be a new pair's. Until that step, a drain waits for main as well, while the heartbeat writes
``state/status.json``: a stop signal that arrives meanwhile does not end main's wait for the lock
(``state.wait_for_lock``), and is not lost: once the command is done, ``run`` stops, for its pair
when the command forgot it, and otherwise by draining.

**After a restart**, before a room is read, the journal's entries for it with no reply sent are
taken up: a decision to run gets ``failed interrupted``, never a second run; a refusal is posted
again, the same bytes; a request received and not decided is decided afresh from the body the
journal kept, with the time it was first received; one whose body was not kept gets nothing.
Nothing is posted for a request in a room abandoned or left.

**Back-off.** Every thread that calls the hub waits after a failure: the hub's ``retry_after`` on a
429, else 1 s doubling to 30 s, plus up to a quarter more at random. No single failure ends a
thread, and each hub error is also a reason for main to resolve the pair.

**Draining** (the first SIGTERM, SIGHUP or Ctrl-C; under ``--exit-on-stdin-eof`` the end of
standard input, :class:`Lifeline`): the readers stop after their current read, the worker
finishes the request it is deciding or running (``jobs.process`` starts nothing more), and the
poster gets :data:`FLUSH` seconds to post what is waiting; the rest is left to the next start. A
request the worker has not written down yet, one waiting for a valid policy among them, is
dropped, and read again at the next start. Meanwhile main goes on writing ``state/status.json``.
A Ctrl-C while draining kills the running child, as ``lucille-host kill`` does. Pause and kill
reach the worker through ``jobs.process``.

**The local policy while ``run`` runs** (spec/CLI.md section 13.5). While the file is invalid,
or not checked yet (at start, and after it changed), ``run`` acts on nothing: the session accepts
no room (``Session.hold_offers``), no reader starts, and the worker writes no request down. Replies
decided before are still posted, and the pair is kept. Once the file is valid, ``run`` goes on.

Nothing here starts a process, opens a listening socket, or writes outside the profile's own
folders and its keyring item.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import hashlib
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from lucille_host import (
    __version__,
    _switchboard,
    envelope,
    gate,
    jobs,
    link,
    paths,
    policy,
    rooms,
    state,
)
from lucille_host.audit import AuditLog
from lucille_host.controls import Controls, no_terminal_stop
from lucille_host.journal import Entry, Journal
from lucille_host.ops import OPERATIONS, args_valid

__all__ = [
    "ALREADY_RUNNING",
    "FLUSH",
    "PAGE",
    "RECOVER",
    "RUN_LOCK",
    "STDIN_END",
    "STDOUT_GONE",
    "WORKER",
    "Backoff",
    "Cursors",
    "Downgrade",
    "Failed",
    "Lifeline",
    "LOADER",
    "Runner",
    "check_version",
    "clock_view",
    "next_cursor",
    "one_at_a_time",
    "one_run",
    "run",
    "wake_gap",
]

# The most messages one peek returns, per channel, and the most one recovery read returns.
PAGE: Final = 200
RECOVER: Final = 1000
# How often main looks at the clock, the gate and the rooms when nothing else wakes it.
TICK: Final = 1.0
# A wall clock that ran this much further than the monotonic clock between two ticks means the
# computer slept.
WAKE_GAP: Final = 30.0
# While draining, how long the poster may still post what is waiting.
FLUSH: Final = 30.0
# How long abandoning a room waits for the killed child of a request from it to end.
KILL_WAIT: Final = 10.0
# Main looks again at which rooms may be read at least this often.
RECONCILE_EVERY: Final = 30.0
# And writes status.json at least this often.
STATUS_EVERY: Final = 60.0
# How long main may wait in one step, on the hub (each call given up after
# ``_switchboard.CLIENT_TIMEOUT``) or on the record lock another command holds, while the heartbeat
# still writes status.json and ``state`` for it. Past this, ``run`` counts as not stepping.
WAIT_MOST: Final = 300.0
# At the end, how long main waits for a reader still in its last read.
READER_GRACE: Final = 1.0
# The threads that load the local policy: the one that handles requests, before each, and the
# one that loads it when main asks.
WORKER: Final = "lucille-host worker"
LOADER: Final = "lucille-host policy loader"
# What ends the lifeline: standard input at its end, or a standard output nobody reads.
STDIN_END: Final = "stdin_end"
STDOUT_GONE: Final = "stdout_gone"
# How much of standard input the lifeline reads, and discards, at a time.
_LIFELINE_READ: Final = 4096

# In the state folder: held by ``run`` for as long as it runs.
RUN_LOCK: Final = ".run.lock"
ALREADY_RUNNING: Final = (
    "`lucille-host run` is already running for this profile, and a profile runs one at a time: "
    "two would each answer Lucille's requests. Stop that one first"
)

_PAIR: Final = _switchboard.PAIR_CHANNEL
_EXEC: Final = _switchboard.EXEC_CHANNEL
_ENDED: Final = frozenset({"ended", "none"})
# The locks :func:`one_at_a_time` holds in this process, by real path. ``lockf`` belongs to the
# process: a second ``run`` here would take the lock again, and closing its descriptor would
# release the first's.
_HELD: set[str] = set()
_HELD_GUARD = threading.Lock()


class Failed(RuntimeError):
    """``run`` stopped because one of its threads met something it cannot go on from; or a command
    found another of the profile holding a lock only one may hold (:func:`one_at_a_time`).
    ``code`` is what ``--json`` calls it (spec/CLI.md section 10): ``busy`` or ``lock_failed``
    for the lock. A thread's failure leaves it ``internal``, and the command names it by its
    cause."""

    def __init__(self, message: str, *, code: str = "internal") -> None:
        super().__init__(message)
        self.code = code


@contextlib.contextmanager
def one_at_a_time(lock: Path, busy: str, verb: str) -> Iterator[None]:
    """Hold the lock file ``lock``, taken without waiting, until the block ends. Raise
    :class:`Failed` with ``busy``, holding nothing, when another command holds it, in this process
    or another; or saying that ``verb`` cannot make sure it is the only one, when it cannot be
    taken here."""
    path = os.path.realpath(lock)
    with _HELD_GUARD:
        if path in _HELD:
            raise Failed(busy, code="busy")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.lockf(descriptor, os.F_TLOCK, 0)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise Failed(busy, code="busy") from None
            raise Failed(
                f"cannot lock {path} ({exc.strerror}), so {verb} cannot make sure it is the only "
                "one of this profile",
                code="lock_failed",
            ) from exc
        _HELD.add(path)
    try:
        yield
    finally:
        with _HELD_GUARD:
            _HELD.discard(path)
            os.close(descriptor)


def one_run(state_dir: Path) -> contextlib.AbstractContextManager[None]:
    """Hold the profile's run lock until the block ends; raise :class:`Failed`, holding nothing,
    when another ``run`` of the profile holds it, in this process or another, or it cannot be
    taken here."""
    return one_at_a_time(paths.ensure_dir(state_dir) / RUN_LOCK, ALREADY_RUNNING, "`run`")


def next_cursor(
    messages: Sequence[_switchboard.Message], channels: Sequence[str], limit: int, cursor: int
) -> int:
    """Where to read from next, once a page is handled: the lowest last sequence number among the
    channels whose page came back full (``limit`` messages), or else the highest returned. A full
    channel may have more past its last message, and a peek past a higher number would skip
    them."""
    if not messages:
        return cursor
    full = []
    for channel in channels:
        seqs = [message.seq for message in messages if message.channel == channel]
        if len(seqs) >= limit:
            full.append(max(seqs))
    return max(cursor, min(full) if full else max(message.seq for message in messages))


def wake_gap(wall: float, monotonic: float) -> bool:
    """Whether, over one interval, the wall clock moved ``wall`` seconds and the monotonic clock
    ``monotonic`` seconds, more than :data:`WAKE_GAP` apart: the computer slept in between."""
    return wall - monotonic > WAKE_GAP


class Downgrade(RuntimeError):
    """This release is older than one that started ``run`` on the profile before."""

    def __init__(self, recorded: str, version: str) -> None:
        super().__init__(
            f"this release, {version}, is older than {recorded}, which started `run` on this "
            "profile before, and an older release can be a vulnerable one: "
            "`lucille-host run --allow-downgrade` starts it all the same"
        )
        self.recorded = recorded


def load_last_version(dirs: paths.Dirs) -> str | None:
    """The newest release that started ``run`` on the profile, by ``state/last_version.json``;
    None when there is no record. A record that holds anything but ``{"v": 1, "version": <a
    release>}`` is a :class:`state.StateError`."""
    data = state.read_json(dirs.last_version_file)
    if data is None:
        return None
    version = data.get("version") if isinstance(data, dict) else None
    valid = (
        isinstance(data, dict)
        and set(data) == {"v", "version"}
        and type(data["v"]) is int
        and data["v"] == 1
        and isinstance(version, str)
        and envelope.is_release(version)
    )
    if not valid or not isinstance(version, str):
        raise state.StateError(f"{dirs.last_version_file} is not a lucille-host version record")
    return version


def check_version(dirs: paths.Dirs, *, allow: bool, warn: Callable[[str], None]) -> None:
    """Refuse a release older than the newest that started ``run`` on the profile
    (:class:`Downgrade`), by the order of ``min_host_version`` (``envelope.older``); with
    ``allow``, say so with ``warn`` and go on, leaving the record as it is. Otherwise record this
    release when it is a release (``MAJOR.MINOR.PATCH`` alone) and not the one recorded: the
    record only goes up. The caller holds the run lock."""
    recorded = load_last_version(dirs)
    version = __version__
    if recorded is not None and envelope.older(version, recorded):
        if not allow:
            raise Downgrade(recorded, version)
        warn(
            f"this release, {version}, is older than {recorded}, which started `run` on this "
            f"profile before; it runs because --allow-downgrade was given, and {recorded} stays "
            "recorded"
        )
        return
    if envelope.is_release(version) and version != recorded:
        state.write_json(dirs.last_version_file, {"v": 1, "version": version})


def clock_view(skew: float | None, skewed: bool) -> dict[str, Any]:
    """The clock, as ``status`` and the ``state`` object say it (spec/CLI.md section 6):
    ``skewed`` once the hub refused a write for this computer's clock, or a skew past
    :data:`_switchboard.SKEW_WINDOW` was measured; ``unknown`` before any was; else ``ok``."""
    if skewed or (skew is not None and abs(skew) > _switchboard.SKEW_WINDOW):
        return {"state": "skewed", "skew_s": skew}
    if skew is None:
        return {"state": "unknown"}
    return {"state": "ok", "skew_s": skew}


class Lifeline:
    """``run --exit-on-stdin-eof``: a thread reads standard input to its end, discarding what it
    reads, and the end drains ``run`` as its first SIGTERM does. The end is ``b""`` (every holder
    of the pipe's write end closed it: a shell that quits, crashes or is force-quit), a read error,
    or no standard input at all (``fd`` -1). A read end handed over non-blocking is made blocking
    and read again. A terminal whose foreground is another process group (``run`` is a background
    job of a shell) is not read until ``run`` is in the foreground again: the thread blocks
    SIGTTIN for itself alone (:func:`controls.no_terminal_stop`), so the read fails rather than
    stopping the whole process, and is tried again each tick. With ``--json-events``, a standard
    output nobody reads ends it too (:meth:`cut`, :data:`STDOUT_GONE`). It never closes standard
    input and writes nothing; it may end before ``run`` starts serving, and ``run`` stops then."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.ended = threading.Event()
        self.cause: str | None = None
        self._lock = threading.Lock()
        self._on_end: Callable[[str], None] | None = None
        self._thread = threading.Thread(
            target=self._watch, name="lucille-host lifeline", daemon=True
        )

    def start(self) -> Lifeline:
        if self.fd < 0:
            self.cut(STDIN_END)
        else:
            self._thread.start()
        return self

    def _watch(self) -> None:
        try:
            no_terminal_stop()
            while True:
                try:
                    data = os.read(self.fd, _LIFELINE_READ)
                except BlockingIOError:
                    os.set_blocking(self.fd, True)
                    continue
                except OSError as exc:
                    if exc.errno != errno.EIO or not _in_background(self.fd):
                        raise
                    time.sleep(TICK)
                    continue
                if not data:
                    return
        except BaseException:  # noqa: BLE001 - any error reading is the end
            return
        finally:
            self.cut(STDIN_END)

    def cut(self, cause: str) -> None:
        """The lifeline ended, for ``cause``; the first cause stays."""
        with self._lock:
            if self.cause is not None:
                return
            self.cause = cause
            self.ended.set()
            handler = self._on_end
        if handler is not None:
            handler(cause)

    def attach(self, handler: Callable[[str], None]) -> None:
        """Call ``handler`` with the cause when the lifeline ends, or at once if it has."""
        with self._lock:
            self._on_end = handler
            cause = self.cause
        if cause is not None:
            handler(cause)


def _in_background(fd: int) -> bool:
    """Whether ``fd`` is a terminal whose foreground is a process group other than this one's."""
    try:
        return os.tcgetpgrp(fd) != os.getpgrp()
    except OSError:
        return False


class Backoff:
    """How long to wait after a failed hub call: the hub's ``retry_after`` when it gave one, else
    1 s doubling with each failure up to 30 s, plus up to a quarter more at random."""

    def __init__(self) -> None:
        self.failures = 0

    def next(self, retry_after: float | None = None) -> float:
        self.failures += 1
        if retry_after is not None:
            return retry_after
        delay = min(link.BACKOFF_MOST, link.BACKOFF_FIRST * 2 ** min(self.failures - 1, 16))
        return delay + delay * secrets.randbelow(1001) / 4000

    def reset(self) -> None:
        self.failures = 0


class Cursors:
    """How far each work room has been read, by workspace: ``state/cursors.json``. A cursor only
    moves forward, and is written whole each time it moves."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seqs = self._load()

    def _load(self) -> dict[str, int]:
        data = state.read_json(self.path)
        if data is None:
            return {}
        rooms_read = data.get("rooms") if isinstance(data, dict) else None
        valid = (
            set(data) == {"v", "rooms"}
            and data["v"] == 1
            and isinstance(rooms_read, dict)
            and all(
                isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0
                for seq in rooms_read.values()
            )
        )
        if not valid:
            raise state.StateError(f"{self.path} is not a lucille-host cursor file")
        return dict(rooms_read)

    def get(self, workspace: str) -> int:
        with self._lock:
            return self._seqs.get(workspace, 0)

    def set(self, workspace: str, seq: int) -> None:
        with self._lock:
            if seq <= self._seqs.get(workspace, 0):
                return
            self._seqs[workspace] = seq
            state.write_json(self.path, {"v": 1, "rooms": dict(self._seqs)})

    def drop(self, workspace: str) -> None:
        with self._lock:
            if self._seqs.pop(workspace, None) is not None:
                state.write_json(self.path, {"v": 1, "rooms": dict(self._seqs)})


@dataclass(eq=False)
class _Room:
    """A work room being read."""

    name: str
    workspace: str
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


@dataclass(eq=False)
class _PairReader:
    workspace: str
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


@dataclass(eq=False)
class _Batch:
    """A page a room reader handed to the worker, and the cursor once it is all handled."""

    room: _Room
    messages: list[_switchboard.Message]
    cursor: int
    # An unopenable message's sequence number, logged and moved past once the others are handled.
    unreadable: int | None = None
    done: bool = False
    finished: bool = False


@dataclass(eq=False)
class _Afresh:
    """A request received before a stop and not decided: decided now, from the journal."""

    room: _Room
    job_id: str
    entry: Entry
    done: bool = False
    finished: bool = False


@dataclass(eq=False)
class _Reply:
    job_id: str
    room: str
    workspace: str
    data: bytes
    # What the reply's TTL counts from: the request's valid not_after, else its receipt.
    basis: float
    backoff: Backoff = field(default_factory=Backoff)
    next_try: float = 0.0


def _workspace_of(record: rooms.Record, name: str) -> str | None:
    """The workspace of room ``name`` by the room record: held, or left."""
    held = record.held.get(name)
    if held is not None:
        return held.workspace
    for workspace, left in record.left.items():
        if left.room == name:
            return workspace
    return None


def _before_unreadable(
    rows: list[_switchboard.Message], cursor: int
) -> tuple[list[_switchboard.Message], int | None]:
    """Of a recovery read: the messages above ``cursor`` up to the first unopenable one, and that
    one's sequence number (None when there is none above the cursor)."""
    taken = []
    for row in sorted(rows, key=lambda message: message.seq):
        if row.seq <= cursor:
            continue
        if row.unreadable:
            return taken, row.seq
        taken.append(row)
    return taken, None


def _asked(body: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """What a request asked for, as its ``received`` object says it: the op, when it has an
    operation name's form and the request is within the protocol's size; the folder alias and the
    path, when the op is in the catalog, works in a folder, and its arguments hold to its schema.
    A path that starts with ``/``, or holds a character a terminal must not show, is never
    written: it is None."""
    if envelope.size(body) > envelope.REQUEST_LIMIT:
        return None, None, None
    op = body.get("op")
    if not isinstance(op, str) or not envelope.is_op_name(op):
        return None, None, None
    operation = OPERATIONS.get(op)
    args = body.get("args")
    if operation is None or not operation.uses_root or not args_valid(operation, args):
        return op, None, None
    path = args["path"]
    if path.startswith("/") or link.has_unsafe(path):
        return op, args["root"], None
    return op, args["root"], path


def _finished(outcome: jobs.Outcome, size: int) -> dict[str, Any]:
    """A ``finished`` object's fields: how the request ended, and the sizes of its reply."""
    if outcome.status == "done":
        return {
            "status": "done",
            "exit_code": outcome.exit_code,
            "duration_ms": outcome.duration_ms,
            "stdout_bytes": envelope.size(outcome.stdout),
            "stderr_bytes": envelope.size(outcome.stderr),
            "reply_bytes": size,
        }
    if outcome.status == "refused":
        return {"status": "refused", "code": outcome.code, "reply_bytes": size}
    return {
        "status": "failed",
        "code": outcome.code,
        "exit_code": outcome.exit_code,
        "duration_ms": outcome.duration_ms,
        "stdout_bytes": None if outcome.stdout is None else envelope.size(outcome.stdout),
        "stderr_bytes": None if outcome.stderr is None else envelope.size(outcome.stderr),
        "reply_bytes": size,
    }


class Runner:
    """One ``lucille-host run``: its threads, and what they share. ``tell``, when given, writes
    one event of ``run --json-events``; ``lifeline``, when given, drains ``run`` at its end."""

    def __init__(
        self,
        dirs: paths.Dirs,
        session: link.Session,
        *,
        controls: Controls,
        say: Callable[[str], None],
        tell: Callable[[str, dict[str, Any]], None] | None = None,
        lifeline: Lifeline | None = None,
    ) -> None:
        self.dirs = dirs
        self.session = session
        self.link = session.link
        self.controls = controls
        self.say = say
        self.tell = tell
        self.lifeline = lifeline
        # How ``run`` ended, once it returned: ``drained``, ``forgotten`` or ``ended``.
        self.outcome: str | None = None
        # Whether ``ready`` was reached: ``run`` serves the pair from there on.
        self.ready_sent = False
        paths.ensure_dir(dirs.state)
        self.audit = AuditLog(dirs.state)
        self.journal = Journal(dirs.journal_file, clock=self.link.now)
        self.cursors = Cursors(dirs.cursors_file)
        self._cv = threading.Condition()
        self._events: collections.deque[tuple[str, Any]] = collections.deque()
        self._work: collections.deque[_Batch | _Afresh] = collections.deque()
        self._replies: list[_Reply] = []
        self._current: _Batch | _Afresh | None = None
        self._rooms: dict[str, _Room] = {}
        self._abandoned: set[str] = set()
        self._restarted: set[str] = set()
        self._pair: _PairReader | None = None
        self._closing = False
        self._threads_stop = False
        self._failure: BaseException | None = None
        self._snapshot: Any = None
        self._reconciled = 0.0
        # The gate's record and the room record as main last read them.
        self._seen_gate: gate.Gate | None = None
        self._seen_record: rooms.Record | None = None
        # The local policy, as main knows it: ``ok``, ``invalid``, or ``checking`` until a load
        # of the file as it is now answers (at start, and after a change). Main asks the loader to
        # load it (``_recheck``), numbering each ask; a verdict from a load asked before the file
        # last changed counts for nothing. Each load is numbered too, in the order loads began:
        # main takes a verdict only from a load that began after the last one it took, and keeps
        # when that load began (monotonic).
        self._policy_state = "checking"
        self._policy_sig: tuple[int, ...] | None = None
        self._policy_asked = 0.0
        self._policy_seq = 0
        self._policy_changed_at = 0
        self._recheck = True
        self._loads = 0
        self._verdict = 0
        self._verdict_at = 0.0
        # The last good load: the effective policy and its digest.
        self._loaded: tuple[policy.Effective, str] | None = None
        self._digest_key = b""
        # Set while the worker decides or runs a request it has written down.
        self._deciding = False
        # The pair ``run`` serves, once its start checks passed.
        self._name = ""
        # What the last ``state`` object said, and when.
        self._state_seen: Any = None
        self._state_at = 0.0
        # What status.json says.
        self._last_contact: float | None = None
        self._skew: float | None = None
        self._skewed = False
        # Set by the poster when the hub accepted a post: main clears the session's own skew.
        self._posted = False
        self._stuck: set[str] = set()
        self._status_at = 0.0
        self._status_seen: Any = None
        self._started = False
        # When main began the step it is in, while it is in one (it may wait on the hub there);
        # what it last wrote in status.json and ``state``, for the heartbeat to write again; and
        # the lock that both write them under.
        self._waiting_since: float | None = None
        self._said = threading.Lock()
        self._said_status: dict[str, Any] | None = None
        self._said_state: dict[str, Any] | None = None
        self._beat_stop = threading.Event()
        self._beat = threading.Thread(
            target=self._guarded(self._beat_loop), name="lucille-host heartbeat", daemon=True
        )
        self._worker = threading.Thread(
            target=self._guarded(self._work_loop), name=WORKER, daemon=True
        )
        self._loader = threading.Thread(
            target=self._guarded(self._load_loop), name=LOADER, daemon=True
        )
        self._poster = threading.Thread(
            target=self._guarded(self._post_loop), name="lucille-host poster", daemon=True
        )

    # --- the threads' messages to main --------------------------------------------------------

    def _event(self, kind: str, value: Any = None) -> None:
        with self._cv:
            self._events.append((kind, value))
            self._cv.notify_all()

    def _fail(self, exc: BaseException) -> None:
        """Something a thread did not expect: ``run`` stops (it fails closed)."""
        with self._cv:
            if self._failure is None:
                self._failure = exc
            self._closing = True
            self._cv.notify_all()

    def _guarded(self, target: Callable[..., None]) -> Callable[..., None]:
        """A thread's body: anything it did not expect stops ``run`` (it fails closed)."""

        def body(*args: Any) -> None:
            try:
                target(*args)
            except BaseException as exc:  # noqa: BLE001 - handed to main, which stops
                self._fail(exc)

        return body

    def _tell(self, type_name: str, **fields: Any) -> None:
        if self.tell is not None:
            self.tell(type_name, fields)

    def _lifeline_ended(self, cause: str) -> None:
        """The lifeline ended (on its own thread, the events' writer, or main): drain first, as
        the first SIGTERM does, then wake main, then say so in the audit log. A log that cannot be
        written stops ``run``, which drains already."""
        self.controls.drain(cause)
        self._event("lifeline")
        try:
            self.audit.write("lifeline", event=cause)
        except BaseException as exc:  # noqa: BLE001 - handed to main, which stops
            self._fail(exc)

    def _hub_failed(self, exc: _switchboard.HubError) -> None:
        if exc.skewed:
            self._skewed = True
        self._event("hub_error")

    def _contact(self) -> None:
        self._last_contact = self.link.now()

    # --- main ---------------------------------------------------------------------------------

    def run(self) -> str:
        """Run until drained, or until the pair ends; returns what to print. Raises
        :class:`link.Stopped` when the pair cannot be used here (at start, or later for any
        reason but its end), and :class:`Failed` (another ``run`` of the profile runs, among other
        reasons)."""
        with one_run(self.dirs.state):
            return self.serve()

    def serve(self) -> str:
        """:meth:`run`, for a caller that holds the run lock already (the command, which takes it
        before its other start checks). Until ``ready``, a stop signal only drains, a second
        Ctrl-C too, as the command's own start checks have it: nothing runs that a kill could
        stop, and nothing may be written yet."""
        on_main = threading.current_thread() is threading.main_thread()
        restores: list[Callable[[], None]] = []
        if on_main:
            restores.append(self.controls.install_handlers(kill=False))
        self.session.stop_room = self._stop_room
        self.session.offers_held = self._policy_moved
        self.session.hold_offers = True
        try:
            try:
                self._digest_key = policy.digest_key(paths.ensure_dir(self.dirs.state))
            except policy.PolicyError as exc:
                # A damaged key is a file of the host's own, not an invalid policy.
                raise state.StateError(str(exc)) from exc
            name = self.session.check_ready()
            self._name = name
            if self.lifeline is not None:
                self.lifeline.attach(self._lifeline_ended)
            if self.controls.draining.is_set():
                # Its shell was gone, or a stop signal came, before it served: nothing was read,
                # written down or started, and no hub was called.
                self.outcome = "drained"
                return "drained: run stopped"
            if on_main:
                # From here on a Ctrl-C while draining kills the running child.
                restores.append(self.controls.install_handlers())
            self.say(f"running: pair {name}; Ctrl-C stops once the running operation ends")
            self._tell("ready", host_version=__version__, pair=name)
            self.ready_sent = True
            self._started = True
            self._policy_sig = policy.signature(self.dirs.policy_file)
            self._policy_asked = time.monotonic()
            self._worker.start()
            self._loader.start()
            self._poster.start()
            self._beat.start()
            try:
                # The first status and ``state``, before any hub call, once the loader's first
                # look at the policy is in, or a tick has passed: the heartbeat writes them again
                # while the first calls wait.
                self._snapshot_now()
                self._apply(self._next_events())
                self._report(running=True)
                with self._waiting():
                    self.session.start()
                    found = self.session.gate(name)
                    if found is None or found.opened_at is None:
                        self.say(
                            "no work room is taken and nothing runs until the SAS is confirmed: "
                            "`lucille-host pair status` shows it, and `lucille-host pair confirm` "
                            "confirms it"
                        )
                    self._abandoned_at_start()
                    self._reconcile(force=True)
                self._report(running=True)
                self._main_loop()
            except link.Stopped as exc:
                if exc.state not in _ENDED:
                    raise
                self._end_rooms()
                self.outcome = "forgotten" if exc.state == "none" else "ended"
                return f"{exc.state}: {exc}; run stopped"
            self._drain()
            self.outcome = "drained"
            return "drained: run stopped"
        finally:
            self._shutdown()
            for restore in reversed(restores):
                restore()

    def _next_events(self) -> list[tuple[str, Any]]:
        """The other threads' events for main, waiting up to a tick for one."""
        with self._cv:
            if not self._events and self._failure is None:
                self._cv.wait(TICK)
            events = list(self._events)
            self._events.clear()
        return events

    def _main_loop(self) -> None:
        wall, mono = self.link.now(), time.monotonic()
        while True:
            events = self._next_events()
            if self._failure is not None:
                raise Failed(f"run stopped: {self._failure!r}") from self._failure
            if self.controls.draining.is_set():
                return
            with self._waiting():
                changed = self._apply(events)
                now_wall, now_mono = self.link.now(), time.monotonic()
                if wake_gap(now_wall - wall, now_mono - mono):
                    self.session.wake()
                wall, mono = now_wall, now_mono
                if self._posted:
                    # The hub took a write signed by this clock: the session's refusal is past.
                    self._posted = False
                    self.session.skewed = False
                changed = self._watch_policy() or changed
                self.session.step()
                self._reconcile(force=changed)
            self._report(running=True)

    @contextlib.contextmanager
    def _waiting(self) -> Iterator[None]:
        """A step of main's, in which it can wait on the hub or on the record lock: meanwhile
        the heartbeat writes what main last wrote again (:meth:`_beat_loop`)."""
        self._waiting_since = time.monotonic()
        try:
            yield
        finally:
            self._waiting_since = None

    def _beat_loop(self) -> None:
        """The heartbeat. While main is in a step, for up to :data:`WAIT_MOST` seconds, it writes
        status.json and the ``state`` object again as main last wrote them, at least every
        :data:`STATUS_EVERY` seconds: a ``run`` waiting on a hub that does not answer still says
        so, and its ``last_hub_contact`` grows old, rather than going quiet as one that is not
        stepping does. It decides and reads nothing else."""
        while not self._beat_stop.wait(TICK):
            since = self._waiting_since
            if since is None or time.monotonic() - since > WAIT_MOST:
                continue
            with self._said:
                if self._waiting_since is None:
                    continue
                now = time.monotonic()
                if self._said_status is not None and now - self._status_at >= STATUS_EVERY:
                    self._status_at = now
                    state.write_json(
                        self.dirs.status_file,
                        {**self._said_status, "written_at": self.link.now()},
                    )
                if self._said_state is not None and now - self._state_at >= STATUS_EVERY:
                    self._state_at = now
                    self._tell("state", **self._said_state)

    def _apply(self, events: list[tuple[str, Any]]) -> bool:
        """Hand the threads' events to the session; whether a room may have changed."""
        changed = False
        for kind, value in events:
            if kind == "note":
                self.session.note(value)
            elif kind == "hub_error":
                self.session.hub_error()
            elif kind == "room":
                # A reader or the poster found the room failing: the session judges it, and
                # leaves it if it does fail.
                self.session.check_room(value)
                changed = True
            elif kind == "policy":
                asked, ticket, began, ok, why = value
                # A load asked for before the file last changed says nothing of the file now; one
                # that began before the last load whose verdict was taken says nothing newer.
                if asked >= self._policy_changed_at and ticket > self._verdict:
                    self._verdict, self._verdict_at = ticket, began
                    changed = self._set_policy("ok" if ok else "invalid", why) or changed
        return changed

    # --- the local policy, as main sees it --------------------------------------------------

    def _watch_policy(self) -> bool:
        """Main's look at the policy file, once a tick. It never loads it: it stats it, and
        when the file changed checks what it holds without looking at any folder, so that a file
        made invalid holds the offers at once, even while the worker runs an operation; a changed
        file valid by what it holds holds them too, ``checking`` if it was valid and ``invalid``
        still if it was not, until a load of it says it is valid. Either way, and every
        :data:`RECONCILE_EVERY` seconds, it asks the loader to load it. Whether the policy turned
        valid."""
        now = time.monotonic()
        sig = policy.signature(self.dirs.policy_file)
        changed = sig != self._policy_sig
        if not changed and now - self._policy_asked < RECONCILE_EVERY:
            return False
        self._policy_sig, self._policy_asked = sig, now
        with self._cv:
            self._policy_seq += 1
            if changed:
                self._policy_changed_at = self._policy_seq
            self._recheck = True
            self._cv.notify_all()
        if changed:
            try:
                policy.check_file(self.dirs.policy_file)
            except policy.PolicyError as exc:
                self._set_policy("invalid", str(exc))
            else:
                if self._policy_state == "ok":
                    self._set_policy("checking", None)
        return False

    def _policy_moved(self) -> bool:
        """The session's look, on main, just before it accepts a room or completes an accept a
        stop interrupted, at the end of a step that may have waited long on the hub: whether the
        policy file changed since main last looked at it, or the last load whose verdict main
        took began :data:`RECONCILE_EVERY` seconds ago or more (a folder may have turned into the
        home folder since, the file unchanged). The offers then wait: main looks at the file at
        its next tick, as :meth:`_watch_policy` says; for a load that old, until a new load says
        the file is valid (``checking``)."""
        if policy.signature(self.dirs.policy_file) != self._policy_sig:
            return True
        if time.monotonic() - self._verdict_at < RECONCILE_EVERY:
            return False
        self._set_policy("checking", None)
        return True

    def _set_policy(self, new: str, why: str | None) -> bool:
        """The policy as main knows it is now ``new``: the session holds offers unless it is
        ``ok``. A change to or from ``invalid`` is logged. Whether it turned ``ok``."""
        old = self._policy_state
        if new == old:
            return False
        self._policy_state = new
        if new == "invalid":
            self.audit.write("policy", event="invalid", error=why)
        elif old == "invalid":
            self.audit.write("policy", event="valid")
        if new != "ok":
            self.session.hold_offers = True
            return False
        self.session.allow_offers()
        return True

    def _snapshot_now(self) -> Any:
        found = gate.load(self.dirs.gate_file)
        record = rooms.load(self.dirs.rooms_file)
        self._seen_gate, self._seen_record = found, record
        opened = None if found is None else (found.pair, found.opened_at, found.touched_at)
        held = sorted((name, held.workspace, held.abandoned) for name, held in record.held.items())
        return opened, held

    def _reconcile(self, *, force: bool = False) -> None:
        """Follow the pair to the generation the last resolve found, and start a reader for each
        room the session says may be read."""
        pair = self.session.pair
        if pair is not None and pair.checked:
            if self._pair is None or self._pair.workspace != pair.workspace:
                if self._pair is not None:
                    self._pair.stop.set()
                self._pair = self._start_pair_reader(pair)
        snapshot = self._snapshot_now()
        if self._policy_state != "ok" or self._loaded is None:
            # No room is read while the policy is invalid, or not checked yet.
            return
        now = time.monotonic()
        if not force and snapshot == self._snapshot and now - self._reconciled < RECONCILE_EVERY:
            return
        self._snapshot, self._reconciled = snapshot, now
        for view in self.session.usable():
            room = self._rooms.get(view.name)
            if room is not None and room.thread is not None and room.thread.is_alive():
                continue
            if view.name in self._abandoned:
                continue
            self._start_room(view)

    def _start_pair_reader(self, pair: _switchboard.Pair) -> _PairReader:
        reader = _PairReader(pair.workspace)
        reader.thread = threading.Thread(
            target=self._guarded(self._read_pair), args=(reader, pair), daemon=True
        )
        reader.thread.start()
        return reader

    def _start_room(self, view: _switchboard.Room) -> None:
        room = _Room(view.name, view.workspace)
        if view.workspace not in self._restarted:
            self._restarted.add(view.workspace)
            self._restart_room(room)
        room.thread = threading.Thread(
            target=self._guarded(self._read_room), args=(room,), daemon=True
        )
        self._rooms[view.name] = room
        room.thread.start()

    def _abandoned_at_start(self) -> None:
        """Mark the journal entries of rooms abandoned or left: nothing is posted for them."""
        record = rooms.load(self.dirs.rooms_file)
        gone = set(record.left) | {
            held.workspace for held in record.held.values() if held.abandoned is not None
        }
        for workspace in sorted(gone):
            for job_id in self.journal.abandon_room(workspace):
                self.audit.write("reply", event="not_posted", job_id=job_id, why="abandoned")

    def _restart_room(self, room: _Room) -> None:
        """Take up what a stop left of this room's requests, before the room is read."""
        for job_id, entry in self.journal.pending(room.workspace):
            if entry.decision == "run":
                outcome = jobs.Outcome(status="failed", code="interrupted", duration_ms=0)
                # The digest of the last good load: main never loads the policy, and rooms are
                # read only while it is valid.
                assert self._loaded is not None
                data = jobs.encode(jobs.reply_body(job_id, outcome, self._loaded[1]))
                self.audit.write("restart", job_id=job_id, room=room.name, reply="interrupted")
                self._reply(room, job_id, data, entry.basis, restarted=True)
            elif entry.decision == "refused" and entry.reply is not None:
                self.audit.write("restart", job_id=job_id, room=room.name, reply="refused")
                self._reply(room, job_id, entry.reply.encode("ascii"), entry.basis, restarted=True)
            elif entry.decision is None and entry.body is not None:
                self.audit.write("restart", job_id=job_id, room=room.name, reply="decide")
                with self._cv:
                    self._work.append(_Afresh(room, job_id, entry))
                    self._cv.notify_all()
            else:
                # Received, not decided, and too large to have been kept: no reply.
                self.audit.write("restart", job_id=job_id, room=room.name, reply=None)

    def _end_rooms(self) -> None:
        """The pair is over: abandon every room still read here, so that a request running from
        one is killed with no reply. The session has left the rooms it held; when another
        command forgot the pair, that command left them."""
        for name in sorted(self._rooms):
            if name not in self._abandoned:
                self.audit.write("room", event="abandoned", room=name, why=rooms.PAIR_OVER)
                self._stop_room(name)

    def _stop_room(self, name: str) -> None:
        """The session abandons room ``name`` (it has recorded that already): start nothing more
        from it, kill what runs from it with no reply, stop reading it (:meth:`_halt`), and wait
        for the child to end. The session leaves it once this returns."""
        self._halt(name)
        deadline = time.monotonic() + KILL_WAIT
        with self._cv:
            while (
                self._current is not None
                and self._current.room.name == name
                and time.monotonic() < deadline
            ):
                self._cv.wait(0.05)

    def _halt(self, name: str, workspace: str | None = None) -> None:
        """Start nothing more from room ``name``, and stop reading it: its queued pages and
        replies are dropped, and its journal entries marked, so that nothing is posted for them.
        From here on the context's ``stop`` of a request from it answers yes, so a child running
        from it is killed at its next poll, with no reply; this does not wait for that. Any
        thread may call it, again too: the second call drops and marks only what came since."""
        with self._cv:
            self._abandoned.add(name)
            self._stuck.discard(name)
            room = self._rooms.get(name)
            if room is not None:
                room.stop.set()
                workspace = room.workspace
            kept: collections.deque[_Batch | _Afresh] = collections.deque()
            for item in self._work:
                if item.room.name == name:
                    item.done = True
                else:
                    kept.append(item)
            self._work = kept
            dropped = [reply for reply in self._replies if reply.room == name]
            self._replies = [reply for reply in self._replies if reply.room != name]
            self._cv.notify_all()
        if workspace is None:
            workspace = _workspace_of(rooms.load(self.dirs.rooms_file), name)
        marked = []
        if workspace is not None:
            marked = self.journal.abandon_room(workspace)
            self.cursors.drop(workspace)
        for job_id in sorted({reply.job_id for reply in dropped} | set(marked)):
            self.audit.write("reply", event="not_posted", job_id=job_id, room=name, why="abandoned")

    def _given_up(self, found: gate.Gate | None, record: rooms.Record, name: str) -> bool:
        """Whether, by the gate's record and the room record as read, room ``name`` is no longer
        this run's to use: the gate record is not the one the session serves (another command
        forgot the pair), or the room is no longer held, or is marked abandoned (by this run's
        session, or by another command)."""
        held = record.held.get(name)
        return not self.session.serves(found) or held is None or held.abandoned is not None

    def _halted(self, room: _Room) -> bool:
        """Whether nothing more may be done for ``room``: abandoned here, or given up by the
        records as they are now (:meth:`_given_up`), which halts it here at once
        (:meth:`_halt`). The records are read with no lock, so that a command holding the record
        lock (``forget``, while it leaves the rooms it has marked and tells Lucille) holds none
        of this up; one that cannot be read tells nothing, and main judges it. No hub call; any
        thread may call it."""
        if room.name in self._abandoned:
            return True
        try:
            found = gate.load(self.dirs.gate_file)
            record = rooms.load(self.dirs.rooms_file)
        except state.StateError:
            return False
        if not self._given_up(found, record, room.name):
            return False
        self._halt(room.name, room.workspace)
        return True

    def _drain(self) -> None:
        """Stop reading; let the worker finish the request it decides or runs; give the poster
        :data:`FLUSH` s. A request the worker has not written down (one waiting for a valid
        policy, or held in a load of it on a stalled mount) is not waited for. ``status.json``
        and ``state`` go on being written meanwhile."""
        self.say("draining: the running operation finishes, and nothing new starts")
        with self._cv:
            self._closing = True
            for item in self._work:
                item.done = True
            self._work.clear()
            self._stop_readers()
            self._cv.notify_all()
        self._wait_while(lambda: self._deciding)
        deadline = time.monotonic() + FLUSH
        self._wait_while(lambda: bool(self._replies) and time.monotonic() < deadline)
        if self._failure is not None:
            raise Failed(f"run stopped: {self._failure!r}") from self._failure

    def _wait_while(self, condition: Callable[[], bool]) -> None:
        """Wait while ``condition`` holds and nothing failed, reporting as main does."""
        while True:
            with self._cv:
                if self._failure is not None or not condition():
                    return
                self._cv.wait(0.1)
            self._report(running=True)

    def _stop_readers(self) -> None:
        if self._pair is not None:
            self._pair.stop.set()
        for room in self._rooms.values():
            room.stop.set()

    def _shutdown(self) -> None:
        self._beat_stop.set()
        with self._cv:
            self._closing = True
            self._threads_stop = True
            self._stop_readers()
            for item in self._work:
                item.done = True
            self._work.clear()
            self._cv.notify_all()
        for thread in (self._worker, self._poster, self._beat):
            if thread.is_alive():
                thread.join(KILL_WAIT)
        # The loader may be held in a load on a stalled mount: it is waited for no longer than a
        # reader in its last read.
        readers = [self._loader, *(room.thread for room in self._rooms.values())]
        if self._pair is not None:
            readers.append(self._pair.thread)
        ends = time.monotonic() + READER_GRACE
        for thread in readers:
            if thread is not None and thread.is_alive():
                thread.join(max(0.0, ends - time.monotonic()))
        with self._cv:
            dropped = list(self._replies)
            self._replies.clear()
        for reply in dropped:
            self.audit.write(
                "reply", event="not_posted", job_id=reply.job_id, room=reply.room, why="stopped"
            )
        if self._started:
            self._write_status(running=False)

    def _report(self, *, running: bool) -> None:
        """``state/status.json``, and the ``state`` object of ``run --json-events``: written when
        what it says changes, and at least every :data:`STATUS_EVERY` seconds whatever changed,
        so that a shell that hears nothing for twice that knows ``run`` is not stepping."""
        self._write_status(running=running)
        if self.tell is None or not running:
            return
        found, record = self._seen_gate, self._seen_record
        reading = sorted(name for name, room in self._rooms.items() if not room.stop.is_set())
        with self._cv:
            stuck = set(self._stuck)
        held = [
            {"name": name, "state": "read_stuck" if name in stuck else "reading"}
            for name in reading
        ]
        left = None
        if record is not None and not record.held and record.no_room is not None:
            left = rooms.WORDS[record.no_room]
        contact = None if self._last_contact is None else link.iso(self._last_contact)
        clock = clock_view(self._skew, self._skewed or self.session.skewed)
        view = {
            "pair": self._name,
            "confirmed": found is not None and found.opened_at is not None,
            "paused": self.controls.paused(),
            "policy": self._policy_state,
            "last_hub_contact": contact,
            "clock": clock,
            "rooms": held,
            "offers_waiting": 0 if record is None else len(record.offers),
            "last_left_because": left,
        }
        # What a new object is written for at once; a new contact time or skew waits.
        seen = tuple(
            (key, json.dumps(value, sort_keys=True))
            for key, value in view.items()
            if key not in ("last_hub_contact", "clock")
        ) + (contact is None, clock["state"])
        with self._said:
            now = time.monotonic()
            if seen == self._state_seen and now - self._state_at < STATUS_EVERY:
                return
            self._state_seen, self._state_at = seen, now
            self._said_state = view
            self._tell("state", **view)

    def _write_status(self, *, running: bool) -> None:
        """``state/status.json``: what ``lucille-host status`` reads of a running host. Written
        when it changes, and at least every :data:`STATUS_EVERY` seconds."""
        reading = sorted(name for name, room in self._rooms.items() if not room.stop.is_set())
        with self._cv:
            stuck = sorted(self._stuck)
        # The hub refused one of the host's own posts, or a write the library made for the
        # session, for this computer's clock.
        skewed = self._skewed or self.session.skewed
        # What changes it at once; a new contact time or skew alone waits for STATUS_EVERY.
        seen = (
            running,
            reading,
            stuck,
            skewed,
            self._skew is None,
            self._last_contact is None,
        )
        record = {
            "v": 1,
            "running": running,
            "pid": os.getpid(),
            "written_at": self.link.now(),
            "last_contact": self._last_contact,
            "skew": self._skew,
            "skewed": skewed,
            "reading": reading,
            "read_stuck": stuck,
        }
        with self._said:
            now = time.monotonic()
            if running and seen == self._status_seen and now - self._status_at < STATUS_EVERY:
                return
            self._status_seen, self._status_at = seen, now
            self._said_status = record
            state.write_json(self.dirs.status_file, record)

    # --- the readers --------------------------------------------------------------------------

    def _wait(self, stop: threading.Event, delay: float) -> None:
        stop.wait(delay)

    def _read_pair(self, reader: _PairReader, pair: _switchboard.Pair) -> None:
        """The ``pair`` channel of pair:<name>: the library's notes, as wakes for main."""
        backoff = Backoff()
        cursor = 0
        with self.link.pair_client(pair) as client:
            while not reader.stop.is_set() and not self._closing:
                unreadable = None
                try:
                    messages = client.peek(_PAIR, since=cursor, wait=link.LONG_POLL, limit=PAGE)
                    after = next_cursor(messages, [_PAIR], PAGE, cursor)
                except _switchboard.Unopenable:
                    try:
                        rows = client.recover(_PAIR, limit=RECOVER)
                    except _switchboard.HubError as exc:
                        self._hub_failed(exc)
                        self._wait(reader.stop, backoff.next(exc.retry_after))
                        continue
                    messages, unreadable = _before_unreadable(rows, cursor)
                    if unreadable is None:
                        self.audit.write("read_stuck", room="pair", cursor=cursor)
                        self._wait(reader.stop, backoff.next())
                        continue
                    after = unreadable
                except _switchboard.HubError as exc:
                    self._hub_failed(exc)
                    self._wait(reader.stop, backoff.next(exc.retry_after))
                    continue
                backoff.reset()
                self._contact()
                if reader.stop.is_set():
                    return
                for message in messages:
                    if message.tag is not None and not message.mine:
                        self._event("note", message.tag)
                    else:
                        self.audit.write("ignored", room="pair", seq=message.seq, why="no_tag")
                if unreadable is not None:
                    self.audit.write("unreadable", room="pair", seq=unreadable)
                cursor = after

    def _check(self, name: str) -> _switchboard.Room | None:
        """Room ``name``, if it may be read or posted in now: held and not abandoned, the gate
        open and a touch run since the pair was made, checks 3 and 4 passed, and its list naming
        the pair. A room the records give up (:meth:`_given_up`) is halted here at once. No hub
        call and no lock; any thread may call it."""
        if name in self._abandoned:
            return None
        try:
            found = gate.load(self.dirs.gate_file)
            record = rooms.load(self.dirs.rooms_file)
        except state.StateError:
            return None
        if self._given_up(found, record, name):
            # Another command forgot the pair or abandoned the room, or this run's session did:
            # halted here at once, whatever main is waiting for.
            self._halt(name)
            return None
        held = record.held[name]
        if found is None or found.opened_at is None or found.touched_at is None:
            return None
        try:
            view = self.link.room(name)
        except _switchboard.RoomError:
            return None
        if view.workspace != held.workspace:
            return None
        failed = rooms.failed_check(
            url=view.url,
            carries_token=view.carries_token,
            workspace=view.workspace,
            hub=found.hub,
            record=record,
        )
        if failed is not None or found.pair not in view.pairs:
            return None
        return view

    def _read_room(self, room: _Room) -> None:
        """``lucille.exec`` in one work room, handed to the worker a page at a time."""
        backoff = Backoff()
        cursor = self.cursors.get(room.workspace)
        client: _switchboard.RoomClient | None = None
        try:
            while not room.stop.is_set() and not self._closing:
                view = self._check(room.name)
                if view is None or view.workspace != room.workspace:
                    room.stop.set()
                    if room.name not in self._abandoned:
                        self._event("room", room.name)
                    return
                if client is None:
                    client = self.link.room_client(view)
                unreadable = None
                try:
                    messages = client.peek(_EXEC, since=cursor, wait=link.LONG_POLL, limit=PAGE)
                    after = next_cursor(messages, [_EXEC], PAGE, cursor)
                except _switchboard.Unopenable:
                    try:
                        rows = client.recover(_EXEC, limit=RECOVER)
                    except _switchboard.HubError as exc:
                        self._hub_failed(exc)
                        self._wait(room.stop, backoff.next(exc.retry_after))
                        continue
                    messages, unreadable = _before_unreadable(rows, cursor)
                    if unreadable is None:
                        with self._cv:
                            first = room.name not in self._stuck
                            self._stuck.add(room.name)
                        if first:
                            self.audit.write("read_stuck", room=room.name, cursor=cursor)
                        self._wait(room.stop, backoff.next())
                        continue
                    after = unreadable
                except _switchboard.HubError as exc:
                    self._hub_failed(exc)
                    self._wait(room.stop, backoff.next(exc.retry_after))
                    continue
                backoff.reset()
                self._contact()
                with self._cv:
                    self._stuck.discard(room.name)
                if room.stop.is_set() or self._closing:
                    return
                if (messages or unreadable is not None) and not self._hand(
                    _Batch(room, messages, after, unreadable)
                ):
                    return
                cursor = after
        finally:
            if client is not None:
                client.close()

    def _hand(self, batch: _Batch) -> bool:
        """Give a page to the worker and wait until it is handled; whether all of it was."""
        with self._cv:
            self._work.append(batch)
            self._cv.notify_all()
            while not batch.done and not self._threads_stop:
                self._cv.wait(0.5)
            return batch.finished

    # --- the worker ---------------------------------------------------------------------------

    def _work_loop(self) -> None:
        while True:
            with self._cv:
                while not (self._work or self._threads_stop):
                    self._cv.wait()
                if self._threads_stop:
                    return
                item = self._work.popleft()
                self._current = item
            finished = False
            try:
                if isinstance(item, _Batch):
                    finished = self._handle(item)
                else:
                    finished = self._afresh(item)
            finally:
                with self._cv:
                    item.done, item.finished = True, finished
                    self._current = None
                    self._cv.notify_all()

    def _load_loop(self) -> None:
        """The loader: each time main asks, load the policy and tell main whether it is valid."""
        while True:
            with self._cv:
                while not (self._recheck or self._threads_stop):
                    self._cv.wait()
                if self._threads_stop:
                    return
            self._load_policy()

    def _load_policy(self) -> tuple[policy.Effective, str] | None:
        """Load the local policy, and tell main whether it is valid; the effective policy and
        its digest, or None while it is invalid. Only the worker and the loader load it, never
        main: loading looks at each folder, and a folder on a stalled mount holds up whoever loads
        it."""
        with self._cv:
            self._recheck = False
            asked = self._policy_seq
            self._loads += 1
            ticket = self._loads
            began = time.monotonic()
        try:
            eff = policy.effective(policy.load(self.dirs.policy_file), paths.os_family())
        except policy.PolicyError as exc:
            self._event("policy", (asked, ticket, began, False, str(exc)))
            return None
        loaded = (eff, policy.digest(eff, self._digest_key))
        self._loaded = loaded
        self._event("policy", (asked, ticket, began, True, None))
        return loaded

    def _await_policy(self, room: _Room) -> tuple[policy.Effective, str] | None:
        """The policy a request is decided under: loaded now, and, while it is invalid, again
        each time main asks. None when ``run`` drains or stops, or the room is given up, first:
        the request was not written down, and is read again."""
        while True:
            if self._closing or self.controls.draining.is_set() or self._halted(room):
                return None
            with self._cv:
                seen = self._policy_seq
            loaded = self._load_policy()
            if self._closing or self.controls.draining.is_set():
                return None
            if loaded is not None:
                return loaded
            with self._cv:
                while not (
                    self._policy_seq != seen
                    or self._closing
                    or self.controls.draining.is_set()
                    or room.name in self._abandoned
                ):
                    self._cv.wait(TICK)

    def _handle(self, batch: _Batch) -> bool:
        room = batch.room
        for message in batch.messages:
            if self._halted(room) or not self._message(room, message, batch.cursor):
                return False
        if batch.unreadable is not None:
            self.audit.write("unreadable", room=room.name, seq=batch.unreadable)
            self.cursors.set(room.workspace, batch.unreadable)
        return True

    def _message(self, room: _Room, message: _switchboard.Message, page_cursor: int) -> bool:
        """One message read in a room; False when the batch stops here (a drain, or the room
        abandoned). Its cursor is written after its audit lines and journal entry."""
        why = envelope.ignored(
            mine=message.mine, kind=message.kind, thread=message.thread, body=message.body
        )
        if why is not None:
            self.audit.write("ignored", room=room.name, seq=message.seq, why=why)
            self.cursors.set(room.workspace, min(message.seq, page_cursor))
            return True
        body = message.body
        job_id = body["job_id"]
        entry = None
        loaded = None
        if not self.journal.seen(job_id):
            loaded = self._await_policy(room)
            if loaded is None or self.controls.draining.is_set():
                return False
            received_at = self.link.now()
            # None when the job id was taken between the two reads: a duplicate as well.
            entry = self.journal.received(
                job_id,
                room=room.workspace,
                seq=message.seq,
                received_at=received_at,
                not_after=envelope.valid_not_after(body, received_at),
                body=body,
            )
        if entry is None or loaded is None:
            self.audit.write(
                "ignored", room=room.name, seq=message.seq, job_id=job_id, why="duplicate"
            )
            self.cursors.set(room.workspace, min(message.seq, page_cursor))
            return True
        if not self._decide(room, job_id, body, entry, loaded):
            return False
        self.cursors.set(room.workspace, min(message.seq, page_cursor))
        return True

    def _afresh(self, item: _Afresh) -> bool:
        if self._halted(item.room) or self.controls.draining.is_set():
            return False
        loaded = self._await_policy(item.room)
        if loaded is None or self.controls.draining.is_set():
            return False
        return self._decide(item.room, item.job_id, item.entry.body, item.entry, loaded)

    def _context(
        self, room: _Room, loaded: tuple[policy.Effective, str], observe: Any
    ) -> jobs.Context:
        """The context a request is decided in: the policy the worker loaded for it, so that
        what was judged valid is what is used."""
        eff, digest = loaded
        return jobs.Context(
            os_family=paths.os_family(),
            policy=eff,
            policy_digest=digest,
            controls=self.controls,
            audit=self.audit,
            own_dirs=tuple(str(own) for own in self.dirs.own),
            stop=lambda: self._halted(room),
            observe=observe,
        )

    def _observer(self, job_id: str, body: dict[str, Any]) -> Callable[..., None] | None:
        """The request's ``request`` objects (and ``host_too_old``), each written right after the
        audit line it mirrors: never an absolute path, never output (spec/CLI.md section 6)."""
        if self.tell is None:
            return None
        op, root, path = _asked(body)

        def observe(phase: str, *values: Any) -> None:
            if phase == "received":
                self._tell("request", phase="received", job_id=job_id, op=op, root=root, path=path)
            elif phase == "decided":
                decision, code = values
                if decision == "run":
                    self._tell("request", phase="decided", job_id=job_id, decision="run")
                    return
                self._tell("request", phase="decided", job_id=job_id, decision="refused", code=code)
                if code == "host_too_old":
                    self._tell(
                        "host_too_old",
                        job_id=job_id,
                        min_host_version=body["min_host_version"],
                        host_version=__version__,
                    )
            elif phase == "started":
                self._tell("request", phase="started", job_id=job_id)
            elif phase == "finished":
                outcome, size = values
                self._tell("request", phase="finished", job_id=job_id, **_finished(outcome, size))

        return observe

    def _decide(
        self,
        room: _Room,
        job_id: str,
        body: dict[str, Any],
        entry: Entry,
        loaded: tuple[policy.Effective, str],
    ) -> bool:
        """Steps 2 to 9 of the refusal order and the run, then the reply to the poster; False
        when the request was left undecided (a drain) or its room abandoned."""
        early = envelope.early_code(
            body, received_at=entry.received_at, now=self.link.now(), host_version=__version__
        )
        ctx = self._context(room, loaded, self._observer(job_id, body))
        # A request over the size limit is refused, and its fields are not copied to the log.
        small = envelope.size(body) <= envelope.REQUEST_LIMIT
        job = jobs.Job(
            job_id=job_id,
            op=body.get("op") if small else None,
            args=body.get("args") if small else None,
            caps=body.get("caps") if early is None else None,
            seq=entry.seq,
        )

        def decided(decision: str, _code: str | None, data: bytes | None) -> None:
            self.journal.decided(job_id, decision, data)

        # Until its reply is with the poster: a drain waits for it.
        with self._cv:
            self._deciding = True
        try:
            try:
                data = jobs.process(job, ctx, earlier_code=early, decided=decided)
            except jobs.Draining:
                return False
            except jobs.Abandoned:
                self.audit.write(
                    "reply", event="not_posted", job_id=job_id, room=room.name, why="abandoned"
                )
                self._tell("request", phase="finished", job_id=job_id, status="abandoned")
                return False
            self._reply(room, job_id, data, entry.basis)
            return True
        finally:
            with self._cv:
                self._deciding = False
                self._cv.notify_all()

    def _reply(
        self, room: _Room, job_id: str, data: bytes, basis: float, *, restarted: bool = False
    ) -> None:
        if restarted:
            self.audit.sent(job_id, hashlib.sha256(data).hexdigest(), len(data))
        halted = self._halted(room)
        with self._cv:
            if not halted and room.name not in self._abandoned:
                self._replies.append(_Reply(job_id, room.name, room.workspace, data, basis))
                self._cv.notify_all()
                return
        self.audit.write(
            "reply", event="not_posted", job_id=job_id, room=room.name, why="abandoned"
        )

    # --- the poster ---------------------------------------------------------------------------

    def _post_loop(self) -> None:
        clients: dict[str, tuple[str, _switchboard.RoomClient]] = {}
        try:
            while True:
                with self._cv:
                    while True:
                        if self._threads_stop:
                            return
                        now = time.monotonic()
                        ready = [reply for reply in self._replies if reply.next_try <= now]
                        if ready:
                            reply = min(ready, key=lambda waiting: waiting.next_try)
                            break
                        soonest = min((r.next_try for r in self._replies), default=None)
                        self._cv.wait(None if soonest is None else max(0.01, soonest - now))
                self._attempt(reply, clients)
        finally:
            for _, client in clients.values():
                client.close()

    def _drop(self, reply: _Reply, why: str, **fields: Any) -> None:
        """Drop a reply waiting to be posted, and log it; nothing, when it is gone already (a
        room halted meanwhile dropped and logged it)."""
        with self._cv:
            if reply not in self._replies:
                return
            self._replies.remove(reply)
            self._cv.notify_all()
        self.audit.write(
            "reply", event="not_posted", job_id=reply.job_id, room=reply.room, why=why, **fields
        )

    def _attempt(
        self, reply: _Reply, clients: dict[str, tuple[str, _switchboard.RoomClient]]
    ) -> None:
        ttl = envelope.reply_ttl(reply.basis, self.link.now())
        if ttl is None:
            self._drop(reply, "ttl")
            return
        view = self._check(reply.room)
        if view is None or view.workspace != reply.workspace:
            self._drop(reply, "abandoned" if reply.room in self._abandoned else "room")
            if reply.room not in self._abandoned:
                self._event("room", reply.room)
            return
        kept = clients.get(reply.room)
        if kept is None or kept[0] != view.workspace:
            if kept is not None:
                kept[1].close()
            kept = (view.workspace, self.link.room_client(view))
            clients[reply.room] = kept
        started = self.link.now()
        try:
            posted = kept[1].post(_EXEC, json.loads(reply.data), ttl=ttl)
        except _switchboard.HubError as exc:
            self._hub_failed(exc)
            delay = reply.backoff.next(exc.retry_after)
            self.audit.write(
                "reply",
                event="retry",
                job_id=reply.job_id,
                room=reply.room,
                status=exc.status,
                after=round(delay, 3),
            )
            with self._cv:
                reply.next_try = time.monotonic() + delay
            return
        ended = self.link.now()
        self._contact()
        # The skew first, so that main never writes the flag cleared with no skew measured.
        if posted.created_at is not None:
            self._skew = round(posted.created_at - (started + ended) / 2, 3)
        self._skewed = False
        self._posted = True
        self.journal.sent(reply.job_id)
        with self._cv:
            if reply in self._replies:
                self._replies.remove(reply)
            self._cv.notify_all()
        self.audit.write(
            "reply", event="posted", job_id=reply.job_id, room=reply.room, ttl=ttl, seq=posted.seq
        )


def run(
    dirs: paths.Dirs,
    session: link.Session,
    *,
    controls: Controls,
    say: Callable[[str], None],
) -> str:
    """``lucille-host run``: see the module docstring. The command calls :meth:`Runner.serve`
    under its own run lock, with ``--json-events`` and the lifeline when asked."""
    return Runner(dirs, session, controls=controls, say=say).run()
