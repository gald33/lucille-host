"""tools/macos/verify_app.py on synthetic bundles, on Linux: an app assembled here by the build's
own steps from a runtime archive and wheels made here passes, and each way of changing it fails
the check that should see it. Nothing is downloaded: every test runs with ``offline``."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import plistlib
import re
import struct
import subprocess
import tarfile
import urllib.error
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from tools.macos import build_app, payload, verify_app

APP_NAME = build_app.load_pins().app_name
HOST_VERSION = "0.4.0"
BUILD_NUMBER = "17"
COMMIT = "0123456789ab"
SWIFTC = "swift-driver version: 1.0 Apple Swift version 6.1"
# A Mach-O file's first four bytes, as build_app.is_macho reads them, then anything.
MACHO = b"\xcf\xfa\xed\xfe"
# The prune list of the runtime made here: each entry is in it, as the build requires.
PRUNE = ("bin/python3", "include", "lib/python3.13/site-packages/README.txt")
SITE = f"{build_app.RUNTIME_DIR}/{build_app.SITE_PACKAGES}"
EXECUTABLE = f"Contents/MacOS/{APP_NAME}"


def _runtime_archive(folder: Path) -> Path:
    """A python-build-standalone-like archive: the interpreter and a link to it, headers, a
    standard library with a Mach-O extension and the licence, site-packages with its README
    and the runtime's own pip, which the build uninstalls by its RECORD."""
    entries: dict[str, bytes] = {
        "python/bin/python3.13": MACHO + b" interpreter",
        "python/include/python3.13/Python.h": b"/* header */\n",
        "python/lib/python3.13/os.py": b"import sys\n",
        "python/lib/python3.13/json/__init__.py": b"",
        "python/lib/python3.13/LICENSE.txt": b"PSF licence\n",
        "python/lib/python3.13/lib-dynload/_json.cpython-313-darwin.so": MACHO + b" json",
        "python/lib/python3.13/site-packages/README.txt": b"site\n",
        "python/lib/python3.13/site-packages/pip/__init__.py": b"",
        "python/bin/pip3": b"#!/the/build/machine/python3.13\n",
    }
    record = (
        "pip/__init__.py,sha256=x,0\n"
        "pip/__pycache__/__init__.cpython-313.pyc,sha256=x,1\n"
        "../../../bin/pip3,sha256=x,30\n"
        "pip-26.2.2.dist-info/RECORD,,\n"
    )
    entries["python/lib/python3.13/site-packages/pip-26.2.2.dist-info/RECORD"] = record.encode()
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / build_app.load_pins().asset
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.startswith("python/bin/") or name.endswith(".so") else 0o644
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("python/bin/python3")
        link.type = tarfile.SYMTYPE
        link.linkname = "python3.13"
        tar.addfile(link)
    return archive


def _wheel(folder: Path, name: str, dist: str, version: str, files: Mapping[str, bytes]) -> Path:
    info = f"{name}-{version}.dist-info"
    entries = {
        **files,
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/licenses/LICENSE": f"{dist} licence\n".encode(),
    }
    rows = [f"{path},{payload.record_hash(data)},{len(data)}" for path, data in entries.items()]
    rows.append(f"{info}/RECORD,,")
    folder.mkdir(parents=True, exist_ok=True)
    wheel = folder / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
        archive.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")
    return wheel


def _install(site: Path, wheel: Path) -> None:
    """What the installer writes for ``wheel``: its files, INSTALLER and REQUESTED, and a RECORD
    with their rows."""
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
        info = next(n.split("/")[0] for n in archive.namelist() if n.endswith(".dist-info/RECORD"))
    record = site / info / "RECORD"
    for name, data in (("INSTALLER", b"uv"), ("REQUESTED", b"")):
        (site / info / name).write_bytes(data)
        row = f"{info}/{name},{payload.record_hash(data)},{len(data)}\n"
        record.write_text(record.read_text(encoding="utf-8") + row, encoding="utf-8")


def _write_bytecode(app: Path) -> None:
    """A checked-hash .pyc beside each .py of the runtime's library, as the build writes them
    (their contents are check_app.py bytecode's to judge, not verify_app's)."""
    library = app / build_app.RUNTIME_DIR / build_app.STDLIB
    header = b"\xf3\r\r\n" + (3).to_bytes(4, "little") + bytes(8)
    for source in sorted(library.rglob("*.py")):
        cache = source.parent / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / f"{source.stem}.{build_app.BYTECODE_TAG}.pyc").write_bytes(header + b"code")


@dataclasses.dataclass
class Fixture:
    app: Path
    source: verify_app.Source
    cache: Path
    host_wheel: Path
    demo_wheel: Path
    archive: Path

    def verify(self, **changes: Any) -> verify_app.Report:
        source = changes.pop("source", self.source)
        options: dict[str, Any] = {
            "host_wheel": self.host_wheel,
            "cache": self.cache,
            "offline": True,
            "on_mac": False,
        }
        options.update(changes)
        return verify_app.verify(self.app, source, **options)


