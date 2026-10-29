"""The ``lucille-host`` command.

With no arguments, or with ``--version``, it prints ``lucille-host <version>`` and exits 0. The
verbs in this release change only local state; none of them talks to a hub:

- ``ops list|enable|disable``: which catalog operations the local policy enables;
- ``roots list|add|remove``: which folders the operations may work in, by alias;
- ``pause``, ``resume`` and ``kill``: the local controls.

``--profile <name>`` selects a separate config and state directory, so that two computers can be
simulated on one machine. A verb that fails prints why on stderr and exits 1.

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
from collections.abc import Iterator, Sequence
from dataclasses import replace
from types import MappingProxyType

from lucille_host import __version__, paths, policy
from lucille_host.containment import inside_own, on_proc
from lucille_host.controls import Controls
from lucille_host.ops import OPERATIONS


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
            "Runs a closed set of named operations for a paired Lucille account. This release "
            "has the catalog, the local policy and the local controls; it does not pair yet."
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
            os.lockf(descriptor, os.F_LOCK, 0)
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


def _controls(verb: str, dirs: paths.Dirs) -> str:
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
    return message


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``lucille-host`` console script and ``python -m lucille_host``."""
    args = build_parser().parse_args(argv)
    if args.verb is None:
        print(version_line())
        return 0
    try:
        dirs = _dirs(args.profile)
        if args.verb in ("ops", "roots"):
            action = args.ops_verb if args.verb == "ops" else args.roots_verb
            verb = _ops if args.verb == "ops" else _roots
            with contextlib.nullcontext() if action == "list" else _policy_lock(dirs):
                message = verb(args, dirs)
        else:
            message = _controls(args.verb, dirs)
    except (_Refused, paths.UnsupportedPlatform, policy.PolicyError) as exc:
        print(f"lucille-host: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        print(f"lucille-host: {exc.strerror or exc}{where}", file=sys.stderr)
        return 1
    if message:
        print(message)
    return 0
