"""Build the Mac app that runs the host, for Apple silicon Macs, and a disk image that holds it.
The app's name and identifiers are in ``tools/macos/pins.toml``; ``<name>`` below is that name.

    uv run --no-project python tools/macos/build_app.py plan --build-number N   # anywhere
    uv run --no-project python tools/macos/build_app.py wheel --out DIR         # anywhere
    uv run --no-project python tools/macos/build_app.py toolchain               # on a Mac
    uv run --no-project python tools/macos/build_app.py assemble ...            # on a Mac
    uv run --no-project python tools/macos/build_app.py build ...               # on a Mac
    uv run --no-project python tools/macos/build_app.py compare DIR_A DIR_B     # anywhere

``assemble`` and ``build`` take ``--host-wheel P --host-wheel-sha256 H`` (the wheel ``wheel``
built) and exactly one of ``--tag T`` (a build from a ``v*`` tag) or ``--build-number N`` (any
other build; CI passes its run number). Before doing any work they check the pins, the version
rule (:func:`bundle_versions`), the runtime archive's sha256 when ``--python-archive`` is given,
and the host wheel's name and sha256, and the icons' PNGs in ``macos/Icons/``
(:func:`icons.source_problems`); then that they run on an Apple silicon Mac with the pinned Xcode.
``assemble`` then writes, under ``--out`` (``build/macos/`` by default, which git ignores):

1. The Python runtime that the pins name, downloaded unless given, and refused unless its sha256
   is the pinned one, unpacked into ``<name>.app/Contents/Resources/runtime``.
2. The runtime prepared (:func:`prepare_runtime`): its own pip uninstalled by its ``RECORD``, the
   paths ``tools/macos/prune.txt`` lists removed, the interpreter renamed ``lucille-host-runtime``,
   every symbolic link replaced by its target, and the result checked for exactly the expected
   shape: one program, one library folder, an empty ``site-packages``, no ``.pyc``.
3. ``site-packages`` filled by :func:`payload.assemble_site_packages`: the lock's wheels for this
   platform and then the host wheel, installed with hashes required, and checked file by file
   against the wheels. The installer compiles nothing.
4. The bytecode (:func:`compile_bytecode`): the bundled interpreter writes a checked-hash ``.pyc``
   beside every ``.py`` of ``lib/python3.13``, and a second run of it checks that each is the
   same, byte for byte. The app runs the interpreter with ``-B``, so it writes none at run time.
5. The menu bar app in ``macos/App/`` compiled with ``swiftc``; ``Info.plist`` (:func:`info_plist`);
   the wrapper ``Resources/bin/lucille-host`` for Terminal users; the licence texts in
   ``Resources/licenses/``; the icons, ``Resources/AppIcon.icns`` packed from the committed PNGs
   and the six menu bar glyphs copied beside it, with nothing rendered (:func:`icons.write_icons`);
   and ``Resources/MANIFEST.json`` (:func:`manifest`).
6. ``<name>-unsigned-macOS-arm64.tar.gz``, the unsigned app as a tarball whose bytes depend on the
   files alone (:func:`payload.write_tarball`), so that two builds can be compared. It is written
   only after the bytecode's layout, M10, with the ``.pyc`` files admitted, and the icons
   (:func:`icons.bundle_problems`) are checked again.

``build`` runs ``assemble``, then signs every Mach-O file ad hoc in the order
:func:`signing_plan` gives (the libraries deepest first, then the interpreter with its own
identifier, then the bundle; with the hardened runtime when the pins say so; no entitlements),
and builds ``<name>-<version>-<commit>-adhoc-macOS-arm64.dmg``, which holds the app and a link to
``/Applications``. ``hdiutil create`` writes it in ``work/``, and the build hands on a copy that has
the same bytes, a whole UDIF trailer and a passing ``hdiutil verify`` (:func:`make_dmg`). A
signature macOS trusts needs a Developer ID and notarization, which this script does not do.

``plan`` reads the pins, the build constraints, the prune list, the version and the icons' PNGs,
and writes the ``Info.plist`` the app would get, without running any other program. ``toolchain``
checks that the pinned Xcode is installed and is the one ``xcodebuild`` runs, and with
``--github-env FILE`` writes its ``DEVELOPER_DIR`` there for the later steps of a GitHub Actions
job. ``compare`` prints whether two builds' unsigned tarballs are the same, as Markdown, and always
exits 0.

Exit codes: 0 done, 1 refused or failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

if __package__ in (None, ""):  # run as a script: make ``tools.macos`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.macos import icons, payload  # noqa: E402

# This script and its pins live in tools/macos/; the app's Swift sources stay in macos/.
TOOLS_DIR: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = TOOLS_DIR.parents[1]
MACOS_DIR: Final = REPO_ROOT / "macos"
PINS_FILE: Final = TOOLS_DIR / "pins.toml"
BUILD_CONSTRAINTS: Final = TOOLS_DIR / "build-constraints.txt"
PRUNE_FILE: Final = TOOLS_DIR / "prune.txt"
PYPROJECT: Final = REPO_ROOT / "pyproject.toml"
LOCK_FILE: Final = REPO_ROOT / "uv.lock"
SWIFT_DIR: Final = MACOS_DIR / "App"
VERSION_FILE: Final = REPO_ROOT / "src" / "lucille_host" / "__init__.py"
DEFAULT_OUT: Final = REPO_ROOT / "build" / "macos"
# Every disk image this script makes starts with the app's name and a hyphen, and ends with
# DMG_SUFFIX. A release's image, made elsewhere with a Developer ID signature, will not end so.
DMG_SUFFIX: Final = "-adhoc-macOS-arm64.dmg"
# The unsigned app, as a tarball: the app's name, then this.
UNSIGNED_SUFFIX: Final = "-unsigned-macOS-arm64.tar.gz"

PBS_RELEASES: Final = "https://github.com/astral-sh/python-build-standalone/releases/download/"
# Folder and file names inside the runtime. The pins allow Python 3.13 only, so these are fixed.
STDLIB: Final = "lib/python3.13"
SITE_PACKAGES: Final = f"{STDLIB}/site-packages"
UPSTREAM_INTERPRETER: Final = "bin/python3.13"
INTERPRETER: Final = "bin/lucille-host-runtime"
# Where the runtime and the rest of the payload are in the app (0002 §6).
RUNTIME_DIR: Final = "Contents/Resources/runtime"
RUNTIME_IN_APP: Final = f"{RUNTIME_DIR}/{INTERPRETER}"
WRAPPER_IN_APP: Final = "Contents/Resources/bin/lucille-host"
LICENSES_IN_APP: Final = "Contents/Resources/licenses"
MANIFEST_PATH: Final = "Contents/Resources/MANIFEST.json"
# How the app, the wrapper and the checks run the interpreter (0002 §6): -I ignores every
# PYTHON* variable and the user's site folder, -B writes no .pyc into the signed bundle, and
# -X utf8 makes the text encoding UTF-8 whatever the locale.
RUNTIME_FLAGS: Final = ("-I", "-B", "-X", "utf8")
# The .pyc files the build writes (0002 §6): one checked-hash file in __pycache__ beside each .py of
# the runtime's library, with this tag. The pins allow Python 3.13 only, so the tag is fixed.
BYTECODE_TAG: Final = "cpython-313"
# The flags of a .pyc file that is hash-based and checked: at each import, the interpreter checks
# the source hash in its header against the .py beside it. It does not check the code.
CHECKED_HASH: Final = 0b11
# How the build and the checks run BYTECODE_PROGRAM: the app's flags, with -S, so that no .pth file
# or site step loads a module first, and -W ignore, so that a warning while compiling loads none
# either. A module loaded first changes the bytes marshal writes (0002 §6). The environment is
# fixed, not inherited, and the program may run for up to BYTECODE_TIMEOUT seconds.
BYTECODE_FLAGS: Final = ("-I", "-B", "-S", "-W", "ignore", "-X", "utf8")
BYTECODE_ENV: Final = {"PATH": "/usr/bin:/bin"}
BYTECODE_TIMEOUT: Final = 900
# The program the bundled interpreter runs, as ``-c`` text, to write the .pyc files (``write``) and
# to check them (``check``), with RUNTIME LIB after the mode (:func:`bytecode_argv`). It compiles
# every .py under RUNTIME/LIB in memory, with the header the importer gives a checked-hash file:
# the magic number, the flags, and the source's hash. ``write`` writes each file and refuses a
# runtime that holds any bytecode; ``check`` compares each file with what it compiles, byte for
# byte, and prints a line for each that differs. The last line says how many matched. Exit 0 if
# nothing is wrong, 1 if a file is, 2 on a usage error or if compiling loaded a module. Python also
# exits 1 on an exception the program does not catch, with nothing on stdout, so a check run's
# exit 1 is read as findings only when its lines add up (:func:`bytecode_check_problems`).
#
# The bytes marshal writes for one source depend on the state of the process that compiles it,
# not only on the source (measured on Linux, 0002 §6). So the program keeps these rules, each of
# which a measurement made load-bearing: one program text for both modes (its own constants are
# part of that state); every file compiled in one process, in sorted order of its path from the
# runtime; each code object dropped before the next compile; only built-in or frozen modules
# imported, and none loaded while it compiles (else it stops, with exit 2); -S and -W ignore
# (BYTECODE_FLAGS), which it checks. The name each .pyc records for its source is its path from
# the runtime (``lib/python3.13/json/__init__.py``): the importer replaces it with the real path
# when it loads the file, so no .pyc holds the build folder, and two copies of a runtime get the
# same bytes. A .py added or removed can change the bytes of the files compiled after it.
BYTECODE_PROGRAM: Final = """\
import os
import sys
import marshal
import _imp
import _frozen_importlib_external as bootstrap

USAGE = "usage: lucille-host-runtime -I -B -S -W ignore -X utf8 -c PROGRAM write|check RUNTIME LIB"
CHECKED_HASH = (3).to_bytes(4, "little")


