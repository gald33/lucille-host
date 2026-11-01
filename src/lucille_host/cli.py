"""The ``lucille-host`` command.

With no arguments, or with ``--version``, it prints ``lucille-host <version>`` and exits 0. The
verbs:

- ``pair [CODE]``: answer the pairing code Lucille shows, wait for Lucille to collect the answer,
  then show the pair's SAS and ask for ``y`` if Lucille's page shows the same (``link.py``,
  ``gate.py``): ``y`` confirms, ``n`` forgets the pair, and the end of stdin leaves the answer to
  ``pair confirm`` or ``pair reject``. What was typed at a terminal before the question is
  discarded, unanswered. The code is read from stdin when it is not given, and only
  from stdin with ``--code-stdin``, which also stops once the SAS is shown (``sas_pending``).
  ``--hub`` names a loopback hub to accept codes for, and ``--store-file`` keeps the pair in files
  rather than the OS keyring; both are saved for the profile. ``pair`` refuses while a pair is
  held, and while another ``pair`` of the profile runs: it holds ``state/.pair.lock``, taken
  without waiting, until it returns. It resets the policy to the fresh one before it answers a new
  code, keeping the earlier one aside;
- ``pair status``, ``pair confirm`` and ``pair reject``: where the pair is (showing the SAS again
  while it waits to be confirmed), the ``y`` given later, and forgetting a pair that has not been
  confirmed;
- ``run``: the long-running host (``loop.py``): it keeps the pair, takes the work rooms Lucille
  offers over it, and reads and answers Lucille's requests there, once the local gate is open.
  The first SIGTERM, SIGHUP or Ctrl-C drains it; it also stops, exiting 0, when the pair ends.
  ``--json-events`` writes its events on stdout, ``--exit-on-stdin-eof`` drains it when stdin
  ends, and ``--allow-downgrade`` starts a release older than one that ran on the profile
  before. It has exit codes of its own (:data:`RUN_EXITS`, spec/CLI.md section 13);
- ``status``: the version, the local policy, the pair, the work rooms, and what ``run`` last saw
  of the hub and the clock, read from the profile's files with no hub call (``report.py``);
- ``log [--limit N]``: the last lines of the audit log, oldest first, across its rotated files;
- ``forget``: leave the pair's work rooms, forget the pair here, and tell Lucille if the hub can
  be reached; it says whether Lucille was told. When another command forgot the pair while this
  one waited for the record lock, it says the pair is gone (``none``), as with no pair at all.
  With no pair held, it leaves the work rooms still held over a pair already gone from this
  computer, and tells Lucille nothing (``left``);
- ``ops list|enable|disable``: which catalog operations the local policy enables;
- ``roots list|add|remove|check``: which folders the operations may work in, by alias.
  ``roots add --suggest-alias PATH`` suggests an alias for a folder and changes nothing; ``roots
  check`` says whether the host can open each folder and read an entry in it;
- ``policy previous``: the operations and folders of the last policy a pairing set aside (a
  pairing sets aside only a policy that is not the fresh one); ``policy reset``: set an invalid
  policy file aside, and start from the fresh policy;
- ``pause``, ``resume`` and ``kill``: the local controls.

Each verb is dispatched by name; one with no handler is refused, never run as another verb.
``--profile <name>`` selects a separate config and state directory, so that two computers can be
simulated on one machine. A verb that fails prints why on stderr and exits 1; a pairing that ends
early, or a refusal of the pairing verbs, prints its state word first (``leaked: ...``,
``expired: ...``).

``--json``, anywhere before a ``--``, makes every verb but ``run`` write on stdout the JSON objects
of ``spec/CLI.md`` instead of its text, one per line, with the same exit codes; stderr stays as it
is. A failure ends with an ``error`` object, and a usage error writes one too. Each verb writes its
text and its objects at the same points, from the same values, so that they say the same. With
``--json``, ``pair`` takes the code only on stdin, with ``--code-stdin``, and asks nothing.
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
import sys
import termios
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Final, NoReturn

from lucille_host import (
    __version__,
    _switchboard,
    audit,
    gate,
    link,
    loop,
    paths,
    policy,
    report,
    store,
)
from lucille_host import state as host_state
from lucille_host.containment import inside_own, on_proc
from lucille_host.controls import Controls
from lucille_host.ops import OPERATIONS

# The words ``pair`` takes in place of a code.
PAIR_ACTIONS: Final = ("status", "confirm", "reject")
# In the state folder: held by ``pair [CODE]`` until it returns.
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
        help="only suggest an alias for the folder PATH, which no folder has yet; change nothing",
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
        help="pair with Lucille: answer its code, then confirm the SAS both sides show",
        description=(
            "`pair [CODE]` answers the pairing code Lucille shows, then shows the pair's SAS and "
            "asks for y if Lucille's page shows the same; `pair status` says where the pair is, "
            "and shows the SAS again while it waits; `pair confirm` confirms the SAS shown; "
            "`pair reject` forgets a pair that is not confirmed."
        ),
    )
    _json_option(pair)
    pair.add_argument(
        "code",
        nargs="?",
        metavar="CODE | status | confirm | reject",
        help="the pairing code (read from stdin when it is not given), or one of the actions",
    )
    pair.add_argument(
        "--code-stdin",
        action="store_true",
        help="read the code from stdin only, and stop once the SAS is shown",
    )
    pair.add_argument(
        "--hub",
        metavar="URL",
        help="also accept codes for this hub: http://127.0.0.1:<port> or http://localhost:<port>",
    )
    pair.add_argument(
        "--store-file",
        action="store_true",
        help="keep the pair in files in the state folder, not the OS keyring (weaker)",
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
            "clock. It reads this computer's files only, and makes no hub call."
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

    _json_option(verbs.add_parser("pause", help="start nothing until resume; refuse new requests"))
    _json_option(verbs.add_parser("resume", help="clear pause and any kill request"))
    _json_option(verbs.add_parser("kill", help="kill the operation in flight, and pause"))
    parser.forms = {"run": run, "pair": pair, "roots add": add}
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
    if args.verb == "pair" and as_json and args.code is None and not args.code_stdin:
        forms["pair"].error("with --json, pair reads the code with --code-stdin")
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
            "an alias is lowercase letters, digits, '-' and '_', starting with a letter",
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
    It takes no lock, opens no session, and sends and writes nothing."""
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
    if args.code_stdin or args.hub is not None or args.store_file:
        raise _Refused("bad_options", f"`pair {args.code}` takes no options")
    session = link.open_session(dirs, host_state.load_host_config(dirs), say=_say, warn=_warn)
    if args.code == "status":
        view = session.status_view()
        word, detail = link.pair_words(view)
        out.say(f"{word}: {detail}", "pair", **_pair_view(view))
        return
    if args.code == "reject":
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
    if args.code in PAIR_ACTIONS:
        _pair_action(args, dirs, out)
        return
    if args.code_stdin and args.code is not None:
        raise _Refused(
            "code_in_argv", "with --code-stdin the code is read from stdin only; do not pass it"
        )
    if out.as_json and args.code is not None:
        raise _Refused(
            "code_in_argv",
            "with --json the code is read from stdin only, with --code-stdin; do not pass it",
        )
    try:
        wanted = host_state.HostConfig(hub=args.hub, store="file" if args.store_file else "keyring")
    except ValueError as exc:
        raise _Refused("bad_hub", str(exc)) from exc
    host_state.startup_checks()
    # One `pair` of the profile at a time, from before it looks for a pair held until it returns,
    # its question included: two at once could each answer a code. `pair status`, `pair confirm`
    # and `pair reject` take no such lock, so that the SAS can be confirmed in another shell.
    lock = paths.ensure_dir(dirs.state) / PAIR_LOCK
    with loop.one_at_a_time(lock, ALREADY_PAIRING, "`pair`"):
        _answer(args, dirs, wanted, out)


