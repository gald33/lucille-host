"""The ``lucille-host`` command.

With no arguments, or with ``--version``, it prints ``lucille-host <version>`` and exits 0. The
verbs:

- ``pair begin`` (or ``pair`` alone): Connect from Lucille's chat (decisions/0002 §11.4). It reads
  the chat service and the user's own username there, shows a connect code to send Lucille, and
  waits for Lucille's signed answer in the request's drop room (``connecting.py``). It answers the
  pairing code that answer carries only once it has passed every check, its note naming this
  request's nonce among them, then waits for Lucille to collect the answer, shows the pair's SAS,
  and asks for ``y`` if Lucille's page shows the same (``link.py``, ``gate.py``): ``y`` confirms,
  ``n`` forgets the pair, and the end of stdin leaves the answer to ``pair confirm`` or ``pair
  reject``. What was typed at a terminal before the question is discarded, unanswered. With
  ``--json`` it stops once the SAS is shown (``sas_pending``). ``--hub`` names a loopback hub to
  connect through, and ``--store-file`` keeps the pair in files rather than the OS keyring; both
  are saved for the profile. It refuses while a pair is held, and while another ``pair`` of the
  profile runs: it holds ``state/.pair.lock``, taken without waiting, until it returns. It resets
  the policy to the fresh one before it answers a new code, keeping the earlier one aside. No
  pairing code is ever pasted: one that answers no request of this computer's is refused, in
  every shell (``link.Session.accept``);
- ``pair resume`` and ``pair cancel``: wait again on the kept request, or drop it;
- ``pair status``, ``pair confirm`` and ``pair reject``: where the pair is (showing the SAS again
  while it waits to be confirmed, and the request while one is kept), the ``y`` given later, and
  forgetting a pair that has not been confirmed;
- ``run``: the long-running host (``loop.py``): it keeps the pair, takes the work rooms Lucille
  offers over it, and reads and answers Lucille's requests there, once the local gate is open.
  The first SIGTERM, SIGHUP or Ctrl-C drains it; it also stops, exiting 0, when the pair ends.
  ``--json-events`` writes its events on stdout, ``--exit-on-stdin-eof`` drains it when stdin
  ends, and ``--allow-downgrade`` starts a release older than one that ran on the profile
  before. It has exit codes of its own (:data:`RUN_EXITS`, spec/CLI.md section 13);
- ``status``: the version, the local policy, the pair, the work rooms, and what ``run`` last saw
  of the hub and the clock, read from the profile's files with no hub call, then the claim of
  PRIVACY.md that this release makes (``report.py``);
- ``log [--limit N]``: the last lines of the audit log, oldest first, across its rotated files;
- ``forget``: leave the pair's work rooms, forget the pair here, and tell Lucille if the hub can
  be reached; it says whether Lucille was told. When another command forgot the pair while this
  one waited for the record lock, it says the pair is gone (``none``), as with no pair at all.
  With no pair held, it leaves the work rooms still held over a pair already gone from this
  computer, and tells Lucille nothing (``left``);
- ``ops list|enable|disable``: which catalog operations the local policy enables;
- ``roots list|add|remove|check``: which folders the operations may work in, by alias.
  ``roots add --suggest-alias PATH`` suggests an alias for a folder and changes no setting; ``roots
  check`` says whether the host can open each folder and read an entry in it;
- ``policy previous``: the operations and folders of the last policy a pairing set aside (a
  pairing sets aside only a policy that is not the fresh one); ``policy reset``: set an invalid
  policy file aside, and start from the fresh policy;
- ``pause``, ``resume`` and ``kill``: the local controls;
- ``uninstall-data --yes``: delete the profile's keyring items, then its folders, once it holds
  no pair and no work room (decisions/0002 section 17). It refuses while ``run`` or ``pair`` of
  the profile runs: it takes their locks without waiting, and the policy lock.

Each verb is dispatched by name; one with no handler is refused, never run as another verb.
``--profile <name>`` selects a separate config and state directory, so that two computers can be
simulated on one machine. A verb that fails prints why on stderr and exits 1; a pairing that ends
early, or a refusal of the pairing verbs, prints its state word first (``leaked: ...``,
``expired: ...``).

``--json``, anywhere before a ``--``, makes every verb but ``run`` write on stdout the JSON objects
of ``spec/CLI.md`` instead of its text, one per line, with the same exit codes; stderr stays as it
is. A failure ends with an ``error`` object, and a usage error writes one too. Each verb writes its
text and its objects at the same points, from the same values, so that they say the same. With
``--json``, ``pair`` asks nothing.
``run``'s JSON form is ``run --json-events`` (``--json`` beside it adds nothing): one writer thread
writes its events (:class:`_Events`), and ``stopped`` is the last.

A verb that changes the policy holds the profile's policy lock from reading the file to writing
it, so that two verbs run at once do not lose a change; on a filesystem that cannot lock, it
refuses, and the policy file can be edited by hand. ``pause``, ``resume`` and ``kill`` hold the
controls' lock the same way, and go ahead without it where it cannot be had (``controls.py``).
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import re
import shutil
import sys
import termios
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Final, NoReturn

from lucille_host import (
    __version__,
    _switchboard,
    audit,
    connect,
    connecting,
    gate,
    link,
    loop,
    paths,
    policy,
    report,
    rooms,
    runs,
    store,
)
from lucille_host import state as host_state
from lucille_host.containment import inside_own, on_proc
from lucille_host.controls import Controls
from lucille_host.ops import (
    HOST_PROFILE,
    HOST_PYTHON,
    HOST_STATE,
    OPERATIONS,
    args_valid,
    host_interpreter,
)

# The words ``pair`` takes in place of a code.
PAIR_ACTIONS: Final = ("status", "confirm", "reject")
# The actions of a Connect request from Lucille's chat (decisions/0002 §11.4).
CONNECT_ACTIONS: Final = ("begin", "resume", "cancel")
# In the state folder: held by ``pair begin``, ``pair resume`` and ``pair cancel`` until each
# returns.
PAIR_LOCK: Final = ".pair.lock"
ALREADY_PAIRING: Final = (
    "another `lucille-host pair` is running for this profile, and a profile answers one pairing "
    "code at a time: two could each answer one, and the profile keeps one pair. Let that one "
    "end first"
)
# How many lines ``log`` prints unless told otherwise.
LOG_LINES: Final = 20
# The version of the --json contract this release writes (spec/CLI.md).
CONTRACT: Final = "1.0"
# The message of every usage error with --json: argparse's own words, which can quote the command
# line, a pairing code included, stay on stderr.
USAGE_MESSAGE: Final = "the command line could not be parsed; standard error says why"
# What --json writes for an unexpected error, whose traceback is on stderr.
INTERNAL_MESSAGE: Final = "an unexpected error; standard error holds its traceback"
# Unpaired surrogates, as a file name that is not UTF-8 reaches the host: written as U+FFFD.
_SURROGATES: Final = re.compile("[\ud800-\udfff]")
# What every verb but `--version` and `--help` says as uid 0 (decisions/0004-running-as-root.md,
# 4.2). It names the repair that record gives for a profile a root run has already touched.
ROOT_REFUSED: Final = (
    "lucille-host does not run as root (uid 0); run it as the user whose files it may read. "
    "If an earlier run as root left files or folders in your profile that you cannot read, "
    "see decisions/0004-running-as-root.md, section 4.3, 'Profiles that are already root-owned'"
)


def _is_root() -> bool:
    """Whether this process is uid 0 (read at each call, so that a test may inject another)."""
    return os.geteuid() == 0


# `run`'s exit codes, by the reason its `stopped` object gives (spec/CLI.md section 13.3). Each
# reason has one exit code. 22 is not used.
RUN_EXITS: Final = MappingProxyType(
    {
        "drained": 0,
        "forgotten": 0,
        "ended": 0,
        "not_paired": 20,
        "no_gate": 20,
        "several_pairs": 20,
        "pair_unusable": 20,
        "leaked": 20,
        "expired": 20,
        "already_running": 21,
        "insecure_keyring": 23,
        "store_error": 23,
        "startup_refused": 24,
        "downgrade": 25,
        "failed": 1,
    }
)
# The reasons a pairing word of `link.Stopped` gives, by its code, when `run` stops for it.
_STOPPED_REASONS: Final = {
    "several_pairs": "several_pairs",
    "no_gate": "no_gate",
    "pair_unusable": "pair_unusable",
    "leaked": "leaked",
    "expired": "expired",
}
# The codes a `stopped` object of reason `failed` gives (spec/CLI.md section 10).
RUN_FAILURES: Final = frozenset(
    {
        "internal",
        "bad_profile",
        "unsupported_platform",
        "os_error",
        "state_invalid",
        "runtime_folder",
        "lock_failed",
        "room_not_left",
        "store_error",
        "hub_error",
    }
)
# How many events may wait for a reader that has stopped reading, before standard output counts as
# one nobody reads; and how long `run` waits, at its end, for its events to be written.
EVENTS_QUEUE: Final = 1000
EVENTS_FLUSH: Final = 5.0


class _Refused(Exception):
    """A verb that cannot do what it was asked; the message says why. ``code`` is what ``--json``
    calls it (spec/CLI.md section 10), and ``extra`` the fields its ``error`` object adds."""

    def __init__(self, code: str, message: str, **extra: str) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


class _Usage(Exception):
    """The command line could not be parsed: argparse's own error, raised rather than exiting, so
    that ``--json`` can write its ``error`` object first."""

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


class _Parser(argparse.ArgumentParser):
    """The command's parsers: a usage error raises :class:`_Usage`."""

    # Whether the command line asked for --json, for --version, which ends it.
    json_wanted = False
    # The parsers of the verbs whose forms are checked once the line is parsed, by name.
    forms: dict[str, argparse.ArgumentParser] = {}
    # The ``credential`` verb's parser and its actions': a usage error any of them raises
    # repeats nothing typed (:func:`_usage_said`).
    credential_parsers: frozenset[argparse.ArgumentParser] = frozenset()

    def error(self, message: str) -> NoReturn:
        raise _Usage(self, message)


