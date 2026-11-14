"""The local credential store (``credentials.py``): kept in its own keyring item, never shown
with its value, kept only in a keyring backend it names (refused on any other, on every read and
write), and never reached by anything Lucille sends."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.backends.null import Keyring as NullKeyring

from lucille_host import credentials, paths, store
from tests._keyring import MemoryKeyring

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "lucille_host"
SECRET = "correct-horse-battery-staple"


MEMORY = f"{MemoryKeyring.__module__}.{MemoryKeyring.__qualname__}"


@pytest.fixture(autouse=True)
def _memory_keyring_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-memory test keyring stands in for the system keyring here, and only here."""
    family = paths.os_family()
    trusted = dict(credentials.TRUSTED)
    trusted[family] = trusted[family] | {MEMORY}
    monkeypatch.setattr(credentials, "TRUSTED", trusted)


class FileKeyring(KeyringBackend):
    """Writes every item to a JSON file in the clear, under a name the old rule did not know."""

    priority = 1

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def _items(self) -> dict[str, str]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._items().get(f"{service}/{username}")

    def set_password(self, service: str, username: str, password: str) -> None:
        items = self._items()
        items[f"{service}/{username}"] = password
        self.path.write_text(json.dumps(items))

    def delete_password(self, service: str, username: str) -> None:
        raise NotImplementedError


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


def test_insecure_keyring_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keyring.core._keyring_backend", NullKeyring())
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    with pytest.raises(store.InsecureKeyring):
        creds.save("gmail", "x", SECRET)


def test_unknown_backend_refused_before_the_value_reaches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's case on cce15be: a backend that writes in the clear, under any class name
    but ``PlaintextKeyring``, passed the pair's rule and received the value."""
    plain = tmp_path / "plain.json"
    monkeypatch.setattr("keyring.core._keyring_backend", FileKeyring(plain))
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    with pytest.raises(credentials.UntrustedKeyring) as info:
        creds.save("gmail", "x", SECRET)
    assert not plain.exists()
    assert "FileKeyring" in str(info.value)
    assert "--store-file" not in str(info.value)


@pytest.mark.parametrize("call", ["names", "value", "remove"])
def test_every_read_and_write_checks_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    monkeypatch.setattr("keyring.core._keyring_backend", NullKeyring())
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    args = () if call == "names" else ("gmail", "x")
    with pytest.raises(credentials.UntrustedKeyring):
        getattr(creds, call)(*args)


def test_exact_class_not_a_subclass() -> None:
    from keyring.backends import macOS

    class LooksLikeKeychain(macOS.Keyring):
        pass

    credentials.check_backend(object.__new__(macOS.Keyring), platform="darwin")
    with pytest.raises(credentials.UntrustedKeyring):
        credentials.check_backend(object.__new__(LooksLikeKeychain), platform="darwin")


def test_trusted_is_per_platform() -> None:
    from keyring.backends import SecretService, macOS

    with pytest.raises(credentials.UntrustedKeyring):
        credentials.check_backend(object.__new__(SecretService.Keyring), platform="darwin")
    with pytest.raises(credentials.UntrustedKeyring):
        credentials.check_backend(object.__new__(macOS.Keyring), platform="linux")
    credentials.check_backend(object.__new__(SecretService.Keyring), platform="linux")


def test_chainer_needs_every_member_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chainer's members are every installed backend keyring finds, read when it is used."""
    good, plain = MemoryKeyring(), FileKeyring(tmp_path / "p.json")
    for members, ok in (([good], True), ([], False), ([good, plain], False)):
        monkeypatch.setattr("keyring.backend.get_all_keyring", lambda m=members: m)
        if ok:
            credentials.check_backend(ChainerBackend())
        else:
            with pytest.raises(credentials.UntrustedKeyring):
                credentials.check_backend(ChainerBackend())


def test_every_trusted_name_is_a_real_class() -> None:
    """A typo in TRUSTED would refuse a real keychain rather than let a bad one in; still, catch it.
    Each name must be the module and class of a backend keyring ships."""
    from keyring.backends import SecretService, kwallet, libsecret, macOS

    real = {
        "macos": {macOS.Keyring},
        "linux": {
            SecretService.Keyring,
            libsecret.Keyring,
            kwallet.DBusKeyring,
            kwallet.DBusKeyringKWallet4,
        },
    }
    for family, classes in real.items():
        names = {f"{c.__module__}.{c.__qualname__}" for c in classes}
        assert credentials.TRUSTED[family] - {MEMORY} == names


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
