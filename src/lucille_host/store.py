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
- The host keeps the item in one keyring backend on each system, and builds it itself: keyring's
  Secret Service backend, ``keyring.backends.SecretService.Keyring``, on Linux, and its Keychain
  backend, ``keyring.backends.macOS.Keyring``, on macOS (:data:`KNOWN_BACKENDS`). :func:`_build`
  imports that system's module alone, never the other's, and builds its class once a process. The
  host never asks keyring for a backend, so keyring chooses none and the backends keyring's
  installed entry points name are not loaded (on Linux, loading its macOS backend runs ``ldconfig``,
  ``gcc`` and ``ld``, through ``ctypes.util.find_library``).
- keyring's own off switch is kept. Before it imports its system's module, :func:`_build` reads
  ``PYTHON_KEYRING_BACKEND``, and the ``default-keyring`` of the ``[backend]`` section of keyring's
  configuration file, ``keyringrc.cfg``, as plain text, once a process with the backend it builds.
  When either names keyring's ``null`` or ``fail`` backend, by the names in :data:`OFF_SWITCH`
  (``keyring --disable`` writes ``default-keyring=keyring.backends.null.Keyring``), it takes
  keyring's ``null`` backend, which :func:`check_backend` refuses as storing nothing, with the
  sentence that points to ``--store-file``. Either is enough, though keyring itself reads the file
  only when the variable is unset. Any other value is not consulted: the host builds its system's
  backend as it does with neither set. Nothing either names is imported, the file's ``keyring-path``
  is never looked up, and nothing is added to ``sys.path``. The variable is compared as it is, as
  keyring takes it, and the file is read as keyring reads it (``configparser.RawConfigParser``,
  UTF-8, universal newlines, the value stripped), from where keyring 25.7 reads it on both systems
  (``config_root`` in ``keyring/util/platform_.py``, which macOS shares with Linux):
  ``python_keyring/keyringrc.cfg`` under ``$XDG_CONFIG_HOME``, or under ``~/.config`` when that is
  unset or empty. It is opened so that a FIFO there does not wait for a writer, and read only when
  it is a regular file, and then no further than one byte past :data:`CONFIG_LIMIT`, so a FIFO or a
  device there is never read; a regular file on a file system that does not answer (measured with
  FUSE) is waited on, as keyring waits on it. The limit is far above what ``keyring
  --disable`` writes (55 bytes) and small for a reason: Python 3.11's ``configparser`` took 0.14 s
  to reject a 4 KiB line with no ``=`` or ``:``, and half a minute at 64 KiB, where 3.13 takes
  milliseconds (measured on 3.11.15 and 3.13.14; keyring, which reads with no limit, takes as long).
  A file that is missing, is not a regular file, is longer than that, cannot be read or does not
  parse, or has no such value, and a home folder that cannot be found, are no setting: the host goes
  on as with none, and nothing is raised. That includes a file keyring itself fails on (one that is
  not UTF-8 or does not parse): keyring then stores nothing, and the host builds its system's
  backend.
- Before it builds the class, :func:`_build` asks for the class's ``priority``, keyring's own test
  of whether a backend can be used here, which keyring asked of every backend before it chose one.
  It is needed: a Secret Service backend built where there is no session bus raises secretstorage's
  own error at the first read, which is not a keyring error. The Secret Service's ``priority``
  connects to the session bus that ``DBUS_SESSION_BUS_ADDRESS`` names, a local socket (jeepney,
  which secretstorage uses, connects to a ``unix:`` address only), and asks the bus whether a Secret
  Service runs there or can be started, without starting it; the Keychain's asks only whether this
  is macOS and its module loaded the Security framework. Neither starts a process: read from keyring
  25.7, secretstorage 3.5 and jeepney 0.9; tests/test_keyring_backend.py checks it on Linux with no
  session bus, and on macOS, and nothing checks it with a session bus. Where the class cannot be
  used (there is no session bus, or no Secret Service on it, secretstorage is missing or fails, or
  the module does not import), the host takes keyring's ``fail`` backend
  instead, as keyring did where it found nothing it could use, and refuses it with the same
  sentence.
