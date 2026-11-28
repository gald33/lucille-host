"""Who owns a profile's folders and files: what ``lucille-host profile check`` reports
(decisions/0004-running-as-root.md, 4.3, "Profiles that are already root-owned").

A command run as root before the host refused uid 0 could leave folders and files in a user's
profile that root owns, mode 0700 or 0600, so that the user's own commands fail with ``Permission
denied``. This module finds them, run as the user, and says how to repair each one. It changes
nothing: the repair needs root, and the host never runs as root. It sends nothing and writes
nothing (the verb's line in the run record is :func:`lucille_host.cli.main`'s, as for every
command).

What it looks at, for each of the profile's folders (the config folder, and the state folder where
it is not inside the config folder, as it is on macOS):

- The folder, resolved as :mod:`lucille_host.paths` resolves it (``HOME``, an absolute
  ``XDG_CONFIG_HOME`` or ``XDG_STATE_HOME``, the macOS layout), then with every symbolic link
  followed (:func:`os.path.realpath`), since the host follows them too.
- The folder and each folder above it, up to the first one the user owns, and never the home
  folder or anything above it. A folder that is missing is not damage: the host makes it. A folder
  that cannot be looked at, because the folder above it does not let the user in, is not reported
  when a folder above it is another user's: repairing that one repairs it. Of the folders owned by
  another user, the highest is reported: ``chown -R`` of it repairs the ones below.
- Where the folder itself is the user's, every folder and file inside it. One owned by another
  user is reported, and nothing inside it. A symbolic link is not followed into, and its own owner
  does not matter; what it points to, which the host follows (a ``policy.toml`` linked to a file
  kept elsewhere), is looked at as the folder or file it is, and reported by its own path.

A folder or file outside the home folder is reported with no repair: the host cannot tell whether
another program needs it as it is (decisions/0004, 4.3, "Not checked"), so a person decides.

Each finding inside the home folder comes with two command lines, for a person to run as root:
one that shows what is there (``ls -lR``), since the folder may hold another program's files, and
one that gives it back to the user (``chown -R``, or ``chown`` for a file), naming the user by
uid, which needs no lookup. The group is left as it is.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lucille_host import paths

__all__ = ["Found", "check"]

# What a finding is.
NOT_YOURS: Final = "not_yours"
OUTSIDE_HOME: Final = "outside_home"
UNREADABLE: Final = "unreadable"


@dataclass(frozen=True)
class Found:
    """One folder or file of the profile that is not the user's, or that cannot be looked at.

    ``state`` is ``not_yours`` (inside the home folder, with ``look`` and ``repair``),
    ``outside_home`` (no command is given), or ``unreadable`` (``kind`` and ``uid`` are None)."""

    state: str
    path: Path
    kind: str | None
    uid: int | None
    look: str | None = None
    repair: str | None = None


def _uid(info: os.stat_result) -> int:
    """The uid that owns what ``info`` describes. A test replaces it, and only it, to plant another
    user's folder or file by its device and inode, since only root could make one, and to give the
    suite's own files the uid every test is given (tests/conftest.py): every ``stat`` and
    ``lstat`` is still a real one, and refuses as it would."""
    return info.st_uid


def _kind(info: os.stat_result) -> str:
    return "folder" if stat.S_ISDIR(info.st_mode) else "file"


def _owner(path: Path) -> int | None:
    """The uid that owns ``path``, a symbolic link followed; None where nothing is there. Raises
    :class:`PermissionError` where a folder above it does not let this process look."""
    try:
        return _uid(os.stat(path))
    except (FileNotFoundError, NotADirectoryError):
        return None


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _above(folder: Path, home: Path) -> Iterator[Path]:
    """``folder`` and each folder above it, stopping before ``home`` or before ``/``."""
    current = folder
    while current != home and current.parent != current:
        yield current
        current = current.parent


def _quoted(word: str) -> str:
    """``word`` in single quotes for a POSIX shell, always, so that a command reads the same
    whatever the path holds."""
    return "'" + word.replace("'", "'\"'\"'") + "'"


def _commands(path: Path, kind: str, me: int) -> tuple[str, str]:
    """The command that shows what is at ``path``, and the one that gives it to the uid ``me``."""
    quoted = _quoted(str(path))
    if kind == "folder":
        return f"ls -lR {quoted}", f"chown -R {me} {quoted}"
    return f"ls -l {quoted}", f"chown {me} {quoted}"


def _finding(path: Path, kind: str, uid: int, home: Path, me: int) -> Found:
    if not path.is_relative_to(home) or path == home:
        return Found(OUTSIDE_HOME, path, kind, uid)
    look, repair = _commands(path, kind, me)
    return Found(NOT_YOURS, path, kind, uid, look, repair)


def _chain(folder: Path, home: Path, me: int) -> tuple[list[Found], bool]:
    """What :func:`check` reports of ``folder`` and the folders above it, and whether ``folder``
    itself is the user's (so that what is inside it can be looked at)."""
    # Walked from the profile up: the folders that cannot be looked at come first, since each is
    # inside one that refuses the user, and the highest folder another user owns last.
    highest: tuple[Path, int] | None = None
    hidden: Path | None = None
    own = False
    for each in _above(folder, home):
        try:
            uid = _owner(each)
        except PermissionError:
            hidden = each
            continue
        if uid == me:
            own = each == folder
            break
        if uid is not None:
            highest = (each, uid)
    if highest is not None:
        return [_finding(highest[0], "folder", highest[1], home, me)], False
    if hidden is not None:
        return [Found(UNREADABLE, hidden, None, None)], False
    return [], own


