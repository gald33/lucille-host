"""Checks on the built Mac app. CI runs each one on a macOS runner after build_app.py.

    uv run --no-project python tools/macos/check_app.py CHECK [--out build/macos] [options]

Each check prints what it saw and exits 1 if anything is wrong (2 on a usage error, and off a
Mac). The app is ``<out>/<name>.app``; every check but ``dmg``, ``unpack-dmg``, ``handoff-check``
and ``library-snapshot`` refuses to run without it. The interpreter always runs as the app runs
it, ``lucille-host-runtime -I -B -X utf8`` (:data:`build_app.RUNTIME_FLAGS`).

The bundle as built:

- ``bundle``: no symbolic link and no bytecode in the bundle; its Mach-O files are exactly the
  expected five (:data:`RUNTIME_MACHO` and the app's executable); the app and every one of them
  is signed ad hoc by this build, not merely by the linker, with no Team ID and the identifier
  ``build_app.signing_identifier`` gave it (:func:`codesign_problems`).
- ``record`` (M10): each wheel MANIFEST.json names, the host's among them, is found again and
  hashed; the installed distributions are exactly those; and every file in ``site-packages`` is
  byte for byte a file of its wheel's ``RECORD`` (``payload.record_problems``, the build's own
  check), apart from the Mach-O files, which codesign rewrites in place: those are compared after
  ``codesign --remove-signature`` on both copies, and that comparison is reported, not failed.
- ``version`` (M11, with ``--tag`` or ``--build-number``): Info.plist's two version strings are
  the version rule's, and MANIFEST.json, the bundled host's ``--version`` and the pins agree.
- ``links``: every library a Mach-O file in the bundle loads is macOS's own, or resolves, the
  way dyld resolves ``@loader_path``, ``@executable_path`` and ``@rpath``, to a file that exists
  inside the bundle; every run path resolves inside the bundle (:func:`resolve_links`). It prints
  where each one resolves. No Mach-O file says it needs a newer macOS than
  ``LSMinimumSystemVersion``; no file in the bundle holds the build folder's path; and the app's
  own executable imports no function from :data:`FORBIDDEN_IMPORTS` and the lists after it, which
  start processes or open network connections, and links none of :data:`FORBIDDEN_LIBRARIES`.
  That last part reads only what the executable imports by name: code that reaches such a
  function another way (through the Objective-C runtime, say) is not seen.
- ``signature``: ``codesign --verify --deep --strict`` passes on the app, and ``codesign --verify
  --strict`` on each Mach-O file in it; each is signed ad hoc as ``bundle`` says, with the
  hardened runtime exactly when the pins ask for it; nothing carries entitlements. CI runs it
  again after every other check, so a run that wrote into the bundle fails it.
- ``wrapper``: on a writable copy of the app, the Terminal wrapper ``Resources/bin/lucille-host``
  runs the host through a link to it from another folder (and never a runtime beside the link),
  and neither it nor the interpreter run with the app's flags writes any bytecode; then, as the
  control, the same run without ``-B`` does write some, so the check can see it.

On the bundle made read-only (CI runs ``chmod -R a-w`` first):

- ``host``: ``--version`` prints this checkout's version, through the wrapper too; ``ops list``
  works; a ``roots`` add, list and remove round trip works, and so does ``pause`` then ``resume``;
  a home folder is refused without ``--allow-home-or-root``; the dependencies' native code loads
  under the hardened runtime, with the app's flags in force. HOME is a new empty folder, and
  PYTHONHOME and PYTHONPATH point nowhere, which isolated mode must ignore.
- ``environment`` (M6): a sitecustomize, a usercustomize, a ``.pth`` file, a startup file and a
  shadowing ``lucille_host`` are planted where PYTHONPATH, PYTHONHOME, PYTHONSTARTUP, the user
  site, PYTHONUSERBASE and the working folder would load them. For each, a control run without
  ``-I`` must load what was planted (otherwise the check proves nothing), and the same run with
  the app's flags must load nothing. PYTHONSTARTUP is read only by an interactive interpreter, so
  no run the app makes (``-m lucille_host``) could load it even without ``-I``; its control runs
  ``-i``. Then ``--version``, ``status`` and the native probe run with every one of those set,
  from the planted folder, and load nothing. And no ``.pth`` file in ``site-packages`` is missing
  from the installed ``RECORD`` files (``record`` compares those with the wheels' own).
- ``readonly`` (M8): the bundle cannot be written; ``--version``, ``status`` and ``status
  --json`` work from it, and the wrapper too; no file in the bundle changed and no bytecode
  appeared; then ``codesign --verify --strict``, and ``--deep``, still pass.
- ``swift``: compiles the app's Swift files that do not draw anything with a test driver
  (``macos/Tests/SelfTest.swift``), or takes the one ``--selftest`` names, and runs it against
  the app. It makes the calls the menu makes, through the same code.
- ``relocate``: copies the app to another folder, whose path has a space in it, and runs ``host``
  and ``swift`` on the copy.
- ``launch``: opens the app the way Finder does, checks that it is still running 10 seconds later,
  then stops it.
- ``ui``: opens the app and drives it as a user would, through System Events: reads the note
  that says what the app is for and where the menu is, when the app opens and when it is opened
  again; reads the menu bar item's name and the menu; chooses Pause Lucille, then Resume Lucille,
  and reads the name again; switches an ability on and off; adds a folder through the Open panel
  and the name dialog, with a name the app must correct first, and removes it from the Folders
  submenu; chooses the home folder, which the app asks about first, picks Choose a Smaller Folder
  once, then adds it; chooses the Data volume, and then ``/`` through a link to it, which the app
  refuses each time with Choose a Smaller Folder and Cancel and no button that adds the folder,
  and adds neither. It checks each step against the host's own state.
  It uses the real HOME and leaves the host as it found it. Where macOS does not let System Events
  drive other apps, it says so and drives nothing, which is a failure with ``--require-driven``
  (the macos-app job passes it; the smoke jobs record whether it could drive). Screenshots go to
  ``check/screens``.
- ``library-snapshot --to F`` and ``library-diff --before F`` (M12): ``~/Library`` is walked
  before ``launch`` and after ``ui`` (``Developer`` and ``Android`` skipped, no link followed). A
  new, changed or removed path whose name holds the bundle id, the app's name or "lucille" and
  that ``tools/macos/library-allowlist.txt`` does not allow is a failure; every other change is
  printed. The host's own state folder must be among the changes, the control that the walk saw
  the host's writes at all; ``--control-run`` makes those writes itself (``pause``, then
  ``resume``), for a job where ``ui`` may not have driven the app. A write to a path with an
  unrelated name is not caught. The snapshots are never uploaded.
- ``startup``: the median and fastest of ten runs of ``--version`` and ``status --json`` with no
  ``.pyc`` (as shipped), then on a writable copy with ``checked-hash`` files for the standard
  library, then for everything (decisions/0002 §6). It prints a table.
- ``suite --copy-to DIR``: the host's whole test suite on a copy of the app's runtime: the dev
  and conformance dependencies are installed into the copy only, by hash; the host's wheel and
  agent-switchboard are installed again, by MANIFEST.json's sha256, so that their console
  scripts sit beside the copy's interpreter, as the suite needs; then pytest runs with it.
- ``gatekeeper``: prints what ``spctl`` says about the app and the disk image. It never fails: an
  ad hoc signature is refused until the app is signed with a Developer ID and notarized.
- ``dmg``: ``hdiutil verify`` passes, and the mounted image holds the app, with a valid signature,
  and a link to ``/Applications``.

Moving the bytes between jobs (the smoke jobs build nothing):

- ``selftest --to F``: compiles the Swift self-test to ``F``, with the pinned Xcode.
- ``handoff``: copies the disk image beside the self-test in ``<out>/handoff`` and writes the
  cache key and both files' sha256 to ``--github-output`` (:func:`handoff_outputs`).
- ``handoff-check``: refuses unless ``<out>/handoff`` (or ``--from``) holds exactly that disk
  image and that self-test, by the sha256 the macos-app job gave.
- ``unpack-dmg``: copies the app out of that disk image into ``<out>``, and the image beside it.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

if __package__ in (None, ""):  # run as a script: make ``tools.macos`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.macos import build_app, payload  # noqa: E402

TESTS_DIR: Final = build_app.MACOS_DIR / "Tests"
ALIAS_VECTORS: Final = TESTS_DIR / "alias-vectors.json"
# The app's Swift files that use Foundation only, so a test driver can run them without a screen.
LOGIC_SOURCES: Final = ("Alias.swift", "HostCLI.swift", "HostText.swift")
# Where macOS keeps its own libraries. Most of them exist only in the system's shared cache, so
# the links check does not look for them on disk. Every other library must be in the bundle.
SYSTEM_LIBRARY_PREFIXES: Final = ("/usr/lib/", "/System/Library/")
# The load commands that name a library a Mach-O file loads.
LOAD_COMMANDS: Final = frozenset(
    {
        "LC_LOAD_DYLIB",
        "LC_LOAD_WEAK_DYLIB",
        "LC_REEXPORT_DYLIB",
        "LC_LAZY_LOAD_DYLIB",
        "LC_LOAD_UPWARD_DYLIB",
    }
)
# What the app's own executable must not import: functions that start a process or open a
# network connection, and the classes that do either. HostCLI.swift starts the interpreter with
# Foundation's Process (NSTask), which imports none of these names.
FORBIDDEN_IMPORTS: Final = frozenset(
    {
        "_fork",
        "_vfork",
        "_execl",
        "_execle",
        "_execlp",
        "_execv",
        "_execve",
        "_execvp",
        "_execvP",
        "_fexecve",
        "_system",
        "_popen",
        "_socket",
        "_socketpair",
        "_bind",
        "_listen",
        "_accept",
        "_connect",
        "_getaddrinfo",
        "_gethostbyname",
        "_dlopen",
        "_dlsym",
    }
)
FORBIDDEN_IMPORT_PREFIXES: Final = (
    "_posix_spawn",
    "_nw_",
    "_CFSocket",
    "_CFStream",
    "_CFReadStream",
    "_CFWriteStream",
    "_CFHTTP",
    "_CFHost",
    "_CFNetService",
    "_xpc_connection",
    "_OBJC_CLASS_$_NSURLSession",
    "_OBJC_CLASS_$_NSURLConnection",
    "_OBJC_CLASS_$_NSURLDownload",
    "_OBJC_CLASS_$_NSWorkspace",
    "_OBJC_CLASS_$_NSAppleScript",
    "_OBJC_CLASS_$_NSUserScriptTask",
    "_OBJC_CLASS_$_NSUserUnixTask",
    "_OBJC_CLASS_$_NSUserAppleScriptTask",
    "_OBJC_CLASS_$_NSNetService",
    "_OBJC_CLASS_$_NSStream",
    "_OBJC_CLASS_$_NSInputStream",
    "_OBJC_CLASS_$_NSOutputStream",
    "_OBJC_CLASS_$_NSSocketPort",
    "_OBJC_CLASS_$_NSXPCConnection",
    "_OBJC_CLASS_$_NSXPCListener",
    "_OBJC_CLASS_$_WKWebView",
)
# Swift names, as they appear inside a mangled symbol: Foundation's URLSession and URLRequest, and
# Network's connection and listener types.
FORBIDDEN_SWIFT_NAMES: Final = ("10URLSession", "10URLRequest", "12NWConnection", "10NWListener")
FORBIDDEN_LIBRARIES: Final = (
    "/Network.framework/",
    "/CFNetwork.framework/",
    "/NetworkExtension.framework/",
    "/WebKit.framework/",
)
# The app's words that the ui check looks for, as in App/HostText.swift and App/HostModel.swift
# (tests/test_macos_build.py checks that they agree): each listed ability's name and what Lucille
# gets from it, the note the app shows when it opens and the sentence in it on what the app is for
# (the same sentence the Info.plist gives macOS), the menu's line while paused, the menu bar item's
# name, and the Open panel's button. The app's name in them comes from tools/macos/pins.toml, which
# the app reads back from its Info.plist.
APP_NAME: Final = build_app.load_pins().app_name
ABILITY_TITLES: Final = {"files.sha256": "Check a file's fingerprint"}
ABILITY_DETAILS: Final = {
    "files.sha256": (
        "Lucille gets a code that identifies one file's exact contents, not the contents."
    )
}
WELCOME_TITLE: Final = f"{APP_NAME} is in your menu bar"
PURPOSE: Final = build_app.folder_usage(APP_NAME)
PAUSED_NOTE: Final = "Lucille's requests are turned away until you resume."
STATUS_ITEM_NAMES: Final = (APP_NAME, f"{APP_NAME}, paused")
OPEN_PROMPT: Final = f"Allow {APP_NAME} to Use This Folder"
# What the app says when the whole disk is chosen, and the only buttons that note has. The ui
# check chooses the two folders of the whole disk that the design names: the Data volume by its
# path, and ``/`` through a link to it, so that Go to Folder is given a path that names one
# folder, as everywhere else in the check.
WHOLE_DISK_TITLE: Final = f"{APP_NAME} can't use your whole disk"
WHOLE_DISK_TEXT: Final = "This folder holds every user's files on "
WHOLE_DISK_BUTTONS: Final = ("Choose a Smaller Folder", "Cancel")
WHOLE_DISK_FOLDERS: Final = (Path("/System/Volumes/Data"), Path("/"))
# The start of a button that would add the folder or allow it: Add Anyway, Add Folder, and the
# Open panel's own button.
ADDING_BUTTONS: Final = ("Add", "Allow")
# Loads the native code of the runtime dependencies, the way the host will.
NATIVE_PROBE: Final = """\
import sys
import _cffi_backend
import keyring.backends.macOS
import lucille_host._switchboard
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
X25519PrivateKey.generate().public_key()
assert sys.flags.isolated == 1, sys.flags
assert sys.flags.dont_write_bytecode == 1, sys.flags
assert sys.flags.utf8_mode == 1, sys.flags
print("native code loaded from", sys.prefix)
"""
# The runs of the host that the environment check makes with every variable it plants set, as
# (what to call it, the arguments after the interpreter and its flags).
HOST_RUNS: Final = (
    ("--version", ("-m", "lucille_host", "--version")),
    ("status", ("-m", "lucille_host", "status")),
    ("the native probe", ("-c", NATIVE_PROBE)),
)
# Every Mach-O file the runtime holds, relative to it: the interpreter, the one extension the
# pruned runtime keeps (lib-dynload/_dbm), and the two the wheels bring (cffi's backend and
# cryptography's Rust extension). The app's own executable is the fifth Mach-O file of the bundle.
# A new one is a change to review, so the bundle check refuses it until it is listed here.
RUNTIME_MACHO: Final = (
    build_app.INTERPRETER,
    f"{build_app.STDLIB}/lib-dynload/_dbm.cpython-313-darwin.so",
    f"{build_app.SITE_PACKAGES}/_cffi_backend.cpython-313-darwin.so",
    f"{build_app.SITE_PACKAGES}/cryptography/hazmat/bindings/_rust.abi3.so",
)
# Under --out: what the macos-app job hands to the smoke jobs (the disk image and the compiled
# self-test), and where the ~/Library snapshots go, outside check/, which CI uploads.
HANDOFF_DIR: Final = "handoff"
SELFTEST_NAME: Final = "selftest"
# M12: the paths under ~/Library the app may change, the folders the walk skips (Xcode's and the
# Android SDK's, which the runner's own tools write), and the host's state folder, whose change
# shows that the walk saw the host's writes.
LIBRARY_ALLOWLIST: Final = build_app.TOOLS_DIR / "library-allowlist.txt"
LIBRARY_SKIPPED: Final = ("Developer", "Android")
LIBRARY_CONTROL: Final = "Application Support/lucille-host/default/state"
# The suite needs the host's console script and Switchboard's beside the interpreter
# (tests/test_cli.py, tests/test_run_process.py); the app ships no console script.
SUITE_SCRIPTS: Final = ("lucille-host", "switchboard")
SWITCHBOARD_DISTRIBUTION: Final = "agent-switchboard"
# Start-up time: what is timed, and how many times after one warm-up run.
STARTUP_VERBS: Final = (("--version", ("--version",)), ("status --json", ("status", "--json")))
STARTUP_RUNS: Final = 10

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUN_NUMBER = re.compile(r"[1-9][0-9]{0,19}")
_CACHE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,200}")
_WHEEL_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.whl")
_CODE_FLAGS = re.compile(r"\bflags=0x[0-9a-f]+\(([^)]*)\)")


class CheckError(ValueError):
    """A check cannot go on: what it was given or found is not what it needs."""


class Checker:
    """Collects the result of each step of one check."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        if ok:
            print(f"ok: {what}", flush=True)
        else:
            print(f"FAIL: {what}" + (f"\n{detail}" if detail else ""), flush=True)
            self.failures.append(what)
        return ok


