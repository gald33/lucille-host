"""Whether a request's path stays inside its folder (spec/PROTOCOL.md section 10, "Paths and
roots").

:func:`contain` resolves the path itself, one component at a time, following symbolic links as
the kernel would and refusing any that leads out. Every lookup names a single component relative to
a descriptor on its directory (``fstatat``, ``openat``, ``readlinkat``), so no lookup's argument is
ever longer than one name. It does not use ``os.path.realpath`` on the joined path: that silently
skips a component it cannot ``lstat`` (once the absolute path reaches PATH_MAX, say), and so could
pass a link the child, which opens the shorter relative path, then follows out of the folder. Nor
does it look up the joined path relative to the folder to decide, which grows with every link it
expands while the kernel's does not.

Following a link by its text is what the kernel does, except on ``/proc``: a link there such as
``<pid>/fd/0`` or ``<pid>/root`` takes the kernel straight to an object (a pipe, another
namespace's root) that its text does not name, and ``self``, ``<pid>`` and a process not yet
started mean the host when the host looks and the child when the child opens the path. No check the
host makes can stand in for the child's lookup there, so a path is never walked through ``/proc``
(Linux's mount table says which devices are ``/proc``: :mod:`lucille_host.mounts`). Once the walk
has decided, the host also looks the whole path up itself, following links, and keeps the answer
only if that finds the very object the walk ended on, by device and inode, or finds nothing where
the walk found nothing: a check that the walk and the kernel agree, for the host.

The rules, all decided before anything runs:

- an absolute path, or one with a ``..`` segment, is an escape;
- a folder that is not an existing directory the host can open is an escape (containment cannot be
  shown);
- a link whose target is absolute leads out unless the target names the folder's real path or
  something under it, written out in full: a target reaching the folder by another spelling (through
  another link, say) is refused, which errs on the safe side;
- a relative target is resolved from the link's own directory, and a ``..`` that would climb above
  the folder is an escape;
- more than :data:`MAX_LINKS` links is an escape, as the kernel's own limit makes it an error (its
  MAXSYMLINKS: 40 on Linux, 32 on macOS);
- a component that does not exist, or whose name the filesystem refuses as too long, ends the walk
  inside, with nothing found: nothing further exists for the child to follow;
- a component that is not a directory where the path goes on below it ends the walk inside too,
  with nothing found: nothing below a file can lead out of the folder, and the child's lookup of
  what follows fails with ENOTDIR. So does a file named with a trailing ``/``. (On macOS a file's
  named forks, ``..namedfork/rsrc``, are found below it; the host's own lookup then finds something
  where the walk found nothing, and the path is refused.);
- a path that reaches a directory on ``/proc``, and a folder on ``/proc``, are escapes. On Linux,
  where the mount table lists no ``/proc``, every path is: which folders are ``/proc`` cannot be
  shown;
- a path naming something other than a file or a folder (a FIFO, a socket, a device) is refused as
  an escape is: opening one can block until the time limit, or take data meant for another
  program;
- the host's own files (:attr:`lucille_host.paths.Dirs.own`: its config and state folders and
  everything in them, the policy, the digest key and the audit log among them) are outside every
  folder. They are compared by device and inode, with links in them followed and not and linked
  folders walked too, so another spelling of one (a second mount of one of its folders, a hard link
  to one of its files, the folder a link in them points to and the files in it) is caught too: a
  path that reaches one is an escape, and so is a folder inside one. Folders holding more than
  :data:`MAX_OWN_ENTRIES` entries make every path an escape;
- a directory the host cannot open (one it may search but not read, say), more than
  :data:`MAX_STEPS` lookups, and any other error while walking are escapes: the host does not run
  what it could not check.

It checks before the operation runs, and the child then opens the path by its own name. A link
swapped in between by a process writing inside the folder is not caught, and nor is a hard link
made to an earlier version of a file the host has since replaced (THREAT_MODEL.md, "Known gaps").
"""

from __future__ import annotations

import collections
import errno
import os
import stat
import sys
from collections.abc import Iterable
from typing import Final

from lucille_host import mounts

__all__ = ["MAX_LINKS", "contain", "inside_own", "on_proc"]

