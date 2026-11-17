"""The local credential store (``credentials.py``): kept in its own keyring item, never shown
with its value, kept only in the keyring backend the host builds for this system, by the pair's
rule (refused on any other, before every read and every write), and never reached by anything
Lucille sends."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.backends.fail import Keyring as FailKeyring
from keyring.backends.null import Keyring as NullKeyring

from lucille_host import credentials, paths, store
from tests._keyring import MemoryKeyring, turned_off

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "lucille_host"
SECRET = "correct-horse-battery-staple"
HOSTS = ("imap.gmail.com",)


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
    saved = creds.save("gmail", "app-password", SECRET, HOSTS)
    assert saved == credentials.SavedCredential("gmail", "app-password", 1000.0, HOSTS)
    creds.save("bank", "login", "x", HOSTS)
    assert [(c.service, c.name) for c in creds.names()] == [
        ("bank", "login"),
        ("gmail", "app-password"),
    ]
    assert creds.value("gmail", "app-password") == SECRET
    with pytest.raises(credentials.CredentialExists):
        creds.save("gmail", "app-password", "replaced", HOSTS)
    assert creds.value("gmail", "app-password") == SECRET
    creds.save("gmail", "app-password", "replaced", HOSTS, replace=True)
    assert creds.value("gmail", "app-password") == "replaced"
    assert creds.remove("gmail", "app-password") is True
    assert creds.remove("gmail", "app-password") is False
    with pytest.raises(credentials.CredentialError):
        creds.value("gmail", "app-password")


def test_own_item_beside_the_pair(
    creds: credentials.CredentialStore, memory_keyring: MemoryKeyring
) -> None:
    creds.save("gmail", "x", SECRET, HOSTS)
    assert list(memory_keyring.items) == [(credentials.SERVICE, "default")]
    assert credentials.SERVICE != store.SERVICE


def test_names_never_carry_the_value(creds: credentials.CredentialStore) -> None:
    creds.save("gmail", "x", SECRET, HOSTS)
    assert SECRET not in repr(creds.names())


# "gmail\n": a name is matched whole, so a newline at its end is refused too.
@pytest.mark.parametrize("name", ["", "Gmail", "a b", "a/b", "-x", "x" * 65, "gmail\n", "\ngmail"])
def test_bad_names_refused(creds: credentials.CredentialStore, name: str) -> None:
    with pytest.raises(credentials.CredentialError):
        creds.save(name, "ok", SECRET, HOSTS)
    with pytest.raises(credentials.CredentialError):
        creds.save("ok", name, SECRET, HOSTS)


@pytest.mark.parametrize("value", ["", "a\x00b", "x" * (credentials.MAX_VALUE_CHARS + 1)])
def test_bad_values_refused(creds: credentials.CredentialStore, value: str) -> None:
    with pytest.raises(credentials.CredentialError) as info:
        creds.save("gmail", "x", value, HOSTS)
    if value:
        assert value not in str(info.value)


def _every_use(creds: credentials.CredentialStore) -> Iterator[Callable[[], object]]:
    yield lambda: creds.save("gmail", "y", SECRET, HOSTS)
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


@pytest.mark.parametrize(
    "setting", ["variable-null", "variable-fail", "configuration-null", "configuration-fail"]
)
def test_keyrings_off_switch_refuses_every_use_with_the_credentials_sentence(
    setting: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keyring's own off switch, as PYTHON_KEYRING_BACKEND or keyringrc.cfg's default-keyring
    names its null or fail backend: the host takes the null backend, as it does for the pair, and
    refuses it with this module's sentence, not the pair's."""
    turned_off(monkeypatch, home, setting)
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    for use in _every_use(creds):
        with pytest.raises(store.InsecureKeyring) as info:
            use()
        assert str(info.value) == (
            f"the keyring backend here (Keyring) stores nothing, {credentials.REFUSAL}"
        )