- On Linux that check runs under a deadline, :data:`PROBE_TIMEOUT` (issue 39). jeepney bounds the
  connect and the authentication, but sends the bus's ``Hello`` with no timeout, and neither
  secretstorage nor keyring lets a caller pass one, so a socket at that address that authenticates
  and then never answers (a relay whose other side is gone, a stuck bus daemon) would make every
  command that opens the store wait without end, ``run`` past its signals and its lifeline. The
  check is asked on a thread of its own; past the deadline, the host takes :class:`NoAnswer`,
  keyring's ``fail`` backend marked as such, and refuses it with the sentence that says the session
  bus did not answer and points to ``--store-file``, once a process with the backend it builds. The
  thread is left waiting on its socket, a daemon thread, which the process does not wait for at
  exit. A real bus answers the whole check in about a millisecond (measured on dbus-daemon 1.14).
- Each read and each write of an item through the Secret Service, on Linux, is bounded too, by
  :data:`CALL_TIMEOUT` (:func:`keyring_call`): it opens a connection of its own, with the same
  unbounded ``Hello``, and it may also wait, rightly, on the user at the collection's unlock
  prompt, so its bound is the longer. Past it the call is a :class:`StoreError`
  (:class:`Unanswered`) that says so, and that process asks the keyring nothing more: every later
  read and write fails at once, so that no thread is left waiting for each, and no write is sent
  that could land after a newer one. The thread left waiting is not stopped, and can finish its
  call until the process exits: a write past the bound may still land, so its result is not known,
  the host changes none of its own files on the assumption that it failed, and the next process
  reads the item afresh.
  The macOS Keychain, which talks to no bus and may wait on its own prompt, is not bounded.
- The check cannot see everything that makes a read or write fail. A Secret Service the bus lists
  as one it can start, and that fails to start, passes it, and the first read, which starts it, gets
  the bus's error reply ``org.freedesktop.DBus.Error.Spawn.ChildExited``; a bus that goes away
  after the backend was built fails each later call (issue 38). secretstorage turns only some D-Bus
  errors into its own, and keyring wraps neither, so these reach the store as secretstorage's
  errors or jeepney's ``DBusErrorResponse``, neither a keyring error nor an ``OSError``. The store
  gives the classes :data:`SERVICE_ERRORS` names as a :class:`StoreError` (``store_error``), whose
  message names the error's class and a D-Bus error's name, never anything read from the item or
  said with the error; any other error is raised as it is. Those that say the service could not be
  reached or started are :class:`KeyringUnusable`, on which ``pair --store-file`` may fall back to
  the file store; a locked collection or a dismissed unlock prompt is not. keyring wraps some of
  them in its own ``InitError`` (a service whose ``Exec`` names a program that does not exist
  gives ``Spawn.ExecFailed``, which secretstorage turns into its own error, and keyring into
  ``InitError``), so the cause chain is looked at, through those errors only and up to
  :data:`CHAIN_LIMIT` links (:func:`keyring_failure`).
- :func:`check_backend` takes a backend only when its class is exactly the one this system's entry
  in :data:`KNOWN_BACKENDS` names, as its ``(__module__, __qualname__)``, compared as strings. It
  checks the backend the host built when the store is opened, and again before each read and each
  write of the item, which go to the backend it checked, and refuses every other: keyring's ``fail``
  and ``null`` backends, a subclass of the known class, a class under any other name, one that
  writes secrets in the clear to a file among them, and every chain of backends (keyring's
  ``ChainerBackend``), which it never asks what it holds: that would load every backend keyring's
  entry points name. What is checked is the class's identity, not how the backend keeps a secret:
  which service answers on the session bus, and whether the collection that keeps the item is
  encrypted, are not checked; nor are the settings keyring gives a backend from
  ``KEYRING_PROPERTY_*`` environment variables (the Secret Service collection, a Keychain file); and
  code already running in the process could give a class of its own those names.

- ``uninstall-data`` deletes the profile's items, this one and the credential store's
  (:data:`OWNED_SERVICES`), through the checked backend, and reads each again to see that it is
  gone (:func:`delete_items`). Nothing else deletes an item.

