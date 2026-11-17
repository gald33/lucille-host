"""The local credential store: credentials for online services, kept only on this computer.

- One OS-keyring item per profile, service ``lucille-host.credentials`` and the profile's name as
  its user name, holds one JSON document: ``{"v": 1, "credentials": {"<service>/<name>":
  {"service": s, "name": n, "value": v, "hosts": [h, ...], "saved_at": t}}}``. ``hosts`` are the
  hosts the user bound the credential to when they saved it: the only ones a credential may ever be
  sent to, once something uses one (decisions/0003, "Use"). An entry saved before hosts were kept
  has none, and so may be sent nowhere. It is a separate item from the pair's (``store.py``), so a
  damaged credential item never puts the pair at risk, and ``forget`` (which ends the pair) does
  not delete the user's credentials. ``forget`` is not the way to delete them:
  :meth:`CredentialStore.remove` is.
- The value is entered on this computer, by the user, with ``lucille-host credential set``. Nothing
  Lucille sends can write, read or list this item: no request handler, operation or reply refers to
  this module, and the command line names it only in the ``credential`` verb's own function (the
  design record, decisions/0003, says what would have to change for an operation to use one).
- What may be shown, here or anywhere, is the names (service and credential name), the hosts
  each is bound to, and when each was saved. The value is returned only by
  :meth:`CredentialStore.value`, and no error message holds anything read from the item.
- The item is kept by the pair's rule (:func:`store.check_backend`), checked before every read
  and every write of it: only in the keyring backend the host builds for this system itself
  (``store.KNOWN_BACKENDS``: keyring's Keychain backend on macOS, its Secret Service backend on
  Linux), compared by its class's module and name, so a subclass, a chain of backends, or a
  backend under any other name, one that writes to a plain file among them, is refused, and is
  never read or written. Reads and writes go through the backend that check returns, never
  through keyring's own ``get_password`` and ``set_password``, which would ask keyring to choose
  one, and each is bounded on Linux as the pair item's are (:func:`store.keyring_call`). The
  refusal is this module's own (:data:`REFUSAL`): a file fallback, like the pair's
  ``--store-file``, is deliberately not offered, and the refusal does not point at one.
- ``.credentials.lock`` in the state folder is taken around each read and each read-modify-write
  of the item, the same kind of lock as the store's own (:func:`store.file_lock`).
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lucille_host import paths, store

__all__ = [
    "REFUSAL",
    "SERVICE",
    "CredentialError",
    "CredentialExists",
    "CredentialStore",
    "SavedCredential",
    "hosts_of",
]

SERVICE: Final = "lucille-host.credentials"
VERSION: Final = 1
LOCK: Final = ".credentials.lock"
# Lower-case service and credential names, the same rule Lucille checks: ``gmail``, ``github.com``.
NAME: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# A password, a token or a small JSON document; well above any of them.
MAX_VALUE_CHARS: Final = 64 * 1024
# A host a credential is bound to: a DNS name of two labels or more, lowercase ASCII (a name in
# another script is written in its ``xn--`` form), no port, no IP address.
HOST: Final = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
# Names that reach this computer or the user's own network, not a service on the internet. The
# proxy that will use a credential checks each address it connects to as well (decisions/0003,
# "Hardening"); this only stops such a name being saved.
LOCAL_SUFFIXES: Final = ("localhost", "local", "internal", "lan", "home.arpa", "arpa")
MAX_HOSTS: Final = 16


# What a refusal of the keyring says after the reason (:func:`store.check_backend`). It names no
# file fallback: there is none.
REFUSAL: Final = "so lucille-host cannot keep credentials in it; set up the system keyring"


class CredentialError(ValueError):
    """A name or value this store does not take, or no such credential."""


class CredentialExists(CredentialError):
    """A credential of that service and name is saved already, and ``replace`` was not asked."""


@dataclass(frozen=True)
class SavedCredential:
    """What may be shown about a credential: never its value."""

    service: str
    name: str
    saved_at: float
    hosts: tuple[str, ...] = ()


def _key(service: str, name: str) -> str:
    for label, value in (("service", service), ("name", name)):
        if not isinstance(value, str) or not NAME.match(value):
            raise CredentialError(
                f"{label} must be 1-64 characters of a-z, 0-9, '.', '_' or '-', "
                "starting with a letter or digit"
            )
    return f"{service}/{name}"


def hosts_of(hosts: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """``hosts`` as they are kept: each lowercased, with one trailing dot taken off, in the order
    given, each once. :class:`CredentialError` names, by its place, the first that is not a host a
    credential may be bound to (:data:`HOST`, :data:`LOCAL_SUFFIXES`), and refuses none, or more
    than :data:`MAX_HOSTS`."""
    kept: list[str] = []
    for number, given in enumerate(hosts, 1):
        # A host is named by its place, not repeated: a value pasted there by mistake is not shown.
        which = f"host {number}" if len(hosts) > 1 else "the host"
        if not isinstance(given, str):
            raise CredentialError("a host must be text")
        host = given.lower()
        if host.endswith("."):
            host = host[:-1]
        if not host.isascii():
            raise CredentialError(
                f"{which} is not plain ASCII: write a name in another script in its xn-- form"
            )
        if not HOST.match(host):
            raise CredentialError(
                f"{which} is not a host name such as mail.example.com: two labels or "
                "more, of a-z, 0-9 and '-', with no port, path or IP address"
            )
        if any(host == end or host.endswith("." + end) for end in LOCAL_SUFFIXES):
            raise CredentialError(
                f"{which} names this computer or a local network, not a service on the internet"
            )
        if host not in kept:
            kept.append(host)
    if not kept:
        raise CredentialError("a credential is bound to one host or more")
    if len(kept) > MAX_HOSTS:
        raise CredentialError(f"a credential is bound to at most {MAX_HOSTS} hosts")
    return tuple(kept)


def _saved(entry: dict[str, Any]) -> SavedCredential:
    return SavedCredential(
        entry["service"],
        entry["name"],
        float(entry["saved_at"]),
        tuple(entry.get("hosts", ())),
    )


def _empty() -> dict[str, Any]:
    return {"v": VERSION, "credentials": {}}


class CredentialStore:
    """One profile's credential item, and the lock around it."""

    def __init__(
        self,
        lock_dir: Path,
        username: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        paths.ensure_dir(lock_dir)
        # Always this module's own service, never the pair's (store.SERVICE): there is no way to
        # point the store at another item.
        self.service = SERVICE
        self.username = username
        self._clock = clock
        self._lock_path = lock_dir / LOCK

    def save(
        self,
        service: str,
        name: str,
        value: str,
        hosts: list[str] | tuple[str, ...],
        *,
        replace: bool = False,
    ) -> SavedCredential:
        """Save one credential: its value and the hosts it is bound to (:func:`hosts_of`). One of
        that service and name saved already is replaced only with ``replace``, value and hosts
        both, the hosts given now taking the place of the ones it had; otherwise
        :class:`CredentialExists`, decided under the store's lock, and nothing changes."""
        key = _key(service, name)
        bound = hosts_of(hosts)
        if not isinstance(value, str) or not value:
            raise CredentialError("the value is empty")
        if len(value) > MAX_VALUE_CHARS:
            raise CredentialError(f"the value is longer than {MAX_VALUE_CHARS} characters")
        if "\x00" in value:
            raise CredentialError("the value holds a NUL character")
        saved_at = self._clock()

        def edit(doc: dict[str, Any]) -> None:
            if key in doc["credentials"] and not replace:
                raise CredentialExists(f"credential {key} is saved already")
            doc["credentials"][key] = {
                "service": service,
                "name": name,
                "value": value,
                "hosts": list(bound),
                "saved_at": saved_at,
            }

        self._change(edit)
        return SavedCredential(service, name, saved_at, bound)

    def saved(self, service: str, name: str) -> SavedCredential | None:
        """What may be shown of one credential, or None when there is none."""
        entry = self._read()["credentials"].get(_key(service, name))
        return None if entry is None else _saved(entry)

    def names(self) -> list[SavedCredential]:
        """Every saved credential, by service then name, without values."""
        doc = self._read()
        return sorted(
            (_saved(entry) for entry in doc["credentials"].values()),
            key=lambda c: (c.service, c.name),
        )

    def value(self, service: str, name: str) -> str:
        """One credential's value. No operation calls this yet (module docstring)."""
        key = _key(service, name)
        entry = self._read()["credentials"].get(key)
        if entry is None:
            raise CredentialError(f"no credential {key}")
        return str(entry["value"])

    def remove(self, service: str, name: str) -> bool:
        """Delete one credential. True when there was one."""
        key = _key(service, name)
        return self._change(lambda doc: doc["credentials"].pop(key, None) is not None)

    # --- the item ------------------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        with store.file_lock(self._lock_path):
            return self._load()

    def _change(self, edit: Callable[[dict[str, Any]], Any]) -> Any:
        with store.file_lock(self._lock_path):
            before = self._load()
            after = copy.deepcopy(before)
            result = edit(after)
            if after != before:
                self._save(after)
            return result

    def _load(self) -> dict[str, Any]:
        backend = store.check_backend(refusal=REFUSAL)
        try:
            raw = store.keyring_call(
                lambda: backend.get_password(self.service, self.username), "read"
            )
        except store.StoreError:
            raise
        except Exception as exc:
            failed = store.keyring_failure(exc, "read")
            if failed is None:
                raise
            raise failed from exc
        if raw is None:
            return _empty()
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError):
            doc = None
        if not _well_formed(doc):
            raise store.StoreError(
                f"the keyring item {self.service}/{self.username} is not one this version "
                "wrote; it may hold the only copy of your credentials, so it is left as it is"
            )
        return doc

    def _save(self, doc: dict[str, Any]) -> None:
        backend = store.check_backend(refusal=REFUSAL)
        text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        try:
            store.keyring_call(
                lambda: backend.set_password(self.service, self.username, text), "write"
            )
        except store.StoreError:
            raise
        except Exception as exc:
            failed = store.keyring_failure(exc, "write")
            if failed is None:
                raise
            raise failed from exc


def _well_formed(doc: object) -> bool:
    if not isinstance(doc, dict) or doc.get("v") != VERSION:
        return False
    entries = doc.get("credentials")
    if not isinstance(entries, dict):
        return False
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            return False
        service, name = entry.get("service"), entry.get("name")
        if (
            not isinstance(service, str)
            or not isinstance(name, str)
            or key != f"{service}/{name}"
            or not isinstance(entry.get("value"), str)
            or not isinstance(entry.get("saved_at"), int | float)
        ):
            return False
        hosts = entry.get("hosts", [])
        if not isinstance(hosts, list) or not all(
            isinstance(host, str) and HOST.match(host) for host in hosts
        ):
            return False
    return True
