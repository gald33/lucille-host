"""The suite's pairing: its Lucille finds the host's Connect request in every epoch Lucille reads
(decisions/0002 §11.4, step 6), and a pairing that stalls says how far each side got, without the
codes.

The tests of :meth:`Lucille.deliver` start a real hub (``switchboard serve``, from the
``conformance`` extra), and play the host's side by posting its hello into a drop room. They need
no ``lucille-host``, so they run as root too.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from switchboard import pairing

from lucille_host import connect, delivery
from lucille_host.conformance import _spawn
from lucille_host.conformance._host import (
    HarnessError,
    check_sas,
    host_env,
    outline,
    summary,
    until,
)
from lucille_host.conformance._lucille import (
    EPOCH_OFFSETS,
    Lucille,
    NoRequest,
    connect_client,
)

# A moment in the middle of an epoch, so that no test here crosses into the next one.
NOW = float(connect.EPOCH_SECONDS * 1_480_000 + connect.EPOCH_SECONDS // 2)


@pytest.fixture(scope="module")
def hub(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """One hub for the module: each test draws a code of its own, so no two share a drop room."""
    pytest.importorskip("uvicorn", reason="switchboard serve needs the conformance extra")
    pytest.importorskip("fastapi", reason="switchboard serve needs the conformance extra")
    assert _spawn.SWITCHBOARD_EXE.is_file(), _spawn.SWITCHBOARD_EXE
    folder = tmp_path_factory.mktemp("hub")
    folder.chmod(0o700)
    child = _spawn.start_hub(folder, host_env(folder, folder))
    try:

        def listening() -> str | None:
            if not child.alive():
                raise HarnessError(f"the hub exited\n{child.describe()}")
            found = _spawn.LISTENING.search(child.stderr)
            return found.group(1) if found else None

        yield until(listening, "the hub listening")
    finally:
        child.stop(grace=1.0)


@pytest.fixture
def code() -> str:
    """A connect code of this test's own, as the host shows it."""
    return connect.shown(connect.draw_code())


def _lucille(url: str, tmp_path: Path) -> Lucille:
    folder = tmp_path / "lucille"
    folder.mkdir(mode=0o700)
    lucille = Lucille(url, folder)
    lucille.clock = lambda: NOW
    return lucille


@functools.cache
def _key(code: str, offset: int) -> bytes:
    canonical = connect.parse_code(code)
    assert canonical is not None
    return connect.stretch(canonical, connect.epoch(NOW) + offset)


def _bodies(lucille: Lucille, key: bytes) -> list[object]:
    with connect_client(lucille._drop(key), agent_id=pairing.throwaway_agent("host")) as reader:
        return [row.get("body") for row in reader.history(connect.CHANNEL)]


def _hello(lucille: Lucille, key: bytes) -> None:
    """Post the host's hello into the drop room of ``key``, as ``pair begin`` does."""
    with connect_client(lucille._drop(key), agent_id=pairing.throwaway_agent("host")) as poster:
        poster.post(connect.CHANNEL, connect.HELLO, ttl=connect.POST_TTL)


def test_lucille_reads_the_four_epochs_lucille_reads() -> None:
    assert EPOCH_OFFSETS == (0, -1, 1, -2)


# Named here, not read from EPOCH_OFFSETS, so that a shorter list there fails these too.
@pytest.mark.parametrize("offset", [0, -1, 1, -2])
def test_deliver_answers_a_request_in_any_epoch_lucille_reads(
    hub: str, tmp_path: Path, code: str, offset: int
) -> None:
    # The host draws its code a moment before an epoch ends, and Lucille stretches it in the
    # next one: the hello is in the room of E-1 of Lucille's clock, and its code must go there.
    lucille = _lucille(hub, tmp_path)
    _hello(lucille, _key(code, offset))
    assert lucille.deliver(code, sender="telegram:123456789", handle="@ada") == offset
    assert lucille.name is not None
    for other in EPOCH_OFFSETS:
        bodies = _bodies(lucille, _key(code, other))
        if other != offset:
            assert bodies == []
            continue
        assert bodies[0] == connect.HELLO
        assert len(bodies) == 2
        sent = json.loads(str(bodies[1]))
        assert sent["t"] == delivery.TYPE
        assert sent["from"] == "telegram:123456789"


def test_deliver_without_a_hello_delivers_nothing(hub: str, tmp_path: Path, code: str) -> None:
    lucille = _lucille(hub, tmp_path)
    with pytest.raises(NoRequest, match="epochs none of Lucille's clock"):
        lucille.deliver(code, sender="telegram:123456789", handle="@ada")
    assert lucille.name is None
    assert all(_bodies(lucille, _key(code, offset)) == [] for offset in EPOCH_OFFSETS)


def test_deliver_with_a_hello_in_two_epochs_delivers_nothing(
    hub: str, tmp_path: Path, code: str
) -> None:
    lucille = _lucille(hub, tmp_path)
    _hello(lucille, _key(code, 0))
    _hello(lucille, _key(code, -1))
    with pytest.raises(NoRequest, match="epochs \\+0, -1 of Lucille's clock"):
        lucille.deliver(code, sender="telegram:123456789", handle="@ada")
    assert lucille.name is None
    assert _bodies(lucille, _key(code, 0)) == [connect.HELLO]
    assert _bodies(lucille, _key(code, -1)) == [connect.HELLO]