A keyring failure, a backend's own ``OSError`` included, is a :class:`StoreError`, an ``OSError``,
which is what Switchboard's stores raise for a storage failure; the wrapper never reads one as the
hub's (``_switchboard.py``). No error message holds anything read from the item.
"""

from __future__ import annotations

import configparser
import contextlib
import copy
import errno
import functools
import io
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeVar

from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.backends.fail import Keyring as FailKeyring
from keyring.backends.null import Keyring as NullKeyring
from keyring.errors import KeyringError, PasswordDeleteError

from lucille_host import nesting, paths, state

__all__ = [
    "KNOWN_BACKENDS",
    "OFF_SWITCH",
    "PAIR_REFUSAL",
    "PAIR_STAMPS",
    "ROOM_STAMPS",
    "SERVICE",
    "SERVICE_ERRORS",
    "SPAWN_ERRORS",
    "STAMP_EVERY",
    "UNUSABLE_REPLIES",
    "UNUSABLE_ERRORS",
    "CALL_TIMEOUT",
    "CHAIN_LIMIT",
    "CREDENTIALS_SERVICE",
    "OWNED_SERVICES",
    "PROBE_TIMEOUT",
    "InsecureKeyring",
    "Item",
    "KeyringUnusable",
    "NoAnswer",
    "StoreError",
    "Unanswered",
    "check_backend",
    "delete_items",
    "file_lock",
    "keyring_call",
    "keyring_failure",
    "keyring_turned_off",
    "pair_write_tried",
    "stamps_only",
]

SERVICE: Final = "lucille-host"
# The credential store's item (``credentials.py``), named here so that ``uninstall-data`` can
# delete it without importing that module, which only the ``credential`` verb may load.
CREDENTIALS_SERVICE: Final = "lucille-host.credentials"
# Every keyring service an item of the host's stores is kept under, each with the profile's name as
# its user name: what ``uninstall-data`` deletes (:func:`delete_items`).
OWNED_SERVICES: Final = (SERVICE, CREDENTIALS_SERVICE)
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
# __qualname__): the one _build builds. The module docstring says what the check of it does and does
# not check.
KNOWN_BACKENDS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "linux": ("keyring.backends.SecretService", "Keyring"),
        "macos": ("keyring.backends.macOS", "Keyring"),
    }
)
SYSTEM_NAMES: Final[Mapping[str, str]] = MappingProxyType({"linux": "Linux", "macos": "macOS"})
# keyring's own off switch: its null and fail backends, as PYTHON_KEYRING_BACKEND or keyringrc.cfg
# names them. The module docstring says how each is read.
OFF_SWITCH: Final = frozenset({"keyring.backends.null.Keyring", "keyring.backends.fail.Keyring"})
# The most of keyringrc.cfg the host takes, in bytes; it reads at most one byte more, and a longer
# file is no setting. The module docstring says why it is small.
CONFIG_LIMIT: Final = 4 * 1024
# How long, in seconds, the host waits for the session bus to answer the Secret Service's check of
# whether it can be used here, on Linux (the module docstring says why). A real bus answers it in
# about a millisecond.
PROBE_TIMEOUT: Final = 5.0
# How long, in seconds, the host waits for one read or one write of an item through the Secret
# Service, on Linux: longer, as one may wait on the user at the collection's unlock prompt.
CALL_TIMEOUT: Final = 60.0
# The systems whose keyring calls are bounded (keyring_call): the Secret Service's.
BOUNDED: Final = frozenset({"linux"})
# The end of every refusal of the pair's keyring, after "the keyring backend here (X) <reason>, ".
PAIR_REFUSAL: Final = (
    "so lucille-host cannot keep its pair in it; set up the system keyring, or pair with "
    "--store-file to keep the pair in a file instead, which is weaker"
)

T = TypeVar("T")


class StoreError(OSError):
    """The keyring item could not be read or written."""


class Unanswered(StoreError):
    """A keyring call that did not answer within :data:`CALL_TIMEOUT`, or one not made because an
    earlier call in this process did not answer. A write that did not answer may still land,
    from the thread left waiting on it, until the process exits: whether it was saved is not
    known, and nothing kept beside the item is changed on the assumption that it was not. The next
    process reads the item afresh."""


class KeyringUnusable(StoreError):
    """A read or write of an item that the Secret Service could not be reached or started for, said
    by an error of its own libraries rather than keyring's (issue 38): its service can be listed as
    one the bus starts on demand and then fail to start, or its bus can go away after the backend
    was built. ``pair --store-file`` may fall back to the file store on it. Its own libraries'
    other errors (:data:`SERVICE_ERRORS`) are a plain :class:`StoreError`. The message names the
    error's class, and a D-Bus error's name, never anything read from the item or said with the
    error."""


class NoAnswer(FailKeyring):
    """keyring's ``fail`` backend, taken where the session bus did not answer the Secret Service's
    check within :data:`PROBE_TIMEOUT`. It is refused as no keyring is, with a sentence of its
    own."""


class _NotAnswered(Exception):
    """A bounded call did not return within its deadline."""


# Set once a read or write of the keyring has not answered within CALL_TIMEOUT in this process:
# every later one then fails at once (the module docstring says why).
_UNANSWERED = threading.Event()

# Set once this process has sent, or tried to send, a write of the pair item to the keyring
# (:func:`pair_write_tried`).
_PAIR_WRITE_TRIED = threading.Event()


def pair_write_tried() -> bool:
    """Whether this process has tried to write the pair item to the keyring, whether or not the
    write landed: ``pair --store-file`` then never falls back to the file store, which could leave
    one pair in the keyring and another in the file."""
    return _PAIR_WRITE_TRIED.is_set()


def _within(seconds: float, call: Callable[[], T], name: str) -> T:
    """``call()``, made on a daemon thread of its own and waited for no longer than ``seconds``;
    :class:`_NotAnswered` past that, leaving the thread waiting where it is. What ``call`` raises is
    raised here."""
    done: dict[str, Any] = {}

    def run() -> None:
        try:
            done["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller as it is
            done["error"] = exc

    worker = threading.Thread(target=run, name=f"lucille-host {name}", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise _NotAnswered(name)
    if "error" in done:
        raise done["error"]
    return done["value"]


def keyring_call(call: Callable[[], T], what: str) -> T:
    """``call()``, one read or one write of an item through the backend :func:`check_backend`
    returned (``what`` says which, for the message). On Linux it is bounded by
    :data:`CALL_TIMEOUT`, and once one has not answered in this process none is made again (the
    module docstring says why); past the bound, or after one that did not answer, it is a
    :class:`Unanswered`, a :class:`StoreError`. Elsewhere it is ``call()``."""
    if paths.os_family() not in BOUNDED:
        return call()
    if _UNANSWERED.is_set():
        raise Unanswered(
            "the keyring did not answer earlier in this process, so it is not asked again; "
            "try again in a new one"
        )
    try:
        return _within(CALL_TIMEOUT, call, f"keyring {what}")
    except _NotAnswered:
        _UNANSWERED.set()
        message = f"the keyring did not answer a {what} within {CALL_TIMEOUT:g} s"
        if what == "write":
            # The thread left waiting may still finish it.
            message += "; whether it was saved is not known, and the next start reads it again"
        elif what == "delete":
            message += "; whether it was deleted is not known: try again in a new process"
        raise Unanswered(message) from None


# The errors the Secret Service's own libraries raise past keyring, at a read or write of an item,
# by their class's ``(__module__, __qualname__)``, compared as strings as :data:`KNOWN_BACKENDS`
# is: so that neither library is imported here, and macOS, which has neither, needs nothing. Each
# is a :class:`StoreError`: every error class secretstorage 3.5 defines (its base, and each of its
# subclasses named, since the class is matched exactly), and jeepney's D-Bus error reply. Any other
# error, a subclass of one of these among them, is raised as it is.
SERVICE_ERRORS: Final = frozenset(
    {
        ("secretstorage.exceptions", "SecretStorageException"),
        ("secretstorage.exceptions", "SecretServiceNotAvailableException"),
        ("secretstorage.exceptions", "LockedException"),
        ("secretstorage.exceptions", "ItemNotFoundException"),
        ("secretstorage.exceptions", "PromptDismissedException"),
        ("jeepney.wrappers", "DBusErrorResponse"),
    }
)
# Of those, the ones that say the Secret Service could not be reached or started
# (:class:`KeyringUnusable`), wherever they are in the error's cause chain (keyring wraps
# secretstorage's error in its own ``InitError`` when it looks up the collection): secretstorage's
# error for no service, a bus that refused the connection, a service that failed to exec or did
# not reply; and a D-Bus error reply whose name is one of the bus's errors for a service it could
# not start (``Spawn.ChildExited``, ``Spawn.ExecFailed``, say: :data:`SPAWN_ERRORS`) or reach
# (:data:`UNUSABLE_REPLIES`). The others are not: a locked collection, a dismissed unlock prompt
# (the user said no), no collection to keep the item in, or any other reply, which a service that
# runs gives.
UNUSABLE_ERRORS: Final = frozenset(
    {("secretstorage.exceptions", "SecretServiceNotAvailableException")}
)
_ERROR_REPLY: Final = ("jeepney.wrappers", "DBusErrorResponse")
SPAWN_ERRORS: Final = "org.freedesktop.DBus.Error.Spawn."
# The bus's other replies for a service it could not start or reach: none owns the name and none
# could be started, or the service did not reply.
UNUSABLE_REPLIES: Final = frozenset(
    {"org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NoReply"}
)
# How many links of an error's cause chain (``__cause__``, else ``__context__``) are looked at:
# keyring wraps secretstorage's error, which wraps jeepney's reply, so three are met in practice.
CHAIN_LIMIT: Final = 8


# A D-Bus error name, as the bus sends it: dotted, each element a name, 255 bytes at most.
_DBUS_ERROR_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _kind(exc: BaseException) -> tuple[str, str]:
    return (type(exc).__module__, type(exc).__qualname__)


def _mapped(exc: BaseException) -> bool:
    """Whether ``exc`` is one of the errors a keyring call is mapped by: keyring's, an ``OSError``,
    or one of :data:`SERVICE_ERRORS`."""
    return isinstance(exc, KeyringError | OSError) or _kind(exc) in SERVICE_ERRORS


def _links(exc: BaseException) -> list[BaseException]:
    """``exc`` and the errors it was raised from, each link its ``__cause__``, else its
    ``__context__``: at most :data:`CHAIN_LIMIT`, each once, and only while each is one of the
    errors :func:`_mapped` names. The walk stops at any other error, which is not looked into."""
    links: list[BaseException] = []
    link: BaseException | None = exc
    while (
        link is not None
        and _mapped(link)
        and len(links) < CHAIN_LIMIT
        and all(link is not seen for seen in links)
    ):
        links.append(link)
        link = link.__cause__ or link.__context__
    return links


def _reply_name(exc: BaseException) -> str | None:
    """The error name of a D-Bus error reply, when ``exc`` is one and its name is one."""
    if _kind(exc) != _ERROR_REPLY:
        return None
    name = getattr(exc, "name", None)
    if isinstance(name, str) and len(name) <= 255 and _DBUS_ERROR_NAME.fullmatch(name):
        return name
    return None


def _says_unusable(exc: BaseException) -> bool:
    """Whether ``exc`` says the Secret Service could not be reached or started."""
    if _kind(exc) in UNUSABLE_ERRORS:
        return True
    name = _reply_name(exc)
    return name is not None and (name.startswith(SPAWN_ERRORS) or name in UNUSABLE_REPLIES)


def keyring_failure(exc: Exception, what: str) -> StoreError | None:
    """``exc``, raised by a ``what`` (read or write) of an item through the keyring, as the
    :class:`StoreError` the store raises for it; None when it is not one of the keyring's errors,
    an ``OSError``, or one of :data:`SERVICE_ERRORS`, for the caller to raise as it is.

    It is a :class:`KeyringUnusable` when any link of its cause chain (:func:`_links`, which stops
    at the first link that is none of those errors) says the Secret Service could not be reached or
    started. The message names its class, and a D-Bus
    error's name; below a wrapping error, the Secret Service error it was raised from, the deepest
    reply with a name first. Nothing else said with any of them is given."""
    if not _mapped(exc):
        return None
    links = _links(exc)
    detail = type(exc).__name__
    name = _reply_name(exc)
    if name is not None:
        detail += f": {name}"
    below = [link for link in links[1:] if _kind(link) in SERVICE_ERRORS]
    named = [link for link in below if _reply_name(link) is not None]
    if below:
        source = named[-1] if named else below[0]
        detail += f", from {type(source).__name__}"
        if named:
            detail += f": {_reply_name(source)}"
    verb = {"read": "read", "delete": "deleted"}.get(what, "written")
    message = f"the keyring could not be {verb} ({detail})"
    if any(_says_unusable(link) for link in links):
        return KeyringUnusable(message)
    return StoreError(message)


class InsecureKeyring(state.Refused):
    """The keyring backend is not, by its class, the one the host keeps the item in on this system
    (:data:`KNOWN_BACKENDS`), or that one cannot be used here."""


def _system_backend() -> KeyringBackend:
    """The keyring backend the host reads and writes the item through: the one :func:`_build` built
    for this system. It is the one place the host gets a backend, and the one the tests replace
    (tests/conftest.py)."""
    return _build(paths.os_family())


def _read_config(path: Path) -> bytes | None:
    """The bytes of the regular file at ``path``, or None where it is not one or is longer than
    :data:`CONFIG_LIMIT`. It is opened with ``O_NONBLOCK``, so a FIFO there does not block the open,
    what is not a regular file is never read, and a regular file is read no further than one byte
    past the limit."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= CONFIG_LIMIT:
            chunk = os.read(fd, CONFIG_LIMIT + 1 - size)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
        return None
    finally:
        os.close(fd)


