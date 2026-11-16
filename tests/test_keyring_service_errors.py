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

import inspect
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from keyring.errors import InitError
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


ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"


def _raised() -> list[Any]:
    """Each error, what the store's message says of it, and whether it says the service could not
    be reached or started (:class:`store.KeyringUnusable`) or is a plain store error."""
    errors = secretstorage_exceptions
    unusable, plain = True, False
    cases = [
        (lambda: _reply(SPAWN_FAILED), f"DBusErrorResponse: {SPAWN_FAILED}", unusable),
        (
            lambda: _reply("org.freedesktop.DBus.Error.Spawn.ChildSignaled"),
            "DBusErrorResponse: org.freedesktop.DBus.Error.Spawn.ChildSignaled",
            unusable,
        ),
        (
            lambda: errors.SecretServiceNotAvailableException("[Errno 111] Connection refused"),
            "SecretServiceNotAvailableException",
            unusable,
        ),
        # A service that runs, and says no: not one to fall back from.
        (lambda: _reply(ACCESS_DENIED), f"DBusErrorResponse: {ACCESS_DENIED}", plain),
        (lambda: errors.LockedException("Collection is locked!"), "LockedException", plain),
        (
            lambda: errors.PromptDismissedException("Prompt dismissed."),
            "PromptDismissedException",
            plain,
        ),
        (
            lambda: errors.ItemNotFoundException("No collections found."),
            "ItemNotFoundException",
            plain,
        ),
        (lambda: errors.SecretStorageException("other"), "SecretStorageException", plain),
    ]
    ids = [
        "spawn-child-exited",
        "spawn-child-signaled",
        "not-available",
        "access-denied",
        "locked",
        "prompt-dismissed",
        "item-not-found",
        "base",
    ]
    return [pytest.param(*case, id=name) for case, name in zip(cases, ids, strict=True)]


def _check(raised: BaseException, unusable: bool) -> None:
    assert isinstance(raised, store.StoreError)
    assert isinstance(raised, store.KeyringUnusable) is unusable
    assert not isinstance(raised, store.Unanswered)


def test_every_error_class_secretstorage_defines_is_named() -> None:
    # The store matches classes exactly, by name, so a class a later secretstorage adds would
    # reach a command as `internal`: this fails first, for the list to be extended, with a case
    # of its own.
    defined = {
        (kind.__module__, kind.__qualname__)
        for _, kind in inspect.getmembers(secretstorage_exceptions, inspect.isclass)
        if issubclass(kind, Exception) and kind.__module__ == "secretstorage.exceptions"
    }
    named = {kind for kind in store.SERVICE_ERRORS if kind[0] == "secretstorage.exceptions"}
    assert defined == named
    # And each named class has a case of its own in _raised().
    cased = {case.values[1].split(":")[0] for case in _raised()}
    assert {qualname for _, qualname in store.SERVICE_ERRORS} <= cased


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


@pytest.mark.parametrize(("make", "detail", "unusable"), _raised())
def test_a_service_error_at_a_read_or_write_of_the_pair_item_is_a_store_error(
    raising: Raising,
    host_env: HostEnv,
    make: Callable[[], Exception],
    detail: str,
    unusable: bool,
) -> None:
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    raising.on_read = make
    with pytest.raises(store.StoreError) as read:
        item.read()
    assert str(read.value) == f"the keyring could not be read ({detail})"
    _check(read.value, unusable)
    raising.on_read = None
    raising.on_write = make
    with pytest.raises(store.StoreError) as written:
        item.change(_edit)
    assert str(written.value) == f"the keyring could not be written ({detail})"
    _check(written.value, unusable)
    for message in (str(read.value), str(written.value)):
        assert BODY not in message and "Errno" not in message and "!" not in message


@pytest.mark.parametrize(("make", "detail", "unusable"), _raised())
def test_a_service_error_at_a_read_or_write_of_the_credential_item_is_a_store_error(
    raising: Raising,
    tmp_path: Path,
    make: Callable[[], Exception],
    detail: str,
    unusable: bool,
) -> None:
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    raising.on_read = make
    with pytest.raises(store.StoreError) as read:
        creds.names()
    assert str(read.value) == f"the keyring could not be read ({detail})"
    _check(read.value, unusable)
    raising.on_read = None
    raising.on_write = make
    with pytest.raises(store.StoreError) as written:
        creds.save("gmail", "x", "correct-horse-battery-staple")
    assert str(written.value) == f"the keyring could not be written ({detail})"
    _check(written.value, unusable)


