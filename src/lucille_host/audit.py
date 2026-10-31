"""The audit log: append-only JSONL, mode 0600, in the state directory.

For each request it records these lines, each written, flushed and synced before the host acts on
what it records:

- ``received``: the job id, the hub sequence number, the op, and the args as received;
- ``decision``: ``run``, or ``refused`` with its code;
- ``start``, for an operation that runs a process: the executable and its sha256, and the resolved
  argv, before the process starts, so that a crash mid-run still leaves a record of what ran;
- ``execution``: how the operation ended, its exit code, duration and output sizes, with the
  executable, its sha256 and the argv again (output bodies only when the policy sets
  ``log_results = true``);
- ``sent``: the sha256 and size of the exact bytes about to be posted.

The read loop (``loop.py``) adds lines of its own: what it ignored and why, a message it could not
open, a room it could read no further in, each reply posted or not posted, and the pair's and the
rooms' events (``link.py``). No line holds the pairing code, the SAS, or a key.

The files rotate by size (``audit.log`` becomes ``audit.log.1``, and so on, keeping
:data:`KEEP` old files) and are never edited. One lock, for every log in the process, keeps the
read loop's threads from writing, or rotating, at once. No key is ever written. The log is
user-writable: it records, and proves nothing against someone with the user's privileges. The host
never derives its dedupe or restart state from it, because it rotates. ``lucille-host log`` prints
its last lines, across the rotated files (:func:`tail`).
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

__all__ = ["FILE_NAME", "KEEP", "MAX_BYTES", "AuditLog", "tail"]

MAX_BYTES: Final = 8 * 1024 * 1024
KEEP: Final = 5
FILE_NAME: Final = "audit.log"

# Held while a line is written, by every log in the process: the read loop writes from several
# threads, and a size check, a rotation and an append must not interleave.
_WRITING = threading.Lock()


def _sync_dir(path: Path) -> None:
    """Sync a directory, so that a file created or renamed in it survives a crash."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


class AuditLog:
    """One profile's audit log."""

    def __init__(
        self,
        state_dir: Path,
        *,
        max_bytes: int = MAX_BYTES,
        keep: int = KEEP,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.path = state_dir / FILE_NAME
        self._max_bytes = max_bytes
        self._keep = keep
        self._clock = clock

    def _rotate(self) -> None:
        for index in range(self._keep - 1, 0, -1):
            older = self.path.with_name(f"{FILE_NAME}.{index}")
            if older.exists():
                os.replace(older, self.path.with_name(f"{FILE_NAME}.{index + 1}"))
        os.replace(self.path, self.path.with_name(f"{FILE_NAME}.1"))
        _sync_dir(self.path.parent)

    def write(self, kind: str, **fields: Any) -> None:
        """Append one line and sync it to disk before returning."""
        line = json.dumps(
            {"ts": self._clock(), "kind": kind, **fields},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        line += b"\n"
        with _WRITING:
            self._append(line)

    def _append(self, line: bytes) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(line) > self._max_bytes:
            self._rotate()
            size = 0
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(line)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if not size:
            # A new file: its directory entry must reach the disk too.
            _sync_dir(self.path.parent)

    def received(self, job_id: str, seq: int | None, op: str, args: Any) -> None:
        self.write("received", job_id=job_id, seq=seq, op=op, args=args)

    def decision(self, job_id: str, decision: str, code: str | None = None) -> None:
        if code is None:
            self.write("decision", job_id=job_id, decision=decision)
        else:
            self.write("decision", job_id=job_id, decision=decision, code=code)

    def start(self, job_id: str, **fields: Any) -> None:
        self.write("start", job_id=job_id, **fields)

    def execution(self, job_id: str, **fields: Any) -> None:
        self.write("execution", job_id=job_id, **fields)

    def sent(self, job_id: str, sha256: str, size: int) -> None:
        self.write("sent", job_id=job_id, sha256=sha256, size=size)


def tail(state_dir: Path, limit: int, *, keep: int = KEEP) -> list[str]:
    """The last ``limit`` whole lines of the profile's audit log, oldest first, read across the
    rotated files, newest file first. A line still being written (no newline yet) is left out.
    Each byte outside printable ASCII is shown as ``?``: the log writes only escaped ASCII, so
    anything else was put there by another hand, and must not reach a terminal as it is."""
    found: list[str] = []
    names = [FILE_NAME] + [f"{FILE_NAME}.{index}" for index in range(1, keep + 1)]
    for name in names:
        if len(found) >= limit:
            break
        try:
            data = (state_dir / name).read_bytes()
        except FileNotFoundError:
            continue
        lines = [_printable(line) for line in data.split(b"\n")[:-1]]
        found = lines[max(0, len(lines) - (limit - len(found))) :] + found
    return found


def _printable(line: bytes) -> str:
    return "".join(chr(byte) if 0x20 <= byte < 0x7F else "?" for byte in line)
