"""The host's stores: the keyring item, its locks and its write throttle, and the file store.

The pairing and sharing calls here are the library's own, made the way the host makes them: with
``base=None``, the host's two stores and its own ``connect``, against Switchboard's in-process
test hub, with a fake Lucille on the other side.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.backends.fail import Keyring as FailKeyring
from keyring.backends.null import Keyring as NullKeyring
from switchboard import pairing, sharing
from switchboard.testing import hub

from lucille_host import _switchboard, cli, journal, paths, state, store
from lucille_host import link as link_module
from lucille_host.controls import Controls
from tests import _audit_hook
from tests._host import HostEnv, lock_free, lock_held
from tests._keyring import MemoryKeyring, NotesKeyring, notes_keyring
from tests._lucille import FakeLucille

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

KEYRING = state.HostConfig()
FILE = state.HostConfig(store="file")


@pytest.fixture
def h() -> Iterator[Any]:
    with hub() as handle:
        yield handle


def _calls(link: _switchboard.Link, h: Any) -> dict[str, Any]:
    return {"store": link.pairs, "connect": link.connect, "now": h.now}


def _sharing(link: _switchboard.Link, h: Any) -> dict[str, Any]:
    return {"store": link.rooms, "pair_store": link.pairs, "connect": link.connect, "now": h.now}


def _pair(link: _switchboard.Link, lucille: FakeLucille, h: Any) -> str:
    """Accept Lucille's code as the host does, and let Lucille collect the answer."""
    accepted = pairing.accept(lucille.start(), None, **_calls(link, h))
    lucille.collect()
    assert pairing.resolve(accepted.name, None, **_calls(link, h)).state == "paired"
    return accepted.name


def _files(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if not path.is_dir()}


@pytest.mark.parametrize("config", [KEYRING, FILE], ids=["keyring", "file"])
def test_no_write_under_home_switchboard(
    config: state.HostConfig, host_env: HostEnv, h: Any, tmp_path: Path
) -> None:
    before = _files(host_env.home)
    link = _switchboard.open_link(host_env.dirs, config, client_class=h.client_class())
    lucille = FakeLucille(h, tmp_path)
    with _audit_hook.recording() as events:
        name = _pair(link, lucille, h)
        lucille.offer("work-1")
        sharing.touch(None, pairs=[name], **_sharing(link, h))
        kept = sharing.accept(f"{name}/work-1", None, **_sharing(link, h))
        sharing.resolve_room(kept.name, None, store=link.rooms, now=h.now)
        sharing.leave(f"shared:{kept.name}", None, **_sharing(link, h))
        pairing.forget(name, None, **_calls(link, h))
    assert not (host_env.home / ".switchboard").exists()
    written = _files(host_env.home) - before
    assert written, "the flow wrote its state somewhere"
    assert all(path.is_relative_to(host_env.dirs.state) for path in written), written
    assert _audit_hook.spawns(events) == []


# The class the host keeps the pair in on each system, as (module, qualname): stated here, and
# checked against keyring's installed classes by test_the_known_backends_are_keyrings_own_classes.
SYSTEM_BACKENDS = {
    "linux": ("keyring.backends.SecretService", "Keyring"),
    "macos": ("keyring.backends.macOS", "Keyring"),
}
# What every command prints where keyring has no backend to offer (spec/cli/v1/fixtures/
# status-unreadable.json pins it).
FAIL_SENTENCE = (
    "the keyring backend here (Keyring) has no keyring to store in, so lucille-host cannot keep "
    "its pair in it; set up the system keyring, or pair with --store-file to keep the pair in a "
    "file instead, which is weaker"
)
# An item as the host writes it, with nothing in it.
DOC = {
    "v": 1,
    "written_at": None,
    "pairs": {"records": {}, "pending": {}, "verdicts": {}},
    "shared": {},
}


def _system() -> tuple[str, str]:
    return SYSTEM_BACKENDS[paths.os_family()]


def _other_system() -> tuple[str, str]:
    return SYSTEM_BACKENDS["macos" if paths.os_family() == "linux" else "linux"]


def _dotted(identity: tuple[str, str]) -> str:
    return ".".join(identity)


class _Elsewhere(KeyringBackend):
    """A keyring in memory that is not a MemoryKeyring, so that only its own class decides
    whether the host takes it; it counts the reads and the writes."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}
        self.reads = 0
        self.writes = 0

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes += 1
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.writes += 1
        self.items.pop((service, username))


def _named(module: str, qualname: str, base: type[Any] = _Elsewhere) -> type[Any]:
    """A subclass of ``base`` that the host sees as ``module``.``qualname``."""
    return type(
        qualname.rpartition(".")[2], (base,), {"__module__": module, "__qualname__": qualname}
    )


class _CountedMemory(MemoryKeyring):
    """The tests' stand-in for the system keyring, counting the reads too."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        return super().get_password(service, username)


