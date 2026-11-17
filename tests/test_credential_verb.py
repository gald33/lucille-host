"""``lucille-host credential set``, ``list`` and ``remove``: the value is typed on this computer,
read from one line of stdin or at a prompt that does not echo it, never taken from the command
line and never shown; each credential is bound to the hosts given; the verb has no JSON form."""

from __future__ import annotations

import io
import os
import pty
import select
import sys
import termios
import threading
import time
import traceback
from pathlib import Path

import pytest
from keyring.backends.fail import Keyring as FailKeyring

from lucille_host import cli, credentials, store
from tests._host import HostEnv
from tests._keyring import MemoryKeyring, turned_off

SECRET = "correct-horse-battery-staple"


def _main(argv: list[str], monkeypatch: pytest.MonkeyPatch, stdin: str = "") -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(argv)


def _store(host_env: HostEnv) -> credentials.CredentialStore:
    return credentials.CredentialStore(host_env.dirs.state, host_env.dirs.profile)


def test_set_list_remove(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["credential", "set", "gmail", "app-password", "--host", "imap.gmail.com"]
    argv += ["--host", "SMTP.gmail.com", "--value-stdin"]
    assert _main(argv, monkeypatch, SECRET + "\n") == 0
    said = capsys.readouterr()
    assert said.out == (
        "saved gmail/app-password, to be sent only to imap.gmail.com, smtp.gmail.com. "
        "Nothing uses saved credentials yet.\n"
    )
    assert _store(host_env).value("gmail", "app-password") == SECRET

    assert _main(["credential", "list"], monkeypatch) == 0
    said = capsys.readouterr()
    lines = said.out.splitlines()
    assert len(lines) == 1, said.out
    assert lines[0].startswith(
        "gmail/app-password: sent only to imap.gmail.com, smtp.gmail.com; saved "
    )
    assert SECRET not in said.out + said.err

    argv = ["credential", "set", "gmail", "app-password", "--host", "imap.gmail.com"]
    # A credential saved already is replaced only with --replace, and nothing is read before the
    # refusal.
    assert _main([*argv, "--value-stdin"], monkeypatch, "another\n") == 1
    assert capsys.readouterr().err == (
        "lucille-host: credential gmail/app-password is saved already; nothing was changed. Pass "
        "--replace to replace its value, and its hosts with the ones given now\n"
    )
    assert sys.stdin.tell() == 0
    assert _store(host_env).value("gmail", "app-password") == SECRET
    assert _main([*argv, "--value-stdin", "--replace"], monkeypatch, "another\r\n") == 0
    assert capsys.readouterr().out.startswith("replaced gmail/app-password, to be sent only to ")
    assert _store(host_env).value("gmail", "app-password") == "another"
    assert _store(host_env).names()[0].hosts == ("imap.gmail.com",)

    assert _main(["credential", "remove", "gmail", "app-password"], monkeypatch) == 0
    assert capsys.readouterr().out == "removed gmail/app-password\n"
    assert _store(host_env).names() == []
    assert _main(["credential", "remove", "gmail", "app-password"], monkeypatch) == 1
    assert "no credential gmail/app-password is saved" in capsys.readouterr().err


def test_list_with_none_saved(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main(["credential", "list"], monkeypatch) == 0
    said = capsys.readouterr()
    assert said.out == ""
    assert "no credentials are saved on this computer" in said.err


def test_the_value_is_never_taken_from_the_command_line(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["credential", "set", "gmail", "x", SECRET, "--host", "imap.gmail.com"]
    with pytest.raises(SystemExit) as exited:
        _main(argv, monkeypatch)
    assert exited.value.code == 2
    assert _store(host_env).names() == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "credential", "list"],
        ["credential", "list", "--json"],
        ["credential", "--json", "list"],
        ["--json", "credential", "set", "gmail", "x", "--host", "a.example", "--value-stdin"],
    ],
)
def test_the_verb_has_no_json_form(
    host_env: HostEnv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        _main(argv, monkeypatch, SECRET + "\n")
    assert exited.value.code == 2
    assert '"code":"usage"' in capsys.readouterr().out
    assert _store(host_env).names() == []


def test_a_host_is_required(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        _main(["credential", "set", "gmail", "x", "--value-stdin"], monkeypatch, SECRET + "\n")
    assert exited.value.code == 2
    assert "--host" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["Gmail", "x", "--host", "imap.gmail.com"], "service must be"),
        (["gmail", "a/b", "--host", "imap.gmail.com"], "name must be"),
        (["gmail", "x", "--host", "localhost"], "is not a host name"),
        (["gmail", "x", "--host", "printer.local"], "names this computer or a local network"),
    ],
)
def test_names_and_hosts_are_refused_before_the_value_is_read(
    host_env: HostEnv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    said: str,
) -> None:
    stdin = io.StringIO(SECRET + "\n")
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main(["credential", "set", *argv, "--value-stdin"]) == 1
    err = capsys.readouterr().err
    assert said in err and SECRET not in err
    # Nothing was read: the user is not asked for a value a refusal throws away.
    assert stdin.tell() == 0
    assert _store(host_env).names() == []