# --- what a stalled pairing shows -------------------------------------------------------------

SECRETS = ("7KQ4M-X2D9P", "pair-abcdef", "0123456789abcdef0123456789abcdef", "4821", "hunter2")


def _child(objects: list[object], stderr: str) -> _spawn.Child:
    script = (
        "import sys\n"
        f"sys.stdout.write({''.join(json.dumps(o) + chr(10) for o in objects)!r})\n"
        f"sys.stderr.write({stderr!r})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child = _spawn.Child(proc, ["pair", "begin"])
    assert child.wait(30) == 0
    return child


def test_outline_shows_types_states_and_error_codes_only() -> None:
    child = _child(
        [
            {
                "v": 1,
                "type": "connect",
                "state": "code",
                "code": SECRETS[0],
                "service": "telegram",
                "username": "ada",
                "expires_at": "2026-11-01T12:00:00Z",
            },
            {
                "v": 1,
                "type": "connect",
                "state": "connecting",
                "service": "telegram",
                "handle": "@ada",
                "from": "telegram:123456789",
            },
            {
                "v": 1,
                "type": "pair_code",
                "name": SECRETS[1],
                "hub": "http://127.0.0.1:1",
                "note": f"lucille-host/2 n={SECRETS[2]}",
            },
            {
                "v": 1,
                "type": "pair",
                "state": "sas_pending",
                "name": SECRETS[1],
                "sas": SECRETS[3],
                "deadline": "2026-11-01T12:00:00Z",
            },
            {
                "v": 1,
                "type": "error",
                "kind": "hub",
                "code": "hub_unreachable",
                "message": f"the hub refused {SECRETS[4]}",
            },
        ],
        f"a line naming {SECRETS[4]}\nand another\n",
    )
    shown = outline(child)
    assert shown == (
        "pair begin: exit 0; wrote connect code, connect connecting, pair_code, pair sas_pending, "
        "error (code hub_unreachable, kind hub); 2 line(s) on stderr"
    )
    assert not [secret for secret in SECRETS if secret in shown]
    assert "@ada" not in shown
    assert "telegram:" not in shown


def test_outline_of_a_child_that_wrote_nothing() -> None:
    assert outline(_child([], "")) == "pair begin: exit 0; wrote nothing; 0 line(s) on stderr"


def test_a_wait_that_times_out_says_what_detail_says_then() -> None:
    seen: list[int] = []

    def never() -> bool:
        seen.append(1)
        return False

    with pytest.raises(HarnessError) as raised:
        until(never, "the thing", seconds=0.2, detail=lambda: f"asked {len(seen)} times")
    assert str(raised.value) == f"the thing did not happen within 0.2 s\nasked {len(seen)} times"


def test_a_wait_without_detail_says_only_what_did_not_happen() -> None:
    with pytest.raises(HarnessError) as raised:
        until(lambda: False, "the thing", seconds=0.1)
    assert str(raised.value) == "the thing did not happen within 0.1 s"


def test_summary_of_pair_confirm_shows_its_state_not_its_name() -> None:
    written = [{"v": 1, "type": "pair", "state": "sas_pending", "name": SECRETS[1]}]
    assert summary(written) == "pair sas_pending"
    assert summary([]) == "nothing"


def _sas_pending(sas: str) -> dict[str, object]:
    return {
        "v": 1,
        "type": "pair",
        "state": "sas_pending",
        "name": SECRETS[1],
        "sas": sas,
        "deadline": "2026-11-01T12:00:00Z",
    }


def test_check_sas_passes_when_the_host_shows_lucilles_sas() -> None:
    check_sas(_child([_sas_pending("4821")], ""), "4821")


@pytest.mark.parametrize(
    ("written", "held", "said"),
    [
        (
            [_sas_pending("4821")],
            "9374",
            "pair begin ended on pair sas_pending; the SAS it showed is not the one Lucille holds",
        ),
        (
            [_sas_pending("4821")],
            None,
            "pair begin ended on pair sas_pending; the SAS it showed is not the one Lucille "
            "holds, which is none",
        ),
        (
            [{"v": 1, "type": "pair", "state": "waiting", "name": SECRETS[1]}],
            "4821",
            "pair begin ended on pair waiting; the SAS it showed is not the one Lucille holds",
        ),
        (
            [],
            "4821",
            "pair begin ended on nothing; the SAS it showed is not the one Lucille holds",
        ),
    ],
)
def test_check_sas_says_which_without_saying_either_sas(
    written: list[object], held: str | None, said: str
) -> None:
    with pytest.raises(HarnessError) as raised:
        check_sas(_child(written, f"a line naming {SECRETS[4]}\n"), held)
    message = str(raised.value)
    assert message.splitlines()[0] == said
    assert message.splitlines()[1].startswith("pair begin: exit 0; wrote ")
    for secret in ("4821", "9374", *SECRETS):
        assert secret not in message