def app_path(out: Path) -> Path:
    return out / f"{build_app.load_pins().app_name}.app"


def python_in(app: Path) -> Path:
    return app / build_app.RUNTIME_IN_APP


def _shown(done: subprocess.CompletedProcess[str]) -> str:
    return f"exit {done.returncode}\nstdout: {done.stdout!r}\nstderr: {done.stderr!r}"


def host_argv(python: Path, args: Sequence[str]) -> list[str]:
    """The command line that runs the host's CLI as the app runs it: the interpreter with the
    app's flags (``-I -B -X utf8``), ``-m lucille_host``, then ``args``."""
    return [str(python), *build_app.RUNTIME_FLAGS, "-m", "lucille_host", *args]


def _home_env(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": str(home)}


def _host(python: Path, home: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the host's CLI as the app does, with a fixed environment."""
    env = {
        **_home_env(home),
        "PYTHONHOME": str(home / "no-such-python-home"),
        "PYTHONPATH": str(home / "no-such-python-path"),
    }
    argv = host_argv(python, args)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=120,
        check=False,
    )


def check_host(app: Path, checker: Checker) -> None:
    python = python_in(app)
    version = build_app.host_version()
    checker.expect(python.is_file() and not python.is_symlink(), f"{python} is a file")
    with tempfile.TemporaryDirectory(prefix="lucille-host-check-") as tmp:
        home = Path(tmp)
        done = _host(python, home, ["--version"])
        checker.expect(
            done.returncode == 0 and done.stdout == f"lucille-host {version}\n",
            f"--version prints lucille-host {version}",
            _shown(done),
        )
        done = _host(python, home, ["ops", "list"])
        lines = done.stdout.splitlines()
        checker.expect(
            done.returncode == 0 and bool(lines) and all("  " in line for line in lines),
            f"ops list lists {len(lines)} operations",
            _shown(done),
        )
        print(done.stdout, end="")

        folder = home / "Shared Folder"
        folder.mkdir()
        done = _host(python, home, ["roots", "add", "shared", str(folder)])
        checker.expect(
            done.returncode == 0 and done.stdout == "added shared\n",
            "roots add shared",
            _shown(done),
        )
        done = _host(python, home, ["roots", "list"])
        checker.expect(
            done.returncode == 0 and done.stdout == f"shared  {folder}\n",
            "roots list shows it",
            _shown(done),
        )
        done = _host(python, home, ["roots", "remove", "shared"])
        checker.expect(
            done.returncode == 0 and done.stdout == "removed shared\n",
            "roots remove shared",
            _shown(done),
        )
        done = _host(python, home, ["roots", "list"])
        checker.expect(
            done.returncode == 0 and done.stdout == "", "roots list is empty again", _shown(done)
        )
        done = _host(python, home, ["roots", "add", "home", str(home)])
        checker.expect(
            done.returncode == 1 and "--allow-home-or-root" in done.stderr,
            "the home folder is refused without --allow-home-or-root",
            _shown(done),
        )

        flag = home / "Library" / "Application Support" / "lucille-host" / "default" / "state"
        flag = flag / "paused"
        done = _host(python, home, ["pause"])
        checker.expect(
            done.returncode == 0 and flag.is_file(), "pause writes the pause flag", _shown(done)
        )
        done = _host(python, home, ["resume"])
        checker.expect(
            done.returncode == 0 and not flag.exists(), "resume removes it", _shown(done)
        )

        done = subprocess.run(
            [str(python), *build_app.RUNTIME_FLAGS, "-c", NATIVE_PROBE],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(home)},
            cwd=home,
            timeout=120,
            check=False,
        )
        checker.expect(
            done.returncode == 0 and "native code loaded" in done.stdout,
            "cffi, cryptography, keyring's macOS backend and switchboard load, with -I -B -X utf8",
            _shown(done),
        )

        wrapper = app / build_app.WRAPPER_IN_APP
        done = subprocess.run(
            [str(wrapper), "--version"],
            capture_output=True,
            text=True,
            env=_home_env(home),
            cwd=home,
            timeout=120,
            check=False,
        )
        checker.expect(
            done.returncode == 0 and done.stdout == f"lucille-host {version}\n",
            f"the wrapper {build_app.WRAPPER_IN_APP} runs it too",
            _shown(done),
        )


def compile_selftest(exe: Path) -> bool:
    """Compile the Swift self-test (the app's Foundation-only files and ``SelfTest.swift``) to
    ``exe``, with whatever Xcode ``DEVELOPER_DIR`` names; whether it compiled."""
    pins = build_app.load_pins()
    exe.parent.mkdir(parents=True, exist_ok=True)
    sources = [str(build_app.SWIFT_DIR / name) for name in LOGIC_SOURCES]
    sources.append(str(TESTS_DIR / "SelfTest.swift"))
    built = subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-target",
            f"arm64-apple-macos{pins.deployment_target}",
            "-o",
            str(exe),
            *sources,
        ],
        check=False,
    )
    return built.returncode == 0 and exe.is_file()


def check_swift(app: Path, checker: Checker, work: Path, selftest: Path | None = None) -> None:
    """Run the Swift self-test against ``app``: ``selftest`` if given (compiled once, by the
    macos-app job, and run as it is by the smoke jobs), else one compiled now."""
    if selftest is None:
        exe = work / SELFTEST_NAME
        if not checker.expect(compile_selftest(exe), "the Swift self-test compiles"):
            return
    else:
        exe = selftest
        if not checker.expect(exe.is_file(), f"the compiled self-test {exe} is there"):
            return
    with tempfile.TemporaryDirectory(prefix="lucille-host-swift-") as tmp:
        done = subprocess.run(
            [
                str(exe),
                "--app",
                str(app),
                "--home",
                tmp,
                "--version",
                build_app.host_version(),
                "--vectors",
                str(ALIAS_VECTORS),
            ],
            timeout=300,
            check=False,
        )
    checker.expect(done.returncode == 0, "the Swift self-test passes")


def make_writable(root: Path) -> None:
    """Give the owner write permission on ``root`` and everything under it, links aside: a copy of
    the read-only bundle (``ditto`` keeps its modes) can then be removed."""
    if not root.is_symlink():
        root.chmod(root.stat().st_mode | stat.S_IWUSR)
    for folder, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            path = Path(folder) / name
            if not path.is_symlink():
                path.chmod(path.lstat().st_mode | stat.S_IWUSR)


def check_relocate(app: Path, checker: Checker, work: Path, selftest: Path | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="lucille-host-moved-") as tmp:
        try:
            moved = Path(tmp) / "Some Other Folder" / "Moved Copy.app"
            moved.parent.mkdir()
            subprocess.run(["ditto", str(app), str(moved)], check=True)
            print(f"copied the app to {moved}")
            check_host(moved, checker)
            check_swift(moved, checker, work, selftest)
        finally:
            make_writable(Path(tmp))


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def minimum_macos_versions(commands: Sequence[str]) -> list[str]:
    """The oldest macOS each slice of a Mach-O file says it runs on, from ``otool -l``'s lines:
    ``minos`` of each macOS ``LC_BUILD_VERSION``, or ``version`` of an ``LC_VERSION_MIN_MACOSX``."""
    found = []
    command = None
    platform = None
    for line in commands:
        words = line.split()
        if len(words) < 2:
            continue
        if words[0] == "cmd":
            command = words[1]
            platform = None
        elif command == "LC_BUILD_VERSION" and words[0] == "platform":
            platform = words[1]
        elif command == "LC_BUILD_VERSION" and words[0] == "minos" and platform in ("1", "MACOS"):
            found.append(words[1])
        elif command == "LC_VERSION_MIN_MACOSX" and words[0] == "version":
            found.append(words[1])
    return found


def newer_than(version: str, limit: str) -> bool:
    """Whether macOS ``version`` is later than ``limit``; an unreadable version counts as later."""
    try:
        theirs, ours = _version(version), _version(limit)
    except ValueError:
        return True
    width = max(len(theirs), len(ours))
    return theirs + (0,) * (width - len(theirs)) > ours + (0,) * (width - len(ours))


def forbidden_imports(symbols: Sequence[str]) -> list[str]:
    """The symbols in ``symbols`` (what ``nm -u`` prints) that the app's executable must not
    import."""
    return [
        symbol
        for symbol in symbols
        if symbol in FORBIDDEN_IMPORTS
        or symbol.startswith(FORBIDDEN_IMPORT_PREFIXES)
        or any(name in symbol for name in FORBIDDEN_SWIFT_NAMES)
        or ("10contentsOf" in symbol and "3URLV" in symbol)
    ]


def _otool(flag: str, path: Path) -> list[str]:
    return subprocess.run(
        ["otool", flag, str(path)], capture_output=True, text=True, check=True
    ).stdout.splitlines()


def load_commands(commands: Sequence[str]) -> tuple[list[str], list[str]]:
    """What a Mach-O file loads, from ``otool -l``'s lines: the libraries its load commands name
    (:data:`LOAD_COMMANDS`), and its run paths (``LC_RPATH``), each in order and once. Its own
    install name (``LC_ID_DYLIB``) and dyld's (``LC_LOAD_DYLINKER``) are not libraries it loads."""
    libraries: list[str] = []
    rpaths: list[str] = []
    command = None
    for line in commands:
        key, _, value = line.strip().partition(" ")
        value = value.strip()
        if key == "cmd":
            command = value
            continue
        if key == "name" and command in LOAD_COMMANDS:
            found = libraries
        elif key == "path" and command == "LC_RPATH":
            found = rpaths
        else:
            continue
        name = value.rsplit(" (offset ", 1)[0]
        if name not in found:
            found.append(name)
    return libraries, rpaths


def _expand(path: str, loader: Path, executable: Path) -> Path | None:
    """``path`` with a leading ``@loader_path`` or ``@executable_path`` replaced, and ``..``
    resolved, as dyld reads it; None for any other form."""
    for token, folder in (("@loader_path", loader.parent), ("@executable_path", executable.parent)):
        if path == token or path.startswith(token + "/"):
            return Path(os.path.normpath(f"{folder}{path[len(token) :]}"))
    return None


def _shown_in(app: Path, path: Path) -> str:
    return str(path.relative_to(app)) if path.is_relative_to(app) else str(path)


def resolve_links(
    app: Path,
    file: Path,
    executable: Path,
    names: tuple[Sequence[str], Sequence[str]],
    executable_rpaths: Sequence[str] = (),
    is_file: Callable[[Path], bool] = Path.is_file,
) -> tuple[list[str], list[str]]:
    """Where each library ``file`` loads would come from, and what is wrong with any of them.

    ``names`` is what :func:`load_commands` read from ``file``, and ``executable_rpaths`` the run
    paths of ``executable``, the program that loads it (the app's own executable, or the
    interpreter). As dyld does, ``@loader_path`` is ``file``'s folder, ``@executable_path`` is
    ``executable``'s, and ``@rpath`` tries ``file``'s run paths, then ``executable``'s. A library
    from macOS (:data:`SYSTEM_LIBRARY_PREFIXES`) is not looked for on disk. Any other library must
    resolve to a file that exists inside ``app``, and each of ``file``'s run paths to a folder
    inside it: a ``..`` that leaves the bundle, an absolute path anywhere else, and a library the
    build removed are each a problem. dyld would also try the run paths of whatever loaded
    ``file``; this does not, so it can refuse, but never pass, a library only those would find.

    Returns one line for each library and run path, saying where it resolves, and the problems.
    """
    libraries, rpaths = names
    shown: list[str] = []
    problems: list[str] = []
    search: list[Path] = []
    for rpath in rpaths:
        where = _expand(rpath, file, executable)
        if where is None:
            problems.append(f"run path {rpath}: not a path inside the app")
        elif not where.is_relative_to(app):
            problems.append(f"run path {rpath}: {where} is outside the app")
        else:
            search.append(where)
            shown.append(f"run path {rpath} -> {_shown_in(app, where)}")
    for rpath in executable_rpaths:
        where = _expand(rpath, executable, executable)
        if where is not None and where.is_relative_to(app) and where not in search:
            search.append(where)
    for name in libraries:
        if name.startswith(SYSTEM_LIBRARY_PREFIXES):
            shown.append(f"{name} (macOS)")
            continue
        if name.startswith("@rpath/"):
            if not search:
                problems.append(f"{name}: no run path inside the app to look in")
                continue
            rest = name.removeprefix("@rpath/")
            candidates = [Path(os.path.normpath(folder / rest)) for folder in search]
        else:
            where = _expand(name, file, executable)
            candidates = [] if where is None else [where]
        outside = [path for path in candidates if not path.is_relative_to(app)]
        found = next((path for path in candidates if is_file(path)), None)
        if not candidates:
            problems.append(f"{name}: not from macOS, and not a path inside the app")
        elif outside:
            problems.append(f"{name}: {outside[0]} is outside the app")
        elif found is None:
            tried = ", ".join(_shown_in(app, path) for path in candidates)
            problems.append(f"{name}: no such file in the app (tried {tried})")
        else:
            shown.append(f"{name} -> {_shown_in(app, found)}")
    return shown, problems


def check_imports(executable: Path, checker: Checker) -> None:
    """The app's own executable imports nothing that starts a process or opens a connection."""
    done = subprocess.run(
        ["nm", "-u", "-j", str(executable)], capture_output=True, text=True, check=False
    )
    symbols = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    checker.expect(
        done.returncode == 0 and bool(symbols),
        f"nm lists the {len(symbols)} symbols the app's executable imports",
        _shown(done),
    )
    print("\n".join(symbols))
    bad = forbidden_imports(symbols)
    checker.expect(
        not bad,
        "the app's executable imports nothing that starts a process or opens a connection",
        "\n".join(bad),
    )
    libraries = [
        name
        for name in load_commands(_otool("-l", executable))[0]
        if any(lib in name for lib in FORBIDDEN_LIBRARIES)
    ]
    checker.expect(
        not libraries,
        "the app's executable links no networking or web framework",
        "\n".join(libraries),
    )


