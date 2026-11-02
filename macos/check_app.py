"""Checks on the built Mac app. CI runs each one on a macOS runner after build_app.py.

    uv run --no-project python macos/check_app.py CHECK [--out build/macos]

Each check prints what it saw and exits 1 if anything is wrong:

- ``host``: the embedded interpreter, in isolated mode, runs the host. ``--version`` prints this
  checkout's version; ``ops list`` works; a ``roots`` add, list and remove round trip works, and
  so does ``pause`` then ``resume``; a home folder is refused without ``--allow-home-or-root``;
  the dependencies' native code loads under the hardened runtime. HOME is a new empty folder, and
  PYTHONHOME and PYTHONPATH point nowhere, which isolated mode must ignore.
- ``swift``: compiles the app's Swift files that do not draw anything with a test driver
  (``macos/Tests/SelfTest.swift``), and runs it against the app. It makes the calls the menu
  makes, through the same code; reads the host's golden fixtures through the app's JSON reader
  and its pairing and supervision state machines; and drives the child runner with fake children,
  a shell script in place of the interpreter.
- ``relocate``: copies the app to another folder, whose path has a space in it, and runs ``host``
  and ``swift`` on the copy.
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
  --strict`` on each Mach-O file in it; every Mach-O file has the hardened runtime flag; the app
  and the interpreter carry no entitlements; the interpreter's signing identifier is the bundle
  identifier, a dot and ``python3``. It prints each Mach-O file's signing identifier.
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
  and adds neither. Then, with a code-shaped text on the pasteboard, it chooses Connect to
  Lucille…: the code window opens with its field empty, and the field stays empty once it has the
  focus, so the app read nothing from the pasteboard; a typed word gets the hint; Command-V, typed
  through System Events, pastes exactly what was copied; Connect hands it to the host, which
  refuses it as no code, and the app says so in its own words. It checks each step against the
  host's own state, on the app's own profile (:data:`APP_PROFILE`).
  It uses the real HOME and leaves the host as it found it. Where macOS does not let System Events
  drive other apps, it says so and drives nothing, which is a failure with ``--require-driven``
  (CI passes it). Screenshots go to ``check/screens``.
- ``pairing``: end to end, against a Switchboard hub it starts on a loopback port and a fake
  Lucille (``tests/_lucille.py``). It needs Switchboard's hub and the test harness, which it
  imports only when it runs: CI runs it in the environment the host suite's step made from the
  app's own interpreter. It pairs the app's profile through the command line, as a test harness,
  with ``--hub`` and ``--store-file`` (which the app never passes), and leaves the SAS pending.
  Then it drives the app: the pairing window, opened from the menu, shows the number Lucille's
  side shows, Return does nothing there, and They Match confirms; the app starts ``run``, which
  reads the room Lucille offers; a ``files.sha256`` request on a folder added through the command
  line is answered; ``run`` killed with SIGKILL is started again after the back-off; ``run``
  drains when the app itself is killed; the app's own Quit asks first, shows the finishing panel,
  and waits for ``run`` to drain; Stop now pauses the host, which the app never undoes on its own;
  Disconnect drains ``run``, then forgets the pair. The app's profile, if there was one, is set
  aside first and put back at the end. Screenshots go to ``check/screens``.
- ``gatekeeper``: prints what ``spctl`` says about the app and the disk image. It never fails: an
  ad hoc signature is refused until the app is signed with a Developer ID and notarized.
- ``dmg``: ``hdiutil verify`` passes, and the mounted image holds the app, with a valid signature,
  and a link to ``/Applications``.
- ``probe``: prints what the verbs the app is about to use do on this runner, as the app runs
  them. It gates nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

if __package__ in (None, ""):  # run as a script: make ``macos`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macos import build_app  # noqa: E402

TESTS_DIR: Final = build_app.MACOS_DIR / "Tests"
ALIAS_VECTORS: Final = TESTS_DIR / "alias-vectors.json"
# The app's Swift files that use Foundation only, so a test driver can run them without a screen.
LOGIC_SOURCES: Final = (
    "Alias.swift",
    "HostCLI.swift",
    "HostText.swift",
    "HostJSON.swift",
    "Pairing.swift",
    "Supervisor.swift",
    "ConnectionText.swift",
)
# The host's golden fixtures (spec/CLI.md section 11), which the self-test reads through the
# app's JSON reader and its pairing and supervision state machines.
CLI_FIXTURES: Final = build_app.REPO_ROOT.joinpath(
    "src", "lucille_host", "spec", "cli", "v1", "fixtures"
)
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
    # The app never reads the pasteboard: the user pastes into a field, through the field.
    "_OBJC_CLASS_$_NSPasteboard",
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
# name, and the Open panel's button. The app's name in them comes from macos/runtime.toml, which
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
# The profile the app runs every verb on (``HostCLI.appProfile``): its own folders, abilities,
# pause flag, Keychain item and run lock, apart from a command-line host's ``default``. The ui and
# pairing checks read and change the host's state on this profile, as the app does.
APP_PROFILE: Final = "mac-app"
# Loads the native code of the runtime dependencies, the way the host will.
NATIVE_PROBE: Final = """\
import sys
import _cffi_backend
import keyring.backends.macOS
import lucille_host._switchboard
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
X25519PrivateKey.generate().public_key()
assert sys.flags.isolated == 1, sys.flags
print("native code loaded from", sys.prefix)
"""


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
    return app / "Contents" / "Resources" / "python" / build_app.INTERPRETER


def _shown(done: subprocess.CompletedProcess[str]) -> str:
    return f"exit {done.returncode}\nstdout: {done.stdout!r}\nstderr: {done.stderr!r}"


def _host(python: Path, home: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the host's CLI as the app does, with a fixed environment."""
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(home),
        "PYTHONHOME": str(home / "no-such-python-home"),
        "PYTHONPATH": str(home / "no-such-python-path"),
    }
    return subprocess.run(
        [str(python), "-I", "-m", "lucille_host", *args],
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
            [str(python), "-I", "-B", "-c", NATIVE_PROBE],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(home)},
            cwd=home,
            timeout=120,
            check=False,
        )
        checker.expect(
            done.returncode == 0 and "native code loaded" in done.stdout,
            "cffi, cryptography, keyring's macOS backend and switchboard load",
            _shown(done),
        )


def check_swift(app: Path, checker: Checker, work: Path) -> None:
    pins = build_app.load_pins()
    exe = work / "selftest"
    work.mkdir(parents=True, exist_ok=True)
    sources = [str(build_app.SWIFT_DIR / name) for name in LOGIC_SOURCES]
    sources.append(str(TESTS_DIR / "SelfTest.swift"))
    built = subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-target",
            f"arm64-apple-macos{pins.minimum_macos}",
            "-o",
            str(exe),
            *sources,
        ],
        check=False,
    )
    if not checker.expect(built.returncode == 0, "the Swift self-test compiles"):
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
                "--fixtures",
                str(CLI_FIXTURES),
            ],
            # The pairing checks give status, pair status and run a minute each, in case macOS's
            # Keychain is slow to answer under the temporary HOME.
            timeout=600,
            check=False,
        )
    checker.expect(done.returncode == 0, "the Swift self-test passes")


