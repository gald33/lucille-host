"""``lucille-host pair``: taking Lucille's code, waiting for it to collect the answer, and the gate.

The command runs in-process against Switchboard's test hub, with a fake Lucille on the other side
(``tests/_cli.py``). Lucille's codes name Lucille's hub; the in-process transport reaches the test
hub whatever the address.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from keyring.backends.fail import Keyring as FailKeyring
from switchboard import pairing
from switchboard.testing import hub

from lucille_host import _switchboard, gate, link, paths, policy
from lucille_host.cli import main
from tests import _audit_hook
from tests._cli import HostCli, collecting, until
from tests._host import HostEnv
from tests._lucille import FakeLucille

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

LOOPBACK = "http://127.0.0.1:8765"


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
    # A run still waiting when the test ends stops before the hub does.
    host.stopping.set()


def _recode(code: str, **changes: Any) -> str:
    """The same code with some of its fields changed, as somebody who held it could make."""
    return dataclasses.replace(pairing.PairingCode.decode(code), **changes).encode()


def _enable_and_add_a_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    assert main(["ops", "enable", "files.sha256"]) == 0
    assert main(["roots", "add", "docs", str(folder)]) == 0
    return folder


def _nothing_changed(cli: HostCli, before: policy.Policy) -> None:
    assert cli.gate() is None
    assert cli.held() == []
    assert cli.policy() == before
    assert not cli.dirs.previous_policy_file.exists()
    assert not cli.dirs.host_file.exists()


# --- the code ------------------------------------------------------------------------------------


def test_pair_refuses_foreign_hub(cli: HostCli, tmp_path: Path) -> None:
    _enable_and_add_a_folder(tmp_path)
    before = cli.policy()
    for url in ("https://hub.example", "https://switchboard.lucille-ai.com.example"):
        lucille = cli.lucille(url=url)
        ran = cli.run("pair", lucille.start())
        assert ran.rc == 1
        assert ran.err.startswith("refused: this code is for another hub"), ran.err
        _nothing_changed(cli, before)
        # Nothing was answered: Lucille has nothing to collect.
        assert lucille.collect().state == "waiting"
    # The same code with Lucille's hub in it is accepted.
    lucille = cli.lucille(url="https://hub.example")
    code = _recode(lucille.start(), url=_switchboard.LUCILLE_HUB + "/")
    with collecting(lucille):
        ran = cli.run("pair", "--code-stdin", stdin=code + "\n")
    assert ran.rc == 0, ran.text


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://switchboard.lucille-ai.com", id="http"),
        pytest.param("HTTPS://SWITCHBOARD.LUCILLE-AI.COM", id="upper-case"),
        pytest.param("https://Switchboard.Lucille-AI.com", id="host-case"),
        pytest.param("https://switchboard.lucille-ai.com:443", id="default-port"),
        pytest.param("https://switchboard.lucille-ai.com/path", id="path"),
        pytest.param("https://switchboard.lucille-ai.com/?q", id="query"),
        pytest.param("https://switchboard.lucille-ai.com#f", id="fragment"),
    ],
)
def test_pair_compares_the_hub_as_a_string(url: str, cli: HostCli) -> None:
    # Only Lucille's hub URL as it is written, every trailing / aside: another spelling of it is
    # another hub, as check 3 has it for a room's hub.
    lucille = cli.lucille(url="https://hub.example")
    code = _recode(lucille.start(), url=url)
    assert pairing.PairingCode.decode(code).url == url
    ran = cli.run("pair", code)
    assert ran.rc == 1
    assert ran.err.startswith("refused: this code is for another hub"), ran.err
    assert cli.gate() is None and cli.held() == []


def test_pair_refuses_code_with_token(cli: HostCli, tmp_path: Path) -> None:
    _enable_and_add_a_folder(tmp_path)
    before = cli.policy()
    lucille = cli.lucille()
    code = _recode(lucille.start(), token="sb_a_hub_token")
    assert pairing.PairingCode.decode(code).token == "sb_a_hub_token"
    ran = cli.run("pair", code)
    assert ran.rc == 1
    assert ran.err.startswith("refused: this code carries a hub token"), ran.err
    assert "sb_a_hub_token" not in ran.text
    _nothing_changed(cli, before)
    assert lucille.collect().state == "waiting"


def test_pair_refuses_unconfigured_loopback(cli: HostCli, tmp_path: Path) -> None:
    _enable_and_add_a_folder(tmp_path)
    before = cli.policy()
    lucille = cli.lucille(url=LOOPBACK)
    code = lucille.start()
    for options in ([], ["--hub", "http://127.0.0.1:8766"], ["--hub", "http://localhost:8765"]):
        ran = cli.run("pair", *options, code)
        assert ran.rc == 1
        assert ran.err.startswith("refused: "), ran.err
        assert "--hub" in ran.err
        _nothing_changed(cli, before)
    assert lucille.collect().state == "waiting"


def test_pair_accepts_configured_loopback(cli: HostCli) -> None:
    for wrong in ("https://127.0.0.1:8765", "http://127.0.0.1:0", "http://10.0.0.1:8765"):
        ran = cli.run("pair", "--hub", wrong, "swbp1_x")
        assert ran.rc == 1 and ran.err.startswith("lucille-host: a configured hub"), ran.err
    lucille = cli.lucille(url=LOOPBACK)
    ran = cli.paired(lucille, "--hub", LOOPBACK)
    assert f"on {LOOPBACK}" in ran.out
    saved = json.loads(cli.dirs.host_file.read_text())
    assert saved == {"v": 1, "hub": LOOPBACK, "store": "keyring"}
    found = cli.gate()
    assert found is not None and found.hub == LOOPBACK


def test_pair_note_shown_sanitized(cli: HostCli) -> None:
    lucille = cli.lucille()
    unsafe = "Gal's‮ laptop\x1b[2J⁦x‏؜ \x85\ud800" + "n" * 400
    code = lucille.start(note=unsafe)
    assert pairing.PairingCode.decode(code).note == unsafe
    with collecting(lucille):
        ran = cli.run("pair", "--code-stdin", stdin=code + "\n")
    assert ran.rc == 0, ran.text
    notes = [line for line in ran.out.splitlines() if line.startswith("note: ")]
    assert len(notes) == 1
    shown = notes[0].removeprefix("note: ")
    assert shown.startswith("Gal's  laptop [2J x")
    assert len(shown) <= link.NOTE_LIMIT
    assert all(" " <= char <= "~" for char in shown), shown


def test_sanitize_replaces_every_unsafe_character() -> None:
    unsafe = [chr(code) for code in range(0x20)] + [chr(code) for code in range(0x7F, 0xA0)]
    unsafe += ["\ud800", "\udfff", "؜", "‎", "‏", " ", " "]
    unsafe += [chr(code) for code in range(0x202A, 0x202F)]
    unsafe += [chr(code) for code in range(0x2066, 0x206A)]
    for char in unsafe:
        assert link.sanitize(f"a{char}b") == "a b", repr(char)
    assert link.sanitize("Gal's laptop, 2nd floor é") == "Gal's laptop, 2nd floor é"
    assert link.sanitize("x" * 300) == "x" * 200


def test_pair_refuses_while_held(cli: HostCli, tmp_path: Path) -> None:
    first = cli.lucille()
    cli.interrupted(first.start())
    held = cli.held()
    assert len(held) == 1
    _enable_and_add_a_folder(tmp_path)
    before, kept = cli.policy(), cli.gate()
    second = cli.lucille()
    for argv in (["pair", second.start()], ["pair", "--store-file", second.start()]):
        ran = cli.run(*argv)
        assert ran.rc == 1
        assert "Disconnect first" in ran.err, ran.err
        assert cli.held() == held and cli.gate() == kept and cli.policy() == before
    assert second.collect().state == "waiting"
    # Once the first pair is confirmed, the same.
    assert first.collect().state == "paired"
    assert cli.run("pair", "status").out.startswith("sas_pending:")
    assert cli.run("pair", "confirm").rc == 0
    ran = cli.run("pair", second.start())
    assert ran.rc == 1 and "Disconnect first" in ran.err
    assert cli.held() == held


def test_store_file_pairs_where_the_keyring_stores_nothing(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A computer with no keyring to store in, such as a headless Linux box: the case
    # --store-file is for. Before, `pair --store-file` looked in the keyring first, and refused.
    monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring())
    lucille = cli.lucille()
    ran = cli.run("pair", "--code-stdin", stdin=lucille.start() + "\n")
    assert ran.rc == 1 and "has no keyring to store in" in ran.err and "--store-file" in ran.err
    assert cli.gate() is None
    ran = cli.paired(lucille, "--store-file")
    assert "weaker than the OS keyring" in ran.err
    assert cli.run("pair", "confirm").rc == 0
    assert cli.held() == [lucille.name]
    assert any(cli.dirs.store_files.iterdir()) and cli.env.keyring.items == {}
    status = cli.run("status")
    assert status.rc == 0 and f"pair: confirmed: pair {lucille.name}" in status.out, status.text


def test_code_never_in_argv(cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    ran = cli.run("pair", "--code-stdin", code)
    assert ran.rc == 1
    assert "read from stdin only" in ran.err
    assert cli.gate() is None and cli.held() == []
    assert cli.run("pair", "--code-stdin", stdin="\n").rc == 1
    with collecting(lucille):
        ran = cli.run("pair", "--code-stdin", stdin=code + "\n")
    assert ran.rc == 0, ran.text
    # The code is nowhere the host writes: not its output, its files or its keyring item.
    assert code not in ran.text
    for path in cli.env.home.rglob("*"):
        if path.is_file():
            assert code not in path.read_text(errors="replace"), path
    assert all(code not in value for value in cli.env.keyring.items.values())


def test_pair_reads_the_code_from_stdin_when_it_is_not_given(cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    with collecting(lucille):
        ran = cli.run("pair", stdin=code + "\n")
    # With no answer after the code, the SAS is shown, and the answer left for `pair confirm`
    # or `pair reject`.
    assert ran.rc == 0, ran.text
    found = cli.gate()
    assert found is not None and found.deadline is not None
    assert ran.out.splitlines()[-2:] == [
        f"sas_pending: pair {lucille.name}: the SAS is {lucille.sas()}. If Lucille's page shows "
        f"the same, confirm it with `lucille-host pair confirm` before "
        f"{link.iso(found.deadline)}; if not, `lucille-host pair reject`",
        f"sas_pending: pair {lucille.name} is not confirmed yet: `lucille-host pair confirm` "
        f"confirms it before {link.iso(found.deadline)}, and `lucille-host pair reject` "
        "rejects it",
    ]
    assert found.opened_at is None and cli.held() == [lucille.name]


# --- waiting for Lucille to collect --------------------------------------------------------------


def test_code_stdin_waits_until_sas_pending(cli: HostCli) -> None:
    lucille = cli.lucille()
    ran = cli.paired(lucille)
    lines = ran.out.splitlines()
    assert lines[0] == f"pairing code for pair {lucille.name}, on {_switchboard.LUCILLE_HUB}"
    assert [line.split(":", 1)[0] for line in lines[1:]] == ["accepted", "waiting", "sas_pending"]
    found = cli.gate()
    assert found is not None and found.pair == lucille.name
    assert found.shown_at == cli.h.now and found.deadline == found.shown_at + gate.WINDOW
    assert found.opened_at is None
    # It stops once the SAS is shown, as Lucille's page shows it, with the deadline.
    assert f"the SAS is {lucille.sas()}." in lines[-1]
    assert link.iso(found.deadline) in lines[-1]


def test_pair_completes_without_paired_note(cli: HostCli) -> None:
    suppressed: list[str] = []

    class Quiet(cli.h.client_class()):  # type: ignore[misc]
        """Lucille's client, losing the best-effort note that says the answer was collected."""

        def post(self, channel: str, body: Any, **options: Any) -> Any:
            if isinstance(body, dict) and body.get("t") == pairing.TAG_PAIRED:
                suppressed.append(channel)
                raise httpx.ConnectError("the paired note is lost")
            return super().post(channel, body, **options)

    lucille = cli.lucille(connect=Quiet)
    running = cli.start("pair", "--code-stdin", stdin=lucille.start() + "\n")
    # Lucille collects only once the host has resolved and is listening for a note.
    until(lambda: cli.listens > 0)
    assert lucille.collect().state == "paired"
    assert suppressed and set(suppressed) == {pairing.CHANNEL}
    ran = running.wait(15)
    assert ran.rc == 0, ran.text
    assert ran.out.splitlines()[-1].startswith("sas_pending: ")