def check_links(app: Path, checker: Checker) -> None:
    build_dir = str(build_app.REPO_ROOT)
    minimum = build_app.load_pins().deployment_target
    main = app / "Contents" / "MacOS" / build_app.load_pins().app_name
    python = python_in(app)
    files = build_app.macho_files(app)
    checker.expect(bool(files), f"{len(files)} Mach-O files in the bundle")
    commands = {path: _otool("-l", path) for path in files}
    for path in files:
        versions = minimum_macos_versions(commands[path])
        checker.expect(
            bool(versions) and not any(newer_than(version, minimum) for version in versions),
            f"{path.relative_to(app)} runs on macOS {minimum} (it asks for {versions})",
        )
        # The app's own executable runs alone; everything else is loaded by the interpreter.
        executable = main if path.parent == main.parent else python
        names = load_commands(commands[path])
        executable_rpaths = load_commands(commands.get(executable, []))[1]
        shown, problems = resolve_links(app, path, executable, names, executable_rpaths)
        problems += [
            f"{name}: names the build folder"
            for name in [*names[0], *names[1]]
            if build_dir in name
        ]
        print(f"{path.relative_to(app)} loads:")
        for line in shown:
            print(f"  {line}")
        checker.expect(
            not problems,
            f"{path.relative_to(app)} loads only from macOS and the bundle",
            "\n".join(problems),
        )
    needle = build_dir.encode()
    holding = [
        str(path.relative_to(app))
        for path in sorted(app.rglob("*"))
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes()
    ]
    checker.expect(
        not holding,
        f"no file in the bundle holds the build folder ({build_dir})",
        "\n".join(holding[:50]),
    )
    check_imports(app / "Contents" / "MacOS" / build_app.load_pins().app_name, checker)


def _codesign_display(path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["codesign", "--display", *extra, str(path)], capture_output=True, text=True, check=False
    )


def codesign_problems(text: str, *, identifier: str, hardened: bool | None) -> list[str]:
    """What is wrong with one file's signature, from what ``codesign --display --verbose=2``
    printed: it must be signed ad hoc (``Signature=adhoc``, and ``adhoc`` among the
    CodeDirectory flags) by the build itself, not only by the linker (a wheel's extension and
    swiftc's output arrive ``linker-signed``, which also reads as ad hoc); have no Team ID; and
    carry ``identifier``. With ``hardened`` True or False, the hardened runtime flag must be there
    or must not be; with None it is not looked at. Only whole lines that start with a key are
    read, so a path that holds one in the ``Executable=`` line is not taken for it."""
    lines = text.splitlines()

    def value(key: str) -> str | None:
        found = [line[len(key) + 1 :] for line in lines if line.startswith(key + "=")]
        return found[0] if len(found) == 1 else None

    problems = []
    if value("Signature") != "adhoc":
        problems.append("it is not signed ad hoc: there is no Signature=adhoc line")
    team = value("TeamIdentifier")
    if team != "not set":
        problems.append(f"its TeamIdentifier is {team!r}, not 'not set': a Team ID is not ad hoc")
    named = value("Identifier")
    if named != identifier:
        problems.append(f"its signing identifier is {named!r}, not {identifier!r}")
    directory = [line for line in lines if line.startswith("CodeDirectory ")]
    found = _CODE_FLAGS.search(directory[0]) if len(directory) == 1 else None
    if found is None:
        problems.append("codesign shows no CodeDirectory flags")
        return problems
    flags = {flag.strip() for flag in found.group(1).split(",")}
    if "adhoc" not in flags:
        problems.append(f"its flags are {sorted(flags)}, without adhoc: it is not signed ad hoc")
    if "linker-signed" in flags:
        problems.append("it carries only the linker's signature: the build did not sign it")
    if hardened is True and "runtime" not in flags:
        problems.append("it lacks the hardened runtime, which the pins ask for")
    if hardened is False and "runtime" in flags:
        problems.append("it carries the hardened runtime, which the pins do not ask for")
    return problems


def expected_macho_files(pins: build_app.Pins) -> frozenset[str]:
    """The Mach-O files the bundle holds, relative to it: the app's executable and
    :data:`RUNTIME_MACHO`."""
    runtime = {f"{build_app.RUNTIME_DIR}/{rel}" for rel in RUNTIME_MACHO}
    return frozenset({f"Contents/MacOS/{pins.app_name}", *runtime})


def macho_set_problems(found: Iterable[str], pins: build_app.Pins) -> list[str]:
    """How the Mach-O files found in the bundle (relative paths) differ from the expected ones."""
    found = set(found)
    expected = expected_macho_files(pins)
    problems = [] if found else ["the bundle holds no Mach-O file"]
    problems += [f"{rel} is not a Mach-O file in the bundle" for rel in sorted(expected - found)]
    problems += [
        f"{rel} is a Mach-O file nobody expected: review it, then list it in RUNTIME_MACHO"
        for rel in sorted(found - expected)
    ]
    return problems


def expected_identifier(app: Path, path: Path, pins: build_app.Pins) -> str:
    """The signing identifier ``path`` must carry: the bundle identifier for the app and its
    executable, which the bundle's signature covers; what build_app gave any other file."""
    if path in (app, app / "Contents" / "MacOS" / pins.app_name):
        return pins.bundle_id
    return build_app.signing_identifier(app, path, pins)


def _signed_files(app: Path, pins: build_app.Pins) -> list[Path]:
    """The app and every Mach-O file in it but its executable, which the app's signature covers."""
    main = app / "Contents" / "MacOS" / pins.app_name
    return [app, *(path for path in build_app.macho_files(app) if path != main)]


def check_bundle(app: Path, checker: Checker) -> None:
    pins = build_app.load_pins()
    links = build_app.symlinks_in(app)
    checker.expect(not links, "no symbolic link in the bundle", "\n".join(map(str, links)))
    bytecode = build_app.bytecode_in(app)
    checker.expect(
        not bytecode, "no .pyc and no __pycache__ in the bundle", "\n".join(map(str, bytecode))
    )
    found = [path.relative_to(app).as_posix() for path in build_app.macho_files(app)]
    problems = macho_set_problems(found, pins)
    checker.expect(
        not problems,
        f"the bundle's {len(found)} Mach-O files are exactly the expected ones",
        "\n".join(problems),
    )
    for path in _signed_files(app, pins):
        done = _codesign_display(path, "--verbose=2")
        identifier = expected_identifier(app, path, pins)
        problems = codesign_problems(done.stderr, identifier=identifier, hardened=None)
        checker.expect(
            done.returncode == 0 and not problems,
            f"{path.relative_to(app.parent)}: signed ad hoc by this build as {identifier}, "
            "with no Team ID",
            "\n".join([*problems, _shown(done)]),
        )


def check_signature(app: Path, checker: Checker) -> None:
    done = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    checker.expect(done.returncode == 0, "codesign --verify --deep --strict passes", _shown(done))
    print(done.stderr, end="")
    pins = build_app.load_pins()
    hardened = pins.adhoc_hardened_runtime
    for path in _signed_files(app, pins)[1:]:
        done = subprocess.run(
            ["codesign", "--verify", "--strict", "--verbose=2", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        checker.expect(
            done.returncode == 0,
            f"{path.relative_to(app.parent)} has a valid signature of its own",
            _shown(done),
        )
    for path in _signed_files(app, pins):
        done = _codesign_display(path, "--verbose=2")
        named = [line for line in done.stderr.splitlines() if line.startswith("Identifier=")]
        print(f"{path.relative_to(app.parent)}: {named[0] if named else 'no Identifier line'}")
        identifier = expected_identifier(app, path, pins)
        problems = codesign_problems(done.stderr, identifier=identifier, hardened=hardened)
        checker.expect(
            done.returncode == 0 and not problems,
            f"{path.relative_to(app.parent)} is signed ad hoc as {identifier}, "
            f"{'with' if hardened else 'without'} the hardened runtime, as the pins say",
            "\n".join([*problems, _shown(done)]),
        )
    for path in _signed_files(app, pins):
        done = _codesign_display(path, "--entitlements", "-", "--xml")
        checker.expect(
            done.returncode == 0 and not done.stdout.strip(),
            f"{path.relative_to(app.parent)} has no entitlements",
            _shown(done),
        )
    print(_codesign_display(app, "--verbose=4").stderr, end="")


def _verify_strict(app: Path, checker: Checker) -> None:
    for extra in ([], ["--deep"]):
        argv = ["codesign", "--verify", *extra, "--strict", "--verbose=2", str(app)]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        checker.expect(done.returncode == 0, " ".join(argv[:-1]) + " passes", _shown(done))


# ---------------------------------------------------------------------------------------------
# Writing: the wrapper on a writable copy, and the read-only bundle (M8)


def _run_in(
    argv: list[str], home: Path, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=_home_env(home),
        cwd=cwd or home,
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )


def check_wrapper(app: Path, checker: Checker) -> None:
    """The wrapper and the app's flags write no bytecode, on a copy that could take it."""
    version = build_app.host_version()
    with tempfile.TemporaryDirectory(prefix="lucille-host-wrapper-") as tmp:
        root = Path(tmp)
        try:
            copy = root / "Writable Copy" / app.name
            copy.parent.mkdir()
            subprocess.run(["ditto", str(app), str(copy)], check=True)
            make_writable(copy)
            home = root / "home"
            home.mkdir()
            checker.expect(not build_app.bytecode_in(copy), "the writable copy holds no bytecode")
            # A link to the wrapper from a folder that could be on PATH, with a runtime beside
            # the link that must never run: the wrapper finds the bundle from its own real path.
            link = root / "bin" / "lucille-host"
            link.parent.mkdir()
            link.symlink_to(copy / build_app.WRAPPER_IN_APP)
            decoy = root / "runtime" / "bin" / "lucille-host-runtime"
            decoy.parent.mkdir(parents=True)
            marker = root / "decoy-ran"
            decoy.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 3\n", "ascii")
            decoy.chmod(0o755)
            for args in (["--version"], ["status"], ["ops", "list"]):
                done = _run_in([str(link), *args], home)
                ok = done.returncode == 0
                if args == ["--version"]:
                    ok = ok and done.stdout == f"lucille-host {version}\n"
                checker.expect(
                    ok,
                    f"the wrapper, through a link to it, runs `{shlex.join(args)}`",
                    _shown(done),
                )
            checker.expect(
                not marker.exists(), "it ran the bundle's runtime, not one beside the link"
            )
            done = _run_in(host_argv(copy / build_app.RUNTIME_IN_APP, ["status"]), home)
            checker.expect(
                done.returncode == 0,
                "the interpreter runs the host with the app's flags",
                _shown(done),
            )
            written = build_app.bytecode_in(copy)
            checker.expect(
                not written,
                "with -B, none of those runs wrote bytecode into the writable copy",
                "\n".join(str(path.relative_to(copy)) for path in written[:50]),
            )
            # The control: the same run without -B writes bytecode, so the check above could
            # have seen it.
            argv = [
                str(copy / build_app.RUNTIME_IN_APP),
                "-I",
                "-X",
                "utf8",
                "-m",
                "lucille_host",
                "--version",
            ]
            done = _run_in(argv, home)
            checker.expect(
                done.returncode == 0 and bool(build_app.bytecode_in(copy)),
                "without -B the same run writes bytecode into the copy, so the check above sees it",
                _shown(done),
            )
        finally:
            make_writable(root)


def file_listing(root: Path) -> dict[str, tuple[int, int, int]]:
    """``{path: (mode, size, mtime_ns)}`` of everything under ``root``, links not followed."""
    found = {}
    for folder, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            path = Path(folder) / name
            status = path.lstat()
            found[path.relative_to(root).as_posix()] = (
                status.st_mode,
                status.st_size,
                status.st_mtime_ns,
            )
    return found


def _json_lines(text: str) -> list[Any]:
    try:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        return []


def check_readonly(app: Path, checker: Checker) -> None:
    """M8: the bundle cannot be written, the host runs from it, and nothing in it changes."""
    before = file_listing(app)
    checker.expect(not build_app.bytecode_in(app), "the bundle holds no bytecode before the runs")
    for folder in (
        app / "Contents" / "Resources",
        app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES,
    ):
        probe = folder / ".lucille-host-write-probe"
        try:
            handle = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except PermissionError:
            checker.expect(True, f"{folder.relative_to(app.parent)} cannot be written")
        else:
            os.close(handle)
            probe.unlink()
            checker.expect(
                False,
                f"{folder.relative_to(app.parent)} cannot be written",
                "the bundle is writable: make it read-only first (chmod -R a-w)",
            )
    version = build_app.host_version()
    python = python_in(app)
    with tempfile.TemporaryDirectory(prefix="lucille-host-readonly-") as tmp:
        home = Path(tmp)
        done = _host(python, home, ["--version"])
        checker.expect(
            done.returncode == 0 and done.stdout == f"lucille-host {version}\n",
            "--version runs from the read-only bundle",
            _shown(done),
        )
        done = _host(python, home, ["status"])
        checker.expect(
            done.returncode == 0 and bool(done.stdout), "status runs from it", _shown(done)
        )
        print(done.stdout, end="")
        done = _host(python, home, ["status", "--json"])
        objects = _json_lines(done.stdout)
        checker.expect(
            done.returncode == 0
            and any(isinstance(item, dict) and item.get("type") == "status" for item in objects),
            "status --json runs from it, and prints a status object",
            _shown(done),
        )
        done = _run_in([str(app / build_app.WRAPPER_IN_APP), "--version"], home)
        checker.expect(
            done.returncode == 0 and done.stdout == f"lucille-host {version}\n",
            "so does the wrapper",
            _shown(done),
        )
    after = file_listing(app)
    changed = sorted(rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel))
    checker.expect(
        not changed, "nothing in the bundle changed during the runs", "\n".join(changed[:50])
    )
    checker.expect(not build_app.bytecode_in(app), "no bytecode appeared in the bundle")
    _verify_strict(app, checker)


# ---------------------------------------------------------------------------------------------
# The environment (M6)


@dataclass(frozen=True)
class EnvironmentVector:
    """One way the environment could make the runtime load something else: the variables it
    sets, the files planted for it (path: text) and the links (path: target), the markers those
    files write when they load, the arguments after the interpreter that would load them without
    ``-I`` (the control), the working folder for its runs, and a note to print."""

    name: str
    env: Mapping[str, str]
    files: Mapping[Path, str]
    markers: tuple[str, ...]
    control: tuple[str, ...]
    links: Mapping[Path, Path] = field(default_factory=dict)
    cwd: Path | None = None
    note: str = ""


def _touch(markers: Path, name: str) -> str:
    """One line of Python that creates the marker ``name``."""
    return f"import pathlib; pathlib.Path({str(markers / name)!r}).touch()\n"


def environment_vectors(
    scratch: Path,
    *,
    stdlib: Path,
    home_user_site: Path,
    userbase: Path,
    userbase_user_site: Path,
) -> list[EnvironmentVector]:
    """The vectors M6 names, each planted under ``scratch`` (the user sites are under it too:
    HOME and PYTHONUSERBASE point there). ``stdlib`` is the runtime's ``lib/python3.X``: the
    fake PYTHONHOME links to every entry of it but ``site-packages``, so that an interpreter that
    honoured PYTHONHOME would start, and load the sitecustomize planted in its own
    ``site-packages``."""
    marks = scratch / "markers"
    pythonpath = scratch / "pythonpath"
    fake_stdlib = scratch / "pythonhome" / "lib" / stdlib.name
    links = (
        {
            fake_stdlib / entry.name: entry
            for entry in sorted(stdlib.iterdir())
            if entry.name != "site-packages"
        }
        if stdlib.is_dir()
        else {}
    )
    startup = scratch / "startup.py"
    working = scratch / "working-folder"
    shadow_main = "print('a planted lucille_host')\n"
    host = ("-m", "lucille_host", "--version")
    plain = ("-c", "pass")
    return [
        EnvironmentVector(
            name="PYTHONPATH",
            env={"PYTHONPATH": str(pythonpath)},
            files={
                pythonpath / "sitecustomize.py": _touch(marks, "pythonpath-sitecustomize"),
                pythonpath / "lucille_host" / "__init__.py": _touch(marks, "pythonpath-shadow"),
                pythonpath / "lucille_host" / "__main__.py": shadow_main,
            },
            markers=("pythonpath-sitecustomize", "pythonpath-shadow"),
            control=host,
        ),
        EnvironmentVector(
            name="PYTHONHOME",
            env={"PYTHONHOME": str(scratch / "pythonhome")},
            files={
                fake_stdlib / "site-packages" / "sitecustomize.py": _touch(
                    marks, "pythonhome-sitecustomize"
                )
            },
            links=links,
            markers=("pythonhome-sitecustomize",),
            control=plain,
        ),
        EnvironmentVector(
            name="PYTHONSTARTUP",
            env={"PYTHONSTARTUP": str(startup)},
            files={startup: _touch(marks, "pythonstartup")},
            markers=("pythonstartup",),
            control=("-i",),
            note=(
                "PYTHONSTARTUP is read only by an interactive interpreter, so no run the app "
                "makes (-m lucille_host) could load it, even without -I; its control runs -i"
            ),
        ),
        EnvironmentVector(
            name="the user site",
            env={},
            files={
                home_user_site / "usercustomize.py": _touch(marks, "usersite-usercustomize"),
                home_user_site / "lucille-planted.pth": _touch(marks, "usersite-pth"),
            },
            markers=("usersite-usercustomize", "usersite-pth"),
            control=plain,
        ),
        EnvironmentVector(
            name="PYTHONUSERBASE",
            env={"PYTHONUSERBASE": str(userbase)},
            files={
                userbase_user_site / "usercustomize.py": _touch(marks, "userbase-usercustomize"),
                userbase_user_site / "lucille-planted.pth": _touch(marks, "userbase-pth"),
            },
            markers=("userbase-usercustomize", "userbase-pth"),
            control=plain,
        ),
        EnvironmentVector(
            name="the working folder",
            env={},
            files={
                working / "lucille_host" / "__init__.py": _touch(marks, "working-folder-shadow"),
                working / "lucille_host" / "__main__.py": shadow_main,
            },
            markers=("working-folder-shadow",),
            control=host,
            cwd=working,
        ),
    ]


def _plant(vectors: Sequence[EnvironmentVector]) -> None:
    for vector in vectors:
        for path, text in vector.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for path, target in vector.links.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target, target_is_directory=target.is_dir())


