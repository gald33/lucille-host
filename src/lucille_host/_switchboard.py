"""The only runtime module that imports Switchboard.

The host talks to Lucille through agent-switchboard (MIT, pinned ``==2.6.*``, base install with no
extras). Keeping every import of it in this one module lets a reader see the whole surface the
host uses in one place, and the Switchboard-surface lint (``tools/guards/switchboard_lint.py``)
fails CI on a ``switchboard`` import or use anywhere else in the runtime package, including one
that reaches it through this module.

The surface is the plain synchronous library. The calls the design names are:

- ``switchboard.pairing``: ``PairingCode.decode``, ``accept``, ``resolve`` and ``forget``, with
  ``PairStore`` for ``--store-file``. Not ``pairing.status``, which resolves each pair over the
  hub: ``lucille-host status`` makes no hub call, and reads the pair as the host's own store keeps
  it (:meth:`Link.kept`);
- ``switchboard.client.Client``: ``post``, ``inbox`` with a ``since`` cursor, and
  ``read_channels``, on clients built from the ``config`` of a pairing ``Resolution`` or a sharing
  ``SharedRoom``, each given a fresh ``switchboard.signing.SigningIdentity.generate()``;
- ``switchboard.sharing``: ``touch``, ``accept``, ``resolve_room`` and ``leave``, the calls that
  read and take a room Lucille offers over the pair, with the host's own share store. Never
  ``share_new``: the host offers no room of its own.

Never ``register()``, an invite, a pairing the host starts (``pairing.start``), a heal, an export
of the pair secret, the Switchboard CLI, its MCP bridge, or a hosted bridge.

The lint enforces this at the level of modules and their members: it allows only the submodules
and members listed in its ``SURFACE``, which also covers the types and errors these calls take and
raise. A method called on a client object is not checked by name, except ``register``, anything
named for an invite, and private members; review covers the rest.

Another runtime module may take from here only what this module shows to be its own: a function
or class it defines, or a constant it assigns, that uses no part of Switchboard in its
decorators, bases or value. The lint presumes every other name here is Switchboard.

What this module gives every pairing and sharing call the host makes, through a :class:`Link`:

- the host's own two stores, never Switchboard's defaults under ``~/.switchboard``: the keyring
  item of the profile (``store.py``), seen as a pair store (:class:`KeyringPairs`) and a share
  store (:class:`KeyringRooms`), or, under ``--store-file``, ``PairStore`` (seen through
  :class:`FilePairs`, so that its storage failures are the host's ``store.StoreError``, never a hub
  error) and ``ShareStore`` on ``<state>/pairs`` with an empty environment, so that no
  ``SWITCHBOARD_PAIR_*`` variable is read;
- its own way of building a client (:meth:`Link.connect`), which refuses unless
  ``XDG_RUNTIME_DIR`` names the host's private runtime folder (``state.py``), refuses a
  configuration that would keep Switchboard's peer log or stash on disk, and gives the client a
  fresh in-memory signing identity in place of whatever it found. The clients the host reads with
  itself also read a body that opens but cannot be parsed as one that does not open
  (:class:`_Opener`).

Every call is made with ``base=None``, so the library builds each room's configuration itself,
with no file of its own and no hub token but the one the managed hub publishes.

What the rest of the host gets back is its own: the views :class:`Code`, :class:`Pair`,
:class:`Kept`, :class:`Note`, :class:`Told`, :class:`Touch` (with its :class:`ShareNote` items),
:class:`Room`, :class:`RoomTaken`, :class:`RoomLeft`, :class:`Message` and :class:`Posted`, which
carry no key and no SAS, and the errors :class:`BadCode`, :class:`PairFailed`,
:class:`RoomError` and :class:`HubError` (with :class:`Unopenable`), raised in place of the
library's. The read loop reads and posts in a room through a :class:`RoomClient`, one per thread:
a peek past a sequence number (``inbox`` with ``since``, never without), the recovery read of a
channel that holds a message the room's key cannot open (``read_channels``), and a post of type
``note``, with no thread and an explicit TTL. The SAS leaves this module only through
:meth:`Link.sas`, for the local gate to show the user, who compares it with Lucille's page
(``gate.py``); the host writes it to no file of its own and to no audit line. A :class:`Room` keeps
the configuration a client of the room is built from, and nothing outside this module reads a key
out of it.
"""

from __future__ import annotations

import contextlib
import datetime
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from switchboard import client, crypto, pairing, sharing, signing

from lucille_host import paths, store
from lucille_host import state as host_state

# Nothing here is for ``from lucille_host._switchboard import *``, which the lints refuse anyway.
__all__: list[str] = []