# The kernel's own limit on links followed in one lookup (MAXSYMLINKS).
MAX_LINKS: Final = 32 if sys.platform == "darwin" else 40
# A bound on the lookups one walk may make, so that link targets full of ".." cannot make it slow.
MAX_STEPS: Final = 65536
# A bound on the entries listed in the host's own folders, which hold a few files.
MAX_OWN_ENTRIES: Final = 10000

# Errors from looking up one name that mean "nothing is there to follow": the walk ends inside.
_ABSENT: Final = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG})
_OPEN_DIR: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class _Out(Exception):
    """The walk passed MAX_STEPS lookups, or entered one of the host's own folders or /proc."""


def _parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part not in ("", ".")]


def _steps(path: str) -> list[str]:
    """The names to walk for a path or a link's text. A trailing "/" or "/." means "a directory":
    it is kept as a last "." step, which ends the walk with nothing found below a file, as the
    child's own lookup fails there with ENOTDIR."""
    steps = _parts(path)
    if path.split("/")[-1] in ("", "."):
        steps.append(".")
    return steps


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class _TooManyOwn(OSError):
    """The host's own folders hold more than MAX_OWN_ENTRIES entries."""


def _identities(folders: Iterable[str]) -> frozenset[tuple[int, int]]:
    """The device and inode of each of the host's own folders and of everything in them, links
    in them followed and not, into linked folders too (a profile folder moved to another disk and
    linked back, say), each folder walked once however it is reached. A folder that does not exist
    holds nothing to reach. More than :data:`MAX_OWN_ENTRIES` entries raises :class:`_TooManyOwn`:
    rather than walk a tree a link has pulled in, the host refuses every path."""
    found: set[tuple[int, int]] = set()
    walked: set[tuple[int, int]] = set()
    pending = list(folders)
    entries = 0
    while pending:
        folder = pending.pop()
        try:
            info = os.stat(folder)
        except (OSError, ValueError):
            continue
        found.add(_identity(info))
        if not stat.S_ISDIR(info.st_mode) or _identity(info) in walked:
            continue
        walked.add(_identity(info))
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            entries += 1
            if entries > MAX_OWN_ENTRIES:
                raise _TooManyOwn(errno.E2BIG, "the host's own folders hold too many entries")
            path = os.path.join(folder, name)
            for follow in (False, True):
                try:
                    entry = os.stat(path, follow_symlinks=follow)
                except OSError:
                    continue
                found.add(_identity(entry))
                if follow and stat.S_ISDIR(entry.st_mode):
                    pending.append(path)
    return frozenset(found)


def _proc_devices() -> frozenset[int]:
    """The devices of every ``/proc`` in Linux's mount table. On Linux, a table that lists none
    (``/proc`` not mounted, or covered by another mount) raises: it cannot show which folders are
    ``/proc``, so nothing is walked."""
    devices = frozenset(mount.device for mount in mounts.table() if mount.fstype == "proc")
    if not devices and sys.platform.startswith("linux"):
        raise OSError(errno.ENOENT, "the mount table lists no /proc")
    return devices


def on_proc(folder: str) -> bool:
    """Whether ``folder`` is on ``/proc`` (Linux), where no path is walked. True also where Linux's
    mount table cannot say which folders are ``/proc``."""
    try:
        return os.stat(folder).st_dev in _proc_devices()
    except ValueError:
        return False
    except OSError:
        return sys.platform.startswith("linux")


def inside_own(folder: str, own: Iterable[str]) -> bool:
    """Whether ``folder`` is one of the host's own folders, or inside one, by device and inode.
    True also where the host's own folders are too large to list."""
    try:
        own_ids = _identities(own)
        return any(_identity(os.stat(above)) in own_ids for above in _above(folder))
    except _TooManyOwn:
        return True
    except (OSError, ValueError):
        return False


