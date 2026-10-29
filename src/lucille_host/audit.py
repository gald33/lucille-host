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

The files rotate by size (``audit.log`` becomes ``audit.log.1``, and so on, keeping
:data:`KEEP` old files) and are never edited. No key is ever written. The log is user-writable: it
records, and proves nothing against someone with the user's privileges. The host never derives its
dedupe or restart state from it, because it rotates.
"""

from __future__ import annotations

import datetime
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

__all__ = ["KEEP", "MAX_BYTES", "AuditLog"]

MAX_BYTES: Final = 8 * 1024 * 1024
KEEP: Final = 5
FILE_NAME: Final = "audit.log"


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