def _lock_text(wheel: Path) -> str:
    digest = payload.sha256_of(wheel)
    url = f"https://files.pythonhosted.org/packages/ab/cd/{wheel.name}"
    return (
        'version = 1\n\n[[package]]\nname = "demo"\nversion = "1.0"\n'
        f'wheels = [{{ url = "{url}", hash = "sha256:{digest}" }}]\n'
    )


def _assemble(tmp: Path) -> Fixture:
    """The app, made here as ``build_app.assemble`` makes it, step for step, then signed the
    way this test can: a CodeResources file, and nothing rewritten."""
    cache = tmp / "cache"
    archive = _runtime_archive(cache)
    demo = _wheel(
        cache / "wheels",
        "demo",
        "demo",
        "1.0",
        {"demo/__init__.py": b"VALUE = 1\n", "demo/_speedups.abi3.so": MACHO + b" speedups"},
    )
    host = _wheel(
        tmp / "host-wheel",
        "lucille_host",
        "lucille-host",
        HOST_VERSION,
        {"lucille_host/__init__.py": f'__version__ = "{HOST_VERSION}"\n'.encode()},
    )
    pins = dataclasses.replace(build_app.load_pins(), sha256=payload.sha256_of(archive))
    source = verify_app.Source(
        pins=pins,
        prune=PRUNE,
        lock_text=_lock_text(demo),
        requirements_text=f"demo==1.0 --hash=sha256:{payload.sha256_of(demo)}\n",
        host_version=HOST_VERSION,
        icons=build_app.icons.ICONS_DIR,
    )
    versions = build_app.bundle_versions(HOST_VERSION, None, BUILD_NUMBER)

    app = tmp / "built" / f"{APP_NAME}.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    runtime = build_app.unpack_runtime(archive, contents)
    removed = build_app.prepare_runtime(runtime, PRUNE)
    site = runtime / build_app.SITE_PACKAGES
    plan = payload.planned_wheels(
        source.requirements_text, source.lock_text, pins.python_version, pins.deployment_target
    )
    wheels = payload.fetch_wheels(plan, cache / "wheels", tmp / "find-links")
    for wheel in wheels:
        _install(site, wheel.path)
    _install(site, host)
    _write_bytecode(app)
    (contents / "MacOS" / APP_NAME).write_bytes(MACHO + b" the Swift app")
    (contents / "MacOS" / APP_NAME).chmod(0o755)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(build_app.info_plist(pins, versions), handle, sort_keys=True)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    build_app.write_wrapper(app)
    build_app.copy_licenses(
        app / build_app.LICENSES_IN_APP, site, runtime / build_app.STDLIB / "LICENSE.txt"
    )
    build_app.icons.write_icons(contents / "Resources")
    host_file = payload.WheelFile(
        payload.HOST_DISTRIBUTION, HOST_VERSION, host.name, None, payload.sha256_of(host), host
    )
    assembly = payload.Assembly(site, tuple(wheels), host_file, ())
    data = build_app.manifest(
        pins,
        versions,
        assembly,
        commit=COMMIT,
        tag=None,
        build_number=BUILD_NUMBER,
        swiftc=SWIFTC,
        removed=removed,
    )
    (app / build_app.MANIFEST_PATH).write_text(build_app.manifest_text(data), encoding="utf-8")
    (contents / "_CodeSignature").mkdir()
    (contents / "_CodeSignature" / "CodeResources").write_bytes(b"<plist/>")
    return Fixture(app, source, cache, host, demo, archive)


@pytest.fixture
def built(tmp_path: Path) -> Fixture:
    return _assemble(tmp_path)


def _failures(report: verify_app.Report) -> str:
    return "\n".join(report.checker.failures)


