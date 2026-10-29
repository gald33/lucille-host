"""Build the Lucille Host app for Apple silicon Macs, and a disk image that holds it.

    uv run --no-project python macos/build_app.py build   # on a Mac: the app and the .dmg
    uv run --no-project python macos/build_app.py plan    # anywhere: check the pins, show the plan

``build`` needs macOS on Apple silicon, Xcode or its command line tools (``swiftc``,
``codesign``, ``hdiutil``), ``uv``, and network access to GitHub and PyPI. It writes everything
under ``build/macos/``, which git ignores:

1. Downloads the Python runtime that ``macos/runtime.toml`` pins, and stops unless the file's
   sha256 is the pinned one.
2. Builds the host's wheel from this checkout (``uv build --wheel``), and exports the runtime
   dependencies ``uv.lock`` pins, with their hashes (``uv export --frozen``).
3. Unpacks the runtime into ``Lucille Host.app/Contents/Resources/python``. Keeps one program,
   ``bin/python3``, and removes what the app never uses (:func:`prune_runtime`). No symbolic link
   is left in the bundle.
4. Installs the dependencies, then the host wheel, into that interpreter with ``pip install
   --require-hashes --no-deps --only-binary :all:``. The host wheel comes from step 2 only
   (``--no-index``). Then removes pip.
5. Compiles every ``.py`` file in the runtime to a hash-checked ``.pyc``, so that the interpreter
   never has a reason to write one inside the signed bundle.
6. Compiles the menu bar app in ``macos/App/`` with ``swiftc``, and writes ``Info.plist`` and a
   build manifest.
7. Signs every Mach-O file in the bundle, innermost first, then the bundle. The signature is ad
   hoc (``codesign -s -``), with the hardened runtime and no entitlements. A signature macOS
   trusts needs a Developer ID and notarization, which this script does not do.
8. Builds ``Lucille-Host-<version>-macOS-arm64.dmg``, which holds the app and a link to
   ``/Applications``.

``plan`` reads the pins and the version and writes the ``Info.plist`` the app would get, without
running any other program. Exit codes: 0 done, 1 refused or failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
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

MACOS_DIR: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = MACOS_DIR.parent
PINS_FILE: Final = MACOS_DIR / "runtime.toml"
SWIFT_DIR: Final = MACOS_DIR / "App"
VERSION_FILE: Final = REPO_ROOT / "src" / "lucille_host" / "__init__.py"
DEFAULT_OUT: Final = REPO_ROOT / "build" / "macos"

PBS_RELEASES: Final = "https://github.com/astral-sh/python-build-standalone/releases/download/"
# Folder and file names inside the runtime. The pins allow Python 3.13 only, so these are fixed.
STDLIB: Final = "lib/python3.13"
INTERPRETER: Final = "bin/python3"
# What the bundle's Info.plist says each time macOS asks the user about a protected folder.
FOLDER_USAGE: Final = (
    "Lucille Host reads files here only for abilities you turn on, in folders you add."
)
# Keys an Info.plist of this app must never carry: a scripting surface, Services, network
# exceptions, or an update feed. The app has none of these.
FORBIDDEN_PLIST_KEYS: Final = frozenset(
    {
        "NSAppleScriptEnabled",
        "OSAScriptingDefinition",
        "NSServices",
        "NSAppTransportSecurity",
        "SUFeedURL",
        "SUPublicEDKey",
    }
)
# Paths under the runtime's top folder that the app never uses. Besides these, bin/ keeps only
# python3 and lib/ keeps only python3.13/ (prune_runtime). The host's test suite runs on the
# pruned runtime in CI, so a module the host needs cannot go missing unnoticed.
PRUNED: Final = (
    "include",
    "share",
    f"{STDLIB}/config-3.13-darwin",
    f"{STDLIB}/ensurepip",
    f"{STDLIB}/idlelib",
    f"{STDLIB}/tkinter",
    f"{STDLIB}/turtle.py",
    f"{STDLIB}/turtledemo",
    f"{STDLIB}/lib-dynload/_tkinter.cpython-313-darwin.so",
)

_ASSET = re.compile(
    r"cpython-(?P<version>3\.13\.\d+)\+(?P<release>\d{8})-aarch64-apple-darwin-"
    r"install_only(?:_stripped)?\.tar\.gz"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BUNDLE_ID = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){2,}")
_MACOS_VERSION = re.compile(r"(?P<major>\d+)\.\d+")
_VERSION_LINE = re.compile(r'^__version__ = "([^"\n]+)"$', re.MULTILINE)
_RELEASE = re.compile(r"\d+(?:\.\d+){0,2}")
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
# A .pyc header's flags word: bit 0 is "hash-based", bit 1 is "check the source".
_CHECKED_HASH_PYC: Final = 0b11


class PinError(ValueError):
    """runtime.toml is not well formed."""


class BuildError(RuntimeError):
    """The build cannot go on; the message says why."""


@dataclass(frozen=True)
class Pins:
    """What runtime.toml pins."""

    python_version: str
    asset: str
    url: str
    sha256: str
    app_name: str
    bundle_id: str
    minimum_macos: str
    category: str


def _table(data: Mapping[str, Any], name: str, keys: frozenset[str]) -> Mapping[str, str]:
    table = data.get(name)
    if not isinstance(table, dict) or set(table) != keys:
        raise PinError(f"[{name}] holds exactly these keys: {', '.join(sorted(keys))}")
    for key in sorted(keys):
        if not isinstance(table[key], str) or not table[key]:
            raise PinError(f"[{name}] {key} must be a non-empty string")
    return table


def parse_pins(text: str) -> Pins:
    """The pins in ``text`` (the contents of runtime.toml), checked for consistency."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PinError(f"runtime.toml does not parse: {exc}") from exc
    if set(data) != {"python", "app"}:
        raise PinError("runtime.toml holds exactly two tables, [python] and [app]")
    python = _table(data, "python", frozenset({"version", "asset", "url", "sha256"}))
    app = _table(data, "app", frozenset({"name", "bundle_id", "minimum_macos", "category"}))

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

    if "/" in app["name"] or app["name"].strip() != app["name"]:
        raise PinError("[app] name must be a plain name")
    if _BUNDLE_ID.fullmatch(app["bundle_id"]) is None:
        raise PinError("[app] bundle_id must be a lower-case reverse-DNS name")
    macos = _MACOS_VERSION.fullmatch(app["minimum_macos"])
    if macos is None or int(macos["major"]) < 13:
        raise PinError("[app] minimum_macos must be 13.0 or later (the menu bar API needs 13)")
    if not app["category"].startswith("public.app-category."):
        raise PinError("[app] category must be a public.app-category.* value")
    return Pins(
        python_version=python["version"],
        asset=python["asset"],
        url=python["url"],
        sha256=python["sha256"],
        app_name=app["name"],
        bundle_id=app["bundle_id"],
        minimum_macos=app["minimum_macos"],
        category=app["category"],
    )