def _inside(folder: Path, home: Path, me: int) -> list[Found]:
    """Every folder and file inside ``folder`` that another user owns, or that cannot be looked
    at, and what each symbolic link inside it points to; nothing inside one is looked at."""
    found: list[Found] = []

    def unreadable(exc: OSError) -> None:
        if not isinstance(exc, PermissionError):
            raise exc
        found.append(Found(UNREADABLE, Path(exc.filename), None, None))

    def linked(path: Path) -> None:
        """What the symbolic link ``path`` points to, recorded where it is not the user's."""
        try:
            info = os.stat(path)
        except PermissionError:
            found.append(Found(UNREADABLE, _real(path), None, None))
            return
        except OSError:
            return  # it points at nothing, or at itself: the host reads nothing there
        uid = _uid(info)
        if uid != me:
            found.append(_finding(_real(path), _kind(info), uid, home, me))

    def other(path: Path) -> bool:
        """Whether nothing inside ``path`` is to be looked at: it is not the user's, or it is a
        symbolic link, or it cannot be looked at. Records what is found."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return True  # gone since the folder was listed: nothing to look inside
        except PermissionError:
            found.append(Found(UNREADABLE, path, None, None))
            return True
        if stat.S_ISLNK(info.st_mode):
            linked(path)
            return True
        uid = _uid(info)
        if uid == me:
            return False
        found.append(_finding(path, _kind(info), uid, home, me))
        return True

    for where, folders, files in os.walk(folder, onerror=unreadable):
        folders[:] = [name for name in sorted(folders) if not other(Path(where, name))]
        for name in sorted(files):
            other(Path(where, name))
    return found


def check(
    profile: str = paths.DEFAULT_PROFILE,
    *,
    family: str | None = None,
    environ: Mapping[str, str] | None = None,
    uid: int | None = None,
) -> list[Found]:
    """What is not the user's in ``profile``'s folders, by path. Empty when everything that is
    there is the user's."""
    env = os.environ if environ is None else environ
    found_dirs = paths.dirs(profile, family=family, environ=env)
    home = _real(paths.home(env))
    me = os.geteuid() if uid is None else uid
    sides = [found_dirs.config]
    if not found_dirs.state.is_relative_to(found_dirs.config):
        sides.append(found_dirs.state)
    found: list[Found] = []
    for side in sides:
        folder = _real(side)
        chain, own = _chain(folder, home, me)
        found.extend(chain)
        if own:
            found.extend(_inside(folder, home, me))
    unique: dict[Path, Found] = {}
    for each in found:
        unique.setdefault(each.path, each)
    return sorted(unique.values(), key=lambda each: str(each.path))