def _fired(
    markers: Path, argv: list[str], env: Mapping[str, str], cwd: Path
) -> tuple[subprocess.CompletedProcess[str], set[str]]:
    """Run ``argv`` with no input, and return how it ended and the markers it created."""
    for marker in markers.iterdir():
        marker.unlink()
    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=dict(env),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    return done, {marker.name for marker in markers.iterdir()}


def _user_site(python: Path, env: Mapping[str, str], cwd: Path) -> Path | None:
    argv = [str(python), "-B", "-c", "import site; print(site.getusersitepackages())"]
    done = subprocess.run(
        argv, capture_output=True, text=True, env=dict(env), cwd=cwd, timeout=120, check=False
    )
    found = done.stdout.strip()
    return Path(found) if done.returncode == 0 and found else None


def environment_check(
    python: Path,
    stdlib: Path,
    scratch: Path,
    checker: Checker,
    host_runs: Sequence[tuple[str, Sequence[str]]],
) -> None:
    """M6 with ``python``, whose standard library is ``stdlib``, planting under ``scratch``. For
    each vector: without ``-I`` (with ``-B``, so nothing is written into the runtime) its control
    must load what was planted, and with the app's flags the same run must load nothing. Then
    each of ``host_runs`` runs with the app's flags, every vector's variables set and from the
    planted working folder, and must exit 0 and load nothing."""
    home = scratch / "home"
    markers = scratch / "markers"
    elsewhere = scratch / "elsewhere"
    userbase = scratch / "userbase"
    for folder in (home, markers, elsewhere):
        folder.mkdir(parents=True)
    base = _home_env(home)
    home_site = _user_site(python, base, elsewhere)
    userbase_site = _user_site(python, {**base, "PYTHONUSERBASE": str(userbase)}, elsewhere)
    if not checker.expect(
        home_site is not None
        and home_site.is_relative_to(home)
        and userbase_site is not None
        and userbase_site.is_relative_to(userbase),
        "the runtime names a user site under HOME, and one under PYTHONUSERBASE",
        f"{home_site}, {userbase_site}",
    ):
        return
    assert home_site is not None and userbase_site is not None
    vectors = environment_vectors(
        scratch,
        stdlib=stdlib,
        home_user_site=home_site,
        userbase=userbase,
        userbase_user_site=userbase_site,
    )
    _plant(vectors)
    flags = " ".join(build_app.RUNTIME_FLAGS)
    for vector in vectors:
        if vector.note:
            print(f"note: {vector.note}")
        env = {**base, **vector.env}
        cwd = vector.cwd or elsewhere
        done, fired = _fired(markers, [str(python), "-B", *vector.control], env, cwd)
        checker.expect(
            set(vector.markers) <= fired,
            f"{vector.name}: without -I, what was planted loads ({', '.join(vector.markers)}), "
            "so this check can see it",
            f"loaded: {sorted(fired)}\n{_shown(done)}",
        )
        done, fired = _fired(
            markers, [str(python), *build_app.RUNTIME_FLAGS, *vector.control], env, cwd
        )
        checker.expect(
            not fired,
            f"{vector.name}: with {flags}, nothing planted loads",
            f"loaded: {sorted(fired)}\n{_shown(done)}",
        )
    every = dict(base)
    for vector in vectors:
        every.update(vector.env)
    working = next((vector.cwd for vector in vectors if vector.cwd is not None), elsewhere)
    for label, tail in host_runs:
        done, fired = _fired(
            markers, [str(python), *build_app.RUNTIME_FLAGS, *tail], every, working
        )
        checker.expect(
            done.returncode == 0 and not fired,
            f"{label}: with {flags}, every variable set and the planted folder as the working "
            "folder, it runs and loads nothing planted",
            f"loaded: {sorted(fired)}\n{_shown(done)}",
        )


def _record_rows(record: Path) -> list[list[str]]:
    with record.open(encoding="utf-8", newline="") as handle:
        return [row for row in csv.reader(handle) if row]


def pth_problems(site: Path) -> list[str]:
    """Each ``.pth`` file under ``site`` that no installed ``RECORD`` lists."""
    listed: set[str] = set()
    for info in payload.dist_infos(site):
        record = info / "RECORD"
        if record.is_file():
            listed.update(row[0] for row in _record_rows(record))
    found = sorted(path.relative_to(site).as_posix() for path in site.rglob("*.pth"))
    return [f"{rel} is in no RECORD" for rel in found if rel not in listed]


def check_environment(app: Path, checker: Checker) -> None:
    with tempfile.TemporaryDirectory(prefix="lucille-host-environment-") as tmp:
        environment_check(
            python_in(app),
            app / build_app.RUNTIME_DIR / build_app.STDLIB,
            Path(tmp),
            checker,
            HOST_RUNS,
        )
    problems = pth_problems(app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES)
    checker.expect(
        not problems,
        "no .pth file in site-packages that an installed RECORD does not list (the record "
        "check compares those RECORD files with the wheels' own)",
        "\n".join(problems),
    )


# ---------------------------------------------------------------------------------------------
# The wheels (M10) and the version (M11)


def reference_folders(out: Path) -> list[Path]:
    """Where the macos-app job keeps the wheels it installed: the build's download cache, and
    the host wheel ``build_app.py wheel`` built and ``assemble`` copied."""
    return [out / "cache" / "wheels", out / "host-wheel", out / "work" / "host-wheel"]


def find_reference_wheels(
    manifest: Mapping[str, Any], folders: Sequence[Path]
) -> tuple[dict[str, Path], list[str]]:
    """``({file name: path}, problems)``: each wheel MANIFEST.json names, the lock's and the
    host's, found in the first of ``folders`` that holds it, and hashed again. A wheel missing,
    a name that is not a plain file name, or a sha256 that is not the manifest's is a problem."""
    wheels = manifest.get("wheels") or []
    problems = [] if wheels else ["MANIFEST.json lists no wheel"]
    host = (manifest.get("host") or {}).get("wheel") or {}
    entries = [(wheel.get("file"), wheel.get("sha256")) for wheel in wheels]
    entries.append((host.get("file"), host.get("sha256")))
    found: dict[str, Path] = {}
    for name, digest in entries:
        if not isinstance(name, str) or _WHEEL_FILE.fullmatch(name) is None:
            problems.append(f"MANIFEST.json names a wheel {name!r}, which is not a file name")
            continue
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            problems.append(f"MANIFEST.json gives {name} the sha256 {digest!r}")
            continue
        path = next((folder / name for folder in folders if (folder / name).is_file()), None)
        if path is None:
            problems.append(f"{name}: in none of {', '.join(map(str, folders))}")
            continue
        actual = payload.sha256_of(path)
        if actual != digest:
            problems.append(f"{name}: its sha256 is {actual}, but MANIFEST.json says {digest}")
            continue
        found[name] = path
    return found, problems


def lock_wheel_problems(
    manifest: Mapping[str, Any],
    lock: Mapping[tuple[str, str], Sequence[payload.LockWheel]],
    *,
    host_wheel_sha256: str,
) -> list[str]:
    """How MANIFEST.json's wheels differ from what uv.lock pins, and its host wheel from the one
    the wheel step built. Each wheel must be one ``lock`` lists for that distribution and
    version, by file name, URL and sha256; the host wheel's sha256 must be
    ``host_wheel_sha256``. MANIFEST.json and the wheels beside it are written by the same build,
    so their agreeing with each other proves nothing about where the wheels came from."""
    problems = []
    for wheel in manifest.get("wheels") or []:
        name, version = payload.normal_name(str(wheel.get("name"))), str(wheel.get("version"))
        pinned = [(pin.filename, pin.url, pin.sha256) for pin in lock.get((name, version), ())]
        if (wheel.get("file"), wheel.get("url"), wheel.get("sha256")) not in pinned:
            problems.append(
                f"{name}=={version}: {wheel.get('file')} with the sha256 {wheel.get('sha256')} "
                "is not a wheel uv.lock pins"
            )
    host = ((manifest.get("host") or {}).get("wheel") or {}).get("sha256")
    if host != host_wheel_sha256:
        problems.append(
            f"MANIFEST.json's host wheel has the sha256 {host}; the wheel step built "
            f"{host_wheel_sha256}"
        )
    return problems


def distribution_set_problems(
    installed: Mapping[str, str], manifest: Mapping[str, Any]
) -> list[str]:
    """How the installed distributions ``{name: version}`` differ from MANIFEST.json's wheels
    and the host."""
    expected = {
        payload.normal_name(wheel["name"]): wheel["version"]
        for wheel in manifest.get("wheels") or []
    }
    problems = [] if expected else ["MANIFEST.json lists no wheel"]
    expected[payload.HOST_DISTRIBUTION] = (manifest.get("host") or {}).get("version")
    for name in sorted(set(expected) | set(installed)):
        if expected.get(name) != installed.get(name):
            problems.append(
                f"{name}: installed {installed.get(name)}, MANIFEST.json says {expected.get(name)}"
            )
    return problems


def split_record_problems(
    problems: Sequence[str], macho: Iterable[str]
) -> tuple[list[str], list[str]]:
    """``payload.record_problems``' findings on the signed bundle, split into those that fail
    and those only reported: that a Mach-O file in ``macho`` (paths relative to site-packages)
    does not match its ``RECORD`` row or its wheel's bytes, because codesign rewrote it in place.
    Everything else fails, a Mach-O file that no ``RECORD`` lists included."""
    names = set(macho)
    failing, signed = [], []
    for problem in problems:
        rel, differs, _ = problem.partition(" differs from the file in ")
        if not differs:
            rel, mismatched, rest = problem.partition(" does not match its RECORD row")
            differs = mismatched if not rest else ""
        (signed if differs and rel in names else failing).append(problem)
    return failing, signed


def _stripped_comparison(
    site: Path, macho: Sequence[str], wheels: Sequence[Path]
) -> list[tuple[str, str]]:
    """Each Mach-O file in ``site`` against the same file in its wheel, after ``codesign
    --remove-signature`` on a copy of each: SAME or DIFFERENT."""
    holders: dict[str, Path] = {}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                holders.setdefault(name, wheel)
    results = []
    with tempfile.TemporaryDirectory(prefix="lucille-host-record-") as tmp:
        for number, rel in enumerate(macho):
            wheel = holders.get(rel)
            if wheel is None:
                results.append((rel, "in no wheel"))
                continue
            ours, theirs = Path(tmp) / f"{number}-bundle", Path(tmp) / f"{number}-wheel"
            shutil.copyfile(site / rel, ours)
            with zipfile.ZipFile(wheel) as archive:
                theirs.write_bytes(archive.read(rel))
            for copy in (ours, theirs):
                subprocess.run(
                    ["codesign", "--remove-signature", str(copy)], capture_output=True, check=False
                )
            results.append(
                (rel, "SAME" if ours.read_bytes() == theirs.read_bytes() else "DIFFERENT")
            )
    return results


def _summarize(summary: Path | None, text: str) -> None:
    print(text)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def _manifest(app: Path) -> dict[str, Any]:
    return json.loads((app / build_app.MANIFEST_PATH).read_text(encoding="utf-8"))