def _chain(*errors: BaseException) -> Exception:
    """The first error, each raised from the next, as ``raise ... from`` links them."""
    for upper, lower in zip(errors, errors[1:], strict=False):
        upper.__cause__ = lower
    top = errors[0]
    assert isinstance(top, Exception)
    return top


EXEC_FAILED = "org.freedesktop.DBus.Error.Spawn.ExecFailed"
SERVICE_UNKNOWN = "org.freedesktop.DBus.Error.ServiceUnknown"
NO_REPLY = "org.freedesktop.DBus.Error.NoReply"


def _chained() -> list[Any]:
    """Errors keyring raises wrapping the Secret Service's (its ``InitError``, from the collection
    lookup), or others raised from them; what the message says; and whether it is unusable."""
    errors = secretstorage_exceptions
    unavailable = errors.SecretServiceNotAvailableException
    cases = [
        # A service whose Exec names a program that does not exist: keyring's InitError, from
        # secretstorage's error, from the bus's reply (reproduced on dbus-daemon 1.14).
        (
            lambda: _chain(InitError("x"), unavailable("y"), _reply(EXEC_FAILED)),
            f"InitError, from DBusErrorResponse: {EXEC_FAILED}",
            True,
        ),
        # The same wrapping, exactly, for the bus's other activation failures: secretstorage maps
        # each to its SecretServiceNotAvailableException, from the reply (secretstorage/util.py).
        (
            lambda: _chain(InitError("x"), unavailable("y"), _reply(SERVICE_UNKNOWN)),
            f"InitError, from DBusErrorResponse: {SERVICE_UNKNOWN}",
            True,
        ),
        (
            lambda: _chain(InitError("x"), unavailable("y"), _reply(NO_REPLY)),
            f"InitError, from DBusErrorResponse: {NO_REPLY}",
            True,
        ),
        # Or with secretstorage's error left out.
        (
            lambda: _chain(InitError("x"), _reply(SERVICE_UNKNOWN)),
            f"InitError, from DBusErrorResponse: {SERVICE_UNKNOWN}",
            True,
        ),
        (
            lambda: _chain(InitError("x"), _reply(NO_REPLY)),
            f"InitError, from DBusErrorResponse: {NO_REPLY}",
            True,
        ),
        (
            lambda: _chain(InitError("x"), unavailable("[Errno 111] Connection refused")),
            "InitError, from SecretServiceNotAvailableException",
            True,
        ),
        (
            lambda: _chain(unavailable("y"), _reply(EXEC_FAILED)),
            f"SecretServiceNotAvailableException, from DBusErrorResponse: {EXEC_FAILED}",
            True,
        ),
        # A service that runs, and says no, under keyring's error: not one to fall back from.
        (
            lambda: _chain(InitError("x"), errors.LockedException("Collection is locked!")),
            "InitError, from LockedException",
            False,
        ),
        (
            lambda: _chain(InitError("x"), errors.PromptDismissedException("Prompt dismissed.")),
            "InitError, from PromptDismissedException",
            False,
        ),
        (
            lambda: _chain(InitError("x"), _reply(ACCESS_DENIED)),
            f"InitError, from DBusErrorResponse: {ACCESS_DENIED}",
            False,
        ),
        (lambda: InitError("x"), "InitError", False),
        (lambda: _chain(InitError("x"), ValueError("not the service's")), "InitError", False),
        # The walk stops at an error that is none of the keyring's, an OSError or the service's,
        # and does not look past it.
        (
            lambda: _chain(
                InitError("x"),
                ValueError("not the service's"),
                unavailable("y"),
                _reply(EXEC_FAILED),
            ),
            "InitError",
            False,
        ),
    ]
    ids = [
        "exec-failed",
        "service-unknown",
        "no-reply",
        "service-unknown-bare",
        "no-reply-bare",
        "connection-refused",
        "unwrapped",
        "locked",
        "prompt-dismissed",
        "access-denied",
        "alone",
        "other-cause",
        "stops-at-other",
    ]
    return [pytest.param(*case, id=name) for case, name in zip(cases, ids, strict=True)]


