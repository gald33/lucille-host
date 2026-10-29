"""The parts of macos/build_app.py that run anywhere: the pins, the sha256 gate, Info.plist,
and the rules the Swift sources keep. The build itself runs on a Mac, in the macos-app workflow.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import plistlib
import re
import sys
from pathlib import Path

import pytest

import lucille_host
from lucille_host import policy
from macos import build_app, check_app
from tools.guards import ast_lint

PINS_TEXT = build_app.PINS_FILE.read_text(encoding="utf-8")
SWIFT_FILES = sorted(build_app.SWIFT_DIR.glob("*.swift"))


def test_the_pins_are_well_formed() -> None:
    pins = build_app.load_pins()
    assert pins.python_version.startswith("3.13.")
    assert pins.asset.startswith(f"cpython-{pins.python_version}+")
    assert pins.asset.endswith("-aarch64-apple-darwin-install_only_stripped.tar.gz")
    assert pins.url.startswith(build_app.PBS_RELEASES)
    assert pins.url.endswith("/" + pins.asset.replace("+", "%2B"))
    assert re.fullmatch(r"[0-9a-f]{64}", pins.sha256)
    assert pins.app_name == "Lucille Host"
    assert pins.minimum_macos == "15.0"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('sha256 = "8392', 'sha256 = "8392Z'),
        ('sha256 = "8392b6ea', 'sha256 = "8392B6EA'),
        ("releases/download/20261026/", "releases/download/20261020/"),
        ("https://github.com/astral-sh/", "https://example.com/astral-sh/"),
        ('version = "3.13.18"', 'version = "3.13.17"'),
        (
            'aarch64-apple-darwin-install_only_stripped.tar.gz"\nurl',
            'x86_64-apple-darwin-install_only_stripped.tar.gz"\nurl',
        ),
        ('minimum_macos = "15.0"', 'minimum_macos = "12.0"'),
        ('bundle_id = "com.lucille-ai.host"', 'bundle_id = "Lucille Host"'),
        ('category = "public.app-category.utilities"', 'category = "utilities"'),
        ("[app]", '[app]\nextra = "x"'),
        ("[app]", "[apps]"),
    ],
)
def test_a_changed_pin_is_refused(old: str, new: str) -> None:
    assert old in PINS_TEXT
    with pytest.raises(build_app.PinError):
        build_app.parse_pins(PINS_TEXT.replace(old, new, 1))


def test_a_runtime_whose_sha256_differs_is_refused(tmp_path: Path) -> None:
    pins = build_app.load_pins()
    archive = tmp_path / pins.asset
    archive.write_bytes(b"not the pinned runtime")
    with pytest.raises(build_app.BuildError, match="refusing to build"):
        build_app.fetch_runtime(pins, tmp_path / "cache", archive)
    # A cached download that does not match is refused and deleted, so the next build fetches it
    # again; no network is needed to reach the check, because the file is already there.
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / pins.asset
    cached.write_bytes(b"a tampered download")
    with pytest.raises(build_app.BuildError, match="refusing to build"):
        build_app.fetch_runtime(pins, cache)
    assert not cached.exists()


def test_a_runtime_whose_sha256_matches_is_used(tmp_path: Path) -> None:
    content = b"stand-in runtime"
    pins = dataclasses.replace(build_app.load_pins(), sha256=hashlib.sha256(content).hexdigest())
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / pins.asset).write_bytes(content)
    assert build_app.fetch_runtime(pins, cache) == cache / pins.asset


def test_the_version_is_the_packages() -> None:
    assert build_app.host_version() == lucille_host.__version__


@pytest.mark.parametrize(
    ("version", "triple"),
    [
        ("0.1.0.dev0", "0.1.0"),
        ("0.1.0", "0.1.0"),
        ("1.2", "1.2.0"),
        ("3", "3.0.0"),
        ("2.0.1rc1", "2.0.1"),
        ("1.2.3.4", "1.2.3"),
    ],
)
def test_release_triple(version: str, triple: str) -> None:
    assert build_app.release_triple(version) == triple


def test_info_plist() -> None:
    pins = build_app.load_pins()
    plist = build_app.info_plist(pins, "0.1.0.dev0")
    assert plist["LSUIElement"] is True
    assert plist["CFBundleIdentifier"] == pins.bundle_id
    assert plist["CFBundleExecutable"] == plist["CFBundleName"] == pins.app_name
    assert plist["CFBundlePackageType"] == "APPL"
    assert plist["CFBundleShortVersionString"] == plist["CFBundleVersion"] == "0.1.0"
    assert plist["LucilleHostVersion"] == "0.1.0.dev0"
    assert plist["LSMinimumSystemVersion"] == "15.0"
    assert plist["LSArchitecturePriority"] == ["arm64"]
    assert not build_app.FORBIDDEN_PLIST_KEYS & set(plist)
    usage = {key: value for key, value in plist.items() if key.endswith("UsageDescription")}
    assert len(usage) == 5
    for text in usage.values():
        assert not re.search(r"\b(safe|secure|private|protect)", text, re.IGNORECASE)
    assert plistlib.loads(plistlib.dumps(plist)) == plist


def test_plan_writes_the_info_plist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert build_app.main(["plan", "--out", str(tmp_path)]) == 0
    with (tmp_path / "Info.plist").open("rb") as handle:
        written = plistlib.load(handle)
    assert written == build_app.info_plist(build_app.load_pins(), lucille_host.__version__)
    assert build_app.load_pins().sha256 in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "darwin", reason="the refusal is for other systems")
def test_build_refuses_off_a_mac(tmp_path: Path) -> None:
    assert build_app.main(["build", "--out", str(tmp_path)]) == 1
    assert not any(tmp_path.iterdir())


def test_macho_detection(tmp_path: Path) -> None:
    thin = tmp_path / "deep" / "er" / "lib.so"
    thin.parent.mkdir(parents=True)
    thin.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(28))
    fat = tmp_path / "tool"
    fat.write_bytes(b"\xca\xfe\xba\xbe" + bytes(28))
    (tmp_path / "script.py").write_text("print('not native')\n", encoding="ascii")
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "link.so").symlink_to(thin)
    assert build_app.macho_files(tmp_path) == [thin, fat]
    assert build_app.symlinks_in(tmp_path) == [tmp_path / "link.so"]


def _pyc(path: Path, flags: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 4 + flags.to_bytes(4, "little") + bytes(8))


def test_bytecode_problems(tmp_path: Path) -> None:
    lib = tmp_path / build_app.STDLIB
    lib.mkdir(parents=True)
    for name in ("good", "unchecked", "missing"):
        (lib / f"{name}.py").write_text("", encoding="ascii")
    _pyc(lib / "__pycache__" / "good.cpython-313.pyc", 0b11)
    _pyc(lib / "__pycache__" / "unchecked.cpython-313.pyc", 0b01)
    problems = build_app.bytecode_problems(tmp_path)
    assert problems == [
        f"no .pyc for {build_app.STDLIB}/missing.py",
        f"{build_app.STDLIB}/__pycache__/unchecked.cpython-313.pyc is not a hash-checked .pyc",
    ]


def test_the_pruned_modules_are_not_ones_the_runtime_imports() -> None:
    pruned = {
        Path(rel).name.removesuffix(".py")
        for rel in build_app.PRUNED
        if rel.startswith(build_app.STDLIB + "/") and "/lib-dynload/" not in rel
    }
    assert pruned == {
        "config-3.13-darwin",
        "ensurepip",
        "idlelib",
        "tkinter",
        "turtle",
        "turtledemo",
    }
    assert not pruned & ast_lint.RUNTIME_IMPORTS


def test_the_alias_vectors_are_valid_host_aliases() -> None:
    vectors = json.loads(check_app.ALIAS_VECTORS.read_text(encoding="utf-8"))
    folders = [vector["folder"] for vector in vectors]
    assert len(folders) == len(set(folders))
    for vector in vectors:
        assert set(vector) == {"folder", "alias"}
        assert policy.valid_alias(vector["alias"]), vector
    assert any(vector["alias"] == "folder" for vector in vectors)
    assert any(len(vector["alias"]) == 32 for vector in vectors)


def test_the_logic_sources_draw_nothing() -> None:
    for name in check_app.LOGIC_SOURCES:
        text = (build_app.SWIFT_DIR / name).read_text(encoding="utf-8")
        assert "import AppKit" not in text and "import SwiftUI" not in text, name


def test_the_app_starts_only_the_embedded_interpreter() -> None:
    assert SWIFT_FILES
    starting = [path.name for path in SWIFT_FILES if "Process()" in path.read_text("utf-8")]
    assert starting == ["HostCLI.swift"]
    cli = (build_app.SWIFT_DIR / "HostCLI.swift").read_text(encoding="utf-8")
    assert '"Contents/Resources/python/bin/python3"' in cli
    assert '["-I", "-B", "-m", "lucille_host"]' in cli
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        for needle in (
            "URLSession",
            "import Network",
            "NWConnection",
            "/bin/sh",
            "/bin/zsh",
            "launchPath",
            "NSAppleScript",
            "UserDefaults",
            "SWITCHBOARD_",
        ):
            assert needle not in text, (path.name, needle)