def check_relocate(app: Path, checker: Checker, work: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="lucille-host-moved-") as tmp:
        moved = Path(tmp) / "Some Other Folder" / "Moved Copy.app"
        moved.parent.mkdir()
        subprocess.run(["ditto", str(app), str(moved)], check=True)
        print(f"copied the app to {moved}")
        check_host(moved, checker)
        check_swift(moved, checker, work)


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
    minimum = build_app.load_pins().minimum_macos
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
    main = app / "Contents" / "MacOS" / pins.app_name
    # sign_app signs with --prefix "<bundle id>.", which codesign puts before a derived identifier
    # that has no dot in it, such as the interpreter's file name.
    interpreter_id = f"{pins.bundle_id}.{Path(build_app.INTERPRETER).name}"
    for path in build_app.macho_files(app):
        if path == main:
            continue
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
    for path in [app, *build_app.macho_files(app)]:
        if path == main:
            continue
        done = _codesign_display(path, "--verbose=2")
        flags = [line for line in done.stderr.splitlines() if "flags=" in line]
        named = [line for line in done.stderr.splitlines() if line.startswith("Identifier=")]
        print(f"{path.relative_to(app.parent)}: {named[0] if named else 'no Identifier line'}")
        if path == python_in(app):
            checker.expect(
                named == [f"Identifier={interpreter_id}"],
                f"the interpreter's signing identifier is {interpreter_id}",
                _shown(done),
            )
        checker.expect(
            done.returncode == 0 and bool(flags) and "runtime" in flags[0],
            f"{path.relative_to(app.parent)} is signed with the hardened runtime",
            _shown(done),
        )
    for path in (app, python_in(app)):
        done = _codesign_display(path, "--entitlements", "-", "--xml")
        checker.expect(
            done.returncode == 0 and not done.stdout.strip(),
            f"{path.relative_to(app.parent)} has no entitlements",
            _shown(done),
        )
    print(_codesign_display(app, "--verbose=4").stderr, end="")


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


def _stop_earlier_copies(checker: Checker, name: str) -> bool:
    """Stop a copy of the app that is still running, with SIGTERM. One that does not stop fails the
    check, and is then killed: ``open`` would otherwise hand the next step that old copy, whose
    output goes nowhere the check reads."""
    if not _running(name):
        return True
    subprocess.run(["pkill", "-TERM", "-x", name], check=False)
    stopped = _wait_until(lambda: not _running(name), 15)
    checker.expect(stopped, "a copy of the app still running from before stops on SIGTERM")
    if not stopped:
        subprocess.run(["pkill", "-KILL", "-x", name], check=False)
        _wait_until(lambda: not _running(name), 10)
    return stopped


def check_launch(app: Path, checker: Checker) -> None:
    name = build_app.load_pins().app_name
    _stop_earlier_copies(checker, name)
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
# The menu's item that opens the code window, while the app's profile holds no pair.
CONNECT_ITEM: Final = "Connect to Lucille…"
# The items the menu shows while the app's profile holds no pair, besides its first line.
MENU_ITEMS: Final = (
    "Abilities",
    "Add a Folder…",
    "Folders",
    CONNECT_ITEM,
    f"Quit {APP_NAME}",
)
# The menu's status line while the app's profile holds no pair.
NOT_CONNECTED: Final = "Not connected to Lucille"
# The app's words about connecting, as in App/ConnectionText.swift (tests/test_macos_build.py
# checks that they agree). The menu's other items and status lines.
FINISH_ITEM: Final = "Finish Connecting…"
DISCONNECT_ITEM: Final = "Disconnect from Lucille…"
CONNECTING: Final = "Connecting to Lucille…"
CONNECTED: Final = "Connected to Lucille"
# The code window: its title, the three sentences it says, the hint under a typed word that is no
# code, and its button. Then what the app says when the host refuses a text as no code (bad_code).
CODE_TITLE: Final = "Connect to Lucille"
CODE_WORDS: Final = (
    "In Lucille, open Computers and add this computer. Copy the code Lucille shows, then paste it "
    "below.",
    "To paste, press Command-V, or Control-click the box and choose Paste.",
    "Paste only a code you copied from Lucille's page yourself. Never paste one someone sent you.",
)
CODE_HINT: Final = "That doesn't look like a code from Lucille. Copy it again from Lucille's page."
CONNECT_BUTTON: Final = "Connect"
BAD_CODE: Final = "That isn't a code from Lucille. Copy it again from Lucille's page."
# A code-shaped text the ui check leaves on the pasteboard when the code window opens, which the
# app must not read; and the text it pastes, which passes the app's own check on a code (the
# prefix, then 100 to 8192 characters of base64url) and which the host refuses as no code.
PASTEBOARD_SENTINEL: Final = "swbp1_" + "S" * 120
PASTED_TEXT: Final = "swbp1_" + "A" * 120
# The compare page, and the note after They Match.
COMPARE_TITLE: Final = "Compare the numbers"
COMPARE_QUESTION: Final = "Does Lucille's page show this number?"
THEY_MATCH: Final = "They Match"
CONFIRMED_TITLE: Final = "Confirmed on this Mac"
# Quit while ``run`` runs: the question, its button, the finishing panel and its button, and the
# panel's words once Stop now is chosen.
QUIT_ITEM: Final = f"Quit {APP_NAME}"
QUIT_QUESTION: Final = f"Quit {APP_NAME}?"
QUIT_TEXT: Final = f"Your Mac will stop answering Lucille until you open {APP_NAME} again."
QUIT_BUTTON: Final = "Quit"
FINISHING: Final = "Finishing the current task…"
STOP_NOW: Final = f"Stop now ({APP_NAME} will be paused when you next open it)"
STOPPING_NOW: Final = "Stopping…"
# The note at launch while the host is paused, and its button that resumes.
PAUSED_TITLE: Final = f"{APP_NAME} is paused"
RESUME_BUTTON: Final = "Resume"
# Disconnect: the question, its button, and the note once Lucille was told.
DISCONNECT_TITLE: Final = "Disconnect this Mac from Lucille?"
DISCONNECT_BUTTON: Final = "Disconnect"
DISCONNECTED_TOLD: Final = "Disconnected. Lucille was told."
# The line the app writes on standard error when ``run`` exits after a drain its standard input's
# end began (``RunSupervisor.describe``), and the start of the one for a ``run`` a signal ended.
DRAINED_LINE: Final = "run exited 0 (drained, stdin_end)"
SIGNALED_LINE: Final = "run ended by signal"


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
    """The state ``ops list`` gives one operation, for the real HOME, on the app's profile."""
    for line in _app_host(python, ["ops", "list"]).stdout.splitlines():
        name, _, state = line.partition("  ")
        if name == op:
            return state
    return ""


def _roots(python: Path) -> dict[str, str]:
    """``roots list`` for the real HOME, on the app's profile, as {alias: path}."""
    listed = _app_host(python, ["roots", "list"]).stdout.splitlines()
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
            _app_host(python, ["ops", "disable", op])


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


def _quoted(text: str) -> str:
    """``text`` as an AppleScript string. The app's words hold no quote and no backslash."""
    if '"' in text or "\\" in text:
        raise ValueError(f"{text!r} cannot be quoted for AppleScript here")
    return f'"{text}"'


def _target(kind: str, name: str | None = None) -> str:
    """AppleScript that sets ``theTarget`` to the first UI element of the class ``kind``
    (``button``, ``text field``), with the name ``name`` if one is given, in any window of the app
    and at any depth, or stops with an error. The app's own windows may hold their contents in
    groups, where ``of window 1`` does not reach."""
    named = "" if name is None else f" and name of theElement is {_quoted(name)}"
    said = kind if name is None else f"{kind} named {name}"
    return (
        "set theTarget to missing value\n"
        "repeat with theWindow in windows\n"
        "try\n"
        "repeat with theElement in (entire contents of theWindow)\n"
        "try\n"
        f"if class of theElement is {kind}{named} then\n"
        "set theTarget to contents of theElement\n"
        "exit repeat\n"
        "end if\n"
        "end try\n"
        "end repeat\n"
        "end try\n"
        "if theTarget is not missing value then exit repeat\n"
        "end repeat\n"
        f"if theTarget is missing value then error {_quoted(f'no {said} on the screen')}\n"
    )


