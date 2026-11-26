"""``pair begin``, ``pair resume``, ``pair cancel``, and ``pair status`` with a Connect request
(decisions/0002 §11.4; spec/CLI.md §7), in-process against Switchboard's test hub, with a fake
Lucille that signs with a throwaway key pinned in place of Lucille's.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from switchboard.testing import hub

from lucille_host import _switchboard, connect, connecting, gate, loop, state
from lucille_host.cli import PAIR_LOCK
from tests._cli import HostCli, Ran, collecting, until
from tests._host import HostEnv, lock_held
from tests._lucille import FakeLucille, Signer
from tests.spec import _support

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

STDIN = "telegram\nada\n"


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
    monkeypatch.setattr("lucille_host.connecting.POLL_EVERY", 0.05)
    host = HostCli(h, host_env, tmp_path, capsys, monkeypatch)
    poll = connecting.poll

    def stoppable(*args: Any, **kwargs: Any) -> Any:
        # A wait the test stopped ends as Ctrl-C ends it.
        if host.stopping.is_set():
            raise KeyboardInterrupt
        return poll(*args, **kwargs)

    monkeypatch.setattr("lucille_host.connecting.poll", stoppable)
    yield host
    host.stopping.set()


@pytest.fixture
def signer(monkeypatch: pytest.MonkeyPatch) -> Signer:
    made = Signer()
    monkeypatch.setattr("lucille_host._switchboard.LUCILLE_SIGNING_KEYS", made.keys)
    return made


def _objects(ran: Ran) -> list[dict[str, Any]]:
    return _support.cli_objects(ran.out)


def _request(cli: HostCli) -> connecting.Request:
    found = connecting.load(cli.dirs.connect_file)
    assert found is not None
    return found


def _waiting(cli: HostCli) -> connecting.Request:
    until(lambda: cli.dirs.connect_file.exists())
    return _request(cli)


def _key(request: connecting.Request) -> bytes:
    return connect.stretch(request.code, request.epoch)


def _begun(cli: HostCli, username: str = "ada") -> connecting.Live:
    """A request ``pair begin`` would have kept, made here without the command."""
    link = _switchboard.open_link(
        cli.dirs, state.load_host_config(cli.dirs), client_class=cli.client_class
    )
    return connecting.begin(
        link,
        cli.dirs.connect_file,
        service="telegram",
        username=username,
        hub=_switchboard.LUCILLE_HUB,
        now=lambda: float(cli.h.now),
    )


# --- pair begin -----------------------------------------------------------------------------


def test_begin_connects_through_lucilles_delivery(cli: HostCli, signer: Signer) -> None:
    running = cli.start("pair", "begin", "--json", stdin=STDIN)
    request = _waiting(cli)
    lucille = cli.lucille()
    lucille.deliver(connect.shown(request.code), signer)
    with collecting(lucille):
        ran = running.wait()
    assert ran.rc == 0, ran.text
    found = _objects(ran)
    assert found[0] == {
        "v": 1,
        "type": "connect",
        "state": "code",
        "code": connect.shown(request.code),
        "expires_at": found[0]["expires_at"],
        "service": "telegram",
        "username": "ada",
    }
    assert found[1] == {
        "v": 1,
        "type": "connect",
        "state": "connecting",
        "service": "telegram",
        "from": "telegram:123456789",
        "handle": "@ada",
    }
    assert [(o["type"], o.get("state")) for o in found[2:]] == [
        ("pair_code", None),
        ("pair", "accepted"),
        ("pair", "waiting"),
        ("pair", "sas_pending"),
    ]
    assert found[2]["note"] == "lucille-host/2 n=" + connect.nonce(_key(request)).hex()
    # The request is spent once its code is answered; the pair's gate takes over.
    assert not cli.dirs.connect_file.exists()
    assert cli.held() == [lucille.name]
    assert gate.load(cli.dirs.gate_file) is not None
    assert "connect" in cli.audit()


def test_begin_in_text_shows_the_code_to_send(cli: HostCli, signer: Signer) -> None:
    running = cli.start("pair", "begin", stdin=STDIN)
    request = _waiting(cli)
    lucille = cli.lucille()
    lucille.deliver(connect.shown(request.code), signer)
    with collecting(lucille):
        ran = running.wait()
    assert ran.rc == 0, ran.text
    assert f"connect {connect.shown(request.code)}" in ran.out
    assert "connecting: as @ada on Telegram" in ran.out


@pytest.mark.parametrize("entry", ["slack\nada\n", "\nada\n", "Telegram\nada\n", ""])
def test_begin_refuses_a_service_capsule_does_not_offer(cli: HostCli, entry: str) -> None:
    ran = cli.run("pair", "begin", "--json", stdin=entry)
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "bad_service"
    assert not cli.dirs.connect_file.exists()


@pytest.mark.parametrize("name", ["", "@", "1ada", "ada lovelace", "@telegram:ada", "adа"])
def test_begin_refuses_a_username_outside_its_grammar(cli: HostCli, name: str) -> None:
    ran = cli.run("pair", "begin", "--json", stdin=f"telegram\n{name}\n")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "bad_username"
    assert not cli.dirs.connect_file.exists()


def test_begin_takes_every_spelling_of_the_username(cli: HostCli) -> None:
    running = cli.start("pair", "begin", "--json", stdin="telegram\n  Telegram:@ADA \n")
    assert _waiting(cli).username == "ada"
    cli.stopping.set()
    ran = running.wait()
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "interrupted"
    # Ctrl-C keeps the request, for `pair resume`.
    assert _request(cli).username == "ada"


def test_begin_keeps_no_request_when_the_hub_cannot_be_reached(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*args: Any, **kwargs: Any) -> Any:
        raise _switchboard.HubError("the hub could not be reached (OSError)")

    monkeypatch.setattr("lucille_host._switchboard.DropClient.post", down)
    ran = cli.run("pair", "begin", "--json", stdin=STDIN)
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "hub_error"
    assert not cli.dirs.connect_file.exists()


def test_begin_refuses_while_a_pair_is_held(cli: HostCli, signer: Signer) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    with collecting(lucille):
        assert cli.run("pair", "--code-stdin", stdin=code + "\n").rc == 0
    ran = cli.run("pair", "begin", "--json", stdin=STDIN)
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "held"
    assert not cli.dirs.connect_file.exists()


def test_begin_refuses_while_another_pair_runs(cli: HostCli) -> None:
    cli.dirs.state.mkdir(parents=True, exist_ok=True)
    with lock_held(cli.dirs.state / PAIR_LOCK):
        ran = cli.run("pair", "begin", "--json", stdin=STDIN)
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "busy"


@pytest.mark.parametrize(
    "argv",
    [
        ("pair", "begin", "--code-stdin"),
        ("pair", "resume", "--hub", "http://127.0.0.1:8765"),
        ("pair", "resume", "--store-file"),
        ("pair", "cancel", "--store-file"),
    ],
)
def test_the_connect_actions_take_their_options_only(cli: HostCli, argv: tuple[str, ...]) -> None:
    ran = cli.run(*argv, "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "bad_options"


# --- how a request ends ------------------------------------------------------------------------


def test_another_account_ends_the_connect_and_names_no_account(
    cli: HostCli, signer: Signer
) -> None:
    running = cli.start("pair", "begin", "--json", stdin=STDIN)
    request = _waiting(cli)
    cli.lucille().deliver(
        connect.shown(request.code), signer, sender="telegram:987654321", handle="@Lucille_help"
    )
    ran = running.wait()
    assert ran.rc == 1
    error = _objects(ran)[-1]
    assert error["code"] == "other_account"
    assert "Lucille_help" not in ran.text
    assert "Lucille_help" not in cli.audit()
    assert "telegram:987654321" in cli.audit()
    assert not cli.dirs.connect_file.exists()
    assert cli.held() == []


def test_a_second_hello_ends_the_connect_with_the_interference_warning(cli: HostCli) -> None:
    running = cli.start("pair", "begin", "--json", stdin=STDIN)
    request = _waiting(cli)
    cli.lucille().drop(_key(request), connect.HELLO)
    ran = running.wait()
    assert ran.rc == 1
    error = _objects(ran)[-1]
    assert error["code"] == "interference"
    assert error["message"] == connecting.MESSAGES["interference"]
    assert not cli.dirs.connect_file.exists()


def test_two_deliveries_end_the_connect(cli: HostCli, signer: Signer) -> None:
    live = _begun(cli)
    lucille = cli.lucille()
    lucille.deliver(connect.shown(live.request.code), signer)
    lucille.deliver(connect.shown(live.request.code), signer)
    ran = cli.run("pair", "resume", "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "key_misused"
    assert cli.held() == []


def test_the_request_expires(cli: HostCli) -> None:
    _begun(cli)
    cli.h.advance(connecting.LIFE)
    ran = cli.run("pair", "resume", "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "connect_expired"
    assert not cli.dirs.connect_file.exists()


def test_a_note_for_another_request_is_not_this_macs(cli: HostCli, signer: Signer) -> None:
    live = _begun(cli)
    cli.lucille().deliver(
        connect.shown(live.request.code), signer, note="lucille-host/2 n=" + "0" * 32
    )
    ran = cli.run("pair", "resume", "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "not_this_mac"
    assert cli.held() == []


def test_a_delivered_code_for_another_hub_is_refused_and_spends_the_request(
    cli: HostCli, signer: Signer
) -> None:
    live = _begun(cli)
    other = FakeLucille(cli.h, cli.folder / "elsewhere", url="https://hub.example")
    # Lucille's room, a code naming another hub.
    other.deliver(connect.shown(live.request.code), signer)
    ran = cli.run("pair", "resume", "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "wrong_hub"
    assert not cli.dirs.connect_file.exists()


# --- pair resume and pair cancel ------------------------------------------------------------------


def test_resume_waits_on_the_kept_request(cli: HostCli, signer: Signer) -> None:
    live = _begun(cli)
    lucille = cli.lucille()
    lucille.deliver(connect.shown(live.request.code), signer)
    with collecting(lucille):
        ran = cli.run("pair", "resume", "--json")
    assert ran.rc == 0, ran.text
    assert [(o["type"], o.get("state")) for o in _objects(ran)][:3] == [
        ("connect", "code"),
        ("connect", "connecting"),
        ("pair_code", None),
    ]
    assert cli.held() == [lucille.name]


def test_resume_with_no_request_is_none(cli: HostCli) -> None:
    ran = cli.run("pair", "resume", "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "none"


def test_cancel_drops_the_request(cli: HostCli) -> None:
    _begun(cli)
    ran = cli.run("pair", "cancel", "--json")
    assert ran.rc == 0, ran.text
    assert _objects(ran) == [{"v": 1, "type": "connect", "state": "cancelled"}]
    assert not cli.dirs.connect_file.exists()
    ran = cli.run("pair", "cancel", "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "none"


# --- pair status --------------------------------------------------------------------------------


def test_status_reports_the_live_request_after_one_read(cli: HostCli) -> None:
    live = _begun(cli)
    ran = cli.run("pair", "status", "--json")
    assert ran.rc == 0, ran.text
    assert _objects(ran) == [
        {
            "v": 1,
            "type": "connect",
            "state": "code",
            "code": connect.shown(live.request.code),
            "expires_at": _objects(ran)[0]["expires_at"],
            "service": "telegram",
            "username": "ada",
        }
    ]
    assert _request(cli).last_read == "ok"


def test_status_finds_a_delivery_and_redeems_nothing(cli: HostCli, signer: Signer) -> None:
    live = _begun(cli)
    lucille = cli.lucille()
    lucille.deliver(connect.shown(live.request.code), signer)
    ran = cli.run("pair", "status", "--json")
    assert ran.rc == 0, ran.text
    assert _objects(ran) == [
        {"v": 1, "type": "connect", "state": "delivered", "service": "telegram"}
    ]
    assert cli.held() == []
    assert gate.load(cli.dirs.gate_file) is None
    kept = _request(cli)
    assert kept.delivery_seen and not kept.used
    # Lucille's code was not answered: it has nothing to collect.
    assert lucille.collect().state == "waiting"


def test_status_while_begin_waits_reports_from_the_file(cli: HostCli, signer: Signer) -> None:
    live = _begun(cli)
    with lock_held(cli.dirs.state / PAIR_LOCK):
        ran = cli.run("pair", "status", "--json")
    assert ran.rc == 0, ran.text
    assert _objects(ran)[0]["state"] == "code"
    # No read was made: what the file says is unchanged.
    assert _request(cli) == live.request


def test_status_reports_an_expired_request_and_spends_nothing(cli: HostCli) -> None:
    live = _begun(cli)
    cli.h.advance(connecting.LIFE + 1)
    ran = cli.run("pair", "status", "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "connect_expired"
    assert _request(cli) == live.request
    # `pair resume` spends it.
    ran = cli.run("pair", "resume", "--json")
    assert _objects(ran)[-1]["code"] == "connect_expired"
    assert not cli.dirs.connect_file.exists()


def test_status_reports_interference_and_spends_nothing(cli: HostCli) -> None:
    live = _begun(cli)
    cli.lucille().drop(_key(live.request), connect.HELLO)
    ran = cli.run("pair", "status", "--json")
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "interference"
    assert _request(cli) == live.request


def test_status_with_no_request_and_no_pair_is_none(cli: HostCli) -> None:
    ran = cli.run("pair", "status", "--json")
    assert ran.rc == 0
    assert _objects(ran) == [{"v": 1, "type": "pair", "state": "none"}]


# --- what is kept --------------------------------------------------------------------------------


def test_the_code_and_username_are_kept_nowhere_once_connected(
    cli: HostCli, signer: Signer
) -> None:
    running = cli.start("pair", "begin", "--json", stdin="telegram\nada_lovelace\n")
    request = _waiting(cli)
    lucille = cli.lucille()
    lucille.deliver(connect.shown(request.code), signer, handle="@ada_lovelace")
    with collecting(lucille):
        ran = running.wait()
    assert ran.rc == 0, ran.text
    for path in cli.env.home.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert request.code not in text, path
            assert "ada_lovelace" not in text, path
    assert all(
        request.code not in value and "ada_lovelace" not in value
        for value in cli.env.keyring.items.values()
    )
    assert json.dumps(request.code) not in cli.audit()


# --- the test key --------------------------------------------------------------------------------

LOOPBACK = "http://127.0.0.1:8765"


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_begin_with_a_test_key_takes_answers_signed_with_it(cli: HostCli) -> None:
    # No key is pinned in Lucille's place: the test key, named on a loopback hub, is taken.
    made = Signer()
    raw = next(iter(made.keys.values()))
    running = cli.start(
        "pair", "begin", "--json", "--hub", LOOPBACK, "--test-key", _b64(raw), stdin=STDIN
    )
    request = _waiting(cli)
    assert request.hub == LOOPBACK
    lucille = cli.lucille(url=LOOPBACK)
    lucille.deliver(connect.shown(request.code), made)
    with collecting(lucille):
        ran = running.wait()
    assert ran.rc == 0, ran.text
    assert _objects(ran)[-1]["state"] == "sas_pending"
    assert state.load_host_config(cli.dirs).test_key == _b64(raw)


def test_without_the_test_key_its_answers_are_unknown(cli: HostCli) -> None:
    made = Signer()
    running = cli.start("pair", "begin", "--json", "--hub", LOOPBACK, stdin=STDIN)
    request = _waiting(cli)
    cli.lucille(url=LOOPBACK).deliver(connect.shown(request.code), made)
    # Once a read has seen the answer, and ignored it for its unknown key.
    until(lambda: _request(cli).unknown_key_seen)
    cli.h.advance(connecting.LIFE)
    ran = running.wait()
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "newer_capsule"


@pytest.mark.parametrize(
    "argv",
    [
        ("pair", "begin", "--test-key", "A" * 43),
        ("pair", "resume", "--test-key", "A" * 43),
        ("pair", "cancel", "--test-key", "A" * 43),
        ("pair", "status", "--test-key", "A" * 43),
        ("pair", "--code-stdin", "--test-key", "A" * 43),
        ("pair", "begin", "--hub", LOOPBACK, "--test-key", "A" * 42),
        ("pair", "begin", "--hub", LOOPBACK, "--test-key", "A" * 42 + "B"),
        ("pair", "begin", "--hub", LOOPBACK, "--test-key", "A" * 42 + "="),
    ],
)
def test_the_test_key_is_taken_by_begin_with_a_loopback_hub_only(
    cli: HostCli, argv: tuple[str, ...]
) -> None:
    ran = cli.run(*argv, "--json", stdin=STDIN)
    assert ran.rc == 1
    assert _objects(ran)[-1]["code"] == "bad_options"
    assert not cli.dirs.connect_file.exists()


def test_trusted_keys_add_the_test_key_for_its_own_hub_only() -> None:
    made = Signer()
    raw = next(iter(made.keys.values()))
    request = connecting.Request(
        code="7KQ4MX2D9P",
        epoch=1,
        hub=LOOPBACK,
        service="telegram",
        username="ada",
        posted_at=1.0,
        expires_at=2.0,
    )
    release = dict(_switchboard.LUCILLE_SIGNING_KEYS)
    with_key = state.HostConfig(hub=LOOPBACK, test_key=_b64(raw))
    assert dict(connecting.trusted_keys(request, with_key)) == {**release, made.kid: raw}
    assert dict(connecting.trusted_keys(request, state.HostConfig(hub=LOOPBACK))) == release
    on_lucilles = dataclasses.replace(request, hub=_switchboard.LUCILLE_HUB)
    assert dict(connecting.trusted_keys(on_lucilles, with_key)) == release
    other_port = state.HostConfig(hub="http://127.0.0.1:9999", test_key=_b64(raw))
    assert dict(connecting.trusted_keys(request, other_port)) == release


def test_status_never_writes_back_a_request_answered_while_it_waited_for_the_lock(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pair resume` answers the code and deletes the request in the moment between `status`
    # seeing the file and taking the pair lock.
    _begun(cli)
    real = loop.one_at_a_time

    @contextlib.contextmanager
    def answered_first(lock: Path, busy: str, verb: str) -> Iterator[None]:
        if verb == "`pair status`":
            connecting.remove(cli.dirs.connect_file)
        with real(lock, busy, verb):
            yield

    monkeypatch.setattr("lucille_host.loop.one_at_a_time", answered_first)
    ran = cli.run("pair", "status", "--json")
    assert ran.rc == 0, ran.text
    assert _objects(ran) == [{"v": 1, "type": "pair", "state": "none"}]
    assert not cli.dirs.connect_file.exists()
