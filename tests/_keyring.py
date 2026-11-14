"""Keyring backends for the tests: one in memory, so that no test reaches the system keyring, and
one that keeps secrets in a plain file, which the host must refuse; and the host's own Linux
backend where there is no session bus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from lucille_host import store


class MemoryKeyring(KeyringBackend):
    """Keeps each item in a dict, and counts the writes and deletes."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}
        self.writes = 0
        self.deletes = 0

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password
        self.writes += 1

    def delete_password(self, service: str, username: str) -> None:
        if self.items.pop((service, username), None) is None:
            raise PasswordDeleteError("no such item")
        self.deletes += 1


class NotesKeyring(KeyringBackend):
    """Keeps every item unencrypted, as JSON, in the file :attr:`path` (``{service: {username:
    password}}``), and counts the reads and the writes: a backend that writes secrets in the clear
    to a file of its own, as keyrings.alt's ``PlaintextKeyring`` does. A test gives it a path, and
    any class name, by subclassing it (:func:`notes_keyring`). It takes no argument, so keyring
    could build it as it builds any backend, and it is not a :class:`MemoryKeyring`, which the
    tests stand in for the system keyring (conftest.py)."""

    priority = 1
    path = Path("/nonexistent/notes.json")

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.writes = 0

    def _notes(self) -> dict[str, dict[str, str]]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        return self._notes().get(service, {}).get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes += 1
        notes = self._notes()
        notes.setdefault(service, {})[username] = password
        self.path.write_text(json.dumps(notes))

    def delete_password(self, service: str, username: str) -> None:
        notes = self._notes()
        if notes.get(service, {}).pop(username, None) is None:
            raise PasswordDeleteError("no such item")
        self.writes += 1
        self.path.write_text(json.dumps(notes))


def notes_keyring(
    path: Path, module: str = __name__, qualname: str = "NotesKeyring"
) -> type[NotesKeyring]:
    """A :class:`NotesKeyring` that writes to ``path``, as a class named ``qualname`` in the module
    named ``module``: the names the host would see, whatever they are."""
    return type(
        qualname.rpartition(".")[2],
        (NotesKeyring,),
        {"__module__": module, "__qualname__": qualname, "path": path},
    )


def no_session_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the host its own Linux backend, built as a real host builds it (``store._build``, past
    its once-a-process cache), with no session bus to find a Secret Service on: what keyring's
    ``fail`` backend stands in for elsewhere in the tests. On macOS, where secretstorage is not
    installed, the class says so instead, which comes to the same."""
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(store, "_system_backend", lambda: store._build.__wrapped__("linux"))