def _configured_backend() -> str | None:
    """The ``default-keyring`` of the ``[backend]`` section of keyring's ``keyringrc.cfg``, as
    text, or None: the module docstring says where the file is, how it is read, and when it is no
    setting."""
    try:
        root = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        data = _read_config(Path(root, "python_keyring", "keyringrc.cfg"))
        if data is None:
            return None
        config = configparser.RawConfigParser()
        # As keyring reads it: open(..., encoding="utf-8"), universal newlines and all.
        config.read_file(io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))
        return config.get("backend", "default-keyring").strip()
    except (OSError, ValueError, RuntimeError, configparser.Error):
        return None


def _turned_off() -> bool:
    """Whether keyring's own settings turn the keyring off: ``PYTHON_KEYRING_BACKEND``, or the
    ``default-keyring`` of ``keyringrc.cfg``, names one of the backends in :data:`OFF_SWITCH`."""
    return (
        os.environ.get("PYTHON_KEYRING_BACKEND") in OFF_SWITCH
        or _configured_backend() in OFF_SWITCH
    )


def keyring_turned_off() -> bool:
    """Whether keyring's own settings, in this process's environment, turn the keyring off
    (:func:`_turned_off`). ``run`` refuses ``github.read`` while they do, before its child starts:
    the child's environment holds neither ``PYTHON_KEYRING_BACKEND`` nor ``XDG_CONFIG_HOME``, so it
    could not see the setting itself (decisions/0003, "First use")."""
    return _turned_off()


