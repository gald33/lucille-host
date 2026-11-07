"""The local credential store (``credentials.py``): kept in its own keyring item, never shown
with its value, refused on a keyring that stores nothing or stores in the clear, and never
reached by anything Lucille sends."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from keyring.backends.null import Keyring as NullKeyring

from lucille_host import credentials, store
from tests._keyring import MemoryKeyring

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "lucille_host"
SECRET = "correct-horse-battery-staple"


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