def check_record(
    app: Path, out: Path, checker: Checker, summary: Path | None, *, host_wheel_sha256: str
) -> None:
    """M10 on the signed bundle (the build checked it byte for byte before signing): every wheel
    MANIFEST.json names is one uv.lock pins and the host wheel is the one the wheel step built
    (``host_wheel_sha256``); each is here, with its sha256; the installed distributions are
    exactly those; and each site-packages file is a file of its wheel."""
    manifest = _manifest(app)
    lock = payload.parse_lock(build_app.LOCK_FILE.read_text(encoding="utf-8"))
    problems = lock_wheel_problems(manifest, lock, host_wheel_sha256=host_wheel_sha256)
    checker.expect(
        not problems,
        "MANIFEST.json's wheels are uv.lock's, and its host wheel is the one the wheel step built",
        "\n".join(problems),
    )
    wheels, problems = find_reference_wheels(manifest, reference_folders(out))
    if not checker.expect(
        not problems,
        f"each of the {len(wheels)} wheels MANIFEST.json names is here, with its sha256",
        "\n".join(problems),
    ):
        return
    site = app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES
    installed = payload.installed_versions(site)
    problems = distribution_set_problems(installed, manifest)
    checker.expect(
        not problems,
        f"the {len(installed)} installed distributions are exactly MANIFEST.json's wheels and "
        "lucille-host",
        "\n".join(problems),
    )
    macho = [path.relative_to(site).as_posix() for path in build_app.macho_files(site)]
    failing, signed = split_record_problems(
        payload.record_problems(site, list(wheels.values())), macho
    )
    checker.expect(
        not failing,
        "every site-packages file is byte for byte a file of its wheel's RECORD, apart from the "
        "installer's named files and the Mach-O files signing rewrote (M10)",
        "\n".join(failing[:100]),
    )
    compared = _stripped_comparison(site, macho, list(wheels.values()))
    lines = [
        "### M10 after signing: each Mach-O file in site-packages against its wheel's",
        "",
        "Compared after `codesign --remove-signature` on a copy of each. Reported, not a gate: "
        "the build compared every file byte for byte before signing.",
        "",
        "| file | without signatures |",
        "|---|---|",
        *(f"| `{rel}` | {verdict} |" for rel, verdict in compared),
        "",
        f"{len(signed)} findings of the RECORD check were about these signed files only.",
        "",
    ]
    _summarize(summary, "\n".join(lines))


def version_problems(
    plist: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    versions: build_app.BundleVersions,
    host_version: str,
    bundled_version: str,
    pins: build_app.Pins,
    tag: str | None,
    build_number: str | None,
) -> list[str]:
    """How the built app differs from the version rule (M11): Info.plist's two strings are
    ``versions``; MANIFEST.json gives the same app, the version inputs and the host's real
    version, which the bundled host prints; and a build from a tag has one version number."""
    problems = []

    def same(what: str, got: Any, want: Any) -> None:
        if got != want:
            problems.append(f"{what} is {got!r}, not {want!r}")

    same("CFBundleShortVersionString", plist.get("CFBundleShortVersionString"), versions.short)
    same("CFBundleVersion", plist.get("CFBundleVersion"), versions.build)
    same("CFBundleIdentifier", plist.get("CFBundleIdentifier"), pins.bundle_id)
    same("LSMinimumSystemVersion", plist.get("LSMinimumSystemVersion"), pins.deployment_target)
    same("the bundled host's --version", bundled_version, f"lucille-host {host_version}\n")
    same(
        "MANIFEST.json's app",
        manifest.get("app"),
        {
            "name": pins.app_name,
            "bundle_id": pins.bundle_id,
            "runtime_id": pins.runtime_id,
            "deployment_target": pins.deployment_target,
            "short_version": versions.short,
            "bundle_version": versions.build,
        },
    )
    build = manifest.get("build") or {}
    same("MANIFEST.json's build tag", build.get("tag"), tag)
    same("MANIFEST.json's build number", build.get("build_number"), build_number)
    same("MANIFEST.json's host version", (manifest.get("host") or {}).get("version"), host_version)
    if tag is not None and not versions.short == versions.build == host_version == tag[1:]:
        problems.append(f"a build from {tag} has more than one version number")
    return problems


def check_version(app: Path, checker: Checker, tag: str | None, build_number: str | None) -> None:
    pins = build_app.load_pins()
    version = build_app.host_version()
    versions = build_app.bundle_versions(version, tag, build_number)
    plist = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    with tempfile.TemporaryDirectory(prefix="lucille-host-version-") as tmp:
        done = _host(python_in(app), Path(tmp), ["--version"])
    problems = version_problems(
        plist,
        _manifest(app),
        versions=versions,
        host_version=version,
        bundled_version=done.stdout,
        pins=pins,
        tag=tag,
        build_number=build_number,
    )
    checker.expect(
        not problems,
        f"Info.plist says {versions.short} ({versions.build}), and MANIFEST.json and the bundled "
        f"host give the host's own version, {version} (M11)",
        "\n".join(problems),
    )


# ---------------------------------------------------------------------------------------------
# ~/Library (M12)


class AllowlistError(CheckError):
    """tools/macos/library-allowlist.txt is not well formed."""


@dataclass(frozen=True)
class AllowRule:
    """One line of the allowlist: a folder and everything in it (``prefix``), or a path matched
    one segment at a time; and why the app may change it."""

    pattern: str
    prefix: bool
    reason: str


def parse_library_allowlist(text: str, *, bundle_id: str, app_name: str) -> tuple[AllowRule, ...]:
    """The rules in ``text``: one per line, a path relative to ``~/Library``, then ``#`` and the
    reason, which every line must give. ``{bundle_id}`` and ``{app_name}`` are replaced. A line
    ending ``/**`` allows that one literal folder and everything under it; any other line is
    matched one path segment at a time with ``fnmatchcase``, so ``*`` never crosses a ``/``.
    Refused: no reason, an absolute path, an empty, ``.`` or ``..`` segment, a backslash, any
    other ``{...}`` or ``**``, a pattern in a folder rule, and a line given twice."""
    rules: list[AllowRule] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{LIBRARY_ALLOWLIST.name}:{number}: {line!r}"
        entry, sep, reason = line.partition(" #")
        entry, reason = entry.strip(), reason.strip()
        if not sep or not reason:
            raise AllowlistError(f"{where}: every line ends with '#' and the reason for it")
        entry = entry.replace("{bundle_id}", bundle_id).replace("{app_name}", app_name)
        if any(char in entry for char in "{}\\"):
            raise AllowlistError(f"{where}: only {{bundle_id}} and {{app_name}}, and no backslash")
        prefix = entry.endswith("/**")
        if prefix:
            entry = entry.removesuffix("/**")
        if entry.startswith("/") or any(part in ("", ".", "..") for part in entry.split("/")):
            raise AllowlistError(f"{where}: a path relative to ~/Library, with no . or ..")
        if "**" in entry:
            raise AllowlistError(f"{where}: ** only as the last segment of a folder rule")
        if prefix and any(char in entry for char in "*?["):
            raise AllowlistError(f"{where}: a folder rule names one literal folder")
        if any(rule.pattern == entry and rule.prefix == prefix for rule in rules):
            raise AllowlistError(f"{where}: given twice")
        rules.append(AllowRule(entry, prefix, reason))
    return tuple(rules)


def library_allowed(rel: str, rules: Sequence[AllowRule]) -> bool:
    """Whether a path relative to ``~/Library`` is one the rules allow."""
    parts = rel.split("/")
    for rule in rules:
        if rule.prefix:
            if rel == rule.pattern or rel.startswith(rule.pattern + "/"):
                return True
            continue
        segments = rule.pattern.split("/")
        if len(segments) == len(parts) and all(
            fnmatch.fnmatchcase(part, segment)
            for part, segment in zip(parts, segments, strict=True)
        ):
            return True
    return False


def library_snapshot(root: Path) -> dict[str, Any]:
    """Every path under ``root`` (``~/Library``) with its kind, size and modification time, and
    what could not be read. Links are recorded, not followed; ``Developer`` and ``Android`` at
    the top are skipped."""
    entries: dict[str, list[Any]] = {}
    unreadable: list[str] = []

    def failed(exc: OSError) -> None:
        unreadable.append(f"{exc.filename}: {exc.strerror}")

    for folder, dirs, files in os.walk(root, onerror=failed):
        here = Path(folder)
        if here == root:
            dirs[:] = [name for name in dirs if name not in LIBRARY_SKIPPED]
        for name in sorted((*dirs, *files)):
            path = here / name
            try:
                status = path.lstat()
            except OSError as exc:
                failed(exc)
                continue
            if stat.S_ISLNK(status.st_mode):
                kind = "link"
            elif stat.S_ISDIR(status.st_mode):
                kind = "dir"
            elif stat.S_ISREG(status.st_mode):
                kind = "file"
            else:
                kind = "other"
            size = status.st_size if kind == "file" else 0
            entries[path.relative_to(root).as_posix()] = [kind, size, status.st_mtime_ns]
    return {"root": str(root), "entries": entries, "unreadable": sorted(unreadable)}


def library_changes(
    before: Mapping[str, Sequence[Any]], after: Mapping[str, Sequence[Any]]
) -> list[tuple[str, str]]:
    """``(path, "new" | "removed" | "changed")`` for each path that differs, sorted."""
    changes = []
    for rel in sorted(set(before) | set(after)):
        if rel not in before:
            changes.append((rel, "new"))
        elif rel not in after:
            changes.append((rel, "removed"))
        elif list(before[rel]) != list(after[rel]):
            changes.append((rel, "changed"))
    return changes


@dataclass(frozen=True)
class LibraryVerdict:
    failures: list[str]
    allowed: list[str]
    noise: list[str]
    control_seen: bool


def library_problems(
    changes: Sequence[tuple[str, str]],
    rules: Sequence[AllowRule],
    *,
    bundle_id: str,
    app_name: str,
) -> LibraryVerdict:
    """Sort the changes: those the rules allow; those whose path names the bundle id, the app's
    name or "lucille" (in any case) and that no rule allows, which fail; and the rest, which are
    printed. ``control_seen`` says whether the host's state folder is among them."""
    needles = (bundle_id.lower(), app_name.lower(), "lucille")
    failures, allowed, noise = [], [], []
    for rel, what in changes:
        line = f"{what}: {rel}"
        if library_allowed(rel, rules):
            allowed.append(line)
        elif any(needle in rel.lower() for needle in needles):
            failures.append(line)
        else:
            noise.append(line)
    control = any(
        rel == LIBRARY_CONTROL or rel.startswith(LIBRARY_CONTROL + "/") for rel, _ in changes
    )
    return LibraryVerdict(failures, allowed, noise, control)


def _library() -> Path:
    return Path.home() / "Library"


def check_library_snapshot(to: Path, checker: Checker) -> None:
    snapshot = library_snapshot(_library())
    to.parent.mkdir(parents=True, exist_ok=True)
    to.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
    checker.expect(
        bool(snapshot["entries"]),
        f"{len(snapshot['entries'])} paths under {snapshot['root']} recorded in {to}",
        "\n".join(snapshot["unreadable"][:20]),
    )


def _control_writes(app: Path, checker: Checker) -> None:
    """Make the host write its state folder under the real HOME, as the ui check does when it
    drives Pause and Resume: ``pause`` then ``resume`` (or the other way round, if it is paused,
    so that it is left as it was)."""
    python = python_in(app)
    flag = _library() / LIBRARY_CONTROL / "paused"
    verbs = (["resume"], ["pause"]) if flag.exists() else (["pause"], ["resume"])
    for verb in verbs:
        done = _host(python, Path.home(), verb)
        checker.expect(
            done.returncode == 0, f"the control: the host runs `{verb[0]}`", _shown(done)
        )


def check_library_diff(app: Path, before_file: Path, control_run: bool, checker: Checker) -> None:
    pins = build_app.load_pins()
    rules = parse_library_allowlist(
        LIBRARY_ALLOWLIST.read_text(encoding="utf-8"),
        bundle_id=pins.bundle_id,
        app_name=pins.app_name,
    )
    before = json.loads(before_file.read_text(encoding="utf-8"))
    if control_run:
        _control_writes(app, checker)
    after = library_snapshot(_library())
    checker.expect(before.get("root") == after["root"], f"both snapshots are of {after['root']}")
    verdict = library_problems(
        library_changes(before.get("entries", {}), after["entries"]),
        rules,
        bundle_id=pins.bundle_id,
        app_name=pins.app_name,
    )
    for title, lines in (
        ("allowed", verdict.allowed),
        ("not the app's, by their names (printed only)", verdict.noise),
    ):
        print(f"{len(lines)} changes {title}:")
        for line in lines[:500]:
            print(f"  {line}")
    if after["unreadable"]:
        print("not readable, so not compared: " + "; ".join(after["unreadable"][:50]))
    checker.expect(
        verdict.control_seen,
        f"the walk saw the host's own writes to ~/Library/{LIBRARY_CONTROL} (the control)",
        "without them, an empty diff would prove nothing",
    )
    checker.expect(
        not verdict.failures,
        "nothing that names the app or the host changed under ~/Library outside "
        f"{LIBRARY_ALLOWLIST.name} (M12)",
        "\n".join(verdict.failures),
    )


# ---------------------------------------------------------------------------------------------
# The hand-off to the smoke jobs, and the disk image unpacked there


def handoff_key(app_name: str, run_id: str, run_attempt: str) -> str:
    """The cache key the macos-app job saves the disk image and the self-test under."""
    for what, value in (("run id", run_id), ("run attempt", run_attempt)):
        if _RUN_NUMBER.fullmatch(value) is None:
            raise CheckError(f"the {what} must be a positive whole number, not {value!r}")
    return f"{app_name.lower().replace(' ', '-')}-adhoc-macos-arm64-{run_id}-{run_attempt}"


def handoff_outputs(key: str, dmg_sha256: str, selftest_sha256: str) -> str:
    """The lines for ``$GITHUB_OUTPUT``: the cache key and the two files' sha256. Each value is
    checked whole, so nothing can add a line."""
    if _CACHE_KEY.fullmatch(key) is None:
        raise CheckError(f"{key!r} is not a cache key")
    for digest in (dmg_sha256, selftest_sha256):
        if _SHA256.fullmatch(digest) is None:
            raise CheckError(f"{digest!r} is not a sha256")
    return f"cache_key={key}\ndmg_sha256={dmg_sha256}\nselftest_sha256={selftest_sha256}\n"


def handoff_problems(
    folder: Path, app_name: str, dmg_sha256: str, selftest_sha256: str
) -> list[str]:
    """Why ``folder`` is not exactly what the macos-app job handed on: one disk image and the
    compiled self-test, nothing else, with the sha256 that job gave for each."""
    problems = []
    for what, digest in (("the disk image", dmg_sha256), ("the self-test", selftest_sha256)):
        if _SHA256.fullmatch(digest) is None:
            problems.append(f"the sha256 given for {what}, {digest!r}, is not one")
    if not folder.is_dir():
        return [*problems, f"{folder} is not there"]
    dmgs = build_app.disk_images(folder, app_name)
    if len(dmgs) != 1:
        problems.append(f"{folder} holds {len(dmgs)} disk images, not one")
    names = {entry.name for entry in folder.iterdir()}
    extra = sorted(names - {SELFTEST_NAME, *(dmg.name for dmg in dmgs)})
    if extra:
        problems.append(f"{folder} also holds {extra}")
    selftest = folder / SELFTEST_NAME
    files = [(selftest, selftest_sha256)] + ([(dmgs[0], dmg_sha256)] if len(dmgs) == 1 else [])
    for path, digest in files:
        if path.is_symlink() or not path.is_file():
            problems.append(f"{path.name} is not a file in {folder}")
            continue
        actual = payload.sha256_of(path)
        if actual != digest:
            problems.append(f"{path.name}: its sha256 is {actual}, not {digest}")
    return problems