@functools.cache
def _build(family: str) -> KeyringBackend:
    """keyring's backend for ``family`` (:data:`KNOWN_BACKENDS`), built here with only its own
    module imported, once a process, as keyring kept the backend it chose; keyring's ``null``
    backend, without importing that module, when keyring's own settings turn the keyring off
    (:func:`_turned_off`); keyring's ``fail`` backend when the system's cannot be used here (the
    module docstring says when, and what asking tells)."""
    if _turned_off():
        return NullKeyring()
    kind: type[KeyringBackend]
    try:
        if family == "linux":
            from keyring.backends import SecretService

            kind = SecretService.Keyring
            # Bounded: the bus may authenticate and then never answer (the module docstring).
            try:
                usable = _within(PROBE_TIMEOUT, lambda: kind.viable, "keyring check")
            except _NotAnswered:
                return NoAnswer()
            return kind() if usable else FailKeyring()
        elif family == "macos":
            from keyring.backends import macOS

            kind = macOS.Keyring
        else:
            return FailKeyring()
        # The class's priority, which raises when it cannot be used here; viable is False then.
        if kind.viable:
            return kind()
    except Exception:
        pass
    return FailKeyring()


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
    if isinstance(backend, NoAnswer):
        return (
            f"has no keyring to store in: the session bus did not answer within {PROBE_TIMEOUT:g} s"
        )
    if isinstance(backend, FailKeyring):
        return "has no keyring to store in"
    if isinstance(backend, NullKeyring):
        return "stores nothing"
    if isinstance(backend, ChainerBackend):
        # Never asked what it holds: keyring works that out from every backend its entry points
        # name, which it loads to do so.
        return (
            "is a chain of keyring backends, and lucille-host uses one keyring backend, not a "
            "chain of them"
        )
    if _known(backend):
        return None
    family = paths.os_family()
    system = ".".join(KNOWN_BACKENDS[family])
    module, qualname = _identity(backend)
    return f"is {module}.{qualname}, and on {SYSTEM_NAMES[family]} lucille-host uses only {system}"


