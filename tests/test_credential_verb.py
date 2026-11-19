"""``lucille-host credential set``, ``list`` and ``remove``: the value is typed on this computer,
read from one line of stdin or at a prompt that does not echo it, never taken from the command
line and never shown; each credential is bound to the hosts given; the verb has no JSON form."""

from __future__ import annotations

import io
import json
import os
import pty
import select
import subprocess
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
        "Only github.read uses saved credentials: those of the service github, bound to "
        "api.github.com.\n"
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

# Values that each escape differently: in JSON, in repr(), in an error's text. Each holds a token
# of its own, letters and digits that no escaping changes, so that a value is found in what was
# shown whatever form it was shown in.
SECRETS: dict[str, str] = {
    "quote": 'vQ7zx1"q',
    "backslash": "vQ7zx2\\b",
    "accents-cjk": "vQ7zx3é中",
    "emoji": "vQ7zx4😀",
}
SENTINEL = SECRETS["quote"]
# Another credential stored beside the one each path works on, with a tab too: it must not show
# on any path, error paths for the first among them.
OTHER = 'oW3kp9"\\\té中😀'
TOKENS = ("vQ7zx", "oW3kp9")
SET = ["credential", "set", "gmail", "x", "--host", "imap.gmail.com"]


def _shown(text: str) -> list[str]:
    """The tokens of the values above that ``text`` holds, in any form."""
    return [token for token in TOKENS if token in text]


def _item(entries: dict[str, str]) -> str:
    """The credentials item holding ``entries``, ``service/name`` to value, as the store writes
    it."""
    doc = {"v": 1, "credentials": {}}
    for key, value in entries.items():
        service, name = key.split("/")
        doc["credentials"][key] = {
            "service": service,
            "name": name,
            "value": value,
            "hosts": [f"{service}.example.com"],
            "saved_at": 1.0,
        }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


class _SaysTheValue(MemoryKeyring):
    """A keyring whose write fails with an error that says what it was asked to write: every
    credential in the item."""

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError(f"could not store {password!r}")


class _SaysTheItemOnRead(MemoryKeyring):
    """A keyring whose read fails with an error that says what the item holds."""

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError(f"could not read {self.items.get((service, username))!r}")


class _NotesTheValue(MemoryKeyring):
    """A keyring whose write fails with an error that says nothing, but carries what it was
    asked to write in a note (``add_note``), which a traceback prints."""

    def set_password(self, service: str, username: str, password: str) -> None:
        error = RuntimeError("the keychain is busy")
        error.add_note(password)
        raise error


class _FailsPlainly(MemoryKeyring):
    """A keyring whose write fails with an error of its own that says nothing of the value."""

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("the keychain is busy")


def _unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    def call(run: object, verb: str) -> object:
        raise store.KeyringUnusable(f"the keyring could not be {verb} (stand-in)")

    monkeypatch.setattr(store, "keyring_call", call)