@pytest.mark.parametrize("stdin", ["", "\n", "\r\n"])
def test_no_value_on_stdin_saves_nothing(
    host_env: HostEnv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: str,
) -> None:
    argv = ["credential", "set", "gmail", "x", "--host", "imap.gmail.com", "--value-stdin"]
    assert _main(argv, monkeypatch, stdin) == 1
    assert "no value was given on standard input; nothing was saved" in capsys.readouterr().err
    assert _store(host_env).names() == []


def test_only_the_first_line_of_stdin_is_the_value(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = ["credential", "set", "gmail", "x", "--host", "imap.gmail.com", "--value-stdin"]
    assert _main(argv, monkeypatch, "first\nsecond\n") == 0
    assert _store(host_env).value("gmail", "x") == "first"


def test_without_a_terminal_it_asks_for_value_stdin(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["credential", "set", "gmail", "x", "--host", "imap.gmail.com"]
    assert _main(argv, monkeypatch, SECRET + "\n") == 1
    assert "standard input is not a terminal" in capsys.readouterr().err
    assert _store(host_env).names() == []


class _Typed:
    """What the user types at each hidden prompt, in turn."""

    def __init__(self, *lines: str | type[BaseException]) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        line = self.lines.pop(0)
        if isinstance(line, type):
            raise line
        return line


def _at_a_terminal(monkeypatch: pytest.MonkeyPatch, typed: _Typed) -> None:
    stdin = io.StringIO("")
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(cli, "_read_hidden", typed)


def test_typed_twice_at_a_terminal(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    typed = _Typed(SECRET, SECRET)
    _at_a_terminal(monkeypatch, typed)
    assert cli.main(["credential", "set", "gmail", "x", "--host", "imap.gmail.com"]) == 0
    assert typed.prompts == ["value of gmail/x (not shown): ", "the same value again: "]
    assert _store(host_env).value("gmail", "x") == SECRET
    said = capsys.readouterr()
    assert SECRET not in said.out + said.err


@pytest.mark.parametrize(
    ("lines", "said"),
    [
        ((SECRET, "something else"), "the two values differ; nothing was saved"),
        (("",), "no value was typed; nothing was saved"),
        ((EOFError,), "nothing was saved"),
        ((SECRET, KeyboardInterrupt), "nothing was saved"),
    ],
)
def test_at_a_terminal_a_value_not_typed_the_same_twice_saves_nothing(
    host_env: HostEnv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lines: tuple[str | type[BaseException], ...],
    said: str,
) -> None:
    _at_a_terminal(monkeypatch, _Typed(*lines))
    assert cli.main(["credential", "set", "gmail", "x", "--host", "imap.gmail.com"]) == 1
    err = capsys.readouterr().err
    assert said in err and SECRET not in err
    assert _store(host_env).names() == []


def test_the_hidden_prompt_turns_the_terminals_echo_off_while_the_value_is_typed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A real terminal: the value is typed once the prompt has turned echo off, and the terminal
    # shows nothing of it; echo is on again afterwards.
    master, slave = pty.openpty()
    stdin = os.fdopen(slave, "r", closefd=True)
    monkeypatch.setattr("sys.stdin", stdin)

    def type_once_echo_is_off() -> None:
        deadline = time.monotonic() + 5
        while termios.tcgetattr(slave)[3] & termios.ECHO and time.monotonic() < deadline:
            time.sleep(0.01)
        os.write(master, SECRET.encode() + b"\n")

    typist = threading.Thread(target=type_once_echo_is_off)
    try:
        typist.start()
        assert cli._read_hidden("value: ") == SECRET
        typist.join(5)
        assert termios.tcgetattr(slave)[3] & termios.ECHO
        shown = b""
        while select.select([master], [], [], 0.2)[0]:
            chunk = os.read(master, 1024)
            if not chunk:
                break
            shown += chunk
        assert SECRET.encode() not in shown
        assert capsys.readouterr().err == "value: \n"
    finally:
        stdin.close()
        os.close(master)


def test_the_value_is_kept_in_the_credentials_item_alone(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring
) -> None:
    argv = ["credential", "set", "gmail", "x", "--host", "imap.gmail.com", "--value-stdin"]
    assert _main(argv, monkeypatch, SECRET + "\n") == 0
    holding = [key for key, text in memory_keyring.items.items() if SECRET in text]
    assert holding == [(credentials.SERVICE, host_env.dirs.profile)]
    for path in Path(host_env.home).rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path


def test_it_does_not_run_as_root(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert _main(["credential", "list"], monkeypatch) == 1
    assert "does not run as root" in capsys.readouterr().err


# --- the value appears nowhere but the keyring -------------------------------------------------

SENTINEL = "s3ntinel-VALUE-7f2c"
SET = ["credential", "set", "gmail", "x", "--host", "imap.gmail.com"]


class _SaysTheValue(MemoryKeyring):
    """A keyring whose write fails with an error that says what it was asked to write."""

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError(f"could not store {password!r}")


class _FailsPlainly(MemoryKeyring):
    """A keyring whose write fails with an error of its own that says nothing of the value."""

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("the keychain is busy")


def _unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    def call(run: object, verb: str) -> object:
        raise store.KeyringUnusable(f"the keyring could not be {verb} (stand-in)")

    monkeypatch.setattr(store, "keyring_call", call)


# Each: the command line, what stdin holds (SENTINEL stands for the value), whether stdin is a
# terminal (then the hidden prompt gives SENTINEL, twice), a set-up, and the exit expected (None:
# the error is raised out of main, as a bug's is).
_PATHS: dict[str, tuple[list[str], str, bool, str, int | None]] = {
    "set from stdin": ([*SET, "--value-stdin"], SENTINEL + "\n", False, "", 0),
    "set at a terminal": (SET, "", True, "", 0),
    "set over one saved, refused": ([*SET, "--value-stdin"], SENTINEL + "\n", False, "saved", 1),
    "set with --replace": (
        [*SET, "--value-stdin", "--replace"],
        SENTINEL + "\n",
        False,
        "saved",
        0,
    ),
    "set, bad name": (
        ["credential", "set", "Gmail", "x", "--host", "imap.gmail.com", "--value-stdin"],
        SENTINEL + "\n",
        False,
        "",
        1,
    ),
    "set, bad host": (
        ["credential", "set", "gmail", "x", "--host", "localhost", "--value-stdin"],
        SENTINEL + "\n",
        False,
        "",
        1,
    ),
    "set, value on the command line": ([*SET, SENTINEL], "", False, "", 2),
    "set, value as an action": (["credential", SENTINEL], "", False, "", 2),
    "set, value as --host": (
        ["credential", "set", "gmail", "x", "--host", SENTINEL, "--value-stdin"],
        "",
        False,
        "",
        1,
    ),
    "set with --json": (["--json", *SET, "--value-stdin"], SENTINEL + "\n", False, "", 2),
    "set with --store-file": (
        [*SET, "--value-stdin", "--store-file"],
        SENTINEL + "\n",
        False,
        "",
        2,
    ),
    "set, no terminal": (SET, SENTINEL + "\n", False, "", 1),
    "set, --value-stdin at a terminal": ([*SET, "--value-stdin"], "", True, "", 1),
    "set, a control character": ([*SET, "--value-stdin"], SENTINEL + "\x1b\n", False, "", 1),
    "set, no keyring": ([*SET, "--value-stdin"], SENTINEL + "\n", False, "no keyring", 1),
    "set, keyring off": ([*SET, "--value-stdin"], SENTINEL + "\n", False, "keyring off", 1),
    "set, keyring unusable": ([*SET, "--value-stdin"], SENTINEL + "\n", False, "unusable", 1),
    "set, keyring says the value": ([*SET, "--value-stdin"], SENTINEL + "\n", False, "says", 1),
    "set, keyring fails plainly": ([*SET, "--value-stdin"], SENTINEL + "\n", False, "plain", None),
    "list": (["credential", "list"], "", False, "saved", 0),
    "remove": (["credential", "remove", "gmail", "x"], "", False, "saved", 0),
    "remove, none saved": (["credential", "remove", "gmail", "x"], "", False, "", 1),
}


@pytest.mark.parametrize("path", list(_PATHS))
def test_the_value_appears_nowhere_but_the_keyring(
    path: str,
    host_env: HostEnv,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, stdin_text, at_terminal, setup, expected = _PATHS[path]
    keyring = host_env.keyring
    if setup == "saved":
        _store(host_env).save("gmail", "x", SENTINEL, ("imap.gmail.com",))
    elif setup == "no keyring":
        monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring())
    elif setup == "keyring off":
        turned_off(monkeypatch, home, "variable-null")
    elif setup == "unusable":
        _unusable(monkeypatch)
    elif setup in ("says", "plain"):
        keyring = _SaysTheValue() if setup == "says" else _FailsPlainly()
        monkeypatch.setattr("keyring.core._keyring_backend", keyring)
    stdin = io.StringIO(stdin_text)
    monkeypatch.setattr(stdin, "isatty", lambda: at_terminal)
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(cli, "_read_hidden", _Typed(SENTINEL, SENTINEL))
    raised: BaseException | None = None
    code: int | None
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # a bug's error, which Python prints with its traceback
        raised, code = exc, None
    said = capsys.readouterr()
    assert code == expected, said.err
    assert SENTINEL not in said.out and SENTINEL not in said.err, said
    if raised is not None:
        shown = "".join(traceback.format_exception(raised))
        assert SENTINEL not in shown, shown
    # Nor in any file the host wrote, logs among them, nor in what may be shown of a credential.
    for each in home.rglob("*"):
        if each.is_file():
            assert SENTINEL.encode() not in each.read_bytes(), each
    if setup in ("", "saved"):
        creds = _store(host_env)
        assert SENTINEL not in repr(creds.names()) + repr(creds.saved("gmail", "x"))
        assert SENTINEL not in repr(creds)
        for item, text in keyring.items.items():
            if SENTINEL in text:
                assert item == (credentials.SERVICE, host_env.dirs.profile)


def test_a_keyring_error_that_says_the_value_is_not_shown(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("keyring.core._keyring_backend", _SaysTheValue())
    assert _main([*SET, "--value-stdin"], monkeypatch, SENTINEL + "\n") == 1
    assert capsys.readouterr().err == (
        "lucille-host: the keyring could not be written (RuntimeError); its message is not "
        "shown, since it holds the value. Nothing may have been saved\n"
    )


def test_the_stores_refusals_surface_as_they_are(
    host_env: HostEnv,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No file fallback: a keyring the host refuses, or one it cannot use, is the end of it.
    for verb in (["list"], ["remove", "gmail", "x"], ["set", *SET[2:], "--value-stdin"]):
        monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring())
        assert _main(["credential", *verb], monkeypatch, SENTINEL + "\n") == 1
        err = capsys.readouterr().err
        assert err.endswith(f", {credentials.REFUSAL}\n"), err
        assert "file" not in err
    turned_off(monkeypatch, home, "variable-null")
    assert _main(["credential", "list"], monkeypatch) == 1
    assert capsys.readouterr().err == (
        f"lucille-host: the keyring backend here (Keyring) stores nothing, {credentials.REFUSAL}\n"
    )
    # Nothing was kept in a file instead: the store's lock is the only file it made.
    made = [path.name for path in home.rglob("*") if path.is_file() and "credential" in path.name]
    assert set(made) <= {credentials.LOCK}, made
    for path in home.rglob("*"):
        if path.is_file():
            assert SENTINEL.encode() not in path.read_bytes(), path


def test_an_unusable_keyring_surfaces_as_it_is(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _unusable(monkeypatch)
    for verb in (["list"], ["remove", "gmail", "x"], ["set", *SET[2:], "--value-stdin"]):
        assert _main(["credential", *verb], monkeypatch, SENTINEL + "\n") == 1
        assert capsys.readouterr().err == "lucille-host: the keyring could not be read (stand-in)\n"


def test_removing_one_credential_leaves_the_others(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = _store(host_env)
    creds.save("gmail", "x", "one", ("imap.gmail.com",))
    creds.save("gmail", "y", "two", ("smtp.gmail.com",))
    creds.save("bank", "x", "three", ("bank.example.com",))
    assert _main(["credential", "remove", "gmail", "x"], monkeypatch) == 0
    assert [(c.service, c.name, c.hosts) for c in creds.names()] == [
        ("bank", "x", ("bank.example.com",)),
        ("gmail", "y", ("smtp.gmail.com",)),
    ]
    assert (creds.value("gmail", "y"), creds.value("bank", "x")) == ("two", "three")
    assert _main(["credential", "remove", "gmail", "x"], monkeypatch) == 1
    assert len(creds.names()) == 2


def test_replace_takes_the_hosts_given_now(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(host_env).save("gmail", "x", "one", ("imap.gmail.com", "smtp.gmail.com"))
    argv = ["credential", "set", "gmail", "x", "--host", "pop.gmail.com", "--replace"]
    assert _main([*argv, "--value-stdin"], monkeypatch, "two\n") == 0
    [saved] = _store(host_env).names()
    assert saved.hosts == ("pop.gmail.com",)
    assert _store(host_env).value("gmail", "x") == "two"


def test_replace_with_none_saved_saves_it(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main([*SET, "--value-stdin", "--replace"], monkeypatch, "one\n") == 0
    assert capsys.readouterr().out.startswith("saved gmail/x, ")


def test_one_saved_meanwhile_is_not_replaced_without_replace(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Another command saves the same name between the check before the value is asked for and
    # the write: the write, under the store's lock, still refuses.
    def typed(prompt: str) -> str:
        if not _store(host_env).names():
            _store(host_env).save("gmail", "x", "theirs", ("imap.gmail.com",))
        return "mine"

    _at_a_terminal(monkeypatch, _Typed(SENTINEL, SENTINEL))
    monkeypatch.setattr(cli, "_read_hidden", typed)
    assert cli.main(SET) == 1
    assert "credential gmail/x is saved already; nothing was changed" in capsys.readouterr().err
    assert _store(host_env).value("gmail", "x") == "theirs"


@pytest.mark.parametrize("value", ["a\tb", "a\x1bb", "a\x7fb", "a\rb"])
def test_a_value_with_a_control_character_is_refused(
    host_env: HostEnv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    assert _main([*SET, "--value-stdin"], monkeypatch, value + "\n") == 1
    assert "the value holds a control character; nothing was saved" in capsys.readouterr().err
    assert _store(host_env).names() == []


def test_value_stdin_at_a_terminal_is_refused_before_anything_is_read(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin = io.StringIO(SENTINEL + "\n")
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main([*SET, "--value-stdin"]) == 1
    assert "where the value would show as it is typed" in capsys.readouterr().err
    assert stdin.tell() == 0
    assert _store(host_env).names() == []


def test_a_usage_error_of_another_verb_still_says_what_argparse_says(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        _main(["roots", "remove", "a", "unexpected-word"], monkeypatch)
    assert "unrecognized arguments: unexpected-word" in capsys.readouterr().err