def _every(kind: str, what: str) -> list[str]:
    """``what`` of every UI element of the class ``kind`` in every window of the app, at any depth,
    one per line (``value`` of every ``static text``, say); empty when System Events cannot say."""
    worked, said = _system_events(
        "set found to {}\n"
        "repeat with theWindow in windows\n"
        "try\n"
        "repeat with theElement in (entire contents of theWindow)\n"
        "try\n"
        f"if class of theElement is {kind} then "
        f"set end of found to (({what} of theElement) as text)\n"
        "end try\n"
        "end repeat\n"
        "end try\n"
        "end repeat\n"
        f"{LINES}return found as text"
    )
    if not worked:
        print(f"note: reading every {kind}: {said}")
        return []
    return said.splitlines()


def _texts() -> list[str]:
    """Every text the app's windows show, its notes included."""
    return _every("static text", "value")


def _says(words: str) -> bool:
    return any(words in text for text in _texts())


def _press(name: str) -> tuple[bool, str]:
    """Click the button named ``name``, in whichever window of the app holds it."""
    return _system_events(_target("button", name) + 'click theTarget\nreturn "pressed"')


def _enabled(name: str) -> str:
    """Whether the button named ``name`` is on: ``true``, ``false``, or ``absent``."""
    worked, said = _system_events(
        _target("button", name) + "return ((enabled of theTarget) as text)"
    )
    return said if worked else "absent"


def _field() -> tuple[bool, str]:
    """What the first text field in the app's windows holds."""
    return _system_events(_target("text field") + "return ((value of theTarget) as text)")


def _window_names() -> list[str]:
    worked, said = _system_events(f"{LINES}return (name of every window) as text")
    return said.splitlines() if worked else []


def _choose(start: str) -> tuple[bool, str]:
    """Open the menu and choose the item whose name starts with ``start``, which spares the
    script the ellipsis some items end with."""
    return _system_events(
        f"click {STATUS_ITEM}\ndelay 1\n"
        f"click (first menu item of {MENU} whose name starts with {_quoted(start)})",
        timeout=60,
    )


def _menu_until(test: Callable[[list[str]], bool], seconds: float) -> tuple[bool, list[str]]:
    """Open the menu, read it and close it again, until ``test`` holds of what it shows, for at
    most ``seconds``. The menu shows what the app knew when it opened, and opening it starts a
    refresh, so one read can be a refresh behind."""
    deadline = time.monotonic() + seconds
    while True:
        worked, shown = _open_menu_and_read()
        _system_events("key code 53")
        if worked and test(shown):
            return True, shown
        if time.monotonic() >= deadline:
            return False, shown
        time.sleep(3)


def _menu_shot(path: Path) -> None:
    """A screenshot with the menu open."""
    _system_events(f"click {STATUS_ITEM}\ndelay 1")
    _screenshot(path)
    _system_events("key code 53")


def _set_pasteboard(text: str) -> None:
    """Put ``text`` on the general pasteboard, as a user copying it would; empty clears it."""
    done = subprocess.run(
        ["pbcopy"], input=text, capture_output=True, text=True, timeout=30, check=False
    )
    if done.returncode != 0:
        print(f"note: pbcopy: {_shown(done)}")


def _profile_folder() -> Path:
    """Where the host keeps the app's profile, for the real HOME (src/lucille_host/paths.py)."""
    return Path.home() / "Library" / "Application Support" / "lucille-host" / APP_PROFILE