# The hub the host talks to. A pairing code naming any other hub is refused (a loopback hub is
# accepted only when the local configuration names it, for the conformance suite).
LUCILLE_HUB: Final = "https://switchboard.lucille-ai.com"

# A long-poll holds for up to 25 s; the client waits longer than that for an answer.
CLIENT_TIMEOUT: Final = 40.0

# The hub refuses a write signed by a clock more than this many seconds from its own.
SKEW_WINDOW: Final = 300

# The host's own readers read a message whose body nests more than this many levels of objects and
# arrays as one they cannot open. No message of the protocol nests more than a few, and the host's
# checks of a body walk every level.
NEST_MOST: Final = 64

# The host's agent id, the same on every computer and in every room (spec/PROTOCOL.md section 8).
AGENT_ID: Final = "lucille-host"

# The library's notes on the ``pair`` channel of pair:<name>, as ``pairing.message_tag`` reads
# them: the first three only make the host resolve the pair, the last only makes it touch.
TAG_PAIRED: Final = "swbp1/paired"
TAG_MOVED: Final = "swbp1/moved"
TAG_ENDED: Final = "swbp1/ended"
TAG_SHARED: Final = "swbp1/shared"

# The channels the host reads: the library's own in pair:<name>, and the protocol's in each work
# room (spec/PROTOCOL.md section 5).
PAIR_CHANNEL: Final = "pair"
EXEC_CHANNEL: Final = "lucille.exec"

# The handle the library takes for a held room, so that a leave never names a pending offer.
_HELD: Final = "shared:"

FILE_STORE_WARNING: Final = (
    "the file store keeps the pair secret and the work rooms' keys unencrypted, in files only "
    "your user can read, in the host's state folder; that is weaker than the OS keyring"
)


class BadCode(Exception):
    """Not a pairing code this release can read."""


class PairFailed(Exception):
    """A pairing call refused. ``state`` is Switchboard's word for why (``expired``,
    ``already_redeemed``, ``aborted``, ``never_confirmed``, ``ended_by_peer``, ...), or
    ``error`` when it gave none. ``skewed`` is true when the hub refused a write of the call's
    because this computer's clock is more than 300 s from its own."""

    def __init__(self, state: str, message: str, *, skewed: bool = False) -> None:
        super().__init__(message)
        self.state = state
        self.skewed = skewed


class HubError(Exception):
    """The hub could not be reached, or answered with an error. It decided nothing.

    ``skewed`` is true when the hub refused a write because this computer's clock is more than
    300 s from its own (a write signature outside the hub's time window)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        skewed: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.skewed = skewed


class Unopenable(HubError):
    """A message in a room could not be opened with the room's key."""


class RoomError(Exception):
    """A sharing call refused. ``state`` is Switchboard's word for why: for a room looked up by
    name, ``left`` (this computer left a room of that name), ``offered`` (one of that name is
    offered and not accepted) or ``unknown`` (nothing of that name); for an accept, ``withdrawn``,
    ``ambiguous``, ``name_taken``, the pair's own state, and the like; ``error`` when it gave
    none. The message is the host's own: the library's text can quote what the other side
    wrote. ``skewed`` is as :class:`PairFailed` has it."""

    def __init__(self, state: str, message: str, *, skewed: bool = False) -> None:
        super().__init__(message)
        self.state = state
        self.skewed = skewed


@dataclass(frozen=True)
class Code:
    """A decoded pairing code: what the host checks before it answers it."""

    # The code as it was given, to answer it with. Never shown, never logged.
    text: str = field(repr=False)
    # The hub the code names, with every trailing ``/`` removed.
    url: str
    carries_token: bool
    # The pair's note as the code carries it, unsanitized: shown only after sanitizing.
    note: str
    # The pair's name: the one the code gives, or the default the library derives.
    name: str


@dataclass(frozen=True)
class Pair:
    """A pair after one resolve. ``sas_known`` is true once Lucille has collected this
    computer's answer and the SAS has reached the host; the SAS itself is not here."""

    name: str
    sas_known: bool
    generation: int
    # The workspace of pair:<name> at that generation.
    workspace: str
    # False when the pair room could not be read: the rest is what this computer last saw.
    checked: bool
    # The configuration of pair:<name>, to read the library's notes there.
    room: Any = field(repr=False, compare=False)
    # When the pair room could not be read because the hub refused (a 429), how long it asked
    # the host to wait before it tries again, in seconds; else None.
    retry_after: float | None = None


