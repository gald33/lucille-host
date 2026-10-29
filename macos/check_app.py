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
  ``/Library/Frameworks``, ``/usr/local``, ``/opt/homebrew`` or build folder), and no file in the
  bundle holds the build folder's path.
- ``signature``: ``codesign --verify --deep --strict`` passes; every Mach-O file has the hardened
  runtime flag; the app and the interpreter carry no entitlements.
- ``launch``: opens the app the way Finder does, checks that it is still running 10 seconds later,
  then stops it.
- ``gatekeeper``: prints what ``spctl`` says about the app and the disk image. It never fails: an
  ad hoc signature is refused until the app is signed with a Developer ID and notarized.
- ``dmg``: ``hdiutil verify`` passes, and the mounted image holds the app, with a valid signature,
  and a link to ``/Applications``.
"""

from __future__ import annotations

import argparse
import json
import os
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


def _links(path: Path) -> list[str]:
    """The libraries and run paths a Mach-O file names, as otool prints them."""
    listed = subprocess.run(
        ["otool", "-L", str(path)], capture_output=True, text=True, check=True
    ).stdout.splitlines()[1:]
    names = [line.strip().split(" (")[0] for line in listed if line.strip()]
    commands = subprocess.run(
        ["otool", "-l", str(path)], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for number, line in enumerate(commands):
        if line.strip() == "cmd LC_RPATH" and number + 2 < len(commands):
            names.append("rpath " + commands[number + 2].strip().split(" (offset")[0])
    return names


def check_links(app: Path, checker: Checker) -> None:
    build_dir = str(build_app.REPO_ROOT)
    files = build_app.macho_files(app)
    checker.expect(bool(files), f"{len(files)} Mach-O files in the bundle")
    for path in files:
        names = _links(path)
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


def check_gatekeeper(app: Path, dmg: Path) -> None:
    for argv in (
        ["spctl", "--assess", "--type", "execute", "--verbose=4", str(app)],
        [
            "spctl",
            "--assess",
            "--type",
            "open",
            "--context",
            "context:primary-signature",
            "--verbose=4",
            str(dmg),
        ],
    ):
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        print(f"{' '.join(argv[:4])} ...: exit {done.returncode}")
        print((done.stdout + done.stderr).strip())
    print("An ad hoc signature is expected to be refused here until notarization exists.")


def check_dmg(dmg: Path, checker: Checker) -> None:
    if not checker.expect(dmg.is_file(), f"{dmg.name} exists"):
        return
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


CHECKS: Final = ("host", "swift", "relocate", "links", "signature", "launch", "gatekeeper", "dmg")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_app.py", description="Check a built app.")
    parser.add_argument("check", choices=CHECKS)
    parser.add_argument(
        "--out", type=Path, default=build_app.DEFAULT_OUT, help="the folder build_app.py wrote to"
    )
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("check_app.py: these checks run on macOS", file=sys.stderr)
        return 2
    out = args.out.resolve()
    app = app_path(out)
    dmg = out / build_app.dmg_name(build_app.host_version())
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
    elif args.check == "gatekeeper":
        check_gatekeeper(app, dmg)
    else:
        check_dmg(dmg, checker)
    if checker.failures:
        print(f"{len(checker.failures)} failed: " + "; ".join(checker.failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
