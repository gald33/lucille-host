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
  The first SIGTERM, SIGHUP or Ctrl-C drains it; it also stops, exiting 0, when the pair ends;
- ``status``: the version, the local policy, the pair, the work rooms, and what ``run`` last saw
  of the hub and the clock, read from the profile's files with no hub call (``report.py``);
- ``log [--limit N]``: the last lines of the audit log, oldest first, across its rotated files;
- ``forget``: leave the pair's work rooms, forget the pair here, and tell Lucille if the hub can
  be reached; it says whether Lucille was told. When another command forgot the pair while this
  one waited for the record lock, it says the pair is gone (``none``), as with no pair at all.
  With no pair held, it leaves the work rooms still held over a pair already gone from this
  computer, and tells Lucille nothing (``left``);
- ``ops list|enable|disable``: which catalog operations the local policy enables;
- ``roots list|add|remove``: which folders the operations may work in, by alias;
- ``policy previous``: the operations and folders of the last policy a pairing set aside (a
  pairing sets aside only a policy that is not the fresh one);
- ``pause``, ``resume`` and ``kill``: the local controls.

Each verb is dispatched by name; one with no handler is refused, never run as another verb.
``--profile <name>`` selects a separate config and state directory, so that two computers can be
simulated on one machine. A verb that fails prints why on stderr and exits 1; a pairing that ends
early, or a refusal of the pairing verbs, prints its state word first (``leaked: ...``,
``expired: ...``).

A verb that changes the policy holds the profile's policy lock from reading the file to writing
it, so that two verbs run at once do not lose a change; on a filesystem that cannot lock, it
refuses, and the policy file can be edited by hand. ``pause``, ``resume`` and ``kill`` hold the
controls' lock the same way, and go ahead without it where it cannot be had (``controls.py``).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import termios
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Final

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


class _Refused(Exception):
    """A verb that cannot do what it was asked; the message says why."""