def _answer(
    args: argparse.Namespace, dirs: paths.Dirs, wanted: host_state.HostConfig, out: _Out
) -> None:
    """``pair [CODE]``, under the profile's pair lock: answer a code, wait for Lucille to collect
    the answer, show the SAS and ask about it."""
    current = host_state.load_host_config(dirs)
    try:
        session = link.open_session(dirs, current, say=_say, warn=_warn)
        opened = current
    except store.InsecureKeyring:
        if wanted.store != "file":
            raise
        # A keyring that stores nothing holds no pair: only the file store is looked in.
        session = link.open_session(dirs, wanted, say=_say, warn=_warn)
        opened = wanted
    if session.held():
        raise _Refused(
            "held",
            "Disconnect first: this computer already holds a pair (`lucille-host pair reject` "
            "forgets one that is not confirmed yet)",
        )
    text = args.code
    if text is None:
        text = _read_line("Paste the pairing code Lucille shows: ", out).strip()
        if not text:
            raise _Refused("no_code", "no pairing code was given on stdin")
    code = session.check_code(text, wanted)
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
    name = session.accept(code)
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
    if args.code_stdin:
        return
    try:
        out.text(_ask(session, name, found.deadline, out))
    except KeyboardInterrupt:
        raise link.Stopped(
            "interrupted",
            f"pair {name} is kept; `lucille-host pair status` shows its SAS again, and "
            "`lucille-host pair confirm` confirms it",
        ) from None


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
) -> tuple[str, dict[str, Any], str]:
    """``run``'s start checks, in the order spec/CLI.md section 13.3 gives, each before the next
    can write or read anything; then the runner. Returns the reason ``run`` stopped for, the
    ``stopped`` object's other fields, and the text line; raises for any other end."""

    def ended() -> None:
        if lifeline is not None and lifeline.ended.is_set():
            raise _EndedEarly

    say = _say if events is None else (lambda _line: None)
    try:
        dirs = _dirs(args.profile)
        config = host_state.load_host_config(dirs)
        host_state.startup_checks()
        ended()
        with loop.one_run(dirs.state):
            ended()
            loop.check_version(dirs, allow=args.allow_downgrade, warn=_warn_quietly)
            ended()
            session = link.open_session(dirs, config, say=say, warn=_warn_quietly)
            ended()
            controls = Controls(paths.ensure_dir(dirs.state))
            runner = loop.Runner(
                dirs,
                session,
                controls=controls,
                say=say,
                tell=None if events is None else events.tell,
                lifeline=lifeline,
            )
            made.append(runner)
            said = runner.serve()
    except _EndedEarly:
        assert lifeline is not None
        return "drained", {"cause": lifeline.cause}, "drained: run stopped"
    reason = runner.outcome or "drained"
    fields = {"cause": controls.drain_cause or "signal"} if reason == "drained" else {}
    return reason, fields, said


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
        code = exc.code if exc.code in RUN_FAILURES else "internal"
        return "failed", {"code": code}, line
    if isinstance(exc, loop.Failed):
        if exc.code == "busy":
            return "already_running", {}, line
        code = exc.code
        if code == "internal" and exc.__cause__ is not None:
            cause = exc.__cause__
            code = next((name for kind, name in FAILURE_CODES if isinstance(cause, kind)), code)
        if code == "internal":
            return "failed", {"code": "internal"}, None
        return "failed", {"code": code if code in RUN_FAILURES else "internal"}, line
    if isinstance(exc, _switchboard.PairFailed):
        return "pair_unusable", {}, line
    if isinstance(exc, store.InsecureKeyring):
        return "insecure_keyring", {}, line
    if isinstance(exc, host_state.Refused):
        return "startup_refused", {}, line
    if isinstance(exc, store.StoreError):
        if ready:
            return "failed", {"code": "store_error"}, line
        return "store_error", {}, line
    code = _failure(exc)[1]
    return "failed", {"code": code if code in RUN_FAILURES else "internal"}, line