def test_the_store_never_uses_the_pairs_item(
    creds: credentials.CredentialStore, memory_keyring: MemoryKeyring
) -> None:
    """The item's service is the module's own, and the constructor takes no other."""
    assert creds.service == credentials.SERVICE != store.SERVICE
    with pytest.raises(TypeError):
        credentials.CredentialStore(Path("unused"), "default", service=store.SERVICE)  # type: ignore[call-arg]
    creds.save("gmail", "x", SECRET, HOSTS)
    assert (store.SERVICE, "default") not in memory_keyring.items


def test_a_keyring_that_changes_before_the_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backend is checked again before the write, not only when the item is read."""
    good, plain = MemoryKeyring(), FileKeyring(tmp_path / "plain.json")
    built = iter([good, plain])
    monkeypatch.setattr(store, "_system_backend", lambda: next(built))
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    with pytest.raises(store.InsecureKeyring, match="cannot keep credentials"):
        creds.save("gmail", "x", SECRET, HOSTS)
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
    creds.save("gmail", "x", SECRET, HOSTS)
    assert creds.value("gmail", "x") == SECRET
    assert list(memory_keyring.items) == [(credentials.SERVICE, "default")]


def test_foreign_item_left_alone(
    creds: credentials.CredentialStore, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.items[(credentials.SERVICE, "default")] = json.dumps({"v": 99, "secret": SECRET})
    with pytest.raises(store.StoreError) as info:
        creds.save("gmail", "x", "new", HOSTS)
    assert SECRET not in str(info.value)
    assert json.loads(memory_keyring.items[(credentials.SERVICE, "default")])["v"] == 99


def _importers() -> list[tuple[str, str]]:
    """Each runtime module but this one that names the credential store, with the function the
    import is in ("" at module level)."""
    found: list[tuple[str, str]] = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "credentials.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = str(path.relative_to(PACKAGE))

        def visit(node: ast.AST, function: str, where: str = where) -> None:
            for child in ast.iter_child_nodes(node):
                inner = function
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    inner = child.name if not function else f"{function}.{child.name}"
                names: list[str] = []
                if isinstance(child, ast.ImportFrom):
                    names = [child.module or ""] + [a.name for a in child.names]
                elif isinstance(child, ast.Import):
                    names = [a.name for a in child.names]
                if any(n == "credentials" or n.endswith(".credentials") for n in names):
                    found.append((where, function))
                visit(child, inner, where)

        visit(tree, "")
    return sorted(found)


def test_only_the_credential_verb_imports_it() -> None:
    """No request handler, operation or reply can reach a credential: no runtime module but this
    one names the module, except the command line's ``credential`` verb, in its own function, so
    that no other verb's process (``run`` among them) loads it. When an operation is allowed to
    use one, this test changes with the design record."""
    assert _importers() == [("cli.py", "_credential_verb")]


def test_the_credential_verb_never_reads_a_value() -> None:
    """The verb saves, lists and removes; it never reads a value back (nothing uses one yet)."""
    tree = ast.parse((PACKAGE / "cli.py").read_text(encoding="utf-8"))
    verb = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in ("_credential", "_credential_verb")
    ]
    assert len(verb) == 2
    read = [
        node.lineno
        for function in verb
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute) and node.attr == "value"
    ]
    assert read == []


def test_importing_the_command_line_does_not_load_it() -> None:
    """``run`` and every other verb but ``credential`` run without the module loaded."""
    code = "import sys, lucille_host.cli; print('lucille_host.credentials' in sys.modules)"
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "False", done.stdout + done.stderr


@pytest.mark.parametrize(
    "hosts",
    [
        ("localhost",),
        ("gmail",),
        ("1.2.3.4",),
        ("[::1]",),
        ("gmail.com:443",),
        ("https://gmail.com",),
        ("mail.gmail.com/inbox",),
        ("printer.local",),
        ("router.lan",),
        ("nas.home.arpa",),
        ("db.internal",),
        ("dev.localhost",),
        ("bücher.de",),
        ("a..b.com",),
        ("-a.com",),
        ("a.123",),
        # Matched whole: a newline at the end is refused, also after a local name.
        ("imap.gmail.com\n",),
        ("foo.localhost\n",),
        # ASCII is checked before lowercasing: the Kelvin sign lowercases to "k".
        ("\u212aelvin.example.com",),
        # An xn-- label that IDNA does not decode.
        ("xn--abc.example.com",),
        ("xn--zzzzzzzz-a.example.com",),
        ("mail.xn--a1b2",),
        # More names of this computer, or names that resolve nowhere on the internet.
        ("localhost.localdomain",),
        ("printer.test",),
        ("x.invalid",),
        (),
        tuple(f"h{n}.example.com" for n in range(credentials.MAX_HOSTS + 1)),
    ],
)
def test_hosts_that_are_not_internet_names_are_refused(
    creds: credentials.CredentialStore, hosts: tuple[str, ...]
) -> None:
    with pytest.raises(credentials.CredentialError):
        creds.save("gmail", "x", SECRET, hosts)
    assert creds.names() == []


def test_hosts_are_kept_lowercased_once_each_in_order(creds: credentials.CredentialStore) -> None:
    saved = creds.save(
        "gmail", "x", SECRET, ["SMTP.Gmail.com.", "imap.gmail.com", "smtp.gmail.com"]
    )
    assert saved.hosts == ("smtp.gmail.com", "imap.gmail.com")
    assert creds.names()[0].hosts == ("smtp.gmail.com", "imap.gmail.com")
    assert creds.saved("gmail", "x") == saved
    assert creds.saved("gmail", "y") is None
    # Replacing a credential replaces its hosts.
    creds.save("gmail", "x", SECRET, ["xn--bcher-kva.example"], replace=True)
    assert creds.names()[0].hosts == ("xn--bcher-kva.example",)


def test_an_entry_saved_without_hosts_is_read_with_none(
    creds: credentials.CredentialStore, memory_keyring: MemoryKeyring
) -> None:
    # Saved before hosts were kept (no release did): it is read, bound to no host, and may be
    # sent nowhere once something uses credentials.
    entry = {"service": "gmail", "name": "x", "value": SECRET, "saved_at": 5.0}
    doc = {"v": 1, "credentials": {"gmail/x": entry}}
    memory_keyring.items[(credentials.SERVICE, "default")] = json.dumps(doc)
    assert creds.names() == [credentials.SavedCredential("gmail", "x", 5.0, ())]


@pytest.mark.parametrize(
    "hosts", ["imap.gmail.com", ["Gmail.com"], [1], ["localhost"], ["imap.gmail.com\n"]]
)
def test_an_item_with_hosts_this_version_would_not_write_is_left_alone(
    creds: credentials.CredentialStore, memory_keyring: MemoryKeyring, hosts: object
) -> None:
    entry = {"service": "gmail", "name": "x", "value": SECRET, "saved_at": 5.0, "hosts": hosts}
    text = json.dumps({"v": 1, "credentials": {"gmail/x": entry}})
    memory_keyring.items[(credentials.SERVICE, "default")] = text
    with pytest.raises(store.StoreError):
        creds.names()
    assert memory_keyring.items[(credentials.SERVICE, "default")] == text


def test_a_name_in_another_script_written_as_idna_writes_it_is_kept(
    creds: credentials.CredentialStore,
) -> None:
    saved = creds.save("shop", "x", SECRET, ["xn--bcher-kva.example", "xn--55qx5d.cn"])
    assert saved.hosts == ("xn--bcher-kva.example", "xn--55qx5d.cn")


def test_a_bad_xn_label_has_its_own_message() -> None:
    with pytest.raises(credentials.CredentialError) as info:
        credentials.hosts_of(["xn--abc.example.com"])
    assert str(info.value) == (
        "the host has an xn-- label that is not a name in another script as IDNA writes one"
    )