def main():
    f = sys.flags
    if (f.isolated, f.dont_write_bytecode, f.no_site, f.utf8_mode, f.optimize) != (1, 1, 1, 1, 0):
        print(USAGE)
        return 2
    if sys.warnoptions != ["ignore"] or sys.pycache_prefix is not None or len(sys.argv) != 4:
        print(USAGE)
        return 2
    mode, runtime, lib = sys.argv[1:]
    if mode not in ("write", "check"):
        print(USAGE)
        return 2
    tag = sys.implementation.cache_tag
    sources = []
    held = []
    for folder, dirs, files in os.walk(os.path.join(runtime, lib)):
        dirs.sort()
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
            held.append(os.path.join(folder, "__pycache__"))
        for name in files:
            if name.endswith(".py"):
                sources.append(os.path.relpath(os.path.join(folder, name), runtime))
            elif name.endswith((".pyc", ".pyo")):
                held.append(os.path.join(folder, name))
    sources.sort()
    if mode == "write" and held:
        print("the runtime already holds bytecode: " + ", ".join(sorted(held)[:10]))
        return 1
    loaded = sorted(sys.modules)
    problems = []
    done = 0
    for source in sources:
        folder, name = os.path.split(source)
        cached = os.path.join(folder, "__pycache__", name[:-3] + "." + tag + ".pyc")
        with open(os.path.join(runtime, source), "rb") as handle:
            data = handle.read()
        source_hash = _imp.source_hash(bootstrap._RAW_MAGIC_NUMBER, data)
        try:
            code = compile(data, source, "exec", dont_inherit=True, optimize=0)
        except (SyntaxError, ValueError) as error:
            problems.append(source + " does not compile: " + str(error))
            continue
        pyc = bootstrap.MAGIC_NUMBER + CHECKED_HASH + source_hash + marshal.dumps(code)
        del code
        path = os.path.join(runtime, cached)
        if mode == "write":
            os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                view = memoryview(pyc)
                while view:
                    view = view[os.write(handle, view) :]
            finally:
                os.close(handle)
            done += 1
            continue
        try:
            with open(path, "rb") as handle:
                found = handle.read()
        except FileNotFoundError:
            problems.append(cached + " is missing: " + source + " has no .pyc")
            continue
        if found[:4] != bootstrap.MAGIC_NUMBER:
            problems.append(cached + " has another Python's magic number, not this one's")
        elif found[4:8] != CHECKED_HASH:
            flags = str(int.from_bytes(found[4:8], "little"))
            problems.append(cached + " is not checked-hash: its flags are " + flags)
        elif found[8:16] != source_hash:
            problems.append(cached + " has the source hash of another file, not " + source)
        elif found != pyc:
            problems.append(cached + " is not what this interpreter compiles from " + source)
        else:
            done += 1
    if sorted(sys.modules) != loaded:
        added = sorted(set(sys.modules) - set(loaded))
        print("compiling loaded modules, so the bytes would depend on them: " + ", ".join(added))
        return 2
    for problem in problems:
        print(problem)
    said = "wrote" if mode == "write" else "found the same bytes in"
    print(said + " " + str(done) + " of " + str(len(sources)) + " .pyc files, " + tag)
    return 1 if problems else 0


raise SystemExit(main())
"""
# The profile the app runs every verb on (0002 §9), apart from the ``default`` profile of a
# command-line install. The Swift app names it too (``HostCLI.appProfile``).
APP_PROFILE: Final = "mac-app"
# The wrapper for Terminal users: it finds the bundle from its own real path, so a link to it from
# a folder on PATH still runs this bundle's interpreter, never one next to the link. It runs the
# host as the app does, with the app's flags and on the app's profile; a --profile given to it
# comes later, so it replaces that one.
WRAPPER_TEXT: Final = (
    "#!/bin/sh\n"
    'self=$(/bin/realpath -- "$0") || exit 1\n'
    'exec "${self%/*}/../runtime/bin/lucille-host-runtime" '
    + " ".join(RUNTIME_FLAGS)
    + f' -m lucille_host --profile {APP_PROFILE} "$@"\n'
)
# The distributions of the runtime itself that the build uninstalls, by their RECORD, before any
# wheel is installed: pip is the runtime's own installer and is in no locked wheel. It is the only
# *.dist-info folder the build removes; prune.txt never names one.
RUNTIME_DISTRIBUTIONS_REMOVED: Final = ("pip",)
# The Info.plist keys that hold folder_usage(): Documents, Desktop, Downloads, removable and
# network volumes, and file providers, the places macOS asks about before the host may read them.
# Each name is Apple's (developer.apple.com, Bundle Resources, Information Property List).
FOLDER_USAGE_KEYS: Final = (
    "NSDocumentsFolderUsageDescription",
    "NSDesktopFolderUsageDescription",
    "NSDownloadsFolderUsageDescription",
    "NSRemovableVolumesUsageDescription",
    "NSNetworkVolumesUsageDescription",
    "NSFileProviderDomainUsageDescription",
)
# Keys an Info.plist of this app must never carry: a scripting surface, Services, network
# exceptions, an update feed, or a URL scheme (0002 §11.5: none until the release that adds the
# nonce check of §11.4).
FORBIDDEN_PLIST_KEYS: Final = frozenset(
    {
        "NSAppleScriptEnabled",
        "OSAScriptingDefinition",
        "NSServices",
        "NSAppTransportSecurity",
        "SUFeedURL",
        "SUPublicEDKey",
        "CFBundleURLTypes",
        "CFBundleURLSchemes",
    }
)
# The tables of pins.toml, each with its keys.
PIN_TABLES: Final = {
    "python": frozenset({"version", "asset", "url", "sha256"}),
    "app": frozenset({"name", "bundle_id", "runtime_id", "deployment_target", "category"}),
    "xcode": frozenset({"version"}),
    "ripgrep": frozenset({"commit"}),
    "signing": frozenset({"adhoc_hardened_runtime"}),
}

_ASSET = re.compile(
    r"cpython-(?P<version>3\.13\.\d+)\+(?P<release>\d{8})-aarch64-apple-darwin-"
    r"install_only(?:_stripped)?\.tar\.gz"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
# The app's name becomes a file name, a volume name and part of a glob pattern, so it is only
# letters and digits, with single spaces between words.
_APP_NAME = re.compile(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+)*")
_BUNDLE_ID = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){2,}")
_MACOS_VERSION = re.compile(r"(?P<major>\d+)\.\d+")
_XCODE_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_DEVELOPER_DIR = re.compile(r"/Applications/Xcode_\d+\.\d+(?:\.\d+)?\.app/Contents/Developer")
_VERSION_LINE = re.compile(r'^__version__ = "([^"\n]+)"$', re.MULTILINE)
_RELEASE = re.compile(r"\d+(?:\.\d+){0,2}")
# A version a tag build may carry: three numbers, nothing after them.
_FINAL_RELEASE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
_BUILD_NUMBER = re.compile(r"[1-9]\d{0,17}")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
# One exact requirement, ``name==version``, as pyproject.toml and build-constraints.txt write it.
_EXACT = re.compile(r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==(?P<version>[^\s;,]+)")
# The first four bytes of a Mach-O file: thin 64- and 32-bit, either byte order, and universal.
_MACHO_MAGIC: Final = frozenset(
    {
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
)


class PinError(ValueError):
    """pins.toml, build-constraints.txt or prune.txt is not well formed."""


class BuildError(RuntimeError):
    """The build cannot go on; the message says why."""


@dataclass(frozen=True)
class Pins:
    """What pins.toml pins."""

    python_version: str
    asset: str
    url: str
    sha256: str
    app_name: str
    bundle_id: str
    runtime_id: str
    deployment_target: str
    category: str
    xcode: str
    ripgrep_commit: str
    adhoc_hardened_runtime: bool


def _table(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    keys = PIN_TABLES[name]
    table = data.get(name)
    if not isinstance(table, dict) or set(table) != keys:
        raise PinError(f"[{name}] holds exactly these keys: {', '.join(sorted(keys))}")
    for key in sorted(keys):
        value = table[key]
        if name == "signing":
            if not isinstance(value, bool):
                raise PinError(f"[{name}] {key} must be true or false")
        elif name == "ripgrep":
            if not isinstance(value, str):
                raise PinError(f"[{name}] {key} must be a string")
        elif not isinstance(value, str) or not value:
            raise PinError(f"[{name}] {key} must be a non-empty string")
    return table


def parse_pins(text: str) -> Pins:
    """The pins in ``text`` (the contents of pins.toml), checked for consistency."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PinError(f"pins.toml does not parse: {exc}") from exc
    if set(data) != set(PIN_TABLES):
        raise PinError(
            "pins.toml holds exactly these tables: "
            + ", ".join(f"[{name}]" for name in sorted(PIN_TABLES))
        )
    python = _table(data, "python")
    app = _table(data, "app")
    xcode = _table(data, "xcode")
    ripgrep = _table(data, "ripgrep")
    signing = _table(data, "signing")

    match = _ASSET.fullmatch(python["asset"])
    if match is None:
        raise PinError(
            "[python] asset must name a python-build-standalone CPython 3.13 build for "
            "aarch64-apple-darwin, flavour install_only or install_only_stripped"
        )
    if match["version"] != python["version"]:
        raise PinError(
            f"[python] version is {python['version']}, but the asset holds Python "
            f"{match['version']}"
        )
    url = f"{PBS_RELEASES}{match['release']}/{python['asset'].replace('+', '%2B')}"
    if python["url"] != url:
        raise PinError(f"[python] url must be the asset's release URL, {url}")
    if _SHA256.fullmatch(python["sha256"]) is None:
        raise PinError("[python] sha256 must be 64 lower-case hexadecimal digits")

    if _APP_NAME.fullmatch(app["name"]) is None:
        raise PinError("[app] name must be letters and digits, with single spaces between words")
    if _BUNDLE_ID.fullmatch(app["bundle_id"]) is None:
        raise PinError("[app] bundle_id must be a lower-case reverse-DNS name")
    if app["runtime_id"] != app["bundle_id"] + ".runtime":
        raise PinError("[app] runtime_id must be the bundle_id followed by .runtime")
    macos = _MACOS_VERSION.fullmatch(app["deployment_target"])
    if macos is None or int(macos["major"]) < 15:
        raise PinError("[app] deployment_target must be 15.0 or later (0002 §5: macOS 15 or later)")
    if not app["category"].startswith("public.app-category."):
        raise PinError("[app] category must be a public.app-category.* value")
    if _XCODE_VERSION.fullmatch(xcode["version"]) is None:
        raise PinError("[xcode] version must be numbers, such as 16.4")
    if ripgrep["commit"] and _GIT_COMMIT.fullmatch(ripgrep["commit"]) is None:
        raise PinError("[ripgrep] commit must be empty or 40 lower-case hexadecimal digits")
    return Pins(
        python_version=python["version"],
        asset=python["asset"],
        url=python["url"],
        sha256=python["sha256"],
        app_name=app["name"],
        bundle_id=app["bundle_id"],
        runtime_id=app["runtime_id"],
        deployment_target=app["deployment_target"],
        category=app["category"],
        xcode=xcode["version"],
        ripgrep_commit=ripgrep["commit"],
        adhoc_hardened_runtime=signing["adhoc_hardened_runtime"],
    )


