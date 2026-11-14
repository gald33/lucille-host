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
  because closing any descriptor of a file releases the process's lock on it. Since the kernel sees
  processes, not threads, it can report a deadlock (``EDEADLK``) that is none: one thread of
  ``run`` inside the item's lock, another waiting for the pair store's lock, which a command holds
  while it waits for the item's lock. The thread inside lets go without waiting on anything, since
  no lock is ever taken while the item's lock is held, so the host asks again after a pause. A
  signal ``run`` handles does not end a wait for any of them either (``state.wait_for_lock``): a
  stop signal that arrives while ``run`` waits for a command is kept, and ``run`` stops once the
  command lets go (``loop.py`` says how).
- Saves that change nothing but when the pair or a room was last read or used (the stamps listed
  in :data:`PAIR_STAMPS` and :data:`ROOM_STAMPS`) are skipped while the item was written less
  than :data:`STAMP_EVERY` seconds ago. Switchboard saves the pair on every resolve and touch, and
  the rooms on every look-up of a room, which would otherwise be thousands of keyring writes a
  day. Those stamps only decide whether a pair has gone unread for days, and what is shown about
  a room, so an hour's delay changes neither. Every other change, and every delete, is written
  before the call returns, as Switchboard's store contract asks.
- The host keeps the item in one keyring backend on each system, and knows it by its class:
  keyring's Secret Service backend, ``keyring.backends.SecretService.Keyring``, on Linux, and its
  Keychain backend, ``keyring.backends.macOS.Keyring``, on macOS (:data:`KNOWN_BACKENDS`). The class
  of the backend in use must be that one exactly, as its ``(__module__, __qualname__)``, compared as
  strings, so neither module is ever imported here. Every other backend is refused: keyring's
  ``fail`` and ``null`` backends, a subclass of the known class, a class under any other name, one
  that writes secrets in the clear to a file among them, and every chain of backends (keyring's
  ``ChainerBackend``), whatever it holds: a chain is asked only whether it holds any backend, and
  its members are neither judged nor named. :func:`check_backend` refuses them when the store is
  opened, and again before each read and each write of the item, which go to the backend it checked.
  What is checked is the class's identity, not how the backend keeps a secret: which service answers
  on the session bus, and whether the collection that keeps the item is encrypted, are not checked;
  nor are the settings keyring gives a backend from ``KEYRING_PROPERTY_*`` environment variables
  (the Secret Service collection, a Keychain file); and code already running in the process could
  give a class of its own those names. Before the check sees a backend, keyring has already imported
  and built the one that an environment variable or its configuration file names, or, with neither,
  imported every backend its installed entry points name and built every one it can use.

A keyring failure, a backend's own ``OSError`` included, is a :class:`StoreError`, an ``OSError``,
which is what Switchboard's stores raise for a storage failure; the wrapper never reads one as the
hub's (``_switchboard.py``). No error message holds anything read from the item.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeVar

import keyring
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.backends.fail import Keyring as FailKeyring
from keyring.backends.null import Keyring as NullKeyring
from keyring.errors import KeyringError

from lucille_host import paths, state

__all__ = [
    "KNOWN_BACKENDS",
    "PAIR_REFUSAL",
    "PAIR_STAMPS",
    "ROOM_STAMPS",
    "SERVICE",
    "STAMP_EVERY",
    "InsecureKeyring",
    "Item",
    "StoreError",
    "check_backend",
    "file_lock",
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
# After the kernel reports a deadlock on a lock file that is none, the host waits this long before
# it asks again, doubling up to the most.
DEADLOCK_PAUSE: Final = 0.01
DEADLOCK_PAUSE_MOST: Final = 0.5
TABLES: Final = ("records", "pending", "verdicts")
# The one keyring backend the host keeps the item in, by system, as its class's (__module__,
# __qualname__): the module docstring says what that does and does not check.
KNOWN_BACKENDS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "linux": ("keyring.backends.SecretService", "Keyring"),
        "macos": ("keyring.backends.macOS", "Keyring"),
    }
)
SYSTEM_NAMES: Final[Mapping[str, str]] = MappingProxyType({"linux": "Linux", "macos": "macOS"})
# The end of every refusal of the pair's keyring, after "the keyring backend here (X) <reason>, ".
PAIR_REFUSAL: Final = (
    "so lucille-host cannot keep its pair in it; set up the system keyring, or pair with "
    "--store-file to keep the pair in a file instead, which is weaker"
)

T = TypeVar("T")