def _run(args: argparse.Namespace) -> int:
    """``lucille-host run``: its start checks, then the runner (``loop.py``). It ends with a
    ``stopped`` object under ``--json-events``, and returns its exit code (:data:`RUN_EXITS`); an
    unexpected error is raised once ``stopped`` is written, so that its traceback is shown."""
    taken = _Stdout() if args.json_events else None
    events = None if taken is None else _Events(taken.stream)
    raised: Exception | None = None
    try:
        lifeline = None
        if args.exit_on_stdin_eof:
            lifeline = loop.Lifeline(_stdin_fd())
            if events is not None:
                cut = lifeline.cut
                events.on_broken = lambda: cut(loop.STDOUT_GONE)
            lifeline.start()
        made: list[loop.Runner] = []
        try:
            reason, fields, said = _serve(args, events, lifeline, made)
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
    # Listing, checking and suggesting change nothing, and take no lock.
    reads = args.roots_verb in ("list", "check") or (
        args.roots_verb == "add" and args.suggest_alias is not None
    )
    with contextlib.nullcontext() if reads else _policy_lock(dirs):
        _roots(args, dirs, out)


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
        said = f"the pair cannot be used here ({exc.state})"
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


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``lucille-host`` console script and ``python -m lucille_host``."""
    words = list(sys.argv[1:] if argv is None else argv)
    wanted = _wants_json(words)
    parser = build_parser()
    if isinstance(parser, _Parser):
        parser.json_wanted = wanted
    try:
        args = parser.parse_args(words)
        _check_usage(parser, args)
    except _Usage as exc:
        _Out(wanted).error("usage", USAGE_MESSAGE)
        argparse.ArgumentParser.error(exc.parser, exc.message)
    out = _Out(bool(getattr(args, "json", False)))
    if args.verb is None:
        out.version()
        return 0
    if args.verb == "run":
        return _run(args)
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