def write_handoff(
    out: Path, run_id: str, run_attempt: str, github_output: Path, checker: Checker
) -> None:
    """Copy the disk image beside the compiled self-test in ``<out>/handoff``, and give the
    cache key and the sha256 of both to the job's later steps and the smoke jobs."""
    pins = build_app.load_pins()
    folder = out / HANDOFF_DIR
    dmgs = build_app.disk_images(out, pins.app_name)
    selftest = folder / SELFTEST_NAME
    if not checker.expect(len(dmgs) == 1, "the build made one disk image", f"{dmgs}"):
        return
    if not checker.expect(selftest.is_file(), f"the self-test is compiled at {selftest}"):
        return
    for old in build_app.disk_images(folder, pins.app_name):
        old.unlink()
    shutil.copyfile(dmgs[0], folder / dmgs[0].name)
    key = handoff_key(pins.app_name, run_id, run_attempt)
    dmg_sha256, selftest_sha256 = payload.sha256_of(dmgs[0]), payload.sha256_of(selftest)
    text = handoff_outputs(key, dmg_sha256, selftest_sha256)
    problems = handoff_problems(folder, pins.app_name, dmg_sha256, selftest_sha256)
    if not checker.expect(
        not problems, f"{folder} holds the disk image and the self-test", "\n".join(problems)
    ):
        return
    with github_output.open("a", encoding="utf-8") as handle:
        handle.write(text)
    print(text, end="")


def check_handoff(folder: Path, dmg_sha256: str, selftest_sha256: str, checker: Checker) -> None:
    problems = handoff_problems(folder, build_app.load_pins().app_name, dmg_sha256, selftest_sha256)
    checker.expect(
        not problems,
        f"{folder} holds exactly the disk image and the self-test the macos-app job built",
        "\n".join(problems),
    )


def unpack_dmg(out: Path, source: Path, checker: Checker) -> None:
    """Copy the app out of the one disk image in ``source`` into ``out``, and the image into
    ``out`` beside it, for the checks that follow."""
    pins = build_app.load_pins()
    dmgs = build_app.disk_images(source, pins.app_name)
    if not checker.expect(len(dmgs) == 1, f"{source} holds one disk image", f"{dmgs}"):
        return
    app = app_path(out)
    if not checker.expect(not app.exists(), f"there is no app at {app} yet"):
        return
    out.mkdir(parents=True, exist_ok=True)
    if source.resolve() != out.resolve():
        for old in build_app.disk_images(out, pins.app_name):
            old.unlink()
        shutil.copyfile(dmgs[0], out / dmgs[0].name)
    with tempfile.TemporaryDirectory(prefix="lucille-host-unpack-") as tmp:
        mount = Path(tmp) / "mount"
        mount.mkdir()
        done = build_app.hdiutil(
            [
                "attach",
                "-nobrowse",
                "-readonly",
                "-noautoopen",
                "-mountpoint",
                str(mount),
                str(dmgs[0]),
            ]
        )
        if not checker.expect(done.returncode == 0, "the image mounts", _shown(done)):
            return
        try:
            done = subprocess.run(
                ["ditto", str(mount / app.name), str(app)],
                capture_output=True,
                text=True,
                check=False,
            )
            checker.expect(
                done.returncode == 0 and app.is_dir(),
                f"{app.name} is copied out of it",
                _shown(done),
            )
        finally:
            for extra in ([], ["-force"]):
                if build_app.hdiutil(["detach", *extra, str(mount)]).returncode == 0:
                    break


# ---------------------------------------------------------------------------------------------
# Start-up time, and the suite on a copy of the runtime


def timing(samples: Sequence[float]) -> tuple[float, float]:
    """The median and the fastest of ``samples`` (seconds), in milliseconds."""
    return round(statistics.median(samples) * 1000, 1), round(min(samples) * 1000, 1)


def startup_table(results: Mapping[str, Mapping[str, tuple[float, float]]]) -> str:
    """Markdown: one row per state of the bytecode, the median and fastest of each verb."""
    head = " | ".join(f"{verb} median | {verb} fastest" for verb, _ in STARTUP_VERBS)
    lines = [
        f"### Start-up time of the bundled host, ms (median and fastest of {STARTUP_RUNS} runs "
        "after one warm-up)",
        "",
        f"| bytecode | {head} |",
        "|---|" + "---|" * (2 * len(STARTUP_VERBS)),
    ]
    for label, row in results.items():
        cells = [f"{row[verb][index]:.0f}" for verb, _ in STARTUP_VERBS for index in (0, 1)]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _timed(python: Path, home: Path) -> dict[str, tuple[float, float]] | None:
    row = {}
    for verb, args in STARTUP_VERBS:
        argv = host_argv(python, args)
        samples = []
        for run in range(STARTUP_RUNS + 1):
            started = time.perf_counter()
            done = _run_in(argv, home)
            elapsed = time.perf_counter() - started
            if done.returncode != 0:
                print(_shown(done))
                return None
            if run:
                samples.append(elapsed)
        row[verb] = timing(samples)
    return row


def check_startup(app: Path, work: Path, checker: Checker, summary: Path | None) -> None:
    """Time the host with no .pyc, as shipped; then, on a writable copy, with checked-hash .pyc
    files for the standard library, then for everything."""
    results: dict[str, dict[str, tuple[float, float]]] = {}
    with tempfile.TemporaryDirectory(prefix="lucille-host-startup-") as tmp:
        root = Path(tmp)
        try:
            home = root / "home"
            home.mkdir()
            row = _timed(python_in(app), home)
            if not checker.expect(row is not None, "the host runs from the bundle"):
                return
            results["none (as shipped)"] = row or {}
            copy = root / app.name
            subprocess.run(["ditto", str(app), str(copy)], check=True)
            make_writable(copy)
            python = python_in(copy)
            stdlib = copy / build_app.RUNTIME_DIR / build_app.STDLIB
            for label, target, skip in (
                ("checked-hash, standard library", stdlib, ["-x", "site-packages"]),
                ("checked-hash, everything", stdlib / "site-packages", []),
            ):
                argv = [str(python), "-I", "-m", "compileall", "-q", "-j", "0"]
                argv += ["--invalidation-mode", "checked-hash", *skip, str(target)]
                done = subprocess.run(argv, capture_output=True, text=True, check=False)
                if not checker.expect(done.returncode == 0, f"compileall: {label}", _shown(done)):
                    return
                row = _timed(python, home)
                if not checker.expect(row is not None, f"the host runs with {label}"):
                    return
                results[label] = row or {}
        finally:
            make_writable(root)
    work.mkdir(parents=True, exist_ok=True)
    (work / "startup.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    _summarize(summary, startup_table(results))


def suite_export_argv() -> list[str]:
    """``uv export`` of everything the host's test suite needs: the dev group and the
    conformance extra, with every hash. No uv configuration file is read."""
    return [
        "uv",
        "export",
        "--no-config",
        "--frozen",
        "--no-emit-project",
        "--group",
        "dev",
        "--extra",
        "conformance",
        "--format",
        "requirements-txt",
    ]


def suite_install_argv(
    python: Path,
    requirements: Path,
    *,
    find_links: Path | None = None,
    reinstall: Sequence[str] = (),
) -> list[str]:
    """``uv pip install`` into the runtime ``python`` (a copy of the app's), hashes required,
    wheels only, no dependencies beyond the list, no configuration file; with ``find_links``,
    from that folder alone. Wheels only: a pin with no wheel fails, rather than being built by a
    build backend nothing pins. The copy is not a virtual environment, and is the suite's own, so
    it is installed into even if it were marked as managed by something else (the pinned runtime
    is not)."""
    argv = ["uv", "pip", "install", "--python", str(python), "--require-hashes", "--no-deps"]
    argv += ["--only-binary", ":all:"]
    argv += ["--link-mode", "copy", "--no-config", "--no-python-downloads"]
    argv += ["--break-system-packages"]
    if find_links is not None:
        argv += ["--no-index", "--find-links", str(find_links)]
    for name in reinstall:
        argv += ["--reinstall-package", name]
    return [*argv, "-r", str(requirements)]


def suite_reinstall_text(manifest: Mapping[str, Any]) -> str:
    """The host and agent-switchboard, each by the sha256 MANIFEST.json gives, as requirements:
    installed again into the suite's copy so that their console scripts are written there."""
    host = manifest["host"]
    hub_wheels = [
        wheel
        for wheel in manifest.get("wheels") or []
        if payload.normal_name(wheel["name"]) == SWITCHBOARD_DISTRIBUTION
    ]
    if len(hub_wheels) != 1:
        raise CheckError(f"MANIFEST.json lists {len(hub_wheels)} {SWITCHBOARD_DISTRIBUTION} wheels")
    pins = [
        (payload.HOST_DISTRIBUTION, host["version"], host["wheel"]["sha256"]),
        (SWITCHBOARD_DISTRIBUTION, hub_wheels[0]["version"], hub_wheels[0]["sha256"]),
    ]
    for name, _, digest in pins:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise CheckError(f"MANIFEST.json gives {name} the sha256 {digest!r}")
    return "".join(f"{name}=={version} --hash=sha256:{digest}\n" for name, version, digest in pins)


def check_suite(app: Path, out: Path, copy_to: Path, checker: Checker) -> None:
    """The host's whole test suite, run by a copy of the app's runtime with the dev and
    conformance dependencies installed into the copy only."""
    manifest = _manifest(app)
    wheels, problems = find_reference_wheels(manifest, reference_folders(out))
    if not checker.expect(
        not problems, "the wheels MANIFEST.json names are here", "\n".join(problems)
    ):
        return
    if copy_to.exists():
        make_writable(copy_to)
        shutil.rmtree(copy_to)
    copy_to.mkdir(parents=True)
    copy = copy_to / app.name
    subprocess.run(["ditto", str(app), str(copy)], check=True)
    make_writable(copy)
    python = python_in(copy)
    # uv's own configuration from the environment could add an index or change what it picks.
    env = payload.without_uv_settings(os.environ)
    exported = subprocess.run(
        suite_export_argv(),
        capture_output=True,
        text=True,
        cwd=build_app.REPO_ROOT,
        env=env,
        check=False,
    )
    if not checker.expect(
        exported.returncode == 0, "uv exports the dev and conformance pins", _shown(exported)
    ):
        return
    requirements = copy_to / "suite-requirements.txt"
    requirements.write_text(exported.stdout, encoding="utf-8")
    done = subprocess.run(suite_install_argv(python, requirements), env=env, check=False)
    if not checker.expect(done.returncode == 0, "they install into the copy, by hash"):
        return
    find_links = copy_to / "wheels"
    find_links.mkdir()
    for name in (
        manifest["host"]["wheel"]["file"],
        *(
            wheel["file"]
            for wheel in manifest["wheels"]
            if payload.normal_name(wheel["name"]) == SWITCHBOARD_DISTRIBUTION
        ),
    ):
        shutil.copyfile(wheels[name], find_links / name)
    again = copy_to / "reinstall.txt"
    again.write_text(suite_reinstall_text(manifest), encoding="utf-8")
    argv = suite_install_argv(
        python,
        again,
        find_links=find_links,
        reinstall=(payload.HOST_DISTRIBUTION, SWITCHBOARD_DISTRIBUTION),
    )
    done = subprocess.run(argv, env=env, check=False)
    checker.expect(
        done.returncode == 0, "the host's wheel and agent-switchboard install again, by hash"
    )
    for script in SUITE_SCRIPTS:
        path = python.parent / script
        checker.expect(
            path.is_file(), f"{script} is beside the copy's interpreter, as the suite needs"
        )
    probe = (
        "import keyring, lucille_host, switchboard\n"
        "for module in (keyring, lucille_host, switchboard):\n"
        "    print(module.__file__)\n"
    )
    done = subprocess.run(
        [str(python), "-I", "-B", "-c", probe], capture_output=True, text=True, check=False
    )
    found = done.stdout.splitlines()
    checker.expect(
        done.returncode == 0
        and len(found) == 3
        and all(line.startswith(str(copy)) for line in found),
        "the copy imports the host and its dependencies from itself",
        _shown(done),
    )
    if checker.failures:
        return
    clean = {
        key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "UV_"))
    }
    done = subprocess.run(
        [str(python), "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=build_app.REPO_ROOT,
        env=clean,
        check=False,
    )
    checker.expect(
        done.returncode == 0, "the host's whole test suite passes on the copy of the runtime"
    )


def _running(name: str) -> list[str]:
    done = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, check=False)
    return done.stdout.split()


