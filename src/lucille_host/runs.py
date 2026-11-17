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
that cannot be written is skipped, and nothing is said. The file is appended to, one line at a
time; once it holds more than :data:`MAX_BYTES`, the command that finds it so keeps the last
:data:`KEEP` lines, under the file's own lock, which it only tries for.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from lucille_host import paths

__all__ = ["FILE", "KEEP", "MAX_BYTES", "RUNTIME_IN_APP", "command_of", "record"]

FILE: Final = "runs.jsonl"
VERSION: Final = 1
# The record is cut back to its last KEEP lines once it is larger than MAX_BYTES. A line is about
# 150 bytes, so the file stays under 64 KiB and holds the last few hundred commands.
MAX_BYTES: Final = 64 * 1024
KEEP: Final = 200
# Where an app bundle keeps the runtime (tools/macos/build_app.py, RUNTIME_IN_APP).
RUNTIME_IN_APP: Final = "/Contents/Resources/runtime/bin/lucille-host-runtime"


def command_of(words: Sequence[str], verbs: Sequence[str]) -> tuple[str, str] | None:
    """The profile and the command a command line names, read without parsing it: the first word
    before ``--`` that is neither an option nor an option's value, where it is one of ``verbs``;
    else ``version`` where there is no such word, or the line asks for ``--version``. None where
    the line names neither, or asks for help, so that no other word is ever recorded."""
    profile = paths.DEFAULT_PROFILE
    command = None
    plain = []
    given = list(words[: words.index("--")] if "--" in words else words)
    skip = False
    for index, word in enumerate(given):
        if skip:
            skip = False
            continue
        if word == "--profile":
            skip = True
            if index + 1 < len(given):
                profile = given[index + 1]
            continue
        if word.startswith("--profile="):
            profile = word.removeprefix("--profile=")
            continue
        if word.startswith("-"):
            continue
        plain.append(word)
        if len(plain) == 1 and word in verbs:
            command = word
    asks_help = "-h" in given or "--help" in given
    if command is None and not asks_help and ("--version" in given or not plain):
        command = "version"
    return None if command is None else (profile, command)


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
        line = json.dumps(
            {
                "v": VERSION,
                "command": command,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "executable": _in_app(sys.executable if executable is None else executable),
                "started": clock(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        _append(state / FILE, line + "\n")
    except Exception:  # never fails the command (module docstring)
        return


def _in_app(executable: str) -> str | None:
    """The real path of ``executable`` where it is an app bundle's runtime, else None."""
    real = os.path.realpath(executable)
    return real if real.endswith(RUNTIME_IN_APP) and ".app/" in real else None


def _append(path: Path, line: str) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
        if os.fstat(descriptor).st_size <= MAX_BYTES:
            return
        try:
            # Only tried for: a command never waits on its record.
            os.lockf(descriptor, os.F_TLOCK, 0)
        except OSError:
            return
        with open(path, encoding="utf-8", errors="replace") as handle:
            kept = handle.read().splitlines(keepends=True)[-KEEP:]
        os.ftruncate(descriptor, 0)
        os.write(descriptor, "".join(kept).encode("utf-8"))
    finally:
        os.close(descriptor)
