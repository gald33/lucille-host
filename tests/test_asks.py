"""decisions/0003, "Entry": Lucille asks, the user types it here. ``credential.ask``'s child
(``lucille_host.asks``) leaves the ask and waits; ``credential asks`` shows it and ``credential
answer`` answers it, saving the value bound to the service template's hosts; Lucille learns only
"saved" or "declined". And "Hardening": the service templates and ``credential set``'s warnings."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from lucille_host import asks, audit, cli, credentials, gmail, ops, paths, proxy, services
from lucille_host.jobs import Job, process
from tests._host import HostEnv, MakeContext

GMAIL_VALUE = "reader@example.org " + "abcd" + "efgh" + "ijkl" + "mnop"
TOKEN = "ghp" + "_" + "Ab1" * 12
REASON = "To read your receipts, I need a Gmail app password."
JOB_ID = "00000000-0000-4000-8000-000000000001"


class Clock:
    """A clock the test moves on, and a sleep that moves it."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self.on_sleep: list[Any] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in list(self.on_sleep):
            hook()


def _argv(state: Path, args: dict[str, Any]) -> list[str]:
    return ["--state", str(state), "--profile", "default", "--", ops.args_json(args)]


def _main(argv: list[str], monkeypatch: pytest.MonkeyPatch, stdin: str = "") -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(argv)


# --- the child ------------------------------------------------------------------------------


def test_an_answered_ask_tells_lucille_only_saved(tmp_path: Path) -> None:
    clock = Clock()
    seen: list[asks.Ask] = []

    def answer() -> None:
        waiting = asks.Asks(tmp_path, clock).waiting()
        if waiting and not seen:
            seen.append(waiting[0])
            asks.Asks(tmp_path, clock).answer(waiting[0].id, "saved")

    clock.on_sleep.append(answer)
    args = {"service": "gmail", "name": "default", "reason": REASON}
    code, out, err = asks.main(_argv(tmp_path, args), clock=clock, sleep=clock.sleep)
    assert (code, out, err) == (0, b'{"answer":"saved"}', b"")
    (ask,) = seen
    assert (ask.service, ask.name, ask.reason) == ("gmail", "default", REASON)
    # The hosts are the template's.
    assert ask.hosts == ("imap.gmail.com",)
    assert ask.until == ask.asked_at + asks.WAIT_S
    # Collected: it waits no more, and the file holds no value at all.
    assert asks.Asks(tmp_path, clock).waiting() == []
    assert GMAIL_VALUE not in (tmp_path / asks.FILE).read_text()


def test_an_ask_nobody_answers_is_declined_when_it_lapses(tmp_path: Path) -> None:
    clock = Clock()
    start = clock.now
    args = {"service": "github", "name": "personal"}
    code, out, _ = asks.main(_argv(tmp_path, args), clock=clock, sleep=clock.sleep)
    assert (code, out) == (0, b'{"answer":"declined"}')
    assert clock.now >= start + asks.WAIT_S
    assert asks.Asks(tmp_path, clock).waiting() == []


def test_a_declined_ask_is_declined(tmp_path: Path) -> None:
    clock = Clock()

    def decline() -> None:
        for ask in asks.Asks(tmp_path, clock).waiting():
            asks.Asks(tmp_path, clock).answer(ask.id, "declined")

    clock.on_sleep.append(decline)
    code, out, _ = asks.main(
        _argv(tmp_path, {"service": "gmail", "name": "default"}), clock=clock, sleep=clock.sleep
    )
    assert (code, out) == (0, b'{"answer":"declined"}')


def test_lucille_cannot_nag(tmp_path: Path) -> None:
    clock = Clock()
    store = asks.Asks(tmp_path, clock)
    first = store.make("gmail", "default", None)
    assert first is not None
    # Another ask while one waits is declined without being shown, whatever it asks for.
    assert store.make("github", "personal", None) is None
    assert [ask.id for ask in store.waiting()] == [first.id]
    store.answer(first.id, "declined")
    assert store.collect(first) == "declined"
    # The same credential again, within the gap: declined, not shown.
    clock.now += asks.GAP_S - 1
    assert store.make("gmail", "default", None) is None
    assert store.waiting() == []
    # Another credential may be asked for.
    other = store.make("github", "personal", None)
    assert other is not None
    assert store.collect(other) is None
    clock.now += asks.WAIT_S
    assert store.collect(other) == "declined"
    # Past the gap, the first may be asked for again.
    clock.now += 2
    assert store.make("gmail", "default", None) is not None