def _app_host(python: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """The host's command line for the real HOME, on the app's own profile."""
    return _host(python, Path.home(), ["--profile", APP_PROFILE, *args])


def _pair_state(python: Path) -> str | None:
    """The ``pair`` state ``status --json`` gives on the app's profile, for the real HOME."""
    done = _app_host(python, ["--json", "status"])
    try:
        found = json.loads(done.stdout.splitlines()[-1])
        return str(found["pair"]["state"])
    except (IndexError, KeyError, TypeError, ValueError):
        print(f"note: status --json: {_shown(done)}")
        return None


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
                _app_host(python, ["roots", "remove", alias])
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
                _app_host(python, ["roots", "remove", alias])
        # The link alone, never what it leads to.
        if link.is_symlink():
            link.unlink()
        try:
            link.parent.rmdir()
        except OSError:
            pass


def _drive_code_window(checker: Checker, python: Path, screens: Path, logs: Sequence[Path]) -> None:
    """With a code-shaped text on the pasteboard, choose Connect to Lucille…: the code window opens
    with its field empty, and the field stays empty once it has the focus, so the app read nothing
    from the pasteboard. A typed word gets the hint, and Connect stays off. Command-V, typed
    through System Events, pastes exactly the text copied, which the app's own check passes;
    Connect hands it to the host on standard input, which refuses it as no code, and the app says
    so. Nothing is paired, and neither text reaches the app's standard output or error. Cancel
    closes the window, and the pasteboard is cleared."""
    pair = _pair_state(python)
    if not checker.expect(pair is not None, "status --json reads the app's profile"):
        return
    if pair != "none":
        print(f"note: the app's profile holds a pair ({pair}), so the code window was not tried")
        return
    _set_pasteboard(PASTEBOARD_SENTINEL)
    try:
        worked, said = _choose(CONNECT_ITEM.removesuffix("…"))
        opened = worked and _wait_until(lambda: CODE_TITLE in _window_names(), 15)
        _screenshot(screens / "17-code-window.png")
        if not checker.expect(
            opened, f"choosing {CONNECT_ITEM!r} opens the window {CODE_TITLE!r}", said
        ):
            return
        texts = _texts()
        checker.expect(
            all(any(words in text for text in texts) for words in CODE_WORDS),
            "it says where the code comes from, how to paste it, and never to paste one someone "
            "sent",
            f"{texts}",
        )
        checker.expect(
            _field() == (True, ""),
            "the code field opens empty: the app read nothing from the pasteboard",
            f"{_field()}",
        )
        worked, said = _system_events(
            "set frontmost to true\n" + _target("text field") + "set focused of theTarget to true"
        )
        time.sleep(1)
        checker.expect(
            worked and _field() == (True, ""),
            "it is still empty once it has the focus",
            f"{said} {_field()}",
        )
        checker.expect(
            _enabled(CONNECT_BUTTON) == "false",
            f"{CONNECT_BUTTON} is off while the field is empty",
            _enabled(CONNECT_BUTTON),
        )
        _system_events('set frontmost to true\nkeystroke "hello"')
        hinted = _wait_until(lambda: _says(CODE_HINT), 10)
        _screenshot(screens / "18-code-hint.png")
        checker.expect(
            hinted and _enabled(CONNECT_BUTTON) == "false",
            f"a typed word that is no code gets the hint {CODE_HINT!r}, and {CONNECT_BUTTON} "
            "stays off",
            f"{_texts()}",
        )
        # Take the word out with Backspace, then paste with Command-V, which the window forwards
        # to the field: the user's own paste, through the field.
        _system_events("set frontmost to true\nrepeat 20 times\nkey code 51\nend repeat")
        _set_pasteboard(PASTED_TEXT)
        _system_events('set frontmost to true\nkeystroke "v" using command down')
        pasted = _wait_until(lambda: _field() == (True, PASTED_TEXT), 10)
        _screenshot(screens / "19-code-pasted.png")
        checker.expect(
            pasted, "Command-V pastes into the field exactly the text copied", f"{_field()}"
        )
        checker.expect(
            _enabled(CONNECT_BUTTON) == "true",
            f"{CONNECT_BUTTON} is on once the field holds something shaped like a code",
            _enabled(CONNECT_BUTTON),
        )
        worked, said = _press(CONNECT_BUTTON)
        refused = worked and _wait_until(lambda: _says(BAD_CODE), 60)
        _screenshot(screens / "20-code-refused.png")
        checker.expect(
            refused,
            f"{CONNECT_BUTTON} hands it to the host, which refuses it as no code; the app says "
            f"{BAD_CODE!r}",
            f"{said} {_texts()}",
        )
        _press("OK")
        checker.expect(_pair_state(python) == "none", "the app's profile still holds no pair")
        logged = "".join(log.read_text(encoding="utf-8", errors="replace") for log in logs)
        checker.expect(
            PASTED_TEXT not in logged and PASTEBOARD_SENTINEL not in logged,
            "neither text reaches the app's standard output or standard error",
        )
    finally:
        _set_pasteboard("")
        if CODE_TITLE in _window_names():
            worked, said = _press("Cancel")
            checker.expect(
                worked and _wait_until(lambda: CODE_TITLE not in _window_names(), 10),
                "Cancel closes the code window",
                said,
            )


def check_ui(app: Path, checker: Checker, work: Path, require_driven: bool = False) -> None:
    """Open the app and drive it the way a user would, through System Events: read the note that
    says what it is for and where its menu is, when it opens and when it is opened again; read the
    menu; choose Pause Lucille and Resume Lucille, and watch the host's pause flag; switch an
    ability on and off; add a folder through the Open panel and the name dialog, and remove it from
    the Folders submenu; add the home folder, which the app asks about first; choose the Data
    volume, and ``/`` through a link to it, which the app refuses; open the code window and paste
    into it (:func:`_drive_code_window`). Each step is checked against the host's own state, on the
    app's own profile. It uses the real HOME, and leaves the host as it found it. Where macOS
    does not let System Events drive other apps (no accessibility permission), it says so and
    drives nothing; with ``require_driven`` that is a failure. Screenshots go to
    ``<out>/check/screens``."""
    name = build_app.load_pins().app_name
    screens = work / "screens"
    screens.mkdir(parents=True, exist_ok=True)
    flag = _profile_folder() / "state" / "paused"
    _stop_earlier_copies(checker, name)
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
            _drive_code_window(checker, python_in(app), screens, (out_log, err_log))
        elif require_driven:
            checker.expect(False, "System Events drove the menu, as --require-driven asks")
    finally:
        subprocess.run(["pkill", "-TERM", "-x", name], check=False)
        checker.expect(_wait_until(lambda: not _running(name), 15), "the app stops on SIGTERM")
        for log in (out_log, err_log):
            text = log.read_text(encoding="utf-8", errors="replace")
            print(f"the app's {log.stem.removeprefix('app-')}: {text[-5000:]!r}")


# The pairing check's work room, the folder alias it adds, the file it asks the fingerprint of,
# and how long the requests run during which it quits: the first long enough for the finishing
# panel to be read before ``run`` drains, the second long enough that only Stop now ends it.
PAIRING_ROOM: Final = "work-1"
PAIRING_ALIAS: Final = "pairing-check"
PAIRING_FILE: Final = b"the file the pairing check asks the fingerprint of\n"
QUIT_DURING: Final = 20
STOP_DURING: Final = 120
# A SAS as the app shows it (spec/CLI.md section 6, ``pair``).
SAS: Final = re.compile(r"\b[0-9]{4}-[0-9]{4}\b")


class _Stop(Exception):
    """A step that every later one needs did not hold (and was counted): the check tidies up."""


@dataclasses.dataclass(frozen=True)
class _Harness:
    """What the pairing check takes from the test harness and from Switchboard, which
    :func:`check_pairing` imports only when it runs."""

    child: Any  # tests._process.Child
    child_env: Callable[[Path], dict[str, str]]
    until: Callable[..., Any]
    body: Callable[..., dict[str, Any]]
    fake_lucille: Any  # tests._lucille.FakeLucille
    loopback_hub: Any  # tests._process.LoopbackHub
    client: Any  # switchboard.client.Client
    hub_program: Path
    listening: re.Pattern[str]


def _value_within(get: Callable[[], Any], seconds: float) -> Any:
    """What ``get()`` returns once it is truthy, asked every second for at most ``seconds``; its
    last answer if it never is."""
    deadline = time.monotonic() + seconds
    while True:
        found = get()
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(1)


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps(pid: int, field: str) -> str:
    done = subprocess.run(
        ["ps", "-ww", "-o", f"{field}=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()


def _parent(pid: int) -> int | None:
    found = _ps(pid, "ppid")
    return int(found) if found.isdigit() else None


def _sleeping(run: int) -> bool:
    """Whether ``run`` has started the process of a ``host.sleep`` request."""
    done = subprocess.run(
        ["pgrep", "-P", str(run), "-f", "^/bin/sleep "], capture_output=True, text=True, check=False
    )
    return bool(done.stdout.split())


def _profile_processes() -> list[int]:
    """The host's processes on the app's profile, whoever started them."""
    done = subprocess.run(
        ["pgrep", "-f", f"lucille_host --profile {APP_PROFILE} "],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(pid) for pid in done.stdout.split()]


def _close_pairing_window() -> tuple[bool, str]:
    """Click the close button of the app's pairing window: the window titled as the code window
    or the compare page, or the one that holds They Match."""
    titles = "{" + ", ".join(_quoted(title) for title in (CODE_TITLE, COMPARE_TITLE)) + "}"
    return _system_events(
        "repeat with theWindow in windows\n"
        f"set held to (name of theWindow is in {titles})\n"
        "try\n"
        "repeat with theElement in (entire contents of theWindow)\n"
        "try\n"
        f"if class of theElement is button and name of theElement is {_quoted(THEY_MATCH)} then "
        "set held to true\n"
        "end try\n"
        "end repeat\n"
        "end try\n"
        "if held then\n"
        'click (first button of theWindow whose subrole is "AXCloseButton")\n'
        'return "closed"\n'
        "end if\n"
        "end repeat\n"
        'error "no pairing window is open"'
    )


def _pairing_window_open() -> bool:
    return bool({CODE_TITLE, COMPARE_TITLE} & set(_window_names())) or (
        _enabled(THEY_MATCH) != "absent"
    )


class _PairingCheck:
    """One run of the pairing check: a hub of its own, a fake Lucille, the harness's commands on
    the app's profile, and the app, driven one step after another (:meth:`go`)."""

    def __init__(
        self,
        app: Path,
        checker: Checker,
        work: Path,
        tmp: Path,
        harness: _Harness,
        require_driven: bool,
    ) -> None:
        self.app = app
        self.python = python_in(app)
        self.checker = checker
        self.work = work
        self.screens = work / "screens"
        self.tmp = tmp
        self.h = harness
        self.require_driven = require_driven
        self.children: list[Any] = []
        # The app's standard error, a file for each time the check opens it.
        self.logs: list[Path] = []
        # The harness's commands run for the real HOME, where the app keeps its profile.
        self.host_env = harness.child_env(Path.home())
        self.profile = _profile_folder()
        self.set_aside: Path | None = None
        # Whether the profile folder is the check's own, once anything there before was set aside:
        # only then may the check stop the profile's processes and remove the folder.
        self.owns_profile = False
        self.folder = Path.home() / "lucille-host-pairing-check"
        self.lucille: Any = None
        self.url = ""

    # --- small things ---------------------------------------------------------------------

    def need(self, ok: bool, what: str, detail: str = "") -> None:
        """Count ``what``; if it does not hold, stop: the steps after it need it."""
        if not self.checker.expect(ok, what, detail):
            raise _Stop(what)

    def start(self, argv: list[str], *, env: dict[str, str], piped: bool) -> Any:
        child = self.h.child(argv, env=env, cwd=self.tmp, piped=piped)
        self.children.append(child)
        return child

    def host_argv(self, *args: str) -> list[str]:
        """The host on the app's profile, through the app's own interpreter: isolated, and writing
        no bytecode into the bundle, whose signature the check verifies at its end."""
        return [str(self.python), "-I", "-B", "-m", "lucille_host", "--profile", APP_PROFILE, *args]

    def host(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.host_argv(*args),
            input="",
            capture_output=True,
            text=True,
            env=self.host_env,
            cwd=self.tmp,
            timeout=180,
            check=False,
        )

    def status(self) -> dict[str, Any]:
        done = self.host("--json", "status")
        try:
            found = json.loads(done.stdout.splitlines()[-1])
        except (IndexError, ValueError):
            print(f"note: status --json: {_shown(done)}")
            return {}
        return found if isinstance(found, dict) else {}

    def pair_state(self) -> Any:
        return self.status().get("pair", {}).get("state")

    def run_pid(self) -> int | None:
        run = self.status().get("run", {})
        pid = run.get("pid")
        return pid if run.get("state") == "running" and isinstance(pid, int) else None

    def reading(self) -> bool:
        rooms = self.status().get("work_rooms", [])
        return any(room.get("state") == "reading" for room in rooms)

    def lifeline_ends(self, cause: str | None = None) -> int:
        """How many times the host's log says that ``run``'s lifeline ended: for ``cause``
        (``stdin_end``, ``stdout_gone``) if one is given. The log records the first cause only."""
        done = self.host("--json", "log", "--limit", "1000")
        ends = 0
        for line in done.stdout.splitlines():
            try:
                record = json.loads(json.loads(line)["text"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                isinstance(record, dict)
                and record.get("kind") == "lifeline"
                and (cause is None or record.get("event") == cause)
            ):
                ends += 1
        return ends

    def app_pid(self) -> int | None:
        pids = _running(APP_NAME)
        return int(pids[0]) if len(pids) == 1 else None

    def stderr_now(self) -> str:
        """What the app has written on standard error since the check last opened it."""
        if not self.logs:
            return ""
        return self.logs[-1].read_text(encoding="utf-8", errors="replace")

    def answer(self, job_id: str, seconds: float) -> dict[str, Any]:
        """The body of the host's reply to the request ``job_id``, once Lucille can read it."""
        reply = _value_within(lambda: self.lucille.reply(PAIRING_ROOM, job_id), seconds)
        found = reply.get("body") if isinstance(reply, dict) else None
        return found if isinstance(found, dict) else {}

    def launch(self, label: str) -> None:
        """Open the app as Finder does, its standard output and error into files of their own,
        and choose OK on the note it shows first."""
        out = self.work / f"pairing-{label}-stdout.txt"
        err = self.work / f"pairing-{label}-stderr.txt"
        for log in (out, err):
            log.write_text("", encoding="utf-8")
        self.logs.append(err)
        done = subprocess.run(
            ["open", "--stdout", str(out), "--stderr", str(err), str(self.app)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.need(
            done.returncode == 0 and _wait_until(lambda: self.app_pid() is not None, 15),
            f"open starts the app ({label})",
            _shown(done),
        )
        time.sleep(3)
        if not _can_drive():
            if self.require_driven:
                self.checker.expect(False, "System Events drove the app, as --require-driven asks")
            raise _Stop("System Events cannot drive the app here")
        _welcome(self.checker, self.screens / f"p-{label}-welcome.png", f"when it opens ({label})")

    def ask_to_quit(self, shot: str) -> None:
        worked, said = _choose(QUIT_ITEM)
        asked = worked and _wait_until(lambda: _says(QUIT_QUESTION), 15)
        _screenshot(self.screens / shot)
        self.need(asked, f"the app's own Quit asks first: {QUIT_QUESTION!r}", f"{said} {_texts()}")
        self.checker.expect(_says(QUIT_TEXT), f"it says {QUIT_TEXT!r}", f"{_texts()}")

    # --- the steps ------------------------------------------------------------------------

    def set_up(self) -> None:
        """No copy of the app runs, nor any host process of its profile; the profile, if there
        is one, is set aside; then the hub and Lucille."""
        _stop_earlier_copies(self.checker, APP_NAME)
        self.need(
            _wait_until(lambda: not _profile_processes(), 90),
            "no host process of the app's profile runs",
            f"{_profile_processes()}",
        )
        _set_pasteboard("")
        if self.profile.exists():
            aside = self.profile.with_name(f"{APP_PROFILE}.set-aside-{os.getpid()}")
            self.profile.rename(aside)
            self.set_aside = aside
            print(f"note: the app's profile is set aside at {aside} until the end")
        self.owns_profile = True
        # Lucille's client, in this process, reads no Switchboard variable and finds no signer.
        for name in [name for name in os.environ if name.startswith("SWITCHBOARD_")]:
            del os.environ[name]
        runtime = self.tmp / "lucille-runtime"
        runtime.mkdir(mode=0o700)
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
        hub_home = self.tmp / "hub-home"
        hub_home.mkdir(mode=0o700)
        hub = self.start(
            [
                str(self.h.hub_program),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--db",
                str(self.tmp / "hub.db"),
                "--log-level",
                "info",
            ],
            env=self.h.child_env(hub_home),
            piped=False,
        )

        def listening() -> str | None:
            assert hub.proc.poll() is None, hub.describe()
            found = self.h.listening.search(hub.stderr)
            return found.group(1) if found else None

        self.url = self.h.until(listening, "the hub listening", self.children)
        print(f"the hub listens at {self.url}")
        self.lucille = self.h.fake_lucille(
            self.h.loopback_hub(self.url), self.tmp / "lucille", connect=self.h.client
        )

    def pair_through_the_harness(self) -> None:
        """Pair the app's profile as a test harness does (spec/CLI.md section 7): ``pair
        --code-stdin`` with ``--hub`` and ``--store-file``, the code on standard input, Lucille
        collecting the answer. It ends once it has shown the SAS, so the SAS is pending, and the
        app must show it again before They Match counts."""
        code = self.lucille.start()
        pair = self.start(
            self.host_argv("pair", "--code-stdin", "--json", "--hub", self.url, "--store-file"),
            env=self.host_env,
            piped=True,
        )
        pair.write(code.encode() + b"\n")

        def collected() -> bool:
            assert pair.proc.poll() in (None, 0), pair.describe()
            return self.lucille.collect().state == "paired"

        self.h.until(collected, "Lucille collecting this Mac's answer", self.children)
        self.need(
            pair.wait() == 0, "the harness's pair ends once the SAS is shown", pair.describe()
        )
        said = pair.objects()
        shown = said[-1] if said else {}
        self.need(
            shown.get("state") == "sas_pending" and shown.get("sas") == self.lucille.sas(),
            "it showed the SAS Lucille's side shows",
            pair.describe(),
        )
        self.need(self.pair_state() == "sas_pending", "status then says the SAS is pending")

    def confirm_in_the_app(self) -> None:
        """Open the pairing window from the menu: it shows Lucille's number, Return and Space
        answer nothing, and They Match confirms."""
        self.launch("pairing")
        sas = self.lucille.sas()
        # The app picks a pending pairing back up once its first note is closed, and may show the
        # compare page on its own. Close it, as a user would, and open it again from the menu.
        if _wait_until(_pairing_window_open, 15):
            _wait_until(lambda: _enabled(THEY_MATCH) == "true", 20)
            _screenshot(self.screens / "p01-compare-at-launch.png")
            print("the app showed its pairing window on its own, once its first note was closed")
            worked, said = _close_pairing_window()
            self.need(
                worked and _wait_until(lambda: not _pairing_window_open(), 10),
                "its close button closes the pairing window",
                said,
            )
        found, shown = _menu_until(lambda items: FINISH_ITEM in items, 60)
        _menu_shot(self.screens / "p02-menu-finish.png")
        self.need(
            found,
            f"with the SAS pending and no pairing window, the menu offers {FINISH_ITEM!r}",
            f"{shown}",
        )
        self.checker.expect(CONNECTING in shown, f"and says {CONNECTING!r}", f"{shown}")
        worked, said = _choose(FINISH_ITEM.removesuffix("…"))
        self.need(worked, f"{FINISH_ITEM!r} can be chosen", said)

        def numbers() -> list[str]:
            return [number for text in _texts() for number in SAS.findall(text)]

        drawn = _wait_until(lambda: bool(numbers()) and _enabled(THEY_MATCH) == "true", 30)
        _screenshot(self.screens / "p03-compare.png")
        shown_numbers = numbers()
        self.need(
            drawn and shown_numbers == [sas],
            f"the pairing window shows Lucille's number, {sas}, once and no other, and They Match "
            "is on",
            f"{shown_numbers} {_texts()} {_enabled(THEY_MATCH)}",
        )
        self.checker.expect(_says(COMPARE_QUESTION), f"it asks {COMPARE_QUESTION!r}", f"{_texts()}")
        # Neither button answers Return or Space: only a click does.
        for key, code in (("Return", 36), ("Space", 49)):
            _system_events(f"set frontmost to true\nkey code {code}")
            time.sleep(4)
            self.checker.expect(
                self.pair_state() == "sas_pending",
                f"{key} answers nothing: the SAS is still pending",
            )
        worked, said = _press(THEY_MATCH)
        confirmed = worked and _wait_until(lambda: _says(CONFIRMED_TITLE), 60)
        _screenshot(self.screens / "p04-confirmed.png")
        self.need(
            confirmed,
            f"They Match confirms, and the app says {CONFIRMED_TITLE!r}",
            f"{said} {_texts()}",
        )
        _press("OK")
        self.need(self.pair_state() == "confirmed", "status then says the pair is confirmed")

    def run_reads_the_room(self) -> None:
        """The app starts ``run`` as its child, with the lifeline; a second ``run`` of the profile
        is refused; ``run`` reads the room Lucille offers, and the menu says so."""
        run = _value_within(self.run_pid, 60)
        self.need(run is not None, "the app starts run once the pair is confirmed", f"{run}")
        app = self.app_pid()
        self.checker.expect(
            _parent(run) == app, "run is the app's child", f"run {run}, parent {_parent(run)}"
        )
        command = _ps(run, "command")
        self.checker.expect(
            f"-m lucille_host --profile {APP_PROFILE} run --json-events --exit-on-stdin-eof"
            in command,
            "it runs on the app's profile, ended by the end of its standard input",
            command,
        )
        descriptors = subprocess.run(
            ["lsof", "-a", "-p", str(run), "-d", "0", "-F", "t"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.checker.expect(
            "tPIPE" in descriptors.stdout.split(),
            "its standard input is a pipe: the lifeline",
            _shown(descriptors),
        )
        second = self.start(
            self.host_argv("run", "--json-events", "--exit-on-stdin-eof"),
            env=self.host_env,
            piped=True,
        )
        refused = second.wait() == 21
        self.checker.expect(
            refused
            and second.objects() == [{"v": 1, "type": "stopped", "reason": "already_running"}]
            and _alive(run),
            "a second run of the profile is refused (exit 21), and the app's run goes on",
            second.describe(),
        )
        self.lucille.offer(PAIRING_ROOM)
        self.need(_wait_until(self.reading, 90), "run reads the room Lucille offers")
        found, shown = _menu_until(lambda items: CONNECTED in items, 60)
        _menu_shot(self.screens / "p05-connected.png")
        self.checker.expect(found, f"the menu then says {CONNECTED!r}", f"{shown}")
        sockets = subprocess.run(
            ["lsof", "-a", "-p", str(app), "-i"], capture_output=True, text=True, check=False
        )
        self.checker.expect(
            app is not None and not sockets.stdout.strip(),
            "the app itself holds no network connection",
            sockets.stdout,
        )

    def fingerprint(self) -> None:
        """A folder and files.sha256, added through the command line on the app's profile; one
        request on that folder, answered."""
        files = self.folder / "Files"
        files.mkdir(parents=True, exist_ok=True)
        (files / "a.txt").write_bytes(PAIRING_FILE)
        for args in (
            ("roots", "add", "--", PAIRING_ALIAS, str(files)),
            ("ops", "enable", "files.sha256"),
        ):
            done = self.host(*args)
            self.need(
                done.returncode == 0,
                f"{' '.join(args[:2])} on the app's profile, through the command line",
                _shown(done),
            )
        sent = self.h.body(op="files.sha256", args={"root": PAIRING_ALIAS, "path": "a.txt"})
        self.lucille.request(PAIRING_ROOM, sent)
        answer = self.answer(sent["job_id"], 90)
        digest = hashlib.sha256(PAIRING_FILE).hexdigest()
        self.checker.expect(
            answer.get("status") == "done" and digest in str(answer.get("stdout", "")),
            "a files.sha256 request on that folder is answered with the file's fingerprint",
            f"{answer}",
        )

    def welcome_again(self, shot: str) -> None:
        """Open the running app again, as Finder does: it shows its note about the menu bar."""
        done = subprocess.run(["open", str(self.app)], capture_output=True, text=True, check=False)
        noted = done.returncode == 0 and _wait_until(lambda: _says(WELCOME_TITLE), 20)
        _screenshot(self.screens / shot)
        self.need(
            noted,
            f"opened again while it runs, the app says {WELCOME_TITLE!r}",
            f"{_shown(done)} {_texts()}",
        )

    def backoff(self) -> None:
        """``run`` killed with SIGKILL is a crash: the app starts it again after its back-off,
        while a note is up too. A note's modal loop must hold back none of the app's timers, and
        none of its children's events."""
        run = self.run_pid()
        self.need(run is not None, "run is running")
        self.welcome_again("p05b-note-up.png")
        os.kill(run, signal.SIGKILL)

        def started_again() -> int | None:
            pid = self.run_pid()
            return pid if pid is not None and pid != run else None

        again = _value_within(started_again, 60)
        self.need(
            again is not None,
            "killed with SIGKILL, run is started again after the back-off",
            f"{self.status().get('run')}",
        )
        self.checker.expect(
            _says(WELCOME_TITLE), "it was started again while the note was still up", f"{_texts()}"
        )
        worked, said = _press("OK")
        self.checker.expect(
            worked and _wait_until(lambda: not _says(WELCOME_TITLE), 10), "OK closes the note", said
        )
        self.checker.expect(_parent(again) == self.app_pid(), "by the app, as its child")
        self.checker.expect(
            f"{SIGNALED_LINE} 9" in self.stderr_now(),
            "the app says on standard error that a signal ended run",
            self.stderr_now()[-2000:],
        )
        self.need(_wait_until(self.reading, 90), "the new run reads the room again")

    def app_killed(self) -> None:
        """The app killed with SIGKILL leaves ``run``'s standard input with no writer: ``run``
        drains and exits (spec/CLI.md section 13.4)."""
        app, run = self.app_pid(), self.run_pid()
        self.need(app is not None and run is not None, "the app and its run are running")
        ends = self.lifeline_ends()
        os.kill(app, signal.SIGKILL)
        self.need(
            _wait_until(lambda: not _alive(run), 90),
            "once the app is killed with SIGKILL, run exits",
            f"{self.status().get('run')}",
        )
        state = self.status().get("run", {}).get("state")
        self.checker.expect(
            state == "stopped", "status then says run stopped, as after a drain", f"{state}"
        )
        self.checker.expect(
            self.lifeline_ends() > ends,
            "the host's log says run's lifeline ended (its standard input, or its standard output)",
        )

    def relaunch(self) -> None:
        self.launch("relaunched")
        run = _value_within(self.run_pid, 60)
        self.need(
            run is not None and _parent(run) == self.app_pid(),
            "opened again, the app starts run, as its child",
            f"{run}",
        )
        self.need(_wait_until(self.reading, 90), "run reads the room again")

    def quit_and_drain(self) -> None:
        """The app's own Quit, from its note about the menu bar, while a request runs: the
        question, then the finishing panel; the app quits once ``run`` has finished the request and
        drained. The note's Quit is chosen inside the note's own modal loop, and AppKit's wait to
        quit is one too: the drain must go on inside it."""
        done = self.host("ops", "enable", "host.sleep")
        self.need(
            done.returncode == 0,
            "ops enable host.sleep on the app's profile, through the command line",
            _shown(done),
        )
        app, run = self.app_pid(), self.run_pid()
        self.need(app is not None and run is not None, "the app and its run are running")
        ends = self.lifeline_ends("stdin_end")
        sent = self.h.body(op="host.sleep", args={"n": QUIT_DURING})
        self.lucille.request(PAIRING_ROOM, sent)
        self.need(_wait_until(lambda: _sleeping(run), 60), "a host.sleep request starts")
        self.welcome_again("p06-welcome-again.png")
        worked, said = _press(QUIT_ITEM)
        asked = worked and _wait_until(lambda: _says(QUIT_QUESTION), 15)
        _screenshot(self.screens / "p06-quit-question.png")
        self.need(
            asked, f"the note's {QUIT_ITEM} asks first: {QUIT_QUESTION!r}", f"{said} {_texts()}"
        )
        self.checker.expect(_says(QUIT_TEXT), f"it says {QUIT_TEXT!r}", f"{_texts()}")
        worked, said = _press(QUIT_BUTTON)
        finishing = worked and _wait_until(lambda: _says(FINISHING), 15)
        _screenshot(self.screens / "p07-finishing.png")
        self.checker.expect(
            finishing and _enabled(STOP_NOW) == "true",
            f"while run finishes the request, the app says {FINISHING!r}, and offers {STOP_NOW!r}",
            f"{said} {_texts()}",
        )
        self.need(
            _wait_until(lambda: not _alive(app), QUIT_DURING + 120),
            "the app quits once run has drained",
        )
        answer = self.answer(sent["job_id"], 60)
        self.checker.expect(
            answer.get("status") == "done",
            "the request finished, and its reply was posted",
            f"{answer}",
        )
        self.checker.expect(not _alive(run), "run has exited")
        self.checker.expect(
            DRAINED_LINE in self.stderr_now(),
            f"the app says on standard error {DRAINED_LINE!r}",
            self.stderr_now()[-2000:],
        )
        status = self.status()
        self.checker.expect(
            status.get("run", {}).get("state") == "stopped" and status.get("paused") is False,
            "status says run stopped, and nothing paused the host",
            f"{status.get('run')}, paused {status.get('paused')}",
        )
        self.checker.expect(
            self.lifeline_ends("stdin_end") > ends,
            "the host's log says run's standard input ended",
        )

    def stop_now(self) -> None:
        """Quit while a request runs that would outlast the check, then Stop now: ``kill``
        stops the request, Lucille is told, and the host is paused."""
        self.launch("stop-now")
        run = _value_within(self.run_pid, 60)
        self.need(run is not None, "opened again, the app starts run")
        self.need(_wait_until(self.reading, 90), "run reads the room again")
        app = self.app_pid()
        sent = self.h.body(op="host.sleep", args={"n": STOP_DURING})
        self.lucille.request(PAIRING_ROOM, sent)
        self.need(_wait_until(lambda: _sleeping(run), 60), "a host.sleep request starts")
        self.ask_to_quit("p08-quit-question.png")
        _press(QUIT_BUTTON)
        offered = _wait_until(lambda: _enabled(STOP_NOW) == "true", 15)
        _screenshot(self.screens / "p09-finishing.png")
        self.need(offered, f"the finishing panel offers {STOP_NOW!r}", f"{_texts()}")
        worked, said = _press(STOP_NOW)
        self.need(worked, f"{STOP_NOW!r} can be chosen", said)
        stopping = _wait_until(lambda: _says(STOPPING_NOW), 5)
        _screenshot(self.screens / "p10-stopping.png")
        print(f"the panel said {STOPPING_NOW!r} after Stop now: {stopping}")
        self.need(_wait_until(lambda: not _alive(app), 120), "the app quits once run has stopped")
        answer = self.answer(sent["job_id"], 60)
        self.checker.expect(
            (answer.get("status"), answer.get("code")) == ("failed", "killed_locally"),
            "Stop now runs kill: Lucille is told the request failed, killed_locally",
            f"{answer}",
        )
        status = self.status()
        self.checker.expect(
            status.get("paused") is True and status.get("run", {}).get("state") == "stopped",
            "the host is then paused, and run stopped",
            f"{status.get('run')}, paused {status.get('paused')}",
        )

    def paused_at_launch(self) -> None:
        """Opened again, the app says the host is paused, and resumes it only when asked."""
        self.launch("paused")
        asked = _wait_until(lambda: _says(PAUSED_TITLE), 30)
        _screenshot(self.screens / "p11-paused-at-launch.png")
        self.need(
            asked and _enabled(RESUME_BUTTON) == "true",
            f"opened again, the app says {PAUSED_TITLE!r}, and offers {RESUME_BUTTON}",
            f"{_texts()}",
        )
        time.sleep(10)
        self.checker.expect(
            self.status().get("paused") is True,
            "ten seconds later the host is still paused: the app resumes only when asked",
        )
        worked, said = _press(RESUME_BUTTON)
        self.need(
            worked and _wait_until(lambda: self.status().get("paused") is False, 30),
            f"{RESUME_BUTTON} resumes the host",
            said,
        )

    def disconnect(self) -> None:
        """Disconnect drains ``run``, then forgets the pair, and says whether Lucille was told."""
        run = _value_within(self.run_pid, 60)
        self.need(run is not None, "run is running")
        found, shown = _menu_until(lambda items: DISCONNECT_ITEM in items, 60)
        self.need(found, f"while connected, the menu offers {DISCONNECT_ITEM!r}", f"{shown}")
        worked, said = _choose(DISCONNECT_ITEM.removesuffix("…"))
        asked = worked and _wait_until(lambda: _says(DISCONNECT_TITLE), 15)
        _screenshot(self.screens / "p12-disconnect-question.png")
        self.need(asked, f"Disconnect asks first: {DISCONNECT_TITLE!r}", f"{said} {_texts()}")
        worked, said = _press(DISCONNECT_BUTTON)
        told = worked and _wait_until(lambda: _says(DISCONNECTED_TOLD), 120)
        _screenshot(self.screens / "p13-disconnected.png")
        self.checker.expect(told, f"then the app says {DISCONNECTED_TOLD!r}", f"{said} {_texts()}")
        _press("OK")
        self.checker.expect(not _alive(run), "run has exited")
        self.checker.expect(
            DRAINED_LINE in self.stderr_now(),
            f"run was drained before the pair was forgotten: the app says {DRAINED_LINE!r}",
            self.stderr_now()[-2000:],
        )
        self.checker.expect(self.pair_state() == "none", "the app's profile then holds no pair")
        found, shown = _menu_until(
            lambda items: NOT_CONNECTED in items and CONNECT_ITEM in items, 60
        )
        _menu_shot(self.screens / "p14-not-connected.png")
        self.checker.expect(
            found, f"the menu then says {NOT_CONNECTED!r}, and offers {CONNECT_ITEM!r}", f"{shown}"
        )

    def tidy(self) -> None:
        """Stop the app, the host's processes and the hub; forget whatever pair is left; put the
        profile back as it was; and check that nothing was written into the bundle."""
        if _running(APP_NAME):
            subprocess.run(["pkill", "-TERM", "-x", APP_NAME], check=False)
            self.checker.expect(
                _wait_until(lambda: not _running(APP_NAME), 15), "the app stops on SIGTERM"
            )
        if self.owns_profile and not _wait_until(lambda: not _profile_processes(), 90):
            left = _profile_processes()
            print(f"note: host processes of the app's profile still ran, and were killed: {left}")
            for pid in left:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        if self.owns_profile and self.url and self.profile.exists():
            print(f"forget, at the end: {_shown(self.host('--json', 'forget'))}")
        for child in reversed(self.children):
            child.kill()
        if self.owns_profile:
            shutil.rmtree(self.profile, ignore_errors=True)
        if self.set_aside is not None:
            try:
                self.set_aside.rename(self.profile)
                print(f"the app's profile is back at {self.profile}")
            except OSError as exc:
                self.checker.expect(
                    False, f"the app's profile is put back from {self.set_aside}", f"{exc}"
                )
        shutil.rmtree(self.folder, ignore_errors=True)
        _set_pasteboard("")
        done = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(self.app)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.checker.expect(
            done.returncode == 0,
            "the app's signature still verifies: nothing was written into the bundle",
            _shown(done),
        )
        for log in self.logs:
            text = log.read_text(encoding="utf-8", errors="replace")
            print(f"the app's standard error, {log.stem}: {text[-3000:]!r}")

    def go(self) -> None:
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("a hub of its own, and a fake Lucille", self.set_up),
            (
                "pairing through the command line, the SAS left pending",
                self.pair_through_the_harness,
            ),
            ("They Match, in the app", self.confirm_in_the_app),
            ("run reads the room Lucille offers", self.run_reads_the_room),
            ("files.sha256 on a folder", self.fingerprint),
            ("run killed while a note is up, and started again", self.backoff),
            ("the app killed", self.app_killed),
            ("the app opened again", self.relaunch),
            ("Quit from the note about the menu bar, while a request runs", self.quit_and_drain),
            ("Quit, then Stop now", self.stop_now),
            ("paused at launch", self.paused_at_launch),
            ("Disconnect", self.disconnect),
        )
        try:
            for label, step in steps:
                print(f"--- {label}", flush=True)
                step()
        except _Stop as stop:
            print(f"the check stopped at: {stop}")
            _screenshot(self.screens / "p99-stopped.png")
        except Exception:
            self.checker.expect(False, "the pairing check ran to its end", traceback.format_exc())
            _screenshot(self.screens / "p99-stopped.png")
        finally:
            self.tidy()


def check_pairing(app: Path, checker: Checker, work: Path, require_driven: bool = False) -> None:
    """Pair the app's profile through the command line with a hub of the check's own and a fake
    Lucille, then drive the app: confirm, ``run`` and its supervision, Quit, Stop now and
    Disconnect (:class:`_PairingCheck`). It uses the real HOME, where the app keeps its profile,
    and puts that profile back as it found it. Screenshots go to ``<out>/check/screens``."""
    # Imported here, not at the top: every other check runs where neither Switchboard nor the test
    # harness is installed (``uv run --no-project``), and tests/test_macos_build.py imports this
    # module wherever the suite runs.
    from switchboard.client import Client

    from tests._lucille import FakeLucille
    from tests._process import LISTENING, SWITCHBOARD, Child, LoopbackHub, child_env, until
    from tests._run import body

    if not checker.expect(SWITCHBOARD.is_file(), f"Switchboard's hub is at {SWITCHBOARD}"):
        return
    harness = _Harness(
        child=Child,
        child_env=child_env,
        until=until,
        body=body,
        fake_lucille=FakeLucille,
        loopback_hub=LoopbackHub,
        client=Client,
        hub_program=SWITCHBOARD,
        listening=LISTENING,
    )
    (work / "screens").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lucille-host-pairing-") as tmp:
        _PairingCheck(app, checker, work, Path(tmp), harness, require_driven).go()


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
            manifest = app / "Contents" / "Resources" / "build-manifest.json"
            if manifest.is_file():
                print(json.dumps(json.loads(manifest.read_text(encoding="utf-8")), indent=2))
        finally:
            for extra in ([], ["-force"]):
                if build_app.hdiutil(["detach", *extra, str(mount)]).returncode == 0:
                    break


PROBE_VERBS: Final = (
    ("status", ["--json", "status"], None),
    ("pair status", ["--json", "pair", "status"], None),
    ("pair --code-stdin, not a code", ["pair", "--code-stdin", "--json"], "swbp1_not-a-code\n"),
    ("run, standard input already ended", ["run", "--json-events", "--exit-on-stdin-eof"], ""),
)


def _probe_one(python: Path, home: Path, args: list[str], stdin: str | None) -> str:
    env = {
        "HOME": str(home),
        "PATH": "/var/empty",
        "LANG": "en_US.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", ""),
    }
    started = time.monotonic()
    try:
        done = subprocess.run(
            [str(python), "-I", "-B", "-m", "lucille_host", "--profile", "mac-app", *args],
            input=stdin if stdin is not None else None,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            cwd="/",
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after 60 s ({time.monotonic() - started:.1f} s)"
    took = time.monotonic() - started
    return f"{took:.1f} s, {_shown(done)[:2000]}"


def check_probe(app: Path) -> None:
    """Print what the verbs the app is about to use do on this runner. Printed only, never a gate:
    whatever the probe meets, an exception included, it prints and returns.

    Each runs as the app will run it: profile mac-app, PATH=/var/empty, working directory /. Once
    under a fresh temporary HOME and once under the runner's own HOME, because whether macOS's
    Keychain is usable under each is what the app's tests must be written for.
    """
    try:
        _probe(app)
    except Exception:
        print(f"note: the probe stopped: {traceback.format_exc()}")


def _probe(app: Path) -> None:
    python = python_in(app)
    for what in (["security", "list-keychains"], ["security", "default-keychain"]):
        done = subprocess.run(what, capture_output=True, text=True, check=False)
        print(f"{' '.join(what)}: {_shown(done)}")
    with tempfile.TemporaryDirectory(prefix="probe-home-") as temporary:
        for label, home in (("temporary HOME", Path(temporary)), ("runner HOME", Path.home())):
            for name, args, stdin in PROBE_VERBS:
                print(f"--- {label}: {name}")
                print(_probe_one(python, home, args, stdin), flush=True)


CHECKS: Final = (
    "host",
    "swift",
    "relocate",
    "links",
    "signature",
    "launch",
    "ui",
    "gatekeeper",
    "dmg",
    "probe",
    "pairing",
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_app.py", description="Check a built app.")
    parser.add_argument("check", choices=CHECKS)
    parser.add_argument(
        "--out", type=Path, default=build_app.DEFAULT_OUT, help="the folder build_app.py wrote to"
    )
    parser.add_argument(
        "--require-driven",
        action="store_true",
        help="ui and pairing: fail if System Events cannot drive the app (CI passes this)",
    )
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("check_app.py: these checks run on macOS", file=sys.stderr)
        return 2
    out = args.out.resolve()
    app = app_path(out)
    dmgs = build_app.disk_images(out, APP_NAME)
    work = out / "check"
    checker = Checker()
    if args.check != "dmg" and not checker.expect(app.is_dir(), f"{app} exists"):
        return 1
    if args.check == "host":
        check_host(app, checker)
    elif args.check == "swift":
        check_swift(app, checker, work)
    elif args.check == "relocate":
        check_relocate(app, checker, work)
    elif args.check == "links":
        check_links(app, checker)
    elif args.check == "signature":
        check_signature(app, checker)
    elif args.check == "launch":
        check_launch(app, checker)
    elif args.check == "ui":
        check_ui(app, checker, work, args.require_driven)
    elif args.check == "gatekeeper":
        check_gatekeeper(app, dmgs)
    elif args.check == "probe":
        check_probe(app)
    elif args.check == "pairing":
        check_pairing(app, checker, work, args.require_driven)
    else:
        check_dmg(dmgs, checker)
    if checker.failures:
        print(f"{len(checker.failures)} failed: " + "; ".join(checker.failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
