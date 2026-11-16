"""The local credential store (``credentials.py``): kept in its own keyring item, never shown
with its value, kept only in the keyring backend the host builds for this system, by the pair's
rule (refused on any other, before every read and every write), and never reached by anything
Lucille sends."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.backends.fail import Keyring as FailKeyring
from keyring.backends.null import Keyring as NullKeyring

from lucille_host import credentials, paths, store
from tests._keyring import MemoryKeyring

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "lucille_host"
SECRET = "correct-horse-battery-staple"


class FileKeyring(KeyringBackend):
    """Writes every item to a JSON file in the clear, under a name the host does not know."""

    priority = 1

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.calls = 0

    def _items(self) -> dict[str, str]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def get_password(self, service: str, username: str) -> str | None:
        self.calls += 1
        return self._items().get(f"{service}/{username}")

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls += 1
        items = self._items()
        items[f"{service}/{username}"] = password
        self.path.write_text(json.dumps(items))

    def delete_password(self, service: str, username: str) -> None:
        raise NotImplementedError


def _system_class() -> type[KeyringBackend]:
    """The class of the backend the host builds on this system, imported for the test: only this
    system's module, as the host imports it."""
    kind: type[KeyringBackend]
    if paths.os_family() == "macos":
        from keyring.backends import macOS

        kind = macOS.Keyring
    else:
        from keyring.backends import SecretService

        kind = SecretService.Keyring
    assert (kind.__module__, kind.__qualname__) == store.KNOWN_BACKENDS[paths.os_family()]
    return kind


@pytest.fixture
def creds(tmp_path: Path, memory_keyring: MemoryKeyring) -> credentials.CredentialStore:
    return credentials.CredentialStore(tmp_path / "state", "default", clock=lambda: 1000.0)


def test_save_list_value_remove(creds: credentials.CredentialStore) -> None:
    saved = creds.save("gmail", "app-password", SECRET)
    assert saved == credentials.SavedCredential("gmail", "app-password", 1000.0)
    creds.save("bank", "login", "x")
    assert [(c.service, c.name) for c in creds.names()] == [
        ("bank", "login"),
        ("gmail", "app-password"),
    ]
    assert creds.value("gmail", "app-password") == SECRET
    creds.save("gmail", "app-password", "replaced")
    assert creds.value("gmail", "app-password") == "replaced"
    assert creds.remove("gmail", "app-password") is True
    assert creds.remove("gmail", "app-password") is False
    with pytest.raises(credentials.CredentialError):
        creds.value("gmail", "app-password")


def test_own_item_beside_the_pair(
    creds: credentials.CredentialStore, memory_keyring: MemoryKeyring
) -> None:
    creds.save("gmail", "x", SECRET)
    assert list(memory_keyring.items) == [(credentials.SERVICE, "default")]
    assert credentials.SERVICE != store.SERVICE


def test_names_never_carry_the_value(creds: credentials.CredentialStore) -> None:
    creds.save("gmail", "x", SECRET)
    assert SECRET not in repr(creds.names())


@pytest.mark.parametrize("name", ["", "Gmail", "a b", "a/b", "-x", "x" * 65])
def test_bad_names_refused(creds: credentials.CredentialStore, name: str) -> None:
    with pytest.raises(credentials.CredentialError):
        creds.save(name, "ok", SECRET)
    with pytest.raises(credentials.CredentialError):
        creds.save("ok", name, SECRET)


@pytest.mark.parametrize("value", ["", "a\x00b", "x" * (credentials.MAX_VALUE_CHARS + 1)])
def test_bad_values_refused(creds: credentials.CredentialStore, value: str) -> None:
    with pytest.raises(credentials.CredentialError) as info:
        creds.save("gmail", "x", value)
    if value:
        assert value not in str(info.value)


def _every_use(creds: credentials.CredentialStore) -> Iterator[Callable[[], object]]:
    yield lambda: creds.save("gmail", "y", SECRET)
    yield creds.names
    yield lambda: creds.value("gmail", "x")
    yield lambda: creds.remove("gmail", "x")


def _unknown(kind: str, tmp_path: Path) -> KeyringBackend:
    class LooksLikeTheSystems(_system_class()):  # type: ignore[misc, valid-type]
        """A subclass of the system's own class: not that class."""

    made: dict[str, Callable[[], KeyringBackend]] = {
        "fail": FailKeyring,
        "null": NullKeyring,
        "plain-file": lambda: FileKeyring(tmp_path / "plain.json"),
        "chain": lambda: object.__new__(ChainerBackend),
        "subclass": lambda: object.__new__(LooksLikeTheSystems),
    }
    return made[kind]()


@pytest.mark.parametrize("kind", ["fail", "null", "plain-file", "chain", "subclass"])
def test_a_keyring_the_host_does_not_know_is_refused_for_every_use(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's case on cce15be among them: a backend that writes in the clear under a name
    of its own passed the pair's old rule and received the value."""
    backend = _unknown(kind, tmp_path)
    monkeypatch.setattr("keyring.core._keyring_backend", backend)
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    for use in _every_use(creds):
        with pytest.raises(store.InsecureKeyring) as info:
            use()
        message = str(info.value)
        assert message.endswith(credentials.REFUSAL)
        assert "--store-file" not in message
        assert SECRET not in message
    if isinstance(backend, FileKeyring):
        assert backend.calls == 0
        assert not backend.path.exists()


def test_a_keyring_that_changes_before_the_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backend is checked again before the write, not only when the item is read."""
    good, plain = MemoryKeyring(), FileKeyring(tmp_path / "plain.json")
    built = iter([good, plain])
    monkeypatch.setattr(store, "_system_backend", lambda: next(built))
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    with pytest.raises(store.InsecureKeyring, match="cannot keep credentials"):
        creds.save("gmail", "x", SECRET)
    assert plain.calls == 0
    assert not plain.path.exists()
    assert good.items == {}


def test_the_systems_own_backend_is_the_one_used(
    tmp_path: Path, memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads and writes go to the backend the pair's check returns, never through keyring's own
    module-level calls, which would ask keyring to choose one."""
    import keyring

    def chosen_by_keyring(*_: object) -> None:
        raise AssertionError("keyring was asked to choose a backend")

    for name in ("get_keyring", "get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, chosen_by_keyring)
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    creds.save("gmail", "x", SECRET)
    assert creds.value("gmail", "x") == SECRET
    assert list(memory_keyring.items) == [(credentials.SERVICE, "default")]


def test_foreign_item_left_alone(
    creds: credentials.CredentialStore, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.items[(credentials.SERVICE, "default")] = json.dumps({"v": 99, "secret": SECRET})
    with pytest.raises(store.StoreError) as info:
        creds.save("gmail", "x", "new")
    assert SECRET not in str(info.value)
    assert json.loads(memory_keyring.items[(credentials.SERVICE, "default")])["v"] == 99


def test_nothing_else_in_the_runtime_imports_it() -> None:
    """No request handler, operation or reply can reach a credential: no runtime module but this
    one names the module. When an operation is allowed to use one, this test changes with the
    design record."""
    importers = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "credentials.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n == "credentials" or n.endswith(".credentials") for n in names):
                importers.append(str(path.relative_to(PACKAGE)))
    assert importers == []
