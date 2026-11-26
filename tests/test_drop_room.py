"""A Connect request's drop room on Switchboard's in-process test hub (decisions/0002 §11.4).

The host posts its hello and reads the room through ``_switchboard``; Lucille's side opens the
same room with the library's public API, as "The wire" says both ends do.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from switchboard import client, pairing
from switchboard.config import ClientConfig
from switchboard.testing import hub

from lucille_host import _switchboard, connect, state
from tests._cli import HostCli
from tests._host import HostEnv
from tests._lucille import UNPARSABLE, unparsable_bodies

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

KEY = bytes(range(32))


@pytest.fixture
def h() -> Iterator[Any]:
    with hub() as handle:
        yield handle


@pytest.fixture
def cli(
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[HostCli]:
    host = HostCli(h, host_env, tmp_path, capsys, monkeypatch)
    yield host
    host.stopping.set()


def _link(cli: HostCli) -> _switchboard.Link:
    return _switchboard.open_link(
        cli.dirs, state.load_host_config(cli.dirs), client_class=cli.client_class
    )


def _lucille_client(h: Any, key: bytes = KEY) -> Any:
    """Lucille's client of the drop room, built as "The wire" says."""
    room = pairing.handshake_room(key)
    config = ClientConfig(
        url=_switchboard.LUCILLE_HUB,
        workspace=room.workspace,
        key=room.key,
        write_key=room.write_key,
        peer_log="",
        stash_db="",
    )
    return h.client_class()(config, agent_id=pairing.throwaway_agent("lucille"))


def test_the_drop_room_is_the_handshake_room_of_k() -> None:
    drop = _switchboard.drop_room(KEY)
    room = pairing.handshake_room(KEY)
    assert drop.url == _switchboard.LUCILLE_HUB
    assert drop.workspace == room.workspace
    assert drop.config.key == room.key
    assert drop.config.write_key == room.write_key
    assert drop.config.token is None
    assert drop.config.peer_log == ""
    assert drop.config.stash_db == ""
    assert room.key not in repr(drop)
    with pytest.raises(ValueError):
        _switchboard.drop_room(KEY[:31])


def test_another_k_is_another_room() -> None:
    assert _switchboard.drop_room(KEY).workspace != _switchboard.drop_room(bytes(32)).workspace


def test_the_hello_posted_by_the_host_is_read_by_lucille(cli: HostCli, h: Any) -> None:
    with _link(cli).drop_client(_switchboard.drop_room(KEY)) as drop:
        posted = drop.post(connect.CHANNEL, connect.HELLO, ttl=connect.POST_TTL)
        assert posted.created_at is not None
        rows = drop.read(connect.CHANNEL, limit=connect.READ_LIMIT)
    assert [row["body"] for row in rows] == [connect.HELLO]
    seen = _lucille_client(h).history(connect.CHANNEL, limit=connect.READ_LIMIT)
    assert [row["body"] for row in seen] == [connect.HELLO]


def test_a_post_by_lucille_is_read_by_the_host(cli: HostCli, h: Any) -> None:
    _lucille_client(h).post(connect.CHANNEL, "a delivery", ttl=connect.POST_TTL)
    with _link(cli).drop_client(_switchboard.drop_room(KEY)) as drop:
        rows = drop.read(connect.CHANNEL, limit=connect.READ_LIMIT)
    assert [row["body"] for row in rows] == ["a delivery"]


def test_each_drop_client_posts_under_its_own_throwaway_id(
    cli: HostCli, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[str] = []
    real = pairing.throwaway_agent

    def recorded(prefix: str = "pair") -> str:
        made.append(prefix)
        return real(prefix)

    monkeypatch.setattr("switchboard.pairing.throwaway_agent", recorded)
    link = _link(cli)
    for _ in range(2):
        with link.drop_client(_switchboard.drop_room(KEY)) as drop:
            drop.post(connect.CHANNEL, connect.HELLO, ttl=connect.POST_TTL)
    assert made == ["connect", "connect"]
    # The hub blinds the sender in a sealed room; two clients still post as two senders.
    seen = _lucille_client(h).history(connect.CHANNEL, limit=connect.READ_LIMIT)
    assert len({row["from"] for row in seen}) == 2


def test_a_post_that_opens_but_is_not_json_is_interference(cli: HostCli, h: Any) -> None:
    with unparsable_bodies():
        # The poster's own read of what the hub stored cannot parse it either.
        with pytest.raises(ValueError):
            _lucille_client(h).post(connect.CHANNEL, UNPARSABLE, ttl=connect.POST_TTL)
    with _link(cli).drop_client(_switchboard.drop_room(KEY)) as drop:
        with pytest.raises(_switchboard.Interfered):
            drop.read(connect.CHANNEL, limit=connect.READ_LIMIT)


def test_a_hub_that_cannot_be_reached_is_a_hub_error(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("down")

    with _link(cli).drop_client(_switchboard.drop_room(KEY)) as drop:
        monkeypatch.setattr(drop._client, "history", unreachable)
        monkeypatch.setattr(drop._client, "post", unreachable)
        with pytest.raises(_switchboard.HubError):
            drop.read(connect.CHANNEL, limit=connect.READ_LIMIT)
        with pytest.raises(_switchboard.HubError):
            drop.post(connect.CHANNEL, connect.HELLO, ttl=connect.POST_TTL)


def test_a_hub_that_answers_with_an_error_is_a_hub_error(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*args: Any, **kwargs: Any) -> Any:
        raise client.SwitchboardError("nope", status=503)

    with _link(cli).drop_client(_switchboard.drop_room(KEY)) as drop:
        monkeypatch.setattr(drop._client, "history", refused)
        with pytest.raises(_switchboard.HubError) as caught:
            drop.read(connect.CHANNEL, limit=connect.READ_LIMIT)
    assert caught.value.status == 503


@pytest.mark.parametrize("raised", [ValueError("x"), KeyError("x"), RuntimeError("x")])
def test_any_other_failure_of_a_read_is_interference(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch, raised: Exception
) -> None:
    def failing(*args: Any, **kwargs: Any) -> Any:
        raise raised

    with _link(cli).drop_client(_switchboard.drop_room(KEY)) as drop:
        monkeypatch.setattr(drop._client, "history", failing)
        with pytest.raises(_switchboard.Interfered) as caught:
            drop.read(connect.CHANNEL, limit=connect.READ_LIMIT)
    assert caught.value.kind == type(raised).__name__


def test_a_read_that_returns_no_list_is_interference(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _link(cli).drop_client(_switchboard.drop_room(KEY)) as drop:
        monkeypatch.setattr(drop._client, "history", lambda *a, **k: {"messages": []})
        with pytest.raises(_switchboard.Interfered):
            drop.read(connect.CHANNEL, limit=connect.READ_LIMIT)