class StoreError(OSError):
    """The keyring item could not be read or written."""


class InsecureKeyring(state.Refused):
    """The keyring backend in use is not, by its class, the one the host keeps the item in on this
    system (:data:`KNOWN_BACKENDS`)."""


def _identity(backend: object) -> tuple[str, str]:
    kind = type(backend)
    return (kind.__module__, kind.__qualname__)


def _known(backend: object) -> bool:
    """Whether ``backend``'s class is exactly the one this system's entry in
    :data:`KNOWN_BACKENDS` names. The tests take their own backend in memory as that one too, by
    replacing this function (tests/conftest.py)."""
    return _identity(backend) == KNOWN_BACKENDS.get(paths.os_family())


def _unfit(backend: object) -> str | None:
    """Why ``backend`` is not the backend the host keeps the item in, or None when it is."""
    if isinstance(backend, FailKeyring):
        return "has no keyring to store in"
    if isinstance(backend, NullKeyring):
        return "stores nothing"
    family = paths.os_family()
    system = ".".join(KNOWN_BACKENDS[family])
    if isinstance(backend, ChainerBackend):
        # Asked only whether it holds anything, so that an empty one keeps its old reason. keyring
        # works the members out again each time, from each backend's priority, which a backend
        # may raise for (the Secret Service's does when it cannot reach its service, and other
        # backends raise their own errors): a chain whose members cannot be worked out, for any
        # reason, is still a chain, refused as one.
        try:
            empty = not backend.backends
        except Exception:
            empty = False
        if empty:
            return "chains no keyring"
        return (
            "is a chain of keyring backends, and lucille-host uses one keyring backend, not a "
            f"chain of them (keyring's keyringrc.cfg can name the system one, {system})"
        )
    if _known(backend):
        return None
    module, qualname = _identity(backend)
    return f"is {module}.{qualname}, and on {SYSTEM_NAMES[family]} lucille-host uses only {system}"


def check_backend(
    backend: KeyringBackend | None = None, *, refusal: str = PAIR_REFUSAL
) -> KeyringBackend:
    """The keyring backend in use (or ``backend``), when its class is the one the host keeps the
    item in on this system; otherwise :class:`InsecureKeyring`, whose message ends with
    ``refusal``. Read and write through the backend it returns, never through keyring's own
    ``get_password`` and ``set_password``, which look the backend up again."""
    current = keyring.get_keyring() if backend is None else backend
    reason = _unfit(current)
    if reason is not None:
        raise InsecureKeyring(
            f"the keyring backend here ({type(current).__name__}) {reason}, {refusal}"
        )
    return current


class _FileLock:
    """An ``os.lockf`` lock on one file, with a re-entrant thread lock beside it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._guard = threading.RLock()
        self._depth = 0
        self._descriptor: int | None = None

    def _lock(self, descriptor: int) -> None:
        pause = DEADLOCK_PAUSE
        while True:
            try:
                state.wait_for_lock(descriptor)
                return
            except OSError as exc:
                if exc.errno != errno.EDEADLK:
                    raise
            # Not a real deadlock (the module docstring says why): ask again.
            time.sleep(pause)
            pause = min(pause * 2, DEADLOCK_PAUSE_MOST)

    @contextlib.contextmanager
    def held(self) -> Iterator[None]:
        with self._guard:
            if self._depth == 0:
                descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    self._lock(descriptor)
                except OSError as exc:
                    os.close(descriptor)
                    raise StoreError(exc.errno, f"{self._path} cannot be locked") from exc
                except BaseException:
                    # A signal handler that raises, as Ctrl-C's default one does, ends the wait.
                    os.close(descriptor)
                    raise
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


def file_lock(path: Path) -> contextlib.AbstractContextManager[None]:
    """A lock on ``path`` of the kind the store's own are (``os.lockf`` beside a re-entrant thread
    lock), for another file of the host's own that two processes may change at once."""
    return _lock_for(path).held()


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
        backend = check_backend()
        try:
            raw = backend.get_password(self.service, self.username)
        except (KeyringError, OSError) as exc:
            raise StoreError(f"the keyring could not be read ({type(exc).__name__})") from exc
        if raw is None:
            return _empty()
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError):
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
        backend = check_backend()
        after["written_at"] = now
        text = json.dumps(after, sort_keys=True, separators=(",", ":"))
        try:
            backend.set_password(self.service, self.username, text)
        except (KeyringError, OSError) as exc:
            raise StoreError(f"the keyring could not be written ({type(exc).__name__})") from exc