@dataclass(frozen=True)
class Kept:
    """A pair as this computer's store keeps it, read with no hub call: whether the SAS has
    reached the host (the SAS itself is not here), and the generation of pair:<name> it last
    followed."""

    name: str
    sas_known: bool
    generation: int


@dataclass(frozen=True)
class Note:
    """A message on the ``pair`` channel of pair:<name>: its sequence number, and the library's
    tag (``swbp1/paired`` and the like) when it is one of the library's notes."""

    seq: int
    tag: str | None


@dataclass(frozen=True)
class Told:
    """A pair forgotten on this computer, and whether Lucille was told."""

    name: str
    told: bool


@dataclass(frozen=True)
class ShareNote:
    """One of the notes the library makes while it reads the pair's offers: its kind
    (``offered``, ``left``, ``withdrawn``, ``pair_moved``, ``pair_gone`` and the like), the pair
    it is about, and the room it names. For a held room that is this computer's name for it; for
    an offer, the name Lucille gave it. The library's own wording is not kept: it quotes text
    Lucille chose."""

    kind: str
    pair: str | None
    room: str | None


@dataclass(frozen=True)
class Touch:
    """One read of the pair's offers. ``state`` is ``ok`` when the pair room was read and its
    records written, else why not: ``unreachable``, ``gone`` or ``unconfirmed``. ``skewed`` is
    true when the hub refused a write of the touch's for this computer's clock."""

    state: str
    notes: tuple[ShareNote, ...]
    skewed: bool = False

    @property
    def reached(self) -> bool:
        return self.state == "ok"


@dataclass(frozen=True)
class Room:
    """A work room this computer holds, as its share store has it, looked up with no hub call."""

    # This computer's name for the room.
    name: str
    # The room's hub, as the library keeps it (every trailing ``/`` removed).
    url: str
    workspace: str
    # Whether the offer carried a hub token (Switchboard's ``token_source`` is ``shared``).
    carries_token: bool
    # The pairs on the room's list, by name only.
    pairs: tuple[str, ...]
    # The configuration to build the room's client from. It holds the room's keys.
    config: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class RoomTaken:
    """An offered room this computer accepted, under ``name``, and what the library noted on the
    way. ``skewed`` is true when the hub refused the write that tells the pair, for this
    computer's clock: the room is held, and the next touch tells the pair."""

    name: str
    notes: tuple[ShareNote, ...]
    skewed: bool = False


@dataclass(frozen=True)
class RoomLeft:
    """A room this computer left: whether the pair was told yet (every later touch tells it
    until it has been), and what the library noted on the way. ``skewed`` is as
    :class:`RoomTaken` has it."""

    name: str
    told: bool
    notes: tuple[ShareNote, ...]
    skewed: bool = False


# What the hub says when it refuses a write signed by a clock more than 300 s from its own.
_SKEW_DETAIL: Final = "outside the time window"


def _skewed(exc: BaseException | None) -> bool:
    """Whether ``exc``, or an exception it was raised from, is the hub refusing a write for the
    clock's sake. A sharing call that refuses names the cause only in its message: it reads the
    pair room in a touch of its own, which keeps the hub's error as text
    (sb ``sharing.py:991-1001``, ``:1583-1589``)."""
    if isinstance(exc, pairing.PairingError) and _SKEW_DETAIL in str(exc):
        return True
    for _ in range(8):
        if exc is None:
            return False
        if isinstance(exc, client.ReadOnlyRoom) and _SKEW_DETAIL in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


# The longest wait a hub's ``retry_after`` sets. A larger value, or one that is not a finite number,
# is the hub's to send, not the host's to obey for longer than this.
RETRY_AFTER_MAX: Final = 300.0


def _hub_error(exc: BaseException) -> HubError:
    if isinstance(exc, client.SwitchboardError):
        retry = exc.payload.get("retry_after") if exc.status == 429 else None
        after = None
        if isinstance(retry, int | float) and not isinstance(retry, bool) and math.isfinite(retry):
            after = min(max(float(retry), 0.0), RETRY_AFTER_MAX)
        return HubError(
            f"the hub answered {exc.status or 'with an error'}",
            status=exc.status,
            retry_after=after,
            skewed=_skewed(exc),
        )
    return HubError(f"the hub could not be reached ({type(exc).__name__})", skewed=_skewed(exc))


@dataclass(frozen=True)
class Message:
    """A message read in a room: what the host acts on. ``mine`` is true for a post under the
    host's own id in that room; ``kind`` is Switchboard's message type, which travels in the
    clear; ``tag`` is the library's tag (``swbp1/...``) when the body carries one. A message the
    room's key cannot open has ``unreadable`` set, no body and no channel name."""

    seq: int
    channel: str | None
    mine: bool
    kind: str
    thread: str | None
    body: Any = field(repr=False)
    unreadable: bool = False
    tag: str | None = None


