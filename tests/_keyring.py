"""Keyring backends for the tests: one in memory, so that no test reaches the system keyring, and
one that keeps secrets in a plain file, which the host must refuse; the host's own Linux backend
where there is no session bus; and keyring's own off switch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from lucille_host import paths, store

# keyring's null backend, which stores nothing, and its fail backend, by the names keyring's own
# settings give them: what keyring's maintainer documents for turning the keyring off.
NULL_BACKEND = "keyring.backends.null.Keyring"
FAIL_BACKEND = "keyring.backends.fail.Keyring"
# What `keyring --disable` writes to keyringrc.cfg, byte for byte (keyring 25.7.0, keyring/cli.py
# calls keyring/core.py's disable()).
DISABLED = "[backend]\ndefault-keyring=keyring.backends.null.Keyring"
# What every command says where keyring's own settings turn the keyring off: the host's refusal of
# keyring's null backend.
NULL_SENTENCE = (
    "the keyring backend here (Keyring) stores nothing, so lucille-host cannot keep its pair in "
    "it; set up the system keyring, or pair with --store-file to keep the pair in a file instead, "
    "which is weaker"
)


def keyring_config(home: Path) -> Path:
    """Where keyring 25.7.0 reads ``keyringrc.cfg`` on Linux and on macOS when ``XDG_CONFIG_HOME``
    is unset: ``~/.config/python_keyring/keyringrc.cfg``."""
    return home / ".config" / "python_keyring" / "keyringrc.cfg"


def write_keyring_config(home: Path, text: str) -> Path:
    path = keyring_config(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def turned_off(monkeypatch: pytest.MonkeyPatch, home: Path, setting: str) -> None:
    """Turn the keyring off the way keyring's own settings do, under the private ``home``:
    ``variable-null`` and ``variable-fail`` set ``PYTHON_KEYRING_BACKEND`` to keyring's null or fail
    backend, ``configuration-null`` writes what ``keyring --disable`` writes to keyringrc.cfg, and
    ``configuration-fail`` writes keyringrc.cfg naming the fail backend.
    Then give the host its own backend, built as a real host builds it (``store._build``, past its
    once-a-process cache), with no session bus, so that a host that did not honour the setting
    would refuse with the sentence for no keyring instead."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    if setting == "variable-null":
        monkeypatch.setenv("PYTHON_KEYRING_BACKEND", NULL_BACKEND)
    elif setting == "variable-fail":
        monkeypatch.setenv("PYTHON_KEYRING_BACKEND", FAIL_BACKEND)
    elif setting == "configuration-null":
        write_keyring_config(home, DISABLED)
    elif setting == "configuration-fail":
        write_keyring_config(home, f"[backend]\ndefault-keyring = {FAIL_BACKEND}\n")
    else:
        raise AssertionError(setting)
    monkeypatch.setattr(
        store, "_system_backend", lambda: store._build.__wrapped__(paths.os_family())
    )


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


def planted_module(folder: Path, dotted: str, marker: Path) -> Path:
    """Write the module ``dotted`` (``a.b.c``, with a package for each part before the last) under
    ``folder``, where importing it writes ``marker``: a backend module somebody could name in
    keyring's settings, which shows whether it was ever imported. Returns ``folder``."""
    *packages, name = dotted.split(".")
    where = folder
    for package in packages:
        where = where / package
        where.mkdir(parents=True, exist_ok=True)
        (where / "__init__.py").write_text("", encoding="utf-8")
    folder.mkdir(parents=True, exist_ok=True)
    (where / f"{name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text({dotted!r})\n"
        "from keyring.backends.null import Keyring\n"
        "PlaintextKeyring = Keyring\n",
        encoding="utf-8",
    )
    return folder