def load_pins(path: Path = PINS_FILE) -> Pins:
    return parse_pins(path.read_text(encoding="utf-8"))


def _pins_name() -> str:
    return PINS_FILE.relative_to(REPO_ROOT).as_posix()


def host_version(path: Path = VERSION_FILE) -> str:
    """The host's ``__version__``, read from its source (the one place pyproject.toml reads it)."""
    found = _VERSION_LINE.findall(path.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise BuildError(f"{path} must assign __version__ exactly once")
    return found[0]


def release_triple(version: str) -> str:
    """The three numbers Apple's CFBundleShortVersionString takes: the release part of a PEP 440
    version, padded with zeros. ``0.1.0.dev0`` gives ``0.1.0``."""
    match = _RELEASE.match(version)
    if match is None:
        raise BuildError(f"{version!r} does not start with a release number")
    parts = match.group(0).split(".")
    return ".".join(parts + ["0"] * (3 - len(parts)))


@dataclass(frozen=True)
class BundleVersions:
    """The app's two version strings: CFBundleShortVersionString and CFBundleVersion."""

    short: str
    build: str


def bundle_versions(version: str, tag: str | None, build_number: str | None) -> BundleVersions:
    """The version rule (0002 §7). A build from a ``v*`` tag passes ``tag``: the tag must be
    ``v`` and the host's ``__version__``, which must be three numbers and nothing else (no
    ``.devN``, ``rcN``, ``.postN`` or ``+local``), and both strings are that version. Any other
    build passes ``build_number`` (CI's run number): the short version is the release part of
    ``__version__`` and the bundle version is that number. Exactly one of the two is given."""
    if tag is not None and build_number is not None:
        raise BuildError("pass --tag or --build-number, not both")
    if tag is not None:
        if _FINAL_RELEASE.fullmatch(version) is None:
            raise BuildError(
                "a build from a tag needs a release version of three numbers with no suffix; "
                f"__version__ is {version!r}"
            )
        if tag != f"v{version}":
            raise BuildError(f"the tag is {tag!r}, but __version__ is {version!r} (tag v{version})")
        return BundleVersions(version, version)
    if build_number is None:
        raise BuildError("pass --build-number (CI passes the run number) or --tag")
    if _BUILD_NUMBER.fullmatch(build_number) is None:
        raise BuildError(f"the build number must be a positive whole number, not {build_number!r}")
    return BundleVersions(release_triple(version), build_number)


def folder_usage(app_name: str) -> str:
    """What the bundle's Info.plist says for macOS to show when it asks the user about a protected
    folder: what the app is for, in the sentences the app opens with (HostText.appPurpose). The
    second sentence is there because host.describe is on by default. macOS is meant to show this
    under its own question; whether it does, and whether that question names the app or the
    interpreter, is not yet tested on a Mac (macos/README.md, "What CI does not check")."""
    return (
        f"{app_name} lets Lucille use only the folders you choose on this Mac, and only for the "
        "abilities you turn on. Lucille can also see which abilities are on, and the names you "
        "gave your folders."
    )


def info_plist(pins: Pins, versions: BundleVersions) -> dict[str, Any]:
    """The app's Info.plist (0002 §6-§12): a menu bar app with no Dock icon, for Apple silicon on
    the pinned macOS or later, that macOS must not quit on its own, with the six folder usage
    texts. Its icon is ``Resources/AppIcon.icns``, named by ``CFBundleIconFile`` alone: the app has
    no asset catalog for ``CFBundleIconName`` to name an asset in. MANIFEST.json, not this file,
    records the host's exact version."""
    usage = dict.fromkeys(FOLDER_USAGE_KEYS, folder_usage(pins.app_name))
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": pins.app_name,
        "CFBundleExecutable": pins.app_name,
        "CFBundleIconFile": icons.APP_ICON,
        "CFBundleIdentifier": pins.bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": pins.app_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": versions.short,
        "CFBundleVersion": versions.build,
        "LSApplicationCategoryType": pins.category,
        "LSArchitecturePriority": ["arm64"],
        "LSMinimumSystemVersion": pins.deployment_target,
        "LSUIElement": True,
        "NSHumanReadableCopyright": "MIT License",
        "NSSupportsAutomaticTermination": False,
        "NSSupportsSuddenTermination": False,
        **usage,
    }


def dmg_name(app_name: str, version: str, commit: str) -> str:
    """The disk image's file name. It names the commit it was built from and says that its
    signature is ad hoc, so that a build from CI cannot be taken for a release asset."""
    if _COMMIT.fullmatch(commit) is None:
        raise BuildError(f"{commit!r} is not an abbreviated commit id")
    return f"{app_name}-{version}-{commit}{DMG_SUFFIX}"


def disk_images(out: Path, app_name: str) -> list[Path]:
    """The disk images this script has made in ``out``."""
    return sorted(out.glob(f"{glob.escape(app_name)}-*{DMG_SUFFIX}"))


def unsigned_tarball_name(app_name: str) -> str:
    return f"{app_name}{UNSIGNED_SUFFIX}"


