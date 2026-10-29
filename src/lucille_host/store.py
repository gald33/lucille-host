"""The keyring store: one item per profile, holding the pair and the keys of the work rooms.

- One OS-keyring item, service ``lucille-host`` and the profile's name as its user name, holds one
  JSON document: ``{"v": 1, "written_at": t, "pairs": {"records": {}, "pending": {},
  "verdicts": {}}, "shared": {}}``. The pair store and the share store the host gives every
  Switchboard call are two views of it (``_switchboard.py``). It is the only copy of the pair
  secret and of the rooms' keys, so an item that cannot be read is never overwritten.
- Three lock files in the state folder: ``.pairs.lock`` is the pair store's ``lock()``,
  ``.rooms.lock`` the share store's (Switchboard asks for two, and never nests them), and
  ``.store.lock`` is taken inside either, around each read and each read-modify-write of the
  item, and never across a call to the hub. Each is an ``os.lockf`` lock, which keeps other
  processes out, beside a ``threading.RLock``, which keeps the other threads of this one out:
  ``lockf`` belongs to the process, not the thread. There is one lock object per file per process,
  because closing any descriptor of a file releases the process's lock on it.
- Saves that change nothing but when the pair or a room was last read or used (the stamps listed
  in :data:`PAIR_STAMPS` and :data:`ROOM_STAMPS`) are skipped while the item was written less
  than :data:`STAMP_EVERY` seconds ago. Switchboard saves the pair on every resolve and touch, and
  the rooms on every look-up of a room, which would otherwise be thousands of keyring writes a
  day. Those stamps only decide whether a pair has gone unread for days, and what is shown about
  a room, so an hour's delay changes neither. Every other change, and every delete, is written
  before the call returns, as Switchboard's store contract asks.
- The host refuses a keyring backend it can recognise as storing nothing: keyring's ``fail`` and
  ``null`` backends, and a chain of backends with nothing in it.

A keyring failure is a :class:`StoreError`, an ``OSError``, which is what Switchboard's stores
raise for a storage failure. No error message holds anything read from the item.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final, TypeVar

import keyring
from keyring.backends.chainer import ChainerBackend
from keyring.backends.fail import Keyring as FailKeyring
from keyring.backends.null import Keyring as NullKeyring
from keyring.errors import KeyringError

from lucille_host import paths, state

__all__ = [
    "PAIR_STAMPS",
    "ROOM_STAMPS",
    "SERVICE",
    "STAMP_EVERY",
    "InsecureKeyring",
    "Item",
    "StoreError",
    "check_backend",
    "stamps_only",
]

SERVICE: Final = "lucille-host"
VERSION: Final = 1
STAMP_EVERY: Final = 3600.0
# What Switchboard's pair records and held rooms stamp when they are read or used.
PAIR_STAMPS: Final = ("room_read_at", "peer_at", "peer_seen_at", "last_used_at")
ROOM_STAMPS: Final = ("last_used_at", "refreshed_at", "refresh_tried_at")
PAIRS_LOCK: Final = ".pairs.lock"
ROOMS_LOCK: Final = ".rooms.lock"
ITEM_LOCK: Final = ".store.lock"
TABLES: Final = ("records", "pending", "verdicts")

T = TypeVar("T")


class StoreError(OSError):
    """The keyring item could not be read or written."""


class InsecureKeyring(state.Refused):
    """The keyring backend in use stores nothing."""


def _stores_nothing(backend: object) -> str | None:
    """Why ``backend`` cannot keep a secret, or None when it is not one this module knows."""
    if isinstance(backend, FailKeyring):
        return "has no keyring to store in"
    if isinstance(backend, NullKeyring):
        return "stores nothing"
    if isinstance(backend, ChainerBackend):
        members = list(backend.backends)
        if not members:
            return "chains no keyring"
        for member in members:
            reason = _stores_nothing(member)
            if reason is not None:
                return f"chains one that {reason}"
    return None


def check_backend(backend: object | None = None) -> None:
    """Refuse the keyring backend in use (or ``backend``) when it stores nothing."""
    current = keyring.get_keyring() if backend is None else backend
    reason = _stores_nothing(current)
    if reason is not None:
        raise InsecureKeyring(
            f"the keyring backend here ({type(current).__name__}) {reason}, so lucille-host "
            "cannot keep its pair in it; set up the system keyring, or pair with --store-file "
            "to keep the pair in a file instead, which is weaker"
        )


class _FileLock:
    """An ``os.lockf`` lock on one file, with a re-entrant thread lock beside it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._guard = threading.RLock()
        self._depth = 0
        self._descriptor: int | None = None

    @contextlib.contextmanager
    def held(self) -> Iterator[None]:
        with self._guard:
            if self._depth == 0:
                descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    os.lockf(descriptor, os.F_LOCK, 0)
                except OSError as exc:
                    os.close(descriptor)
                    raise StoreError(exc.errno, f"{self._path} cannot be locked") from exc
                self._descriptor = descriptor
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._descriptor is not None:
                    descriptor, self._descriptor = self._descriptor, None
                    os.close(descriptor)