def _note(doc: dict[str, Any]) -> None:
    doc["shared"]["note"] = "kept"


def _refused_everywhere(
    host_env: HostEnv,
    refused: Any,
    seen: Callable[[], tuple[int, int]],
    monkeypatch: pytest.MonkeyPatch,
    files: tuple[Path, ...] = (),
) -> str:
    """Show that ``refused``, as the keyring backend, is refused when the store is opened, before
    every read of the item (on load) and before every write (on save), and that nothing reaches
    it: ``seen()``, its reads and writes, stays (0, 0), and no file in ``files`` changes. Returns
    the refusal's message."""
    kept = [path.read_bytes() for path in files]
    stand_in = host_env.keyring
    with pytest.raises(store.InsecureKeyring, match="cannot keep its pair") as refusal:
        store.check_backend(refused)
    # At open, where the file store still opens.
    monkeypatch.setattr("keyring.core._keyring_backend", refused)
    with pytest.raises(store.InsecureKeyring):
        store.check_backend()
    with pytest.raises(store.InsecureKeyring):
        _switchboard.open_link(host_env.dirs, KEYRING)
    assert _switchboard.open_link(host_env.dirs, FILE).warning is not None
    # On load: the store opened over the keyring the tests stand in for, and then the backend in
    # use changed.
    monkeypatch.setattr("keyring.core._keyring_backend", stand_in)
    link = _switchboard.open_link(host_env.dirs, KEYRING)
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    monkeypatch.setattr("keyring.core._keyring_backend", refused)
    for load in (item.read, link.pairs.names, link.rooms.load, lambda: item.change(_note)):
        with pytest.raises(store.InsecureKeyring, match="cannot keep its pair"):
            load()
    # On save: the backend in use changes after the item is read and before it is written.
    monkeypatch.setattr("keyring.core._keyring_backend", stand_in)

    def swapped(doc: dict[str, Any]) -> None:
        monkeypatch.setattr("keyring.core._keyring_backend", refused)
        _note(doc)

    with pytest.raises(store.InsecureKeyring, match="cannot keep its pair"):
        item.change(swapped)
    assert seen() == (0, 0)
    assert [path.read_bytes() for path in files] == kept
    assert stand_in.writes == 0
    return str(refusal.value)


# keyrings.alt's backend that writes secrets unencrypted to a file of its own, as it is named
# there (keyrings.alt 5.0.2, ``keyrings/alt/file.py``), where keyring would select it when
# installed; it is not installed here. A stand-in that writes a plain file, under that name.
Plaintext = notes_keyring(
    Path("/nonexistent/plaintext.json"), "keyrings.alt.file", "PlaintextKeyring"
)


