"""The Secret Service's own errors at a read or write are a store error, never ``internal``
(issue 38).

On Linux, keyring's Secret Service backend can raise past keyring: secretstorage's errors, and
jeepney's D-Bus error reply, which is neither a keyring error nor an ``OSError``. A service the bus
lists as one it starts on demand, and that then fails to start, gives the reply
``org.freedesktop.DBus.Error.Spawn.ChildExited`` at the first read; a bus that goes away after the
backend was built gives secretstorage's ``SecretServiceNotAvailableException``. The store gives
each as :class:`store.KeyringUnusable`, a ``store_error``, naming the error's class and a D-Bus
error's name, and nothing more of it.

These raise the real classes, from a backend in memory, so they need both libraries (on Linux).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from switchboard.testing import hub

from lucille_host import credentials, store
from tests._cli import HostCli
from tests._host import HostEnv
from tests._keyring import MemoryKeyring
from tests.spec import _support

secretstorage_exceptions = pytest.importorskip("secretstorage.exceptions")
jeepney = pytest.importorskip("jeepney")
wrappers = pytest.importorskip("jeepney.wrappers")

SPAWN_FAILED = "org.freedesktop.DBus.Error.Spawn.ChildExited"
# What the bus says of the child, kept out of every message.
BODY = "Process org.freedesktop.secrets exited with status 1"


def _reply(name: object) -> Exception:
    """jeepney's error reply, as a proxy call raises it, for an error named ``name``."""
    message = SimpleNamespace(
        header=SimpleNamespace(fields={jeepney.HeaderFields.error_name: name}), body=(BODY,)
    )
    return wrappers.DBusErrorResponse(message)


def _raised() -> list[tuple[Callable[[], Exception], str]]:
    """Each error, and what the store's message says of it."""
    errors = secretstorage_exceptions
    return [
        (lambda: _reply(SPAWN_FAILED), f"DBusErrorResponse: {SPAWN_FAILED}"),
        (
            lambda: errors.SecretServiceNotAvailableException("[Errno 111] Connection refused"),
            "SecretServiceNotAvailableException",
        ),
        (lambda: errors.LockedException("the collection is locked"), "LockedException"),
        (lambda: errors.PromptDismissedException("dismissed"), "PromptDismissedException"),
        (lambda: errors.ItemNotFoundException("gone"), "ItemNotFoundException"),
        (lambda: errors.SecretStorageException("other"), "SecretStorageException"),
    ]


class Raising(MemoryKeyring):
    """A keyring in memory whose reads raise ``on_read()``, and writes ``on_write()``, when set."""

    def __init__(self) -> None:
        super().__init__()
        self.on_read: Callable[[], BaseException] | None = None
        self.on_write: Callable[[], BaseException] | None = None

    def get_password(self, service: str, username: str) -> str | None:
        if self.on_read is not None:
            raise self.on_read()
        return super().get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.on_write is not None:
            raise self.on_write()
        super().set_password(service, username, password)


@pytest.fixture
def raising(host_env: HostEnv, monkeypatch: pytest.MonkeyPatch) -> Raising:
    """The keyring in use, in place of host_env's, which it comes after."""
    backend = Raising()
    monkeypatch.setattr("keyring.core._keyring_backend", backend)
    return backend


def _edit(doc: dict[str, object]) -> None:
    doc["shared"] = {"seq": 1}


@pytest.mark.parametrize(("make", "detail"), _raised())
def test_a_service_error_at_a_read_or_write_of_the_pair_item_is_a_store_error(
    raising: Raising, host_env: HostEnv, make: Callable[[], Exception], detail: str
) -> None:
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    raising.on_read = make
    with pytest.raises(store.KeyringUnusable) as read:
        item.read()
    assert str(read.value) == f"the keyring could not be read ({detail})"
    assert isinstance(read.value, store.StoreError)
    raising.on_read = None
    raising.on_write = make
    with pytest.raises(store.KeyringUnusable) as written:
        item.change(_edit)
    assert str(written.value) == f"the keyring could not be written ({detail})"
    for message in (str(read.value), str(written.value)):
        assert BODY not in message and "Errno" not in message