def check_backend(
    backend: KeyringBackend | None = None, *, refusal: str = PAIR_REFUSAL
) -> KeyringBackend:
    """The keyring backend the host built for this system (:func:`_system_backend`), or
    ``backend``, when its class is the one the host keeps the item in on this system; otherwise
    :class:`InsecureKeyring`, whose message ends with ``refusal``. Read and write through the
    backend it returns, never through keyring's own ``get_password`` and ``set_password``, which
    ask keyring for the backend it chooses."""
    current = _system_backend() if backend is None else backend
    reason = _unfit(current)
    if reason is not None:
        raise InsecureKeyring(
            f"the keyring backend here ({type(current).__name__}) {reason}, {refusal}"
        )
    return current


def delete_items(username: str, services: tuple[str, ...] = OWNED_SERVICES) -> None:
    """Delete the keyring item of each of ``services`` kept under ``username`` (a profile's name),
    one after another, stopping at the first that fails (:func:`_delete_item`)."""
    for service in services:
        _delete_item(service, username)


def _delete_item(service: str, username: str) -> None:
    """Delete one keyring item through the backend :func:`check_backend` returns, and check that
    it is gone: a read of it must find nothing. An item that is not there already is no failure.
    keyring raises the same error for an item that is not there as for one it could not delete (its
    Keychain backend turns every error of the Keychain into it), so that error is judged by the
    read that follows. Each call is bounded on Linux (:func:`keyring_call`). A failure is a
    :class:`StoreError` whose message names the error's class (:func:`keyring_failure`) or the
    item, never anything read from it or said with the error; what the read finds is only compared
    with nothing, and kept nowhere."""
    backend = check_backend()
    try:
        keyring_call(lambda: backend.delete_password(service, username), "delete")
    except StoreError:
        raise
    except PasswordDeleteError:
        # Not there, or not deleted: the read below says which.
        pass
    except Exception as exc:
        failed = keyring_failure(exc, "delete")
        if failed is None:
            raise
        raise failed from None
    try:
        gone = keyring_call(lambda: backend.get_password(service, username) is None, "read")
    except StoreError:
        raise
    except Exception as exc:
        failed = keyring_failure(exc, "read")
        if failed is None:
            raise
        raise failed from None
    if not gone:
        raise StoreError(f"the keyring item {service}/{username} could not be deleted")


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
            raw = keyring_call(lambda: backend.get_password(self.service, self.username), "read")
        except StoreError:
            raise
        except Exception as exc:
            failed = keyring_failure(exc, "read")
            if failed is None:
                raise
            raise failed from exc
        if raw is None:
            return _empty()
        try:
            # Past the host's own depth limit, an item this version did not write, whatever
            # Python's parser would make of it.
            doc = None if nesting.json_deeper(raw) else json.loads(raw)
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
        _PAIR_WRITE_TRIED.set()
        try:
            keyring_call(lambda: backend.set_password(self.service, self.username, text), "write")
        except StoreError:
            raise
        except Exception as exc:
            failed = keyring_failure(exc, "write")
            if failed is None:
                raise
            raise failed from exc
