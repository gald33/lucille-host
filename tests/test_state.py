"""Before the host starts: its refusals, its private runtime folder, how it builds a Switchboard
client, and its own configuration file."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from switchboard import pairing, sharing
from switchboard.signing import SigningIdentity
from switchboard.testing import hub

from lucille_host import _switchboard, paths, state
from tests._host import HostEnv
from tests._lucille import FakeLucille

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)


@pytest.fixture
def h() -> Iterator[Any]:
    with hub() as handle:
        yield handle


def test_refuses_uid_0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    with pytest.raises(state.Refused, match="root"):
        state.startup_checks({})
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    state.startup_checks({})


def test_switchboard_env_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    for name in list(os.environ):
        if name.startswith("SWITCHBOARD_"):
            monkeypatch.delenv(name)
    state.startup_checks()
    monkeypatch.setenv("SWITCHBOARD_URL", "https://hub.example/value-not-shown")
    with pytest.raises(state.Refused) as refused:
        state.startup_checks()
    assert "SWITCHBOARD_URL is set" in str(refused.value)
    assert "value-not-shown" not in str(refused.value)
    environ = {"PATH": "/usr/bin", "SWITCHBOARD_KEY": "k", "SWITCHBOARD_PAIR_X": "p"}
    with pytest.raises(state.Refused, match="SWITCHBOARD_KEY, SWITCHBOARD_PAIR_X are set"):
        state.startup_checks(environ)
    state.startup_checks({"PATH": "/usr/bin", "XSWITCHBOARD_URL": "x"})


class _Recorder:
    """A client class that records every client built, on the test hub's transport."""

    def __init__(self, h: Any) -> None:
        self.built: list[Any] = []
        recorder = self

        class Recorded(h.client_class()):  # type: ignore[misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                recorder.built.append(self)

        self.client_class = Recorded


def test_client_refused_without_private_runtime_dir(host_env: HostEnv, h: Any) -> None:
    recorder = _Recorder(h)
    dirs = host_env.dirs
    link = _switchboard.Link(None, None, runtime=dirs.runtime, client_class=recorder.client_class)
    config = h.client_config(agent_id="lucille-host")
    with pytest.raises(state.Unprepared):
        link.connect(config, agent_id="lucille-host")
    assert recorder.built == []

    # A pairing call builds its clients through the same hook, so it is refused too, before it
    # saves anything.
    linked = _switchboard.open_link(dirs, state.HostConfig(), client_class=recorder.client_class)
    os.environ["XDG_RUNTIME_DIR"] = str(host_env.home)
    lucille = FakeLucille(h, host_env.home.parent / "lucille")
    calls = {"store": linked.pairs, "connect": linked.connect, "now": h.now}
    with pytest.raises(state.Unprepared):
        pairing.accept(lucille.start(), None, **calls)
    assert recorder.built == [] and linked.pairs.names() == []

    # open_link put it in place: a private folder under the state folder, mode 0700.
    os.environ["XDG_RUNTIME_DIR"] = str(dirs.runtime)
    assert dirs.runtime.parent == dirs.state
    assert stat.S_IMODE(dirs.runtime.stat().st_mode) == 0o700
    pairing.accept(lucille.start(), None, **calls)
    assert recorder.built and linked.pairs.names() != []

    # It is checked again for each client: a folder that is no longer private is refused.
    built = len(recorder.built)
    os.chmod(dirs.runtime, 0o755)
    with pytest.raises(state.Unprepared, match="not a private folder"):
        link.connect(config, agent_id="lucille-host")
    os.chmod(dirs.runtime, 0o700)
    os.rename(dirs.runtime, dirs.state / "elsewhere")
    os.symlink(dirs.state / "elsewhere", dirs.runtime)
    with pytest.raises(state.Unprepared, match="not a private folder"):
        link.connect(config, agent_id="lucille-host")
    assert len(recorder.built) == built


def test_client_refused_a_configuration_that_keeps_files(host_env: HostEnv, h: Any) -> None:
    link = _switchboard.open_link(host_env.dirs, state.HostConfig(), client_class=h.client_class())
    config = h.client_config(agent_id="x")
    for kept in ({"peer_log": "peers.db"}, {"stash_db": "stash.db"}):
        with pytest.raises(ValueError, match="peer log or stash"):
            link.connect(replace(config, **kept), agent_id="x")
    with pytest.raises(ValueError, match="own configuration"):
        link.connect(None, agent_id="x")
    assert not (host_env.home / ".switchboard").exists()