@pytest.mark.parametrize(("make", "detail"), _raised())
def test_a_service_error_at_a_read_or_write_of_the_credential_item_is_a_store_error(
    raising: Raising, tmp_path: Path, make: Callable[[], Exception], detail: str
) -> None:
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    raising.on_read = make
    with pytest.raises(store.KeyringUnusable, match=r"^the keyring could not be read \("):
        creds.names()
    raising.on_read = None
    raising.on_write = make
    with pytest.raises(store.KeyringUnusable) as written:
        creds.save("gmail", "x", "correct-horse-battery-staple")
    assert str(written.value) == f"the keyring could not be written ({detail})"


@pytest.mark.parametrize(
    "name",
    [None, 7, "", "Spawn", "org freedesktop", "org.freedesktop.\n", "a." + "b" * 300],
    ids=["none", "not-text", "empty", "one-element", "space", "newline", "too-long"],
)
def test_an_error_reply_s_name_is_given_only_when_it_is_a_d_bus_error_name(
    raising: Raising, host_env: HostEnv, name: object
) -> None:
    raising.on_read = lambda: _reply(name)
    with pytest.raises(store.KeyringUnusable) as read:
        store.Item(host_env.dirs.state, host_env.dirs.profile).read()
    assert str(read.value) == "the keyring could not be read (DBusErrorResponse)"


class _Wider(Exception):
    """Stands for an error of another library, or a subclass of a listed class."""


def _wider_reply() -> Exception:
    """A subclass of jeepney's error reply: not one of the listed classes."""

    class Wider(wrappers.DBusErrorResponse):
        pass

    message = SimpleNamespace(
        header=SimpleNamespace(fields={jeepney.HeaderFields.error_name: SPAWN_FAILED}), body=()
    )
    return Wider(message)


@pytest.mark.parametrize("make", [lambda: _Wider("a bug"), _wider_reply], ids=["other", "subclass"])
def test_any_other_error_is_raised_as_it_is(
    raising: Raising, host_env: HostEnv, make: Callable[[], Exception]
) -> None:
    # Not the Secret Service's own: a bug, which the command reports as one.
    raising.on_read = make
    with pytest.raises(Exception) as raised:
        store.Item(host_env.dirs.state, host_env.dirs.profile).read()
    assert type(raised.value).__name__ in {"_Wider", "Wider"}
    assert not isinstance(raised.value, store.StoreError)


@pytest.fixture
def h() -> Iterator[Any]:
    with hub() as handle:
        yield handle


@pytest.fixture
def cli(
    raising: Raising,
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[HostCli]:
    host = HostCli(h, host_env, tmp_path, capsys, monkeypatch)
    yield host
    host.stopping.set()


def test_no_command_reports_a_service_error_as_internal(cli: HostCli, raising: Raising) -> None:
    # The service is listed as one the bus starts, and fails to start, at the first read.
    raising.on_read = lambda: _reply(SPAWN_FAILED)
    message = f"the keyring could not be read (DBusErrorResponse: {SPAWN_FAILED})"
    # status reports the pair it cannot read, and the rest.
    ran = cli.run("--json", "status")
    assert ran.rc == 0, ran.text
    [found] = _support.cli_objects(ran.out)
    assert found["pair"] == {"state": "unreadable", "code": "store_error", "message": message}
    assert found["paused"] is False and found["policy"]["state"] == "ok"
    for argv in (("--json", "pair", "status"), ("--json", "forget")):
        ran = cli.run(*argv)
        assert ran.rc == 1, ran.text
        assert _support.cli_objects(ran.out) == [
            {"v": 1, "type": "error", "code": "store_error", "message": message}
        ]
    # run stops at its start check, with the exit a supervisor does not restart after.
    ran = cli.run("run", "--json-events")
    assert ran.rc == 23, ran.text
    assert _support.cli_objects(ran.out) == [{"v": 1, "type": "stopped", "reason": "store_error"}]
    assert ran.err == f"lucille-host: {message}\n"
    assert "Traceback" not in ran.text and "internal" not in ran.text