def test_an_ask_whose_child_died_lapses_and_counts_as_ended(tmp_path: Path) -> None:
    clock = Clock()
    store = asks.Asks(tmp_path, clock)
    assert store.make("gmail", "default", None) is not None
    # Killed, the child never collects it: past its time it waits no more, and no answer is taken.
    clock.now += asks.WAIT_S
    assert store.waiting() == []
    assert store.make("gmail", "default", None) is None
    clock.now += asks.GAP_S
    assert store.make("gmail", "default", None) is not None


def test_an_answer_after_the_ask_lapsed_is_refused(tmp_path: Path) -> None:
    clock = Clock()
    store = asks.Asks(tmp_path, clock)
    ask = store.make("gmail", "default", None)
    assert ask is not None
    clock.now += asks.WAIT_S
    with pytest.raises(asks.AskGone):
        store.answer(ask.id, "saved")
    with pytest.raises(asks.AskGone):
        store.get(ask.id)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        json.dumps({"v": 2, "open": {}}),
        json.dumps(
            {
                "v": 1,
                "open": {
                    "0123456789abcdef": {
                        "id": "0123456789abcdef",
                        "service": "gmail",
                        "name": "default",
                        "hosts": ["imap.example.com"],
                        "reason": None,
                        "asked_at": 1,
                        "until": 9e12,
                        "answer": None,
                    }
                },
            }
        ),
    ],
)
def test_a_file_this_module_would_not_write_holds_no_ask(tmp_path: Path, text: str) -> None:
    # An ask bound to hosts other than its template's, among them: never shown, never answered.
    (tmp_path / asks.FILE).write_text(text)
    assert asks.Asks(tmp_path).waiting() == []


@pytest.mark.parametrize(
    "args",
    [
        {"service": "gmail", "name": "default", "hosts": ["imap.example.com"]},
        {"service": "bank", "name": "default"},
        {"service": "gmail", "name": "Default"},
        {"service": "gmail"},
        {"service": "gmail", "name": "default", "reason": "a‮b"},
        {"service": "gmail", "name": "default", "reason": "line\nbreak"},
        {"service": "gmail", "name": "default", "reason": "zero​width"},
        {"service": "gmail", "name": "default", "reason": "   "},
        {"service": "gmail", "name": "default", "reason": "x" * 301},
    ],
)
def test_the_child_checks_its_arguments_again(tmp_path: Path, args: dict[str, Any]) -> None:
    assert not ops.args_valid(ops.OPERATIONS["credential.ask"], args)
    code, out, err = asks.main(_argv(tmp_path, args), sleep=lambda _: None)
    assert (code, out, err) == (6, b"", b"lucille-host ask: internal error\n")
    assert not (tmp_path / asks.FILE).exists()


def test_a_reason_in_another_script_is_taken() -> None:
    op = ops.OPERATIONS["credential.ask"]
    assert ops.args_valid(op, {"service": "gmail", "name": "default", "reason": "שלום, Gmail"})