def test_every_client_gets_a_fresh_signing_identity(
    host_env: HostEnv, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As if a signer were listening where the library looks for one: every client built, the
    # fake Lucille's included, would sign through it.
    planted = SigningIdentity.generate()
    monkeypatch.setattr("switchboard.signing.attach", lambda agent_id: planted)
    recorder = _Recorder(h)
    link = _switchboard.open_link(
        host_env.dirs, state.HostConfig(), client_class=recorder.client_class
    )
    lucille = FakeLucille(h, host_env.home.parent / "lucille")
    calls = {"store": link.pairs, "connect": link.connect, "now": h.now}
    name = pairing.accept(lucille.start(), None, **calls).name
    lucille.collect()
    resolution = pairing.resolve(name, None, **calls)
    lucille.offer("work-1")
    rooms = {**calls, "store": link.rooms, "pair_store": link.pairs}
    sharing.touch(None, pairs=[name], **rooms)
    sharing.accept(f"{name}/work-1", None, **rooms)
    room = sharing.resolve_room("work-1", None, store=link.rooms, now=h.now)
    for config in (resolution.config, room.config):
        with link.connect(config, agent_id="lucille-host"):
            pass
    assert len(recorder.built) > 2
    for built in recorder.built:
        assert isinstance(built.signing, SigningIdentity) and built.signing is not planted
    keys = [built.signing.public_key for built in recorder.built]
    assert len(set(keys)) == len(keys)


def test_host_config_round_trips_and_fails_closed(host_env: HostEnv) -> None:
    dirs = host_env.dirs
    assert state.load_host_config(dirs) == state.HostConfig(hub=None, store="keyring")
    config = state.HostConfig(hub="http://127.0.0.1:8765", store="file")
    state.save_host_config(dirs, config)
    assert state.load_host_config(dirs) == config
    assert stat.S_IMODE(dirs.host_file.stat().st_mode) == 0o600
    for text in (
        "not json",
        "[]",
        '{"v": 1, "hub": null}',
        '{"v": 2, "hub": null, "store": "keyring"}',
        '{"v": 1, "hub": null, "store": "keyring", "more": 1}',
        '{"v": 1, "hub": "https://elsewhere.example", "store": "keyring"}',
        '{"v": 1, "hub": null, "store": "cloud"}',
    ):
        dirs.host_file.write_text(text, encoding="utf-8")
        with pytest.raises(state.StateError):
            state.load_host_config(dirs)


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("http://127.0.0.1:1", True),
        ("http://localhost:65535", True),
        ("http://127.0.0.1:65536", False),
        ("http://127.0.0.1:0", False),
        ("http://127.0.0.1:080", False),
        ("http://127.0.0.1", False),
        ("http://127.0.0.1:80/", False),
        ("http://127.0.0.2:80", False),
        ("https://127.0.0.1:80", False),
        ("http://LOCALHOST:80", False),
        ("http://localhost:80 ", False),
        ("http://user@localhost:80", False),
        ("http://127.0.0.1:８０", False),
    ],
)
def test_only_a_loopback_hub_can_be_configured(url: str, valid: bool) -> None:
    assert state.valid_loopback_hub(url) is valid
    if valid:
        assert state.HostConfig(hub=url).hub == url
    else:
        with pytest.raises(ValueError):
            state.HostConfig(hub=url)


def test_the_runtime_folder_is_the_profiles_own(host_env: HostEnv) -> None:
    other = paths.dirs("other")
    assert host_env.dirs.runtime != other.runtime
    assert os.environ.get("XDG_RUNTIME_DIR") is None
    state.prepare_runtime(other.runtime)
    assert os.environ["XDG_RUNTIME_DIR"] == str(other.runtime)
    with pytest.raises(state.Unprepared):
        state.check_runtime(host_env.dirs.runtime)
    state.check_runtime(other.runtime)


def test_write_json_replaces_the_file_whole(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    state.write_json(path, {"a": 1})
    state.write_json(path, {"b": 2})
    assert state.read_json(path) == {"b": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.json"]
    assert state.read_json(tmp_path / "missing.json") is None
