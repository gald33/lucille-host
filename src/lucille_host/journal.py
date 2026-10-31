"""The host's own record of each request it read: ``state/journal.json``.

spec/PROTOCOL.md section 15. One journal per profile, across every work room, so a job id posted
again in a second room gets no reply. It is the host's dedupe state and its restart state, and is
never derived from the audit log, which rotates. Each entry, by ``job_id``, holds:

- ``room`` (the workspace it was read in) and ``seq``, and ``received_at``;
- ``not_after``: the request's valid ``not_after``, or null when it had none (the reply's TTL and
  the prune time depend on it);
- ``prune_at``: when the entry may go (``envelope.prune_at``);
- ``body``: the request as received, kept only until it is decided and only when it measures at
  most 16 KiB, so that a request received but not decided before a stop can be decided afresh;
- ``decision``: null, ``run`` or ``refused``;
- ``reply``: for a refusal, the exact bytes of the reply, until it is sent, so that a restart posts
  the same refusal again;
- ``sent``: whether the hub accepted the reply;
- ``abandoned``: whether its room was abandoned, after which nothing is posted for it.

The entry is written before the request is decided (before the audit log's ``decision`` line), the
decision right after that line and before anything runs, and ``sent`` once the hub has accepted the
reply. Writing the entry also takes the job id: when the profile has an entry for it already,
nothing is written, and the request is a duplicate. Every write replaces the file whole
(``state.write_json``) and first drops the entries past their prune time; a change to an entry
another write has pruned already changes nothing.

Every read, and every read-modify-write, holds ``.journal.lock`` in the same folder: an
``os.lockf`` lock beside a re-entrant thread lock (``store.file_lock``), so that neither another
thread nor another process writes between the read and the write. ``run`` is the only writer, and a
profile runs one ``run`` at a time (``loop.py``); the lock keeps dedupe whole even if a second
process writes the journal.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final

from lucille_host import envelope, state, store

__all__ = ["DECISIONS", "LOCK", "Entry", "Journal"]

VERSION: Final = 1
# Beside the journal: held around each read and each read-modify-write of it.
LOCK: Final = ".journal.lock"
DECISIONS: Final = ("run", "refused")
_KEYS: Final = frozenset(
    {
        "room",
        "seq",
        "received_at",
        "not_after",
        "prune_at",
        "body",
        "decision",
        "reply",
        "sent",
        "abandoned",
    }
)


@dataclass(frozen=True)
class Entry:
    room: str
    seq: int
    received_at: float
    not_after: int | None
    prune_at: float
    body: Any = None
    decision: str | None = None
    reply: str | None = None
    sent: bool = False
    abandoned: bool = False

    @property
    def basis(self) -> float:
        """What the reply's TTL counts from: the valid ``not_after``, else the receipt."""
        return self.received_at if self.not_after is None else self.not_after


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entry(data: Any) -> Entry:
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise ValueError
    valid = (
        isinstance(data["room"], str)
        and _integer(data["seq"])
        and _number(data["received_at"])
        and (data["not_after"] is None or _integer(data["not_after"]))
        and _number(data["prune_at"])
        and (data["decision"] is None or data["decision"] in DECISIONS)
        and (data["reply"] is None or isinstance(data["reply"], str))
        and isinstance(data["sent"], bool)
        and isinstance(data["abandoned"], bool)
    )
    if not valid:
        raise ValueError
    return Entry(**data)


class Journal:
    """The journal of one profile."""

    def __init__(self, path: Path, *, clock: Callable[[], float]) -> None:
        self.path = path
        self._clock = clock

    def _lock(self) -> contextlib.AbstractContextManager[None]:
        return store.file_lock(self.path.parent / LOCK)

    def _load(self) -> dict[str, Entry]:
        data = state.read_json(self.path)
        if data is None:
            return {}
        if not isinstance(data, dict) or set(data) != {"v", "jobs"} or data["v"] != VERSION:
            raise state.StateError(f"{self.path} is not a lucille-host journal")
        jobs = data["jobs"]
        if not isinstance(jobs, dict):
            raise state.StateError(f"{self.path} is not a lucille-host journal")
        try:
            return {job_id: _entry(entry) for job_id, entry in jobs.items()}
        except ValueError:
            raise state.StateError(f"{self.path} is not a lucille-host journal") from None

    def _save(self, entries: dict[str, Entry]) -> None:
        now = self._clock()
        kept = {job_id: e for job_id, e in entries.items() if e.prune_at >= now}
        state.write_json(
            self.path,
            {
                "v": VERSION,
                "jobs": {job_id: asdict(entry) for job_id, entry in kept.items()},
            },
        )

    def _change(self, job_id: str, **fields: Any) -> None:
        with self._lock():
            entries = self._load()
            entry = entries.get(job_id)
            if entry is None:
                # Pruned by another thread's write: past its prune time, the request is settled,
                # and nothing more is posted for it (a reply's TTL runs out first).
                return
            entries[job_id] = replace(entry, **fields)
            self._save(entries)

    def get(self, job_id: str) -> Entry | None:
        with self._lock():
            return self._load().get(job_id)

    def seen(self, job_id: str) -> bool:
        """Whether this profile has an entry for ``job_id``: a request it read already."""
        return self.get(job_id) is not None

    def received(
        self,
        job_id: str,
        *,
        room: str,
        seq: int,
        received_at: float,
        not_after: int | None,
        body: Any,
    ) -> Entry | None:
        """Write the entry of a request about to be decided, and return it; None, writing
        nothing, when the profile has an entry for ``job_id`` already (a duplicate). The look and
        the write are one step under the journal's lock, so that no two threads or processes
        both take one job id."""
        kept = body if envelope.size(body) <= envelope.REQUEST_LIMIT else None
        entry = Entry(
            room=room,
            seq=seq,
            received_at=received_at,
            not_after=not_after,
            prune_at=envelope.prune_at(not_after, received_at),
            body=kept,
        )
        with self._lock():
            entries = self._load()
            if job_id in entries:
                return None
            entries[job_id] = entry
            self._save(entries)
        return entry

    def decided(self, job_id: str, decision: str, reply: bytes | None) -> None:
        """Record the decision; for a refusal, the reply's bytes. The body is no longer kept."""
        if decision not in DECISIONS:
            raise ValueError(f"no decision {decision!r}")
        text = reply.decode("ascii") if decision == "refused" and reply is not None else None
        self._change(job_id, decision=decision, reply=text, body=None)

    def sent(self, job_id: str) -> None:
        """The hub accepted the reply: nothing more is posted for this job."""
        self._change(job_id, sent=True, reply=None)

    def abandon_room(self, room: str) -> list[str]:
        """Mark abandoned every entry read in workspace ``room`` whose reply was not sent:
        nothing more is posted for them. Returns the job ids marked."""
        with self._lock():
            entries = self._load()
            marked = [
                job_id
                for job_id, entry in entries.items()
                if entry.room == room and not entry.abandoned and not entry.sent
            ]
            if marked:
                for job_id in marked:
                    entries[job_id] = replace(entries[job_id], abandoned=True)
                self._save(entries)
            return marked

    def pending(self, room: str | None = None) -> list[tuple[str, Entry]]:
        """Entries with no reply sent and a room not abandoned, in ``seq`` order: all of them,
        or those read in workspace ``room``."""
        with self._lock():
            entries = self._load()
        found = [
            (job_id, entry)
            for job_id, entry in entries.items()
            if not entry.sent and not entry.abandoned and (room is None or entry.room == room)
        ]
        return sorted(found, key=lambda item: item[1].seq)
