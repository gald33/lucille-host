"""The user's local controls: pause, resume, kill, and drain (spec/PROTOCOL.md section 16).

- ``pause`` writes a flag file in the state directory. While it is set the host starts nothing; a
  running host refuses each new request ``paused``.
- ``kill`` sets the pause flag and writes a kill request. The running host polls for it while a
  child runs, kills that child's process group, and replies ``failed killed_locally``. The request
  is consumed when it kills something; ``resume`` clears it too, so a kill sent while nothing ran
  cannot reach a later operation.
- ``resume`` clears the pause flag and any kill request. Nothing Lucille sends can unpause a host:
  only these files, which only the local user's CLI writes, carry the state.
- The first SIGTERM, SIGHUP (the terminal closed) or Ctrl-C drains: the operation in flight
  finishes and replies, and nothing new starts. A second Ctrl-C kills, as ``kill`` does. A signal
  that was ignored when the handlers were installed stays ignored, so a host started with
  ``nohup`` keeps running when its terminal closes. The handler takes no lock: it runs between two
  steps of the main thread, which may hold one.

A ``kill`` and a ``resume`` run at once must never leave a kill request without the pause flag:
a later operation would meet it and be killed. Each verb holds a lock file in the state directory
while it writes, and the order of the writes keeps that true without the lock too, so on a
filesystem that cannot lock (some network mounts) all three go ahead without it and say so.
``kill`` writes the pause flag, the kill request, then the pause flag again; ``resume`` removes the
kill request, then the pause flag, then looks again and, if a kill request came meanwhile, writes
the pause flag back. For a kill request to be left, it must be written after ``resume`` removed it;
for the pause flag to be missing, ``resume`` must remove it after ``kill``'s last write of it,
which comes after the kill request; ``resume`` then looks again after that and finds it.

Only a running host enforces a time limit or a kill. If the host is killed or crashes while an
operation runs, the operation keeps running without either (THREAT_MODEL.md, "Known gaps").
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

__all__ = ["Controls"]

PAUSE_FILE: Final = "paused"
KILL_FILE: Final = "kill-request"
LOCK_FILE: Final = ".controls.lock"


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def _present(path: Path) -> bool:
    return path.exists()


def _write_flag(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, f"{int(time.time())}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _Flag:
    """A flag with an Event's methods and no lock, so that a signal handler can set it. A
    handler runs between two steps of the main thread, which may be inside an Event's own lock,
    and would then wait on it forever. Nothing waits on these flags; they are only read."""

    def __init__(self) -> None:
        self._value = False

    def set(self) -> None:
        self._value = True

    def clear(self) -> None:
        self._value = False

    def is_set(self) -> bool:
        return self._value


class Controls:
    """The pause flag, kill requests and drain state of one profile."""

    def __init__(self, state_dir: Path) -> None:
        self._pause_file = state_dir / PAUSE_FILE
        self._kill_file = state_dir / KILL_FILE
        self._lock_file = state_dir / LOCK_FILE
        self._kill = _Flag()
        self.draining = _Flag()

    # The CLI verbs.

    @contextlib.contextmanager
    def _locked(self) -> Iterator[bool]:
        """Hold the controls' lock; yields whether it is held (False where the filesystem cannot
        lock)."""
        descriptor = os.open(self._lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                os.lockf(descriptor, os.F_LOCK, 0)
            except OSError:
                yield False
                return
            yield True
        finally:
            os.close(descriptor)

    # Each returns whether it ran under the lock. The order of the writes is the module
    # docstring's: it is what keeps a kill request from standing alone without the lock.

    def pause(self) -> bool:
        with self._locked() as locked:
            _write_flag(self._pause_file)
        return locked

    def resume(self) -> bool:
        with self._locked() as locked:
            _remove(self._kill_file)
            self._kill.clear()
            _remove(self._pause_file)
            if _present(self._kill_file):
                _write_flag(self._pause_file)
        return locked

    def kill(self) -> bool:
        with self._locked() as locked:
            _write_flag(self._pause_file)
            _write_flag(self._kill_file)
            _write_flag(self._pause_file)
        return locked

    # What the running host reads.

    def paused(self) -> bool:
        return self._pause_file.exists()

    def may_start(self) -> bool:
        """Whether the host may start new work: not draining and not paused."""
        return not self.draining.is_set() and not self.paused()

    def kill_requested(self) -> bool:
        return self._kill.is_set() or self._kill_file.exists()

    def kill_done(self) -> None:
        """A kill request was carried out: consume it. The pause flag stays set."""
        self._kill_file.unlink(missing_ok=True)
        self._kill.clear()

    # Signals.

    def on_signal(self, signum: int, _frame: Any) -> None:
        """The first SIGTERM, SIGHUP or SIGINT drains; a SIGINT while draining kills."""
        if signum == signal.SIGINT and self.draining.is_set():
            # Not under the lock: the kill is the event, and the flag is only written so that the
            # host stays paused. A lock belongs to the process, so closing the handler's own
            # descriptor would release one the interrupted code holds.
            _write_flag(self._pause_file)
            self._kill.set()
        self.draining.set()

    def install_handlers(self) -> Callable[[], None]:
        """Handle SIGTERM, SIGHUP and SIGINT (from the main thread), except one that is ignored
        (as ``nohup`` ignores SIGHUP). Returns a function that puts the previous handlers back."""
        previous = {
            signum: signal.signal(signum, self.on_signal)
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
            if signal.getsignal(signum) is not signal.SIG_IGN
        }

        def restore() -> None:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

        return restore
