"""Checks on a built Lucille Host app. CI runs each one on a macOS runner after build_app.py.

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
- ``links``: no Mach-O file in the bundle loads a library from outside macOS or the bundle (no
  ``/Library/Frameworks``, ``/usr/local``, ``/opt/homebrew`` or build folder); none says it needs a
  newer macOS than ``LSMinimumSystemVersion``; no file in the bundle holds the build folder's
  path; and the app's own executable imports no function from :data:`FORBIDDEN_IMPORTS` and the
  lists after it, which start processes or open network connections, and links none of
  :data:`FORBIDDEN_LIBRARIES`. That last part reads only what the executable imports by name: code
  that reaches such a function another way (through the Objective-C runtime, say) is not seen.
- ``signature``: ``codesign --verify --deep --strict`` passes on the app, and ``codesign --verify
  --strict`` on each Mach-O file in it; every Mach-O file has the hardened runtime flag; the app
  and the interpreter carry no entitlements.
- ``launch``: opens the app the way Finder does, checks that it is still running 10 seconds later,
  then stops it.
- ``ui``: opens the app and drives its menu as a user would, through System Events: reads the
  menu; chooses Pause Lucille, then Resume Lucille; switches an ability on and off; adds a folder
  through the Open panel and the name dialog, with a name the app must correct first, and removes
  it from the Folders submenu; chooses the home folder, which the app asks about first, picks
  Choose a Smaller Folder once, then adds it. It checks each step against the host's own state.
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
# A library path a Mach-O file may name: macOS's own, or one relative to the bundle.
ALLOWED_LINK_PREFIXES: Final = (
    "/usr/lib/",
    "/System/Library/",
    "@rpath/",
    "@loader_path/",
    "@executable_path/",
)
FORBIDDEN_LINK_PREFIXES: Final = ("/Library/Frameworks", "/usr/local", "/opt/homebrew")
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
# The Abilities menu's words for each operation it lists, as in App/HostText.swift
# (tests/test_macos_build.py checks that the two agree).
ABILITY_TITLES: Final = {"files.sha256": "Check a file's fingerprint"}
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


def _links(path: Path, commands: Sequence[str]) -> list[str]:
    """The libraries and run paths a Mach-O file names, as otool prints them."""
    names = [line.strip().split(" (")[0] for line in _otool("-L", path)[1:] if line.strip()]
    for number, line in enumerate(commands):
        if line.strip() == "cmd LC_RPATH" and number + 2 < len(commands):
            names.append("rpath " + commands[number + 2].strip().split(" (offset")[0])
    return names


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
        name for name in _links(executable, []) if any(lib in name for lib in FORBIDDEN_LIBRARIES)
    ]
    checker.expect(
        not libraries,
        "the app's executable links no networking or web framework",
        "\n".join(libraries),
    )


def check_links(app: Path, checker: Checker) -> None:
    build_dir = str(build_app.REPO_ROOT)
    minimum = build_app.load_pins().minimum_macos
    files = build_app.macho_files(app)
    checker.expect(bool(files), f"{len(files)} Mach-O files in the bundle")
    for path in files:
        commands = _otool("-l", path)
        versions = minimum_macos_versions(commands)
        checker.expect(
            bool(versions) and not any(newer_than(version, minimum) for version in versions),
            f"{path.relative_to(app)} runs on macOS {minimum} (it asks for {versions})",
        )
        names = _links(path, commands)
        bad = []
        for name in names:
            target = name.removeprefix("rpath path ").removeprefix("rpath ")
            if name.startswith("rpath"):
                ok = target.startswith("@")
            else:
                ok = target.startswith(ALLOWED_LINK_PREFIXES) and not target.startswith(
                    FORBIDDEN_LINK_PREFIXES
                )
            if not ok or build_dir in target:
                bad.append(name)
        checker.expect(
            not bad, f"{path.relative_to(app)} names only macOS or the bundle", "\n".join(bad)
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
    main = app / "Contents" / "MacOS" / build_app.load_pins().app_name
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
    "Quit Lucille Host",
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


def _drive_menu(checker: Checker, screens: Path, flag: Path) -> bool:
    """Read the menu, then choose Pause Lucille and Resume Lucille. False if System Events cannot
    drive the app on this machine."""
    version = build_app.host_version()
    worked, items = _system_events(
        f"{LINES}return (description of every menu bar item of last menu bar) as text"
    )
    if not worked:
        print(
            "note: System Events cannot read this app's menu bar on this machine, so the menu "
            f"was not driven: {items}"
        )
        return False
    print(f"the app's status items: {items.splitlines()}")
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
        shown[:1] == [f"Lucille Host {version}"],
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
        and "Resume Lucille" in shown
        and "Pause Lucille" not in shown,
        "the menu then says it is paused, and offers Resume Lucille",
        f"{shown}",
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
    worked, said = _system_events('click button "Choose" of window 1\ndelay 2', timeout=60)
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
        _acknowledge(checker, "Lucille can now use the folder", screens / "8-folder-added.png")
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
            worked and "Choose" in buttons and "Add Anyway" not in buttons,
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
        _acknowledge(checker, "Lucille can now use the folder", screens / "12-home-added.png")
    finally:
        for alias, path in _roots(python).items():
            if alias in added or path in (str(folder), str(home)):
                _host(python, home, ["roots", "remove", alias])
        shutil.rmtree(folder.parent, ignore_errors=True)


def check_ui(app: Path, checker: Checker, work: Path, require_driven: bool = False) -> None:
    """Open the app and drive its menu the way a user would, through System Events: read the menu;
    choose Pause Lucille and Resume Lucille, and watch the host's pause flag; switch an ability on
    and off; add a folder through the Open panel and the name dialog, and remove it from the
    Folders submenu; add the home folder, which the app asks about first. Each step is checked
    against the host's own state. It uses the real HOME, and leaves the host as it found it. Where
    macOS does not let System Events drive other apps (no accessibility permission), it says so
    and drives nothing; with ``require_driven`` that is a failure. Screenshots go to
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
    _screenshot(screens / "1-menu-bar.png")
    try:
        if _drive_menu(checker, screens, flag):
            _drive_abilities(checker, python_in(app), screens)
            _drive_folders(checker, python_in(app), screens)
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
    done = subprocess.run(
        ["hdiutil", "verify", str(dmg)], capture_output=True, text=True, check=False
    )
    checker.expect(done.returncode == 0, "hdiutil verify passes", _shown(done))
    name = build_app.load_pins().app_name
    with tempfile.TemporaryDirectory(prefix="lucille-host-dmg-") as tmp:
        mount = Path(tmp) / "mount"
        mount.mkdir()
        done = subprocess.run(
            [
                "hdiutil",
                "attach",
                "-nobrowse",
                "-readonly",
                "-noautoopen",
                "-mountpoint",
                str(mount),
                str(dmg),
            ],
            capture_output=True,
            text=True,
            check=False,
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
                detached = subprocess.run(
                    ["hdiutil", "detach", *extra, str(mount)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if detached.returncode == 0:
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
    dmgs = build_app.disk_images(out)
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