@pytest.mark.parametrize(
    ("backend", "chained", "reason"),
    [
        pytest.param(FailKeyring, list, "has no keyring to store in", id="fail"),
        pytest.param(NullKeyring, list, "stores nothing", id="null"),
        pytest.param(ChainerBackend, list, "chains no keyring", id="empty-chain"),
        pytest.param(Plaintext, list, "is keyrings.alt.file.PlaintextKeyring,", id="plaintext"),
        pytest.param(
            ChainerBackend,
            lambda: [MemoryKeyring(), Plaintext()],
            "is a chain of keyring backends, and lucille-host uses one keyring backend",
            id="chain-with-plaintext",
        ),
    ],
)
def test_refuses_insecure_keyring_backend(
    backend: Callable[[], Any],
    chained: Callable[[], list[Any]],
    reason: str,
    host_env: HostEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The backends keyring finds viable, which is what a chain holds, for the whole test: keyring
    # works a chain's members out again each time it is asked.
    monkeypatch.setattr("keyring.backend.get_all_keyring", lambda: [NullKeyring(), *chained()])
    monkeypatch.setattr("keyring.core._keyring_backend", backend())
    with pytest.raises(store.InsecureKeyring, match="cannot keep its pair") as refused:
        store.check_backend()
    assert f") {reason}" in str(refused.value)
    with pytest.raises(store.InsecureKeyring):
        _switchboard.open_link(host_env.dirs, KEYRING)
    # The file store does not use the keyring, so it opens where the keyring stores nothing.
    assert _switchboard.open_link(host_env.dirs, FILE).warning is not None


def test_the_sentence_where_keyring_has_no_backend_is_unchanged(host_env: HostEnv) -> None:
    with pytest.raises(store.InsecureKeyring) as refused:
        store.check_backend(FailKeyring())
    assert str(refused.value) == FAIL_SENTENCE
    with pytest.raises(store.InsecureKeyring) as refused:
        store.check_backend(NullKeyring())
    assert str(refused.value) == FAIL_SENTENCE.replace(
        "has no keyring to store in", "stores nothing"
    )
    # A store of the host's own other than the pair's ends the refusal with its own words.
    with pytest.raises(store.InsecureKeyring) as refused:
        store.check_backend(NullKeyring(), refusal="so it cannot be used")
    assert (
        str(refused.value)
        == "the keyring backend here (Keyring) stores nothing, so it cannot be used"
    )


@pytest.mark.parametrize(
    ("module", "qualname"),
    [
        pytest.param("tests._keyring", "NotesKeyring", id="a-name-of-its-own"),
        pytest.param("keyrings.alt.file", "PlaintextKeyring", id="the-name-keyrings-alt-gives"),
        pytest.param("keyrings.alt.file", "Keyring", id="the-system-class-name"),
        pytest.param("keyring.backends.SecretService", "PlaintextKeyring", id="the-linux-module"),
        pytest.param("keyring.backends.macOS", "PlaintextKeyring", id="the-macos-module"),
        pytest.param("keyring.backends", "SecretService.Keyring", id="the-linux-name-split-again"),
        pytest.param("keyring.backends", "macOS.Keyring", id="the-macos-name-split-again"),
        pytest.param("SecretService", "Keyring", id="the-linux-module-short"),
        pytest.param("macOS", "Keyring", id="the-macos-module-short"),
    ],
)
def test_a_backend_that_writes_a_plain_file_is_refused_under_any_name(
    module: str,
    qualname: str,
    host_env: HostEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # It already holds an item the host could read.
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps({store.SERVICE: {host_env.dirs.profile: json.dumps(DOC)}}))
    refused = notes_keyring(notes, module, qualname)()
    message = _refused_everywhere(
        host_env, refused, lambda: (refused.reads, refused.writes), monkeypatch, (notes,)
    )
    assert f"({qualname.rpartition('.')[2]}) is {module}.{qualname}, and on " in message
    assert f"lucille-host uses only {_dotted(_system())}," in message


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: _named("keyrings.alt.file", "EncryptedKeyring"), id="keyrings-alt"),
        pytest.param(lambda: _named("keyring.backends.kwallet", "DBusKeyring"), id="kwallet"),
        pytest.param(lambda: _named("keyring.backends.libsecret", "Keyring"), id="libsecret"),
        pytest.param(lambda: _named("keyring.backends.Windows", "WinVaultKeyring"), id="windows"),
        pytest.param(lambda: _named(*_other_system()), id="the-other-systems-class"),
        pytest.param(lambda: _named(_system()[0], "Keyring2"), id="another-class-of-its-module"),
        pytest.param(
            lambda: _named("tests.elsewhere", "SystemKeyring", base=_named(*_system())),
            id="a-subclass-of-the-system-class",
        ),
    ],
)
def test_a_backend_the_host_does_not_know_is_refused(
    make: Callable[[], type[Any]], host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = make()()
    message = _refused_everywhere(
        host_env, refused, lambda: (refused.reads, refused.writes), monkeypatch
    )
    kind = type(refused)
    assert f"is {kind.__module__}.{kind.__qualname__}, and on " in message
    assert f"lucille-host uses only {_dotted(_system())}," in message


@pytest.mark.parametrize(
    ("chain", "members"),
    [
        pytest.param(
            ChainerBackend, lambda notes: [_named(*_system())()], id="of-the-system-class"
        ),
        pytest.param(
            ChainerBackend,
            lambda notes: [notes_keyring(notes, *_system())()],
            id="of-a-plain-file-named-as-the-system-class",
        ),
        pytest.param(ChainerBackend, lambda notes: [_CountedMemory()], id="of-the-tests-stand-in"),
        pytest.param(
            ChainerBackend,
            lambda notes: [_named(*_system())(), notes_keyring(notes)()],
            id="with-a-plain-file",
        ),
        pytest.param(
            _named("tests.elsewhere", "Chain", base=ChainerBackend),
            lambda notes: [_named(*_system())()],
            id="a-subclass-of-the-chainer",
        ),
    ],
)
def test_a_chain_of_backends_is_refused_whatever_it_holds(
    chain: Callable[[], Any],
    members: Callable[[Path], list[Any]],
    host_env: HostEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps({store.SERVICE: {host_env.dirs.profile: json.dumps(DOC)}}))
    chained = members(notes)
    for member in chained:
        if not isinstance(member, NotesKeyring):
            member.items[(store.SERVICE, host_env.dirs.profile)] = json.dumps(DOC)
    # Each is kept for the whole test: keyring works a chain's members out again each time.
    monkeypatch.setattr("keyring.backend.get_all_keyring", lambda: chained)
    refused = chain()
    assert list(refused.backends) == chained

    def seen() -> tuple[int, int]:
        return (sum(m.reads for m in chained), sum(m.writes for m in chained))

    message = _refused_everywhere(host_env, refused, seen, monkeypatch, (notes,))
    assert f"({type(refused).__name__}) is a chain of keyring backends, and " in message
    assert "lucille-host uses one keyring backend, not a chain of them" in message
    assert f"keyringrc.cfg can name the system one, {_dotted(_system())})," in message


def test_a_chain_whose_members_cannot_be_worked_out_is_refused(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A member that raises when keyring asks for its priority, as the Secret Service does when
    # the session bus goes away: refused as a chain, the code a user can act on, not an error.
    def gone() -> list[Any]:
        raise RuntimeError("the session bus went away")

    monkeypatch.setattr("keyring.backend.get_all_keyring", gone)
    monkeypatch.setattr("keyring.core._keyring_backend", ChainerBackend())
    with pytest.raises(store.InsecureKeyring, match="not a chain of them"):
        store.check_backend()
    with pytest.raises(store.InsecureKeyring, match="not a chain of them"):
        _switchboard.open_link(host_env.dirs, KEYRING)


def test_a_backend_of_the_system_class_is_read_and_written(
    host_env: HostEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not a MemoryKeyring, so taken by its class's names alone, which is all the host checks: this
    # class writes a plain file, and passes because it carries the system class's names.
    notes = tmp_path / "notes.json"
    system = notes_keyring(notes, *_system())()
    monkeypatch.setattr("keyring.core._keyring_backend", system)
    assert store.check_backend() is system
    assert _switchboard.open_link(host_env.dirs, KEYRING).warning is None
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    item.change(_note)
    assert item.read()["shared"] == {"note": "kept"}
    assert system.writes == 1 and system.reads == 2
    kept = json.loads(json.loads(notes.read_text())[store.SERVICE][host_env.dirs.profile])
    assert kept["shared"] == {"note": "kept"}
    # Another class of the same module is not that class.
    with pytest.raises(store.InsecureKeyring, match="cannot keep its pair"):
        store.check_backend(notes_keyring(notes, _system()[0], "Keyring2")())


@pytest.mark.parametrize("family", ["linux", "macos"])
def test_the_known_backends_are_keyrings_own_classes(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Imported here, never by the host, which knows them by their names alone. On Linux, importing
    # the macOS backend runs ldconfig and a compiler (ctypes' find_library), in this test only.
    from keyring.backends import SecretService, macOS

    classes = {"linux": SecretService.Keyring, "macos": macOS.Keyring}
    assert {name: (k.__module__, k.__qualname__) for name, k in classes.items()} == SYSTEM_BACKENDS
    assert store.KNOWN_BACKENDS == SYSTEM_BACKENDS
    monkeypatch.setattr(paths, "os_family", lambda platform=None: family)
    own = classes[family]()
    assert store.check_backend(own) is own
    other = classes["macos" if family == "linux" else "linux"]()
    with pytest.raises(store.InsecureKeyring, match="cannot keep its pair") as refused:
        store.check_backend(other)
    assert f"lucille-host uses only {_dotted(SYSTEM_BACKENDS[family])}," in str(refused.value)
    subclass = _named("tests.elsewhere", "Keyring", base=classes[family])
    with pytest.raises(store.InsecureKeyring, match="cannot keep its pair"):
        store.check_backend(subclass())
    # A chain of this class alone is refused too, whether or not keyring can work its members out
    # here (the Secret Service raises with no session bus).
    monkeypatch.setattr("keyring.backend.get_all_keyring", lambda: [own])
    with pytest.raises(store.InsecureKeyring, match="not a chain of them"):
        store.check_backend(ChainerBackend())


def test_the_backend_keyring_detects_here_is_judged(tmp_path: Path) -> None:
    # keyring's own detection, in a fresh interpreter with nothing in its environment but PATH and
    # an empty HOME (no keyringrc.cfg, no PYTHON_KEYRING_BACKEND, no session bus address), and the
    # host's check of what it found. Not in this process, where every test backend is registered
    # with keyring, so that detection would chain them.
    home = tmp_path / "home"
    home.mkdir()
    source = Path(store.__file__).resolve().parents[1]
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        "import keyring\n"
        "from lucille_host import store\n"
        "kind = type(keyring.get_keyring())\n"
        "try:\n"
        "    store.check_backend()\n"
        "    refusal = None\n"
        "except store.InsecureKeyring as refused:\n"
        "    refusal = str(refused)\n"
        "print(json.dumps([kind.__module__, kind.__qualname__, refusal]))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": os.defpath},
        cwd=home,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    module, qualname, refusal = json.loads(done.stdout.splitlines()[-1])
    if paths.os_family() == "macos":
        # The backend the Mac app keeps the pair in, which nothing else in CI checks.
        assert [module, qualname, refusal] == [*SYSTEM_BACKENDS["macos"], None]
    else:
        # The Secret Service needs a session bus, and there is none here.
        assert [module, qualname, refusal] == ["keyring.backends.fail", "Keyring", FAIL_SENTENCE]


# Where, in src/lucille_host/, keyring may be asked for the backend in use, and the item read or
# written: the check, and the item's one read and one write, which go through the backend the check
# returned. Nothing deletes the item.
KEYRING_USES = {
    "get_keyring": {"store.check_backend"},
    "get_password": {"store.Item._load"},
    "set_password": {"store.Item._save"},
    "delete_password": set(),
    # Two more ways to the item that bypass the check: a credential read, and a chain's members,
    # which hand out live backends. A chain is asked only whether it holds any.
    "get_credential": set(),
    "backends": {"store._unfit"},
}


class _Uses(ast.NodeVisitor):
    """Where each name in KEYRING_USES appears in one module: as an attribute, a name, an imported
    name, a string or a definition, by the qualified name of the scope it is in."""

    def __init__(self, module: str, found: dict[str, set[str]]) -> None:
        self.scope = [module]
        self.found = found

    def _use(self, name: str | None) -> None:
        if name in self.found:
            self.found[name].add(".".join(self.scope))

    def _enter(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._use(node.name)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._use(node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._use(node.id)

    def visit_alias(self, node: ast.alias) -> None:
        self._use(node.name.rpartition(".")[2])
        self._use(node.asname)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._use(node.value)


def test_the_keyring_is_reached_only_through_the_checked_backend() -> None:
    # The lint keeps keyring.get_password and its kin out of the runtime (KEYRING_SURFACE), but it
    # does not follow a backend's own members: this finds every way in by name.
    package = Path(store.__file__).resolve().parent
    found: dict[str, set[str]] = {name: set() for name in KEYRING_USES}
    for path in sorted(package.rglob("*.py")):
        module = ".".join(path.relative_to(package).with_suffix("").parts)
        _Uses(module, found).visit(ast.parse(path.read_text(encoding="utf-8"), str(path)))
    assert found == KEYRING_USES


@pytest.mark.parametrize("config", [KEYRING, FILE], ids=["keyring", "file"])
def test_a_store_failure_is_not_a_hub_error(
    config: state.HostConfig,
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A full disk, or a store that cannot be written, under either store: never read as the hub
    # out of reach, which the host waits out. Before, the file store's plain OSError was one of
    # the hub's errors to the host.
    link = _switchboard.open_link(host_env.dirs, config, client_class=h.client_class())
    name = _pair(link, FakeLucille(h, tmp_path), h)
    session = link_module.Session(host_env.dirs, link, say=lambda _: None)

    def full(*_: Any, **__: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    if config.store == "file":
        monkeypatch.setattr(pairing.PairStore, "save", full)
    else:
        # A keyring backend can raise one too: the session bus's socket, say.
        read = host_env.keyring.get_password
        monkeypatch.setattr(host_env.keyring, "get_password", full)
    h.advance(10)
    with pytest.raises(store.StoreError) as failed:
        link.resolve(name)
    assert not isinstance(failed.value, _switchboard.HubError)
    with pytest.raises(store.StoreError):
        session.resolve(name)
    if config.store == "keyring":
        # And a write: forgetting the pair here deletes its record first.
        monkeypatch.setattr(host_env.keyring, "get_password", read)
        monkeypatch.setattr(host_env.keyring, "set_password", full)
        with pytest.raises(store.StoreError):
            link.forget(name)


def test_a_share_store_the_library_cannot_read_is_a_refusal(
    host_env: HostEnv, h: Any, tmp_path: Path
) -> None:
    # The host reads the library's notes from its share store around each sharing call; a file
    # store holding something else is refused by the call, as the library refuses it, never
    # raised as the library's own error.
    link = _switchboard.open_link(host_env.dirs, FILE, client_class=h.client_class())
    name = _pair(link, FakeLucille(h, tmp_path), h)
    (host_env.dirs.store_files / "shared_rooms.json").write_text("{not json")
    for call in (
        lambda: link.touch(name),
        lambda: link.accept_room(name, "work-1", "room-1"),
        lambda: link.leave_room("room-1"),
    ):
        with pytest.raises(_switchboard.RoomError) as refused:
            call()
        assert refused.value.state == "store"
    assert link.notes() == ()


def test_store_file_warns_weaker(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = _switchboard.open_link(host_env.dirs, FILE, client_class=h.client_class())
    assert link.warning is not None and "weaker" in link.warning
    lucille = FakeLucille(h, tmp_path)
    name = _pair(link, lucille, h)
    lucille.offer("work-1")
    sharing.touch(None, pairs=[name], **_sharing(link, h))
    sharing.accept(f"{name}/work-1", None, **_sharing(link, h))
    folder = host_env.dirs.store_files
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    held = sorted(path.name for path in folder.rglob("*.json"))
    assert held == [f"{name}.json", "shared_rooms.json"]
    for path in folder.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert host_env.keyring.items == {}, "the file store keeps nothing in the keyring"
    # Nothing from the environment: a pair exported to a variable is not one of the store's.
    monkeypatch.setenv("SWITCHBOARD_PAIR_ELSEWHERE", "swbsecret1_x")
    assert link.pairs.names() == [name]
    # The keyring store says nothing of the kind.
    assert _switchboard.open_link(host_env.dirs, KEYRING).warning is None


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _record(**stamps: float) -> dict[str, Any]:
    return {"name": "p", "secret": "s", "generation": 0, **stamps}


def test_keyring_writes_throttled(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    clock = _Clock()
    item = store.Item(tmp_path / "state", "default", clock=clock)

    def put(record: dict[str, Any]) -> None:
        def edit(doc: dict[str, Any]) -> None:
            doc["pairs"]["records"]["p"] = record

        item.change(edit)

    put(_record(room_read_at=1.0))
    assert memory_keyring.writes == 1
    # Only a stamp changed, within the hour: not written.
    clock.now += 10
    put(_record(room_read_at=2.0, last_used_at=2.0))
    assert memory_keyring.writes == 1
    assert item.read()["pairs"]["records"]["p"]["room_read_at"] == 1.0
    # An hour after the last write, the stamps are written.
    clock.now += store.STAMP_EVERY
    put(_record(room_read_at=3.0))
    assert memory_keyring.writes == 2
    # Anything else is written at once, and so is every delete.
    clock.now += 1
    put(_record(room_read_at=3.0, generation=1))
    assert memory_keyring.writes == 3
    clock.now += 1
    assert item.change(lambda doc: doc["pairs"]["records"].pop("p", None) is not None)
    assert memory_keyring.writes == 4
    assert item.read()["pairs"]["records"] == {}

    def share(rooms: dict[str, Any]) -> None:
        def edit(doc: dict[str, Any]) -> None:
            doc["shared"] = {"v": 1, "rooms": rooms}

        item.change(edit)

    clock.now += 1
    share({"work-1": {"key": "k", "last_used_at": 1.0}})
    assert memory_keyring.writes == 5
    clock.now += 1
    share({"work-1": {"key": "k", "last_used_at": 2.0, "refreshed_at": 2.0}})
    assert memory_keyring.writes == 5
    share({"work-1": {"key": "k"}, "work-2": {"key": "k2"}})
    assert memory_keyring.writes == 6
    # Another process's view: a second item over the same keyring reads what the first wrote.
    other = store.Item(tmp_path / "state", "default")
    assert other.read()["shared"]["rooms"] == {"work-1": {"key": "k"}, "work-2": {"key": "k2"}}


def test_keyring_writes_throttled_through_the_library(
    memory_keyring: MemoryKeyring, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    clock = _Clock()
    item = store.Item(tmp_path / "state", "default", clock=clock)
    link = _switchboard.Link(
        _switchboard.KeyringPairs(item),
        _switchboard.KeyringRooms(item),
        runtime=state.prepare_runtime(tmp_path / "state" / "run"),
        client_class=h.client_class(),
    )
    lucille = FakeLucille(h, tmp_path)
    name = _pair(link, lucille, h)
    lucille.offer("work-1")
    sharing.touch(None, pairs=[name], **_sharing(link, h))
    sharing.accept(f"{name}/work-1", None, **_sharing(link, h))
    settled = memory_keyring.writes
    # Half an hour of the host's rhythm: a resolve every 5 s, and a look-up of the room and a
    # touch now and then. Each saves a stamp, and none is written.
    for step in range(360):
        h.advance(5)
        clock.now += 5
        pairing.resolve(name, None, **_calls(link, h))
        if step % 60 == 0:
            sharing.resolve_room("work-1", None, store=link.rooms, now=h.now)
            sharing.touch(None, pairs=[name], **_sharing(link, h))
    assert memory_keyring.writes == settled
    clock.now += store.STAMP_EVERY
    pairing.resolve(name, None, **_calls(link, h))
    assert memory_keyring.writes == settled + 1
    record = json.loads(memory_keyring.items[(store.SERVICE, "default")])
    assert record["pairs"]["records"][name]["room_read_at"] == h.now


def _blocked(action: Callable[[], object]) -> threading.Event:
    """Run ``action`` in a thread; the event is set once it has run."""
    done = threading.Event()

    def run() -> None:
        action()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


def _taking(lock: Callable[[], contextlib.AbstractContextManager[None]]) -> Callable[[], None]:
    def take() -> None:
        with lock():
            pass

    return take


def _held_in_a_thread(
    lock: Callable[[], contextlib.AbstractContextManager[None]],
) -> threading.Event:
    """Take ``lock`` in another thread, and hold it until the returned event is set."""
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with lock():
            holding.set()
            release.wait(10)

    threading.Thread(target=hold, daemon=True).start()
    assert holding.wait(10)
    return release


# A command that holds the pair store's lock, then waits for the item's: what `pair status`
# does while it resolves the pair.
_WAITS_FOR_THE_ITEM = (
    "import os, sys\n"
    "pairs = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "os.lockf(pairs, os.F_LOCK, 0)\n"
    "print('held', flush=True)\n"
    "item = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "try:\n"
    "    os.lockf(item, os.F_LOCK, 0)\n"
    "except OSError as exc:\n"
    "    print('refused', exc.errno, flush=True)\n"
    "else:\n"
    "    print('took it', flush=True)\n"
)


def test_a_deadlock_the_kernel_sees_between_threads_is_waited_out(
    memory_keyring: MemoryKeyring, tmp_path: Path
) -> None:
    # One thread of `run` is inside the item's lock; another wants the pair store's lock, which
    # a command holds while it waits for the item's. The kernel sees two processes each waiting
    # for the other, and refuses the second request with EDEADLK. It is no deadlock: the thread
    # inside lets go without waiting on anything. Before, the refusal stopped `run`.
    folder = tmp_path / "state"
    item = store.Item(folder, "default")
    entered, release = threading.Event(), threading.Event()

    def slow(doc: dict[str, Any]) -> None:
        entered.set()
        release.wait(10)

    inside = threading.Thread(target=item.change, args=(slow,), daemon=True)
    inside.start()
    assert entered.wait(10)
    command = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WAITS_FOR_THE_ITEM,
            str(folder / store.PAIRS_LOCK),
            str(folder / store.ITEM_LOCK),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert command.stdout is not None
    try:
        assert command.stdout.readline() == "held\n"
        time.sleep(0.3)  # the command waits for the item's lock now
        outcome: list[Any] = []

        def take() -> None:
            try:
                with item.pairs_lock():
                    outcome.append("taken")
            except OSError as exc:
                outcome.append(exc)

        taker = threading.Thread(target=take, daemon=True)
        taker.start()
        time.sleep(0.3)
        release.set()
        inside.join(10)
        taker.join(10)
        assert command.stdout.readline() == "took it\n"
        assert outcome == ["taken"]
    finally:
        release.set()
        command.wait(10)


def test_store_lock_excludes_threads_and_processes(
    memory_keyring: MemoryKeyring, tmp_path: Path
) -> None:
    folder = tmp_path / "state"
    first, second = store.Item(folder, "default"), store.Item(folder, "default")

    # Threads, through two items over the same files: the second waits for the first.
    for held, wanted in [
        (first.pairs_lock, _taking(second.pairs_lock)),
        (first.rooms_lock, _taking(second.rooms_lock)),
    ]:
        release = _held_in_a_thread(held)
        taken = _blocked(wanted)
        assert not taken.wait(0.3), "a lock was taken while another thread held it"
        release.set()
        assert taken.wait(10)
    entered, release = threading.Event(), threading.Event()

    def slow(doc: dict[str, Any]) -> None:
        entered.set()
        release.wait(10)

    threading.Thread(target=first.change, args=(slow,), daemon=True).start()
    assert entered.wait(10)
    read = _blocked(second.read)
    assert not read.wait(0.3), "the item was read while another thread was changing it"
    release.set()
    assert read.wait(10)

    # Processes: while another process holds a lock file, the store waits for it.
    for name, action in [
        (store.PAIRS_LOCK, _taking(first.pairs_lock)),
        (store.ROOMS_LOCK, _taking(first.rooms_lock)),
        (store.ITEM_LOCK, first.read),
    ]:
        with lock_held(folder / name):
            done = _blocked(action)
            assert not done.wait(0.3), f"{name} was taken while another process held it"
        assert done.wait(10)

    # And other processes are kept out: reading the item, which takes and drops the item's own
    # lock, inside the pair lock, through another item, leaves the pair lock held.
    with first.pairs_lock():
        second.read()
        assert not lock_free(folder / store.PAIRS_LOCK)
    assert lock_free(folder / store.PAIRS_LOCK)


def _waiting(site: str, dirs: paths.Dirs) -> tuple[Path, Callable[[], bool]]:
    """A lock file of the host's own, and what waits for it as the host does: the record lock
    (``run``'s main thread, each step), the controls' lock (``pause``) and the policy lock (a verb
    that changes the policy). The second says whether the lock was taken."""
    folder = paths.ensure_dir(dirs.state)
    if site == "record":
        record = folder / link_module.RECORD_LOCK

        def step() -> bool:
            with store.file_lock(record):
                return True

        return record, step
    if site == "controls":
        return folder / ".controls.lock", Controls(folder).pause
    paths.ensure_dir(dirs.config)

    def change() -> bool:
        with cli._policy_lock(dirs):
            return True

    return dirs.config / ".policy.lock", change


@pytest.mark.parametrize(
    "signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT], ids=["term", "hup", "int"]
)
@pytest.mark.parametrize("site", ["record", "controls", "policy"])
def test_a_signal_run_handles_does_not_end_a_wait_for_a_lock(
    home: Path, site: str, signum: int
) -> None:
    # `run`'s handlers note a stop signal and return. Python's os.lockf then raises
    # InterruptedError rather than wait on, as PEP 475 has most calls do. Before, a stop signal that
    # came while `run`'s main thread waited for the record lock (`forget` holds it through its hub
    # calls) stopped `run` with an error, and its drain never ran.
    assert threading.current_thread() is threading.main_thread()
    dirs = paths.dirs()
    lock, wait = _waiting(site, dirs)
    stops = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    # The handlers as `run` installs them, each from its default (one inherited ignored would stay
    # ignored), and put back after.
    inherited = {stop: signal.signal(stop, signal.SIG_DFL) for stop in stops}
    controls = Controls(dirs.state)
    restore = controls.install_handlers()
    main = threading.main_thread().ident
    assert main is not None
    try:
        with lock_held(lock) as release:

            def interrupt() -> None:
                time.sleep(0.3)  # the main thread waits for the lock now
                signal.pthread_kill(main, signum)
                deadline = time.monotonic() + 10
                while not controls.draining.is_set() and time.monotonic() < deadline:
                    time.sleep(0.02)
                release()

            helper = threading.Thread(target=interrupt, daemon=True)
            helper.start()
            try:
                taken = wait()
            finally:
                helper.join(10)
    finally:
        restore()
        for stop, handler in inherited.items():
            signal.signal(stop, handler)
    # The handler ran while the main thread waited, and the wait went on until the lock was free.
    assert controls.draining.is_set()
    assert taken
    assert lock_free(lock)


@pytest.mark.parametrize(
    "signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT], ids=["term", "hup", "int"]
)
def test_a_signal_run_handles_does_not_end_a_wait_on_another_thread(
    home: Path, signum: int
) -> None:
    # A signal sent to the process (by kill(1), say) may reach any thread that does not block it:
    # in `run`, a reader, the poster or the worker, each of which waits for the journal's lock. The
    # handler runs on the main thread, but the os.lockf of the thread the signal reached is
    # interrupted all the same. Before, that wait raised StoreError, which stopped `run`.
    assert threading.current_thread() is threading.main_thread()
    dirs = paths.dirs()
    lock = paths.ensure_dir(dirs.state) / journal.LOCK
    stops = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    inherited = {stop: signal.signal(stop, signal.SIG_DFL) for stop in stops}
    controls = Controls(dirs.state)
    restore = controls.install_handlers()
    seen: list[str] = []

    def wait() -> None:
        try:
            with store.file_lock(lock):
                seen.append("taken")
        except BaseException as exc:
            seen.append(repr(exc))

    try:
        with lock_held(lock) as release:
            waiter = threading.Thread(target=wait, daemon=True)
            waiter.start()
            time.sleep(0.3)  # the waiter waits for the lock now
            assert waiter.ident is not None
            signal.pthread_kill(waiter.ident, signum)
            # The main thread runs the handler here, between two of its sleeps.
            deadline = time.monotonic() + 10
            while not controls.draining.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.2)  # long enough for the waiter to stop, had the signal ended its wait
            assert waiter.is_alive(), seen
            release()
            waiter.join(10)
    finally:
        restore()
        for stop, handler in inherited.items():
            signal.signal(stop, handler)
    assert controls.draining.is_set()
    assert seen == ["taken"]
    assert lock_free(lock)


def _open_on(path: Path) -> int:
    """How many descriptors this process has open on ``path``."""
    wanted = path.stat()
    count = 0
    for entry in os.listdir("/dev/fd"):
        try:
            found = os.fstat(int(entry))
        except OSError:
            continue  # the one the listing used, closed since
        if (found.st_dev, found.st_ino) == (wanted.st_dev, wanted.st_ino):
            count += 1
    return count


def test_ctrl_c_ends_a_wait_for_a_lock_and_leaves_its_file_closed(home: Path) -> None:
    # Ctrl-C's default handler raises KeyboardInterrupt: that ends a wait for one of the host's lock
    # files, as it should, and the file the wait opened is closed. Only an OSError closed it.
    assert threading.current_thread() is threading.main_thread()
    lock = paths.ensure_dir(paths.dirs().state) / link_module.RECORD_LOCK
    inherited = signal.signal(signal.SIGINT, signal.default_int_handler)
    main = threading.main_thread().ident
    assert main is not None
    try:
        with lock_held(lock):

            def interrupt() -> None:
                time.sleep(0.3)  # the main thread waits for the lock now
                signal.pthread_kill(main, signal.SIGINT)

            helper = threading.Thread(target=interrupt, daemon=True)
            helper.start()
            try:
                with pytest.raises(KeyboardInterrupt), store.file_lock(lock):
                    pass
            finally:
                helper.join(10)
            assert _open_on(lock) == 0
    finally:
        signal.signal(signal.SIGINT, inherited)
    assert lock_free(lock)


def test_an_unreadable_item_is_left_as_it_is(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    memory_keyring.items[(store.SERVICE, "default")] = "not json"
    item = store.Item(tmp_path / "state", "default")
    with pytest.raises(store.StoreError, match="left as it is"):
        item.change(lambda doc: doc["pairs"]["records"].clear())
    assert memory_keyring.items[(store.SERVICE, "default")] == "not json"
    assert memory_keyring.writes == 0