def _wait_until(test: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test():
            return True
        time.sleep(0.5)
    return test()


def check_launch(app: Path, checker: Checker) -> None:
    name = build_app.load_pins().app_name
    if _running(name):
        subprocess.run(["pkill", "-x", name], check=False)
        _wait_until(lambda: not _running(name), 10)
    done = subprocess.run(["open", str(app)], capture_output=True, text=True, check=False)
    checker.expect(done.returncode == 0, "open starts the app", _shown(done))
    started = _wait_until(lambda: bool(_running(name)), 15)
    checker.expect(started, f"a process named {name!r} is running")
    time.sleep(10)
    pids = _running(name)
    alive = checker.expect(bool(pids), f"it is still running 10 seconds later (pid {pids})")
    if alive:
        subprocess.run(["pkill", "-TERM", "-x", name], check=False)
        checker.expect(_wait_until(lambda: not _running(name), 15), "it stops on SIGTERM")
    else:
        reports = Path.home() / "Library" / "Logs" / "DiagnosticReports"
        for report in sorted(reports.glob(f"{name}*")) if reports.is_dir() else []:
            print(f"--- {report}")
            print(report.read_text(encoding="utf-8", errors="replace")[:20000])
        logs = subprocess.run(
            [
                "log",
                "show",
                "--last",
                "2m",
                "--style",
                "compact",
                "--predicate",
                f'process == "{name}"',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        print(logs.stdout[-20000:])


# The menu bar app's status item, and its menu, as System Events names them.
STATUS_ITEM: Final = "menu bar item 1 of last menu bar"
MENU: Final = f"menu 1 of {STATUS_ITEM}"
LINES: Final = "set AppleScript's text item delimiters to linefeed\n"
# The items the menu shows whatever the host's state, besides its first line.
MENU_ITEMS: Final = (
    "Abilities",
    "Add a Folder…",
    "Folders",
    "Connect to Lucille (coming soon)",
    f"Quit {APP_NAME}",
)
# The menu's status line. The app cannot pair yet, so it always says the Mac is not connected.
NOT_CONNECTED: Final = "Not connected to Lucille"


def _system_events(body: str, timeout: float = 30) -> tuple[bool, str]:
    """Run AppleScript against the app's process through System Events: (worked, what it said)."""
    name = build_app.load_pins().app_name
    script = f'tell application "System Events"\ntell process "{name}"\n{body}\nend tell\nend tell'
    try:
        done = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"osascript did not finish within {timeout:.0f} s"
    return done.returncode == 0, (done.stdout if done.returncode == 0 else done.stderr).strip()


def _screenshot(path: Path) -> None:
    done = subprocess.run(
        ["screencapture", "-x", str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    print(f"screenshot {path.name}: " + ("saved" if path.is_file() else _shown(done)))


def _open_menu_and_read() -> tuple[bool, list[str]]:
    worked, text = _system_events(
        f"click {STATUS_ITEM}\ndelay 1\n{LINES}return (name of every menu item of {MENU}) as text"
    )
    return worked, text.splitlines() if worked else [text]


def _can_drive() -> bool:
    """Whether System Events can read this app's menu bar here."""
    worked, items = _system_events(
        f"{LINES}return (description of every menu bar item of last menu bar) as text"
    )
    if not worked:
        print(
            "note: System Events cannot read this app's menu bar on this machine, so the app "
            f"was not driven: {items}"
        )
        return False
    print(f"the app's status items: {items.splitlines()}")
    return True


def _status_item() -> dict[str, str]:
    """What System Events reports of the menu bar item: its title and description, which
    VoiceOver reads as its name, its name and help (the tooltip), and its width."""
    script = (
        f"set found to {{}}\nset theItem to {STATUS_ITEM}\n"
        + "".join(
            f'try\nset end of found to "{key}=" & ({key} of theItem)\nend try\n'
            for key in ("title", "description", "name", "help")
        )
        # Not "item 1 of": inside System Events, "item" names one of its own classes.
        + "try\nset {theWidth, theHeight} to size of theItem\n"
        + 'set end of found to "width=" & (theWidth as text)\nend try\n'
        + f"{LINES}return found as text"
    )
    worked, said = _system_events(script)
    print(f"the menu bar item says: {said.splitlines() if worked else said}")
    if not worked:
        return {}
    return dict(line.partition("=")[::2] for line in said.splitlines())


def _width(item: dict[str, str]) -> float:
    """The menu bar item's width in points, or 0 when System Events did not give one."""
    try:
        return float(item.get("width", "").replace(",", "."))
    except ValueError:
        return 0.0


def _named(item: dict[str, str], name: str) -> bool:
    """Whether VoiceOver reads ``name`` for the menu bar item: its title or its description. Its
    help, the tooltip, does not count."""
    return name in (item.get("title"), item.get("description"))


def _welcome(checker: Checker, shot: Path, when: str) -> None:
    """Read the note that says what the app is for and where its menu is, and choose OK."""
    texts: list[str] = []

    def shown() -> bool:
        nonlocal texts
        worked, texts = _window_list("value of every static text")
        return worked and WELCOME_TITLE in texts

    found = _wait_until(shown, 20)
    _screenshot(shot)
    checker.expect(
        found and any("by the clock" in text for text in texts),
        f"{when}, the app says where its menu is",
        f"{texts}",
    )
    checker.expect(
        found and any(PURPOSE in text for text in texts),
        f"{when}, the app says what it is for",
        f"{texts}",
    )
    worked, said = _system_events('click button "OK" of window 1')
    checker.expect(
        worked
        and _wait_until(
            lambda: WELCOME_TITLE not in _window_list("value of every static text")[1], 10
        ),
        "OK closes the note",
        said,
    )


def _drive_welcome(checker: Checker, screens: Path, app: Path) -> None:
    """The app has no window and no Dock icon, so when it opens, and when it is opened again
    while it runs, it says where its menu is."""
    _welcome(checker, screens / "1-welcome.png", "when it opens")
    done = subprocess.run(["open", str(app)], capture_output=True, text=True, check=False)
    checker.expect(done.returncode == 0, "open opens the running app again", _shown(done))
    _welcome(checker, screens / "1-welcome-again.png", "when it is opened again")


def _drive_menu(checker: Checker, screens: Path, flag: Path) -> bool:
    """Read the menu bar item's name and the menu, then choose Pause Lucille and Resume Lucille.
    False if System Events cannot open the menu on this machine."""
    version = build_app.host_version()
    item = _status_item()
    checker.expect(
        _named(item, STATUS_ITEM_NAMES[0]),
        f"the menu bar item's title or description is {STATUS_ITEM_NAMES[0]!r}",
        f"{item}",
    )
    checker.expect(
        _width(item) >= 16,
        "the menu bar item is wide enough to show its icon",
        f"{item}",
    )
    worked, shown = _open_menu_and_read()
    if not worked:
        print(f"note: System Events cannot open the menu on this machine: {shown}")
        return False
    print(f"the menu shows: {shown}")
    _screenshot(screens / "2-menu-open.png")
    worked, abilities = _system_events(
        f'{LINES}return (name of every menu item of menu 1 of menu item "Abilities" of {MENU}) '
        "as text"
    )
    listed = abilities.splitlines() if worked else []
    print(f"the Abilities submenu shows: {listed if worked else abilities}")
    _system_events("key code 53")
    checker.expect(
        shown[:1] == [f"{APP_NAME} {version}"],
        "the menu's first line names the version",
        f"{shown}",
    )
    checker.expect(
        NOT_CONNECTED in shown and "Pause Lucille" in shown,
        f"the menu says {NOT_CONNECTED!r}, and offers Pause Lucille",
        f"{shown}",
    )
    missing = [item for item in MENU_ITEMS if item not in shown]
    checker.expect(not missing, "the menu holds every item", f"missing: {missing}")
    checker.expect(
        worked
        and set(ABILITY_TITLES.values()) <= set(listed)
        and not {"host.sleep", "host.describe", *ABILITY_TITLES} & set(listed),
        "Abilities lists each ability in plain words, and not host.sleep or host.describe",
        abilities,
    )
    checker.expect(
        worked and set(ABILITY_DETAILS.values()) <= set(listed),
        "under each ability, a sentence says what Lucille gets",
        abilities,
    )
    if flag.exists():
        print("note: the host was paused before this check, so Pause and Resume were not tried")
        return True

    worked, said = _system_events(
        f'click {STATUS_ITEM}\ndelay 1\nclick menu item "Pause Lucille" of {MENU}'
    )
    checker.expect(
        worked and _wait_until(flag.exists, 15),
        "choosing Pause Lucille runs `pause`: the pause flag appears",
        said,
    )
    time.sleep(3)
    worked, shown = _open_menu_and_read()
    _screenshot(screens / "3-paused.png")
    _system_events("key code 53")
    checker.expect(
        worked
        and f"{NOT_CONNECTED} \N{MIDDLE DOT} Paused" in shown
        and PAUSED_NOTE in shown
        and "Resume Lucille" in shown
        and "Pause Lucille" not in shown,
        "the menu then says it is paused, and offers Resume Lucille",
        f"{shown}",
    )
    item = _status_item()
    checker.expect(
        _named(item, STATUS_ITEM_NAMES[1]),
        f"the menu bar item's title or description is then {STATUS_ITEM_NAMES[1]!r}",
        f"{item}",
    )
    worked, said = _system_events(
        f'click {STATUS_ITEM}\ndelay 1\nclick menu item "Resume Lucille" of {MENU}'
    )
    checker.expect(
        worked and _wait_until(lambda: not flag.exists(), 15),
        "choosing Resume Lucille runs `resume`: the pause flag goes",
        said,
    )
    return True


def _op_state(python: Path, op: str) -> str:
    """The state ``ops list`` gives one operation, for the real HOME."""
    for line in _host(python, Path.home(), ["ops", "list"]).stdout.splitlines():
        name, _, state = line.partition("  ")
        if name == op:
            return state
    return ""


def _roots(python: Path) -> dict[str, str]:
    """``roots list`` for the real HOME, as {alias: path}."""
    listed = _host(python, Path.home(), ["roots", "list"]).stdout.splitlines()
    return dict(line.split("  ", 1) for line in listed if "  " in line)


def _drive_abilities(checker: Checker, python: Path, screens: Path) -> None:
    """Switch an ability on and off again from the Abilities submenu, by its plain words."""
    off = [op for op in ABILITY_TITLES if _op_state(python, op) == "disabled"]
    if not off:
        print("note: no listed ability is plainly off here, so the Abilities submenu was not used")
        return
    op = off[0]
    title = ABILITY_TITLES[op]
    submenu = f'menu 1 of menu item "Abilities" of {MENU}'
    open_submenu = f'click {STATUS_ITEM}\ndelay 1\nclick menu item "Abilities" of {MENU}\ndelay 1\n'
    try:
        worked, said = _system_events(f'{open_submenu}click menu item "{title}" of {submenu}')
        checker.expect(
            worked and _wait_until(lambda: _op_state(python, op) == "enabled", 15),
            f"choosing {title!r} under Abilities runs `ops enable {op}`",
            said,
        )
        time.sleep(2)
        worked, mark = _system_events(
            f'{open_submenu}return value of attribute "AXMenuItemMarkChar" of menu item '
            f'"{title}" of {submenu}'
        )
        _screenshot(screens / "4-ability-on.png")
        _system_events("key code 53\ndelay 0.3\nkey code 53")
        checker.expect(
            worked and mark not in ("", "missing value"), f"{title!r} then has a checkmark", mark
        )
        worked, said = _system_events(f'{open_submenu}click menu item "{title}" of {submenu}')
        checker.expect(
            worked and _wait_until(lambda: _op_state(python, op) == "disabled", 15),
            f"choosing {title!r} again runs `ops disable {op}`",
            said,
        )
    finally:
        if _op_state(python, op) == "enabled":
            _host(python, Path.home(), ["ops", "disable", op])


def _choose_folder(folder: Path, screens: Path, shot: str) -> tuple[bool, str]:
    """Choose Add a Folder…, go to ``folder`` in the Open panel and choose it. Whatever the app
    shows next is left open."""
    worked, said = _system_events(
        f"click {STATUS_ITEM}\ndelay 1\n"
        f'click (first menu item of {MENU} whose name starts with "Add a Folder")',
        timeout=60,
    )
    if not worked:
        return False, f"choosing Add a Folder: {said}"
    time.sleep(2)
    return _go_to_folder(folder, screens, shot)


def _go_to_folder(folder: Path, screens: Path, shot: str) -> tuple[bool, str]:
    """In the open Open panel, go to ``folder`` and choose it."""
    worked, said = _system_events(
        "set frontmost to true\n"
        'keystroke "g" using {command down, shift down}\n'
        "delay 1\n"
        f'keystroke "{folder}"\n'
        "delay 1\n"
        "key code 36\n"
        "delay 2",
        timeout=60,
    )
    if not worked:
        return False, f"going to {folder} in the Open panel: {said}"
    _screenshot(screens / shot)
    worked, said = _system_events(f'click button "{OPEN_PROMPT}" of window 1\ndelay 2', timeout=60)
    return worked, said if worked else f"choosing the folder: {said}"


def _window_list(what: str) -> tuple[bool, list[str]]:
    """``what`` of every element of one kind in the app's front window, such as ``name of every
    button``."""
    worked, said = _system_events(f"{LINES}return ({what} of window 1) as text")
    return worked, said.splitlines() if worked else [said]


def _name_field() -> tuple[bool, str]:
    return _system_events("return value of text field 1 of window 1")


def _acknowledge(checker: Checker, words: str, shot: Path) -> None:
    """The app shows a note holding ``words``; read it and choose OK."""
    time.sleep(2)
    worked, texts = _window_list("value of every static text")
    _screenshot(shot)
    checker.expect(
        worked and any(words in text for text in texts),
        f"the app then says {words!r}",
        f"{texts}",
    )
    _system_events('click button "OK" of window 1')


def _drive_folders(checker: Checker, python: Path, screens: Path) -> None:
    """Add a folder through the Open panel and the name dialog, correcting a name the host would
    refuse, then remove it from the Folders submenu. Then choose the home folder: the app asks
    first; Choose a Smaller Folder goes back to the Open panel, and Add Anyway adds it."""
    home = Path.home()
    folder = home / "lucille-host-ui-check" / "My Project Files"
    folder.mkdir(parents=True, exist_ok=True)
    added: list[str] = []
    try:
        worked, said = _choose_folder(folder, screens, "5-open-panel.png")
        if not checker.expect(
            worked, "Add a Folder… opens the Open panel, and a folder is chosen", said
        ):
            return
        worked, suggestion = _name_field()
        _screenshot(screens / "6-name-dialog.png")
        checker.expect(
            worked and suggestion == "my-project-files",
            "the name dialog then suggests my-project-files",
            suggestion,
        )
        # Delete what the field holds with Backspace, which needs no Edit menu, then type a name
        # the host would refuse.
        worked, said = _system_events(
            "set frontmost to true\n"
            "repeat 40 times\nkey code 51\nend repeat\n"
            'keystroke "My Project Files"\n'
            "delay 0.5\n"
            'click button "Add Folder" of window 1\n'
            "delay 2\n"
            "return value of text field 1 of window 1"
        )
        _screenshot(screens / "7-name-corrected.png")
        checker.expect(
            worked and said == "my-project-files",
            "a typed name the host would refuse comes back as one it accepts, not added",
            f"{said} {_roots(python)}",
        )
        checker.expect("my-project-files" not in _roots(python), "nothing is added before then")
        worked, said = _system_events('click button "Add Folder" of window 1')
        checker.expect(
            worked
            and _wait_until(lambda: _roots(python).get("my-project-files") == str(folder), 15),
            "Add Folder then runs `roots add my-project-files <the folder>`",
            f"{said} {_roots(python)}",
        )
        added.append("my-project-files")
        _acknowledge(
            checker, "Once this Mac is connected to Lucille", screens / "8-folder-added.png"
        )
        time.sleep(1)
        folders = f'menu 1 of menu item "Folders" of {MENU}'
        worked, said = _system_events(
            f'click {STATUS_ITEM}\ndelay 1\nclick menu item "Folders" of {MENU}\ndelay 1\n'
            f'click menu item "my-project-files" of {folders}\ndelay 1\n'
            f'click menu item "Remove" of menu 1 of menu item "my-project-files" of {folders}'
        )
        checker.expect(
            worked and _wait_until(lambda: "my-project-files" not in _roots(python), 15),
            "Folders, my-project-files, Remove runs `roots remove my-project-files`",
            f"{said} {_roots(python)}",
        )

        worked, said = _choose_folder(home, screens, "9-open-panel-home.png")
        if not checker.expect(worked, "the home folder can be chosen in the Open panel", said):
            return
        worked, buttons = _window_list("name of every button")
        _screenshot(screens / "10-confirm-home.png")
        checker.expect(
            worked and {"Choose a Smaller Folder", "Add Anyway"} <= set(buttons),
            "the app asks about the home folder before naming it",
            f"{buttons}",
        )
        worked, texts = _window_list("value of every static text")
        checker.expect(
            worked and any(text.startswith("This is your home folder.") for text in texts),
            "it says that this is the home folder, and what it holds",
            f"{texts}",
        )
        worked, said = _system_events('click button "Choose a Smaller Folder" of window 1\ndelay 2')
        worked, buttons = _window_list("name of every button")
        checker.expect(
            worked and OPEN_PROMPT in buttons and "Add Anyway" not in buttons,
            "Choose a Smaller Folder opens the Open panel again",
            f"{said} {buttons}",
        )
        _system_events("key code 53")
        time.sleep(2)
        checker.expect(
            str(home) not in _roots(python).values(), "cancelling the Open panel adds nothing"
        )

        worked, said = _choose_folder(home, screens, "11-open-panel-home.png")
        if not checker.expect(worked, "the home folder can be chosen again", said):
            return
        worked, said = _system_events('click button "Add Anyway" of window 1\ndelay 2')
        worked, alias = _name_field()
        checker.expect(
            worked and bool(alias) and str(home) not in _roots(python).values(),
            "Add Anyway goes on to the name dialog, and adds nothing yet",
            alias,
        )
        worked, said = _system_events('click button "Add Folder" of window 1')
        checker.expect(
            worked and _wait_until(lambda: _roots(python).get(alias) == str(home), 15),
            "Add Folder then runs `roots add` with --allow-home-or-root",
            f"{said} {_roots(python)}",
        )
        added.append(alias)
        _acknowledge(
            checker, "Once this Mac is connected to Lucille", screens / "12-home-added.png"
        )
    finally:
        for alias, path in _roots(python).items():
            if alias in added or path in (str(folder), str(home)):
                _host(python, home, ["roots", "remove", alias])
        shutil.rmtree(folder.parent, ignore_errors=True)


def _whole_disk_refused(checker: Checker, chosen: str, shot: Path) -> bool:
    """The app's front window is the note that refuses the whole disk: it has Choose a Smaller
    Folder and Cancel, and no button that adds the folder. That the note has those two buttons
    and no other is pinned in its source (``tests/test_macos_build.py``); a named button the
    window itself adds is printed, not failed. If the window is something else, such as the name
    dialog, say what it holds."""
    time.sleep(1)
    worked, buttons = _window_list("name of every button")
    _screenshot(shot)
    named = [button for button in buttons if button not in ("", "missing value")]
    print(f"the window after choosing {chosen} has the named buttons {named}")
    adding = [
        button
        for button in named
        if button not in WHOLE_DISK_BUTTONS and button.startswith(ADDING_BUTTONS)
    ]
    shown = worked and set(WHOLE_DISK_BUTTONS) <= set(named) and not adding
    checker.expect(
        shown,
        f"choosing {chosen} brings up a note with Choose a Smaller Folder and Cancel, and no "
        "button that adds it",
        f"{buttons}" if shown else f"{buttons}; a text field there holds {_name_field()[1]!r}",
    )
    worked, texts = _window_list("value of every static text")
    checker.expect(
        worked
        and WHOLE_DISK_TITLE in texts
        and any(text.startswith(WHOLE_DISK_TEXT) for text in texts),
        f"it says {WHOLE_DISK_TITLE!r}, and what the disk holds",
        f"{texts}",
    )
    return shown


def _drive_whole_disk(checker: Checker, python: Path, screens: Path) -> None:
    """Choose the Data volume, then ``/`` through a link to it: each time the app says it never
    adds the whole disk, with Choose a Smaller Folder, which goes back to the Open panel, and
    Cancel. Nothing is added."""
    before = _roots(python)
    data, disk = WHOLE_DISK_FOLDERS
    link = Path.home() / "lucille-host-ui-check-disk" / "Startup Disk"
    refused = {str(data), str(disk), str(link)}
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(disk, target_is_directory=True)
        worked, said = _choose_folder(data, screens, "13-open-panel-data.png")
        if not checker.expect(worked, f"{data} can be chosen in the Open panel", said):
            return
        if not _whole_disk_refused(checker, str(data), screens / "14-refuse-data.png"):
            return
        worked, said = _system_events('click button "Choose a Smaller Folder" of window 1\ndelay 2')
        worked, buttons = _window_list("name of every button")
        checker.expect(
            worked and OPEN_PROMPT in buttons and "Add Anyway" not in buttons,
            "Choose a Smaller Folder opens the Open panel again",
            f"{said} {buttons}",
        )
        worked, said = _go_to_folder(link, screens, "15-open-panel-disk.png")
        through = f"{disk}, through the link {link}"
        if not checker.expect(worked, f"{through}, can be chosen in the Open panel", said):
            return
        if not _whole_disk_refused(checker, through, screens / "16-refuse-disk.png"):
            return
        worked, said = _system_events('click button "Cancel" of window 1\ndelay 2')
        still, buttons = _window_list("name of every button")
        checker.expect(
            worked
            and not (still and {OPEN_PROMPT, "Add Folder", *WHOLE_DISK_BUTTONS} & set(buttons)),
            "Cancel closes the note, and opens nothing after it",
            f"{said} {buttons}",
        )
        after = _roots(python)
        checker.expect(
            after == before and not refused & set(after.values()),
            "neither is added",
            f"{after}",
        )
    finally:
        # Close whatever the app still shows, and take out anything of the whole disk it added.
        for _ in range(2):
            _system_events('click button "Cancel" of window 1\ndelay 1')
        for alias, path in _roots(python).items():
            if path in refused:
                _host(python, Path.home(), ["roots", "remove", alias])
        # The link alone, never what it leads to.
        if link.is_symlink():
            link.unlink()
        try:
            link.parent.rmdir()
        except OSError:
            pass


def check_ui(app: Path, checker: Checker, work: Path, require_driven: bool = False) -> None:
    """Open the app and drive it the way a user would, through System Events: read the note that
    says what it is for and where its menu is, when it opens and when it is opened again; read the
    menu; choose Pause Lucille and Resume Lucille, and watch the host's pause flag; switch an
    ability on and off; add a folder through the Open panel and the name dialog, and remove it from
    the Folders submenu; add the home folder, which the app asks about first; choose the Data
    volume, and ``/`` through a link to it, which the app refuses. Each step is checked against
    the host's own state. It uses the real HOME, and leaves the host as it found it. Where macOS
    does not let System Events drive other apps (no accessibility permission), it says so and
    drives nothing; with ``require_driven`` that is a failure. Screenshots go to
    ``<out>/check/screens``."""
    name = build_app.load_pins().app_name
    screens = work / "screens"
    screens.mkdir(parents=True, exist_ok=True)
    flag = Path.home() / "Library" / "Application Support" / "lucille-host" / "default"
    flag = flag / "state" / "paused"
    if _running(name):
        subprocess.run(["pkill", "-x", name], check=False)
        _wait_until(lambda: not _running(name), 10)
    out_log = work / "app-stdout.txt"
    err_log = work / "app-stderr.txt"
    for log in (out_log, err_log):
        log.write_text("", encoding="utf-8")
    done = subprocess.run(
        ["open", "--stdout", str(out_log), "--stderr", str(err_log), str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    checker.expect(done.returncode == 0, "open starts the app", _shown(done))
    if not checker.expect(_wait_until(lambda: bool(_running(name)), 15), "the app is running"):
        return
    time.sleep(5)
    try:
        driven = _can_drive()
        if driven:
            _drive_welcome(checker, screens, app)
            _screenshot(screens / "1-menu-bar.png")
            driven = _drive_menu(checker, screens, flag)
        if driven:
            _drive_abilities(checker, python_in(app), screens)
            _drive_folders(checker, python_in(app), screens)
            _drive_whole_disk(checker, python_in(app), screens)
        elif require_driven:
            checker.expect(False, "System Events drove the menu, as --require-driven asks")
    finally:
        subprocess.run(["pkill", "-TERM", "-x", name], check=False)
        checker.expect(_wait_until(lambda: not _running(name), 15), "the app stops on SIGTERM")
        for log in (out_log, err_log):
            text = log.read_text(encoding="utf-8", errors="replace")
            print(f"the app's {log.stem.removeprefix('app-')}: {text[-5000:]!r}")


def check_gatekeeper(app: Path, dmgs: Sequence[Path]) -> None:
    assessments = [["spctl", "--assess", "--type", "execute", "--verbose=4", str(app)]]
    for dmg in dmgs:
        assessments.append(
            [
                "spctl",
                "--assess",
                "--type",
                "open",
                "--context",
                "context:primary-signature",
                "--verbose=4",
                str(dmg),
            ]
        )
    for argv in assessments:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        print(f"{' '.join(argv[:4])} ...: exit {done.returncode}")
        print((done.stdout + done.stderr).strip())
    print("An ad hoc signature is expected to be refused here until notarization exists.")


def check_dmg(dmgs: Sequence[Path], checker: Checker) -> None:
    if not checker.expect(
        len(dmgs) == 1, "exactly one disk image was built", f"{[dmg.name for dmg in dmgs]}"
    ):
        return
    dmg = dmgs[0]
    checker.expect(
        dmg.name.endswith(build_app.DMG_SUFFIX),
        f"its name, {dmg.name}, says that it is signed ad hoc",
    )
    print(f"{dmg.name}: {dmg.stat().st_size} bytes, sha256 {build_app.sha256_of(dmg)}")
    done = build_app.hdiutil(["verify", str(dmg)])
    checker.expect(done.returncode == 0, "hdiutil verify passes", _shown(done))
    name = build_app.load_pins().app_name
    with tempfile.TemporaryDirectory(prefix="lucille-host-dmg-") as tmp:
        mount = Path(tmp) / "mount"
        mount.mkdir()
        done = build_app.hdiutil(
            ["attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount), str(dmg)]
        )
        if not checker.expect(done.returncode == 0, "the image mounts", _shown(done)):
            return
        try:
            entries = sorted(entry.name for entry in mount.iterdir())
            print(f"the image holds: {entries}")
            app = mount / f"{name}.app"
            link = mount / "Applications"
            checker.expect(app.is_dir(), f"it holds {name}.app")
            checker.expect(
                link.is_symlink() and os.readlink(link) == "/Applications",
                "it holds a link to /Applications",
            )
            done = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(app)],
                capture_output=True,
                text=True,
                check=False,
            )
            checker.expect(
                done.returncode == 0, "the app in the image has a valid signature", _shown(done)
            )
            manifest = app / build_app.MANIFEST_PATH
            if manifest.is_file():
                print(json.dumps(json.loads(manifest.read_text(encoding="utf-8")), indent=2))
        finally:
            for extra in ([], ["-force"]):
                if build_app.hdiutil(["detach", *extra, str(mount)]).returncode == 0:
                    break


CHECKS: Final = (
    "bundle",
    "record",
    "version",
    "host",
    "wrapper",
    "environment",
    "readonly",
    "swift",
    "relocate",
    "links",
    "signature",
    "library-snapshot",
    "launch",
    "ui",
    "library-diff",
    "gatekeeper",
    "dmg",
    "startup",
    "suite",
    "selftest",
    "handoff",
    "handoff-check",
    "unpack-dmg",
)
# The checks that run with no app at ``<out>``: the disk image's, those that put the app there,
# and the first ~/Library snapshot.
NEEDS_NO_APP: Final = frozenset({"dmg", "unpack-dmg", "handoff-check", "library-snapshot"})
# The options each check cannot run without, by their argparse names.
REQUIRED: Final = {
    "selftest": ("to",),
    "library-snapshot": ("to",),
    "library-diff": ("before",),
    "record": ("host_wheel_sha256",),
    "suite": ("copy_to",),
    "handoff": ("run_id", "run_attempt", "github_output"),
    "handoff-check": ("dmg_sha256", "selftest_sha256"),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_app.py", description="Check a built app.")
    parser.add_argument("check", choices=CHECKS)
    parser.add_argument(
        "--out", type=Path, default=build_app.DEFAULT_OUT, help="the folder build_app.py wrote to"
    )
    parser.add_argument(
        "--require-driven",
        action="store_true",
        help="ui: fail if System Events cannot drive the app's menu (macos-app passes this)",
    )
    parser.add_argument(
        "--selftest", type=Path, help="swift, relocate: run this compiled self-test"
    )
    parser.add_argument("--to", type=Path, help="selftest, library-snapshot: the file to write")
    parser.add_argument("--before", type=Path, help="library-diff: what library-snapshot wrote")
    parser.add_argument(
        "--control-run",
        action="store_true",
        help="library-diff: have the host write its state folder first (pause, then resume)",
    )
    parser.add_argument("--copy-to", type=Path, help="suite: the folder for the runtime's copy")
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        help="handoff-check, unpack-dmg: the hand-off folder (default OUT/handoff)",
    )
    parser.add_argument("--run-id", help="handoff: the workflow run's id")
    parser.add_argument("--run-attempt", help="handoff: the workflow run's attempt")
    parser.add_argument("--github-output", type=Path, help="handoff: append the outputs here")
    parser.add_argument("--dmg-sha256", help="handoff-check: the disk image's sha256")
    parser.add_argument("--selftest-sha256", help="handoff-check: the self-test's sha256")
    parser.add_argument(
        "--summary", type=Path, help="record, startup: also append Markdown to this file"
    )
    parser.add_argument(
        "--host-wheel-sha256", help="record: the sha256 of the host wheel the wheel step built"
    )
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument("--tag", help="version: the build was made from this v* tag")
    versions.add_argument("--build-number", help="version: the build number it was given")
    return parser


def _run_check(args: argparse.Namespace, out: Path, checker: Checker) -> None:
    app = app_path(out)
    work = out / "check"
    source = (args.source or out / HANDOFF_DIR).resolve()
    check = args.check
    if check == "bundle":
        check_bundle(app, checker)
    elif check == "record":
        check_record(app, out, checker, args.summary, host_wheel_sha256=args.host_wheel_sha256)
    elif check == "version":
        check_version(app, checker, args.tag, args.build_number)
    elif check == "host":
        check_host(app, checker)
    elif check == "wrapper":
        check_wrapper(app, checker)
    elif check == "environment":
        check_environment(app, checker)
    elif check == "readonly":
        check_readonly(app, checker)
    elif check == "swift":
        check_swift(app, checker, work, args.selftest)
    elif check == "relocate":
        check_relocate(app, checker, work, args.selftest)
    elif check == "links":
        check_links(app, checker)
    elif check == "signature":
        check_signature(app, checker)
    elif check == "library-snapshot":
        check_library_snapshot(args.to.resolve(), checker)
    elif check == "launch":
        check_launch(app, checker)
    elif check == "ui":
        check_ui(app, checker, work, args.require_driven)
    elif check == "library-diff":
        check_library_diff(app, args.before.resolve(), args.control_run, checker)
    elif check == "gatekeeper":
        check_gatekeeper(app, build_app.disk_images(out, APP_NAME))
    elif check == "dmg":
        check_dmg(build_app.disk_images(out, APP_NAME), checker)
    elif check == "startup":
        check_startup(app, work, checker, args.summary)
    elif check == "suite":
        check_suite(app, out, args.copy_to.resolve(), checker)
    elif check == "selftest":
        to = args.to.resolve()
        checker.expect(compile_selftest(to), f"the Swift self-test compiles to {to}")
    elif check == "handoff":
        write_handoff(out, args.run_id, args.run_attempt, args.github_output, checker)
    elif check == "handoff-check":
        check_handoff(source, args.dmg_sha256, args.selftest_sha256, checker)
    else:
        unpack_dmg(out, source, checker)


def missing_options(args: argparse.Namespace) -> list[str]:
    """The options ``args.check`` cannot run without that ``args`` lacks, as flags."""
    missing = [
        "--" + name.replace("_", "-")
        for name in REQUIRED.get(args.check, ())
        if getattr(args, name) is None
    ]
    if args.check == "version" and args.tag is None and args.build_number is None:
        missing.append("--tag or --build-number")
    return missing


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if sys.platform != "darwin":
        print("check_app.py: these checks run on macOS", file=sys.stderr)
        return 2
    missing = missing_options(args)
    if missing:
        print(f"check_app.py: {args.check} needs {', '.join(missing)}", file=sys.stderr)
        return 2
    out = args.out.resolve()
    app = app_path(out)
    checker = Checker()
    if args.check not in NEEDS_NO_APP and not checker.expect(app.is_dir(), f"{app} exists"):
        return 1
    try:
        _run_check(args, out, checker)
    except (CheckError, build_app.BuildError, build_app.PinError, payload.AssemblyError) as exc:
        checker.expect(False, f"{args.check} cannot go on", str(exc))
    if checker.failures:
        print(f"{len(checker.failures)} failed: " + "; ".join(checker.failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