@dataclass(frozen=True)
class Posted:
    """A post the hub accepted: its sequence number, and when the hub stamped it, by the hub's
    clock (None when that could not be read)."""

    seq: int
    created_at: float | None


def _deeper(value: Any, most: int) -> bool:
    """Whether ``value`` nests more than ``most`` levels of objects and arrays, found without
    recursion."""
    stack = [(value, 0)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if level >= most:
            return True
        stack.extend((child, level + 1) for child in children)
    return False


class _Opener:
    """A room's cipher, as the host's own readers use it: a message body that opens with the
    room's key but is not JSON, or nests more than :data:`NEST_MOST` levels deep, is read as one
    that does not open. The library raises ``DecryptionError`` only for the first kind; its JSON
    parser raises anything else past it (sb ``crypto.py:365``, ``client.py:1473-1481``), so a
    peek fails with an error no reader expects, and the recovery read fails with it too, where a
    ``DecryptionError`` marks the one message unreadable and gives its sequence number."""

    def __init__(self, cipher: Any) -> None:
        self._cipher = cipher

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cipher, name)

    def unseal(self, value: Any, context: str) -> Any:
        try:
            opened = self._cipher.unseal(value, context)
        except crypto.CryptoError:
            raise
        except Exception:  # noqa: BLE001 - whatever parsing the plaintext raised
            raise crypto.DecryptionError(f"the value at {context!r} is not readable") from None
        # The library keeps each body in one object of its own, with the channel's name.
        if context == "message.body" and _deeper(opened, NEST_MOST + 1):
            raise crypto.DecryptionError(f"the value at {context!r} nests too deep")
        return opened


def _stamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _message(row: dict[str, Any], own: str) -> Message:
    unreadable = bool(row.get("unreadable"))
    channel = row.get("channel")
    thread = row.get("thread")
    return Message(
        seq=int(row["seq"]),
        channel=None if unreadable or not isinstance(channel, str) else channel,
        mine=row.get("from") == own,
        kind=str(row.get("type")),
        thread=thread if isinstance(thread, str) else None,
        body=None if unreadable else row.get("body"),
        unreadable=unreadable,
        tag=None if unreadable else pairing.message_tag(row),
    )


def _failed(exc: pairing.PairingError) -> PairFailed:
    return PairFailed(exc.state or "error", str(exc), skewed=_skewed(exc))


def _refused(exc: pairing.PairingError, what: str) -> RoomError:
    state = exc.state or "error"
    return RoomError(state, f"{what} was refused ({state})", skewed=_skewed(exc))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _share_notes(raw: list[dict[str, Any]]) -> tuple[ShareNote, ...]:
    return tuple(
        ShareNote(
            kind=str(note.get("kind")), pair=_text(note.get("pair")), room=_text(note.get("room"))
        )
        for note in raw
    )


def decode_code(text: str) -> Code:
    """Decode a ``swbp1_`` code. Its hub and its token are the caller's to check."""
    try:
        code = pairing.PairingCode.decode(text)
    except pairing.PairingError:
        raise BadCode("this is not a pairing code; paste the whole code Lucille shows") from None
    return Code(
        text=text,
        url=code.url,
        carries_token=code.token is not None,
        note=code.note,
        name=code.pair_name,
    )


