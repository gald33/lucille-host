"""The only runtime module that imports Switchboard.

The host talks to Lucille through agent-switchboard (MIT, pinned ``==2.6.*``, base install with no
extras). Keeping every import of it in this one module lets a reader see the whole surface the
host uses in one place, and the Switchboard-surface lint (``tools/guards/switchboard_lint.py``)
fails CI on a ``switchboard`` import or use anywhere else in the runtime package, including one
that reaches it through this module.

The surface is the plain synchronous library. The calls the design names are:

- ``switchboard.pairing``: ``PairingCode.decode``, ``accept``, ``resolve``, ``forget`` and
  ``status``, with ``PairStore`` for ``--store-file``;
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
  store (:class:`KeyringRooms`), or, under ``--store-file``, ``PairStore`` and ``ShareStore`` on
  ``<state>/pairs`` with an empty environment, so that no ``SWITCHBOARD_PAIR_*`` variable is read;
- its own way of building a client (:meth:`Link.connect`), which refuses unless
  ``XDG_RUNTIME_DIR`` names the host's private runtime folder (``state.py``), refuses a
  configuration that would keep Switchboard's peer log or stash on disk, and gives the client a
  fresh in-memory signing identity in place of whatever it found.

Every call is made with ``base=None``, so the library builds each room's configuration itself,
with no file of its own and no hub token but the one the managed hub publishes.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from switchboard import client, pairing, sharing, signing

from lucille_host import paths, store
from lucille_host import state as host_state

# Nothing here is for ``from lucille_host._switchboard import *``, which the lints refuse anyway.
__all__: list[str] = []

# The hub the host talks to. A pairing code naming any other hub is refused (a loopback hub is
# accepted only when the local configuration names it, for the conformance suite).
LUCILLE_HUB: Final = "https://switchboard.lucille-ai.com"

# A long-poll holds for up to 25 s; the client waits longer than that for an answer.
CLIENT_TIMEOUT: Final = 40.0

FILE_STORE_WARNING: Final = (
    "the file store keeps the pair secret and the work rooms' keys unencrypted, in files only "
    "your user can read, in the host's state folder; that is weaker than the OS keyring"
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


def open_link(
    dirs: paths.Dirs,
    config: host_state.HostConfig,
    *,
    client_class: type | None = None,
    clock: Callable[[], float] | None = None,
) -> Link:
    """The profile's stores, with ``XDG_RUNTIME_DIR`` pointed at its private runtime folder.

    The keyring store refuses a keyring backend that stores nothing (``store.check_backend``).
    """
    paths.ensure_dir(dirs.state)
    host_state.prepare_runtime(dirs.runtime)
    options = {"runtime": dirs.runtime, "client_class": client_class, "clock": clock}
    if config.store == "file":
        folder = paths.ensure_dir(dirs.store_files)
        pairs = pairing.PairStore(folder, env={})
        rooms = sharing.ShareStore(folder, env={})
        return Link(pairs, rooms, warning=FILE_STORE_WARNING, **options)
    store.check_backend()
    item = store.Item(dirs.state, dirs.profile)
    return Link(KeyringPairs(item), KeyringRooms(item), **options)
