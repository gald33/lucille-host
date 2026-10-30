"""The only place in the runtime package that may start a process.

Every operation that runs an external command does it through :func:`run`, and nowhere else:
the AST lint (``tools/guards/ast_lint.py``) fails CI on a process-spawning call or import in
``src/lucille_host/`` outside this file, in the forms it lists and through the re-exports it
follows, including a name taken from this module that stands for a launcher. In this file it
requires anything that starts a process to be called directly, so that nothing leaves it under
another name; what this module exports is its own functions. The conformance package,
``src/lucille_host/conformance/``, is not part of the runtime and has its own, narrower rule.

The rules :func:`run` follows:

- argv only, never a shell; the executable is an absolute path, taken by the caller from the
  release's catalog, and its sha256 is recorded (not enforced: OS updates change it, and for a
  script it does not cover the interpreter);
- the caller puts every string that came from a request after ``--`` (the catalog's templates);
- stdin is ``/dev/null``, and the child's environment is :data:`CHILD_ENV`, never copied from the
  host's own;
- the child runs in its own session and process group, and that whole group is killed on timeout
  or when the user kills it; once the child has ended, whatever is left in its group is killed
  too;
- output is read as it comes, by one reader thread per stream (a selector would need a method the
  Switchboard-surface lint refuses everywhere), keeping at most ``_output.RAW_CEILING`` bytes of
  each stream, and is then decoded, redacted and capped by ``_output.render``.

A process that leaves the child's process group (by calling ``setpgid`` or ``setsid`` itself) is
out of reach: it is not killed, and if it keeps a stream open the host stops waiting for its
streams :data:`STREAM_GRACE_S` after the child's group is gone, and treats each one still open as
cut short. Containing that needs the OS's
own tools (cgroups, sandbox profiles), which this release does not use. A killed child stuck in
the kernel (on a stalled network mount, say) is waited for :data:`KILL_WAIT_S` seconds and then
left; its exit code is then unknown. The sweep of the group after a normal exit runs once the
child is reaped, so in principle its process-group id could be reused in between; a group id
stays in use while any member of the group lives, so that needs the group to be empty already.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import IO, Final

from lucille_host import _output

__all__ = ["CHILD_ENV", "Command", "NotStarted", "Result", "run"]

# The child's whole environment. A fixed PATH, and the C locale so that output does not depend on
# the user's language settings.
CHILD_ENV: Final = MappingProxyType({"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})

# How often the wait loop checks the time limit and for a kill.
POLL_S: Final = 0.05
# How long the host waits for a stream to close once the child's process group is gone.
STREAM_GRACE_S: Final = 2.0
# How long the host waits for a child it killed to end.
KILL_WAIT_S: Final = 10.0
_READ_CHUNK: Final = 65536
_MAX_DURATION_MS: Final = 2147483647


@dataclass(frozen=True)
class Command:
    """One command to run. ``argv[0]`` is an absolute executable; ``cwd`` an absolute directory."""

    argv: tuple[str, ...]
    cwd: str
    timeout_s: float
    stdout_cap: int = _output.STREAM_CAP
    stderr_cap: int = _output.STREAM_CAP


@dataclass(frozen=True)
class Result:
    """How a command ended, and its output as it goes in a reply."""

    # "exited", "timeout" (the time limit killed it) or "killed" (the user killed it).
    ended: str
    # As the OS reported it: negative when a signal ended the child. None when a child the host
    # killed had still not ended KILL_WAIT_S seconds later.
    exit_code: int | None
    stdout: str
    stdout_truncated: bool
    stderr: str
    stderr_truncated: bool
    duration_ms: int
    executable_sha256: str
    # Raw bytes read from each stream, kept or not.
    stdout_bytes: int
    stderr_bytes: int


class NotStarted(Exception):
    """The process could not be started: reading the executable or starting it failed."""

    def __init__(self, cause: OSError) -> None:
        super().__init__(f"the process could not be started (errno {cause.errno})")
        self.errno = cause.errno


class _Reader(threading.Thread):
    """Reads one stream to its end, keeping at most RAW_CEILING bytes and dropping the rest."""

    def __init__(self, stream: IO[bytes]) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._lock = threading.Lock()
        self._chunks: list[bytes] = []
        self._kept = 0
        self._total = 0
        self._dropped = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read1(_READ_CHUNK)  # type: ignore[attr-defined]
                if not chunk:
                    return
                with self._lock:
                    self._total += len(chunk)
                    room = _output.RAW_CEILING - self._kept
                    if len(chunk) > room:
                        self._dropped = True
                        chunk = chunk[: max(room, 0)]
                    if chunk:
                        self._chunks.append(chunk)
                        self._kept += len(chunk)
        except (OSError, ValueError):
            with self._lock:
                self._dropped = True

    def taken(self) -> tuple[bytes, bool, int]:
        """What was kept, whether anything was dropped (or the stream not read to its end), and
        how many bytes were read."""
        with self._lock:
            return b"".join(self._chunks), self._dropped or self.is_alive(), self._total


def _never() -> bool:
    return False


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Nothing is left in the group (or only processes the host may no longer signal).
        pass


def _check(command: Command) -> None:
    if not command.argv or not os.path.isabs(command.argv[0]):
        raise ValueError("the executable must be an absolute path")
    if not os.path.isabs(command.cwd):
        raise ValueError("the working directory must be an absolute path")
    if not 0 < command.timeout_s:
        raise ValueError("the time limit must be positive")
    if command.stdout_cap < 2 or command.stderr_cap < 2:
        raise ValueError("a cap is at least 2")


def _reap(child: subprocess.Popen[bytes]) -> None:
    """Wait for a killed child, but not forever: a process stuck in the kernel (on a stalled
    network mount, say) cannot die until the kernel lets it go."""
    try:
        child.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def _wait(
    child: subprocess.Popen[bytes], deadline: float, kill_requested: Callable[[], bool]
) -> str:
    """Wait for the child to exit, the deadline, or a kill. Returns how it ended."""
    while True:
        try:
            child.wait(timeout=POLL_S)
            return "exited"
        except subprocess.TimeoutExpired:
            pass
        if kill_requested():
            ended = "killed"
        elif time.monotonic() >= deadline:
            ended = "timeout"
        else:
            continue
        _kill_group(child.pid)
        _reap(child)
        return ended


def run(
    command: Command,
    *,
    kill_requested: Callable[[], bool] = _never,
    before_start: Callable[[str], None] | None = None,
) -> Result:
    """Run one catalog command to its end, its time limit, or a kill.

    ``before_start`` is called with the executable's sha256 just before the process starts (the
    audit log's ``start`` line); if it raises, nothing starts and the exception goes on.
    ``kill_requested`` is polled every :data:`POLL_S` seconds while
    the child runs; when it returns True, the child's process group is killed and the result says
    ``killed``. If the process cannot be started (the executable or the folder gone, say), this
    raises :class:`NotStarted`, and nothing ran. If anything raises once the child has started, its
    group is killed before the exception goes on unchanged, so no child outlives this call.
    """
    _check(command)
    argv: Sequence[str] = list(command.argv)
    try:
        executable_sha256 = _sha256_file(command.argv[0])
    except OSError as exc:
        raise NotStarted(exc) from exc
    if before_start is not None:
        before_start(executable_sha256)
    started = time.monotonic()
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=command.cwd,
            env=dict(CHILD_ENV),
            start_new_session=True,
            close_fds=True,
            shell=False,
        )
    except OSError as exc:
        raise NotStarted(exc) from exc
    try:
        assert child.stdout is not None and child.stderr is not None
        readers = (_Reader(child.stdout), _Reader(child.stderr))
        for reader in readers:
            reader.start()
        ended = _wait(child, started + command.timeout_s, kill_requested)
    except BaseException:
        _kill_group(child.pid)
        _reap(child)
        raise
    finished = time.monotonic()
    # Whatever the child left in its process group goes too, so it cannot hold a stream open.
    _kill_group(child.pid)
    grace_ends = time.monotonic() + STREAM_GRACE_S
    for reader in readers:
        reader.join(max(grace_ends - time.monotonic(), 0.0))
    for reader, stream in zip(readers, (child.stdout, child.stderr), strict=True):
        if not reader.is_alive():
            stream.close()
    out_raw, out_dropped, out_total = readers[0].taken()
    err_raw, err_dropped, err_total = readers[1].taken()
    stdout, stdout_truncated = _output.render(out_raw, command.stdout_cap, dropped=out_dropped)
    stderr, stderr_truncated = _output.render(err_raw, command.stderr_cap, dropped=err_dropped)
    duration_ms = min(max(int((finished - started) * 1000), 0), _MAX_DURATION_MS)
    return Result(
        ended=ended,
        exit_code=child.returncode,
        stdout=stdout,
        stdout_truncated=stdout_truncated,
        stderr=stderr,
        stderr_truncated=stderr_truncated,
        duration_ms=duration_ms,
        executable_sha256=executable_sha256,
        stdout_bytes=out_total,
        stderr_bytes=err_total,
    )