class KeyringPairs:
    """Switchboard's pair store (``pairing.Store``) over the profile's keyring item."""

    def __init__(self, item: store.Item) -> None:
        self.enabled = True
        self._item = item

    def require(self) -> object:
        if not self.enabled:
            raise pairing.PairingError("this profile keeps no pairs", state="disabled")
        return self._item

    def lock(self) -> contextlib.AbstractContextManager[None]:
        return self._item.pairs_lock()

    def _table(self, table: str) -> dict[str, Any]:
        return self._item.read()["pairs"][table]

    def _put(self, table: str, name: str, value: dict[str, Any]) -> None:
        def edit(doc: dict[str, Any]) -> None:
            doc["pairs"][table][name] = value

        self._item.change(edit)

    def _pop(self, table: str, name: str) -> bool:
        def edit(doc: dict[str, Any]) -> bool:
            return doc["pairs"][table].pop(name, None) is not None

        return self._item.change(edit)

    def get(self, name: str) -> pairing.PairRecord | None:
        data = self._table("records").get(name)
        if data is None:
            return None
        return pairing.PairRecord.from_json(data, origin=f"pair {name!r} in the keyring")

    def save(self, record: pairing.PairRecord) -> None:
        self._put("records", record.name, record.to_json())

    def delete(self, name: str) -> bool:
        return self._pop("records", name)

    def names(self) -> list[str]:
        return sorted(self._table("records"))

    def pending(self, name: str) -> pairing.PendingHandshake | None:
        data = self._table("pending").get(name)
        if data is None:
            return None
        return pairing.PendingHandshake.from_json(data, origin=f"pairing {name!r} in the keyring")

    def save_pending(self, pending: pairing.PendingHandshake) -> None:
        self._put("pending", pending.name, pending.to_json())

    def delete_pending(self, name: str) -> bool:
        return self._pop("pending", name)

    def pending_names(self) -> list[str]:
        return sorted(self._table("pending"))

    def verdict(self, name: str, *, now: float | None = None) -> dict[str, Any] | None:
        data = self._table("verdicts").get(name)
        if not isinstance(data, dict):
            return None
        local = time.time() if now is None else now
        try:
            expired = local > float(data.get("until") or 0)
        except (TypeError, ValueError):
            expired = True
        if expired:
            self.clear_verdict(name)
            return None
        return data

    def save_verdict(self, name: str, state: str, message: str, *, now: float) -> None:
        verdict = {
            "name": name,
            "state": state,
            "message": message,
            "at": now,
            "until": now + pairing.VERDICT_KEEP,
        }
        self._put("verdicts", name, verdict)

    def clear_verdict(self, name: str) -> bool:
        return self._pop("verdicts", name)

    def verdict_names(self) -> list[str]:
        return sorted(self._table("verdicts"))

    def taken(self, name: str) -> str | None:
        pairs = self._item.read()["pairs"]
        if name in pairs["records"]:
            return f"this computer already has a pair called {name!r}"
        if name in pairs["pending"]:
            return f"this computer is already waiting on a pairing called {name!r}"
        return None


@contextlib.contextmanager
def _stored() -> Iterator[None]:
    """A storage failure of the file store, raised as the host's own :class:`store.StoreError`.
    A plain ``OSError`` is also one of the errors ``pairing.hub_errors()`` names for the hub, so
    one must not be read as the other."""
    try:
        yield
    except store.StoreError:
        raise
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise store.StoreError(exc.errno, f"the file store could not be used ({reason})") from exc


class FilePairs:
    """Switchboard's ``PairStore`` on the host's state folder, under ``--store-file``, as a pair
    store whose storage failures are :class:`store.StoreError`, as the keyring store's are."""

    def __init__(self, inner: pairing.PairStore) -> None:
        self._inner = inner

    @property
    def enabled(self) -> bool:
        return bool(self._inner.enabled)

    def require(self) -> object:
        with _stored():
            return self._inner.require()

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with contextlib.ExitStack() as stack:
            with _stored():
                stack.enter_context(self._inner.lock())
            yield

    def get(self, name: str) -> pairing.PairRecord | None:
        with _stored():
            return self._inner.get(name)

    def save(self, record: pairing.PairRecord) -> None:
        with _stored():
            self._inner.save(record)

    def delete(self, name: str) -> bool:
        with _stored():
            return self._inner.delete(name)

    def names(self) -> list[str]:
        with _stored():
            return self._inner.names()

    def pending(self, name: str) -> pairing.PendingHandshake | None:
        with _stored():
            return self._inner.pending(name)

    def save_pending(self, pending: pairing.PendingHandshake) -> None:
        with _stored():
            self._inner.save_pending(pending)

    def delete_pending(self, name: str) -> bool:
        with _stored():
            return self._inner.delete_pending(name)

    def pending_names(self) -> list[str]:
        with _stored():
            return self._inner.pending_names()

    def verdict(self, name: str, *, now: float | None = None) -> dict[str, Any] | None:
        with _stored():
            return self._inner.verdict(name, now=now)

    def save_verdict(self, name: str, state: str, message: str, *, now: float) -> None:
        with _stored():
            self._inner.save_verdict(name, state, message, now=now)

    def clear_verdict(self, name: str) -> bool:
        with _stored():
            return self._inner.clear_verdict(name)

    def verdict_names(self) -> list[str]:
        with _stored():
            return self._inner.verdict_names()

    def taken(self, name: str) -> str | None:
        with _stored():
            return self._inner.taken(name)