def contain(root_path: str, path: str, *, own: Iterable[str] = ()) -> str | None:
    """The real path of the folder, if ``path`` stays inside it and names a file, a folder or
    nothing; None for any escape. ``own`` are the host's own folders, which no path may reach."""
    if path.startswith("/") or ".." in path.split("/"):
        return None
    try:
        os.fsencode(path)
        own_ids = _identities(own)
        proc = _proc_devices()
        root_real = os.path.realpath(root_path, strict=True)
        if any(_identity(os.stat(above)) in own_ids for above in _above(root_real)):
            return None
        root_fd = os.open(root_real, os.O_RDONLY | os.O_DIRECTORY)
    except (OSError, ValueError, UnicodeError):
        return None
    walk = _Walk(root_fd, own_ids, proc)
    try:
        if os.fstat(root_fd).st_dev in proc or not walk.inside(_parts(root_real), _steps(path)):
            return None
        return root_real if _host_finds(root_fd, path, walk.found) else None
    except (OSError, _Out):
        return None
    finally:
        walk.close()
        os.close(root_fd)


def _host_finds(root_fd: int, path: str, found: os.stat_result | None) -> bool:
    """Whether the host's own lookup of ``path`` from the folder, following links, finds what the
    walk found: the same object, or nothing where the walk found nothing."""
    try:
        info = os.stat(path or ".", dir_fd=root_fd)
    except OSError as exc:
        return found is None and exc.errno in _ABSENT
    return found is not None and _identity(info) == _identity(found)


def _above(path: str) -> list[str]:
    """``path`` and each folder above it."""
    chain = [path]
    while chain[-1] != os.path.dirname(chain[-1]):
        chain.append(os.path.dirname(chain[-1]))
    return chain


class _Walk:
    """Where the walk stands: the names from the folder down, and a descriptor on the last one.
    Once :meth:`inside` has said True, :attr:`found` is what the path names, or None for nothing."""

    def __init__(self, root_fd: int, own: frozenset[tuple[int, int]], proc: frozenset[int]) -> None:
        self._root_fd = root_fd
        self._own = own
        self._proc = proc
        self.found: os.stat_result | None = None
        self._fd: int | None = None
        self._names: list[str] = []
        self._steps = 0

    def _step(self) -> None:
        self._steps += 1
        if self._steps > MAX_STEPS:
            raise _Out

    def _here(self) -> int:
        return self._root_fd if self._fd is None else self._fd

    def _move(self, fd: int | None) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._fd = fd

    def close(self) -> None:
        self._move(None)

    def _descend(self, name: str) -> None:
        self._step()
        fd = os.open(name, _OPEN_DIR, dir_fd=self._here())
        self._move(fd)
        self._names.append(name)
        info = os.fstat(fd)
        if _identity(info) in self._own or info.st_dev in self._proc:
            raise _Out

    def _to_root(self) -> None:
        self._move(None)
        self._names = []

    def _ascend(self) -> None:
        """Go up one directory: open the remaining names again from the folder."""
        names = self._names[:-1]
        self._to_root()
        for name in names:
            self._descend(name)

    def inside(self, root_parts: list[str], path_parts: list[str]) -> bool:
        """Resolve ``path_parts`` beneath the folder. True if it stays inside."""
        pending = collections.deque(path_parts)
        links = 0
        while pending:
            name = pending.popleft()
            if name == "..":
                if not self._names:
                    return False
                self._ascend()
                continue
            self._step()
            try:
                info = os.stat(name, dir_fd=self._here(), follow_symlinks=False)
            except OSError as exc:
                self.found = None
                return exc.errno in _ABSENT
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > MAX_LINKS:
                    return False
                self._step()
                target = os.readlink(name, dir_fd=self._here())
                if target.startswith("/"):
                    target_parts = _parts(target)
                    if target_parts[: len(root_parts)] != root_parts:
                        return False
                    self._to_root()
                    pending.extendleft(reversed(_steps(target)[len(root_parts) :]))
                else:
                    pending.extendleft(reversed(_steps(target)))
                continue
            if not pending:
                self.found = info
                if _identity(info) in self._own or info.st_dev in self._proc:
                    return False
                return stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            if not stat.S_ISDIR(info.st_mode):
                # Nothing below a file can lead out of the folder (see the module docstring).
                self.found = None
                return True
            try:
                self._descend(name)
            except OSError:
                return False
        # The path ended on a directory the walk is in (".", or a link to one).
        self.found = os.fstat(self._here())
        return True