def _child(*args: str) -> subprocess.CompletedProcess[bytes]:
    command = [sys.executable, "-I", "-B", "-W", "ignore", "-X", "utf8", "-m", "lucille_host.asks"]
    return subprocess.run(
        [*command, *args],
        capture_output=True,
        check=False,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


@pytest.mark.parametrize("args", [(), ("--help",), ("--state", "relative", "--profile", "x")])
def test_the_child_refuses_a_malformed_argument_list_with_one_line(args: tuple[str, ...]) -> None:
    done = _child(*args)
    assert (done.returncode, done.stdout, done.stderr) == (
        6,
        b"",
        b"lucille-host ask: internal error\n",
    )


def test_through_the_host_the_child_never_loads_the_credential_store(
    make_context: MakeContext, state_dir: Path
) -> None:
    # The real child, through jobs.process: it leaves the ask, the user declines it here, and the
    # reply carries the one word. The child imports neither credentials nor the network modules.
    ctx = make_context(enabled=["credential.ask"])
    job = Job(job_id=JOB_ID, op="credential.ask", args={"service": "gmail", "name": "default"})
    replies: list[bytes] = []
    worker = threading.Thread(target=lambda: replies.append(process(job, ctx)))
    worker.start()
    deadline = time.monotonic() + 30
    store = asks.Asks(state_dir)
    while not store.waiting() and time.monotonic() < deadline:
        time.sleep(0.05)
    (ask,) = store.waiting()
    assert ask.hosts == ("imap.gmail.com",)
    store.answer(ask.id, "declined")
    worker.join(30)
    reply = json.loads(replies[0])
    assert (reply["status"], reply["exit_code"]) == ("done", 0)
    assert json.loads(reply["stdout"]) == {"answer": "declined"}
    assert reply["stderr"] == ""


def test_the_child_imports_no_credential_store_and_no_network() -> None:
    code = (
        "import sys\n"
        "from lucille_host import asks\n"
        "bad = {'lucille_host.credentials', 'lucille_host.proxy', 'lucille_host.gmail', 'socket',"
        " 'ssl', 'keyring'} & set(sys.modules)\n"
        "print(sorted(bad))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code], capture_output=True, check=True, timeout=60
    )
    assert done.stdout == b"[]\n"


# --- credential asks and credential answer --------------------------------------------------


def _ask(host_env: HostEnv, service: str = "gmail", reason: str | None = REASON) -> asks.Ask:
    name = "default" if service == "gmail" else "personal"
    made = asks.Asks(paths.ensure_dir(host_env.dirs.state)).make(service, name, reason)
    assert made is not None
    return made


def _store(host_env: HostEnv) -> credentials.CredentialStore:
    return credentials.CredentialStore(host_env.dirs.state, host_env.dirs.profile)


def test_asks_shows_the_hosts_own_lines_first_and_lucilles_words_last(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ask = _ask(host_env)
    assert _main(["credential", "asks"], monkeypatch) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Lucille asks you to save a credential on this computer (ask {ask.id}):"
    assert "  sent only to: imap.gmail.com" in lines
    assert lines[-2] == f"Lucille says: {REASON}"
    assert lines[-1] == f"  answer it with: lucille-host credential answer {ask.id}"
    assert not any(line.startswith("Lucille says:") for line in lines[:-2])


def test_asks_with_none_waiting(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main(["credential", "asks"], monkeypatch) == 0
    said = capsys.readouterr()
    assert (said.out, said.err) == ("", "lucille-host: Lucille is asking for no credential\n")


def test_answer_saves_it_bound_to_the_templates_hosts_and_replaces_one_saved(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["credential", "set", "gmail", "default", "--host", "imap.example.com", "--value-stdin"]
    assert _main(argv, monkeypatch, "old value\n") == 0
    ask = _ask(host_env)
    capsys.readouterr()
    assert _main(["--json", "credential", "asks"], monkeypatch) == 0
    (shown,) = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert shown["replaces"] is True and shown["hosts"] == ["imap.gmail.com"]
    argv = ["--json", "credential", "answer", ask.id, "--value-stdin"]
    assert _main(argv, monkeypatch, GMAIL_VALUE + "\n") == 0
    said = capsys.readouterr()
    assert json.loads(said.out) == {
        "v": 1,
        "type": "credential_answer",
        "id": ask.id,
        "service": "gmail",
        "name": "default",
        "answer": "saved",
    }
    assert GMAIL_VALUE not in said.out + said.err
    saved = _store(host_env).saved("gmail", "default")
    assert saved is not None and saved.hosts == ("imap.gmail.com",)
    assert _store(host_env).value("gmail", "default") == GMAIL_VALUE
    # The child collects "saved"; the audit log names the answer, never the value.
    assert asks.Asks(host_env.dirs.state).collect(ask) == "saved"
    log = audit.AuditLog(host_env.dirs.state).path.read_text()
    line = json.loads(log.splitlines()[-1])
    assert (line["kind"], line["service"], line["name"], line["answer"]) == (
        "credential_answer",
        "gmail",
        "default",
        "saved",
    )
    assert GMAIL_VALUE not in log


def test_answer_refuses_a_value_not_in_the_templates_form_and_the_ask_waits(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An account's own password, typed by mistake: never saved through an ask.
    ask = _ask(host_env)
    argv = ["credential", "answer", ask.id, "--value-stdin"]
    assert _main(argv, monkeypatch, "reader@example.org MyAccountPassword1\n") == 1
    said = capsys.readouterr()
    assert said.err.startswith("lucille-host: the value is not the account's address")
    assert "MyAccountPassword1" not in said.out + said.err
    assert _store(host_env).saved("gmail", "default") is None
    assert asks.Asks(host_env.dirs.state).get(ask.id) == ask


def test_answer_decline_saves_nothing(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ask = _ask(host_env, "github")
    assert _main(["credential", "answer", ask.id, "--decline"], monkeypatch) == 0
    assert capsys.readouterr().out == (
        f"declined ask {ask.id} for github/personal; nothing was saved, and Lucille is told it "
        "was declined\n"
    )
    assert _store(host_env).names() == []
    assert asks.Asks(host_env.dirs.state).collect(ask) == "declined"


def test_answer_at_a_terminal_declines_anything_but_yes(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ask = _ask(host_env, "github")
    stdin = io.StringIO("y\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main(["credential", "answer", ask.id]) == 0
    said = capsys.readouterr()
    assert "Type yes to save it" in said.err
    assert said.out.startswith(f"declined ask {ask.id}")
    assert _store(host_env).names() == []


def test_answer_at_a_terminal_saves_after_yes(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ask = _ask(host_env, "github")
    stdin = io.StringIO("yes\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(cli, "_read_hidden", lambda prompt: TOKEN)
    assert cli.main(["credential", "answer", ask.id]) == 0
    said = capsys.readouterr()
    # The host's lines come before Lucille's words, on standard error, and the value is not shown.
    assert said.err.index("sent only to: api.github.com") < said.err.index("Lucille says:")
    assert TOKEN not in said.out + said.err
    assert _store(host_env).value("github", "personal") == TOKEN


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["0123456789abcdef", "--decline"], "no ask 0123456789abcdef waits for an answer"),
        (["not-an-id", "--decline"], "an ask's id is 16 digits of 0-9 and a-f"),
    ],
)
def test_answer_of_no_such_ask(
    host_env: HostEnv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    said: str,
) -> None:
    assert _main(["--json", "credential", "answer", *argv], monkeypatch) == 1
    error = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert error["code"] == "no_such_ask" and error["message"].startswith(said)


def test_with_json_answer_takes_decline_or_value_stdin(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ask = _ask(host_env)
    for argv in (
        ["--json", "credential", "answer", ask.id],
        ["credential", "answer", ask.id, "--decline", "--value-stdin"],
    ):
        with pytest.raises(SystemExit) as stopped:
            _main(argv, monkeypatch)
        assert stopped.value.code == 2
    assert asks.Asks(host_env.dirs.state).get(ask.id) == ask


# --- the service templates and credential set's warnings ("Hardening") ---------------------


def test_every_operation_that_reads_a_credential_is_a_read_of_one_template() -> None:
    reads = [op for template in services.SERVICES.values() for op in template.reads]
    assert len(reads) == len(set(reads))
    keyring_children = {
        name
        for name, op in ops.OPERATIONS.items()
        if op.env_copy is not None and op.runs_host_python
    }
    assert set(reads) == keyring_children
    for template in services.SERVICES.values():
        assert template.changes == ()
        assert template.make_at.startswith("https://")
    with pytest.raises(ValueError, match="needs a prompt"):
        services.Service(
            name="x",
            title="X",
            hosts=("api.example.com",),
            reads=(),
            value=re.compile("x"),
            value_words="x",
            make_at="https://example.com",
            changes=("x.send",),
        )


def test_the_templates_are_what_the_children_and_the_schema_use() -> None:
    assert proxy.TOKEN is services.GITHUB_TOKEN and proxy.HOST in services.SERVICES["github"].hosts
    assert gmail.VALUE is services.GMAIL_VALUE and gmail.HOST in services.SERVICES["gmail"].hosts
    pattern = ops.OPERATIONS["credential.ask"].params()["properties"]["service"]["pattern"]
    assert pattern == "^(" + "|".join(sorted(services.SERVICES)) + ")$"
    for template in services.SERVICES.values():
        for host in template.hosts:
            assert credentials.hosts_of([host]) == (host,)


@pytest.mark.parametrize(
    ("argv", "value", "warned"),
    [
        (["github", "personal", "--host", "api.github.com"], TOKEN, None),
        (["gmail", "default", "--host", "imap.gmail.com"], GMAIL_VALUE, None),
        (
            ["github", "personal", "--host", "api.github.com"],
            "my-account-password",
            "the value is not a GitHub token",
        ),
        (
            ["gmail", "default", "--host", "imap.example.com"],
            GMAIL_VALUE,
            "it is not bound to imap.gmail.com, so gmail.check",
        ),
        (
            ["bank", "main", "--host", "bank.example.com"],
            "hunter2",
            "nothing in this release uses a credential of the service bank",
        ),
    ],
)
def test_set_warns_of_a_credential_nothing_will_send(
    host_env: HostEnv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    value: str,
    warned: str | None,
) -> None:
    assert _main(["credential", "set", *argv, "--value-stdin"], monkeypatch, value + "\n") == 0
    err = capsys.readouterr().err
    if warned is None:
        assert err == ""
    else:
        assert err.startswith(f"lucille-host: warning: {warned}"), err
    assert value not in err
