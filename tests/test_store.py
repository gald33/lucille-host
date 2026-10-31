"""The host's stores: the keyring item, its locks and its write throttle, and the file store.

The pairing and sharing calls here are the library's own, made the way the host makes them: with
``base=None``, the host's two stores and its own ``connect``, against Switchboard's in-process
test hub, with a fake Lucille on the other side.
"""

from __future__ import annotations

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
from tests._keyring import MemoryKeyring
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


# keyrings.alt's backend that writes secrets unencrypted to a file of its own, as it is named
# there (keyrings.alt 5.0.2, ``keyrings/alt/file.py``), where keyring would select it when
# installed; it is not installed here.
Plaintext = type(
    "PlaintextKeyring", (MemoryKeyring,), {"__module__": "keyrings.alt.file", "priority": 0.5}
)


def _chain_with_plaintext() -> ChainerBackend:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("keyring.backend.get_all_keyring", lambda: [MemoryKeyring(), Plaintext()])
        return ChainerBackend()


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(FailKeyring, id="fail"),
        pytest.param(NullKeyring, id="null"),
        pytest.param(ChainerBackend, id="empty-chain"),
        pytest.param(Plaintext, id="plaintext"),
        pytest.param(_chain_with_plaintext, id="chain-with-plaintext"),
    ],
)
def test_refuses_insecure_keyring_backend(
    backend: Callable[[], Any], host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A chain of the backends keyring finds viable: none here, so it chains nothing.
    monkeypatch.setattr("keyring.backend.get_all_keyring", lambda: [NullKeyring()])
    monkeypatch.setattr("keyring.core._keyring_backend", backend())
    with pytest.raises(store.InsecureKeyring, match="cannot keep its pair"):
        store.check_backend()
    with pytest.raises(store.InsecureKeyring):
        _switchboard.open_link(host_env.dirs, KEYRING)
    # The file store does not use the keyring, so it opens where the keyring stores nothing.
    assert _switchboard.open_link(host_env.dirs, FILE).warning is not None


def test_a_keyring_that_stores_is_accepted(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.check_backend()
    assert _switchboard.open_link(host_env.dirs, KEYRING).warning is None
    monkeypatch.setattr("keyring.backend.get_all_keyring", lambda: [MemoryKeyring()])
    store.check_backend(ChainerBackend())


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