_LOCKS: dict[str, _FileLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> _FileLock:
    key = os.path.realpath(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = _FileLock(Path(key))
        return lock


def _empty() -> dict[str, Any]:
    return {"v": VERSION, "written_at": None, "pairs": {t: {} for t in TABLES}, "shared": {}}


def _without_stamps(doc: dict[str, Any]) -> dict[str, Any]:
    bare = copy.deepcopy(doc)
    bare.pop("written_at", None)
    for record in bare["pairs"]["records"].values():
        if isinstance(record, dict):
            for stamp in PAIR_STAMPS:
                record.pop(stamp, None)
    rooms = bare["shared"].get("rooms")
    if isinstance(rooms, dict):
        for room in rooms.values():
            if isinstance(room, dict):
                for stamp in ROOM_STAMPS:
                    room.pop(stamp, None)
    return bare


def stamps_only(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Whether two documents differ in nothing but the stamps."""
    return _without_stamps(before) == _without_stamps(after)


class Item:
    """One profile's keyring item, and the locks around it."""

    def __init__(
        self,
        lock_dir: Path,
        username: str,
        *,
        service: str = SERVICE,
        clock: Callable[[], float] = time.time,
        stamp_every: float = STAMP_EVERY,
    ) -> None:
        paths.ensure_dir(lock_dir)
        self.service = service
        self.username = username
        self._clock = clock
        self._stamp_every = stamp_every
        self._pairs = _lock_for(lock_dir / PAIRS_LOCK)
        self._rooms = _lock_for(lock_dir / ROOMS_LOCK)
        self._item = _lock_for(lock_dir / ITEM_LOCK)

    def pairs_lock(self) -> contextlib.AbstractContextManager[None]:
        return self._pairs.held()

    def rooms_lock(self) -> contextlib.AbstractContextManager[None]:
        return self._rooms.held()

    def read(self) -> dict[str, Any]:
        """The whole document, as a copy of its own."""
        with self._item.held():
            return self._load()

    def change(self, edit: Callable[[dict[str, Any]], T]) -> T:
        """Apply ``edit`` to the document and save the result, under the item's lock."""
        with self._item.held():
            before = self._load()
            after = copy.deepcopy(before)
            result = edit(after)
            self._save(before, after)
            return result

    def _load(self) -> dict[str, Any]:
        try:
            raw = keyring.get_password(self.service, self.username)
        except KeyringError as exc:
            raise StoreError(f"the keyring could not be read ({type(exc).__name__})") from exc
        if raw is None:
            return _empty()
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
        pairs = doc.get("pairs") if isinstance(doc, dict) else None
        if (
            not isinstance(doc, dict)
            or doc.get("v") != VERSION
            or not isinstance(pairs, dict)
            or not all(isinstance(pairs.get(table), dict) for table in TABLES)
            or not isinstance(doc.get("shared"), dict)
        ):
            raise StoreError(
                f"the keyring item {self.service}/{self.username} is not one this version "
                "wrote; it may hold the only copy of the pair secret, so it is left as it is"
            )
        return doc

    def _save(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        if after == before:
            return
        now = self._clock()
        written = before.get("written_at")
        if (
            isinstance(written, int | float)
            and 0 <= now - written < self._stamp_every
            and stamps_only(before, after)
        ):
            return
        after["written_at"] = now
        text = json.dumps(after, sort_keys=True, separators=(",", ":"))
        try:
            keyring.set_password(self.service, self.username, text)
        except KeyringError as exc:
            raise StoreError(f"the keyring could not be written ({type(exc).__name__})") from exc