class _Version(argparse.Action):
    """``--version``: the version, as text or as a ``version`` object, and the end of the line."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        _Out(getattr(parser, "json_wanted", False)).version()
        parser.exit(0)


def _valid(value: Any) -> Any:
    """``value`` with every unpaired surrogate in its strings written as U+FFFD."""
    if isinstance(value, str):
        return _SURROGATES.sub("\ufffd", value)
    if isinstance(value, dict):
        return {key: _valid(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_valid(item) for item in value]
    return value


class _Out:
    """Where a verb writes what it did: its text on stdout, or, with ``--json``, the objects of
    spec/CLI.md, one per line, each flushed before the host does the next thing."""

    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def say(self, line: str, type_name: str, **fields: Any) -> None:
        """The text line, or the object of type ``type_name``."""
        if self.as_json:
            self.obj(type_name, **fields)
        else:
            self.text(line)

    def text(self, line: str) -> None:
        """A line of the text only."""
        if not self.as_json:
            print(line, flush=True)

    def obj(self, type_name: str, **fields: Any) -> None:
        """An object only."""
        if self.as_json:
            # A value that is not JSON (NaN, an infinity) raises here, before anything is written.
            written = json.dumps(
                _valid({"v": 1, "type": type_name, **fields}),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            print(written, flush=True)

    def error(self, code: str, message: str, **extra: Any) -> None:
        self.obj("error", code=code, message=message, **extra)

    def version(self) -> None:
        self.say(version_line(), "version", host_version=__version__, contract=CONTRACT)


def _line(type_name: str, fields: dict[str, Any]) -> str:
    """One object as a line of spec/CLI.md section 4.1, without its newline."""
    return json.dumps(
        _valid({"v": 1, "type": type_name, **fields}),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _quiet(fd: int) -> None:
    """Put /dev/null on ``fd``, so that nothing written there later fails."""
    with contextlib.suppress(OSError):
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, fd)
        finally:
            os.close(null)


def _err(line: str) -> None:
    """A line on stderr that cannot fail ``run``: a stderr nobody reads is put on /dev/null."""
    try:
        print(line, file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        _quiet(2)


def _warn_quietly(line: str) -> None:
    _err(f"lucille-host: warning: {line}")


class _Events:
    """The events of ``run --json-events``: each object is serialized by the thread that has it,
    and written, whole, by one writer thread of its own, so that a shell that reads slowly holds up
    no thread of ``run``. The writer flushes each line. A standard output that fails a write (a
    shell that is gone), or that has :data:`EVENTS_QUEUE` objects waiting (a shell that stopped
    reading), gets nothing more, and ``on_broken`` is called. Once :meth:`close` has queued
    ``stopped``, nothing else is written."""

    def __init__(self, stream: Any, *, most: int = EVENTS_QUEUE) -> None:
        self._stream = stream
        self._most = most
        self._cv = threading.Condition()
        self._queue: collections.deque[str] = collections.deque()
        self._closed = False
        self.broken = False
        self.on_broken: Callable[[], None] | None = None
        self._thread = threading.Thread(
            target=self._write_loop, name="lucille-host events", daemon=True
        )
        self._thread.start()

    def emit(self, type_name: str, **fields: Any) -> None:
        """Queue one object; nothing waits for it to be written."""
        line = _line(type_name, fields)
        with self._cv:
            if self._closed or self.broken:
                return
            overflow = len(self._queue) >= self._most
            if not overflow:
                self._queue.append(line)
            self._cv.notify_all()
        if overflow:
            self._gone()

    def tell(self, type_name: str, fields: dict[str, Any]) -> None:
        self.emit(type_name, **fields)

    def close(self, type_name: str, **fields: Any) -> None:
        """Queue the last object; any later is dropped."""
        line = _line(type_name, fields)
        with self._cv:
            if self._closed:
                return
            self._closed = True
            if not self.broken:
                self._queue.append(line)
            self._cv.notify_all()

    def finish(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for what is queued to be written; whether it was."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _gone(self) -> None:
        with self._cv:
            if self.broken:
                return
            self.broken = True
            self._queue.clear()
            self._cv.notify_all()
        try:
            fd = self._stream.fileno()
        except (AttributeError, OSError, ValueError):
            fd = None
        if fd is not None:
            _quiet(fd)
        if self.on_broken is not None:
            self.on_broken()

    def _write_loop(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed and not self.broken:
                    self._cv.wait()
                if not self._queue:
                    if self._closed or self.broken:
                        return
                    continue
                line = self._queue.popleft()
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except Exception:  # noqa: BLE001 - any failure to write is a stdout nobody reads
                self._gone()


class _Descriptor:
    """A text stream onto a file descriptor of its own, written whole with ``os.write``."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def write(self, text: str) -> int:
        view = memoryview(text.encode("ascii"))
        while view:
            view = view[os.write(self._fd, view) :]
        return len(text)

    def flush(self) -> None:
        pass


class _Stdout:
    """Standard output, taken for ``run --json-events``: the events are written on a private
    duplicate of its descriptor, made blocking, and the descriptor itself, and ``sys.stdout``,
    are pointed at standard error until ``run`` ends. So nothing but the events' writer reaches
    the shell's stdout pipe: neither a line a library prints, nor ``run``'s own text."""

    def __init__(self) -> None:
        self._saved = sys.stdout
        self._fd: int | None = None
        self._real: int | None = None
        try:
            real = self._saved.fileno()
        except (AttributeError, OSError, ValueError):
            real = None
        if real is not None:
            with contextlib.suppress(OSError, ValueError):
                self._saved.flush()
            try:
                self._fd = os.dup(real)
            except OSError:
                self._fd = None
            if self._fd is not None:
                with contextlib.suppress(OSError):
                    os.set_blocking(self._fd, True)
                try:
                    os.dup2(2, real)
                    self._real = real
                except OSError:
                    self._real = None
        self.stream: Any = _Descriptor(self._fd) if self._fd is not None else self._saved
        sys.stdout = sys.stderr

    def give_back(self, *, broken: bool, written: bool) -> None:
        """Standard output as it was: on /dev/null if it broke. The private descriptor stays open
        while the writer may still be in a write (``written`` false)."""
        sys.stdout = self._saved
        if self._fd is None:
            return
        if broken:
            _quiet(self._fd)
        if self._real is not None:
            with contextlib.suppress(OSError, ValueError):
                self._saved.flush()
            with contextlib.suppress(OSError):
                os.dup2(self._fd, self._real)
        if written:
            os.close(self._fd)


def version_line() -> str:
    """The line ``lucille-host`` and ``lucille-host --version`` print."""
    return f"lucille-host {__version__}"


