"""The local credential store: credentials for online services, kept only on this computer.

- One OS-keyring item per profile, service ``lucille-host.credentials`` and the profile's name as
  its user name, holds one JSON document: ``{"v": 1, "credentials": {"<service>/<name>":
  {"service": s, "name": n, "value": v, "saved_at": t}}}``. It is a separate item from the pair's
  (``store.py``), so a damaged credential item never puts the pair at risk, and ``forget`` (which
  ends the pair) does not delete the user's credentials. ``forget`` is not the way to delete them:
  :meth:`CredentialStore.remove` is.
- The value is entered on this computer, by the user. Nothing Lucille sends can write, read or
  list this item: no request handler, operation or reply refers to this module (the design record,
  "Local credentials", says what would have to change for an operation to use one, and that it is
  not decided).
- What may be shown, here or anywhere, is the names (service and credential name) and when each
  was saved. The value is returned only by :meth:`CredentialStore.value`, and no error message
  holds anything read from the item.
- The keyring backend must be one this module names (:data:`TRUSTED`): on macOS the system
  Keychain, on Linux the Secret Service, libsecret or KWallet. The check is on the exact class, on
  every read and every write (:func:`check_backend`), so a backend chosen through
  ``PYTHON_KEYRING_BACKEND``, a ``keyringrc.cfg`` or an installed plugin is refused unless it is
  one of those, whatever it is called. This is stricter than the pair's rule
  (:func:`store.check_backend`), which refuses only backends it knows to be unfit. A file
  fallback, like the pair's ``--store-file``, is deliberately not offered.
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

import keyring
from keyring.backends.chainer import ChainerBackend
from keyring.errors import KeyringError

from lucille_host import paths, store

__all__ = [
    "SERVICE",
    "CredentialError",
    "CredentialStore",
    "SavedCredential",
    "UntrustedKeyring",
    "check_backend",
]

SERVICE: Final = "lucille-host.credentials"
VERSION: Final = 1
LOCK: Final = ".credentials.lock"
# Lower-case service and credential names, the same rule Lucille checks: ``gmail``, ``github.com``.
NAME: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# A password, a token or a small JSON document; well above any of them.
MAX_VALUE_CHARS: Final = 64 * 1024


# The keyring backends a credential may be kept in, by OS family (``paths.os_family``), named by
# module and class. Each one encrypts at rest and is unlocked by the user's login. A backend is
# checked by its exact class, never ``isinstance``, so a subclass is not one of these.
TRUSTED: Final[dict[str, frozenset[str]]] = {
    "macos": frozenset({"keyring.backends.macOS.Keyring"}),
    "linux": frozenset(
        {
            "keyring.backends.SecretService.Keyring",
            "keyring.backends.libsecret.Keyring",
            "keyring.backends.kwallet.DBusKeyring",
            "keyring.backends.kwallet.DBusKeyringKWallet4",
        }
    ),
}


class CredentialError(ValueError):
    """A name or value this store does not take, or no such credential."""


class UntrustedKeyring(store.InsecureKeyring):
    """The keyring backend in use is not one a credential may be kept in."""


def _class_name(backend: object) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__qualname__}"


def check_backend(backend: object | None = None, *, platform: str | None = None) -> None:
    """Refuse the keyring backend in use (or ``backend``) unless it is one of :data:`TRUSTED` for
    this OS. A chainer passes only when it chains at least one backend and every one is trusted:
    it writes to whichever of them takes the write."""
    current = keyring.get_keyring() if backend is None else backend
    trusted = TRUSTED[paths.os_family(platform)]
    members = list(current.backends) if type(current) is ChainerBackend else [current]
    untrusted = [m for m in members if _class_name(m) not in trusted]
    if not members or untrusted:
        named = _class_name(untrusted[0]) if untrusted else "a chainer of no keyring"
        raise UntrustedKeyring(
            f"the keyring backend here ({named}) is not the system keyring, so lucille-host will "
            "not keep a credential in it; credentials are kept only in the system keyring "
            "(the Keychain on macOS; Secret Service, libsecret or KWallet on Linux), with no file "
            "fallback"
        )


@dataclass(frozen=True)
class SavedCredential:
    """What may be shown about a credential: never its value."""

    service: str
    name: str
    saved_at: float


def _key(service: str, name: str) -> str:
    for label, value in (("service", service), ("name", name)):
        if not isinstance(value, str) or not NAME.match(value):
            raise CredentialError(
                f"{label} must be 1-64 characters of a-z, 0-9, '.', '_' or '-', "
                "starting with a letter or digit"
            )
    return f"{service}/{name}"


def _empty() -> dict[str, Any]:
    return {"v": VERSION, "credentials": {}}


class CredentialStore:
    """One profile's credential item, and the lock around it."""

    def __init__(
        self,
        lock_dir: Path,
        username: str,
        *,
        service: str = SERVICE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        paths.ensure_dir(lock_dir)
        self.service = service
        self.username = username
        self._clock = clock
        self._lock_path = lock_dir / LOCK

    def save(self, service: str, name: str, value: str) -> SavedCredential:
        """Save, or replace, one credential's value."""
        key = _key(service, name)
        if not isinstance(value, str) or not value:
            raise CredentialError("the value is empty")
        if len(value) > MAX_VALUE_CHARS:
            raise CredentialError(f"the value is longer than {MAX_VALUE_CHARS} characters")
        if "\x00" in value:
            raise CredentialError("the value holds a NUL character")
        saved_at = self._clock()

        def edit(doc: dict[str, Any]) -> None:
            doc["credentials"][key] = {
                "service": service,
                "name": name,
                "value": value,
                "saved_at": saved_at,
            }

        self._change(edit)
        return SavedCredential(service, name, saved_at)

    def names(self) -> list[SavedCredential]:
        """Every saved credential, by service then name, without values."""
        doc = self._read()
        return sorted(
            (
                SavedCredential(entry["service"], entry["name"], float(entry["saved_at"]))
                for entry in doc["credentials"].values()
            ),
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
        check_backend()
        try:
            raw = keyring.get_password(self.service, self.username)
        except (KeyringError, OSError) as exc:
            raise store.StoreError(f"the keyring could not be read ({type(exc).__name__})") from exc
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
        check_backend()
        text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        try:
            keyring.set_password(self.service, self.username, text)
        except (KeyringError, OSError) as exc:
            raise store.StoreError(
                f"the keyring could not be written ({type(exc).__name__})"
            ) from exc


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
    return True