@pytest.mark.parametrize(("make", "detail", "unusable"), _chained())
def test_an_error_is_classified_by_its_cause_chain(
    raising: Raising,
    host_env: HostEnv,
    tmp_path: Path,
    make: Callable[[], Exception],
    detail: str,
    unusable: bool,
) -> None:
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    raising.on_read = make
    for read in (item.read, creds.names):
        with pytest.raises(store.StoreError) as raised:
            read()
        assert str(raised.value) == f"the keyring could not be read ({detail})"
        _check(raised.value, unusable)
        assert BODY not in str(raised.value) and "Errno" not in str(raised.value)
    raising.on_read = None
    raising.on_write = make
    with pytest.raises(store.StoreError) as raised:
        item.change(_edit)
    assert str(raised.value) == f"the keyring could not be written ({detail})"
    _check(raised.value, unusable)


def test_a_chain_is_followed_through_context_too(raising: Raising, host_env: HostEnv) -> None:
    # An OSError raised while handling the bus's reply: linked by __context__ only.
    def make() -> Exception:
        try:
            raise _reply(EXEC_FAILED)
        except Exception:
            try:
                raise OSError(5, "I/O error")  # noqa: TRY301 - the chain under test
            except OSError as exc:
                return exc

    raising.on_read = make
    with pytest.raises(store.KeyringUnusable) as raised:
        store.Item(host_env.dirs.state, host_env.dirs.profile).read()
    assert str(raised.value) == (
        f"the keyring could not be read (OSError, from DBusErrorResponse: {EXEC_FAILED})"
    )


def test_a_chain_is_followed_only_so_far_and_each_link_once(
    raising: Raising, host_env: HostEnv
) -> None:
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    # A reply further down than CHAIN_LIMIT links is not looked at.
    deep = [InitError(f"wrap {n}") for n in range(store.CHAIN_LIMIT)]
    raising.on_read = lambda: _chain(*deep, _reply(EXEC_FAILED))
    with pytest.raises(store.StoreError) as raised:
        item.read()
    assert str(raised.value) == "the keyring could not be read (InitError)"
    _check(raised.value, unusable=False)
    # A chain that loops ends, each link taken once, before the limit.
    first, second = InitError("a"), InitError("b")
    first.__cause__, second.__cause__ = second, first
    assert store._links(first) == [first, second]
    raising.on_read = lambda: first
    with pytest.raises(store.StoreError) as raised:
        item.read()
    assert str(raised.value) == "the keyring could not be read (InitError)"


@pytest.mark.parametrize(
    "name",
    [None, 7, "", "Spawn", "org freedesktop", "org.freedesktop.\n", "a." + "b" * 300],
    ids=["none", "not-text", "empty", "one-element", "space", "newline", "too-long"],
)
def test_an_error_reply_s_name_is_given_only_when_it_is_a_d_bus_error_name(
    raising: Raising, host_env: HostEnv, name: object
) -> None:
    raising.on_read = lambda: _reply(name)
    with pytest.raises(store.StoreError) as read:
        store.Item(host_env.dirs.state, host_env.dirs.profile).read()
    assert str(read.value) == "the keyring could not be read (DBusErrorResponse)"
    # With no name it can be read by, it does not say the service could not be started.
    _check(read.value, unusable=False)


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
    # No write of the pair item tried yet in this process, whatever earlier tests wrote.
    monkeypatch.setattr(store, "_PAIR_WRITE_TRIED", threading.Event())
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


FALLBACK = (
    f"lucille-host: warning: the keyring could not be read (DBusErrorResponse: {SPAWN_FAILED}); "
    "as --store-file asks, the pair is kept in a file instead\n"
)


@pytest.mark.parametrize(
    ("as_json", "make", "detail"),
    [
        (False, lambda: _reply(SPAWN_FAILED), f"DBusErrorResponse: {SPAWN_FAILED}"),
        (True, lambda: _reply(SPAWN_FAILED), f"DBusErrorResponse: {SPAWN_FAILED}"),
        (
            True,
            lambda: _chain(
                InitError("x"),
                secretstorage_exceptions.SecretServiceNotAvailableException("y"),
                _reply(EXEC_FAILED),
            ),
            f"InitError, from DBusErrorResponse: {EXEC_FAILED}",
        ),
    ],
    ids=["text", "json", "wrapped"],
)
def test_pair_store_file_falls_back_when_the_service_fails_the_first_read(
    cli: HostCli,
    raising: Raising,
    as_json: bool,
    make: Callable[[], Exception],
    detail: str,
) -> None:
    # The keyring is accepted at open, and its service cannot be started at the first read, said
    # directly or by keyring's own error raised from it: with --store-file, the pair is kept in the
    # file store, and the command says so, and why, with nothing of what the bus said beside the
    # error's name.
    raising.on_read = make
    lucille = cli.lucille()
    ran = cli.paired(lucille, "--store-file", *(["--json"] if as_json else []))
    assert ran.err.startswith(
        f"lucille-host: warning: the keyring could not be read ({detail}); "
        "as --store-file asks, the pair is kept in a file instead\n"
    ), ran.text
    assert BODY not in ran.text
    assert "weaker than the OS keyring" in ran.err
    assert cli.run("pair", "confirm").rc == 0
    assert cli.held() == [lucille.name]
    # Nothing was written to the keyring.
    assert raising.writes == 0 and raising.items == {}