def _json_option(parser: argparse.ArgumentParser) -> None:
    # On every parser, so that --json may come before the verb, after it, or after its action. A
    # subparser sets it only when it sees it: a False default would undo one given earlier.
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="write the JSON objects of spec/CLI.md on stdout instead of text",
    )


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for the ``lucille-host`` command."""
    parser = _Parser(
        prog="lucille-host",
        description=(
            "Runs a closed set of named operations for a paired Lucille account: `pair` pairs "
            "this computer with Lucille, and `run` answers Lucille's requests, within the "
            "catalog, the local policy and the local controls."
        ),
    )
    parser.add_argument(
        "--version", nargs=0, action=_Version, help="show the version, and do nothing else"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write the JSON objects of spec/CLI.md on stdout instead of text",
    )
    parser.add_argument(
        "--profile",
        default=paths.DEFAULT_PROFILE,
        help="use a separate config and state directory (default: %(default)s)",
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    ops = verbs.add_parser("ops", help="list, enable or disable catalog operations")
    _json_option(ops)
    ops_verbs = ops.add_subparsers(dest="ops_verb", metavar="ACTION", required=True)
    _json_option(
        ops_verbs.add_parser("list", help="show every catalog operation and whether it is enabled")
    )
    for action in ("enable", "disable"):
        sub = ops_verbs.add_parser(action, help=f"{action} one operation")
        _json_option(sub)
        sub.add_argument("op", help="the operation's name, e.g. files.sha256")

    roots = verbs.add_parser("roots", help="list, add, remove or check folder aliases")
    _json_option(roots)
    roots_verbs = roots.add_subparsers(dest="roots_verb", metavar="ACTION", required=True)
    _json_option(roots_verbs.add_parser("list", help="show each folder alias and its folder"))
    add = roots_verbs.add_parser(
        "add",
        help="let operations work in a folder, under an alias",
        usage=(
            "%(prog)s [-h] [--allow-home-or-root] alias path\n       %(prog)s --suggest-alias PATH"
        ),
    )
    _json_option(add)
    add.add_argument(
        "alias", nargs="?", help="the name Lucille will use: lowercase, at most 32 characters"
    )
    add.add_argument("path", nargs="?", help="the folder")
    add.add_argument(
        "--allow-home-or-root",
        action="store_true",
        help="needed when the folder is your home folder, holds it, or is /",
    )
    add.add_argument(
        "--suggest-alias",
        metavar="PATH",
        help="only suggest an alias for the folder PATH, which no folder has yet; set nothing",
    )
    remove = roots_verbs.add_parser("remove", help="stop operations working in a folder")
    _json_option(remove)
    remove.add_argument("alias")
    check = roots_verbs.add_parser(
        "check", help="say whether the host can open each folder and read an entry in it"
    )
    _json_option(check)
    check.add_argument("alias", nargs="?", help="check this folder alias only")

    pair = verbs.add_parser(
        "pair",
        help="connect to Lucille from your chat, then confirm the SAS both sides show",
        description=(
            "`pair begin` (or `pair` alone) shows a connect code to send Lucille in your chat, "
            "waits for Lucille's answer, then shows the pair's SAS and asks for y if Lucille's "
            "page shows the same; `pair resume` waits again, and `pair cancel` drops the code; "
            "`pair status` says where the pair is, and shows the SAS again while it waits; "
            "`pair confirm` confirms the SAS shown; `pair reject` forgets a pair that is not "
            "confirmed. No pairing code is pasted."
        ),
    )
    _json_option(pair)
    pair.add_argument(
        "action",
        nargs="?",
        metavar="begin | resume | cancel | status | confirm | reject",
        help="what to do; `begin` when none is given",
    )
    pair.add_argument(
        "--hub",
        metavar="URL",
        help=(
            "pair begin: connect through this hub on this computer: http://127.0.0.1:<port> or "
            "http://localhost:<port>"
        ),
    )
    pair.add_argument(
        "--store-file",
        action="store_true",
        help="keep the pair in files in the state folder, not the OS keyring (weaker)",
    )
    pair.add_argument(
        "--test-key",
        metavar="KEY",
        help=(
            "pair begin, with --hub only: also take answers signed with this key (32 bytes, "
            "base64url), for testing against a local hub; the app never passes it"
        ),
    )

    run = verbs.add_parser(
        "run",
        help="answer Lucille's requests over the pair until stopped (Ctrl-C drains)",
        description=(
            "Keeps the pair, takes the work rooms Lucille offers over it, and runs the requests "
            "it posts there that the local policy allows. Nothing is accepted, posted or run "
            "before the SAS is confirmed. The first Ctrl-C, SIGTERM or SIGHUP lets the running "
            "operation finish and stops; a second Ctrl-C kills it. Its exit codes are its own "
            "(spec/CLI.md section 13)."
        ),
    )
    _json_option(run)
    run.add_argument(
        "--json-events",
        action="store_true",
        help="write the events of spec/CLI.md section 13 on stdout, one JSON object per line",
    )
    run.add_argument(
        "--exit-on-stdin-eof",
        action="store_true",
        help="drain, as on the first SIGTERM, when standard input ends",
    )
    run.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="start although a newer release has started `run` on this profile before",
    )

    status = verbs.add_parser(
        "status",
        help="show the policy, the pair, the work rooms and what `run` last saw (no hub call)",
        description=(
            "Shows the version, the local policy and the operations it enables, the controls, "
            "where the pair is kept and the hub, the pair (never its SAS), the work rooms held, "
            "and what `run` last saw: whether it runs, its last contact with the hub, and the "
            "clock, then the claim this release makes, as in PRIVACY.md. It reads this "
            "computer's files only, and makes no hub call."
        ),
    )
    _json_option(status)
    log = verbs.add_parser("log", help="show the last lines of the audit log, oldest first")
    _json_option(log)
    log.add_argument(
        "--limit",
        type=int,
        default=LOG_LINES,
        metavar="N",
        help="how many lines (default: %(default)s)",
    )
    forget = verbs.add_parser(
        "forget",
        help="leave the work rooms, forget the pair here, and tell Lucille if the hub is reachable",
        description=(
            "Leaves every work room held over the pair, then deletes this computer's half of the "
            "pair and tells Lucille, best effort; it says whether Lucille was told. If it was not, "
            "Lucille keeps its half until the computer is removed there. With no pair held, it "
            "leaves the work rooms still held over a pair already gone from this computer, and "
            "tells Lucille nothing."
        ),
    )
    _json_option(forget)

    policy_verb = verbs.add_parser(
        "policy", help="show the last policy a pairing set aside, or reset an invalid one"
    )
    _json_option(policy_verb)
    policy_verbs = policy_verb.add_subparsers(dest="policy_verb", metavar="ACTION", required=True)
    _json_option(
        policy_verbs.add_parser(
            "previous",
            help="list the operations and folders of the last policy a pairing set aside",
        )
    )
    _json_option(
        policy_verbs.add_parser(
            "reset", help="set aside an invalid policy file, and start from the fresh policy"
        )
    )

    uninstall = verbs.add_parser(
        "uninstall-data",
        help="delete this profile's keyring items and folders, once it holds no pair",
        description=(
            "Deletes every keyring item the host's stores keep for this profile (the pair's, "
            "and the credentials'), then this profile's folders: its settings, its state and its "
            "audit log. Only this profile's: another profile, `default` among them, is left "
            "alone. It refuses while `run` or `pair` runs for the profile, and while the profile "
            "holds a pair, or work rooms over one: run `forget` first, which tells Lucille when "
            "the hub can be reached. --yes is required: nothing asks."
        ),
    )
    _json_option(uninstall)
    uninstall.add_argument(
        "--yes", action="store_true", required=True, help="delete without asking (required)"
    )

    _json_option(verbs.add_parser("pause", help="start nothing until resume; refuse new requests"))
    _json_option(verbs.add_parser("resume", help="clear pause and any kill request"))
    _json_option(verbs.add_parser("kill", help="kill the operation in flight, and pause"))

    # No --json: the verb has no JSON form in contract version 1 (spec/CLI.md section 14).
    credential = verbs.add_parser(
        "credential",
        help=(
            "save, list, test or remove credentials for online services, kept only on this computer"
        ),
        description=(
            "Keeps credentials for online services (a password, an app token) in the system "
            "keyring of this computer, each bound to the hosts it may be sent to. The value is "
            "typed here, never given on the command line, and is never shown. Only github.read "
            "and the gmail operations use one: a credential of the service github, bound to "
            "api.github.com, or of the service gmail, bound to imap.gmail.com, read by a "
            "separate process when that operation is on. Nothing Lucille sends can list or "
            "change them."
        ),
    )
    credential_verbs = credential.add_subparsers(
        dest="credential_verb", metavar="ACTION", required=True
    )
    credential_set = credential_verbs.add_parser(
        "set",
        help="save or replace a credential: its value is asked for, and not shown",
    )
    for argument, what in (
        ("service", "the service, such as gmail or github.com"),
        ("name", "the credential's name, such as app-password"),
    ):
        credential_set.add_argument(argument, help=what)
    credential_set.add_argument(
        "--host",
        dest="hosts",
        action="append",
        required=True,
        metavar="HOST",
        help="a host the credential may be sent to, such as imap.gmail.com; once for each host",
    )
    credential_set.add_argument(
        "--value-stdin",
        action="store_true",
        help="read the value from one line of stdin, without asking (stdin must not be a terminal)",
    )
    credential_set.add_argument(
        "--replace",
        action="store_true",
        help="replace a credential saved already: its value, and its hosts with the ones given now",
    )
    credential_list = credential_verbs.add_parser(
        "list", help="show each credential's name, its hosts and when it was saved, never its value"
    )
    credential_remove = credential_verbs.add_parser("remove", help="delete one credential")
    credential_remove.add_argument("service", help="the service")
    credential_remove.add_argument("name", help="the credential's name")
    credential_test = credential_verbs.add_parser(
        "test", help="sign in with one credential and out again, reading nothing (gmail only)"
    )
    credential_test.add_argument("service", help="the service")
    credential_test.add_argument("name", help="the credential's name")
    parser.forms = {"run": run, "pair": pair, "roots add": add, "credential": credential}
    parser.credential_parsers = frozenset(
        {credential, credential_set, credential_list, credential_remove, credential_test}
    )
    for each in parser.credential_parsers:
        _json_option(each)
    return parser


def _wants_json(argv: Sequence[str]) -> bool:
    """Whether the command line holds ``--json`` or ``--json-events`` before any ``--``: what a
    usage error, which has no parsed line to look in, and ``--version``, which ends the line, go
    by."""
    for word in argv:
        if word == "--":
            return False
        if word in ("--json", "--json-events"):
            return True
    return False


def _check_usage(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """The forms the parser takes and the command does not: they are usage errors too."""
    as_json = getattr(args, "json", False)
    forms = getattr(parser, "forms", {})
    if args.verb == "run" and as_json and not args.json_events:
        forms["run"].error("run writes its JSON with --json-events")
    if args.verb == "credential" and as_json and args.credential_verb == "set":
        if not args.value_stdin:
            forms["credential"].error(
                "with --json, credential set reads the value with --value-stdin"
            )
    if args.verb == "roots" and args.roots_verb == "add":
        add = forms["roots add"]
        if args.suggest_alias is not None:
            if args.alias is not None or args.path is not None or args.allow_home_or_root:
                add.error("--suggest-alias takes no alias, no folder and no other option")
            return
        missing = [name for name in ("alias", "path") if getattr(args, name) is None]
        if missing:
            add.error(f"the following arguments are required: {', '.join(missing)}")


def _dirs(profile: str) -> paths.Dirs:
    if not paths.valid_profile(profile):
        raise _Refused(
            "bad_profile",
            "a profile name is lowercase letters, digits, '-' and '_', starting with a letter, "
            "at most 32",
        )
    return paths.dirs(profile)


def _load(dirs: paths.Dirs) -> policy.Policy:
    try:
        return policy.load(dirs.policy_file)
    except policy.PolicyError as exc:
        raise _Refused(
            "policy_invalid", f"the policy file {dirs.policy_file} is invalid: {exc}"
        ) from exc


def _save(dirs: paths.Dirs, new: policy.Policy) -> None:
    paths.ensure_dir(dirs.config)
    policy.save(dirs.policy_file, new)


@contextlib.contextmanager
def _policy_lock(dirs: paths.Dirs) -> Iterator[None]:
    """Hold the profile's policy lock: another verb changing the policy waits until this one has
    written it."""
    paths.ensure_dir(dirs.config)
    lock = dirs.config / ".policy.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            host_state.wait_for_lock(descriptor)
        except OSError as exc:
            raise _Refused(
                "policy_lock_failed",
                f"cannot lock {lock} ({exc.strerror}), so the policy was not changed",
            ) from exc
        yield
    finally:
        os.close(descriptor)


def _op(name: str, current: policy.Policy, family: str) -> dict[str, Any]:
    """An operation as ``ops`` reports it with ``--json``: its switch, and its words."""
    op = OPERATIONS.get(name)
    if op is None:
        return {"name": name, "state": "not_in_catalog", "enabled": False}
    enabled = current.enabled.get(name) == op.definition_hash
    # One this computer cannot run is unavailable whatever definition the policy holds:
    # `ops enable` would refuse it, so it is never offered as changed by an upgrade.
    if not op.available(family):
        state = "unavailable"
    elif name in current.enabled and not enabled:
        state = "disabled_by_upgrade"
    else:
        state = "enabled" if enabled else "disabled"
    found = {
        "name": name,
        "state": state,
        "enabled": enabled,
        "summary": op.about.summary,
        "sends": op.about.sends,
        "sends_file_content": op.about.sends_file_content,
        "diagnostic": op.about.diagnostic,
    }
    if state == "unavailable":
        found["missing_executable"] = op.executable(family)
    return found


def _ops(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    family = paths.os_family()
    current = _load(dirs)
    if args.ops_verb == "list":
        for name in OPERATIONS:
            found = _op(name, current, family)
            if found["state"] == "disabled_by_upgrade":
                state = (
                    f"disabled: an upgrade changed it; `lucille-host ops enable {name}` accepts it"
                )
            elif found["state"] == "unavailable":
                state = (
                    "enabled, but" if found["enabled"] else "disabled;"
                ) + f" not on this computer: {found['missing_executable']} is missing"
            else:
                state = found["state"]
            out.say(f"{name}  {state}", "op", **found)
        for name in policy.effective(current, family).not_in_catalog:
            out.say(
                f"{name}  in the policy, but not in this release's catalog",
                "op",
                **_op(name, current, family),
            )
        return
    name = args.op
    enabled = dict(current.enabled)
    if args.ops_verb == "enable":
        op = OPERATIONS.get(name)
        if op is None:
            raise _Refused("not_in_catalog", f"{name} is not in this release's catalog")
        if not op.available(family):
            raise _Refused(
                "unavailable", f"{name} cannot run here: {op.executable(family)} is missing"
            )
        enabled[name] = op.definition_hash
        changed = replace(current, enabled=MappingProxyType(enabled))
        _save(dirs, changed)
        out.say(f"enabled {name}", "op", **_op(name, changed, family))
        return
    if name not in enabled:
        raise _Refused("not_enabled", f"{name} is not enabled")
    del enabled[name]
    changed = replace(current, enabled=MappingProxyType(enabled))
    _save(dirs, changed)
    if name in OPERATIONS:
        out.say(f"disabled {name}", "op", **_op(name, changed, family))
    else:
        out.say(f"disabled {name}", "op_removed", name=name)


def _broad(path: str) -> _Refused:
    return _Refused(
        "broad_folder",
        f"{path} is your home folder, holds it, or is /; add --allow-home-or-root to mean it",
        kind=policy.broad_kind(path),
    )


def _roots(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    current = _load(dirs)
    roots = dict(current.roots)
    if args.roots_verb == "list":
        for alias in sorted(roots):
            root = roots[alias]
            out.say(f"{alias}  {root.path}", "root", alias=alias, path=root.path, broad=root.broad)
        return
    if args.roots_verb == "check":
        _check(args, roots, out)
        return
    if args.roots_verb == "add" and args.suggest_alias is not None:
        name = os.path.basename(os.path.abspath(args.suggest_alias))
        alias = policy.suggest_alias(name, roots)
        out.say(alias, "alias_suggestion", alias=alias)
        return
    alias = args.alias
    if args.roots_verb == "remove":
        if alias not in roots:
            raise _Refused("no_such_alias", f"no folder alias {alias!r}")
        del roots[alias]
        _save(dirs, replace(current, roots=MappingProxyType(roots)))
        out.say(f"removed {alias}", "root_removed", alias=alias)
        return
    if not policy.valid_alias(alias):
        raise _Refused(
            "bad_alias",
            "an alias is lowercase letters, digits, '-' and '_', starting with a letter, at most "
            "32",
        )
    if alias in roots:
        raise _Refused("alias_taken", f"{alias} is already a folder alias; remove it first")
    if len(roots) >= policy.MAX_ROOTS:
        raise _Refused(
            "too_many_roots", f"the policy holds at most {policy.MAX_ROOTS} folder aliases"
        )
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        raise _Refused("not_a_folder", f"{path} is not a folder")
    if inside_own(path, [str(own) for own in dirs.own]):
        raise _Refused("own_folder", f"{path} is where lucille-host keeps its own files")
    if on_proc(path):
        raise _Refused(
            "on_proc",
            f"{path} is on /proc, where names mean one process to the host and another to an "
            "operation (or this computer's mount table does not say where /proc is)",
        )
    # Only here, with the user at the terminal to interrupt it, does the check look below the
    # folder (policy.holds).
    broad = policy.is_broad(path, look_below=True)
    if broad and not args.allow_home_or_root:
        raise _broad(path)
    roots[alias] = policy.Root(path=path, broad=broad)
    try:
        _save(dirs, replace(current, roots=MappingProxyType(roots)))
    except policy.PolicyError as exc:
        raise _Refused("unstorable_path", str(exc)) from exc
    out.say(f"added {alias}", "root", alias=alias, path=path, broad=broad)


def _check(args: argparse.Namespace, roots: dict[str, policy.Root], out: _Out) -> None:
    """``roots check``: open each folder and read one entry, writing each answer as it is known.
    It takes no lock, opens no session, and sends nothing. It writes nothing but the line of the
    run record that :func:`main` writes for every command (:mod:`lucille_host.runs`)."""
    if args.alias is not None and args.alias not in roots:
        raise _Refused("no_such_alias", f"no folder alias {args.alias!r}")
    family = paths.os_family()
    for alias in [args.alias] if args.alias is not None else sorted(roots):
        path = roots[alias].path
        state, name = policy.check_root(path, family)
        found: dict[str, Any] = {"alias": alias, "path": path, "state": state}
        if name is not None:
            found["errno"] = name
        out.say(f"{alias}  {state}" + (f": {name}" if name else ""), "root_check", **found)


def _policy_previous(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    path = dirs.previous_policy_file
    if not path.exists():
        out.say("no earlier policy: no pairing has set one aside", "previous_policy", state="none")
        return
    try:
        previous = policy.load(path)
    except policy.PolicyError as exc:
        raise _Refused("previous_invalid", f"the earlier policy {path} is invalid: {exc}") from exc
    # When it was set aside: the file's time, which a pairing sets. One no file of the host's holds
    # is refused with --json and without it alike, so that both exit the same.
    set_aside = path.stat().st_mtime
    if not host_state.valid_time(set_aside):
        raise _Refused(
            "previous_invalid",
            f"the earlier policy {path} is invalid: the time it was set aside cannot be read",
        )
    roots = previous.roots
    if out.as_json:
        out.obj(
            "previous_policy",
            state="found",
            set_aside_at=link.iso(set_aside),
            ops=sorted(previous.enabled),
            folders=[
                {"alias": alias, "path": roots[alias].path, "broad": roots[alias].broad}
                for alias in sorted(roots)
            ],
        )
        return
    lines = [f"the last policy a pairing set aside ({path}); nothing uses it:"]
    lines.append("operations:")
    lines += [f"  {name}" for name in sorted(previous.enabled)] or ["  (none)"]
    lines.append("folders:")
    lines += [f"  {alias}  {roots[alias].path}" for alias in sorted(roots)] or ["  (none)"]
    for line in lines:
        out.text(line)


def _policy_reset(dirs: paths.Dirs, out: _Out) -> None:
    """``policy reset``: set an invalid policy file aside, under the policy lock, and write the
    fresh policy. A valid file, and no file, are left as they are."""
    with _policy_lock(dirs):
        moved = policy.set_aside_invalid(dirs.policy_file, dirs.invalid_policy_file)
    if not moved:
        raise _Refused(
            "not_invalid",
            "the local policy is not invalid: `lucille-host policy reset` sets aside only a "
            "policy file that is invalid",
        )
    out.say(
        f"reset: the invalid policy file is set aside as {dirs.invalid_policy_file}; only "
        "host.describe is enabled, and no folder is held",
        "policy_reset",
    )


def _policy_verb(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    if args.policy_verb == "reset":
        _policy_reset(dirs, out)
    else:
        _policy_previous(args, dirs, out)


def _say(line: str) -> None:
    print(line, flush=True)


def _warn(line: str) -> None:
    print(f"lucille-host: warning: {line}", file=sys.stderr, flush=True)


def _read_line(prompt: str, out: _Out) -> str:
    """One line of stdin, with a prompt on stderr when stdin is a terminal and the command asks
    for text; "" at its end."""
    if sys.stdin.isatty() and not out.as_json:
        print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline()


def _pair_view(view: link.PairView) -> dict[str, Any]:
    """What ``pair status`` reports, as the ``pair`` object's fields."""
    found: dict[str, Any] = {"state": view.word}
    if view.name is not None:
        found["name"] = view.name
    if view.word == "sas_pending":
        found["sas"] = view.sas
        found["deadline"] = link.iso(view.deadline or 0)
    return found