def load_pins(path: Path = PINS_FILE) -> Pins:
    return parse_pins(path.read_text(encoding="utf-8"))


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


def info_plist(pins: Pins, version: str) -> dict[str, Any]:
    """The app's Info.plist. ``LucilleHostVersion`` is the host's exact version; the bundle
    version strings carry its release numbers, because Apple's format allows nothing else."""
    triple = release_triple(version)
    usage = {
        key: FOLDER_USAGE
        for key in (
            "NSDocumentsFolderUsageDescription",
            "NSDesktopFolderUsageDescription",
            "NSDownloadsFolderUsageDescription",
            "NSRemovableVolumesUsageDescription",
            "NSNetworkVolumesUsageDescription",
        )
    }
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": pins.app_name,
        "CFBundleExecutable": pins.app_name,
        "CFBundleIdentifier": pins.bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": pins.app_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": triple,
        "CFBundleVersion": triple,
        "LSApplicationCategoryType": pins.category,
        "LSArchitecturePriority": ["arm64"],
        "LSMinimumSystemVersion": pins.minimum_macos,
        "LSUIElement": True,
        "LucilleHostVersion": version,
        "NSHumanReadableCopyright": "MIT License",
        **usage,
    }


def dmg_name(version: str) -> str:
    return f"Lucille-Host-{version}-macOS-arm64.dmg"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def check_sha256(path: Path, expected: str) -> None:
    """Refuse a file whose sha256 is not the pinned one."""
    actual = sha256_of(path)
    if actual != expected:
        raise BuildError(
            f"{path.name}: its sha256 is {actual}, but runtime.toml pins {expected}; "
            "refusing to build"
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


def bytecode_problems(python_dir: Path) -> list[str]:
    """Each ``.py`` file in the runtime's library without a hash-checked ``.pyc`` beside it."""
    tag = "cpython-313"
    problems = []
    for source in sorted((python_dir / STDLIB).rglob("*.py")):
        compiled = source.parent / "__pycache__" / f"{source.stem}.{tag}.pyc"
        if not compiled.is_file():
            problems.append(f"no .pyc for {source.relative_to(python_dir)}")
            continue
        with compiled.open("rb") as handle:
            header = handle.read(8)
        if len(header) < 8 or int.from_bytes(header[4:8], "little") != _CHECKED_HASH_PYC:
            problems.append(f"{compiled.relative_to(python_dir)} is not a hash-checked .pyc")
    return problems


def _run(argv: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> None:
    print("+ " + shlex.join(argv), flush=True)
    subprocess.run(argv, check=True, cwd=cwd, env=None if env is None else dict(env))


def _output(argv: list[str]) -> str:
    done = subprocess.run(argv, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def build_wheel(work: Path, version: str) -> Path:
    """The host's wheel, built from this checkout."""
    out = work / "wheel"
    _run(["uv", "build", "--wheel", "--out-dir", str(out), str(REPO_ROOT)])
    wheels = sorted(out.glob("*.whl"))
    expected = f"lucille_host-{version}-py3-none-any.whl"
    if [wheel.name for wheel in wheels] != [expected]:
        raise BuildError(f"expected exactly {expected} from uv build, got {wheels}")
    return wheels[0]


def export_requirements(work: Path) -> Path:
    """The runtime dependencies uv.lock pins, one per line with their hashes."""
    target = work / "requirements.txt"
    _run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(target),
        ],
        cwd=REPO_ROOT,
    )
    return target


def unpack_runtime(archive: Path, contents: Path) -> Path:
    """Unpack the runtime into ``Contents/Resources/python``; returns that folder."""
    resources = contents / "Resources"
    staging = resources / "unpack"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(staging, filter="data")
    top = staging / "python"
    if not (top / "bin" / "python3.13").is_file():
        raise BuildError(f"{archive.name} has no python/bin/python3.13")
    python_dir = resources / "python"
    top.rename(python_dir)
    staging.rmdir()
    return python_dir


def prune_runtime(python_dir: Path) -> list[str]:
    """Keep one interpreter, ``bin/python3``, and remove what the app never uses. ``bin/python3.13``
    links Python statically, so ``lib/libpython3.13.dylib`` goes with the rest of ``lib/`` that is
    not ``python3.13/``: the Tcl/Tk libraries and pkg-config files. Returns what was removed."""
    removed = []
    bin_dir = python_dir / "bin"
    interpreter = python_dir / INTERPRETER
    if interpreter.is_symlink() or interpreter.exists():
        interpreter.unlink()
    (bin_dir / "python3.13").rename(interpreter)
    for entry in sorted(bin_dir.iterdir()):
        if entry != interpreter:
            _remove(entry)
            removed.append(str(entry.relative_to(python_dir)))
    for entry in sorted((python_dir / "lib").iterdir()):
        if entry.name != "python3.13":
            _remove(entry)
            removed.append(str(entry.relative_to(python_dir)))
    for rel in PRUNED:
        path = python_dir / rel
        if path.is_symlink() or path.exists():
            _remove(path)
            removed.append(rel)
    return removed


def _pip_env() -> dict[str, str]:
    """pip's environment: this one, without pip's own variables and with no pip config file."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def install_packages(python: Path, requirements: Path, wheel: Path, version: str) -> None:
    """Install the locked dependencies from PyPI, then the host wheel from the local build only,
    each by hash, as binary wheels, without resolving anything."""
    common = [
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--no-compile",
        "--no-warn-script-location",
        "--no-deps",
        "--require-hashes",
        "--only-binary",
        ":all:",
    ]
    _run(
        [str(python), "-I", "-m", "pip", "install", *common, "-r", str(requirements)],
        env=_pip_env(),
    )
    host_requirements = wheel.parent.parent / "host-requirements.txt"
    host_requirements.write_text(
        f"lucille-host=={version} --hash=sha256:{sha256_of(wheel)}\n", encoding="ascii"
    )
    _run(
        [
            str(python),
            "-I",
            "-m",
            "pip",
            "install",
            *common,
            "--no-index",
            "--find-links",
            str(wheel.parent),
            "-r",
            str(host_requirements),
        ],
        env=_pip_env(),
    )


def remove_after_install(python_dir: Path) -> list[str]:
    """Remove pip, and any program pip wrote into ``bin/`` (its scripts name the build folder)."""
    removed = []
    site = python_dir / STDLIB / "site-packages"
    for entry in sorted(site.iterdir()):
        if entry.name == "pip" or (entry.name.startswith("pip-") and entry.suffix == ".dist-info"):
            _remove(entry)
            removed.append(str(entry.relative_to(python_dir)))
    interpreter = python_dir / INTERPRETER
    for entry in sorted((python_dir / "bin").iterdir()):
        if entry != interpreter:
            _remove(entry)
            removed.append(str(entry.relative_to(python_dir)))
    return removed


def compile_bytecode(python: Path, python_dir: Path) -> None:
    """Compile every ``.py`` file to a hash-checked ``.pyc``. The paths recorded in them start
    at ``python/``, not at the build folder. ``-B`` keeps the compiler's own imports from writing
    any other kind of ``.pyc``."""
    for cache in [path for path in python_dir.rglob("__pycache__") if path.is_dir()]:
        shutil.rmtree(cache)
    _run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "compileall",
            "-q",
            "-f",
            "-j",
            "0",
            "--invalidation-mode",
            "checked-hash",
            "-s",
            str(python_dir),
            "-p",
            "python",
            str(python_dir / STDLIB),
        ]
    )


def installed_distributions(python_dir: Path) -> list[str]:
    """``name==version`` for each distribution installed in the runtime."""
    site = python_dir / STDLIB / "site-packages"
    names = []
    for entry in sorted(site.glob("*.dist-info")):
        name, _, rest = entry.name[: -len(".dist-info")].partition("-")
        names.append(f"{name}=={rest}")
    return names


def compile_app(executable: Path, minimum_macos: str) -> None:
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
            f"arm64-apple-macos{minimum_macos}",
            "-o",
            str(executable),
            *sources,
        ]
    )


def sign_app(app: Path, main_executable: Path, bundle_id: str) -> None:
    """Sign ad hoc with the hardened runtime and no entitlements: each nested Mach-O file
    innermost first, then the bundle, which signs its main executable. Never ``--deep``."""
    for path in macho_files(app):
        if path == main_executable:
            continue
        _run(
            [
                "codesign",
                "--force",
                "--options",
                "runtime",
                "--timestamp=none",
                "--sign",
                "-",
                "--prefix",
                f"{bundle_id}.",
                str(path),
            ]
        )
    _run(
        ["codesign", "--force", "--options", "runtime", "--timestamp=none", "--sign", "-", str(app)]
    )
    _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])


def make_dmg(app: Path, dmg: Path, volume: str, work: Path) -> None:
    """A compressed disk image holding the app and a link to /Applications to drag it to."""
    staging = work / "dmg"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    _run(["ditto", str(app), str(staging / app.name)])
    (staging / "Applications").symlink_to("/Applications")
    if dmg.exists():
        dmg.unlink()
    argv = [
        "hdiutil",
        "create",
        "-volname",
        volume,
        "-srcfolder",
        str(staging),
        "-format",
        "UDZO",
        "-ov",
        str(dmg),
    ]
    # hdiutil create sometimes fails with "Resource busy" on a busy machine; try again.
    for attempt in range(1, 4):
        print("+ " + shlex.join(argv), flush=True)
        if subprocess.run(argv, check=False).returncode == 0:
            break
        if attempt == 3:
            raise BuildError("hdiutil create failed three times")
        time.sleep(5 * attempt)
    _run(["hdiutil", "verify", str(dmg)])


def plan(out: Path) -> None:
    """Check the pins and the version, write the Info.plist the app would get, and say what
    ``build`` would do. Runs no other program."""
    pins = load_pins()
    version = host_version()
    out.mkdir(parents=True, exist_ok=True)
    plist_path = out / "Info.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(info_plist(pins, version), handle, sort_keys=True)
    print(f"host version      {version} (bundle version {release_triple(version)})")
    print(f"python runtime    {pins.asset}")
    print(f"  from            {pins.url}")
    print(f"  sha256          {pins.sha256}")
    print(f"app               {out / (pins.app_name + '.app')}")
    print(f"bundle id         {pins.bundle_id}, macOS {pins.minimum_macos} or later, arm64")
    print(f"disk image        {out / dmg_name(version)}")
    print(f"Info.plist        written to {plist_path}")


def build(out: Path, archive: Path | None) -> Path:
    """Build the app and the disk image; returns the disk image's path."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise BuildError("build runs on an Apple silicon Mac; `plan` runs anywhere")
    pins = load_pins()
    version = host_version()
    work = out / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    runtime = fetch_runtime(pins, out / "cache", archive)
    wheel = build_wheel(work, version)
    requirements = export_requirements(work)

    app = out / f"{pins.app_name}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    python_dir = unpack_runtime(runtime, contents)
    removed = prune_runtime(python_dir)
    python = python_dir / INTERPRETER
    install_packages(python, requirements, wheel, version)
    removed += remove_after_install(python_dir)
    compile_bytecode(python, python_dir)
    problems = bytecode_problems(python_dir)
    problems += [f"symbolic link in the bundle: {link}" for link in symlinks_in(app)]
    if problems:
        raise BuildError("the runtime is not ready to sign:\n  " + "\n  ".join(problems))

    executable = contents / "MacOS" / pins.app_name
    compile_app(executable, pins.minimum_macos)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info_plist(pins, version), handle, sort_keys=True)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    manifest = {
        "host_version": version,
        "host_wheel": {"name": wheel.name, "sha256": sha256_of(wheel)},
        "python": {"asset": pins.asset, "url": pins.url, "sha256": pins.sha256},
        "installed": installed_distributions(python_dir),
        "removed_from_python": sorted(removed),
        "swiftc": _output(["xcrun", "swiftc", "--version"]).splitlines()[0],
        "signature": "ad hoc, hardened runtime, no entitlements",
    }
    (contents / "Resources" / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sign_app(app, executable, pins.bundle_id)

    dmg = out / dmg_name(version)
    make_dmg(app, dmg, pins.app_name, work)
    print(f"app         {app}")
    print(f"disk image  {dmg} ({dmg.stat().st_size} bytes, sha256 {sha256_of(dmg)})")
    return dmg


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_app.py", description="Build the Lucille Host app and its disk image."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    plan_parser = commands.add_parser("plan", help="check the pins and write the Info.plist only")
    plan_parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    build_parser = commands.add_parser("build", help="build the app and the disk image (macOS)")
    build_parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    build_parser.add_argument(
        "--python-archive",
        type=Path,
        help="use this copy of the pinned runtime archive instead of downloading it",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan(args.out.resolve())
        else:
            build(args.out.resolve(), args.python_archive)
    except (PinError, BuildError) as exc:
        print(f"build_app.py: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"build_app.py: {exc.cmd[0]} exited with {exc.returncode}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