def test_a_paired_note_wakes_the_wait_at_once(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the periodic resolve far off, only the note can end the wait in time.
    monkeypatch.setattr("lucille_host.link.RESOLVE_EVERY", 600.0)
    lucille = cli.lucille()
    running = cli.start("pair", "--code-stdin", stdin=lucille.start() + "\n")
    until(lambda: cli.listens > 0)
    assert lucille.collect().state == "paired"
    ran = running.wait(15)
    assert ran.rc == 0 and ran.out.splitlines()[-1].startswith("sas_pending: "), ran.text


def _answered_twice(cli: HostCli, lucille: FakeLucille, code: str) -> None:
    """Somebody else holding the code answers it too, and Lucille, seeing two answers, aborts."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("switchboard.pairing._refuse_used", lambda seen, code: None)
        other = pairing.PairStore(cli.folder / "other", env={})
        with pytest.raises(pairing.AlreadyRedeemed):
            pairing.accept(code, None, store=other, connect=cli.h.client_class(), now=cli.h.now)
    assert lucille.collect().state == "aborted"


@pytest.mark.parametrize("case", ["already_redeemed", "aborted", "superseded"])
def test_pair_leaked_states_stop(case: str, cli: HostCli, monkeypatch: pytest.MonkeyPatch) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    if case == "already_redeemed":
        other = pairing.PairStore(cli.folder / "other", env={})
        pairing.accept(code, None, store=other, connect=cli.h.client_class(), now=cli.h.now)
        ran = cli.run("pair", "--code-stdin", stdin=code + "\n")
    elif case == "aborted":
        running = cli.start("pair", "--code-stdin", stdin=code + "\n")
        until(lambda: '"event":"accepted"' in cli.audit())
        _answered_twice(cli, lucille, code)
        ran = running.wait()
    else:

        def superseded(*_: Any, **__: Any) -> Any:
            raise pairing.PairingError("another answer was collected", state="superseded")

        monkeypatch.setattr("switchboard.pairing.resolve", superseded)
        ran = cli.run("pair", "--code-stdin", stdin=code + "\n")
    assert ran.rc == 1
    assert ran.err.startswith("leaked: the pairing code leaked"), ran.err
    assert f"({case})" in ran.err
    assert cli.gate() is None and cli.held() == []
    assert not ran.out.splitlines()[-1].startswith("sas_pending")


@pytest.mark.parametrize("case", ["expired", "wrong_hub", "never_confirmed", "unsent"])
def test_pair_again_states_stop(case: str, cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    if case == "expired":
        cli.h.advance(2 * 3600)
        ran = cli.run("pair", code)
    elif case == "wrong_hub":
        ran = cli.run("pair", _recode(code, secret=os.urandom(32)))
    elif case == "never_confirmed":
        running = cli.start("pair", "--code-stdin", stdin=code + "\n")
        until(lambda: '"event":"accepted"' in cli.audit())
        # Lucille never collects; its window ends 12 hours after the code expires.
        cli.h.advance(13 * 3600)
        ran = running.wait()
    else:

        class NoAnswer(cli.h.client_class()):  # type: ignore[misc]
            """The host's client, whose answer to the code never reaches the hub."""

            def post(self, channel: str, body: Any, **options: Any) -> Any:
                if isinstance(body, dict) and body.get("t") == "swbp1/answer":
                    raise httpx.ConnectError("the answer is lost")
                return super().post(channel, body, **options)

        cli.client_class = NoAnswer
        running = cli.start("pair", "--code-stdin", stdin=code + "\n")
        until(lambda: '"event":"accepted"' in cli.audit())
        assert '"sent":false' in cli.audit()
        cli.h.advance(2 * 3600)
        ran = running.wait()
    assert ran.rc == 1
    assert ran.err.startswith("expired: the pairing did not finish in time"), ran.err
    assert f"({case})" in ran.err
    assert cli.gate() is None and cli.held() == []


def test_a_pair_lucille_ended_is_forgotten_here(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.paired(lucille)
    assert lucille.forget().peer_told
    ran = cli.run("pair", "status")
    assert ran.rc == 1
    assert ran.err.startswith("ended: ") and "(ended_by_peer)" in ran.err, ran.err
    assert cli.held() == [] and cli.gate() is None
    assert cli.run("pair", "status").out.startswith("none: ")


def test_a_hub_out_of_reach_while_waiting_is_waited_out(cli: HostCli) -> None:
    lucille = cli.lucille()
    out_of_reach = threading.Event()
    failed: list[str] = []

    class Flaky(cli.client_class):  # type: ignore[misc,name-defined]
        """The host's client, while the hub is out of reach."""

        def _call(self, *args: Any, **options: Any) -> Any:
            if out_of_reach.is_set():
                failed.append(str(args[:2]))
                raise httpx.ConnectError("the hub is out of reach")
            return super()._call(*args, **options)

    cli.client_class = Flaky
    running = cli.start("pair", "--code-stdin", stdin=lucille.start() + "\n")
    until(lambda: cli.listens > 0)
    out_of_reach.set()
    until(lambda: len(failed) >= 4)
    assert lucille.collect().state == "paired"
    out_of_reach.clear()
    ran = running.wait(15)
    assert ran.rc == 0, ran.text
    assert ran.out.splitlines()[-1].startswith("sas_pending: ")


def test_an_unparsable_note_in_the_pair_room_is_waited_out(cli: HostCli) -> None:
    # Whoever can write pair:<name> posts a body that opens with its key and is not JSON: the
    # wait falls back to its periodic resolve, as for any message it cannot open.
    lucille = cli.lucille()
    rooms: list[Any] = []
    notes = _switchboard.Link.pair_notes

    def kept(link: _switchboard.Link, pair: _switchboard.Pair) -> Any:
        rooms.append(pair.room)
        return notes(link, pair)

    cli.monkeypatch.setattr(_switchboard.Link, "pair_notes", kept)
    running = cli.start("pair", "--code-stdin", stdin=lucille.start() + "\n")
    until(lambda: cli.listens > 0)
    lucille.post_unparsable(rooms[-1], pairing.CHANNEL)
    listened = cli.listens
    until(lambda: cli.listens > listened + 2)
    assert lucille.collect().state == "paired"
    ran = running.wait(15)
    assert ran.rc == 0, ran.text
    assert ran.out.splitlines()[-1].startswith("sas_pending: ")


# --- pair status -------------------------------------------------------------------------------


def test_pair_status_resumes(cli: HostCli) -> None:
    ran = cli.run("pair", "status")
    assert (ran.rc, ran.out) == (0, "none: this computer holds no pair\n")
    lucille = cli.lucille()
    cli.interrupted(lucille.start())
    ran = cli.run("pair", "status")
    assert ran.rc == 0 and ran.out.startswith(f"waiting: pair {lucille.name}"), ran.text
    assert lucille.collect().state == "paired"
    ran = cli.run("pair", "status")
    found = cli.gate()
    assert found is not None and found.deadline is not None
    assert ran.rc == 0, ran.text
    assert ran.out == (
        f"sas_pending: pair {lucille.name}: the SAS is {lucille.sas()}. If Lucille's page shows "
        f"the same, confirm it with `lucille-host pair confirm` before "
        f"{link.iso(found.deadline)}; if not, `lucille-host pair reject`\n"
    )
    assert cli.run("pair", "confirm").rc == 0
    assert cli.run("pair", "status").out == f"confirmed: pair {lucille.name}\n"
    for action in ("status", "confirm", "reject"):
        ran = cli.run("pair", action, "--hub", LOOPBACK)
        assert ran.rc == 1 and "takes no options" in ran.err


def test_an_interrupted_pair_is_kept(cli: HostCli) -> None:
    lucille = cli.lucille()
    ran = cli.interrupted(lucille.start())
    assert "pair status` resumes" in ran.err
    assert cli.held() == [lucille.name]
    found = cli.gate()
    assert found is not None and found.pair == lucille.name and found.shown_at is None


# --- the gate ----------------------------------------------------------------------------------


class _User:
    """A user at ``pair``'s prompt: each answer is whatever ``answer`` returns when asked, given
    the gate as it stood then. Past :data:`ASKED_MOST` questions, stdin ends, so that a prompt
    that never takes an answer ends the command rather than asking for ever."""

    ASKED_MOST = 10

    def __init__(self, cli: HostCli, answer: Any) -> None:
        self.cli = cli
        self.answer = answer
        self.asked: list[gate.Gate | None] = []

    def readline(self) -> str:
        self.asked.append(self.cli.gate())
        if len(self.asked) > self.ASKED_MOST:
            return ""
        return str(self.answer(len(self.asked)))

    def isatty(self) -> bool:
        return False


def _events(cli: HostCli, event: str) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in cli.audit().splitlines()]
    return [line for line in lines if line.get("event") == event]


def test_pair_shows_the_sas_and_y_confirms(cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    user = _User(cli, lambda _: "y\n")
    with collecting(lucille):
        ran = cli.start("pair", code, stdin=user).wait()
    assert ran.rc == 0, ran.text
    lines = ran.out.splitlines()
    assert [line.split(":", 1)[0] for line in lines[1:]] == [
        "accepted",
        "waiting",
        "sas_pending",
        "confirmed",
    ]
    # Shown once the pair resolved, as Lucille's page shows it, before the question was asked.
    (asked,) = user.asked
    assert asked is not None and asked.shown_at is not None and asked.opened_at is None
    assert lines[3] == (
        f"sas_pending: pair {lucille.name}: the SAS is {lucille.sas()}. If Lucille's page shows "
        f"the same, confirm it with `lucille-host pair confirm` before "
        f"{link.iso(asked.deadline or 0)}; if not, `lucille-host pair reject`"
    )
    assert lines[4] == f"confirmed: pair {lucille.name} is confirmed on this computer"
    found = cli.gate()
    assert found is not None and found.opened_at == cli.h.now
    assert [line["pair"] for line in _events(cli, "gate_opened")] == [lucille.name]
    # Shown, never kept: not in the audit log, nor in the gate's record.
    shown = cli.audit() + cli.dirs.gate_file.read_text()
    assert lucille.sas() not in shown and lucille.sas().replace("-", "") not in shown


def test_other_answers_are_asked_again(cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()

    def answers(n: int) -> str:
        # Typing the SAS itself is no answer either: the question is whether the two match.
        return ["maybe\n", lucille.sas() + "\n", "\n", " YES \n"][n - 1]

    with collecting(lucille):
        ran = cli.start("pair", code, stdin=_User(cli, answers)).wait()
    assert ran.rc == 0, ran.text
    asked_again = "type y if Lucille's page shows the same SAS, or n if it does not"
    assert ran.err.splitlines() == [asked_again] * 3
    confirmed = f"confirmed: pair {lucille.name} is confirmed on this computer"
    assert ran.out.splitlines()[-1] == confirmed


@pytest.mark.parametrize("no", ["n", "No"])
def test_n_at_the_prompt_forgets(no: str, cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    with collecting(lucille):
        ran = cli.start("pair", code, stdin=_User(cli, lambda _: no + "\n")).wait()
    assert ran.rc == 1
    assert ran.out.splitlines()[-1].startswith("sas_pending: ")
    assert ran.err == (
        f"rejected: pair {lucille.name} is forgotten on this computer; start again in Lucille. "
        "Lucille was told\n"
    )
    assert cli.held() == [] and cli.gate() is None
    assert [line["why"] for line in _events(cli, "forgotten")] == ["rejected"]
    with pytest.raises(pairing.PairingError) as ended:
        lucille.resolve()
    assert ended.value.state == "ended_by_peer"


def test_ctrl_c_at_the_prompt_keeps_the_pair(cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()

    def interrupt(_: int) -> str:
        raise KeyboardInterrupt

    with collecting(lucille):
        ran = cli.start("pair", code, stdin=_User(cli, interrupt)).wait()
    assert ran.rc == 1
    assert ran.err == (
        f"interrupted: pair {lucille.name} is kept; `lucille-host pair status` shows its SAS "
        "again, and `lucille-host pair confirm` confirms it\n"
    )
    assert cli.held() == [lucille.name]
    found = cli.gate()
    assert found is not None and found.shown_at is not None and found.opened_at is None
    assert cli.run("pair", "confirm").rc == 0


def test_y_past_the_deadline_forgets(cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()

    def late(_: int) -> str:
        cli.h.advance(gate.WINDOW + 1)
        return "y\n"

    with collecting(lucille):
        ran = cli.start("pair", code, stdin=_User(cli, late)).wait()
    assert ran.rc == 1
    assert ran.err.startswith("expired: the SAS was not confirmed in time"), ran.err
    assert "Lucille was told" in ran.err
    assert cli.held() == [] and cli.gate() is None


def test_pair_confirm_confirms_the_sas_shown(cli: HostCli) -> None:
    lucille = cli.lucille()
    ran = cli.paired(lucille)
    assert f"the SAS is {lucille.sas()}." in ran.out.splitlines()[-1]
    found = cli.gate()
    assert found is not None and found.opened_at is None
    # The `y` given later: it reads nothing, and compares nothing typed.
    ran = cli.run("pair", "confirm", stdin="00000000\n")
    assert ran.rc == 0, ran.text
    assert ran.out == f"confirmed: pair {lucille.name} is confirmed on this computer\n"
    found = cli.gate()
    assert found is not None and found.opened_at == cli.h.now
    assert [line["pair"] for line in _events(cli, "gate_opened")] == [lucille.name]
    ran = cli.run("pair", "confirm")
    assert ran.rc == 0 and ran.out == f"confirmed: pair {lucille.name} is already confirmed\n"
    assert cli.gate() == found


def test_pair_status_shows_the_sas_while_pending(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.interrupted(lucille.start())
    ran = cli.run("pair", "status")
    assert ran.rc == 0 and ran.out.startswith(f"waiting: pair {lucille.name}"), ran.text
    assert re.search(r"\d{4}-\d{4}", ran.text) is None
    assert lucille.collect().state == "paired"
    sas = lucille.sas()
    shown = [cli.run("pair", "status"), cli.run("pair", "status")]
    found = cli.gate()
    assert found is not None and found.deadline is not None
    for ran in shown:
        assert ran.rc == 0, ran.text
        assert ran.out == (
            f"sas_pending: pair {lucille.name}: the SAS is {sas}. If Lucille's page shows the "
            f"same, confirm it with `lucille-host pair confirm` before "
            f"{link.iso(found.deadline)}; if not, `lucille-host pair reject`\n"
        )
    assert len(_events(cli, "sas_shown")) == 1
    assert cli.run("pair", "confirm").rc == 0
    ran = cli.run("pair", "status")
    assert ran.out == f"confirmed: pair {lucille.name}\n"
    kept = cli.audit() + cli.dirs.gate_file.read_text()
    assert sas not in kept and sas.replace("-", "") not in kept


def test_confirm_before_the_sas_is_shown_is_refused(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.interrupted(lucille.start())
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1 and ran.err.startswith(f"waiting: pair {lucille.name}: "), ran.err
    assert lucille.collect().state == "paired"
    # `pair confirm` resolves, and so has the SAS; but this computer has not shown it, so there
    # is nothing the user can have compared: refused, and no deadline starts.
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1
    assert ran.err == (
        f"sas_pending: pair {lucille.name}: this computer has not shown the SAS yet, so there is "
        "nothing to confirm; `lucille-host pair status` shows it\n"
    )
    found = cli.gate()
    assert found is not None
    assert (found.shown_at, found.deadline, found.opened_at) == (None, None, None)
    assert lucille.sas() in cli.run("pair", "status").out
    ran = cli.run("pair", "confirm")
    assert ran.rc == 0, ran.text
    found = cli.gate()
    assert found is not None and found.opened_at is not None


def test_gate_deadline_from_when_the_sas_is_first_shown(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.interrupted(lucille.start())
    accepted = cli.h.now
    cli.h.advance(3600)
    assert lucille.collect().state == "paired"
    cli.h.advance(3600)
    # A resolve that brings the SAS, showing it nowhere, starts nothing.
    assert cli.run("pair", "confirm").rc == 1
    found = cli.gate()
    assert found is not None and found.deadline is None
    cli.h.advance(3600)
    first = cli.h.now
    assert lucille.sas() in cli.run("pair", "status").out
    found = cli.gate()
    assert found is not None
    assert found.created_at == accepted
    assert (found.shown_at, found.deadline) == (first, first + gate.WINDOW)
    # Showing it again keeps the deadline where the first showing set it.
    cli.h.advance(gate.WINDOW - 1)
    assert lucille.sas() in cli.run("pair", "status").out
    assert cli.gate() == found
    cli.h.advance(2)
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1
    assert ran.err.startswith("expired: the SAS was not confirmed in time"), ran.err
    assert cli.held() == [] and cli.gate() is None
    assert [line["why"] for line in _events(cli, "forgotten")] == ["deadline"]
    with pytest.raises(pairing.PairingError) as ended:
        lucille.resolve()
    assert ended.value.state == "ended_by_peer"


def test_pair_status_past_the_deadline_forgets(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.paired(lucille)
    cli.h.advance(gate.WINDOW + 1)
    ran = cli.run("pair", "status")
    assert ran.rc == 1 and ran.err.startswith("expired: the SAS was not confirmed in time"), ran.err
    assert lucille.sas() not in ran.text
    assert cli.held() == [] and cli.gate() is None


def test_confirm_after_code_expired(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.paired(lucille)
    # The code itself expired long ago; the pair, and its gate, did not.
    cli.h.advance(3 * 3600)
    ran = cli.run("pair", "confirm")
    assert ran.rc == 0, ran.text
    assert ran.out.startswith("confirmed: ")
    assert cli.run("pair", "status").out == f"confirmed: pair {lucille.name}\n"


def test_reject_forgets_and_says_whether_told(cli: HostCli) -> None:
    ran = cli.run("pair", "reject")
    assert ran.rc == 1 and ran.err.startswith("none: ")
    lucille = cli.lucille()
    cli.paired(lucille)
    cli.h.advance(3 * 3600)
    ran = cli.run("pair", "reject")
    assert ran.rc == 0, ran.text
    assert ran.out == (
        f"rejected: pair {lucille.name} is forgotten on this computer; Lucille was told\n"
    )
    assert cli.held() == [] and cli.gate() is None
    with pytest.raises(pairing.PairingError) as ended:
        lucille.resolve()
    assert ended.value.state == "ended_by_peer"

    # The hub out of reach: forgotten here all the same, and Lucille is not told.
    second = cli.lucille()
    cli.paired(second)

    class Down(cli.h.client_class()):  # type: ignore[misc]
        def _call(self, *_: Any, **__: Any) -> Any:
            raise httpx.ConnectError("the hub is out of reach")

    cli.client_class = Down
    ran = cli.run("pair", "reject")
    assert ran.rc == 0, ran.text
    assert "Lucille was not told" in ran.out
    assert cli.held() == [] and cli.gate() is None
    assert second.resolve().state == "paired"

    # A confirmed pair is not rejected: forgetting it is another verb's.
    cli.client_class = cli.h.client_class()
    third = cli.lucille()
    cli.paired(third)
    assert cli.run("pair", "confirm").rc == 0
    ran = cli.run("pair", "reject")
    assert ran.rc == 1 and ran.err.startswith("refused: "), ran.err
    assert cli.held() == [third.name]


def test_a_pair_with_no_gate_record_is_never_confirmed(cli: HostCli) -> None:
    lucille = cli.lucille()
    # The record goes while `pair` waits: with nowhere to note that it showed the SAS, it shows
    # none, and asks nothing.
    running = cli.start("pair", lucille.start(), stdin="y\n")
    until(lambda: cli.listens > 0)
    gate.remove(cli.dirs.gate_file)
    assert lucille.collect().state == "paired"
    ran = running.wait(15)
    assert ran.rc == 1 and ran.err.startswith("refused: ") and "no local gate record" in ran.err
    assert "sas_pending" not in ran.out and lucille.sas() not in ran.text
    ran = cli.run("pair", "status")
    assert ran.rc == 0 and "no local gate record" in ran.out
    assert lucille.sas() not in ran.text
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1 and ran.err.startswith("refused: ") and "no local gate record" in ran.err
    assert cli.gate() is None
    assert cli.run("pair", "reject").rc == 0
    assert cli.held() == []


# --- the policy on a new pair ------------------------------------------------------------------


def test_new_pair_starts_from_fresh_policy(cli: HostCli, tmp_path: Path) -> None:
    _enable_and_add_a_folder(tmp_path)
    assert cli.policy() != policy.fresh()
    ran = cli.paired(cli.lucille())
    assert "the policy is reset for the new pair" in ran.out
    assert cli.policy() == policy.fresh()
    lines = cli.run("ops", "list").out.splitlines()
    assert "host.describe  enabled" in lines
    assert [line for line in lines if " enabled" in line] == ["host.describe  enabled"]
    assert cli.run("roots", "list").out == ""


def test_previous_policy_archived_not_applied(cli: HostCli, tmp_path: Path) -> None:
    folder = _enable_and_add_a_folder(tmp_path)
    earlier = cli.policy()
    first = cli.lucille()
    cli.paired(first)
    assert policy.load(cli.dirs.previous_policy_file) == earlier
    assert cli.policy() == policy.fresh()
    # Pairing again from the fresh policy keeps the earlier one set aside.
    assert cli.run("pair", "reject").rc == 0
    ran = cli.paired(cli.lucille())
    assert "the policy is reset" not in ran.out
    assert policy.load(cli.dirs.previous_policy_file) == earlier
    assert cli.policy() == policy.fresh()
    assert str(folder) not in cli.dirs.policy_file.read_text()


def test_a_refused_or_failed_code_resets_nothing_it_should_not(
    cli: HostCli, tmp_path: Path
) -> None:
    _enable_and_add_a_folder(tmp_path)
    earlier = cli.policy()
    lucille = cli.lucille()
    code = lucille.start()
    assert cli.run("pair", _recode(code, token="t")).rc == 1
    assert cli.policy() == earlier
    # A code that fails at the hub was still a new pairing: the reset comes before the answer.
    cli.h.advance(2 * 3600)
    assert cli.run("pair", code).rc == 1
    assert cli.policy() == policy.fresh()
    assert policy.load(cli.dirs.previous_policy_file) == earlier


def test_policy_previous_lists_archive(cli: HostCli, tmp_path: Path) -> None:
    ran = cli.run("policy", "previous")
    assert ran.rc == 0 and ran.out.startswith("no earlier policy")
    folder = _enable_and_add_a_folder(tmp_path)
    cli.paired(cli.lucille())
    ran = cli.run("policy", "previous")
    assert ran.rc == 0
    assert ran.out.splitlines()[1:] == [
        "operations:",
        "  files.sha256",
        "  host.describe",
        "folders:",
        f"  docs  {folder}",
    ]
    # Read only: nothing it lists is enabled.
    assert cli.policy() == policy.fresh()
    cli.dirs.previous_policy_file.write_text("version = 2\n")
    ran = cli.run("policy", "previous")
    assert ran.rc == 1 and "invalid" in ran.err


def test_policy_archive(tmp_path: Path) -> None:
    current, previous = tmp_path / "policy.toml", tmp_path / "policy.previous.toml"
    assert policy.archive(current, previous) is False
    assert not current.exists() and not previous.exists()
    policy.save(current, policy.fresh())
    assert policy.archive(current, previous) is False
    assert not previous.exists()
    current.write_text("not toml [")
    assert policy.archive(current, previous) is True
    assert previous.read_text() == "not toml ["
    assert policy.load(current) == policy.fresh()


# --- footprint -----------------------------------------------------------------------------------


def test_pairing_spawns_nothing(cli: HostCli) -> None:
    lucille, second, third = cli.lucille(), cli.lucille(), cli.lucille()
    with _audit_hook.recording() as events:
        cli.paired(lucille)
        assert cli.run("pair", "status").rc == 0
        assert cli.run("pair", "reject").rc == 0
        code = second.start()
        with collecting(second):
            assert cli.run("pair", code, stdin="n\n").rc == 1
        code = third.start()
        with collecting(third):
            assert cli.run("pair", code, stdin="y\n").rc == 0
        assert cli.run("pair", "confirm").rc == 0
    assert _audit_hook.spawns(events) == []


def _files(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if not path.is_dir()}


@pytest.mark.parametrize("store_file", [False, True], ids=["keyring", "file"])
def test_no_write_under_home_switchboard(store_file: bool, cli: HostCli) -> None:
    before = _files(cli.env.home)
    options = ["--store-file"] if store_file else []
    lucille = cli.lucille()
    ran = cli.paired(lucille, *options)
    assert ("weaker" in ran.err) is store_file
    assert cli.run("pair", "status").rc == 0
    assert cli.run("pair", "reject").rc == 0
    cli.paired(cli.lucille(), *options)
    assert cli.run("pair", "confirm").rc == 0
    assert not (cli.env.home / ".switchboard").exists()
    written = _files(cli.env.home) - before
    assert written
    own = (cli.dirs.config, cli.dirs.state)
    assert all(any(path.is_relative_to(folder) for folder in own) for path in written), written
    assert bool(cli.env.keyring.items) is not store_file


def test_pairing_verbs_refuse_root_and_switchboard_settings(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = cli.lucille().start()
    monkeypatch.setenv("SWITCHBOARD_URL", "https://hub.example")
    pairing_verbs = (["pair", code], ["pair", "status"], ["pair", "confirm"], ["pair", "reject"])
    # So do `run`, `status` and `forget`, the other verbs that open the pair's store.
    others = (["run"], ["status"], ["forget"])
    for argv in (*pairing_verbs, *others):
        ran = cli.run(*argv, timeout=15)
        assert ran.rc == 1 and "SWITCHBOARD_URL is set" in ran.err
    monkeypatch.delenv("SWITCHBOARD_URL")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    for argv in (["pair", code], *others):
        ran = cli.run(*argv, timeout=15)
        assert ran.rc == 1 and "root" in ran.err
    assert cli.gate() is None and not cli.dirs.host_file.exists()
    # The verbs that touch no pair still run.
    assert cli.run("policy", "previous").rc == 0
    assert cli.run("ops", "list").rc == 0
    assert cli.run("log").rc == 0


# --- the pieces ----------------------------------------------------------------------------------


def test_outcome_of_every_pairing_state() -> None:
    assert {s: link.outcome(s) for s in ("already_redeemed", "aborted", "superseded")} == {
        "already_redeemed": "leaked",
        "aborted": "leaked",
        "superseded": "leaked",
    }
    for word in ("expired", "never_confirmed", "unsent", "wrong_hub"):
        assert link.outcome(word) == "expired"
    assert link.outcome("ended_by_peer") == link.outcome("ended") == "ended"
    assert link.outcome("unknown") == "none"
    for word in ("tampered", "upgrade", "store", "disabled", "pending", "name_taken", "error"):
        assert link.outcome(word) == "error"


def test_gate_answers_and_record(tmp_path: Path) -> None:
    for entry in ("y", "Y", "yes", " Yes \n", "YES"):
        assert gate.answer(entry) is True, entry
    for entry in ("n", "N", "no", " No \n"):
        assert gate.answer(entry) is False, entry
    for entry in ("", "\n", "ye", "yess", "nope", "ok", "1234-5678", "12345678", "y n", "ｙ"):
        assert gate.answer(entry) is None, entry
    record = gate.Gate(pair="p", hub=_switchboard.LUCILLE_HUB, created_at=10.0)
    shown = gate.shown(record, 100.0)
    assert (shown.shown_at, shown.deadline) == (100.0, 100.0 + gate.WINDOW)
    assert gate.shown(shown, 500.0) == shown
    assert not gate.past_deadline(record, 10.0**12)
    assert not gate.past_deadline(shown, 100.0 + gate.WINDOW)
    assert gate.past_deadline(shown, 100.0 + gate.WINDOW + 1)
    path = tmp_path / "pair.json"
    gate.save(path, shown)
    assert gate.load(path) == shown
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    data = json.loads(path.read_text())
    assert set(data) == {
        "v",
        "pair",
        "hub",
        "created_at",
        "shown_at",
        "deadline",
        "opened_at",
        "touched_at",
    }
    for bad in (
        {**data, "shown_at": None},
        {**data, "deadline": True},
        {**data, "tries": 0},
        {**{k: v for k, v in data.items() if k != "shown_at"}, "sas_at": 100.0},
        {**data, "shown_at": None, "deadline": None, "opened_at": 200.0},
        {**data, "extra": 1},
        {**data, "v": 2},
        [data],
    ):
        path.write_text(json.dumps(bad))
        with pytest.raises(Exception, match="not a lucille-host gate record"):
            gate.load(path)
    assert gate.remove(path) is True
    assert gate.remove(path) is False
    assert gate.load(path) is None


def test_dirs_name_the_new_files(home: Path) -> None:
    dirs = paths.dirs()
    assert dirs.gate_file == dirs.state / "pair.json"
    assert dirs.previous_policy_file == dirs.config / "policy.previous.toml"