def _pair_action(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    if args.hub is not None or args.store_file or args.test_key is not None:
        raise _Refused("bad_options", f"`pair {args.action}` takes no options")
    session = link.open_session(dirs, host_state.load_host_config(dirs), say=_say, warn=_warn)
    if args.action == "status":
        view = session.status_view()
        if view.word == "none" and dirs.connect_file.exists():
            if _connect_status(session, dirs, out):
                return
            # A `pair begin` or `pair resume` answered the request's code, or ended it, meanwhile.
            view = session.status_view()
        word, detail = link.pair_words(view)
        out.say(f"{word}: {detail}", "pair", **_pair_view(view))
        return
    if args.action == "reject":
        told = session.reject()
        out.say(
            f"rejected: pair {told.name} is forgotten on this computer; "
            + link.told_sentence(told),
            "pair",
            state="rejected",
            name=told.name,
            lucille_told=told.told,
        )
        return
    name, said = session.confirm_pair()
    out.say(f"confirmed: {said}", "pair", state="confirmed", name=name)


def _discard_typed_ahead() -> bool:
    """Discard what was typed on the terminal before the SAS was shown: a ``y`` typed while
    ``pair`` waited for Lucille was typed before anyone could compare. Whether that was done,
    or there was nothing to do (stdin is not a terminal, and what it holds is read as given)."""
    if not sys.stdin.isatty():
        return True
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (termios.error, OSError):
        return False
    return True


def _ask(session: link.Session, name: str, deadline: float | None, out: _Out) -> str:
    """Ask whether Lucille's page shows the same SAS until ``y``, ``n``, or the end of stdin.
    Nothing typed on the terminal before the question counts; where that cannot be made sure
    of, nothing is asked, and ``pair confirm`` or ``pair reject`` answers later."""
    before = f" before {link.iso(deadline)}" if deadline is not None else ""
    later = (
        f"sas_pending: pair {name} is not confirmed yet: `lucille-host pair confirm` "
        f"confirms it{before}, and `lucille-host pair reject` rejects it"
    )
    if not _discard_typed_ahead():
        return later
    while True:
        entry = _read_line(
            "Does Lucille's page show the same SAS? Type y if it does, n if not: ", out
        )
        if not entry:
            return later
        said = gate.answer(entry)
        if said is True:
            return f"confirmed: {session.open_gate(name)}"
        if said is False:
            told = session.reject()
            raise link.Stopped(
                "rejected",
                f"pair {name} is forgotten on this computer; start again in Lucille. "
                + link.told_sentence(told),
            )
        print(
            "type y if Lucille's page shows the same SAS, or n if it does not",
            file=sys.stderr,
            flush=True,
        )


def _pair(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    if args.action is None:
        # `pair` alone connects, as `pair begin` does (decisions/0002 §11.4, step 5).
        args.action = "begin"
    if args.action in CONNECT_ACTIONS:
        _connect_action(args, dirs, out)
        return
    if args.action in PAIR_ACTIONS:
        _pair_action(args, dirs, out)
        return
    # Anything else may be a pairing code someone was told to paste: it is never taken, and never
    # repeated.
    raise _Refused(
        "code_in_argv",
        "`pair` takes no pairing code: `lucille-host pair begin` shows a code to send Lucille in "
        "your chat; the actions are begin, resume, cancel, status, confirm and reject",
    )


def _pairing_session(
    dirs: paths.Dirs, wanted: host_state.HostConfig
) -> tuple[link.Session, host_state.HostConfig, host_state.HostConfig]:
    """The session a pairing uses, and the configurations it was opened with and found, once
    no pair is held. Refuses ``held`` while one is."""
    current = host_state.load_host_config(dirs)
    try:
        session = link.open_session(dirs, current, say=_say, warn=_warn)
        opened = current
    except store.InsecureKeyring:
        if wanted.store != "file":
            raise
        # A keyring backend the host refuses is never read, whatever it holds: only the file store
        # is looked in.
        session = link.open_session(dirs, wanted, say=_say, warn=_warn)
        opened = wanted
    try:
        held = session.held()
    except store.KeyringUnusable as exc:
        # The keyring was accepted, and then its Secret Service could not be reached or started at
        # the first read (issue 38).
        # With --store-file, the file store is looked in instead, as for a keyring refused at
        # open; never once this process has tried to write the pair item to the keyring, which
        # could leave one pair there and another in the file.
        if wanted.store != "file" or opened.store == "file" or store.pair_write_tried():
            raise
        _warn(f"{exc}; as --store-file asks, the pair is kept in a file instead")
        session = link.open_session(dirs, wanted, say=_say, warn=_warn)
        opened = wanted
        held = session.held()
    if held:
        raise _Refused(
            "held",
            "Disconnect first: this computer already holds a pair (`lucille-host pair reject` "
            "forgets one that is not confirmed yet)",
        )
    return session, opened, current


def _answered(
    session: link.Session,
    code: _switchboard.Code,
    dirs: paths.Dirs,
    wanted: host_state.HostConfig,
    opened: host_state.HostConfig,
    current: host_state.HostConfig,
    out: _Out,
    *,
    nonce: bytes,
    ask: bool,
    answered: Callable[[], None] | None = None,
) -> None:
    """Answer a code the host has checked, delivered for the request whose nonce is ``nonce``,
    wait for Lucille to collect the answer, show the SAS, and ask about it when ``ask``.
    ``answered``, when given, is called once the answer was tried, whatever came of it."""
    note = link.sanitize(code.note)
    out.say(
        f"pairing code for pair {code.name}, on {code.url}",
        "pair_code",
        name=code.name,
        hub=code.url,
        note=note,
    )
    if note:
        out.text(f"note: {note}")
    if wanted != opened:
        session = link.open_session(dirs, wanted, say=_say, warn=_warn)
        if session.held():
            raise _Refused(
                "held",
                "Disconnect first: this computer already holds a pair in that store "
                "(`lucille-host pair reject` forgets one that is not confirmed yet)",
            )
    if wanted != current:
        host_state.save_host_config(dirs, wanted)
    # A new pair starts from the fresh policy; the one it had is set aside, not applied.
    with _policy_lock(dirs):
        moved = policy.archive(dirs.policy_file, dirs.previous_policy_file)
    if moved:
        out.say(
            "the policy is reset for the new pair: only host.describe is enabled, and no folder "
            "is held; `lucille-host policy previous` lists the earlier one",
            "policy_reset",
        )
    try:
        name = session.accept(code, nonce=nonce)
    finally:
        if answered is not None:
            answered()
    if session.skewed:
        _warn(link.SKEWED)
    out.say(
        f"accepted: pair {name}", "pair", state="accepted", name=name, clock_skewed=session.skewed
    )
    out.say(
        "waiting: for Lucille to collect this computer's answer", "pair", state="waiting", name=name
    )
    try:
        session.wait(name)
    except KeyboardInterrupt:
        raise link.Stopped(
            "interrupted", f"pair {name} is kept; `lucille-host pair status` resumes"
        ) from None
    shown = session.show(name)
    if shown is None:
        raise session.no_gate(name)
    sas, found = shown
    out.say(
        f"sas_pending: {session.pending(name, sas, found)}",
        "pair",
        state="sas_pending",
        name=name,
        sas=sas,
        deadline=link.iso(found.deadline or 0),
    )
    if not ask:
        return
    try:
        out.text(_ask(session, name, found.deadline, out))
    except KeyboardInterrupt:
        raise link.Stopped(
            "interrupted",
            f"pair {name} is kept; `lucille-host pair status` shows its SAS again, and "
            "`lucille-host pair confirm` confirms it",
        ) from None


# --- Connect from Lucille's chat (decisions/0002 §11.4) -------------------------------------------

_SERVICE_NAMES: Final = MappingProxyType({"telegram": "Telegram"})
# How every pairing code the library makes begins.
_PAIRING_CODE_PREFIX: Final = "swbp1_"
PASTE_IS_GONE: Final = (
    "pasting a pairing code is no longer supported: `lucille-host pair begin` reads the chat "
    "service (telegram), then your username there, and shows a code to send Lucille in your chat"
)


def _connect_action(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    """``pair begin``, ``pair resume`` and ``pair cancel``, each under the profile's pair lock."""
    options_given = args.hub is not None or args.store_file or args.test_key is not None
    if args.action != "begin" and options_given:
        raise _Refused("bad_options", f"`pair {args.action}` takes no options")
    if args.action == "begin" and args.test_key is not None and args.hub is None:
        raise _Refused("bad_options", "--test-key is taken only with --hub")
    wanted: host_state.HostConfig | None = None
    if args.action == "begin":
        try:
            wanted = host_state.HostConfig(
                hub=args.hub, store="file" if args.store_file else "keyring"
            )
        except ValueError as exc:
            raise _Refused("bad_hub", str(exc)) from exc
        if args.test_key is not None:
            if host_state.decode_test_key(args.test_key) is None:
                raise _Refused("bad_options", "--test-key is 32 bytes in unpadded base64url")
            wanted = replace(wanted, test_key=args.test_key)
    host_state.startup_checks()
    lock = paths.ensure_dir(dirs.state) / PAIR_LOCK
    with loop.one_at_a_time(lock, ALREADY_PAIRING, "`pair`"):
        if args.action == "cancel":
            _connect_cancel(dirs, out)
        elif wanted is not None:
            _connect_begin(dirs, wanted, out)
        else:
            _connect_resume(dirs, out)


def _connect_code(request: connecting.Request, out: _Out) -> None:
    shown = connect.shown(request.code)
    until = link.iso(request.expires_at)
    out.say(
        f"code: send this to Lucille in your chat: connect {shown}. It works once, until {until}",
        "connect",
        state="code",
        code=shown,
        expires_at=until,
        service=request.service,
        username=request.username,
    )


def _connect_ended(session: link.Session, ended: connecting.Ended) -> NoReturn:
    """Log how the request ended, with the other account's signed ``from`` for a mismatch and
    nothing else from the room, then refuse with its code."""
    fields: dict[str, str] = {"code": ended.code, "why": ended.why}
    if ended.sender:
        fields["sender"] = ended.sender
    session.audit.write("connect", event="ended", **fields)
    raise _Refused(ended.code, str(ended))


def _connect_begin(dirs: paths.Dirs, wanted: host_state.HostConfig, out: _Out) -> None:
    """``pair begin``: ask the service and the user's own username, draw a code, post the
    hello, and wait for Lucille's answer."""
    session, opened, current = _pairing_session(dirs, wanted)
    service = _read_line("Chat service (telegram): ", out).strip(" \n")
    if service not in connect.SERVICES:
        if service.lower().startswith(_PAIRING_CODE_PREFIX):
            # A pairing code piped in, as the paste form took one: say that form is gone, and
            # never repeat the code.
            raise _Refused("bad_service", PASTE_IS_GONE)
        raise _Refused(
            "bad_service", f"Capsule connects through {', '.join(sorted(connect.SERVICES))} only"
        )
    entry = _read_line(f"Your username on {_SERVICE_NAMES[service]}: ", out).rstrip("\n")
    username = connect.normalize_username(entry, service)
    if username is None:
        raise _Refused(
            "bad_username",
            "that is not a username: it starts with a letter, and holds letters, digits and _ "
            "only, 64 at most",
        )
    if wanted != current:
        host_state.save_host_config(dirs, wanted)
    try:
        live = connecting.begin(
            session.link,
            dirs.connect_file,
            service=service,
            username=username,
            hub=wanted.hub or _switchboard.LUCILLE_HUB,
            now=session.link.now,
        )
    except _switchboard.HubError as exc:
        raise _Refused("hub_error", f"{exc}: no Connect request was kept; try again") from None
    session.audit.write("connect", event="began", epoch=live.request.epoch)
    _connect_code(live.request, out)
    _connect_wait(session, live, dirs, wanted, opened, current, out)


def _connect_resume(dirs: paths.Dirs, out: _Out) -> None:
    """``pair resume``: wait again on the request ``pair begin`` kept."""
    request = connecting.load(dirs.connect_file)
    if request is None:
        raise _Refused(
            "none", "no Connect request is waiting: `lucille-host pair begin` starts one"
        )
    current = host_state.load_host_config(dirs)
    session, opened, current = _pairing_session(dirs, current)
    live = connecting.open_request(request)
    _connect_code(live.request, out)
    _connect_wait(session, live, dirs, current, opened, current, out)


def _connect_delivered(request: connecting.Request, out: _Out) -> None:
    out.say(
        "delivered: Lucille's answer has arrived; `lucille-host pair resume` connects",
        "connect",
        state="delivered",
        service=request.service,
    )


def _connect_status(session: link.Session, dirs: paths.Dirs, out: _Out) -> bool:
    """``pair status`` with a Connect request and no pair: one read of the drop room under the
    pair lock, which records what it saw and redeems, posts and spends nothing (a request that
    ended is reported, and left for ``pair resume`` or ``pair begin``); or, while ``pair begin``
    or ``pair resume`` holds the lock, what the file says. The request is read under the lock,
    so that one a ``begin`` or ``resume`` answered or ended meanwhile is never written back.
    False when there is no request any more."""
    lock = paths.ensure_dir(dirs.state) / PAIR_LOCK
    try:
        with loop.one_at_a_time(lock, ALREADY_PAIRING, "`pair status`"):
            request = connecting.load(dirs.connect_file)
            if request is None:
                return False
            live = connecting.open_request(request)
            try:
                step = connecting.poll(
                    session.link,
                    dirs.connect_file,
                    live,
                    now=session.link.now,
                    spend=False,
                    keys=connecting.trusted_keys(request, host_state.load_host_config(dirs)),
                )
            except connecting.Ended as ended:
                # Reported, not ended here: the request is left as it is.
                raise _Refused(ended.code, str(ended)) from None
            if step.found is not None:
                _connect_delivered(step.live.request, out)
            else:
                _connect_code(step.live.request, out)
            return True
    except loop.Failed as exc:
        if exc.code != "busy":
            raise
    request = connecting.load(dirs.connect_file)
    if request is None:
        return False
    if request.delivery_seen:
        _connect_delivered(request, out)
    else:
        _connect_code(request, out)
    return True


def _connect_cancel(dirs: paths.Dirs, out: _Out) -> None:
    """``pair cancel``: drop the request; its code connects nothing from now on."""
    request = connecting.load(dirs.connect_file)
    if request is None:
        raise _Refused("none", "no Connect request is waiting")
    connecting.remove(dirs.connect_file)
    out.say(
        "cancelled: the connect code no longer connects this computer", "connect", state="cancelled"
    )


def _connect_wait(
    session: link.Session,
    live: connecting.Live,
    dirs: paths.Dirs,
    wanted: host_state.HostConfig,
    opened: host_state.HostConfig,
    current: host_state.HostConfig,
    out: _Out,
) -> None:
    """Read the drop room every :data:`connecting.POLL_EVERY` seconds until a delivery passes,
    then answer its code as ``pair`` answers one. Ctrl-C keeps the request."""
    path = dirs.connect_file
    try:
        while True:
            try:
                step = connecting.poll(
                    session.link,
                    path,
                    live,
                    now=session.link.now,
                    keys=connecting.trusted_keys(live.request, wanted),
                )
            except connecting.Ended as ended:
                _connect_ended(session, ended)
            live = step.live
            for why in step.ignored:
                _warn(f"a post in the drop room was ignored: {why}")
            if step.found is not None:
                break
            time.sleep(connecting.POLL_EVERY)
    except KeyboardInterrupt:
        raise link.Stopped(
            "interrupted", "the Connect request is kept; `lucille-host pair resume` waits again"
        ) from None
    found = step.found
    try:
        code = session.check_code(found.code, wanted)
    except link.Stopped:
        connecting.remove(path)
        raise
    try:
        live = connecting.redeemable(path, live, found, code, now=session.link.now)
    except connecting.Ended as ended:
        _connect_ended(session, ended)
    out.say(
        f"connecting: as {found.handle} on {_SERVICE_NAMES[live.request.service]}",
        "connect",
        state="connecting",
        service=live.request.service,
        handle=found.handle,
        **{"from": found.sender},
    )
    _answered(
        session,
        code,
        dirs,
        wanted,
        opened,
        current,
        out,
        nonce=live.nonce,
        ask=not out.as_json,
        answered=lambda: connecting.remove(path),
    )


def _stdin_fd() -> int:
    """Standard input's descriptor, or -1 when there is none: it ended before ``run`` started."""
    try:
        return sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return -1


class _EndedEarly(Exception):
    """The lifeline ended before ``run`` started serving."""


def _serve(
    args: argparse.Namespace,
    events: _Events | None,
    lifeline: loop.Lifeline | None,
    made: list[loop.Runner],
    restores: list[Callable[[], None]],
) -> tuple[str, dict[str, Any], str]:
    """``run``'s start checks, in the order spec/CLI.md section 13.3 gives, each before the next
    can write or read anything; then the runner. Returns the reason ``run`` stopped for, the
    ``stopped`` object's other fields, and the text line; raises for any other end. From the
    profile's name on, the first SIGTERM, SIGHUP or Ctrl-C drains, until ``run`` has written its
    last object: it stops the start checks between two of them, as the end of the lifeline does,
    and ``restores`` gets what puts the previous handlers back."""

    def ended() -> None:
        if controls.draining.is_set() or (lifeline is not None and lifeline.ended.is_set()):
            raise _EndedEarly

    say = _say if events is None else (lambda _line: None)
    try:
        dirs = _dirs(args.profile)
        # Nothing is written for it, and before ``run`` serves a signal only drains.
        controls = Controls(dirs.state)
        if threading.current_thread() is threading.main_thread():
            restores.append(controls.install_handlers(kill=False))
        if lifeline is not None:
            lifeline.attach(controls.drain)
        config = host_state.load_host_config(dirs)
        host_state.startup_checks()
        ended()
        with loop.one_run(dirs.state):
            ended()
            loop.check_version(dirs, allow=args.allow_downgrade, warn=_warn_quietly)
            ended()
            session = link.open_session(dirs, config, say=say, warn=_warn_quietly)
            ended()
            runner = loop.Runner(
                dirs,
                session,
                controls=controls,
                say=say,
                tell=None if events is None else events.tell,
                lifeline=lifeline,
                interpreter=host_interpreter(),
            )
            made.append(runner)
            said = runner.serve()
    except _EndedEarly:
        cause = controls.drain_cause or (lifeline.cause if lifeline is not None else None)
        return "drained", {"cause": cause or "signal"}, "drained: run stopped"
    reason = runner.outcome or "drained"
    fields = {"cause": controls.drain_cause or "signal"} if reason == "drained" else {}
    return reason, fields, said


def _failed(code: str, line: str) -> tuple[str, dict[str, Any], str | None]:
    """``stopped`` ``failed`` with ``code``, and the line stderr shows. A code ``run`` does not
    give (:data:`RUN_FAILURES`) is ``internal``: an error ``run`` did not expect, which, as every
    ``internal``, shows its traceback in place of a line (spec/CLI.md section 13.3)."""
    if code == "internal" or code not in RUN_FAILURES:
        return "failed", {"code": "internal"}, None
    return "failed", {"code": code}, line


def _run_failure(exc: Exception, ready: bool) -> tuple[str, dict[str, Any], str | None]:
    """Why ``run`` stopped, for an exception: its reason, the ``stopped`` object's other fields,
    and the line stderr shows (None for a bug, whose traceback is shown)."""
    if isinstance(exc, loop.Downgrade):
        return "downgrade", {"last_version": exc.recorded}, f"lucille-host: {exc}"
    if not isinstance(exc, _FAILURES):
        return "failed", {"code": "internal"}, None
    line = _failure(exc)[0]
    if isinstance(exc, link.Stopped):
        if exc.code == "none":
            return ("forgotten" if ready else "not_paired"), {}, line
        reason = _STOPPED_REASONS.get(exc.code)
        if reason is not None:
            return reason, {}, line
        return _failed(exc.code, line)
    if isinstance(exc, loop.Failed):
        if exc.code == "busy":
            return "already_running", {}, line
        code = exc.code
        if code == "internal" and exc.__cause__ is not None:
            cause = exc.__cause__
            code = next((name for kind, name in FAILURE_CODES if isinstance(cause, kind)), code)
        return _failed(code, line)
    if isinstance(exc, _switchboard.PairFailed):
        return "pair_unusable", {}, line
    if isinstance(exc, store.InsecureKeyring):
        return "insecure_keyring", {}, line
    if isinstance(exc, host_state.Refused):
        return "startup_refused", {}, line
    if isinstance(exc, store.StoreError):
        if ready:
            return _failed("store_error", line)
        return "store_error", {}, line
    return _failed(_failure(exc)[1], line)


def _run(args: argparse.Namespace) -> int:
    """``lucille-host run``: its start checks, then the runner (``loop.py``). It ends with a
    ``stopped`` object under ``--json-events``, and returns its exit code (:data:`RUN_EXITS`); an
    unexpected error is raised once ``stopped`` is written, so that its traceback is shown."""
    taken = _Stdout() if args.json_events else None
    events = None if taken is None else _Events(taken.stream)
    raised: Exception | None = None
    restores: list[Callable[[], None]] = []
    try:
        lifeline = None
        root = _is_root()
        if args.exit_on_stdin_eof and not root:
            lifeline = loop.Lifeline(_stdin_fd())
            if events is not None:
                cut = lifeline.cut
                events.on_broken = lambda: cut(loop.STDOUT_GONE)
            lifeline.start()
        made: list[loop.Runner] = []
        try:
            if root:
                # Before the profile is resolved, a handler installed or a file opened.
                raise host_state.Refused(ROOT_REFUSED)
            reason, fields, said = _serve(args, events, lifeline, made, restores)
            line = None
        except Exception as exc:  # noqa: BLE001 - every end of run is reported, then judged
            ready = bool(made) and made[0].ready_sent
            reason, fields, line = _run_failure(exc, ready)
            said = None
            if line is None:
                raised = exc
        if line is not None:
            _err(line)
        if events is not None:
            events.close("stopped", reason=reason, **fields)
            events.finish(EVENTS_FLUSH)
        elif said:
            print(said, flush=True)
    finally:
        if taken is not None and events is not None:
            taken.give_back(broken=events.broken, written=events.finish(0))
        for restore in restores:
            restore()
    if raised is not None:
        raise raised
    return RUN_EXITS[reason]


def _status(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    config = host_state.load_host_config(dirs)
    failure: BaseException | None = None
    session: link.Session | None = None
    try:
        session = link.open_session(dirs, config, say=_say, warn=_warn)
    except (store.InsecureKeyring, store.StoreError) as exc:
        # A store that cannot be read is reported in the pair, and the rest as it is.
        failure = exc
    lines, found = report.collect(dirs, session, config, failure=failure)
    if out.as_json:
        out.obj("status", **found)
        return
    for line in lines:
        print(line)


def _log(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    if args.limit < 1:
        raise _Refused("bad_limit", "--limit takes a number of lines, 1 or more")
    lines = audit.tail(dirs.state, args.limit)
    if not lines:
        print(
            f"lucille-host: the audit log has no lines yet ({dirs.state / audit.FILE_NAME})",
            file=sys.stderr,
        )
    for line in lines:
        out.say(line, "log_line", text=line)


def _forget(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    session = link.open_session(dirs, host_state.load_host_config(dirs), say=_say, warn=_warn)
    gone = session.leave_left_over()
    if gone is not None:
        out.say(
            f"left: pair {gone} was gone from this computer already, and the work rooms still "
            "held over it are left now",
            "forget",
            state="left",
            name=gone,
        )
        return
    told = session.forget()
    out.say(
        f"forgotten: pair {told.name} is forgotten on this computer, its work rooms left first; "
        + link.told_sentence(told),
        "forget",
        state="forgotten",
        name=told.name,
        lucille_told=told.told,
    )


# What ``uninstall-data`` says while another command of the profile holds a lock it takes.
UNINSTALL_RUNNING: Final = (
    "`lucille-host run` is running for this profile: stop it first, then uninstall its data"
)
UNINSTALL_PAIRING: Final = (
    "another `lucille-host pair` is running for this profile: let it end, or stop it, first"
)
UNINSTALL_HELD: Final = (
    "this profile holds a pair ({name}): disconnect first: run `forget`, which tells Lucille "
    "when the hub can be reached. Nothing was removed"
)
UNINSTALL_ROOMS: Final = (
    "this profile still holds work rooms over pair {name}, which is gone from this computer: "
    "run `forget`, which leaves them. Nothing was removed"
)


def _uninstall_data(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    """``uninstall-data --yes`` (decisions/0002 section 17): delete this profile's keyring items,
    then its folders. Under the profile's run lock, its pairing lock and its policy lock, so that
    no ``run``, ``pair`` or policy change of the profile works on what is being deleted; and only
    once the profile holds no pair and no work room, so that a pair is never dropped here without
    ``forget``, which tells Lucille when it can. A profile with nothing left is no failure: the verb
    can be run again after one that stopped part way."""
    host_state.startup_checks()
    state_dir = paths.ensure_dir(dirs.state)
    with (
        loop.one_at_a_time(state_dir / loop.RUN_LOCK, UNINSTALL_RUNNING, "`uninstall-data`"),
        loop.one_at_a_time(state_dir / PAIR_LOCK, UNINSTALL_PAIRING, "`uninstall-data`"),
        _policy_lock(dirs),
    ):
        session = link.open_session(dirs, host_state.load_host_config(dirs), say=_say, warn=_warn)
        held = session.held()
        if held:
            raise _Refused("held", UNINSTALL_HELD.format(name=", ".join(held)))
        record = rooms.load(dirs.rooms_file)
        if record.pair is not None and (record.held or record.accepting is not None):
            raise _Refused("held", UNINSTALL_ROOMS.format(name=record.pair))
        store.delete_items(dirs.profile)
        folders = [dirs.state, dirs.config]
        for folder in folders:
            if folder.exists() or folder.is_symlink():
                shutil.rmtree(folder)
    out.say(
        f"uninstalled: profile {dirs.profile}'s keyring items and folders are removed from this "
        "computer",
        "uninstalled",
        profile=dirs.profile,
    )


def _controls(verb: str, dirs: paths.Dirs, out: _Out | None = None) -> None:
    if verb not in ("pause", "resume", "kill"):
        raise _Refused("internal", f"{verb} is not a control")
    controls = Controls(paths.ensure_dir(dirs.state))
    if verb == "resume":
        locked = controls.resume()
        message = "resumed"
    elif verb == "pause":
        locked = controls.pause()
        message = "paused: the host starts nothing until `lucille-host resume`"
    else:
        locked = controls.kill()
        message = "paused, and asked the running host to kill the operation in flight, if any"
    if not locked:
        print(
            "lucille-host: note: this filesystem cannot lock, so this ran without the controls' "
            "lock; the order of the writes still keeps a kill request from standing without the "
            "pause flag",
            file=sys.stderr,
        )
    (out or _Out(False)).say(message, "controls", paused=controls.paused())


def _ops_verb(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    with contextlib.nullcontext() if args.ops_verb == "list" else _policy_lock(dirs):
        _ops(args, dirs, out)


def _roots_verb(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    # Listing, checking and suggesting change no setting, and take no lock: they write nothing but
    # the run record's line, which main writes for every command.
    reads = args.roots_verb in ("list", "check") or (
        args.roots_verb == "add" and args.suggest_alias is not None
    )
    with contextlib.nullcontext() if reads else _policy_lock(dirs):
        _roots(args, dirs, out)


def _read_value_line() -> str:
    """One line of stdin. A line that is not text in stdin's encoding is refused, without the
    decoder's error, which names the bytes it could not decode: bytes of the value."""
    try:
        return sys.stdin.readline()
    except UnicodeDecodeError:
        pass
    # Raised outside the except clause, so that the decoder's error is not its context.
    raise _Refused(
        "bad_value", "the value is not text in this terminal's encoding; nothing was saved"
    )


def _read_hidden(prompt: str) -> str:
    """One line typed at the terminal on stdin, with the terminal's echo off while it is typed,
    after a prompt on stderr; what was typed ahead of the prompt is discarded. EOFError at the end
    of stdin."""
    descriptor = sys.stdin.fileno()
    before = termios.tcgetattr(descriptor)
    hidden = termios.tcgetattr(descriptor)
    hidden[3] &= ~termios.ECHO
    print(prompt, end="", file=sys.stderr, flush=True)
    try:
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, hidden)
        line = _read_value_line()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, before)
        print(file=sys.stderr, flush=True)
    if not line:
        raise EOFError
    return line[:-1] if line.endswith("\n") else line


def _credential_value(args: argparse.Namespace, key: str) -> str:
    """The value to save: one line of stdin with ``--value-stdin``, otherwise typed twice at a
    prompt that does not echo it, which needs a terminal."""
    if args.value_stdin:
        if sys.stdin.isatty():
            raise _Refused(
                "no_value",
                "with --value-stdin, standard input is a terminal, where the value would show as "
                "it is typed; leave out --value-stdin to type it at a prompt that hides it",
            )
        line = _read_value_line()
        value = line[:-1] if line.endswith("\n") else line
        if value.endswith("\r"):
            value = value[:-1]
        if not value:
            raise _Refused("no_value", "no value was given on standard input; nothing was saved")
        return value
    if not sys.stdin.isatty():
        raise _Refused(
            "no_value",
            "standard input is not a terminal: run this in a terminal to type the value, or "
            "pass --value-stdin and write it on standard input",
        )
    first = _read_hidden(f"value of {key} (not shown): ")
    if not first:
        raise _Refused("no_value", "no value was typed; nothing was saved")
    again = _read_hidden("the same value again: ")
    if again != first:
        raise _Refused("values_differ", "the two values differ; nothing was saved")
    return first


def _credential(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    """``credential set``, ``list`` and ``remove``. An error none of the verb's own refusals
    names is not shown as it is: its message, its notes, its cause and its context could carry
    a credential's value. :func:`main` shows an error of its own instead, with its traceback,
    which names the place the verb was in and nothing the error said."""
    unexpected = ""
    interrupted = False
    try:
        _credential_verb(args, dirs, out)
    except _FAILURES:
        raise
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        unexpected = type(exc).__name__
    # Each raised again outside the except clause, so that the error caught is not its context:
    # a Ctrl-C while the store handled a keyring's error would otherwise carry that error.
    if interrupted:
        raise KeyboardInterrupt
    if unexpected:
        raise RuntimeError(
            f"an unexpected error ({unexpected}) in the credential verb; its message is not "
            "shown, since it may hold a credential's value"
        )


def _credential_verb(args: argparse.Namespace, dirs: paths.Dirs, out: _Out) -> None:
    """The verb's work. This function is the only place in the runtime, but the module itself,
    that names the credential store (tests/test_credentials.py), so a process that does not run
    this verb never imports it."""
    from lucille_host import credentials  # noqa: PLC0415 - kept out of every other verb

    host_state.startup_checks()
    creds = credentials.CredentialStore(paths.ensure_dir(dirs.state), dirs.profile)
    try:
        if args.credential_verb == "test":
            _credential_test(args, dirs, out, creds.saved(args.service, args.name) is not None)
            return
        if args.credential_verb == "list":
            saved = creds.names()
            if not saved:
                print("lucille-host: no credentials are saved on this computer", file=sys.stderr)
            for each in saved:
                hosts = ", ".join(each.hosts) if each.hosts else "no host (saved without hosts)"
                when = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(each.saved_at))
                out.say(
                    f"{each.service}/{each.name}: sent only to {hosts}; saved {when}",
                    "credential",
                    **_credential_fields(each),
                )
            return
        key = f"{args.service}/{args.name}"
        if args.credential_verb == "remove":
            if not creds.remove(args.service, args.name):
                raise _Refused("no_such_credential", f"no credential {key} is saved")
            out.say(f"removed {key}", "credential_removed", service=args.service, name=args.name)
            return
        # Every check that needs no value comes first, so nothing is typed for a refusal.
        hosts = credentials.hosts_of(args.hosts)
        before = creds.saved(args.service, args.name)
        if before is not None and not args.replace:
            raise credentials.CredentialExists(f"credential {key} is saved already")
        try:
            value = _credential_value(args, key)
        except (EOFError, KeyboardInterrupt) as exc:
            raise _Refused("interrupted", "nothing was saved") from exc
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            # What stdin decodes undecodable bytes to under the C or POSIX locale, or with
            # PYTHONUTF8=1 (surrogateescape): not text, and not to be stored.
            raise _Refused(
                "bad_value", "the value is not text in this terminal's encoding; nothing was saved"
            )
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise _Refused("bad_value", "the value holds a control character; nothing was saved")
        saved = creds.save(args.service, args.name, value, hosts, replace=args.replace)
        out.say(
            f"{'replaced' if before else 'saved'} {key}, to be sent only to "
            f"{', '.join(saved.hosts)}. Only github.read and the gmail operations use saved "
            "credentials: those of the service github, bound to api.github.com, and of the "
            "service gmail, bound to imap.gmail.com.",
            "credential",
            **_credential_fields(saved),
        )
    except credentials.CredentialExists as exc:
        raise _Refused(
            "credential_exists",
            f"{exc}; nothing was changed. Pass --replace to replace its value, and its hosts with "
            "the ones given now",
        ) from exc
    except credentials.CredentialError as exc:
        raise _Refused("bad_credential", str(exc)) from exc


def _credential_fields(saved: Any) -> dict[str, Any]:
    """A ``credential`` object's fields (spec/CLI.md section 6): names, hosts and when it was
    saved, never its value."""
    return {
        "service": saved.service,
        "name": saved.name,
        "hosts": list(saved.hosts),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(saved.saved_at)),
    }


# What `credential test` says for each exit of gmail.check's child (decisions/0003, "Second use").
_TEST_CODES: Final = {
    2: "bad_credential",
    4: "unreachable",
    5: "limit_reached",
    7: "sign_in_refused",
}


def _credential_test(args: argparse.Namespace, dirs: paths.Dirs, out: _Out, saved: bool) -> None:
    """``credential test gmail <name>``: run ``gmail.check``'s child for that credential, which
    signs in to Gmail and out again and reads nothing. It is the user's own act at this computer:
    it runs whether or not ``gmail.check`` is enabled, never because of a request (no request
    handler names this function), and is refused while the host is paused or keyring's own
    settings turn the keyring off. It never shows the value; only the child reads it."""
    from lucille_host import _exec  # noqa: PLC0415 - only this action runs a child

    key = f"{args.service}/{args.name}"
    if args.service != "gmail":
        raise _Refused("no_test", f"{args.service} has no test; only gmail credentials do")
    if not saved:
        raise _Refused("no_such_credential", f"no credential {key} is saved")
    if Controls(dirs.state).paused():
        raise _Refused("paused", "the host is paused, so it starts nothing; resume it first")
    if store.keyring_turned_off():
        raise _Refused(
            "insecure_keyring",
            "keyring's own settings turn the keyring off here, so no credential is read",
        )
    interpreter = host_interpreter()
    if interpreter is None:
        raise _Refused("internal", "the host does not know its own interpreter")
    family = paths.os_family()
    operation = OPERATIONS["gmail.check"]
    test_args = {"credential": args.name}
    if not args_valid(operation, test_args):
        raise _Refused("bad_credential", f"{args.name} is not a credential's name")
    host = {
        HOST_PYTHON: interpreter,
        HOST_STATE: str(paths.ensure_dir(dirs.state)),
        HOST_PROFILE: dirs.profile,
    }
    command = _exec.Command(
        argv=operation.build_argv(family, test_args, host),
        cwd="/",
        timeout_s=operation.timeout(test_args),
        env=operation.child_env(family, os.environ),
    )
    try:
        result = _exec.run(command)
    except _exec.NotStarted as exc:
        raise _Refused("internal", "the test could not be started") from exc
    exit_code = result.exit_code if result.ended == "exited" else None
    code = "ok" if exit_code == 0 else _TEST_CODES.get(exit_code or -1, "internal")
    audit.AuditLog(dirs.state).write(
        "credential_test", service=args.service, name=args.name, code=code
    )
    if code == "ok":
        out.say(
            f"{key}: signed in to imap.gmail.com",
            "credential_test",
            service=args.service,
            name=args.name,
            host="imap.gmail.com",
        )
        return
    line = result.stderr.strip().splitlines()
    said = line[0].removeprefix("lucille-host gmail: ") if line and exit_code != 1 else ""
    said = said or "the test did not finish"
    if exit_code == 2:
        raise _Refused("bad_credential", said)
    if exit_code == 4:
        raise _Refused("unreachable", said)
    if exit_code == 5:
        raise _Refused("limit_reached", said)
    if exit_code == 7:
        raise _Refused("sign_in_refused", said)
    raise _Refused("internal", said)


def _control(verb: str) -> Callable[[argparse.Namespace, paths.Dirs, _Out], None]:
    return lambda args, dirs, out: _controls(verb, dirs, out)


# Every verb the parser knows, and what runs it.
VERBS: Final[dict[str, Callable[..., Any]]] = {
    "pair": _pair,
    # Dispatched on its own, before the other verbs: it has exit codes of its own.
    "run": _run,
    "status": _status,
    "log": _log,
    "forget": _forget,
    "ops": _ops_verb,
    "roots": _roots_verb,
    "policy": _policy_verb,
    "pause": _control("pause"),
    "resume": _control("resume"),
    "kill": _control("kill"),
    "credential": _credential,
    "uninstall-data": _uninstall_data,
}

# What --json calls each failure that carries no code of its own, the first class that matches
# (spec/CLI.md section 10). _Refused, link.Stopped and loop.Failed carry theirs.
FAILURE_CODES: Final = (
    (_switchboard.PairFailed, "pair_unusable"),
    (_switchboard.HubError, "hub_error"),
    (store.InsecureKeyring, "insecure_keyring"),
    (host_state.Refused, "startup_refused"),
    (host_state.StateError, "state_invalid"),
    (host_state.Unprepared, "runtime_folder"),
    (paths.UnsupportedPlatform, "unsupported_platform"),
    (policy.PolicyError, "policy_invalid"),
    (store.StoreError, "store_error"),
    (OSError, "os_error"),
)
_FAILURES: Final = (_Refused, link.Stopped, loop.Failed, *(kind for kind, _ in FAILURE_CODES))


def _failure(exc: BaseException) -> tuple[str, str, str, dict[str, str]]:
    """A failure: the line stderr shows, and the ``error`` object's code, message and other
    fields."""
    if isinstance(exc, link.Stopped):
        return f"{exc.state}: {exc}", exc.code, str(exc), {}
    if isinstance(exc, _switchboard.PairFailed):
        said = link.refusal("the pair cannot be used here", exc)
        return f"error: {said}", "pair_unusable", said, {}
    if isinstance(exc, _switchboard.HubError):
        said = f"{exc}; try again"
        return f"error: {said}", "hub_error", said, {}
    if isinstance(exc, _Refused | loop.Failed):
        code = exc.code
    else:
        code = next(code for kind, code in FAILURE_CODES if isinstance(exc, kind))
    if isinstance(exc, OSError) and not isinstance(exc, _Refused):
        where = f" ({exc.filename})" if exc.filename else ""
        said = f"{exc.strerror or exc}{where}"
    else:
        said = str(exc)
    extra = exc.extra if isinstance(exc, _Refused) else {}
    return f"lucille-host: {said}", code, said, extra


# argparse's usage errors of a credential command line that repeat nothing typed on it: the
# arguments a form needs, by name, and options that need a word after them.
_CREDENTIAL_REQUIRED: Final = "the following arguments are required: "
_CREDENTIAL_ARGUMENTS: Final = frozenset({"service", "name", "--host", "ACTION"})
_CREDENTIAL_USAGE: Final = frozenset(
    {
        "argument --host: expected one argument",
        "with --json, credential set reads the value with --value-stdin",
    }
)


def _usage_said(
    parser: argparse.ArgumentParser,
    raised_by: argparse.ArgumentParser,
    words: Sequence[str],
    message: str,
) -> str:
    """argparse's ``message`` for a usage error, unless the credential verb's parser or one of
    its actions' raised it (``raised_by``), or the line names the verb anywhere, ``--`` or not:
    then only a message that repeats nothing typed on it, since a word there may be a value
    typed by mistake, in any form argparse writes it (quoted, escaped). That is argparse's
    message only where it names the verb's own arguments and options; an unknown action is
    "unknown credential subcommand"; anything else is a message that says where a value given
    on the command line has gone."""
    ours = raised_by in getattr(parser, "credential_parsers", frozenset())
    if not ours and "credential" not in words:
        return message
    if message.startswith("argument ACTION: invalid choice:"):
        return "unknown credential subcommand: it is set, list, remove or test"
    if message in _CREDENTIAL_USAGE or message.startswith(_CREDENTIAL_REQUIRED):
        own = message.removeprefix(_CREDENTIAL_REQUIRED)
        if all(name in _CREDENTIAL_ARGUMENTS for name in own.split(", ")) or own == message:
            return message
    return (
        "the credential command line could not be parsed, and its words are not repeated here, "
        "since one may be a credential's value. A value is never given on the command line: "
        "`credential set` asks for it. If one was typed there, it is in your shell's history now: "
        "remove it there, and change the credential where it was issued"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``lucille-host`` console script and ``python -m lucille_host``."""
    words = list(sys.argv[1:] if argv is None else argv)
    # Before anything else the command does, and never failing it (spec/CLI.md section 16).
    runs.record(words, tuple(VERBS))
    wanted = _wants_json(words)
    parser = build_parser()
    if isinstance(parser, _Parser):
        parser.json_wanted = wanted
    try:
        args, extras = parser.parse_known_args(words)
        if extras:
            # What parse_args says of them, said by the parser of the verb that took them, so
            # that a credential line's are refused as its own (_usage_said).
            forms = getattr(parser, "forms", {})
            credential = getattr(args, "verb", None) == "credential"
            owner = forms["credential"] if credential else parser
            owner.error(f"unrecognized arguments: {' '.join(extras)}")
        _check_usage(parser, args)
    except _Usage as exc:
        _Out(wanted).error("usage", USAGE_MESSAGE)
        said = _usage_said(parser, exc.parser, words, exc.message)
        argparse.ArgumentParser.error(exc.parser, said)
    out = _Out(bool(getattr(args, "json", False)))
    if args.verb is None:
        out.version()
        return 0
    if args.verb == "run":
        return _run(args)
    if _is_root():
        # Before the profile's paths are resolved, a folder made or a lock taken: a refused
        # root run leaves nothing behind (decisions/0004-running-as-root.md, 4.2).
        said, code, message, extra = _failure(host_state.Refused(ROOT_REFUSED))
        print(said, file=sys.stderr)
        out.error(code, message, **extra)
        return 1
    try:
        dirs = _dirs(args.profile)
        verb = VERBS.get(args.verb)
        if verb is None:
            raise _Refused("internal", f"{args.verb} is not a verb this release has")
        verb(args, dirs, out)
    except _FAILURES as exc:
        said, code, message, extra = _failure(exc)
        print(said, file=sys.stderr)
        out.error(code, message, **extra)
        return 1
    except Exception:
        # A bug: the object, then the traceback, as Python prints it, and exit 1.
        out.error("internal", INTERNAL_MESSAGE)
        raise
    return 0
