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
  makes, through the same code.
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
  and adds neither. It checks each step against the host's own state.
  It uses the real HOME and leaves the host as it found it. Where macOS does not let System Events
  drive other apps, it says so and drives nothing, which is a failure with ``--require-driven``
  (CI passes it). Screenshots go to ``check/screens``.
- ``gatekeeper``: prints what ``spctl`` says about the app and the disk image. It never fails: an
  ad hoc signature is refused until the app is signed with a Developer ID and notarized.
- ``dmg``: ``hdiutil verify`` passes, and the mounted image holds the app, with a valid signature,
  and a link to ``/Applications``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

if __package__ in (None, ""):  # run as a script: make ``macos`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macos import build_app  # noqa: E402

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
            ],
            timeout=300,
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
# The menu's status line. Pairing is not built, so the Mac is never connected.
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
            manifest = app / "Contents" / "Resources" / "build-manifest.json"
            if manifest.is_file():
                print(json.dumps(json.loads(manifest.read_text(encoding="utf-8")), indent=2))
        finally:
            for extra in ([], ["-force"]):
                if build_app.hdiutil(["detach", *extra, str(mount)]).returncode == 0:
                    break


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
        help="ui: fail if System Events cannot drive the app's menu (CI passes this)",
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
    else:
        check_dmg(dmgs, checker)
    if checker.failures:
        print(f"{len(checker.failures)} failed: " + "; ".join(checker.failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
