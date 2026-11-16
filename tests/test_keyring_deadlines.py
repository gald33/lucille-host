"""The deadlines on keyring calls (issue 39): a read or write that does not answer is a store error,
and the process asks the keyring nothing more; a check the bus never answers is no keyring.

tests/test_keyring_silent_bus.py runs the real check against a socket that never answers, on Linux.
These run everywhere: they put this system among the bounded ones and shorten the deadline, and a
backend in memory that does not answer until the test lets it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lucille_host import credentials, paths, store
from tests._host import HostEnv
from tests._keyring import MemoryKeyring

# A deadline short enough for a test, and the loose bound a refusal must come within.
DEADLINE = 0.2
WITHIN = 10.0
SECRET = "correct-horse-battery-staple"


class Unanswering(MemoryKeyring):
    """A keyring in memory whose reads wait until ``read_gate`` is set, and writes until
    ``write_gate`` is; ``answer`` sets both."""

    def __init__(self) -> None:
        super().__init__()
        self.read_gate = threading.Event()
        self.write_gate = threading.Event()
        self.calls = 0

    @property
    def answer(self) -> _Both:
        return _Both(self.read_gate, self.write_gate)

    def get_password(self, service: str, username: str) -> str | None:
        self.calls += 1
        self.read_gate.wait()
        return super().get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls += 1
        self.write_gate.wait()
        super().set_password(service, username, password)


class _Both:
    def __init__(self, *events: threading.Event) -> None:
        self.events = events

    def set(self) -> None:
        for event in self.events:
            event.set()


@pytest.fixture(autouse=True)
def _bounded_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """This system's keyring calls bounded by a short deadline, with no call yet unanswered."""
    monkeypatch.setattr(store, "BOUNDED", frozenset({paths.os_family()}))
    monkeypatch.setattr(store, "CALL_TIMEOUT", DEADLINE)
    monkeypatch.setattr(store, "_UNANSWERED", threading.Event())


@pytest.fixture
def unanswering(host_env: HostEnv, monkeypatch: pytest.MonkeyPatch) -> Iterator[Unanswering]:
    """The keyring in use, in place of host_env's, which it comes after."""
    backend = Unanswering()
    monkeypatch.setattr("keyring.core._keyring_backend", backend)
    try:
        yield backend
    finally:
        backend.answer.set()


def test_a_read_that_does_not_answer_is_a_store_error(
    unanswering: Unanswering, host_env: HostEnv
) -> None:
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    started = time.monotonic()
    with pytest.raises(store.Unanswered, match=r"did not answer a read within 0.2 s$"):
        item.read()
    assert time.monotonic() - started < WITHIN
    # The process asks the keyring nothing more: the next read fails at once, unasked.
    with pytest.raises(store.StoreError, match="not asked again"):
        item.read()
    assert unanswering.calls == 1


def test_a_write_that_does_not_answer_is_a_store_error(
    unanswering: Unanswering, host_env: HostEnv
) -> None:
    # Reads answer; writes do not.
    unanswering.read_gate.set()
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)
    assert item.read()["pairs"]["records"] == {}

    def edit(doc: dict[str, object]) -> None:
        doc["shared"] = {"seq": 1}

    with pytest.raises(store.Unanswered, match="did not answer a write within 0.2 s") as info:
        item.change(edit)
    assert "whether it was saved is not known" in str(info.value)
    calls = unanswering.calls
    # Nothing more is sent, a write that could land after a newer one least of all.
    with pytest.raises(store.StoreError, match="not asked again"):
        item.change(edit)
    with pytest.raises(store.StoreError, match="not asked again"):
        item.read()
    assert unanswering.calls == calls


def test_a_write_that_lands_after_its_deadline_is_read_whole_by_the_next_process(
    unanswering: Unanswering, host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The thread left waiting on a write can finish it after the deadline, before the process
    # exits. The next process reads the item afresh: the whole document the late write saved.
    unanswering.read_gate.set()
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)

    def edit(doc: dict[str, object]) -> None:
        doc["shared"] = {"seq": 1}

    with pytest.raises(store.Unanswered, match="whether it was saved is not known"):
        item.change(edit)
    writes = unanswering.writes
    unanswering.write_gate.set()
    deadline = time.monotonic() + WITHIN
    while unanswering.writes == writes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert unanswering.writes == writes + 1
    # A new process, which asks the keyring again.
    monkeypatch.setattr(store, "_UNANSWERED", threading.Event())
    doc = store.Item(host_env.dirs.state, host_env.dirs.profile).read()
    assert doc["shared"] == {"seq": 1}
    assert doc["pairs"]["records"] == {}


def test_a_credential_read_or_write_that_does_not_answer_is_a_store_error(
    unanswering: Unanswering, tmp_path: Path
) -> None:
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    with pytest.raises(store.StoreError, match="did not answer a read") as info:
        creds.save("gmail", "x", SECRET)
    assert SECRET not in str(info.value)
    with pytest.raises(store.StoreError, match="not asked again"):
        creds.names()


def test_a_call_that_answers_in_time_is_unchanged(
    unanswering: Unanswering, host_env: HostEnv
) -> None:
    unanswering.answer.set()
    item = store.Item(host_env.dirs.state, host_env.dirs.profile)

    def edit(doc: dict[str, object]) -> None:
        doc["shared"] = {"seq": 1}

    item.change(edit)
    assert item.read()["shared"] == {"seq": 1}


def test_what_a_call_raises_is_raised_as_before(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing(MemoryKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise OSError("the session bus went away")

    monkeypatch.setattr("keyring.core._keyring_backend", Failing())
    with pytest.raises(store.StoreError, match=r"could not be read \(OSError\)"):
        store.Item(host_env.dirs.state, host_env.dirs.profile).read()
    assert not store._UNANSWERED.is_set()


def test_on_a_system_whose_calls_are_not_bounded_a_slow_call_is_waited_for(
    unanswering: Unanswering, host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The macOS Keychain may wait on its own prompt, and is not bounded.
    monkeypatch.setattr(store, "BOUNDED", frozenset())
    threading.Timer(DEADLINE * 3, unanswering.answer.set).start()
    assert store.Item(host_env.dirs.state, host_env.dirs.profile).read()["shared"] == {}


@pytest.mark.parametrize(
    ("refusal", "end"),
    [(store.PAIR_REFUSAL, "--store-file"), (credentials.REFUSAL, "set up the system keyring")],
)
def test_a_bus_that_did_not_answer_is_refused_with_its_own_sentence(refusal: str, end: str) -> None:
    with pytest.raises(store.InsecureKeyring) as refused:
        store.check_backend(store.NoAnswer(), refusal=refusal)
    message = str(refused.value)
    assert message == (
        "the keyring backend here (NoAnswer) has no keyring to store in: the session bus did not "
        f"answer within {store.PROBE_TIMEOUT:g} s, {refusal}"
    )
    assert end in message