def _a_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug in the store's write: an error that says the value, with a note, raised from
    another that says it too."""

    def save(
        self: object, service: str, name: str, value: str, *rest: object, **kw: object
    ) -> None:
        try:
            raise KeyError(value)
        except KeyError as inner:
            error = ValueError(f"cannot save {value!r}")
            error.add_note(value)
            raise error from inner

    monkeypatch.setattr(credentials.CredentialStore, "save", save)


# Each: the command line, what stdin holds (VALUE stands for the value), whether stdin is a
# terminal (then the hidden prompt gives the value, twice), a set-up, and the exit expected (None:
# an error is raised out of main, as a bug's is, which Python prints with its traceback).
V = "\x00VALUE\x00"
_PATHS: dict[str, tuple[list[str], str, bool, str, int | None]] = {
    "set from stdin": ([*SET, "--value-stdin"], V + "\n", False, "", 0),
    "set at a terminal": (SET, "", True, "", 0),
    "set over one saved, refused": ([*SET, "--value-stdin"], V + "\n", False, "saved", 1),
    "set with --replace": ([*SET, "--value-stdin", "--replace"], V + "\n", False, "saved", 0),
    "set, bad name": (
        ["credential", "set", "Gmail", "x", "--host", "imap.gmail.com", "--value-stdin"],
        V + "\n",
        False,
        "",
        1,
    ),
    "set, bad host": (
        ["credential", "set", "gmail", "x", "--host", "localhost", "--value-stdin"],
        V + "\n",
        False,
        "",
        1,
    ),
    "set, value on the command line": ([*SET, V], "", False, "", 2),
    "set, value as an action": (["credential", V], "", False, "", 2),
    "set, value as the service": (
        ["credential", "set", V, "x", "--host", "a.example.com"],
        "",
        False,
        "",
        1,
    ),
    "set, value as --host": (
        ["credential", "set", "gmail", "x", "--host", V, "--value-stdin"],
        "",
        False,
        "",
        1,
    ),
    "set, value as --host=": (["credential", "set", "gmail", "x", f"--host={V}"], "", True, "", 1),
    "set, value as an option": ([*SET, f"--{V}"], "", False, "", 2),
    "set with --json": (["--json", *SET, "--value-stdin"], V + "\n", False, "", 2),
    "set with --store-file": ([*SET, "--value-stdin", "--store-file"], V + "\n", False, "", 2),
    "set, no terminal": (SET, V + "\n", False, "", 1),
    "set, --value-stdin at a terminal": ([*SET, "--value-stdin"], "", True, "", 1),
    "set, a control character": ([*SET, "--value-stdin"], V + "\x1b\n", False, "", 1),
    "set, no keyring": ([*SET, "--value-stdin"], V + "\n", False, "no keyring", 1),
    "set, keyring off": ([*SET, "--value-stdin"], V + "\n", False, "keyring off", 1),
    "set, keyring unusable": ([*SET, "--value-stdin"], V + "\n", False, "unusable", 1),
    "set, keyring says the item": ([*SET, "--value-stdin"], V + "\n", False, "says", 1),
    "set, keyring notes the item": ([*SET, "--value-stdin"], V + "\n", False, "notes", 1),
    "set, keyring fails plainly": ([*SET, "--value-stdin"], V + "\n", False, "plain", 1),
    "set, keyring says the item on read": ([*SET, "--value-stdin"], V + "\n", False, "reads", 1),
    "set, a bug in the store": ([*SET, "--value-stdin"], V + "\n", False, "bug", None),
    "list": (["credential", "list"], "", False, "saved", 0),
    "list, keyring says the item on read": (["credential", "list"], "", False, "reads", 1),
    "remove": (["credential", "remove", "gmail", "x"], "", False, "saved", 0),
    "remove, none saved": (["credential", "remove", "gmail", "x"], "", False, "", 1),
    "remove, keyring says the item": (["credential", "remove", "gmail", "x"], "", False, "says", 1),
    "remove, keyring says the item on read": (
        ["credential", "remove", "gmail", "x"],
        "",
        False,
        "reads",
        1,
    ),
}


@pytest.mark.parametrize("secret", list(SECRETS))
@pytest.mark.parametrize("path", list(_PATHS))
def test_the_value_appears_nowhere_but_the_keyring(
    path: str,
    secret: str,
    host_env: HostEnv,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = SECRETS[secret]
    template, stdin_template, at_terminal, setup, expected = _PATHS[path]
    argv = [word.replace(V, value) for word in template]
    profile = host_env.dirs.profile
    item = (credentials.SERVICE, profile)
    stored = {"other/y": OTHER}
    if setup in ("saved", "says", "reads"):
        stored["gmail/x"] = value
    keyring: MemoryKeyring = host_env.keyring
    if setup == "says":
        keyring = _SaysTheValue()
    elif setup == "reads":
        keyring = _SaysTheItemOnRead()
    elif setup == "notes":
        keyring = _NotesTheValue()
    elif setup == "plain":
        keyring = _FailsPlainly()
    keyring.items[item] = _item(stored)
    monkeypatch.setattr("keyring.core._keyring_backend", keyring)
    if setup == "no keyring":
        monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring())
    elif setup == "keyring off":
        turned_off(monkeypatch, home, "variable-null")
    elif setup == "unusable":
        _unusable(monkeypatch)
    elif setup == "bug":
        _a_bug(monkeypatch)
    stdin = io.StringIO(stdin_template.replace(V, value))
    monkeypatch.setattr(stdin, "isatty", lambda: at_terminal)
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(cli, "_read_hidden", _Typed(value, value))
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
    assert _shown(said.out) == [] and _shown(said.err) == [], said
    if raised is not None:
        # What Python prints for it: the traceback, with every cause, context and note.
        shown = "".join(traceback.format_exception(raised))
        assert _shown(shown) == [], shown
    # Nor in any file the host wrote, logs among them.
    for each in home.rglob("*"):
        if each.is_file():
            assert _shown(each.read_bytes().decode("utf-8", "replace")) == [], each
    if setup in ("", "saved"):
        # Nor in what may be shown of a credential, nor in any keyring item but the credentials'.
        creds = _store(host_env)
        assert _shown(repr(creds.names()) + repr(creds.saved("gmail", "x")) + repr(creds)) == []
        for each, text in keyring.items.items():
            if _shown(text):
                assert each == item


def test_a_keyring_error_that_says_the_item_is_not_shown(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The write's error says the whole item, the other credential's value among it; a fixed
    # refusal takes its place, whatever form the value is in there.
    keyring = _SaysTheValue()
    keyring.items[(credentials.SERVICE, host_env.dirs.profile)] = _item({"other/y": OTHER})
    monkeypatch.setattr("keyring.core._keyring_backend", keyring)
    assert _main([*SET, "--value-stdin"], monkeypatch, SECRETS["backslash"] + "\n") == 1
    assert capsys.readouterr().err == (
        "lucille-host: the keyring refused to write the credentials item; its error is not "
        "shown, since it may hold a credential's value\n"
    )


def test_a_keyring_error_with_the_value_in_a_note_shows_nothing_of_it(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("keyring.core._keyring_backend", _NotesTheValue())
    assert _main([*SET, "--value-stdin"], monkeypatch, SECRETS["emoji"] + "\n") == 1
    said = capsys.readouterr()
    assert _shown(said.out + said.err) == [], said
    assert said.err == (
        "lucille-host: the keyring refused to write the credentials item; its error is not "
        "shown, since it may hold a credential's value\n"
    )


def test_the_keyrings_refusal_carries_none_of_its_error() -> None:
    # Neither as a cause nor as the context it was raised in, so no traceback prints it.
    error = credentials._refused("write")
    assert error.__cause__ is None and error.__context__ is None
    assert not getattr(error, "__notes__", None)


def test_the_keyrings_refusal_is_raised_with_no_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _NotesTheValue()
    monkeypatch.setattr("keyring.core._keyring_backend", keyring)
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    with pytest.raises(credentials.KeyringRefused) as info:
        creds.save("gmail", "x", SECRETS["quote"], ("imap.gmail.com",))
    raised = info.value
    assert raised.__cause__ is None and raised.__context__ is None
    assert _shown("".join(traceback.format_exception(raised))) == []


def test_a_bug_on_the_credential_path_is_shown_without_its_error(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _a_bug(monkeypatch)
    with pytest.raises(RuntimeError) as info:
        _main([*SET, "--value-stdin"], monkeypatch, SECRETS["accents-cjk"] + "\n")
    raised = info.value
    assert str(raised) == (
        "an unexpected error (ValueError) in the credential verb; its message is not shown, "
        "since it may hold a credential's value"
    )
    assert raised.__cause__ is None and raised.__context__ is None
    assert not getattr(raised, "__notes__", None)
    assert _shown("".join(traceback.format_exception(raised))) == []
    assert '"code":"internal"' not in capsys.readouterr().out  # text mode: no object


def test_an_unknown_action_is_named_as_such_and_not_repeated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        _main(["credential", SECRETS["backslash"]], monkeypatch)
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("usage: lucille-host credential "), err
    assert err.endswith(
        "lucille-host credential: error: unknown credential subcommand: it is set, list or remove\n"
    ), err
    assert _shown(err) == []


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["credential", "set", "gmail", "--host", "a.example.com"], "required: name"),
        (["credential", "set", "gmail", "x"], "required: --host"),
        (["credential", "set", "gmail", "x", "--host"], "argument --host: expected one argument"),
        (["credential"], "required: ACTION"),
    ],
)
def test_usage_errors_that_repeat_nothing_typed_are_argparses_own(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    said: str,
) -> None:
    with pytest.raises(SystemExit):
        _main(argv, monkeypatch)
    assert said in capsys.readouterr().err


def test_an_undecodable_value_on_stdin_is_refused_without_its_bytes(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Standard input in a strict UTF-8 locale, and a value that is not UTF-8: a refusal, not the
    # decoder's error, which names the bytes it could not decode.
    stdin = io.TextIOWrapper(io.BytesIO(b"vQ7zx5\xff\xfe\n"), encoding="utf-8", errors="strict")
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main([*SET, "--value-stdin"]) == 1
    err = capsys.readouterr().err
    assert err == (
        "lucille-host: the value is not text in this terminal's encoding; nothing was saved\n"
    )
    assert _store(host_env).names() == []


def test_an_undecodable_line_at_the_hidden_prompt_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = io.TextIOWrapper(io.BytesIO(b"\xff\n"), encoding="utf-8", errors="strict")
    monkeypatch.setattr("sys.stdin", stdin)
    with pytest.raises(cli._Refused) as info:
        cli._read_value_line()
    assert info.value.code == "bad_value"
    assert info.value.__context__ is None


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


# --- the second review ----------------------------------------------------------------------

W = SECRETS["backslash"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--", "credential", W],
        ["--", "credential", "list", W],
        ["--", "credential", "remove", "gmail", "x", W],
        ["--", "credential", "set", "gmail", "x", "--host", "a.example.com", W],
        ["--json", "--", "credential", W],
        ["--json", "--", "credential", "list", W],
        ["credential", "--", W],
        ["credential", "list", "--", W],
        ["credential", "list", W],
        ["credential", "remove", "gmail", "x", W],
        [f"--{W}", "credential", "list"],
        ["--profile", "default", "--", "credential", W],
    ],
)
def test_a_usage_error_of_a_credential_line_repeats_nothing_typed_with_or_without_dashes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    # Whichever parser raises it, and wherever `--` puts the verb: argparse's own message would
    # repeat the word, quoted (an invalid choice) or as it is (unrecognized arguments).
    with pytest.raises(SystemExit) as exited:
        _main(argv, monkeypatch)
    assert exited.value.code == 2
    said = capsys.readouterr()
    assert _shown(said.out + said.err) == [], said


def test_another_verbs_unrecognized_words_are_said_as_argparse_says_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        _main(["roots", "remove", "a", "unexpected-word"], monkeypatch)
    assert capsys.readouterr().err.endswith(
        "lucille-host: error: unrecognized arguments: unexpected-word\n"
    )


def test_a_lone_surrogate_from_stdin_is_refused(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # stdin as Python opens it under the C or POSIX locale, or with PYTHONUTF8=1: undecodable
    # bytes come out as lone surrogates, not an error.
    stdin = io.TextIOWrapper(
        io.BytesIO(b"vQ7zx6\xff\xfe\n"), encoding="utf-8", errors="surrogateescape"
    )
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main([*SET, "--value-stdin"]) == 1
    err = capsys.readouterr().err
    assert err == (
        "lucille-host: the value is not text in this terminal's encoding; nothing was saved\n"
    )
    assert _store(host_env).names() == []


def test_a_lone_surrogate_typed_at_the_prompt_is_refused(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _at_a_terminal(monkeypatch, _Typed("vQ7zx7\udcff", "vQ7zx7\udcff"))
    assert cli.main(SET) == 1
    err = capsys.readouterr().err
    assert "the value is not text in this terminal's encoding; nothing was saved" in err
    assert _shown(err) == []
    assert _store(host_env).names() == []


# The command line in a fresh interpreter, with stdin as the environment makes it, a keyring in
# memory and a user id that is not root; it prints whether anything was stored, and the exit.
_IN_A_LOCALE = """
import sys
import keyring.core
from lucille_host import cli, credentials, store
from tests._keyring import MemoryKeyring
backend = MemoryKeyring()
keyring.core._keyring_backend = backend
store._system_backend = lambda: backend
store._known = lambda each: each is backend
import os
os.geteuid = lambda: 1000
print("stdin errors:", sys.stdin.errors, file=sys.stderr)
code = cli.main(["credential", "set", "gmail", "x", "--host", "imap.gmail.com", "--value-stdin"])
print("stored:", bool(backend.items), "exit:", code)
"""


@pytest.mark.parametrize("environment", [{"LC_ALL": "C"}, {"PYTHONUTF8": "1"}])
def test_invalid_utf8_on_stdin_under_a_c_locale_or_utf8_mode_is_refused(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LC_", "LANG", "PYTHON", "SWITCHBOARD_"))
    }
    env.update(environment, HOME=str(tmp_path), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_STATE_HOME", None)
    done = subprocess.run(
        [sys.executable, "-c", _IN_A_LOCALE],
        input=b"vQ7zx8\xff\xfe\n",
        capture_output=True,
        env=env,
        check=False,
        timeout=60,
    )
    out, err = done.stdout.decode(), done.stderr.decode("utf-8", "replace")
    assert "stdin errors: surrogateescape" in err, err
    assert out.strip() == "stored: False exit: 1", (out, err)
    assert "the value is not text in this terminal's encoding; nothing was saved" in err, err
    assert "vQ7zx8" not in out + err


class _StopsWhileHandlingAnError(MemoryKeyring):
    """A keyring whose read fails with an error that says the item; the store, while it handles
    that error, is interrupted (Ctrl-C)."""

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError(f"could not read {self.items.get((service, username))!r}")


def test_ctrl_c_while_the_store_handles_a_keyrings_error_does_not_carry_it(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keyring = _StopsWhileHandlingAnError()
    keyring.items[(credentials.SERVICE, host_env.dirs.profile)] = _item({"other/y": OTHER})
    monkeypatch.setattr("keyring.core._keyring_backend", keyring)

    def interrupted(exc: BaseException, verb: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "keyring_failure", interrupted)
    with pytest.raises(KeyboardInterrupt) as info:
        _main(["credential", "list"], monkeypatch)
    raised = info.value
    assert raised.__cause__ is None and raised.__context__ is None
    assert _shown("".join(traceback.format_exception(raised))) == []
    said = capsys.readouterr()
    assert _shown(said.out + said.err) == []


def test_the_keyrings_refusal_on_a_read_carries_none_of_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _SaysTheItemOnRead()
    keyring.items[(credentials.SERVICE, "default")] = _item({"other/y": OTHER})
    monkeypatch.setattr("keyring.core._keyring_backend", keyring)
    creds = credentials.CredentialStore(tmp_path / "state", "default")
    with pytest.raises(credentials.KeyringRefused) as info:
        creds.names()
    raised = info.value
    assert raised.__cause__ is None and raised.__context__ is None
    assert not getattr(raised, "__notes__", None)
    assert str(raised) == (
        "the keyring refused to read the credentials item; its error is not shown, since it "
        "may hold a credential's value"
    )
    assert _shown("".join(traceback.format_exception(raised))) == []


def test_a_usage_error_raised_by_the_credential_verbs_parsers_is_gated_by_its_parser() -> None:
    # Each of the two gates holds alone: here the parser that raised it, with no "credential" in
    # the words looked at.
    parser = cli.build_parser()
    for raised_by in parser.credential_parsers:
        said = cli._usage_said(parser, raised_by, [], f"unrecognized arguments: {W}")
        assert _shown(said) == [], said
    said = cli._usage_said(
        parser, parser.forms["credential"], [], f"argument ACTION: invalid choice: {W!r}"
    )
    assert said == "unknown credential subcommand: it is set, list or remove"


def test_a_usage_error_of_a_line_that_names_the_verb_is_gated_by_its_words() -> None:
    # The other gate alone: the top parser raised it, on a line that names the verb anywhere.
    parser = cli.build_parser()
    said = cli._usage_said(parser, parser, ["--", "credential", W], f"unrecognized arguments: {W}")
    assert _shown(said) == [], said
    other = cli._usage_said(parser, parser, ["roots", W], f"unrecognized arguments: {W}")
    assert other == f"unrecognized arguments: {W}"