class KeyringRooms:
    """Switchboard's share store (``sharing.Store``) over the same keyring item, beside the pair."""

    def __init__(self, item: store.Item) -> None:
        self.enabled = True
        self._item = item

    def require(self) -> object:
        if not self.enabled:
            raise pairing.PairingError("this profile keeps no rooms", state="disabled")
        return self._item

    def lock(self) -> contextlib.AbstractContextManager[None]:
        return self._item.rooms_lock()

    def load(self) -> dict[str, Any]:
        return self._item.read()["shared"]

    def save(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError("the share store saves one document")

        def edit(doc: dict[str, Any]) -> None:
            doc["shared"] = data

        self._item.change(edit)


class Link:
    """The stores and the client factory every pairing and sharing call of the host is given."""

    def __init__(
        self,
        pairs: Any,
        rooms: Any,
        *,
        runtime: Path,
        client_class: type | None = None,
        clock: Callable[[], float] | None = None,
        warning: str | None = None,
    ) -> None:
        self.pairs = pairs
        self.rooms = rooms
        self.runtime = runtime
        # Set under --store-file: the warning to show wherever that store is used.
        self.warning = warning
        self._client_class = client.Client if client_class is None else client_class
        self._clock = time.time if clock is None else clock
        # The id of the last note of the share store this link has returned (:meth:`notes`).
        self._noted: int | None = None

    def now(self) -> float:
        return self._clock()

    def connect(self, config: Any, *, agent_id: str) -> Any:
        """Build a client for ``config``, as pairing's and sharing's ``connect=`` hook."""
        host_state.check_runtime(self.runtime)
        if config is None:
            raise ValueError("a client is built only from a room's own configuration")
        if config.peer_log or config.stash_db:
            raise ValueError("the host keeps no Switchboard peer log or stash on disk")
        built = self._client_class(config, agent_id=agent_id, timeout=CLIENT_TIMEOUT)
        built.signing = signing.SigningIdentity.generate()
        return built

    def _pairing(self) -> dict[str, Any]:
        return {"store": self.pairs, "connect": self.connect, "now": self.now()}

    def names(self) -> list[str]:
        """The pairs this profile holds."""
        return list(self.pairs.names())

    def accept(self, code: Code) -> str:
        """Answer ``code`` with this computer's half of the pair; returns the pair's name.

        A :class:`PairFailed` with state ``unsent`` means the pair is saved and its answer has not
        reached the hub yet: the next resolve sends it again.
        """
        try:
            accepted = pairing.accept(code.text, None, **self._pairing())
        except pairing.PairingError as exc:
            raise _failed(exc) from None
        except store.StoreError:
            raise
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        return accepted.name

    def resolve(self, name: str) -> Pair:
        """Read the pair room once: confirm the pair if Lucille collected the answer, send an
        answer that has not reached the hub, and follow a move. A pair room that cannot be read
        gives ``checked=False``, what this computer last saw, and the hub's ``retry_after`` when
        it gave one: 2.6.0 returns that failure (``Resolution.hub_error``) rather than raising
        it."""
        try:
            resolution = pairing.resolve(name, None, **self._pairing())
        except pairing.PairingError as exc:
            raise _failed(exc) from None
        except store.StoreError:
            raise
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        record = resolution.record
        failed = resolution.hub_error
        return Pair(
            name=resolution.name,
            sas_known=bool(record.confirmed and record.sas),
            generation=resolution.generation,
            workspace=resolution.config.workspace,
            checked=resolution.checked,
            room=resolution.config,
            retry_after=None if failed is None else _hub_error(failed).retry_after,
        )

    def sas(self, name: str) -> str | None:
        """The pair's SAS as the library gives it (``NNNN-NNNN``, as Lucille's page shows it), for
        the local gate to show the user, and for nothing else; None until Lucille has collected
        the answer."""
        try:
            record = self.pairs.get(name)
        except pairing.PairingError as exc:
            raise _failed(exc) from None
        if record is None or not record.confirmed or not record.sas:
            return None
        return str(record.sas)

    def kept(self, name: str) -> Kept | None:
        """Pair ``name`` as this computer's store keeps it, with no hub call; None when the store
        holds no such pair."""
        try:
            record = self.pairs.get(name)
        except pairing.PairingError as exc:
            raise _failed(exc) from None
        if record is None:
            return None
        return Kept(
            name=record.name,
            sas_known=bool(record.confirmed and record.sas),
            generation=record.generation,
        )

    def forget(self, name: str) -> Told:
        """Delete the pair on this computer first, then tell Lucille, best effort. A pair that is
        already gone is forgotten too, and Lucille is not told."""
        try:
            gone = pairing.forget(name, None, **self._pairing())
        except pairing.PairingError as exc:
            if exc.state == "unknown":
                return Told(name, False)
            raise _failed(exc) from None
        return Told(name, gone.peer_told)

    def _reader(self, config: Any) -> Any:
        """A client of the host's own, which reads a body it cannot parse as one it cannot open
        (:class:`_Opener`)."""
        built = self.connect(config, agent_id=AGENT_ID)
        if built.cipher is not None:
            built.cipher = _Opener(built.cipher)
        return built

    def pair_notes(self, pair: Pair) -> PairNotes:
        """A reader of the ``pair`` channel in pair:<name>, at the generation ``pair`` names."""
        return PairNotes(self._reader(pair.room))

    def pair_client(self, pair: Pair) -> RoomClient:
        """A client of pair:<name>, at the generation ``pair`` names, for the read loop."""
        return RoomClient(self._reader(pair.room))

    def room_client(self, room: Room) -> RoomClient:
        """A client of a work room held here, for the read loop."""
        return RoomClient(self._reader(room.config))

    # --- the work rooms Lucille offers over the pair -------------------------------------

    def _sharing(self) -> dict[str, Any]:
        return {
            "store": self.rooms,
            "pair_store": self.pairs,
            "connect": self.connect,
            "now": self.now(),
        }

    def _share_document(self) -> dict[str, Any]:
        with self.rooms.lock():
            return self.rooms.load()

    def _mark(self) -> None:
        """Before the first sharing call: the notes it makes are those after this point."""
        if self._noted is None:
            try:
                seq = self._share_document().get("seq")
            except pairing.PairingError:
                return  # a share store the library cannot read: the call refuses the same way
            self._noted = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0

    def notes(self) -> tuple[ShareNote, ...]:
        """The notes the library wrote in the share store since this link last returned them,
        oldest first: those of a sharing call that returned, and those of one that refused.

        A call returns only the notes it made itself, and only when it returns: ``accept``
        keeps the notes of its first read of the pair room only on success, though that read
        has already saved them (sb ``sharing.py:1583-1627``), and every later call returns only
        notes newer than its own start (``:1246``). So an ``offered`` note made inside an accept
        that then refused would reach no caller. The share store is the host's own, and keeps
        each note under an id that only grows (``:389-393``, ``:1919-1930``)."""
        if self._noted is None:
            return ()
        try:
            found = self._share_document().get("notes")
        except pairing.PairingError:
            return ()
        fresh = sorted(
            (
                note
                for note in (found if isinstance(found, list) else [])
                if isinstance(note, dict)
                and isinstance(note.get("id"), int)
                and note["id"] > self._noted
            ),
            key=lambda note: note["id"],
        )
        if fresh:
            self._noted = fresh[-1]["id"]
        return _share_notes(fresh)

    def touch(self, pair: str) -> Touch:
        """Read pair ``pair``'s offers and write this side's records, naming that pair only.
        The library resolves the pair as part of it, without marking it used."""
        self._mark()
        try:
            touched = sharing.touch(None, pairs=[pair], **self._sharing())
        except pairing.PairingError as exc:
            raise _refused(exc, "reading the offers") from None
        except store.StoreError:
            raise
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        state = "ok" if pair in touched.reached else touched.states.get(pair, "unreachable")
        # The library keeps why a pair could not be read as text, the hub's error included.
        skewed = _SKEW_DETAIL in str(touched.unreached.get(pair, ""))
        return Touch(state=state, notes=self.notes(), skewed=skewed)

    def accept_room(self, pair: str, offer: str, name: str) -> RoomTaken:
        """Accept the room pair ``pair`` offers as ``offer``, and keep it here as ``name``. The
        library reads the live offer again first, and refuses one that is gone."""
        self._mark()
        try:
            kept = sharing.accept(f"{pair}/{offer}", None, name=name, **self._sharing())
        except pairing.PairingError as exc:
            raise _refused(exc, "accepting the room") from None
        except store.StoreError:
            raise
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        # The library keeps why the pair was not told as text, the hub's error included.
        skewed = not kept.offered_back and any(_SKEW_DETAIL in str(w) for w in kept.warnings)
        return RoomTaken(name=kept.name, notes=self.notes(), skewed=skewed)

    def room(self, name: str) -> Room:
        """The room held here as ``name``, from the share store: no hub call. The library stamps
        when it was last used."""
        try:
            shared = sharing.resolve_room(name, None, store=self.rooms, now=self.now())
        except pairing.PairingError as exc:
            raise _refused(exc, "looking up the room") from None
        config = shared.config
        return Room(
            name=shared.name,
            url=config.url,
            workspace=config.workspace,
            carries_token=config.token_source == "shared",
            pairs=tuple(shared.pairs),
            config=config,
        )

    def leave_room(self, name: str) -> RoomLeft:
        """Leave the room held here as ``name``: its keys go from the share store first, then
        the pairs on its list are told, best effort. It revokes nothing on the hub."""
        self._mark()
        try:
            left = sharing.leave(_HELD + name, None, **self._sharing())
        except pairing.PairingError as exc:
            raise _refused(exc, "leaving the room") from None
        except store.StoreError:
            raise
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        skewed = any(_SKEW_DETAIL in str(reason) for reason in left.untold.values())
        return RoomLeft(name=left.name, told=not left.untold, notes=self.notes(), skewed=skewed)


class PairNotes:
    """Reads the ``pair`` channel of pair:<name>, where the library posts its notes. Each read
    is a peek past a sequence number: it moves no cursor on the hub."""

    def __init__(self, reader: Any) -> None:
        self._reader = reader

    def wait(self, *, since: int, wait: float, limit: int = 200) -> list[Note]:
        """The messages after ``since``, waiting up to ``wait`` seconds for one to arrive."""
        try:
            rows = self._reader.inbox(
                channels=[pairing.CHANNEL], since=since, wait=wait, limit=limit
            )
        except crypto.CryptoError:
            raise Unopenable("a message in the pair's room cannot be opened") from None
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        return [Note(seq=int(row["seq"]), tag=pairing.message_tag(row)) for row in rows]

    def close(self) -> None:
        self._reader.close()

    def __enter__(self) -> PairNotes:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class RoomClient:
    """Reads and posts in one room, as the host. Built by :meth:`Link.room_client` or
    :meth:`Link.pair_client` through the host's own client factory; used by one thread."""

    def __init__(self, built: Any) -> None:
        self._client = built

    def peek(self, channel: str, *, since: int, wait: float, limit: int) -> list[Message]:
        """The messages on ``channel`` after sequence number ``since``, waiting up to ``wait``
        seconds for one to arrive: a peek that moves no cursor on the hub. One message the
        room's key cannot open makes the whole read fail with :class:`Unopenable`."""
        try:
            rows = self._client.inbox(channels=[channel], since=since, wait=wait, limit=limit)
        except crypto.CryptoError:
            raise Unopenable("a message in the room cannot be opened with its key") from None
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        own = self._client.agent_id
        return [_message(row, own) for row in rows]

    def recover(self, channel: str, *, limit: int) -> list[Message]:
        """The newest ``limit`` messages on ``channel``, the host's own included, each marked
        ``unreadable`` where the room's key cannot open it: what is read when a peek fails with
        :class:`Unopenable`."""
        hub_channel = self._client.cipher.blind_channel(channel)
        try:
            rows = self._client.read_channels([hub_channel], limit=limit)
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        own = self._client.agent_id
        return sorted((_message(row, own) for row in rows), key=lambda message: message.seq)

    def post(self, channel: str, body: Any, *, ttl: int) -> Posted:
        """Post ``body`` on ``channel``: a note, with no thread, living ``ttl`` seconds."""
        try:
            stored = self._client.post(channel, body, type="note", thread=None, ttl=ttl)
        except pairing.hub_errors() as exc:
            raise _hub_error(exc) from None
        return Posted(seq=int(stored["seq"]), created_at=_stamp(stored.get("created_at")))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RoomClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_link(
    dirs: paths.Dirs,
    config: host_state.HostConfig,
    *,
    client_class: type | None = None,
    clock: Callable[[], float] | None = None,
) -> Link:
    """The profile's stores, with ``XDG_RUNTIME_DIR`` pointed at its private runtime folder.

    The keyring store refuses a keyring backend that stores nothing (``store.check_backend``).
    ``clock``, when given, is the time both the calls and the keyring item's write throttle go by.
    """
    paths.ensure_dir(dirs.state)
    host_state.prepare_runtime(dirs.runtime)
    options = {"runtime": dirs.runtime, "client_class": client_class, "clock": clock}
    if config.store == "file":
        folder = paths.ensure_dir(dirs.store_files)
        # The share store needs no such view: sharing turns its storage failures into a refusal.
        pairs = FilePairs(pairing.PairStore(folder, env={}))
        rooms = sharing.ShareStore(folder, env={})
        return Link(pairs, rooms, warning=FILE_STORE_WARNING, **options)
    store.check_backend()
    item = store.Item(dirs.state, dirs.profile, clock=time.time if clock is None else clock)
    return Link(KeyringPairs(item), KeyringRooms(item), **options)