def test_the_app_the_build_makes_passes(built: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    report = built.verify()
    out = capsys.readouterr().out
    assert report.passed, out
    assert "FAIL" not in out
    text = "\n".join(report.reported)
    assert f"- `{EXECUTABLE}`: the app's executable, not verified here" in text
    assert "- `Contents/_CodeSignature/CodeResources`: codesign's" in text
    assert report.not_compared == 1
    # Nothing was signed here: no runtime file differs from its reference, and each Mach-O file
    # of site-packages, which is listed whatever its bytes, is not compared off a Mac.
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    assert rows == [
        f"| `{SITE}/demo/_speedups.abi3.so` | not compared: `codesign` runs only on a Mac |"
    ]
    assert "0 findings of the RECORD check were about these files only." in text


def test_a_mach_o_file_signing_rewrote_is_reported_not_failed(built: Fixture) -> None:
    for rel in (
        f"{build_app.RUNTIME_IN_APP}",
        f"{build_app.RUNTIME_DIR}/{build_app.STDLIB}/lib-dynload/_json.cpython-313-darwin.so",
        f"{SITE}/demo/_speedups.abi3.so",
    ):
        path = built.app / rel
        path.write_bytes(path.read_bytes() + b"\x00signature")
    report = built.verify()
    assert report.passed, _failures(report)
    text = "\n".join(report.reported)
    assert f"| `{build_app.RUNTIME_IN_APP}` | not compared: `codesign` runs only on a Mac |" in text
    assert f"| `{SITE}/demo/_speedups.abi3.so` | not compared" in text
    assert "2 findings of the RECORD check were about these files only." in text


@pytest.mark.parametrize(
    "rel",
    ["Contents/Resources/fifo", f"{SITE}/demo/fifo", build_app.MANIFEST_PATH],
)
def test_a_fifo_anywhere_fails_before_any_file_is_read(
    built: Fixture, rel: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Reading a FIFO would block, so the check stops first, whether it is planted or stands in
    # for a file the build makes; main's checkout check never runs either.
    target = built.app / rel
    target.unlink(missing_ok=True)
    os.mkfifo(target)
    capsys.readouterr()
    report = built.verify()
    assert not report.passed
    assert _failures(report) == "the app holds nothing but files, folders and links"
    assert f"not a file or a folder: {rel}" in capsys.readouterr().out
    assert verify_app.main([str(built.app)], on_mac=False) == 1
    out = capsys.readouterr().out
    assert "FAIL: the app holds nothing but files, folders and links" in out
    assert "this checkout" not in out


def test_a_file_that_is_not_mach_o_is_never_excused_as_signed(built: Fixture) -> None:
    # The same bytes appended to the wrapper and to a module fail: only a Mach-O file whose
    # reference is one too is set aside.
    for rel in (build_app.WRAPPER_IN_APP, f"{SITE}/demo/__init__.py"):
        path = built.app / rel
        path.write_bytes(path.read_bytes() + b"\x00signature")
    failures = _failures(built.verify())
    assert "every file outside site-packages" in failures
    assert "site-packages holds exactly" in failures


def _edit(rel: str, data: bytes) -> Callable[[Path], None]:
    def change(app: Path) -> None:
        (app / rel).write_bytes(data)

    return change


def _remove(rel: str) -> Callable[[Path], None]:
    return lambda app: (app / rel).unlink()


def _rewrite_manifest(change: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
    def rewrite(app: Path) -> None:
        path = app / build_app.MANIFEST_PATH
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(build_app.manifest_text(data), encoding="utf-8")

    return rewrite


def _plist(key: str, value: object) -> Callable[[Path], None]:
    def change(app: Path) -> None:
        path = app / "Contents" / "Info.plist"
        data = plistlib.loads(path.read_bytes())
        data[key] = value
        path.write_bytes(plistlib.dumps(data, sort_keys=True))

    return change


def _link(app: Path) -> None:
    (app / "Contents" / "Resources" / "elsewhere").symlink_to("/usr/bin/true")


def _not_executable(app: Path) -> None:
    (app / build_app.WRAPPER_IN_APP).chmod(0o644)


def _empty_folder(app: Path) -> None:
    (app / "Contents" / "Plugins").mkdir()


def _set(path: tuple[str | int, ...], value: object) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        here: Any = data
        for key in path[:-1]:
            here = here[key]
        here[path[-1]] = value

    return change


RESOURCES = "Contents/Resources"
LICENSES = build_app.LICENSES_IN_APP
LIBRARY = f"{build_app.RUNTIME_DIR}/{build_app.STDLIB}"


@pytest.mark.parametrize(
    ("change", "check", "said"),
    [
        # Outside site-packages: a file planted, changed, removed, linked, or its mode changed.
        (_edit(f"{RESOURCES}/extra.sh", b"#!/bin/sh\n"), "every file outside", "no such file"),
        (
            _edit(f"{LIBRARY}/sitecustomize.py", b"import os\n"),
            "every file outside",
            "no such file",
        ),
        (
            _edit(build_app.WRAPPER_IN_APP, b"#!/bin/sh\nexec /tmp/x\n"),
            "every file outside",
            "differs",
        ),
        (_edit(f"{LIBRARY}/os.py", b"import evil\n"), "every file outside", "differs"),
        (_edit("Contents/PkgInfo", b"APPLxxxx"), "every file outside", "differs"),
        (_plist("LSUIElement", False), "every file outside", "Info.plist differs"),
        (
            _plist("CFBundleURLTypes", [{"CFBundleURLSchemes": ["capsule"]}]),
            "every file outside",
            "Info.plist differs",
        ),
        (
            _edit(f"{RESOURCES}/{build_app.icons.ICNS_FILE}", b"icns"),
            "every file outside",
            "differs",
        ),
        (_remove(f"{LICENSES}/demo/LICENSE"), "every file outside", "is missing"),
        (_remove(f"{LICENSES}/CPython/LICENSE.txt"), "every file outside", "is missing"),
        (_remove(f"{LIBRARY}/json/__init__.py"), "every file outside", "is missing"),
        (_link, "every file outside", "symbolic link in the app: Contents/Resources/elsewhere"),
        (_not_executable, "every file outside", "is not executable; the build makes it executable"),
        (
            _empty_folder,
            "every file outside",
            "Contents/Plugins/ is a folder the build does not make",
        ),
        (_edit(EXECUTABLE, b"#!/bin/sh\n"), "every file outside", "is not a Mach-O file"),
        (_remove(EXECUTABLE), "every file outside", "is missing"),
        # Only the executable is admitted by name in Contents/MacOS: another Mach-O file fails.
        (_edit("Contents/MacOS/helper", MACHO + b" helper"), "every file outside", "no such file"),
        (
            _edit(f"Contents/MacOS/{APP_NAME}-helper", MACHO + b" helper"),
            "every file outside",
            "no such file",
        ),
        (_edit("Contents/_CodeSignature/Extra", b"x"), "every file outside", "no such file"),
        # site-packages.
        (
            _edit(f"{SITE}/demo/extra.py", b"import os\n"),
            "site-packages",
            "is in no wheel's RECORD",
        ),
        (_edit(f"{SITE}/evil.pth", b"import os\n"), "site-packages", "is in no wheel's RECORD"),
        (
            _edit(f"{SITE}/demo/__init__.py", b"VALUE = 2\n"),
            "site-packages",
            "does not match its RECORD row",
        ),
        (_remove(f"{SITE}/demo/__init__.py"), "site-packages", "which is not installed"),
        # The bytecode's layout.
        (_remove(f"{LIBRARY}/__pycache__/os.cpython-313.pyc"), "checked-hash", "has no .pyc"),
        (
            _edit(f"{LIBRARY}/__pycache__/other.cpython-313.pyc", b"x" * 16),
            "checked-hash",
            "has no source",
        ),
        (_edit(f"{RESOURCES}/stray.pyc", b"x" * 16), "checked-hash", "bytecode outside"),
        # MANIFEST.json: a field changed, added or removed, or the file in another form.
        (
            _rewrite_manifest(_set(("wheels", 0, "sha256"), "0" * 64)),
            "MANIFEST.json is what",
            "wheels[0].sha256",
        ),
        (
            _rewrite_manifest(_set(("wheels", 0, "url"), "https://example.org/demo.whl")),
            "MANIFEST.json is what",
            "wheels[0].url",
        ),
        (
            _rewrite_manifest(_set(("runtime", "removed"), [])),
            "MANIFEST.json is what",
            "runtime.removed",
        ),
        (
            _rewrite_manifest(_set(("runtime", "sha256"), "0" * 64)),
            "MANIFEST.json is what",
            "runtime.sha256",
        ),
        (
            _rewrite_manifest(_set(("host", "wheel", "sha256"), "0" * 64)),
            "MANIFEST.json is what",
            "host.wheel.sha256",
        ),
        (
            _rewrite_manifest(_set(("host", "version"), "9.9.9")),
            "MANIFEST.json is what",
            "host.version",
        ),
        (
            _rewrite_manifest(_set(("app", "bundle_id"), "org.example.other")),
            "MANIFEST.json is what",
            "app.bundle_id",
        ),
        (
            _rewrite_manifest(_set(("build", "xcode"), "1.0")),
            "MANIFEST.json is what",
            "build.xcode",
        ),
        (
            _rewrite_manifest(_set(("ripgrep",), {"commit": "0" * 40})),
            "MANIFEST.json is what",
            "ripgrep",
        ),
        (
            _rewrite_manifest(_set(("signed_by",), "someone")),
            "MANIFEST.json is what",
            "no such field",
        ),
        (
            _rewrite_manifest(lambda data: data["wheels"].clear()),
            "MANIFEST.json is what",
            "MANIFEST.json.wheels is []",
        ),
        (
            lambda app: (app / build_app.MANIFEST_PATH).write_text(
                json.dumps(json.loads((app / build_app.MANIFEST_PATH).read_text("utf-8"))),
                encoding="utf-8",
            ),
            "in the form the build writes",
            "",
        ),
    ],
)
def test_each_change_fails_the_check_that_should_see_it(
    built: Fixture,
    change: Callable[[Path], None],
    check: str,
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert built.verify().passed
    capsys.readouterr()
    change(built.app)
    report = built.verify()
    out = capsys.readouterr().out
    assert not report.passed
    assert any(check in failure for failure in report.checker.failures), out
    assert said in out


def test_a_changed_manifest_also_fails_the_file_comparison(built: Fixture) -> None:
    _rewrite_manifest(_set(("build", "swiftc"), "another swiftc"))(built.app)
    # swiftc is taken as written, so only that field may change and everything still passes.
    assert built.verify().passed
    _rewrite_manifest(_set(("app", "short_version"), "9.9.9"))(built.app)
    failures = _failures(built.verify())
    assert "MANIFEST.json is what" in failures and "every file outside" in failures


def test_another_host_wheel_fails(built: Fixture, tmp_path: Path) -> None:
    other = _wheel(
        tmp_path / "other",
        "lucille_host",
        "lucille-host",
        HOST_VERSION,
        {"lucille_host/__init__.py": b'__version__ = "0.4.0"\n# not the same\n'},
    )
    failures = _failures(built.verify(host_wheel=other))
    assert "MANIFEST.json is what" in failures and "site-packages holds exactly" in failures


def test_a_host_wheel_for_another_version_is_refused(built: Fixture, tmp_path: Path) -> None:
    other = _wheel(tmp_path / "other", "lucille_host", "lucille-host", "0.5.0", {})
    report = built.verify(host_wheel=other)
    assert "the wheels and the runtime archive" in _failures(report)


def test_offline_needs_a_host_wheel(built: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    assert not built.verify(host_wheel=None).passed
    assert "--offline needs --host-wheel" in capsys.readouterr().out


def test_a_cached_file_that_is_not_the_pinned_one_is_refused_and_kept(
    built: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    for path in (built.demo_wheel, built.archive):
        good = path.read_bytes()
        path.write_bytes(good + b"x")
        assert not built.verify().passed
        out = capsys.readouterr().out
        assert f"{path}: its sha256 is" in out
        assert path.read_bytes() == good + b"x"
        path.write_bytes(good)
    assert built.verify().passed


def test_a_cached_file_that_is_missing_is_refused_offline(
    built: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    built.archive.unlink()
    assert not built.verify().passed
    assert f"lacks {built.archive.name}" in capsys.readouterr().out


def test_another_source_fails(built: Fixture) -> None:
    # Another pin: the Xcode the build runs.
    pins = dataclasses.replace(built.source.pins, xcode="99.0")
    assert "MANIFEST.json is what" in _failures(
        built.verify(source=dataclasses.replace(built.source, pins=pins))
    )
    # A prune list that keeps the headers: the build would refuse that runtime, and so does the
    # reference.
    source = dataclasses.replace(built.source, prune=PRUNE[:1] + PRUNE[2:])
    assert "the reference app is made from the source" in _failures(built.verify(source=source))
    # A lock that pins other bytes for the wheel.
    lock = built.source.lock_text.replace(payload.sha256_of(built.demo_wheel), "0" * 64)
    assert "the wheels and the runtime archive" in _failures(
        built.verify(source=dataclasses.replace(built.source, lock_text=lock))
    )
    # Another host version: the version rule, the host wheel's name and the manifest.
    assert not built.verify(source=dataclasses.replace(built.source, host_version="0.4.1")).passed


@pytest.mark.parametrize(
    "change",
    [
        _set(("build", "build_number"), None),
        _set(("build", "tag"), "v0.4.0"),
        _set(("build", "build_number"), "0"),
    ],
)
def test_a_build_the_version_rule_refuses_fails(built: Fixture, change: Any) -> None:
    _rewrite_manifest(change)(built.app)
    failures = _failures(built.verify())
    assert "the app's versions" in failures or "MANIFEST.json is what" in failures


def test_a_manifest_that_does_not_parse_fails(built: Fixture) -> None:
    (built.app / build_app.MANIFEST_PATH).write_text("{", encoding="utf-8")
    assert "MANIFEST.json can be read" in _failures(built.verify())


def test_manifest_problems_names_each_path() -> None:
    expected = {"a": {"b": 1, "c": [1, 2]}, "d": None}
    assert verify_app.manifest_problems(expected, expected) == []
    found = {"a": {"b": 2, "c": [1, 3], "x": 0}}
    assert verify_app.manifest_problems(found, expected) == [
        "MANIFEST.json.a.b is 2; the build writes 1",
        "MANIFEST.json.a.c[1] is 3; the build writes 2",
        "MANIFEST.json.a.x is 0; the build writes no such field",
        "MANIFEST.json.d is missing; the build writes None",
    ]
    assert verify_app.manifest_problems({"c": [1]}, {"c": [1, 2]}) == [
        "MANIFEST.json.c is [1]; the build writes [1, 2]"
    ]


# ---------------------------------------------------------------------------------------------
# The checkout


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "pins.toml").write_text("a\n", encoding="ascii")
    _git(repo, "add", "pins.toml")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_the_checkout_must_be_the_commit_and_unchanged(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    assert verify_app.checkout_problems(repo, head[:12]) == []
    assert verify_app.checkout_problems(repo, head) == []
    # Untracked files, such as build/, are no change.
    (repo / "build").mkdir()
    (repo / "build" / "x").write_text("x", encoding="ascii")
    assert verify_app.checkout_problems(repo, head[:12]) == []
    (problem,) = verify_app.checkout_problems(repo, "0123456789ab")
    assert f"this checkout is at {head[:12]}" in problem
    assert "git checkout 0123456789ab" in problem
    (repo / "pins.toml").write_text("b\n", encoding="ascii")
    (problem,) = verify_app.checkout_problems(repo, head[:12])
    assert "changes to tracked files (pins.toml)" in problem
    _git(repo, "add", "pins.toml")
    assert verify_app.checkout_problems(repo, head[:12])


@pytest.mark.parametrize(
    "commit", [None, "", "xyz", "0123", "0123456", "0123456789a", "0123456789AB", "0" * 41, 12]
)
def test_a_commit_that_is_not_a_commit_id_is_refused(tmp_path: Path, commit: object) -> None:
    repo, _ = _repo(tmp_path)
    (problem,) = verify_app.checkout_problems(repo, commit)
    assert "not a commit id" in problem


def test_a_folder_that_is_not_a_checkout_is_refused(tmp_path: Path) -> None:
    (problem,) = verify_app.checkout_problems(tmp_path, "0123456789ab")
    assert "is not a git checkout" in problem


def test_no_checkout_is_a_failed_check_and_exits_1(
    built: Fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The docstring's exit codes: no checkout is the source check failing (1), not a usage
    # error (2), and nothing after it runs.
    elsewhere = tmp_path / "not-a-checkout"
    elsewhere.mkdir()
    monkeypatch.setattr(build_app, "REPO_ROOT", elsewhere)
    monkeypatch.setattr(
        verify_app, "source_from_checkout", lambda: pytest.fail("read the checkout")
    )
    assert verify_app.main([str(built.app)], on_mac=False) == 1
    out = capsys.readouterr().out
    assert "FAIL: this checkout is the source of the app" in out
    assert "is not a git checkout" in out


# ---------------------------------------------------------------------------------------------
# The host wheel from PyPI


def _pypi(files: Mapping[str, bytes], listed: Mapping[str, str] | None = None) -> Any:
    name = payload.host_wheel_name(HOST_VERSION)
    data = files.get(name, b"")
    entries = [
        {
            "filename": name,
            "url": f"https://files.pythonhosted.org/packages/ab/{name}",
            "digests": {"sha256": (listed or {}).get(name, hashlib.sha256(data).hexdigest())},
        }
    ]
    seen: list[str] = []

    def fetch(url: str, headers: Mapping[str, str]) -> bytes:
        seen.append(url)
        if url == verify_app.PYPI_JSON.format(project="lucille-host", version=HOST_VERSION):
            return json.dumps({"info": {"version": HOST_VERSION}, "urls": entries}).encode()
        if url == entries[0]["url"]:
            return data
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    fetch.seen = seen  # type: ignore[attr-defined]
    return fetch


def test_the_host_wheel_comes_from_pypi_with_its_listed_sha256(tmp_path: Path) -> None:
    name = payload.host_wheel_name(HOST_VERSION)
    fetch = _pypi({name: b"the wheel"})
    wheel = verify_app.pypi_host_wheel(HOST_VERSION, tmp_path, fetch)
    assert wheel.sha256 == hashlib.sha256(b"the wheel").hexdigest()
    assert wheel.path.read_bytes() == b"the wheel" and wheel.filename == name
    assert fetch.seen == [
        f"https://pypi.org/pypi/lucille-host/{HOST_VERSION}/json",
        f"https://files.pythonhosted.org/packages/ab/{name}",
    ]


def test_a_host_wheel_pypi_serves_with_other_bytes_is_refused(tmp_path: Path) -> None:
    name = payload.host_wheel_name(HOST_VERSION)
    fetch = _pypi({name: b"the wheel"}, {name: "0" * 64})
    with pytest.raises(verify_app.VerifyError, match="not its 0000"):
        verify_app.pypi_host_wheel(HOST_VERSION, tmp_path, fetch)
    assert not (tmp_path / "host-wheel" / name).exists()


def test_a_version_pypi_does_not_have_asks_for_host_wheel(tmp_path: Path) -> None:
    fetch = _pypi({})
    with pytest.raises(verify_app.VerifyError, match="needs --host-wheel"):
        verify_app.pypi_host_wheel("9.9.9", tmp_path, fetch)


# ---------------------------------------------------------------------------------------------
# Disk images and the command line


def test_a_disk_image_must_hold_the_app_and_the_link(tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    (mount / f"{APP_NAME}.app").mkdir(parents=True)
    (mount / "Applications").symlink_to("/Applications")
    assert verify_app.image_problems(mount, APP_NAME) == ([], [])
    (mount / ".background").mkdir()
    assert verify_app.image_problems(mount, APP_NAME) == ([], [".background"])
    (mount / "Applications").unlink()
    (mount / "Applications").symlink_to("/tmp")
    problems, _ = verify_app.image_problems(mount, APP_NAME)
    assert problems == ["the image holds no link Applications to /Applications"]


def test_the_disk_image_is_mounted_read_only_and_out_of_sight() -> None:
    argv = verify_app.attach_argv(Path("/x/a.dmg"), Path("/m"))
    assert argv[0] == "attach" and argv[-1] == "/x/a.dmg"
    assert {"-readonly", "-nobrowse", "-noautoopen"} <= set(argv)
    assert argv[argv.index("-mountpoint") + 1] == "/m"


def test_a_disk_image_off_a_mac_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dmg = tmp_path / "Capsule.dmg"
    dmg.write_bytes(b"koly")
    assert verify_app.main([str(dmg)], on_mac=False) == 2
    assert "only on a Mac" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["missing.app", "file.app", "folder"])
def test_a_path_that_is_not_an_app_is_a_usage_error(tmp_path: Path, name: str) -> None:
    if name == "file.app":
        (tmp_path / name).write_bytes(b"")
    elif name == "folder":
        (tmp_path / name).mkdir()
    assert verify_app.main([str(tmp_path / name)], on_mac=False) == 2


def test_an_app_from_another_commit_stops_at_the_checkout(
    built: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fixture's commit is not this checkout's HEAD, so main stops before reading anything
    # else, downloads nothing and runs no uv.
    assert verify_app.main([str(built.app), "--offline"], on_mac=False) == 1
    out = capsys.readouterr().out
    assert "FAIL: this checkout is the source of the app" in out
    assert f"the app was built from {COMMIT}" in out


def test_the_reference_runs_nothing_from_the_app() -> None:
    # verify_app never starts the bundled interpreter: the only programs it runs are git, uv
    # (export, through build_app), codesign and hdiutil.
    text = Path(verify_app.__file__).read_text(encoding="utf-8")
    assert "RUNTIME_IN_APP" not in text and "python_in(" not in text
    assert "host_argv" not in text


# ---------------------------------------------------------------------------------------------
# Mach-O files without their signatures


def _macho(vmsize: int, body: bytes = b"code") -> bytes:
    """A 64-bit Mach-O header with one load command, the __LINKEDIT segment, then ``body``."""
    segment = struct.pack(
        "<II16sQQQQiiII", 0x19, 72, b"__LINKEDIT", 0x4000, vmsize, 0x4000, 0x100, 1, 1, 0, 0
    )
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 6, 1, len(segment), 0, 0)
    return header + segment + body


def _fat(*slices: bytes) -> bytes:
    offset = 4096
    table, data = b"", b""
    for one in slices:
        table += struct.pack(">iiIII", 0x0100000C, 0, offset + len(data), len(one), 12)
        data += one + bytes(-len(one) % 4096)
    head = struct.pack(">II", 0xCAFEBABE, len(slices)) + table
    return head + bytes(offset - len(head)) + data


def test_the_vmsize_of_linkedit_is_found_in_thin_and_universal_files() -> None:
    thin = _macho(0x10000)
    assert verify_app.linkedit_vmsize_offsets(thin) == [32 + 32]
    assert struct.unpack_from("<Q", thin, 64) == (0x10000,)
    fat = _fat(_macho(1), _macho(2))
    found = verify_app.linkedit_vmsize_offsets(fat)
    assert [struct.unpack_from("<Q", fat, at)[0] for at in found] == [1, 2]
    for other in (b"", b"\xcf\xfa\xed\xfe", MACHO + b" text", b"#!/bin/sh\n", thin[:70]):
        assert verify_app.linkedit_vmsize_offsets(other) == []


def test_without_signatures_only_the_vmsize_of_linkedit_may_differ() -> None:
    same = verify_app.compare_without_signatures
    assert same(_macho(0x10000), _macho(0x10000)) == verify_app.SAME
    assert same(_macho(0x14000), _macho(0x10000)) == verify_app.SAME_BUT_VMSIZE
    assert same(_macho(0x14000, b"code"), _macho(0x10000, b"cods")) is None
    assert same(_macho(0x10000, b"code"), _macho(0x10000, b"code!")) is None
    assert same(MACHO + b"a", MACHO + b"b") is None


def _overlapping(second_cmd: int) -> bytes:
    """A 64-bit Mach-O file whose first load command, __LINKEDIT, says it is 36 bytes long, so
    the second command starts inside the first's vmsize: the vmsize's last 4 bytes are the
    second command's ``cmd``. With ``second_cmd`` 0x19 (LC_SEGMENT_64) the second command is a
    __LINKEDIT segment too, and its vmsize is a second place; with 0x1 (LC_SEGMENT) it is not."""
    first = struct.pack("<II16sQ", 0x19, 36, b"__LINKEDIT", 0x4000) + struct.pack("<I", 0x10000)
    second = struct.pack(
        "<II16sQQQQiiII", second_cmd, 72, b"__LINKEDIT", 0x8000, 0x4000, 0, 0, 1, 1, 0, 0
    )
    commands = first + second
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 6, 2, len(commands), 0, 0)
    return header + commands + b"code"


def test_the_vmsize_may_differ_only_where_both_files_have_it() -> None:
    # The guard on the places: each file's load commands must put the vmsize of __LINKEDIT at
    # the same places. These two differ only inside the first file's one vmsize place, but
    # there the second file has a load command of another kind, which puts a second vmsize
    # elsewhere, so the two files do not describe the same segments. Setting aside the first
    # file's places alone would call them SAME.
    ours, theirs = _overlapping(0x1), _overlapping(0x19)
    assert len(ours) == len(theirs)
    differ = [at for at in range(len(ours)) if ours[at] != theirs[at]]
    (place,) = verify_app.linkedit_vmsize_offsets(ours)
    assert differ and all(place <= at < place + 8 for at in differ)
    assert verify_app.linkedit_vmsize_offsets(theirs) == [place, 32 + 36 + 32]
    assert verify_app.compare_without_signatures(ours, theirs) is None
    assert verify_app.compare_without_signatures(theirs, ours) is None
    # Nor may the files differ in length, or have no place at all.
    assert verify_app.compare_without_signatures(_macho(1), _macho(2) + b"!") is None
    assert verify_app.compare_without_signatures(MACHO + b"a", MACHO + b"b") is None


def _fake_strip(path: Path) -> str | None:
    """codesign --remove-signature, as this test signs: a trailing signature taken off."""
    data = path.read_bytes()
    path.write_bytes(data.removesuffix(SIGNATURE))
    return None


SIGNATURE = b"\x00<signature>"


def _sign_every_mach_o(app: Path) -> None:
    for path in build_app.macho_files(app):
        path.write_bytes(path.read_bytes() + SIGNATURE)


def test_on_a_mac_each_signed_mach_o_file_must_be_its_references(
    built: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify_app, "codesign_problems", lambda app: ([], "Signature=adhoc"))
    _sign_every_mach_o(built.app)
    report = built.verify(on_mac=True, strip=_fake_strip)
    assert report.passed, _failures(report)
    assert report.not_compared == 0
    rows = [line for line in report.reported if line.startswith("| `")]
    assert rows == [
        f"| `{build_app.RUNTIME_IN_APP}` | SAME |",
        f"| `{LIBRARY}/lib-dynload/_json.cpython-313-darwin.so` | SAME |",
        f"| `{SITE}/demo/_speedups.abi3.so` | SAME |",
    ]
    assert "Signature=adhoc" in report.reported
    # A signed file whose code is not its reference's fails, in site-packages and outside.
    for rel in (build_app.RUNTIME_IN_APP, f"{SITE}/demo/_speedups.abi3.so"):
        path = built.app / rel
        good = path.read_bytes()
        path.write_bytes(good.replace(b" ", b"!", 1))
        failures = _failures(built.verify(on_mac=True, strip=_fake_strip))
        assert "Mach-O files signing rewrote" in failures, rel
        path.write_bytes(good)
    # So does one codesign cannot strip.
    report = built.verify(on_mac=True, strip=lambda path: "exit 1: not signed at all")
    assert "Mach-O files signing rewrote" in _failures(report)
    assert any(
        "not compared: `codesign --remove-signature` failed" in line for line in report.reported
    )


def _last_line(text: str) -> str:
    return text.strip().splitlines()[-1]


def test_off_a_mac_the_last_line_is_the_one_the_docstring_quotes(
    built: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The docstring's words for the last line, with N the count, are what main prints: off a
    # Mac, a pass gives the count of Mach-O files not compared; a failure gives the count of
    # failed checks only.
    doc = " ".join(str(verify_app.__doc__).split())
    monkeypatch.setattr(verify_app, "checkout_problems", lambda repo, commit: [])
    monkeypatch.setattr(verify_app, "source_from_checkout", lambda: built.source)
    argv = [str(built.app), "--offline", "--host-wheel", str(built.host_wheel)]
    argv += ["--cache", str(built.cache)]
    assert verify_app.main(argv, on_mac=False) == 0
    last = _last_line(capsys.readouterr().out)
    said = re.fullmatch(
        r"verify_app.py: every check passed, but ([1-9][0-9]*) Mach-O files were not compared "
        r"with their references: run this on a Mac for that",
        last,
    )
    assert said is not None, last
    assert f"``{last.replace(said[1], 'N', 1)}``" in doc
    (built.app / "Contents/PkgInfo").write_bytes(b"APPLxxxx")
    assert verify_app.main(argv, on_mac=False) == 1
    last = _last_line(capsys.readouterr().out)
    assert last == "verify_app.py: 1 checks failed"
    assert "``verify_app.py: N checks failed``" in doc


def test_a_copy_that_differs_only_in_the_vmsize_of_linkedit_passes_and_says_so() -> None:
    # Through stripped_verdicts, as the Mac path calls it: the vmsize of __LINKEDIT is the one
    # field set aside, and the verdict says it was; any other byte is DIFFERENT and not the same.
    def strip(path: Path) -> str | None:
        return _fake_strip(path)

    pairs = [
        ("vmsize", _macho(0x14000) + SIGNATURE, _macho(0x10000)),
        ("same", _macho(0x10000) + SIGNATURE, _macho(0x10000)),
        ("code", _macho(0x14000, b"code!") + SIGNATURE, _macho(0x10000)),
    ]
    verdicts = {v.rel: v for v in verify_app.stripped_verdicts(pairs, strip)}
    assert verdicts["vmsize"].same and verdicts["vmsize"].text == verify_app.SAME_BUT_VMSIZE
    assert verdicts["same"].same and verdicts["same"].text == verify_app.SAME
    assert not verdicts["code"].same and verdicts["code"].text.startswith("DIFFERENT")


def test_on_a_mac_the_signature_must_verify(
    built: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        verify_app, "codesign_problems", lambda app: (["codesign --verify fails: modified"], "")
    )
    failures = _failures(built.verify(on_mac=True, strip=_fake_strip))
    assert "codesign --verify --deep --strict passes on the app" in failures