def version_line() -> str:
    """The line ``lucille-host`` and ``lucille-host --version`` print."""
    return f"lucille-host {__version__}"


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for the ``lucille-host`` command."""
    parser = argparse.ArgumentParser(
        prog="lucille-host",
        description=(
            "Runs a closed set of named operations for a paired Lucille account: `pair` pairs "
            "this computer with Lucille, and `run` answers Lucille's requests, within the "
            "catalog, the local policy and the local controls."
        ),
    )
    parser.add_argument("--version", action="version", version=version_line())
    parser.add_argument(
        "--profile",
        default=paths.DEFAULT_PROFILE,
        help="use a separate config and state directory (default: %(default)s)",
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    ops = verbs.add_parser("ops", help="list, enable or disable catalog operations")
    ops_verbs = ops.add_subparsers(dest="ops_verb", metavar="ACTION", required=True)
    ops_verbs.add_parser("list", help="show every catalog operation and whether it is enabled")
    for action in ("enable", "disable"):
        sub = ops_verbs.add_parser(action, help=f"{action} one operation")
        sub.add_argument("op", help="the operation's name, e.g. files.sha256")

    roots = verbs.add_parser("roots", help="list, add or remove folder aliases")
    roots_verbs = roots.add_subparsers(dest="roots_verb", metavar="ACTION", required=True)
    roots_verbs.add_parser("list", help="show each folder alias and its folder")
    add = roots_verbs.add_parser("add", help="let operations work in a folder, under an alias")
    add.add_argument("alias", help="the name Lucille will use: lowercase, at most 32 characters")
    add.add_argument("path", help="the folder")
    add.add_argument(
        "--allow-home-or-root",
        action="store_true",
        help="needed when the folder is your home folder, holds it, or is /",
    )
    remove = roots_verbs.add_parser("remove", help="stop operations working in a folder")
    remove.add_argument("alias")

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

    verbs.add_parser(
        "run",
        help="answer Lucille's requests over the pair until stopped (Ctrl-C drains)",
        description=(
            "Keeps the pair, takes the work rooms Lucille offers over it, and runs the requests "
            "it posts there that the local policy allows. Nothing is accepted, posted or run "
            "before the SAS is confirmed. The first Ctrl-C, SIGTERM or SIGHUP lets the running "
            "operation finish and stops; a second Ctrl-C kills it."
        ),
    )

    verbs.add_parser(
        "status",
        help="show the policy, the pair, the work rooms and what `run` last saw (no hub call)",
        description=(
            "Shows the version, the local policy and the operations it enables, the controls, "
            "where the pair is kept and the hub, the pair (never its SAS), the work rooms held, "
            "and what `run` last saw: whether it runs, its last contact with the hub, and the "
            "clock. It reads this computer's files only, and makes no hub call."
        ),
    )
    log = verbs.add_parser("log", help="show the last lines of the audit log, oldest first")
    log.add_argument(
        "--limit",
        type=int,
        default=LOG_LINES,
        metavar="N",
        help="how many lines (default: %(default)s)",
    )
    verbs.add_parser(
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

    policy_verb = verbs.add_parser("policy", help="show the last policy a pairing set aside")
    policy_verbs = policy_verb.add_subparsers(dest="policy_verb", metavar="ACTION", required=True)
    policy_verbs.add_parser(
        "previous", help="list the operations and folders of the last policy a pairing set aside"
    )

    verbs.add_parser("pause", help="start nothing until resume; refuse new requests")
    verbs.add_parser("resume", help="clear pause and any kill request")
    verbs.add_parser("kill", help="kill the operation in flight, and pause")
    return parser


def _dirs(profile: str) -> paths.Dirs:
    if not paths.valid_profile(profile):
        raise _Refused("a profile name is lowercase letters, digits, '-' and '_', at most 32")
    return paths.dirs(profile)


def _load(dirs: paths.Dirs) -> policy.Policy:
    try:
        return policy.load(dirs.policy_file)
    except policy.PolicyError as exc:
        raise _Refused(f"the policy file {dirs.policy_file} is invalid: {exc}") from exc


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
                f"cannot lock {lock} ({exc.strerror}), so the policy was not changed"
            ) from exc
        yield
    finally:
        os.close(descriptor)


def _ops(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    family = paths.os_family()
    current = _load(dirs)
    if args.ops_verb == "list":
        eff = policy.effective(current, family)
        lines = []
        for name, op in OPERATIONS.items():
            if name in eff.enabled_ops:
                state = "enabled"
            elif name in eff.changed_by_upgrade:
                state = (
                    f"disabled: an upgrade changed it; `lucille-host ops enable {name}` accepts it"
                )
            elif name in eff.unavailable:
                state = f"enabled, but not on this computer: {op.executable(family)} is missing"
            elif not op.available(family):
                state = f"disabled; not on this computer: {op.executable(family)} is missing"
            else:
                state = "disabled"
            lines.append(f"{name}  {state}")
        for name in eff.not_in_catalog:
            lines.append(f"{name}  in the policy, but not in this release's catalog")
        return "\n".join(lines)
    name = args.op
    enabled = dict(current.enabled)
    if args.ops_verb == "enable":
        op = OPERATIONS.get(name)
        if op is None:
            raise _Refused(f"{name} is not in this release's catalog")
        if not op.available(family):
            raise _Refused(f"{name} cannot run here: {op.executable(family)} is missing")
        enabled[name] = op.definition_hash
        _save(dirs, replace(current, enabled=MappingProxyType(enabled)))
        return f"enabled {name}"
    if name not in enabled:
        raise _Refused(f"{name} is not enabled")
    del enabled[name]
    _save(dirs, replace(current, enabled=MappingProxyType(enabled)))
    return f"disabled {name}"


def _roots(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    current = _load(dirs)
    roots = dict(current.roots)
    if args.roots_verb == "list":
        return "\n".join(f"{alias}  {roots[alias].path}" for alias in sorted(roots))
    alias = args.alias
    if args.roots_verb == "remove":
        if alias not in roots:
            raise _Refused(f"no folder alias {alias!r}")
        del roots[alias]
        _save(dirs, replace(current, roots=MappingProxyType(roots)))
        return f"removed {alias}"
    if not policy.valid_alias(alias):
        raise _Refused("an alias is lowercase letters, digits, '-' and '_', starting with a letter")
    if alias in roots:
        raise _Refused(f"{alias} is already a folder alias; remove it first")
    if len(roots) >= policy.MAX_ROOTS:
        raise _Refused(f"the policy holds at most {policy.MAX_ROOTS} folder aliases")
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        raise _Refused(f"{path} is not a folder")
    if inside_own(path, [str(own) for own in dirs.own]):
        raise _Refused(f"{path} is where lucille-host keeps its own files")
    if on_proc(path):
        raise _Refused(
            f"{path} is on /proc, where names mean one process to the host and another to an "
            "operation (or this computer's mount table does not say where /proc is)"
        )
    # Only here, with the user at the terminal to interrupt it, does the check look below the
    # folder (policy.holds).
    broad = policy.is_broad(path, look_below=True)
    if broad and not args.allow_home_or_root:
        raise _Refused(
            f"{path} is your home folder, holds it, or is /; add --allow-home-or-root to mean it"
        )
    roots[alias] = policy.Root(path=path, broad=broad)
    try:
        _save(dirs, replace(current, roots=MappingProxyType(roots)))
    except policy.PolicyError as exc:
        raise _Refused(str(exc)) from exc
    return f"added {alias}"


def _policy_previous(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    path = dirs.previous_policy_file
    if not path.exists():
        return "no earlier policy: no pairing has set one aside"
    try:
        previous = policy.load(path)
    except policy.PolicyError as exc:
        raise _Refused(f"the earlier policy {path} is invalid: {exc}") from exc
    lines = [f"the last policy a pairing set aside ({path}); nothing uses it:"]
    lines.append("operations:")
    lines += [f"  {name}" for name in sorted(previous.enabled)] or ["  (none)"]
    lines.append("folders:")
    roots = previous.roots
    lines += [f"  {alias}  {roots[alias].path}" for alias in sorted(roots)] or ["  (none)"]
    return "\n".join(lines)


def _say(line: str) -> None:
    print(line, flush=True)


def _warn(line: str) -> None:
    print(f"lucille-host: warning: {line}", file=sys.stderr, flush=True)


def _read_line(prompt: str) -> str:
    """One line of stdin, with a prompt on stderr when stdin is a terminal; "" at its end."""
    if sys.stdin.isatty():
        print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline()


def _pair_action(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    if args.code_stdin or args.hub is not None or args.store_file:
        raise _Refused(f"`pair {args.code}` takes no options")
    session = link.open_session(dirs, host_state.load_host_config(dirs), say=_say, warn=_warn)
    if args.code == "status":
        word, detail = session.status()
        return f"{word}: {detail}"
    if args.code == "reject":
        told = session.reject()
        return f"rejected: pair {told.name} is forgotten on this computer; " + link.told_sentence(
            told
        )
    return f"confirmed: {session.confirm()}"


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


def _ask(session: link.Session, name: str, deadline: float | None) -> str:
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
        entry = _read_line("Does Lucille's page show the same SAS? Type y if it does, n if not: ")
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


def _pair(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    if args.code in PAIR_ACTIONS:
        return _pair_action(args, dirs)
    if args.code_stdin and args.code is not None:
        raise _Refused("with --code-stdin the code is read from stdin only; do not pass it")
    try:
        wanted = host_state.HostConfig(hub=args.hub, store="file" if args.store_file else "keyring")
    except ValueError as exc:
        raise _Refused(str(exc)) from exc
    host_state.startup_checks()
    # One `pair` of the profile at a time, from before it looks for a pair held until it returns,
    # its question included: two at once could each answer a code. `pair status`, `pair confirm`
    # and `pair reject` take no such lock, so that the SAS can be confirmed in another shell.
    lock = paths.ensure_dir(dirs.state) / PAIR_LOCK
    with loop.one_at_a_time(lock, ALREADY_PAIRING, "`pair`"):
        return _answer(args, dirs, wanted)


def _answer(args: argparse.Namespace, dirs: paths.Dirs, wanted: host_state.HostConfig) -> str:
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
            "Disconnect first: this computer already holds a pair (`lucille-host pair reject` "
            "forgets one that is not confirmed yet)"
        )
    text = args.code
    if text is None:
        text = _read_line("Paste the pairing code Lucille shows: ").strip()
        if not text:
            raise _Refused("no pairing code was given on stdin")
    code = session.check_code(text, wanted)
    _say(f"pairing code for pair {code.name}, on {code.url}")
    note = link.sanitize(code.note)
    if note:
        _say(f"note: {note}")
    if wanted != opened:
        session = link.open_session(dirs, wanted, say=_say, warn=_warn)
        if session.held():
            raise _Refused(
                "Disconnect first: this computer already holds a pair in that store "
                "(`lucille-host pair reject` forgets one that is not confirmed yet)"
            )
    if wanted != current:
        host_state.save_host_config(dirs, wanted)
    # A new pair starts from the fresh policy; the one it had is set aside, not applied.
    with _policy_lock(dirs):
        moved = policy.archive(dirs.policy_file, dirs.previous_policy_file)
    if moved:
        _say(
            "the policy is reset for the new pair: only host.describe is enabled, and no folder "
            "is held; `lucille-host policy previous` lists the earlier one"
        )
    name = session.accept(code)
    if session.skewed:
        _warn(link.SKEWED)
    _say(f"accepted: pair {name}")
    _say("waiting: for Lucille to collect this computer's answer")
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
    _say(f"sas_pending: {session.pending(name, sas, found)}")
    if args.code_stdin:
        return ""
    try:
        return _ask(session, name, found.deadline)
    except KeyboardInterrupt:
        raise link.Stopped(
            "interrupted",
            f"pair {name} is kept; `lucille-host pair status` shows its SAS again, and "
            "`lucille-host pair confirm` confirms it",
        ) from None


def _run(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    session = link.open_session(dirs, host_state.load_host_config(dirs), say=_say, warn=_warn)
    controls = Controls(paths.ensure_dir(dirs.state))
    return loop.run(dirs, session, controls=controls, say=_say)


def _status(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    config = host_state.load_host_config(dirs)
    session = link.open_session(dirs, config, say=_say, warn=_warn)
    return "\n".join(report.status(dirs, session, config))


def _log(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    if args.limit < 1:
        raise _Refused("--limit takes a number of lines, 1 or more")
    lines = audit.tail(dirs.state, args.limit)
    if not lines:
        print(
            f"lucille-host: the audit log has no lines yet ({dirs.state / audit.FILE_NAME})",
            file=sys.stderr,
        )
    return "\n".join(lines)


def _forget(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    session = link.open_session(dirs, host_state.load_host_config(dirs), say=_say, warn=_warn)
    gone = session.leave_left_over()
    if gone is not None:
        return (
            f"left: pair {gone} was gone from this computer already, and the work rooms still "
            "held over it are left now"
        )
    told = session.forget()
    return (
        f"forgotten: pair {told.name} is forgotten on this computer, its work rooms left first; "
        + link.told_sentence(told)
    )


def _controls(verb: str, dirs: paths.Dirs) -> str:
    controls = Controls(paths.ensure_dir(dirs.state))
    if verb == "resume":
        locked = controls.resume()
        message = "resumed"
    elif verb == "pause":
        locked = controls.pause()
        message = "paused: the host starts nothing until `lucille-host resume`"
    elif verb == "kill":
        locked = controls.kill()
        message = "paused, and asked the running host to kill the operation in flight, if any"
    else:
        raise _Refused(f"{verb} is not a control")
    if not locked:
        print(
            "lucille-host: note: this filesystem cannot lock, so this ran without the controls' "
            "lock; the order of the writes still keeps a kill request from standing without the "
            "pause flag",
            file=sys.stderr,
        )
    return message


def _ops_verb(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    with contextlib.nullcontext() if args.ops_verb == "list" else _policy_lock(dirs):
        return _ops(args, dirs)


def _roots_verb(args: argparse.Namespace, dirs: paths.Dirs) -> str:
    with contextlib.nullcontext() if args.roots_verb == "list" else _policy_lock(dirs):
        return _roots(args, dirs)


def _control(verb: str) -> Callable[[argparse.Namespace, paths.Dirs], str]:
    return lambda args, dirs: _controls(verb, dirs)


# Every verb the parser knows, and what runs it.
VERBS: Final[dict[str, Callable[[argparse.Namespace, paths.Dirs], str]]] = {
    "pair": _pair,
    "run": _run,
    "status": _status,
    "log": _log,
    "forget": _forget,
    "ops": _ops_verb,
    "roots": _roots_verb,
    "policy": _policy_previous,
    "pause": _control("pause"),
    "resume": _control("resume"),
    "kill": _control("kill"),
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``lucille-host`` console script and ``python -m lucille_host``."""
    args = build_parser().parse_args(argv)
    if args.verb is None:
        print(version_line())
        return 0
    try:
        dirs = _dirs(args.profile)
        verb = VERBS.get(args.verb)
        if verb is None:
            raise _Refused(f"{args.verb} is not a verb this release has")
        message = verb(args, dirs)
    except link.Stopped as exc:
        print(f"{exc.state}: {exc}", file=sys.stderr)
        return 1
    except _switchboard.PairFailed as exc:
        print(f"error: the pair cannot be used here ({exc.state})", file=sys.stderr)
        return 1
    except _switchboard.HubError as exc:
        print(f"error: {exc}; try again", file=sys.stderr)
        return 1
    except (
        _Refused,
        paths.UnsupportedPlatform,
        policy.PolicyError,
        host_state.Refused,
        host_state.StateError,
        host_state.Unprepared,
        loop.Failed,
    ) as exc:
        print(f"lucille-host: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        print(f"lucille-host: {exc.strerror or exc}{where}", file=sys.stderr)
        return 1
    if message:
        print(message)
    return 0