def test_pair_without_store_file_does_not_fall_back(cli: HostCli, raising: Raising) -> None:
    raising.on_read = lambda: _reply(SPAWN_FAILED)
    lucille = cli.lucille()
    ran = cli.run("--json", "pair", "--code-stdin", stdin=lucille.start() + "\n")
    assert ran.rc == 1, ran.text
    assert _support.cli_objects(ran.out) == [
        {
            "v": 1,
            "type": "error",
            "code": "store_error",
            "message": f"the keyring could not be read (DBusErrorResponse: {SPAWN_FAILED})",
        }
    ]
    assert "kept in a file" not in ran.text and not cli.dirs.store_files.exists()


def _not_answered() -> Exception:
    return store.Unanswered("the keyring did not answer a read within 60 s")


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (_not_answered, "the keyring did not answer a read within 60 s"),
        (lambda: OSError(5, "I/O error"), "the keyring could not be read (OSError)"),
        (
            lambda: secretstorage_exceptions.PromptDismissedException("Prompt dismissed."),
            "the keyring could not be read (PromptDismissedException)",
        ),
        (
            lambda: secretstorage_exceptions.LockedException("Collection is locked!"),
            "the keyring could not be read (LockedException)",
        ),
        (
            lambda: secretstorage_exceptions.ItemNotFoundException("No collections found."),
            "the keyring could not be read (ItemNotFoundException)",
        ),
        (
            lambda: _reply(ACCESS_DENIED),
            f"the keyring could not be read (DBusErrorResponse: {ACCESS_DENIED})",
        ),
    ],
    ids=["unanswered", "os-error", "prompt-dismissed", "locked", "item-not-found", "access-denied"],
)
def test_pair_store_file_does_not_fall_back_on_any_other_store_error(
    cli: HostCli, raising: Raising, make: Callable[[], Exception], message: str
) -> None:
    # A read that did not answer may be followed by a write of it that lands; a service that runs
    # and says no (the user dismissed its unlock prompt, say) is not one to fall back from; any
    # other store error is not the service failing. None moves the pair to the file store.
    raising.on_read = make
    lucille = cli.lucille()
    ran = cli.run("--json", "pair", "--code-stdin", "--store-file", stdin=lucille.start() + "\n")
    assert ran.rc == 1, ran.text
    [error] = _support.cli_objects(ran.out)
    assert (error["code"], error["message"]) == ("store_error", message)
    assert "kept in a file" not in ran.text and not cli.dirs.store_files.exists()


def test_a_write_of_the_pair_item_tried_is_remembered_whether_or_not_it_landed(
    raising: Raising, host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "_PAIR_WRITE_TRIED", threading.Event())
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    item.read()
    assert not store.pair_write_tried()
    raising.on_write = lambda: _reply(SPAWN_FAILED)
    with pytest.raises(store.KeyringUnusable):
        item.change(_edit)
    assert store.pair_write_tried()
    # The credential item is another item: its writes are not the pair's.
    monkeypatch.setattr(store, "_PAIR_WRITE_TRIED", threading.Event())
    raising.on_write = None
    credentials.CredentialStore(host_env.dirs.state, "default").save("gmail", "x", "a-secret")
    assert not store.pair_write_tried()


def test_pair_store_file_never_falls_back_once_a_write_of_the_pair_item_was_tried(
    cli: HostCli, raising: Raising
) -> None:
    # A write tried in this process may have landed: falling back could leave one pair in the
    # keyring and another in the file.
    store._PAIR_WRITE_TRIED.set()
    raising.on_read = lambda: _reply(SPAWN_FAILED)
    lucille = cli.lucille()
    ran = cli.run("--json", "pair", "--code-stdin", "--store-file", stdin=lucille.start() + "\n")
    assert ran.rc == 1, ran.text
    [error] = _support.cli_objects(ran.out)
    assert error["code"] == "store_error"
    assert "kept in a file" not in ran.text and not cli.dirs.store_files.exists()