def _normal_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_build_constraints(text: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """``{name: (version, sha256s)}`` for each requirement of build-constraints.txt. Each one is
    ``name==version`` with at least one ``--hash=sha256:...``, and no name appears twice."""
    found: dict[str, tuple[str, tuple[str, ...]]] = {}
    for raw in text.replace("\\\n", " ").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        head, *options = line.split()
        match = _EXACT.fullmatch(head)
        if match is None:
            raise PinError(f"build-constraints.txt: {head!r} is not name==version")
        hashes = []
        for option in options:
            digest = option.removeprefix("--hash=sha256:")
            if digest == option or _SHA256.fullmatch(digest) is None:
                raise PinError(f"build-constraints.txt: {option!r} is not --hash=sha256:<hex>")
            hashes.append(digest)
        if not hashes:
            raise PinError(f"build-constraints.txt: {head} has no --hash")
        name = _normal_name(match["name"])
        if name in found:
            raise PinError(f"build-constraints.txt: {name} appears twice")
        found[name] = (match["version"], tuple(hashes))
    return found


def build_constraint_problems(
    requires: Sequence[str], constraints: Mapping[str, tuple[str, tuple[str, ...]]]
) -> list[str]:
    """How the hashed build constraints differ from pyproject.toml's ``[build-system] requires``:
    each requirement there must be exact, and the two must name the same versions."""
    problems = []
    wanted: dict[str, str] = {}
    for requirement in requires:
        match = _EXACT.fullmatch(requirement.strip())
        if match is None:
            problems.append(f"the build requirement {requirement!r} is not pinned with ==")
            continue
        wanted[_normal_name(match["name"])] = match["version"]
    for name, version in sorted(wanted.items()):
        if name not in constraints:
            problems.append(f"{name}=={version} has no line in build-constraints.txt")
        elif constraints[name][0] != version:
            problems.append(
                f"build-constraints.txt pins {name}=={constraints[name][0]}, but pyproject.toml "
                f"requires {name}=={version}"
            )
    for name in sorted(set(constraints) - set(wanted)):
        problems.append(f"build-constraints.txt pins {name}, which pyproject.toml does not require")
    return problems


def load_build_constraints(
    constraints_file: Path = BUILD_CONSTRAINTS, pyproject: Path = PYPROJECT
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """The hashed build constraints, checked against pyproject.toml."""
    constraints = parse_build_constraints(constraints_file.read_text(encoding="utf-8"))
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requires = data.get("build-system", {}).get("requires", [])
    problems = build_constraint_problems(requires, constraints)
    if problems:
        raise PinError(
            "the build constraints do not match pyproject.toml:\n  " + "\n  ".join(problems)
        )
    return constraints


def parse_prune_list(text: str) -> tuple[str, ...]:
    """The paths in ``text`` (the contents of prune.txt), relative to the runtime's top folder,
    in order. A line that is empty or starts with ``#`` is skipped. Refused: a pattern (``*``,
    ``?``, ``[``), an absolute path, ``.`` or ``..``, a backslash or ``#`` inside a path, a path
    given twice, the interpreter or a folder that holds it or ``site-packages``, a
    ``*.dist-info`` folder or anything in one, and anything in ``site-packages`` but its
    ``README.txt``."""
    found: list[str] = []
    keep = (UPSTREAM_INTERPRETER, SITE_PACKAGES)
    for number, raw in enumerate(text.splitlines(), start=1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        where = f"prune.txt:{number}: {entry!r}"
        if any(char in entry for char in "*?[]\\#"):
            raise PinError(f"{where}: one literal path per line, with no pattern or comment")
        parts = entry.split("/")
        if entry.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise PinError(f"{where}: a path relative to the runtime's top folder, with no . or ..")
        if entry in found:
            raise PinError(f"{where}: listed twice")
        if any(path == entry or path.startswith(entry + "/") for path in keep):
            raise PinError(f"{where}: the build keeps this, or a folder of what it keeps")
        if any(part.endswith(".dist-info") for part in parts):
            raise PinError(f"{where}: a *.dist-info folder is never pruned")
        if entry.startswith(SITE_PACKAGES + "/") and entry != f"{SITE_PACKAGES}/README.txt":
            raise PinError(f"{where}: site-packages is emptied by uninstalling, not by pruning")
        found.append(entry)
    return tuple(found)


def load_prune_list(path: Path = PRUNE_FILE) -> tuple[str, ...]:
    return parse_prune_list(path.read_text(encoding="utf-8"))


def sha256_of(path: Path) -> str:
    return payload.sha256_of(path)


def check_sha256(path: Path, expected: str) -> None:
    """Refuse a file whose sha256 is not the pinned one."""
    actual = sha256_of(path)
    if actual != expected:
        raise BuildError(
            f"{path.name}: its sha256 is {actual}, but {_pins_name()} pins {expected}; "
            "refusing to build"
        )


def check_host_wheel(path: Path, expected: str, version: str) -> None:
    """Refuse a host wheel that is not this version's, or whose sha256 is not ``expected``."""
    name = payload.host_wheel_name(version)
    if path.name != name or not path.is_file():
        raise BuildError(f"the host wheel must be a file named {name}; refusing to build")
    if _SHA256.fullmatch(expected) is None:
        raise BuildError("--host-wheel-sha256 must be 64 lower-case hexadecimal digits")
    actual = sha256_of(path)
    if actual != expected:
        raise BuildError(
            f"{path.name}: its sha256 is {actual}, not the {expected} given; refusing to build"
        )


def fetch_runtime(pins: Pins, cache: Path, archive: Path | None = None) -> Path:
    """The pinned runtime archive: ``archive`` if given, else a download into ``cache``. Either
    way it is used only if its sha256 matches the pin; a download that does not is deleted."""
    if archive is not None:
        check_sha256(archive, pins.sha256)
        return archive
    target = cache / pins.asset
    if not target.exists():
        cache.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        print(f"downloading {pins.url}", flush=True)
        with urllib.request.urlopen(pins.url, timeout=300) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        partial.replace(target)
    try:
        check_sha256(target, pins.sha256)
    except BuildError:
        target.unlink()
        raise
    return target


def is_macho(path: Path) -> bool:
    """Whether ``path`` is a regular file that starts like a Mach-O file."""
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in _MACHO_MAGIC


def macho_files(root: Path) -> list[Path]:
    """Every Mach-O file under ``root``, the most deeply nested first: the order to sign them."""
    found = [path for path in root.rglob("*") if is_macho(path)]
    return sorted(found, key=lambda path: (-len(path.parts), str(path)))


def symlinks_in(root: Path) -> list[Path]:
    """Every symbolic link under ``root``, without following any."""
    links = []
    for folder, dirs, files in os.walk(root):
        links.extend(
            Path(folder) / name for name in (*dirs, *files) if (Path(folder) / name).is_symlink()
        )
    return sorted(links)


def bytecode_in(root: Path) -> list[Path]:
    """Every ``.pyc`` or ``.pyo`` file and every ``__pycache__`` folder under ``root``."""
    found = []
    for folder, dirs, files in os.walk(root):
        found += [Path(folder) / name for name in dirs if name == "__pycache__"]
        found += [Path(folder) / name for name in files if name.endswith((".pyc", ".pyo"))]
    return sorted(found)


def bytecode_argv(runtime: Path, mode: str) -> list[str]:
    """The command that runs :data:`BYTECODE_PROGRAM` with the runtime's own interpreter, on its
    ``lib/python3.13``: ``mode`` is ``write`` or ``check``."""
    return [
        str(runtime / INTERPRETER),
        *BYTECODE_FLAGS,
        "-c",
        BYTECODE_PROGRAM,
        mode,
        str(runtime),
        STDLIB,
    ]


def run_bytecode_program(runtime: Path, mode: str) -> subprocess.CompletedProcess[str]:
    """Run :func:`bytecode_argv` from ``/``, with :data:`BYTECODE_ENV`, its output captured as
    text. It prints the command with the program's name in place of its text."""
    argv = bytecode_argv(runtime, mode)
    shown = [argv[0], *BYTECODE_FLAGS, "-c", "BYTECODE_PROGRAM", mode, str(runtime), STDLIB]
    print("+ " + shlex.join(shown), flush=True)
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=dict(BYTECODE_ENV),
            cwd="/",
            timeout=BYTECODE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"the bytecode program did not finish within {BYTECODE_TIMEOUT} s"
        ) from exc


def bytecode_layout_problems(app: Path, tag: str = BYTECODE_TAG) -> list[str]:
    """Where the app's bytecode differs from what the build writes, from the names alone and the
    first 16 bytes of each ``.pyc``: one ``<name>.<tag>.pyc`` in ``__pycache__`` beside each
    ``.py`` of the runtime's library, checked-hash, and no other bytecode anywhere in the app.
    Paths are from the runtime inside it, and from the app outside it. What each ``.pyc`` holds is
    :func:`bytecode_problems`' to judge."""
    runtime = app / RUNTIME_DIR
    library = runtime / STDLIB
    suffix = f".{tag}.pyc"

    def shown(path: Path) -> str:
        return path.relative_to(runtime if runtime in path.parents else app).as_posix()

    problems = []
    for path in bytecode_in(app):
        if library not in path.parents:
            problems.append(f"bytecode outside the runtime's library: {shown(path)}")
        elif "__pycache__" not in (path.name, path.parent.name):
            problems.append(f"bytecode outside a __pycache__ folder: {shown(path)}")
    for folder, dirs, files in os.walk(library):
        here = Path(folder)
        dirs.sort()
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
            cache = here / "__pycache__"
            entries = [] if cache.is_symlink() else sorted(cache.iterdir())
            if cache.is_symlink():
                problems.append(
                    f"{shown(cache)} is a symbolic link, not the folder the build writes"
                )
            elif not entries:
                problems.append(f"an empty __pycache__ folder: {shown(cache)}")
            for path in entries:
                name = path.name
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not name.endswith(suffix)
                    or name == suffix
                ):
                    problems.append(
                        f"{shown(path)} is not a file the build writes: only "
                        f"<name>.{tag}.pyc goes in __pycache__"
                    )
                    continue
                source = here / (name.removesuffix(suffix) + ".py")
                if not source.is_file():
                    problems.append(f"{shown(path)} has no source: there is no {shown(source)}")
                    continue
                with path.open("rb") as handle:
                    head = handle.read(16)
                flags = int.from_bytes(head[4:8], "little")
                if len(head) < 16:
                    problems.append(f"{shown(path)} is too short to be a .pyc")
                elif flags != CHECKED_HASH:
                    problems.append(f"{shown(path)} is not checked-hash: its flags are {flags}")
        for name in sorted(files):
            if name.endswith(".py"):
                pyc = here / "__pycache__" / (name.removesuffix(".py") + suffix)
                if not pyc.is_file():
                    problems.append(f"{shown(here / name)} has no .pyc: there is no {shown(pyc)}")
    return problems


# The last line of a check run of BYTECODE_PROGRAM: how many .pyc files matched, of how many .py.
_CHECKED_COUNT: Final = re.compile(r"found the same bytes in (\d+) of (\d+) \.pyc files, (\S+)")


def bytecode_check_problems(done: subprocess.CompletedProcess[str], tag: str) -> list[str]:
    """What a run of :data:`BYTECODE_PROGRAM` in ``check`` mode says is wrong. It ends with its
    count, for ``tag``: exit 0 with only that line, every file matched, is nothing wrong, unless
    it counts no file at all ("0 of 0"): an app with no ``.py`` in its library, or a check that
    found none of it, would otherwise pass. Exit 1 is the lines before it, one for each file it
    did not match. Any other ending is a problem of its own, an exception among them: Python then
    exits 1 too, with nothing on stdout."""
    lines = done.stdout.splitlines()
    count = _CHECKED_COUNT.fullmatch(lines[-1]) if lines else None
    if count is not None and count[3] == tag:
        found = lines[:-1]
        matched, total = int(count[1]), int(count[2])
        if done.returncode == 0 and not found and total == 0:
            return [f"the bytecode check counted no .py file: {lines[-1]}"]
        if done.returncode == 0 and not found and matched == total:
            return []
        if done.returncode == 1 and found and matched + len(found) == total:
            return found
    said = (done.stdout + done.stderr).strip()
    return [f"the bytecode check did not run (exit {done.returncode}): {said}"]


def bytecode_problems(app: Path, tag: str = BYTECODE_TAG) -> list[str]:
    """:func:`bytecode_layout_problems`, then what the bundled interpreter finds when it checks
    each ``.pyc`` (:data:`BYTECODE_PROGRAM` in ``check`` mode): its magic number, its flags, its
    source's hash, and its bytes against what it compiles from that source now
    (:func:`bytecode_check_problems`). It prints the check run's count."""
    problems = bytecode_layout_problems(app, tag)
    done = run_bytecode_program(app / RUNTIME_DIR, "check")
    lines = done.stdout.splitlines()
    if lines:
        print(lines[-1], flush=True)
    return problems + bytecode_check_problems(done, tag)


def require_bytecode(app: Path, tag: str = BYTECODE_TAG) -> None:
    """Refuse an app whose bytecode :func:`bytecode_problems` finds anything wrong with."""
    problems = bytecode_problems(app, tag)
    if problems:
        raise BuildError(
            "the .pyc files are not the ones the build writes:\n  " + "\n  ".join(problems)
        )


def compile_bytecode(app: Path, tag: str = BYTECODE_TAG) -> None:
    """Write the app's ``.pyc`` files with the bundled interpreter (:data:`BYTECODE_PROGRAM` in
    ``write`` mode), into a runtime that holds no bytecode yet; then check them with a second,
    fresh run of the same program (:func:`require_bytecode`). So if the interpreter does not write
    the same bytes twice, the build stops here, before anything is signed."""
    runtime = app / RUNTIME_DIR
    held = bytecode_in(runtime)
    if held:
        shown = ", ".join(path.relative_to(runtime).as_posix() for path in held[:10])
        raise BuildError(f"the runtime already holds bytecode: {shown}")
    done = run_bytecode_program(runtime, "write")
    if done.returncode != 0:
        said = (done.stdout + done.stderr).strip()
        raise BuildError(f"the bytecode program failed (exit {done.returncode}):\n{said}")
    print(done.stdout, end="", flush=True)
    require_bytecode(app, tag)


def _run(argv: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> None:
    print("+ " + shlex.join(argv), flush=True)
    subprocess.run(argv, check=True, cwd=cwd, env=None if env is None else dict(env))


# hdiutil sometimes refuses a call with "Resource busy" or "Resource temporarily unavailable"
# (EAGAIN, which hdiutil(1) ties to DiskImages' lock on an image file). A call refused so is made
# again after each of HDIUTIL_WAITS, two minutes in all, unless its caller passes other waits. In
# this repository's logs (149 macOS jobs, 2026-10-29 to 2026-11-09) that has never turned a
# refusal into a pass. The three refusals were of the `hdiutil verify` run right after `hdiutil
# create`. Every try the job made of that file was refused. The logs show a diskimages-helper still
# alive at the job's cleanup; that it was alive during the tries, and that the job's create started
# it, is inferred. So make_dmg never relies on a verify of the file create wrote, and never hands
# that file on: it verifies a copy. (It runs `hdiutil verify -debug` on that file only to print,
# for the log, why it is held.)
HDIUTIL_BUSY: Final = ("Resource busy", "Resource temporarily unavailable")
HDIUTIL_WAITS: Final = (5, 10, 15, 20, 30, 40)
HDIUTIL_TRIES: Final = len(HDIUTIL_WAITS) + 1
# Every UDIF disk image ends in a 512-byte trailer that starts "koly" and says where the data fork
# and the XML property list (the chunk table and its checksums) lie.
UDIF_TRAILER: Final = 512
UDIF_MAGIC: Final = b"koly"
# What show_image_holders runs is cut off after DIAGNOSIS_TIMEOUT seconds, and what it prints of
# each after its last DIAGNOSIS_LINES lines. Of ps, it shows only the processes whose executable's
# name matches HOLDER_NAMES, with their own command lines: the job log of a public repository is
# public, and another process's arguments may hold a secret.
DIAGNOSIS_TIMEOUT: Final = 30
DIAGNOSIS_LINES: Final = 60
HOLDER_NAMES: Final = re.compile(r"diskimage|hdiutil|xprotect|^mds|^mdworker", re.IGNORECASE)
# A line of `hdiutil verify -debug` that names a lock: "lock" anywhere, "OpenLockFriendly", "flock"
# and "unlock" included, but not as the end of "block" or "clock".
LOCK_WORD: Final = re.compile(r"(?<![bc])lock", re.IGNORECASE)


def hdiutil_busy(done: subprocess.CompletedProcess[str]) -> bool:
    """Whether ``hdiutil`` refused a call as busy."""
    return done.returncode != 0 and any(word in done.stderr for word in HDIUTIL_BUSY)


def hdiutil(
    args: Sequence[str],
    *,
    waits: Sequence[float] = HDIUTIL_WAITS,
    timeout: float | None = None,
    sleep: Any = time.sleep,
) -> subprocess.CompletedProcess[str]:
    """Run ``hdiutil`` with ``args`` and return how it ended, output captured as text. A call
    refused as busy is made again after each of ``waits`` in turn; any other failure is returned
    at once. Each wait is said, and flushed, before it starts, so the log's times are true.
    ``timeout`` is subprocess.run's own, and raises as it does."""
    argv = ["hdiutil", *args]
    tries = len(waits) + 1
    for attempt in range(1, tries + 1):
        print("+ " + shlex.join(argv), flush=True)
        done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
        if not hdiutil_busy(done) or attempt == tries:
            return done
        said = done.stderr.strip()
        wait = waits[attempt - 1]
        print(
            f"hdiutil is busy; trying again in {wait} s (try {attempt} of {tries}): {said}",
            flush=True,
        )
        sleep(wait)
    raise AssertionError("unreachable")


def image_lock_held(path: Path) -> bool | OSError:
    """Whether another process holds a flock(2) lock on ``path``, shared or exclusive: True when an
    exclusive lock is refused at once, False when it is granted (and given back at once), and the
    error when it could not be asked for. It asks for an exclusive lock because a shared one is
    granted beside another shared one."""
    import fcntl  # here, so that the commands that run anywhere import anywhere

    try:
        with path.open("rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False  # closing the file gives the lock back
    except OSError as error:
        return error


def udif_trailer_problem(path: Path) -> str | None:
    """None when ``path`` ends in a UDIF trailer whose data fork and property list both lie in the
    file before it; otherwise what is wrong. A disk image cut short has no such ending."""
    size = path.stat().st_size
    if size < UDIF_TRAILER:
        return f"it is {size} bytes, too short to end in a UDIF trailer of {UDIF_TRAILER}"
    with path.open("rb") as handle:
        handle.seek(size - UDIF_TRAILER)
        trailer = handle.read(UDIF_TRAILER)
    if trailer[:4] != UDIF_MAGIC:
        return f"it does not end in a UDIF trailer: its last {UDIF_TRAILER} bytes do not start koly"
    (header,) = struct.unpack(">I", trailer[8:12])
    data_offset, data_length = struct.unpack(">QQ", trailer[0x18:0x28])
    xml_offset, xml_length = struct.unpack(">QQ", trailer[0xD8:0xE8])
    end = size - UDIF_TRAILER
    if header != UDIF_TRAILER:
        return f"its UDIF trailer says it is {header} bytes, not {UDIF_TRAILER}"
    if data_offset + data_length > end:
        return (
            f"its UDIF trailer puts the end of the data fork at byte {data_offset + data_length}, "
            f"past the trailer's start at {end}"
        )
    if xml_length == 0:
        return "its UDIF trailer gives the property list no length"
    if xml_offset + xml_length > end:
        return (
            f"its UDIF trailer puts the property list ({xml_length} bytes at {xml_offset}) past "
            f"the trailer's start at {end}"
        )
    return None


def _tail(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= DIAGNOSIS_LINES:
        return text
    cut = len(lines) - DIAGNOSIS_LINES
    return "\n".join([f"({cut} lines before these are not shown)", *lines[-DIAGNOSIS_LINES:]])


def _look(program: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run ``program`` with ``args`` for show_image_holders, within DIAGNOSIS_TIMEOUT; hdiutil
    through :func:`hdiutil`, once. One line says so when it could not be run, did not end in time
    (both give None) or exited non-zero."""
    what = f"{program} {args[0]}" if program == "hdiutil" else program
    try:
        if program == "hdiutil":
            done = hdiutil(args, waits=(), timeout=DIAGNOSIS_TIMEOUT)
        else:
            argv = [program, *args]
            print("+ " + shlex.join(argv), flush=True)
            done = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=DIAGNOSIS_TIMEOUT
            )
    except subprocess.TimeoutExpired:
        print(f"({what} did not end within {DIAGNOSIS_TIMEOUT} s)", flush=True)
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"({what} could not be run: {error})", flush=True)
        return None
    if done.returncode != 0:
        print(f"({what} exited {done.returncode})", flush=True)
    return done


def _show(done: subprocess.CompletedProcess[str] | None, *, only_locks: bool = False) -> None:
    if done is None:
        return
    lines = (done.stdout + done.stderr).strip().splitlines()
    if only_locks:  # the lines that name a lock (LOCK_WORD), and the last line
        lines = [line for line in lines[:-1] if LOCK_WORD.search(line)] + lines[-1:]
    print(_tail("\n".join(lines)) if lines else "(no output)", flush=True)


def show_image_holders(image: Path) -> None:
    """Print, for the log, what may be holding ``image``: the disk-image, XProtect and Spotlight
    processes, with their parents, start times and command lines; what has the file open, also as
    root under ``sudo -n`` on GitHub Actions only, never on a developer's own Mac; the images
    attached; the lines of ``hdiutil verify -debug`` that name a lock (:data:`LOCK_WORD`), and its
    last line; and the disk-image log of the last three minutes. Never fails the build."""
    print(f"what may be holding {image}:", flush=True)
    listed = _look("ps", ["-axo", "pid=,comm="])
    pids = []
    if listed is not None and listed.returncode == 0:
        for line in listed.stdout.splitlines():
            pid, _, executable = line.strip().partition(" ")
            if pid.isdigit() and HOLDER_NAMES.search(Path(executable.strip()).name):
                pids.append(pid)
        if not pids:
            print("(no disk-image, XProtect or Spotlight process is running)", flush=True)
    if pids:
        _show(_look("ps", ["-o", "pid,ppid,user,lstart,etime,stat,command", "-p", ",".join(pids)]))
    _show(_look("lsof", ["--", str(image)]))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        _show(_look("sudo", ["-n", "lsof", "--", str(image)]))
    _show(_look("hdiutil", ["info"]))
    _show(_look("hdiutil", ["verify", "-debug", str(image)]), only_locks=True)
    predicate = 'subsystem == "com.apple.diskimages"'
    _show(
        _look(
            "log",
            ["show", "--last", "3m", "--style", "compact", "--info", "--debug"]
            + ["--predicate", predicate],
        )
    )


def _annotate(message: str) -> None:
    """On GitHub Actions only, print ``message`` as a warning annotation, which the run's page
    shows: a line in a build log of thousands is read by nobody. ``message`` is one line of this
    script's own words, with no ``%``, so it needs no escaping."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=disk image::{message}", flush=True)


def _output(
    argv: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> str:
    done = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=None if env is None else dict(env),
    )
    return done.stdout.strip()


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def build_wheel_argv(out: Path) -> list[str]:
    """``uv build`` for the host's wheel: its build backend comes only from the files
    build-constraints.txt names by hash. No uv configuration file is read, since one could
    turn build isolation off; :func:`build_wheel` removes the ``UV_*`` variables too."""
    return [
        "uv",
        "build",
        "--wheel",
        "--build-constraints",
        str(BUILD_CONSTRAINTS),
        "--require-hashes",
        "--no-config",
        "--out-dir",
        str(out),
        str(REPO_ROOT),
    ]


def build_wheel(out: Path, version: str) -> Path:
    """The host's wheel, built from this checkout into ``out``, with no uv setting from the
    environment (:func:`payload.without_uv_settings`). Any older host wheel there is removed
    first, so that ``out`` then holds exactly one."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("lucille_host-*.whl"):
        old.unlink()
    _run(build_wheel_argv(out), env=payload.without_uv_settings(os.environ))
    wheels = sorted(out.glob("*.whl"))
    expected = payload.host_wheel_name(version)
    if [wheel.name for wheel in wheels] != [expected]:
        raise BuildError(f"expected exactly {expected} from uv build, got {wheels}")
    return wheels[0]


def export_requirements_argv() -> list[str]:
    """``uv export`` of the runtime dependencies uv.lock pins, with every hash, to stdout. No
    uv configuration file is read."""
    return [
        "uv",
        "export",
        "--no-config",
        "--frozen",
        "--no-dev",
        "--no-editable",
        "--no-emit-project",
        "--format",
        "requirements-txt",
    ]


def export_requirements_text() -> str:
    """The runtime dependencies uv.lock pins, one per line with their markers and hashes."""
    print("+ " + shlex.join(export_requirements_argv()), flush=True)
    env = payload.without_uv_settings(os.environ)
    return _output(export_requirements_argv(), cwd=REPO_ROOT, env=env) + "\n"


def unpack_runtime(archive: Path, contents: Path) -> Path:
    """Unpack the runtime into ``Contents/Resources/runtime``; returns that folder."""
    resources = contents / "Resources"
    staging = resources / "unpack"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(staging, filter="data")
    top = staging / "python"
    if not (top / UPSTREAM_INTERPRETER).is_file():
        raise BuildError(f"{archive.name} has no python/{UPSTREAM_INTERPRETER}")
    runtime = contents / RUNTIME_DIR.removeprefix("Contents/")
    top.rename(runtime)
    staging.rmdir()
    return runtime


def prune_runtime(runtime: Path, entries: Sequence[str]) -> list[str]:
    """Remove each path of the prune list from the runtime. Every one must be there: a list that
    names something the pinned runtime does not hold is out of date, and refused."""
    for entry in entries:
        path = runtime / entry
        if not (path.is_symlink() or path.exists()):
            raise BuildError(f"prune.txt names {entry}, which the pinned runtime does not hold")
    for entry in entries:
        _remove(runtime / entry)
    return list(entries)


def replace_symlinks(runtime: Path) -> list[str]:
    """Replace each symbolic link in the runtime that points to a regular file inside it with a
    copy of that file, mode included. Any other link (to a folder, outside the runtime, or to
    nothing) is refused. Returns the links replaced."""
    top = Path(os.path.realpath(runtime))
    replaced = []
    for link in symlinks_in(runtime):
        target = Path(os.path.realpath(link))
        if top not in target.parents or not target.is_file():
            raise BuildError(
                f"{link.relative_to(runtime)} links to {os.readlink(link)}, which is not a file "
                "inside the runtime"
            )
        link.unlink()
        shutil.copyfile(target, link)
        shutil.copymode(target, link)
        replaced.append(link.relative_to(runtime).as_posix())
    return replaced


def runtime_shape_problems(runtime: Path) -> list[str]:
    """How the prepared runtime differs from what the app ships: ``bin/`` holds only the renamed
    interpreter, ``lib/`` only ``python3.13/``, ``site-packages`` is empty, and there is no other
    top-level entry, no symbolic link and no ``.pyc``. Anything the prune list missed is reported
    here, not deleted."""
    problems = []

    def names(folder: Path) -> set[str]:
        return {entry.name for entry in folder.iterdir()} if folder.is_dir() else set()

    if names(runtime) != {"bin", "lib"}:
        problems.append(f"the runtime's top folder holds {sorted(names(runtime))}, not bin and lib")
    if names(runtime / "bin") != {Path(INTERPRETER).name}:
        problems.append(f"bin/ holds {sorted(names(runtime / 'bin'))}, not only the interpreter")
    interpreter = runtime / INTERPRETER
    if interpreter.is_symlink() or not interpreter.is_file():
        problems.append(f"{INTERPRETER} is not a file")
    if names(runtime / "lib") != {"python3.13"}:
        problems.append(f"lib/ holds {sorted(names(runtime / 'lib'))}, not only python3.13")
    site = runtime / SITE_PACKAGES
    if not site.is_dir():
        problems.append(f"{SITE_PACKAGES} is missing")
    elif names(site):
        problems.append(f"{SITE_PACKAGES} is not empty: {sorted(names(site))}")
    problems += [f"symbolic link: {link.relative_to(runtime)}" for link in symlinks_in(runtime)]
    problems += [f"bytecode: {path.relative_to(runtime)}" for path in bytecode_in(runtime)]
    return problems


def prepare_runtime(runtime: Path, entries: Sequence[str]) -> list[str]:
    """Make the unpacked runtime the app's: uninstall the runtime's own distributions
    (:data:`RUNTIME_DISTRIBUTIONS_REMOVED`, pip) by their ``RECORD``, remove the prune list's
    paths, rename the interpreter ``lucille-host-runtime``, replace each symbolic link by its
    target, and refuse unless :func:`runtime_shape_problems` finds nothing. Returns what was
    removed, sorted: each uninstalled distribution as ``name==version``, and each pruned path."""
    site = runtime / SITE_PACKAGES
    removed = []
    for name in RUNTIME_DISTRIBUTIONS_REMOVED:
        try:
            removed.append(payload.uninstall_by_record(runtime, site, name))
        except payload.AssemblyError as exc:
            raise BuildError(str(exc)) from exc
    removed += prune_runtime(runtime, entries)
    (runtime / UPSTREAM_INTERPRETER).rename(runtime / INTERPRETER)
    replace_symlinks(runtime)
    problems = runtime_shape_problems(runtime)
    if problems:
        raise BuildError("the runtime is not ready for the app:\n  " + "\n  ".join(problems))
    return sorted(removed)


def developer_dir(pins: Pins) -> str:
    """Where GitHub's macOS images keep the pinned Xcode's developer folder."""
    path = f"/Applications/Xcode_{pins.xcode}.app/Contents/Developer"
    if _DEVELOPER_DIR.fullmatch(path) is None:
        raise PinError(f"{path!r} is not an Xcode developer folder")
    return path


def toolchain_problems(pins: Pins, xcodebuild_version: str) -> list[str]:
    """What is wrong with the Xcode that ``xcodebuild -version`` described: its first line must be
    ``Xcode`` and the pinned version."""
    lines = xcodebuild_version.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if first != f"Xcode {pins.xcode}":
        return [
            f"xcodebuild runs {first or 'nothing'!r}, but {_pins_name()} pins Xcode {pins.xcode}"
        ]
    return []


def xcode_env(pins: Pins, environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` with ``DEVELOPER_DIR`` set: the one it already names (a local build's own
    choice), or the pinned Xcode's. Refused unless ``xcodebuild -version``, run with that
    environment, reports the pinned version."""
    env = dict(environ)
    env["DEVELOPER_DIR"] = environ.get("DEVELOPER_DIR") or developer_dir(pins)
    text = _output(["xcodebuild", "-version"], env=env)
    problems = toolchain_problems(pins, text)
    if problems:
        raise BuildError("; ".join(problems))
    print(f"Xcode {pins.xcode} at {env['DEVELOPER_DIR']}")
    return env


def require_apple_silicon(what: str) -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise BuildError(f"{what} runs on an Apple silicon Mac; `plan` runs anywhere")


def select_toolchain(pins: Pins, github_env: Path | None) -> None:
    """Check that the pinned Xcode is installed and is the one ``xcodebuild`` runs with its
    ``DEVELOPER_DIR``, and append that ``DEVELOPER_DIR`` to ``github_env`` (``$GITHUB_ENV``) for
    the job's later steps."""
    require_apple_silicon("toolchain")
    path = developer_dir(pins)
    if not Path(path).is_dir():
        raise BuildError(f"{path} does not exist: Xcode {pins.xcode} is not installed")
    environ = {key: value for key, value in os.environ.items() if key != "DEVELOPER_DIR"}
    xcode_env(pins, {**environ, "DEVELOPER_DIR": path})
    if github_env is not None:
        line = f"DEVELOPER_DIR={path}"
        if re.fullmatch(r"DEVELOPER_DIR=" + _DEVELOPER_DIR.pattern, line) is None:
            raise BuildError(f"refusing to write {line!r}")
        with github_env.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(f"wrote {line} to {github_env}")


def compile_app(executable: Path, deployment_target: str, env: Mapping[str, str]) -> None:
    sources = sorted(str(path) for path in SWIFT_DIR.glob("*.swift"))
    if not sources:
        raise BuildError(f"no Swift sources in {SWIFT_DIR}")
    _run(
        [
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-O",
            "-target",
            f"arm64-apple-macos{deployment_target}",
            "-o",
            str(executable),
            *sources,
        ],
        env=env,
    )


def signing_identifier(app: Path, path: Path, pins: Pins) -> str:
    """The signing identifier of a Mach-O file in the app: the pin's runtime identifier for the
    interpreter; for any other file, the bundle identifier, a dot, and the file's path (from the
    runtime's ``lib/python3.13/`` if it is there, else from ``Contents/``) with ``/`` as ``.``,
    without a last ``.so`` or ``.dylib``, and any character but letters, digits, ``.``, ``_`` and
    ``-`` as ``-``. codesign's ``--prefix`` is not used: it adds the prefix only to a derived
    identifier with no dot in it, and ``_rust.abi3`` has one."""
    if path == app / RUNTIME_IN_APP:
        return pins.runtime_id
    stdlib = app / RUNTIME_DIR / STDLIB
    base = stdlib if stdlib in path.parents else app / "Contents"
    parts = list(path.relative_to(base).parts)
    if path.suffix in (".so", ".dylib"):
        parts[-1] = parts[-1].removesuffix(path.suffix)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", ".".join(parts))
    return f"{pins.bundle_id}.{name}"


def signing_plan(app: Path, pins: Pins) -> list[list[str]]:
    """The ``codesign`` calls that sign the app ad hoc, in order (0002 §7): every Mach-O file but
    the app's executable and the interpreter, the most deeply nested first, each with its own
    identifier; then the interpreter, with the pin's runtime identifier; then the bundle, which
    signs its executable. ``--options runtime`` (the hardened runtime) on every call or on none,
    as the pins say. Never ``--deep``, no entitlements, no timestamp."""
    main = app / "Contents" / "MacOS" / pins.app_name
    interpreter = app / RUNTIME_IN_APP
    options = ["--options", "runtime"] if pins.adhoc_hardened_runtime else []
    common = ["codesign", "--force", *options, "--timestamp=none", "--sign", "-"]
    plan = []
    for path in macho_files(app):
        if path not in (main, interpreter):
            plan.append([*common, "--identifier", signing_identifier(app, path, pins), str(path)])
    plan.append([*common, "--identifier", pins.runtime_id, str(interpreter)])
    plan.append([*common, str(app)])
    return plan


def sign_app(app: Path, pins: Pins) -> None:
    """Sign the app as :func:`signing_plan` says, then verify the signature, deep and strict."""
    for argv in signing_plan(app, pins):
        _run(argv)
    _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])


def write_wrapper(app: Path) -> Path:
    """The three-line ``sh`` wrapper for Terminal users, at ``Resources/bin/lucille-host``."""
    wrapper = app / WRAPPER_IN_APP
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(WRAPPER_TEXT, encoding="ascii")
    wrapper.chmod(0o755)
    return wrapper


def copy_licenses(dest: Path, site: Path, runtime_license: Path) -> list[str]:
    """Copy the licence texts into ``dest``, one folder per distribution: ``CPython`` for the
    runtime's ``LICENSE.txt``, and each installed distribution's own files
    (:func:`payload.license_files`), by their path in its ``licenses/`` folder or their name.
    Returns the files written, relative to ``dest``."""
    copies = {f"CPython/{runtime_license.name}": runtime_license}
    for name, files in sorted(payload.license_files(site).items()):
        for path in files:
            info = next(parent for parent in path.parents if parent.name.endswith(".dist-info"))
            rel = path.relative_to(info)
            if rel.parts[0] == "licenses":
                rel = rel.relative_to("licenses")
            copies[f"{name}/{rel.as_posix()}"] = path
    for rel, source in sorted(copies.items()):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644)
    return sorted(copies)


def manifest(
    pins: Pins,
    versions: BundleVersions,
    assembly: payload.Assembly,
    *,
    commit: str,
    tag: str | None,
    build_number: str | None,
    swiftc: str,
    removed: Sequence[str],
) -> dict[str, Any]:
    """MANIFEST.json (0002 §6): the app's identity and version strings; the commit, the version
    inputs and the tools; the host's real ``__version__`` and its wheel's sha256; the runtime's
    archive and what the build removed from it; and every installed wheel, with its file, URL and
    sha256. No time and no signature: it describes the unsigned payload."""
    return {
        "schema": 1,
        "app": {
            "name": pins.app_name,
            "bundle_id": pins.bundle_id,
            "runtime_id": pins.runtime_id,
            "deployment_target": pins.deployment_target,
            "short_version": versions.short,
            "bundle_version": versions.build,
        },
        "build": {
            "commit": commit,
            "tag": tag,
            "build_number": build_number,
            "xcode": pins.xcode,
            "swiftc": swiftc,
        },
        "host": {
            "version": assembly.host.version,
            "wheel": {"file": assembly.host.filename, "sha256": assembly.host.sha256},
        },
        "runtime": {
            "version": pins.python_version,
            "asset": pins.asset,
            "url": pins.url,
            "sha256": pins.sha256,
            "removed": sorted(removed),
        },
        "wheels": payload.manifest_wheels(
            payload.installed_versions(assembly.site), assembly.wheels
        ),
        "ripgrep": {"commit": pins.ripgrep_commit} if pins.ripgrep_commit else None,
    }


def manifest_text(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def make_dmg(app: Path, dmg: Path, volume: str, work: Path) -> None:
    """A compressed disk image holding the app and a link to /Applications to drag it to.
    ``hdiutil create`` writes it as ``work/made.dmg``. This build never relies on a verify of
    that file and never hands it on (when it is locked, ``hdiutil verify -debug`` runs on it only
    for the log): in three CI builds hdiutil refused that file on every try the job made, and that
    a lingering disk-image helper held a lock on it is inferred, not seen. Instead a copy, a new
    file, is checked to have the same bytes and a whole UDIF trailer, and to pass ``hdiutil
    verify``. Only then is it renamed to ``dmg``, so ``dmg`` never names an image that verify has
    not passed. On GitHub Actions, a locked ``made.dmg`` and a copy refused as busy are also said
    as warning annotations."""
    staging = work / "dmg"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    _run(["ditto", str(app), str(staging / app.name)])
    (staging / "Applications").symlink_to("/Applications")
    made = work / "made.dmg"  # beside the folder it is made from, never inside it
    copy = work / "copy.dmg"
    for old in (dmg, made, copy):
        old.unlink(missing_ok=True)  # so each is a new file, never one something still holds
    create = ["create", "-volname", volume, "-srcfolder", str(staging), "-format", "UDZO", "-ov"]
    done = hdiutil([*create, str(made)])
    print(done.stdout, end="", flush=True)
    if done.returncode != 0:
        raise BuildError(f"hdiutil create failed: {done.stderr.strip()}")
    held = image_lock_held(made)
    if isinstance(held, OSError):
        print(
            f"after hdiutil create: {made.name} could not be checked for a lock: {held}",
            flush=True,
        )
    elif held:
        print(
            f"after hdiutil create: {made.name} is locked by another process; this build "
            "verifies and hands on a copy of it instead",
            flush=True,
        )
        _annotate(
            f"{made.name} was locked by another process right after hdiutil create, so this build "
            "verifies a copy of it instead. This step's log says what held it, after the line that "
            "starts 'after hdiutil create'."
        )
        show_image_holders(made)
    else:
        print(f"after hdiutil create: {made.name} has no lock on it", flush=True)
    try:
        try:
            with made.open("rb") as source, copy.open("xb") as target:
                shutil.copyfileobj(source, target)
        except OSError as error:
            raise BuildError(f"could not copy {made} to {copy.name}: {error}") from error
        copied, now = sha256_of(copy), sha256_of(made)
        if copied != now:
            raise BuildError(
                f"{made} changed while it was copied (copy {copied}, now {now}): something was "
                "still writing it after hdiutil create returned"
            )
        problem = udif_trailer_problem(copy)
        if problem is not None:
            raise BuildError(f"hdiutil create made no whole disk image: {problem}")
        done = hdiutil(["verify", str(copy)])
        print(done.stdout, end="", flush=True)
        if done.returncode != 0:
            if hdiutil_busy(done):
                _annotate(
                    f"hdiutil verify refused {copy.name}, a new file this build had just copied "
                    f"from {made.name}, as busy, so the copy did not get past the refusal. This "
                    "step's log says what held it, after the line that starts 'what may be "
                    "holding'."
                )
                show_image_holders(copy)
            raise BuildError(f"hdiutil verify failed: {done.stderr.strip()}")
    except BaseException:
        copy.unlink(missing_ok=True)  # nothing unverified is left to be taken for the image
        raise
    os.replace(copy, dmg)  # the same file system: an atomic rename of the inode verify passed
    print(f"verified a copy of {made.name}; it is now {dmg.name}", flush=True)
    try:
        made.unlink()
    except OSError as error:
        print(f"could not remove {made}: {error}", flush=True)


def check_icon_sources() -> None:
    """Refuse to build unless the PNGs in ``macos/Icons/`` are the ones the build can use
    (:func:`icons.source_problems`). The build renders nothing: it packs and copies these."""
    problems = icons.source_problems()
    if problems:
        raise BuildError(
            "the icons in macos/Icons are not ready to build from:\n  " + "\n  ".join(problems)
        )


def plan(out: Path, tag: str | None, build_number: str | None) -> None:
    """Check the pins, the prune list, the version and the icons' PNGs, write the Info.plist the
    app would get, and say what ``build`` would do. Runs no other program. With neither ``tag`` nor
    ``build_number``, the bundle version is a placeholder, 1."""
    pins = load_pins()
    constraints = load_build_constraints()
    prune = load_prune_list()
    version = host_version()
    check_icon_sources()
    placeholder = tag is None and build_number is None
    versions = bundle_versions(version, tag, "1" if placeholder else build_number)
    out.mkdir(parents=True, exist_ok=True)
    plist_path = out / "Info.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(info_plist(pins, versions), handle, sort_keys=True)
    note = " (a placeholder: pass --build-number or --tag)" if placeholder else ""
    hardened = "with" if pins.adhoc_hardened_runtime else "without"
    print(f"host version      {version}")
    print(f"bundle versions   {versions.short} ({versions.build}){note}")
    print(f"python runtime    {pins.asset}")
    print(f"  from            {pins.url}")
    print(f"  sha256          {pins.sha256}")
    print(f"  pruned          {len(prune)} paths ({PRUNE_FILE.name}), and pip uninstalled")
    print(f"app               {out / (pins.app_name + '.app')}")
    print(f"bundle id         {pins.bundle_id}, macOS {pins.deployment_target} or later, arm64")
    print(f"runtime id        {pins.runtime_id} ({RUNTIME_IN_APP})")
    print(f"xcode             {pins.xcode} ({developer_dir(pins)})")
    print(f"signature         ad hoc, {hardened} the hardened runtime")
    print(f"build backend     {len(constraints)} wheels, by hash, from {BUILD_CONSTRAINTS.name}")
    # One image per slot. The 16 and 32 pixel images are each in two entries, RGB and its mask.
    pngs = len({source for _, _, source in icons.ICNS_ENTRIES})
    print(
        f"app icon          {icons.ICNS_FILE}, {len(icons.iconset_files())} images in "
        f"{len(icons.ICNS_ENTRIES)} entries from {pngs} PNGs in macos/Icons"
    )
    print(f"menu bar icons    {len(icons.GLYPH_FILES)} template PNGs from macos/Icons")
    print(f"unsigned tarball  {out / unsigned_tarball_name(pins.app_name)}")
    print(f"disk image        {out}/{pins.app_name}-{version}-<commit>{DMG_SUFFIX}")
    print(f"Info.plist        written to {plist_path}")


@dataclass(frozen=True)
class Assembled:
    app: Path
    version: str
    commit: str


def assemble(
    out: Path,
    *,
    host_wheel: Path,
    host_sha256: str,
    tag: str | None,
    build_number: str | None,
    archive: Path | None,
    cache: Path | None,
) -> Assembled:
    """Assemble the unsigned app and its tarball in ``out``; the steps are in this module's
    docstring. Every check that needs no Mac runs before the first thing is written."""
    pins = load_pins()
    load_build_constraints()
    prune = load_prune_list()
    version = host_version()
    versions = bundle_versions(version, tag, build_number)
    if archive is not None:
        check_sha256(archive, pins.sha256)
    check_host_wheel(host_wheel, host_sha256, version)
    check_icon_sources()
    require_apple_silicon("assemble")
    toolchain = xcode_env(pins, os.environ)
    commit = _output(["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"])
    commit_time = int(_output(["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%ct"]))
    cache = cache if cache is not None else out / "cache"
    work = out / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    runtime_archive = fetch_runtime(pins, cache, archive)

    app = out / f"{pins.app_name}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    runtime = unpack_runtime(runtime_archive, contents)
    removed = prepare_runtime(runtime, prune)
    assembly = payload.assemble_site_packages(
        runtime / SITE_PACKAGES,
        requirements_text=export_requirements_text(),
        lock_text=LOCK_FILE.read_text(encoding="utf-8"),
        host_wheel=host_wheel,
        host_sha256=host_sha256,
        host_version=version,
        python_version=pins.python_version,
        deployment_target=pins.deployment_target,
        cache=cache / "wheels",
        work=work,
        environ=os.environ,
    )
    compile_bytecode(app)

    executable = contents / "MacOS" / pins.app_name
    compile_app(executable, pins.deployment_target, toolchain)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info_plist(pins, versions), handle, sort_keys=True)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    write_wrapper(app)
    copy_licenses(app / LICENSES_IN_APP, assembly.site, runtime / STDLIB / "LICENSE.txt")
    icons.write_icons(contents / "Resources")
    swiftc = _output(["xcrun", "swiftc", "--version"], env=toolchain).splitlines()[0]
    data = manifest(
        pins,
        versions,
        assembly,
        commit=commit,
        tag=tag,
        build_number=build_number,
        swiftc=swiftc,
        removed=removed,
    )
    (app / MANIFEST_PATH).write_text(manifest_text(data), encoding="utf-8")
    # Before the tarball: no link, the bytecode where the build wrote it, M10 again, with the
    # .pyc files the build wrote admitted by their names (compile_bytecode checked their bytes),
    # and the icons, which must be the committed files.
    problems = [f"symbolic link in the app: {link}" for link in symlinks_in(app)]
    problems += bytecode_layout_problems(app)
    problems += payload.record_problems(
        assembly.site,
        [wheel.path for wheel in assembly.wheels] + [assembly.host.path],
        bytecode_tag=BYTECODE_TAG,
    )
    problems += icons.bundle_problems(app)
    if problems:
        raise BuildError("the app is not ready to sign:\n  " + "\n  ".join(problems))
    tarball = out / unsigned_tarball_name(pins.app_name)
    digests = payload.write_tarball(app, tarball, commit_time)
    print(f"app               {app} (unsigned)")
    print(f"unsigned tarball  {tarball}")
    print(f"  sha256          {digests.gzip_sha256} (tar inside: {digests.tar_sha256})")
    return Assembled(app, version, commit)


def build(out: Path, **options: Any) -> Path:
    """Assemble the app, sign it ad hoc and build the disk image; returns the disk image's path."""
    built = assemble(out, **options)
    pins = load_pins()
    sign_app(built.app, pins)
    for old in disk_images(out, pins.app_name):
        old.unlink()
    dmg = out / dmg_name(pins.app_name, built.version, built.commit)
    make_dmg(built.app, dmg, pins.app_name, out / "work")
    print(f"app         {built.app}")
    print(f"disk image  {dmg} ({dmg.stat().st_size} bytes, sha256 {sha256_of(dmg)})")
    return dmg


def compare(first: Path, second: Path, app_name: str) -> str:
    """Two builds' unsigned tarballs compared, as Markdown: SAME, DIFFERENT, or FAILED when either
    is missing; the sha256 of each gzip file and of the tar inside it; and up to 50 members that
    differ."""
    a, b = (folder / unsigned_tarball_name(app_name) for folder in (first, second))
    lines = ["### The unsigned app, built twice in this job", ""]
    missing = [str(path) for path in (a, b) if not path.is_file()]
    if missing:
        return "\n".join([*lines, f"**FAILED**: no tarball at {', '.join(missing)}", ""])
    da, db = payload.tarball_digests(a), payload.tarball_digests(b)
    same = da == db
    lines += [
        f"**{'SAME' if same else 'DIFFERENT'}**",
        "",
        "| | first build | second build |",
        "|---|---|---|",
        f"| sha256 of the .tar.gz | `{da.gzip_sha256}` | `{db.gzip_sha256}` |",
        f"| sha256 of the .tar inside | `{da.tar_sha256}` | `{db.tar_sha256}` |",
        "",
    ]
    if not same:
        differing = payload.tar_differences(a, b)
        lines.append(f"{len(differing)} members differ" + (", the first 50:" if differing else "."))
        lines += [f"- `{name}`" for name in differing[:50]]
        lines.append("")
    return "\n".join(lines)


def _version_flags(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--tag", help="a build from this v* tag: the version must equal it")
    group.add_argument(
        "--build-number", help="any other build: CFBundleVersion (CI passes its run number)"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_app.py", description="Build the Mac app and its disk image."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    plan_parser = commands.add_parser("plan", help="check the pins and write the Info.plist only")
    plan_parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    _version_flags(plan_parser, required=False)
    wheel_parser = commands.add_parser("wheel", help="build the host's wheel from this checkout")
    wheel_parser.add_argument("--out", type=Path, required=True, help="folder for the wheel")
    wheel_parser.add_argument(
        "--github-output", type=Path, help="also write path= and sha256= lines to this file"
    )
    toolchain_parser = commands.add_parser("toolchain", help="check the pinned Xcode (macOS)")
    toolchain_parser.add_argument(
        "--github-env", type=Path, help="append DEVELOPER_DIR=... to this file ($GITHUB_ENV)"
    )
    for name, text in (
        ("assemble", "assemble the unsigned app and its tarball (macOS)"),
        ("build", "assemble, sign ad hoc, and build the disk image (macOS)"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
        sub.add_argument("--host-wheel", type=Path, required=True, help="the host's wheel")
        sub.add_argument(
            "--host-wheel-sha256", required=True, help="the host wheel's sha256, checked first"
        )
        _version_flags(sub, required=True)
        sub.add_argument(
            "--python-archive",
            type=Path,
            help="use this copy of the pinned runtime archive instead of downloading it",
        )
        sub.add_argument("--cache", type=Path, help="download cache (default: OUT/cache)")
    compare_parser = commands.add_parser("compare", help="compare two builds' unsigned tarballs")
    compare_parser.add_argument("first", type=Path)
    compare_parser.add_argument("second", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            plan(args.out.resolve(), args.tag, args.build_number)
        elif args.command == "wheel":
            wheel = build_wheel(args.out.resolve(), host_version())
            digest = sha256_of(wheel)
            print(f"wheel   {wheel}\nsha256  {digest}")
            if args.github_output is not None:
                if "\n" in str(wheel):
                    raise BuildError("the wheel's path holds a newline")
                with args.github_output.open("a", encoding="utf-8") as handle:
                    handle.write(f"path={wheel}\nsha256={digest}\n")
        elif args.command == "toolchain":
            select_toolchain(load_pins(), args.github_env)
        elif args.command == "compare":
            print(compare(args.first, args.second, load_pins().app_name))
        else:
            options: dict[str, Any] = {
                "host_wheel": args.host_wheel.resolve(),
                "host_sha256": args.host_wheel_sha256,
                "tag": args.tag,
                "build_number": args.build_number,
                "archive": args.python_archive,
                "cache": args.cache.resolve() if args.cache is not None else None,
            }
            if args.command == "assemble":
                assemble(args.out.resolve(), **options)
            else:
                build(args.out.resolve(), **options)
    except (PinError, BuildError, payload.AssemblyError) as exc:
        print(f"build_app.py: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"build_app.py: {exc.cmd[0]} exited with {exc.returncode}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
