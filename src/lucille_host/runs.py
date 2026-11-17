"""The run record: one line for each command started on this computer, kept in the profile's
state folder (``runs.jsonl``), on this computer only.

Each line is a JSON object: ``{"v": 1, "command": ..., "pid": ..., "ppid": ..., "executable": ...,
"started": ...}``:

- ``command``: the verb (``status``, ``run``, ``credential``, ...), or ``version`` for
  ``--version`` or no verb. Never an argument: a word that is not a verb is not recorded, so a value
  typed on the command line by mistake never lands here.
- ``pid`` and ``ppid``: the command's process and the process that started it.
- ``executable``: the real path of the interpreter that runs the command, where it is an app
  bundle's runtime (``…/Contents/Resources/runtime/bin/lucille-host-runtime``); ``null``
  otherwise, so that no path outside an app is kept.
- ``started``: when the command started, in seconds since the epoch.

It is what lets the Mac app's checks prove that the app started its own host, from the host's own
word, without catching a process that may run for a fraction of a second (spec/CLI.md section 16).
Nothing reads it at run time, and nothing sends it anywhere.

The record is written before the command's work starts, and it never fails the command: a record
that cannot be written is skipped, and nothing is said. It is only ever a regular file: a FIFO,
a device, a symbolic link or a file with another name linked to it is left alone. Each line is
appended under the record's lock, waited for at most :data:`LOCK_WAIT` seconds, only while
another command holds it. Once the record holds more than :data:`MAX_BYTES`, the command that
finds it so replaces it by its last :data:`KEEP_BYTES` bytes, from the first whole line in them,
written beside it and renamed over it, so no line written after the cut is lost and a reader
never finds the record empty or half written.

Any process of the same user can write a line of its own, in the right shape; the record is the
host's word only as far as the user's own account is trusted (THREAT_MODEL.md, Known gaps).
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from lucille_host import paths

__all__ = ["FILE", "KEEP_BYTES", "MAX_BYTES", "MAX_LINE", "RUNTIME_IN_APP", "command_of", "record"]

FILE: Final = "runs.jsonl"
VERSION: Final = 1
# The record is cut back to its last KEEP_BYTES bytes (whole lines) once it is larger than
# MAX_BYTES, so it is never larger than MAX_BYTES and one line. A line is about 150 bytes, and
# never more than MAX_LINE: the record holds the last few hundred commands.
MAX_BYTES: Final = 64 * 1024
KEEP_BYTES: Final = MAX_BYTES // 2
MAX_LINE: Final = 1024
# How long a command waits, at most, for the record's lock while another command holds it, and
# how often it tries; past that, its line is skipped.
LOCK_WAIT: Final = 0.2
LOCK_RETRY: Final = 0.002
_FLAGS: Final = (
    os.O_RDWR
    | os.O_APPEND
    | os.O_CREAT
    | os.O_NOFOLLOW
    | os.O_NONBLOCK
    | os.O_NOCTTY
    | os.O_CLOEXEC
)
# Where an app bundle keeps the runtime (tools/macos/build_app.py, RUNTIME_IN_APP).
RUNTIME_IN_APP: Final = "/Contents/Resources/runtime/bin/lucille-host-runtime"


def command_of(words: Sequence[str], verbs: Sequence[str]) -> tuple[str, str] | None:
    """The profile and the command a command line names, read as the parser reads it but without
    parsing it. The options before the first plain word are the parser's own (``--profile``,
    ``--version``, ``--help``, ``--json``, each also by an abbreviation the parser takes, such as
    ``--prof``); after ``--`` every word is plain. The command is the first plain word, where it
    is one of ``verbs``; else ``version`` where no plain word comes before ``--version``, or there
    is none and no help is asked for. None where the line names neither, so that no other word is
    ever recorded."""
    profile = paths.DEFAULT_PROFILE
    asks_help = False
    given = list(words)
    index = 0
    while index < len(given):
        word = given[index]
        index += 1
        if word == "--":
            first = given[index] if index < len(given) else None
            break
        if not word.startswith("-") or word == "-":
            first = word
            break
        name, equals, value = word.partition("=")
        if _abbreviates(name, "--profile"):
            if equals:
                profile = value
            elif index < len(given):
                profile = given[index]
                index += 1
        elif _abbreviates(name, "--version"):
            return profile, "version"
        elif name == "-h" or _abbreviates(name, "--help"):
            asks_help = True
    else:
        first = None
    if first is not None:
        return (profile, first) if first in verbs else None
    return None if asks_help else (profile, "version")


def _abbreviates(word: str, option: str) -> bool:
    """Whether the parser takes ``word`` for the long option ``option``: the option itself, or a
    prefix of it no other option of the parser shares (``--p``, ``--v``, ``--h`` and ``--j`` are
    each one option's)."""
    return len(word) >= 3 and option.startswith(word)


def record(
    words: Sequence[str],
    verbs: Sequence[str],
    *,
    clock: Callable[[], float] = time.time,
    executable: str | None = None,
) -> None:
    """Append this command's line to its profile's run record. Never raises, and says nothing:
    a record that cannot be written is skipped. Nothing is written for a line that names no
    command, for an invalid profile, or as root (the host does not run as root)."""
    try:
        named = command_of(words, verbs)
        if named is None or os.geteuid() == 0:
            return
        profile, command = named
        if not paths.valid_profile(profile):
            return
        state = paths.ensure_dir(paths.dirs(profile).state)
        fields: dict[str, object] = {
            "v": VERSION,
            "command": command,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "executable": _in_app(sys.executable if executable is None else executable),
            "started": clock(),
        }
        line = _line(fields)
        if len(line) > MAX_LINE:
            # Only a very long app path makes a line this long: it is kept without the path.
            line = _line({**fields, "executable": None})
        if len(line) <= MAX_LINE:
            _append(state / FILE, line)
    except Exception:  # never fails the command (module docstring)
        return


def _line(fields: Mapping[str, object]) -> bytes:
    return (
        json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _in_app(executable: str) -> str | None:
    """The real path of ``executable`` where it is an app bundle's runtime, else None."""
    real = os.path.realpath(executable)
    return real if real.endswith(RUNTIME_IN_APP) and ".app/" in real else None


def _append(path: Path, line: bytes) -> None:
    """Append ``line`` to the record at ``path``, under the record's lock, which it waits for at
    most :data:`LOCK_WAIT` seconds; then, where the record is larger than :data:`MAX_BYTES`, cut it
    (:func:`_cut`). Only a regular file is written: the open never blocks (a FIFO with no reader
    refuses it, ENXIO; one with a reader is opened and refused), follows no symbolic link, and
    makes no terminal the process's own. A record replaced since it was opened, by another
    command's cut, is opened again."""
    give_up = time.monotonic() + LOCK_WAIT
    while time.monotonic() < give_up:
        descriptor = os.open(path, _FLAGS, 0o600)
        try:
            opened = os.fstat(descriptor)
            # A file with another name linked to it is left alone. One with none was replaced by
            # a cut since it was opened: the check below opens the new record.
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1:
                return
            if not _lock(descriptor, give_up):
                return
            now = os.stat(path, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino):
                continue  # cut and replaced since it was opened: the new record is the one
            size = now.st_size
            if size and os.pread(descriptor, 1, size - 1) != b"\n":
                line = b"\n" + line  # a line of its own, whatever was left unended before it
            _write(descriptor, line)
            if os.fstat(descriptor).st_size > MAX_BYTES:
                _cut(path, descriptor)
            return
        finally:
            os.close(descriptor)  # and with it the lock


def _lock(descriptor: int, give_up: float) -> bool:
    """Take the record's lock (a POSIX lock on the whole file), trying until ``give_up``. Each
    command holds it for one short write, or for a cut of :data:`KEEP_BYTES` bytes."""
    os.lseek(descriptor, 0, os.SEEK_SET)  # the lock covers the file from here on: all of it
    while True:
        try:
            os.lockf(descriptor, os.F_TLOCK, 0)
            return True
        except OSError:
            if time.monotonic() >= give_up:
                return False
            time.sleep(LOCK_RETRY)


def _cut(path: Path, descriptor: int) -> None:
    """Replace the record, under its lock, by its last :data:`KEEP_BYTES` bytes at most, from the
    first line that starts in them, written to a new file beside it and renamed over it: a reader
    sees the old record or the new one, never a part of either, and a crash leaves the old one.
    Only those bytes are read, however large the record has grown."""
    size = os.fstat(descriptor).st_size
    start = max(0, size - KEEP_BYTES)
    kept = os.pread(descriptor, size - start, start)
    if start:
        newline = kept.find(b"\n")
        kept = kept[newline + 1 :] if newline >= 0 else b""
    handle, temporary = tempfile.mkstemp(prefix=f".{FILE}.", suffix=".cut", dir=path.parent)
    try:
        try:
            _write(handle, kept)
        finally:
            os.close(handle)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _write(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]
