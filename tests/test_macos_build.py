"""The parts of tools/macos/build_app.py and tools/macos/check_app.py that run anywhere: the pins,
the sha256 gate, the hashed build backend, Info.plist, the disk image's name, the parsers the
checks use, and the rules the Swift sources keep. The build itself runs on a Mac, in ci.yml's
macos-app job, whose steps tests/test_ci_workflow.py reads.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import lucille_host
from lucille_host import policy
from lucille_host.cli import build_parser
from lucille_host.ops import OPERATIONS
from tools.guards import ast_lint, macos_lint
from tools.macos import build_app, check_app, payload

PINS_TEXT = build_app.PINS_FILE.read_text(encoding="utf-8")
CONSTRAINTS_TEXT = build_app.BUILD_CONSTRAINTS.read_text(encoding="utf-8")
SWIFT_FILES = sorted(build_app.SWIFT_DIR.glob("*.swift"))
WORKFLOW = build_app.REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_the_pins_are_well_formed() -> None:
    pins = build_app.load_pins()
    assert pins.python_version.startswith("3.13.")
    assert pins.asset.startswith(f"cpython-{pins.python_version}+")
    assert pins.asset.endswith("-aarch64-apple-darwin-install_only_stripped.tar.gz")
    assert pins.url.startswith(build_app.PBS_RELEASES)
    assert pins.url.endswith("/" + pins.asset.replace("+", "%2B"))
    assert re.fullmatch(r"[0-9a-f]{64}", pins.sha256)
    assert pins.app_name == "Capsule"
    assert pins.bundle_id == "com.lucille-ai.capsule"
    assert pins.runtime_id == "com.lucille-ai.capsule.runtime"
    assert pins.deployment_target == "15.0"
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", pins.xcode)
    assert pins.ripgrep_commit == ""
    assert isinstance(pins.adhoc_hardened_runtime, bool)
    assert build_app.PINS_FILE == build_app.TOOLS_DIR / "pins.toml"
    assert (
        build_app.PINS_FILE.relative_to(build_app.REPO_ROOT).as_posix() == "tools/macos/pins.toml"
    )


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
        ('deployment_target = "15.0"', 'deployment_target = "14.0"'),
        ('deployment_target = "15.0"', 'deployment_target = "15"'),
        ('deployment_target = "15.0"', 'minimum_macos = "15.0"'),
        ('bundle_id = "com.lucille-ai.capsule"', 'bundle_id = "Capsule"'),
        (
            'runtime_id = "com.lucille-ai.capsule.runtime"',
            'runtime_id = "com.lucille-ai.capsule.py"',
        ),
        ('runtime_id = "com.lucille-ai.capsule.runtime"', 'runtime_id = ""'),
        ('version = "16.4"', 'version = "latest"'),
        ('version = "16.4"', 'version = "16.4\\nEVIL=1"'),
        ('version = "16.4"', "version = 16.4"),
        ("[xcode]", "[xcodes]"),
        ('commit = ""', 'commit = "main"'),
        ('commit = ""', 'commit = "4466aba"'),
        ('commit = ""', "commit = false"),
        ("adhoc_hardened_runtime = true", 'adhoc_hardened_runtime = "yes"'),
        ("adhoc_hardened_runtime = true", "adhoc_hardened_runtime = 1"),
        ("[signing]", "[signing]\nnotarize = false"),
        ("[ripgrep]", '[extra]\nx = "y"\n\n[ripgrep]'),
        ('name = "Capsule"', 'name = "Capsule/Capsule"'),
        ('name = "Capsule"', 'name = " Capsule"'),
        ('name = "Capsule"', 'name = "Capsule  Two"'),
        ('name = "Capsule"', 'name = "C*"'),
        ('name = "Capsule"', 'name = "Cap?ule"'),
        ('name = "Capsule"', 'name = "Cap[s]ule"'),
        ('name = "Capsule"', 'name = "Capsule:"'),
        ('name = "Capsule"', 'name = ""'),
        ('category = "public.app-category.utilities"', 'category = "utilities"'),
        ("[app]", '[app]\nextra = "x"'),
        ("[app]", "[apps]"),
    ],
)
def test_a_changed_pin_is_refused(old: str, new: str) -> None:
    assert old in PINS_TEXT
    with pytest.raises(build_app.PinError):
        build_app.parse_pins(PINS_TEXT.replace(old, new, 1))


def test_a_pins_file_without_a_table_is_refused() -> None:
    for table in ("xcode", "ripgrep", "signing"):
        text = PINS_TEXT.replace(f"[{table}]", "#", 1)
        with pytest.raises(build_app.PinError):
            build_app.parse_pins(text)
    # A pin the build reads, with no other change, still parses: the refusals above are each
    # for the one change they make.
    assert build_app.parse_pins(PINS_TEXT.replace('commit = ""', 'commit = "' + "a" * 40 + '"'))
    off = build_app.parse_pins(PINS_TEXT.replace("= true", "= false"))
    assert off.adhoc_hardened_runtime is False


def _host_wheel(folder: Path) -> tuple[Path, str]:
    """A file named as this checkout's host wheel, and its sha256. The checks that read it run
    before anything else, so it need not be a real wheel."""
    folder.mkdir(parents=True, exist_ok=True)
    wheel = folder / payload.host_wheel_name(build_app.host_version())
    wheel.write_bytes(b"a stand-in for the host wheel")
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def test_runtime_sha256_mismatch_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pins = build_app.load_pins()
    archive = tmp_path / pins.asset
    archive.write_bytes(b"not the pinned runtime")
    with pytest.raises(
        build_app.BuildError, match=r"tools/macos/pins\.toml pins .*refusing to build"
    ):
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
    # The commands refuse other bytes before any work, on any system: nothing is written.
    wheel, digest = _host_wheel(tmp_path / "wheel")
    for command in ("assemble", "build"):
        out = tmp_path / f"out-{command}"
        argv = [command, "--out", str(out), "--python-archive", str(archive)]
        argv += ["--host-wheel", str(wheel), "--host-wheel-sha256", digest, "--build-number", "1"]
        assert build_app.main(argv) == 1
        err = capsys.readouterr().err
        assert "tools/macos/pins.toml pins" in err and "refusing to build" in err, err
        assert not out.exists()


def test_host_wheel_sha256_mismatch_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel, digest = _host_wheel(tmp_path / "wheel")
    other = tmp_path / "wheel" / "lucille_host-9.9.9-py3-none-any.whl"
    other.write_bytes(wheel.read_bytes())
    cases = [
        (wheel, "0" * 64, f"not the {'0' * 64} given; refusing to build"),
        (wheel, digest.upper(), "must be 64 lower-case hexadecimal digits"),
        (other, digest, f"must be a file named {wheel.name}; refusing to build"),
        (tmp_path / "missing" / wheel.name, digest, "refusing to build"),
    ]
    for path, sha, said in cases:
        out = tmp_path / "out"
        argv = ["build", "--out", str(out), "--host-wheel", str(path)]
        argv += ["--host-wheel-sha256", sha, "--build-number", "1"]
        assert build_app.main(argv) == 1
        assert said in capsys.readouterr().err
        assert not out.exists()


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


# The six folder usage keys (0002 §12: Documents, Desktop, Downloads, removable and network
# volumes, and file providers), each name as Apple's Information Property List reference spells it.
SIX_USAGE_KEYS = {
    "NSDocumentsFolderUsageDescription",
    "NSDesktopFolderUsageDescription",
    "NSDownloadsFolderUsageDescription",
    "NSRemovableVolumesUsageDescription",
    "NSNetworkVolumesUsageDescription",
    "NSFileProviderDomainUsageDescription",
}


def test_plist_keys() -> None:
    pins = build_app.load_pins()
    plist = build_app.info_plist(pins, build_app.bundle_versions("0.1.0.dev0", None, "523"))
    assert set(plist) == {
        "CFBundleDevelopmentRegion",
        "CFBundleDisplayName",
        "CFBundleExecutable",
        "CFBundleIdentifier",
        "CFBundleInfoDictionaryVersion",
        "CFBundleName",
        "CFBundlePackageType",
        "CFBundleShortVersionString",
        "CFBundleVersion",
        "LSApplicationCategoryType",
        "LSArchitecturePriority",
        "LSMinimumSystemVersion",
        "LSUIElement",
        "NSHumanReadableCopyright",
        "NSSupportsAutomaticTermination",
        "NSSupportsSuddenTermination",
        *SIX_USAGE_KEYS,
    }
    assert plist["LSUIElement"] is True
    assert plist["CFBundleIdentifier"] == pins.bundle_id
    assert plist["CFBundleExecutable"] == plist["CFBundleName"] == pins.app_name
    # The app reads its name from this key (HostText.appName).
    assert plist["CFBundleDisplayName"] == pins.app_name
    assert plist["CFBundlePackageType"] == "APPL"
    assert plist["CFBundleShortVersionString"] == "0.1.0"
    assert plist["CFBundleVersion"] == "523"
    assert plist["LSMinimumSystemVersion"] == "15.0" == pins.deployment_target
    assert plist["LSArchitecturePriority"] == ["arm64"]
    assert plist["NSSupportsAutomaticTermination"] is False
    assert plist["NSSupportsSuddenTermination"] is False
    # No URL scheme, no App Transport Security exception, no scripting, no update feed.
    assert not build_app.FORBIDDEN_PLIST_KEYS & set(plist)
    assert {"CFBundleURLTypes", "CFBundleURLSchemes", "NSAppTransportSecurity"} <= (
        build_app.FORBIDDEN_PLIST_KEYS
    )
    usage = {key: value for key, value in plist.items() if key.endswith("UsageDescription")}
    assert set(usage) == set(build_app.FOLDER_USAGE_KEYS) == SIX_USAGE_KEYS
    for text in usage.values():
        assert not re.search(r"\b(safe|secure|private|protect)", text, re.IGNORECASE)
    assert plistlib.loads(plistlib.dumps(plist)) == plist


def _plist_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in (key, *_plist_strings(item))]
    if isinstance(value, list):
        return [s for item in value for s in _plist_strings(item)]
    return []


def test_info_plist_values_make_no_forbidden_claim() -> None:
    # macos_lint reads the plist functions' string constants and the pins' [app] strings; this
    # reads every string the rendered Info.plist holds, whatever it was built from.
    phrases = macos_lint.must_not_claim_phrases(
        (build_app.REPO_ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    )
    for versions in (
        build_app.bundle_versions("0.1.0.dev0", None, "523"),
        build_app.bundle_versions("1.2.3", "v1.2.3", None),
    ):
        strings = _plist_strings(build_app.info_plist(build_app.load_pins(), versions))
        assert build_app.folder_usage(build_app.load_pins().app_name) in strings
        for text in strings:
            assert macos_lint.claims(text, phrases) == [], text
    # The check is not vacuous: a name the pins could hold is caught.
    named = dataclasses.replace(build_app.load_pins(), app_name="Sandboxed Capsule")
    plist = build_app.info_plist(named, build_app.bundle_versions("1.2.3", "v1.2.3", None))
    assert any(macos_lint.claims(text, phrases) for text in _plist_strings(plist))


def test_runtime_flags_match_the_app() -> None:
    # The lint holds the Swift process file to the runtime path and flags the build uses.
    assert macos_lint.RUNTIME_IN_APP == build_app.RUNTIME_IN_APP
    assert macos_lint.RUNTIME_ARGV == (*build_app.RUNTIME_FLAGS, "-m", "lucille_host")
    wrapper_argv = f'{" ".join(macos_lint.RUNTIME_ARGV)} --profile {build_app.APP_PROFILE} "$@"'
    assert wrapper_argv in build_app.WRAPPER_TEXT
    # ... and reads the files the build reads.
    assert macos_lint.PLIST_SOURCE == "tools/macos/build_app.py"
    assert (build_app.REPO_ROOT / macos_lint.PLIST_SOURCE).resolve() == (
        build_app.TOOLS_DIR / "build_app.py"
    )
    assert build_app.REPO_ROOT / macos_lint.PINS_FILE == build_app.PINS_FILE
    assert (build_app.REPO_ROOT / macos_lint.PROCESS_FILE).parent == build_app.SWIFT_DIR
    # The functions whose strings the lint reads are the two that render Info.plist (both are
    # called by name in this file, so a rename fails here and in the lint).
    assert macos_lint.PLIST_FUNCTIONS == ("info_plist", "folder_usage")


def _swift_array(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


def test_the_app_and_its_self_test_run_the_bundled_runtime() -> None:
    # The lint sees only that HostCLI.swift writes the runtime's path and arguments. These lines
    # are where its Process takes them, and the self-test, which the lint does not read for this,
    # expects the same arguments. Both compile only on a Mac, so their text is pinned here.
    cli = (build_app.REPO_ROOT / macos_lint.PROCESS_FILE).read_text(encoding="utf-8")
    assert (
        "static func python(inApp app: URL) -> URL {\n"
        f'        app.appendingPathComponent("{build_app.RUNTIME_IN_APP}")\n'
        "    }"
    ) in cli
    # The runtime's arguments, then the app's own profile (0002 §9), then the verb.
    assert (
        "func arguments(for verb: [String]) -> [String] {\n"
        f'        {_swift_array(macos_lint.RUNTIME_ARGV)} + ["--profile", profile] + verb\n'
        "    }"
    ) in cli
    assert "process.executableURL = python\n" in cli
    assert "process.arguments = arguments(for: verb)\n" in cli
    selftest = (build_app.MACOS_DIR / "Tests" / "SelfTest.swift").read_text(encoding="utf-8")
    expected = _swift_array((*macos_lint.RUNTIME_ARGV, "--profile", "mac-app", "pause"))
    assert f'cli.arguments(for: ["pause"]) == {expected}' in selftest
    assert "HostCLI(python: HostCLI.python(inApp: app), home: home)" in selftest
    # The self-test's stand-in for the interpreter skips exactly those arguments before the verb.
    skipped = len(macos_lint.RUNTIME_ARGV) + 2
    assert f"\n            shift {skipped}\n" in selftest
    assert selftest.count("\n            shift ") == 1
    for source in (cli, selftest):
        assert "python/bin/python3" not in source
        assert '["-I", "-B", "-m"' not in source


@pytest.mark.parametrize(
    ("version", "build_number", "short"),
    [("0.1.0.dev0", "523", "0.1.0"), ("1.2", "7", "1.2.0"), ("2.0.1rc1", "1", "2.0.1")],
)
def test_version_rule_maps_dev_builds(version: str, build_number: str, short: str) -> None:
    versions = build_app.bundle_versions(version, None, build_number)
    assert (versions.short, versions.build) == (short, build_number)
    plist = build_app.info_plist(build_app.load_pins(), versions)
    assert plist["CFBundleShortVersionString"] == short
    assert plist["CFBundleVersion"] == build_number
    for bad in ("0", "", "x", "-1", "1.2", "01", " 7", "7\n"):
        with pytest.raises(build_app.BuildError):
            build_app.bundle_versions(version, None, bad)
    with pytest.raises(build_app.BuildError, match="pass --build-number"):
        build_app.bundle_versions(version, None, None)


def test_version_rule_refuses_suffix_on_tag_builds() -> None:
    versions = build_app.bundle_versions("0.1.0", "v0.1.0", None)
    assert (versions.short, versions.build) == ("0.1.0", "0.1.0")
    assert build_app.bundle_versions("12.0.3", "v12.0.3", None).build == "12.0.3"
    for version in ("0.1.0.dev0", "0.1.0+mac1", "0.1.0rc1", "0.1.0.post1", "0.1", "0.1.0.0"):
        with pytest.raises(build_app.BuildError, match="three numbers with no suffix"):
            build_app.bundle_versions(version, f"v{version}", None)
    for tag in ("0.1.0", "v0.1.1", "v0.1.0 ", "V0.1.0", "v0.1.0+mac1"):
        with pytest.raises(build_app.BuildError):
            build_app.bundle_versions("0.1.0", tag, None)
    with pytest.raises(build_app.BuildError, match="not both"):
        build_app.bundle_versions("0.1.0", "v0.1.0", "7")


@pytest.mark.parametrize("command", ["plan", "assemble", "build"])
def test_version_flags_are_exclusive(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [command, "--out", str(tmp_path / "out")]
    if command != "plan":
        argv += ["--host-wheel", str(tmp_path / "w.whl"), "--host-wheel-sha256", "0" * 64]
    with pytest.raises(SystemExit) as raised:
        build_app.main([*argv, "--tag", "v0.1.0", "--build-number", "1"])
    assert raised.value.code == 2
    assert "argument --build-number: not allowed with argument --tag" in capsys.readouterr().err
    if command != "plan":
        with pytest.raises(SystemExit) as raised:
            build_app.main(argv)
        assert raised.value.code == 2
        err = capsys.readouterr().err
        assert "one of the arguments --tag --build-number is required" in err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "key",
    [
        "NSDocumentsFolderUsageDescription",
        "NSDesktopFolderUsageDescription",
        "NSDownloadsFolderUsageDescription",
        "NSRemovableVolumesUsageDescription",
        "NSNetworkVolumesUsageDescription",
        "NSFileProviderDomainUsageDescription",
    ],
)
def test_each_folder_question_says_what_the_app_is_for(key: str) -> None:
    # macOS is meant to show this text under its question about a protected folder or volume (not
    # yet tested on a Mac). It names Lucille, says that the user chooses, and says what Lucille
    # sees without being asked: host.describe is on by default.
    versions = build_app.BundleVersions("0.1.0", "1")
    text = build_app.info_plist(build_app.load_pins(), versions)[key]
    assert isinstance(text, str) and text.strip()
    assert text.startswith(f"{build_app.load_pins().app_name} lets Lucille ")
    assert "the folders you choose" in text and "abilities you turn on" in text
    assert "which abilities are on" in text and "names you gave your folders" in text
    assert [name for name, op in OPERATIONS.items() if op.default_enabled] == ["host.describe"]


def test_plan_writes_the_info_plist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert build_app.main(["plan", "--out", str(tmp_path), "--build-number", "41"]) == 0
    with (tmp_path / "Info.plist").open("rb") as handle:
        written = plistlib.load(handle)
    versions = build_app.bundle_versions(lucille_host.__version__, None, "41")
    assert written == build_app.info_plist(build_app.load_pins(), versions)
    out = capsys.readouterr().out
    assert build_app.load_pins().sha256 in out and "placeholder" not in out
    # Without a build number the bundle version is 1, and plan says it is a placeholder.
    assert build_app.main(["plan", "--out", str(tmp_path)]) == 0
    assert "placeholder" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "darwin", reason="the refusal is for other systems")
def test_build_refuses_off_a_mac(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Valid arguments, so that the platform is what stops it: nothing is written first.
    wheel, digest = _host_wheel(tmp_path / "wheel")
    for command in ("assemble", "build"):
        out = tmp_path / "out"
        argv = [command, "--out", str(out), "--host-wheel", str(wheel)]
        argv += ["--host-wheel-sha256", digest, "--build-number", "1"]
        assert build_app.main(argv) == 1
        assert "runs on an Apple silicon Mac" in capsys.readouterr().err
        assert not out.exists()
    github_env = tmp_path / "github-env"
    assert build_app.main(["toolchain", "--github-env", str(github_env)]) == 1
    assert "runs on an Apple silicon Mac" in capsys.readouterr().err
    assert not github_env.exists()


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


def test_nothing_is_precompiled() -> None:
    # No .pyc is shipped (0002 §6): the build compiles nothing, so the functions that did are
    # gone, and neither build script runs compileall.
    for name in (
        "compile_bytecode",
        "bytecode_problems",
        "install_packages",
        "remove_after_install",
    ):
        assert not hasattr(build_app, name), name
    for script in ("build_app.py", "payload.py"):
        assert "compileall" not in (build_app.TOOLS_DIR / script).read_text(encoding="utf-8")
    assert "--no-compile" in payload.install_argv(Path("t"), Path("f"), Path("r"), "3.13.18")


def test_the_pruned_modules_are_not_ones_the_runtime_imports() -> None:
    pruned = {
        Path(rel).name.removesuffix(".py")
        for rel in build_app.load_prune_list()
        if rel.startswith(build_app.STDLIB + "/")
        and "/lib-dynload/" not in rel
        and "/site-packages/" not in rel
        and "__pycache__" not in rel
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
    cases = [(vector["folder"], tuple(vector.get("taken", ()))) for vector in vectors]
    assert len(cases) == len(set(cases))
    for vector in vectors:
        assert set(vector) in ({"folder", "alias"}, {"folder", "taken", "alias"})
        assert policy.valid_alias(vector["alias"]), vector
        assert vector["alias"] not in vector.get("taken", ()), vector
        assert all(policy.valid_alias(alias) for alias in vector.get("taken", ())), vector
    assert any(vector["alias"] == "folder" for vector in vectors)
    assert sum(len(vector["alias"]) == 32 for vector in vectors) >= 2
    assert any(vector["alias"].endswith("-10") for vector in vectors)


# What a Swift file may import. The logic files run in the self-test without a screen, so they
# use Foundation alone; the app's other files add AppKit and SwiftUI, and nothing else.
_SWIFT_IMPORT = re.compile(
    r"^\s*(?:@\w+\s+)*import\s+(?:(?:typealias|struct|class|enum|protocol|let|var|func)\s+)?"
    r"(\w+)",
    re.MULTILINE,
)
_LOGIC_IMPORTS = {"Foundation", "Darwin", "Glibc"}
_APP_IMPORTS = _LOGIC_IMPORTS | {"AppKit", "SwiftUI"}


def _swift_imports(text: str) -> set[str]:
    return set(_SWIFT_IMPORT.findall(text))


def test_the_swift_import_scan_sees_each_form() -> None:
    text = "import Cocoa\n@_exported import Network\nimport struct WebKit.WKWebView\n  import os\n"
    assert _swift_imports(text) == {"Cocoa", "Network", "WebKit", "os"}


def test_the_logic_sources_draw_nothing() -> None:
    for name in check_app.LOGIC_SOURCES:
        text = (build_app.SWIFT_DIR / name).read_text(encoding="utf-8")
        assert _swift_imports(text) <= _LOGIC_IMPORTS, name


def test_the_self_test_compiles_every_file_that_draws_nothing() -> None:
    # The self-test compiles LOGIC_SOURCES and nothing else of the app. Every file that imports
    # no UI framework is in it, so its code runs on every build; no file that draws is.
    drawless = {
        path.name
        for path in SWIFT_FILES
        if _swift_imports(path.read_text(encoding="utf-8")) <= _LOGIC_IMPORTS
    }
    assert drawless == set(check_app.LOGIC_SOURCES)
    assert len(check_app.LOGIC_SOURCES) == len(drawless)


def test_the_app_imports_only_apples_ui_frameworks() -> None:
    assert SWIFT_FILES
    for path in SWIFT_FILES:
        assert _swift_imports(path.read_text(encoding="utf-8")) <= _APP_IMPORTS, path.name


# What the Swift sources may not do (start a process other than the bundled runtime, use the
# network, read the pasteboard, ...) is tools/guards/macos_lint.py's, tested in
# tests/guards/test_macos_lint.py.


def _swift_string_table(text: str, name: str) -> dict[str, str]:
    body = re.search(rf"static let {name}: \[String: String\] = \[(.*?)\]", text, re.DOTALL)
    assert body is not None, name
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', body.group(1)))


def _swift_string_set(text: str, name: str) -> set[str]:
    body = re.search(rf"static let {name}: Set<String> = \[(.*?)\]", text, re.DOTALL)
    assert body is not None, name
    return set(re.findall(r'"([^"]+)"', body.group(1)))


def test_every_operation_has_plain_words_or_is_left_out_of_the_menu() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    titles = _swift_string_table(text, "abilityTitles")
    unlisted = _swift_string_set(text, "unlistedAbilities")
    assert not set(titles) & unlisted
    assert set(titles) | unlisted == set(OPERATIONS)
    assert {"host.sleep", "host.describe"} <= unlisted
    assert titles == check_app.ABILITY_TITLES
    for title in titles.values():
        assert title[0].isupper() and "." not in title, title


def test_every_listed_ability_says_what_lucille_gets() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    details = _swift_string_table(text, "abilityDetails")
    assert set(details) == set(_swift_string_table(text, "abilityTitles"))
    assert details == check_app.ABILITY_DETAILS
    for detail in details.values():
        assert detail.startswith("Lucille gets ") and detail.endswith("."), detail


def _named_in_swift(words: str, reference: str = "appName") -> str:
    """``words`` as the Swift source spells them: the app's name is read from the bundle there."""
    return words.replace(check_app.APP_NAME, f"\\({reference})")


def test_the_ui_check_looks_for_the_apps_own_words() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    welcome = _named_in_swift(check_app.WELCOME_TITLE)
    assert f'static let welcomeTitle = "{welcome}"' in text
    assert f'static let pausedNote = "{check_app.PAUSED_NOTE}"' in text
    assert check_app.STATUS_ITEM_NAMES == (check_app.APP_NAME, f"{check_app.APP_NAME}, paused")
    assert 'paused ? "\\(appName), paused" : appName' in text
    prompt = _named_in_swift(check_app.OPEN_PROMPT, "HostText.appName")
    assert f'panel.prompt = "{prompt}"' in model
    assert "by the clock" in text
    assert '"Once this Mac is connected to Lucille, Lucille can use' in text
    source = (build_app.TOOLS_DIR / "check_app.py").read_text(encoding="utf-8")
    assert source.count('"Once this Mac is connected to Lucille"') == 2
    # The welcome note says what the app is for in the sentences the folder usage keys hold.
    assert f'static let appPurpose = "{_named_in_swift(check_app.PURPOSE)}"' in text
    assert "        \\(appPurpose)\n\n" in text
    # The note that refuses the whole disk.
    assert f'"{_named_in_swift(check_app.WHOLE_DISK_TITLE)}",' in text
    assert f'"{check_app.WHOLE_DISK_TEXT}\\(diskName), your startup disk: ' in text


_BROAD_FLAG = '"--allow-home-or-root"'


def test_the_app_never_passes_the_broad_flag_for_the_whole_disk() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    # One place builds the argument, and it leaves the flag out for the whole disk. Elsewhere the
    # app only reads the flag's name in the host's refusals.
    for path in SWIFT_FILES:
        source = path.read_text(encoding="utf-8")
        if path.name != "HostText.swift":
            assert _BROAD_FLAG not in source, path.name
    assert text.count(f"append({_BROAD_FLAG})") == 1
    assert text.count(_BROAD_FLAG) == 1 + text.count(f".contains({_BROAD_FLAG})")
    adds = re.search(r"static func addFolderArguments\(.*?\n    \}\n", text, re.DOTALL)
    assert adds is not None
    assert (
        f"if confirmedHome && !isWholeDisk(path: path) {{\n            verb.append({_BROAD_FLAG})"
    ) in adds.group(0)
    assert model.count("HostText.addFolderArguments(") == 2
    # The whole disk the app refuses holds the two folders the design names, which the ui check
    # chooses, and the two folders between them.
    listed = re.search(r"static let wholeDisk = \[(.*?)\]", text)
    assert listed is not None
    disks = set(re.findall(r'"([^"]+)"', listed.group(1)))
    assert disks == {"/", "/System", "/System/Volumes", "/System/Volumes/Data"}
    assert {str(folder) for folder in check_app.WHOLE_DISK_FOLDERS} == {"/", "/System/Volumes/Data"}
    # Its note has exactly two buttons, a smaller folder, the default, and Cancel. The ui check
    # finds both on the screen, and no button that adds the folder; the "no other" is this pin.
    refusal = re.search(
        r"private func refuseWholeDisk\(\) -> Bool \{(.*?)\n    \}\n", model, re.DOTALL
    )
    assert refusal is not None
    buttons = re.findall(r'addButton\(withTitle: "([^"]+)"\)', refusal.group(1))
    assert buttons == list(check_app.WHOLE_DISK_BUTTONS)
    assert "return alert.runModal() == .alertFirstButtonReturn" in refusal.group(1)


def _swift_function(text: str, signature: str) -> str:
    found = re.search(re.escape(signature) + r".*?\n    \}\n", text, re.DOTALL)
    assert found is not None, signature
    return found.group(0)


def test_the_whole_disk_check_reads_the_disk_only_when_it_must() -> None:
    # The app runs these on its main thread, where a stalled network folder would freeze the
    # menu with no Cancel. A path that names the whole disk is never read; a folder that is not
    # the home folder or above it is read once, where the app chooses it, and not again to ask
    # about it or to build the command line.
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    whole = _swift_function(text, "static func isWholeDisk(path: String) -> Bool {")
    assert whole.index("if namesWholeDisk(path) {") < whole.index("resolvingSymlinksInPath")
    assert whole.count("resolvingSymlinksInPath") == 1
    identity = _swift_function(text, "private static func fileIdentity(")
    assert "resolvingSymlinksInPath" not in identity
    broad = _swift_function(text, "static func broadFolder(path: String, home: String)")
    assert broad.count("isWholeDisk(") == 1
    assert broad.index("return nil") < broad.index("isWholeDisk(")
    adds = _swift_function(text, "static func addFolderArguments(")
    assert "confirmedHome && !isWholeDisk(path: path)" in adds


def test_the_app_and_its_checks_use_the_apps_name() -> None:
    name = build_app.load_pins().app_name
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    app = (build_app.SWIFT_DIR / "MenuBarApp.swift").read_text(encoding="utf-8")
    assert check_app.APP_NAME == name
    assert check_app.WELCOME_TITLE == f"{name} is in your menu bar"
    assert check_app.STATUS_ITEM_NAMES == (name, f"{name}, paused")
    assert check_app.OPEN_PROMPT == f"Allow {name} to Use This Folder"
    assert f"Quit {name}" in check_app.MENU_ITEMS
    assert 'static let quitApp = "Quit \\(appName)"' in text
    assert app.count("HostText.quitApp") == 2
    assert 'version.map { "\\(HostText.appName) \\($0)" } ?? HostText.appName' in model
    assert check_app.PURPOSE == build_app.folder_usage(name)
    assert check_app.PURPOSE.startswith(f"{name} lets Lucille ")
    # The Swift code reads the name from the bundle's Info.plist and never spells it. Code run
    # outside the app (the self-test) gets a fallback that is not the name, so the ui check, which
    # looks for the name, fails if the app stops reading it from Info.plist.
    fallback = "the app"
    assert fallback != name
    assert (
        'static let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName")'
        f' as? String ?? "{fallback}"'
    ) in text
    versions = build_app.BundleVersions("0.1.0", "1")
    assert build_app.info_plist(build_app.load_pins(), versions)["CFBundleDisplayName"] == name
    # The workflow spells the name only in its three artifacts' names, in lower case: the app's
    # paths come from check_app.py and build_app.py, which read the pins.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert f"name: {name.lower()}-macos-app-logs" in workflow
    assert f"name: {name.lower()}-adhoc-macos-arm64-dmg-" in workflow
    assert f"name: {name.lower()}-macos-smoke-" in workflow
    assert len(re.findall(rf"\b{re.escape(name)}\b", workflow, re.IGNORECASE)) == 3
    assert name not in workflow
    for path in [*SWIFT_FILES, build_app.MACOS_DIR / "Tests" / "SelfTest.swift"]:
        source = path.read_text(encoding="utf-8")
        assert not re.search(rf"\b{re.escape(name)}\b", source), path
    # The app's earlier names are gone from what the app shows and what checks it. The host
    # program inside keeps its own name, lucille-host.
    for path in [
        *SWIFT_FILES,
        build_app.MACOS_DIR / "Tests" / "SelfTest.swift",
        build_app.TOOLS_DIR / "build_app.py",
        build_app.TOOLS_DIR / "check_app.py",
        build_app.MACOS_DIR / "README.md",
        build_app.PINS_FILE,
        WORKFLOW,
    ]:
        source = path.read_text(encoding="utf-8")
        assert "Lucille Host" not in source, path
        assert "loft" not in source.lower(), path


def _constraints_with(old: str, new: str) -> str:
    assert old in CONSTRAINTS_TEXT
    return CONSTRAINTS_TEXT.replace(old, new, 1)


def _requires() -> list[str]:
    import tomllib

    data = tomllib.loads(build_app.PYPROJECT.read_text(encoding="utf-8"))
    return data["build-system"]["requires"]


def test_the_build_backend_is_pinned_by_hash() -> None:
    constraints = build_app.load_build_constraints()
    assert set(constraints) == {
        "hatchling",
        "packaging",
        "pathspec",
        "pluggy",
        "tomlkit",
        "trove-classifiers",
    }
    for version, hashes in constraints.values():
        assert version and hashes
        assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes)
    assert not build_app.build_constraint_problems(_requires(), constraints)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("hatchling==1.32.4 \\\n    --hash=sha256:08ec", "hatchling==1.32.4\n#"),
        ("--hash=sha256:08ec", "--hash=sha256:08EC"),
        ("--hash=sha256:08ec", "--hash=md5:08ec"),
        ("hatchling==1.32.4", "hatchling>=1.32.4"),
        ("tomlkit==0.15.2", "packaging==26.3"),
    ],
)
def test_a_malformed_constraint_is_refused(old: str, new: str) -> None:
    with pytest.raises(build_app.PinError):
        build_app.parse_build_constraints(_constraints_with(old, new))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("hatchling==1.32.4", "hatchling==1.32.3"),
        ("pluggy==1.7.0", "pluggy-extra==1.7.0"),
    ],
)
def test_constraints_that_differ_from_pyproject_are_refused(old: str, new: str) -> None:
    constraints = build_app.parse_build_constraints(_constraints_with(old, new))
    assert build_app.build_constraint_problems(_requires(), constraints)


def test_an_unpinned_build_requirement_is_refused() -> None:
    constraints = build_app.parse_build_constraints(CONSTRAINTS_TEXT)
    requires = ["hatchling>=1.32" if r.startswith("hatchling") else r for r in _requires()]
    problems = build_app.build_constraint_problems(requires, constraints)
    assert any("not pinned with ==" in problem for problem in problems)


def test_the_wheel_is_built_with_the_hashed_build_backend(tmp_path: Path) -> None:
    argv = build_app.build_wheel_argv(tmp_path)
    assert argv[:3] == ["uv", "build", "--wheel"]
    position = argv.index("--build-constraints")
    assert argv[position + 1] == str(build_app.BUILD_CONSTRAINTS)
    assert "--require-hashes" in argv
    assert "--no-build-isolation" not in argv
    # A uv.toml could turn build isolation off, or add an index: uv reads none.
    assert "--no-config" in argv
    assert "--no-config" in build_app.export_requirements_argv()


def test_uv_settings_from_the_environment_do_not_reach_the_wheel_or_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With build isolation off, uv build would use whatever hatchling is installed, unpinned and
    # never hashed, and still exit 0. So neither uv build nor uv export sees any UV_* variable.
    monkeypatch.setenv("UV_NO_BUILD_ISOLATION", "1")
    monkeypatch.setenv("UV_INDEX_URL", "https://example.invalid/simple")
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        calls.append((list(argv), dict(env) if isinstance(env, dict) else None))
        if argv[:2] == ["uv", "build"]:
            out = Path(argv[argv.index("--out-dir") + 1])
            (out / payload.host_wheel_name("0.1.0")).write_bytes(b"")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", run)
    build_app.build_wheel(tmp_path, "0.1.0")
    build_app.export_requirements_text()
    assert [argv[:2] for argv, _ in calls] == [["uv", "build"], ["uv", "export"]]
    for argv, env in calls:
        assert "--no-config" in argv, argv
        assert env is not None, argv
        assert [key for key in env if key.startswith("UV_")] == [], argv
        assert env["PATH"] == os.environ["PATH"]


def test_the_disk_image_name_says_it_is_ad_hoc() -> None:
    name = build_app.dmg_name("Capsule", "0.1.0", "4466aba0c0de")
    assert name == "Capsule-0.1.0-4466aba0c0de-adhoc-macOS-arm64.dmg"
    assert name.endswith(build_app.DMG_SUFFIX)
    assert name != "Capsule-0.1.0-macOS-arm64.dmg"
    for commit in ("", "HEAD", "4466ABA", "4466ab", "../x"):
        with pytest.raises(build_app.BuildError):
            build_app.dmg_name("Capsule", "0.1.0", commit)


def test_only_this_scripts_disk_images_are_found(tmp_path: Path) -> None:
    ours = tmp_path / build_app.dmg_name("Capsule", "0.1.0", "abcdef1")
    ours.write_bytes(b"")
    (tmp_path / "Capsule-0.1.0-macOS-arm64.dmg").write_bytes(b"")
    (tmp_path / build_app.dmg_name("Other", "0.1.0", "abcdef1")).write_bytes(b"")
    (tmp_path / "other.dmg").write_bytes(b"")
    assert build_app.disk_images(tmp_path, "Capsule") == [ours]


def test_the_disk_image_glob_takes_the_name_literally(tmp_path: Path) -> None:
    # build() deletes every image disk_images() returns, so a name that is also a pattern must
    # match its own images only.
    other = tmp_path / build_app.dmg_name("Capsule", "0.1.0", "abcdef1")
    other.write_bytes(b"")
    assert build_app.disk_images(tmp_path, "C*") == []
    assert build_app.disk_images(tmp_path, "Cap?ule") == []
    ours = tmp_path / build_app.dmg_name("Cap[s]ule", "0.1.0", "abcdef1")
    ours.write_bytes(b"")
    assert build_app.disk_images(tmp_path, "Cap[s]ule") == [ours]


OTOOL_L = """\
Load command 9
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 11.0
      sdk 15.0
   ntools 1
Load command 10
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 6
    minos 17.0
      sdk 18.0
Load command 11
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 10.9
      sdk 10.9
"""


def test_the_minimum_macos_is_read_from_the_load_commands() -> None:
    lines = OTOOL_L.splitlines()
    assert check_app.minimum_macos_versions(lines) == ["11.0", "10.9"]
    assert check_app.minimum_macos_versions(lines[:4]) == []
    assert check_app.minimum_macos_versions(
        [line.replace("platform 1", "platform MACOS") for line in lines]
    ) == ["11.0", "10.9"]


@pytest.mark.parametrize(
    ("version", "later"),
    [
        ("11.0", False),
        ("15.0", False),
        ("15", False),
        ("15.0.1", True),
        ("15.1", True),
        ("26.0", True),
        ("x", True),
    ],
)
def test_newer_than(version: str, later: bool) -> None:
    assert check_app.newer_than(version, "15.0") is later


def test_forbidden_imports() -> None:
    allowed = [
        "_objc_msgSend",
        "_kill",
        "_OBJC_CLASS_$_NSTask",
        "_OBJC_CLASS_$_NSPipe",
        "_$sSS17UnicodeScalarViewV6append10contentsOfyqd___tSTRd__7ElementQyd__AERtsF",
    ]
    refused = [
        "_posix_spawn",
        "_posix_spawnp",
        "_socket",
        "_connect",
        "_nw_connection_create",
        "_OBJC_CLASS_$_NSURLSession",
        "_OBJC_CLASS_$_NSWorkspace",
        "_$s10Foundation10URLSessionC6sharedACvgZ",
        "_$s10Foundation4DataV10contentsOf7optionsAcA3URLV_So20NSDataReadingOptionsVtKcfC",
        "_OBJC_CLASS_$_NSPasteboard",
    ]
    assert check_app.forbidden_imports(allowed + refused) == refused


OTOOL_LOADS = """\
Load command 3
          cmd LC_ID_DYLIB
      cmdsize 72
         name @rpath/libself.dylib (offset 24)
Load command 4
          cmd LC_LOAD_DYLINKER
      cmdsize 32
         name /usr/lib/dyld (offset 12)
Load command 5
          cmd LC_LOAD_DYLIB
      cmdsize 56
         name @rpath/libtcl9.0.dylib (offset 24)
   time stamp 0 Thu Jan  1 00:00:00 1970
      current version 9.0.4
Load command 6
          cmd LC_LOAD_WEAK_DYLIB
      cmdsize 56
         name /usr/lib/libSystem.B.dylib (offset 24)
Load command 7
          cmd LC_RPATH
      cmdsize 32
         path @loader_path/../.. (offset 12)
Load command 8
          cmd LC_LOAD_DYLIB
      cmdsize 56
         name /usr/lib/libSystem.B.dylib (offset 24)
Load command 9
          cmd LC_RPATH
      cmdsize 48
         path /opt/My Libs/lib (offset 12)
"""


def test_the_libraries_and_run_paths_are_read_from_the_load_commands() -> None:
    assert check_app.load_commands(OTOOL_LOADS.splitlines()) == (
        ["@rpath/libtcl9.0.dylib", "/usr/lib/libSystem.B.dylib"],
        ["@loader_path/../..", "/opt/My Libs/lib"],
    )
    assert check_app.load_commands([]) == ([], [])


def _bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bundle laid out like the app's, with a space in its name: the interpreter, one extension
    module, and one library kept in ``lib/``."""
    app = tmp_path / "Example App.app"
    runtime = app / build_app.RUNTIME_DIR
    dynload = runtime / build_app.STDLIB / "lib-dynload"
    dynload.mkdir(parents=True)
    (runtime / "bin").mkdir()
    python = app / build_app.RUNTIME_IN_APP
    module = dynload / "_mod.cpython-313-darwin.so"
    for path in (python, module, runtime / "lib" / "libkept.dylib"):
        path.write_bytes(b"")
    return app, python, module


def test_libraries_that_resolve_inside_the_bundle_pass(tmp_path: Path) -> None:
    app, python, module = _bundle(tmp_path)
    libraries = [
        "/usr/lib/libSystem.B.dylib",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        "@rpath/libkept.dylib",
        "@loader_path/../../libkept.dylib",
        "@executable_path/../lib/libkept.dylib",
    ]
    shown, problems = check_app.resolve_links(
        app, module, python, (libraries, ["@loader_path/../.."])
    )
    assert problems == []
    kept = "Contents/Resources/runtime/lib/libkept.dylib"
    assert shown == [
        "run path @loader_path/../.. -> Contents/Resources/runtime/lib",
        "/usr/lib/libSystem.B.dylib (macOS)",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation (macOS)",
        f"@rpath/libkept.dylib -> {kept}",
        f"@loader_path/../../libkept.dylib -> {kept}",
        f"@executable_path/../lib/libkept.dylib -> {kept}",
    ]
    # @rpath also tries the interpreter's own run paths, resolved against the interpreter.
    shown, problems = check_app.resolve_links(
        app, module, python, (["@rpath/libkept.dylib"], []), ["@executable_path/../lib"]
    )
    assert problems == [] and shown == [f"@rpath/libkept.dylib -> {kept}"]


@pytest.mark.parametrize(
    ("libraries", "rpaths", "problem"),
    [
        # A library the build removed, still loaded by a module it kept.
        (["@rpath/libtcl9.0.dylib"], ["@loader_path/../.."], "no such file in the app"),
        (["@loader_path/../../libgone.dylib"], [], "no such file in the app"),
        # A relative path that climbs out of the bundle.
        (
            ["@loader_path/../../../../../../../../opt/homebrew/lib/libssl.3.dylib"],
            [],
            "is outside the app",
        ),
        (["@executable_path/../../../../../../usr/local/lib/x.dylib"], [], "is outside the app"),
        (["@rpath/../../../../../../../../x.dylib"], ["@loader_path/../.."], "is outside the app"),
        # A run path outside the bundle, even one nothing uses.
        ([], ["@loader_path/../../../../../../../.."], "is outside the app"),
        ([], ["/usr/local/lib"], "not a path inside the app"),
        ([], ["@rpath/lib"], "not a path inside the app"),
        # A library from anywhere but macOS or the bundle.
        (["/opt/homebrew/lib/libssl.3.dylib"], [], "not from macOS"),
        (["/usr/local/lib/libffi.dylib"], [], "not from macOS"),
        (["/Library/Frameworks/Python.framework/Versions/3.13/Python"], [], "not from macOS"),
        (["libfoo.dylib"], [], "not from macOS"),
        (["@rpath/libkept.dylib"], [], "no run path inside the app"),
    ],
)
def test_libraries_that_do_not_resolve_inside_the_bundle_fail(
    tmp_path: Path, libraries: list[str], rpaths: list[str], problem: str
) -> None:
    app, python, module = _bundle(tmp_path)
    _, problems = check_app.resolve_links(app, module, python, (libraries, rpaths))
    assert len(problems) == 1 and problem in problems[0], problems


def test_only_the_title_or_description_names_the_menu_bar_item() -> None:
    # What System Events reported when only the tooltip carried the name.
    tooltip_only = {"title": "Computer", "description": "status menu", "help": "Capsule"}
    assert not check_app._named(tooltip_only, "Capsule")
    assert check_app._named({**tooltip_only, "title": "Capsule"}, "Capsule")
    assert check_app._named({"description": "Capsule, paused"}, "Capsule, paused")
    assert not check_app._named({}, "Capsule")


@pytest.mark.parametrize(
    ("said", "width"), [("30", 30.0), ("24.0", 24.0), ("24,5", 24.5), ("", 0.0), ("missing", 0.0)]
)
def test_the_menu_bar_items_width_is_read(said: str, width: float) -> None:
    assert check_app._width({"width": said}) == width
    assert check_app._width({}) == 0.0


class _Hdiutil:
    """A stand-in for subprocess.run that answers each hdiutil call from a script."""

    def __init__(self, answers: list[tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: object) -> object:
        import subprocess

        self.calls.append(argv)
        code, stderr = self.answers[len(self.calls) - 1]
        return subprocess.CompletedProcess(argv, code, stdout="", stderr=stderr)


@pytest.mark.parametrize("busy", build_app.HDIUTIL_BUSY)
def test_hdiutil_tries_again_while_the_machine_is_busy(
    busy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    fake = _Hdiutil([(1, f"hdiutil: verify failed - {busy}\n"), (1, busy), (0, "")])
    monkeypatch.setattr("subprocess.run", fake)
    done = build_app.hdiutil(["verify", "x.dmg"], sleep=waits.append)
    assert done.returncode == 0
    assert fake.calls == [["hdiutil", "verify", "x.dmg"]] * 3
    assert waits == [5, 10]


def test_hdiutil_returns_any_other_failure_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    fake = _Hdiutil([(1, "hdiutil: attach failed - no mountable file systems\n")])
    monkeypatch.setattr("subprocess.run", fake)
    done = build_app.hdiutil(["attach", "x.dmg"], sleep=waits.append)
    assert done.returncode == 1 and len(fake.calls) == 1 and waits == []


def test_hdiutil_gives_up_after_its_last_try(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    busy = build_app.HDIUTIL_BUSY[1]
    fake = _Hdiutil([(1, busy)] * build_app.HDIUTIL_TRIES)
    monkeypatch.setattr("subprocess.run", fake)
    done = build_app.hdiutil(["create", "x.dmg"], sleep=waits.append)
    assert done.returncode == 1 and busy in done.stderr
    assert len(fake.calls) == build_app.HDIUTIL_TRIES
    assert waits == [5 * n for n in range(1, build_app.HDIUTIL_TRIES)]


def test_every_hdiutil_call_goes_through_the_retry() -> None:
    for script in ("build_app.py", "check_app.py"):
        source = (build_app.TOOLS_DIR / script).read_text(encoding="utf-8")
        assert '["hdiutil"' not in source.replace('argv = ["hdiutil", *args]', ""), script


# ---------------------------------------------------------------------------------------------
# The runtime: the prune list, pip, the interpreter's name, links


def _runtime_like_the_pinned_one(root: Path, prune: tuple[str, ...]) -> Path:
    """A runtime shaped like the pinned archive's ``python/``: the interpreter and the links to
    it, pip with a RECORD that names files the archive no longer holds and scripts in ``bin/``,
    each path of ``prune``, and a module, an extension and a link that stay."""
    runtime = root / "python"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "python3.13").write_bytes(b"\xcf\xfa\xed\xfe interpreter")
    (runtime / "bin" / "python3.13").chmod(0o755)
    for entry in prune:
        path = runtime / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry in ("bin/python", "bin/python3"):
            path.symlink_to("python3.13")
        elif entry.startswith("bin/") or Path(entry).suffix in (".py", ".so", ".dylib", ".txt"):
            path.write_bytes(b"pruned")
        else:
            path.mkdir()
            (path / "inside.pyc" if "__pycache__" in entry else path / "inside").write_bytes(b"")
    stdlib = runtime / build_app.STDLIB
    (stdlib / "lib-dynload").mkdir(parents=True, exist_ok=True)
    (stdlib / "lib-dynload" / "_dbm.cpython-313-darwin.so").write_bytes(b"\xcf\xfa\xed\xfe")
    (stdlib / "os.py").write_text("import sys\n", encoding="ascii")
    (stdlib / "osalias.py").symlink_to("os.py")
    site = runtime / build_app.SITE_PACKAGES
    (site / "pip").mkdir(parents=True, exist_ok=True)
    (site / "pip" / "__init__.py").write_bytes(b"")
    info = site / "pip-26.2.2.dist-info"
    info.mkdir()
    rows = ["pip/__init__.py,sha256=x,0", "pip/__pycache__/__init__.cpython-313.pyc,sha256=x,1"]
    for script in ("pip", "pip3", "pip3.13"):
        (runtime / "bin" / script).write_bytes(b"#!/the/build/machine/python3.13\n")
        rows.append(f"../../../bin/{script},sha256=x,208")
    rows.append("pip-26.2.2.dist-info/RECORD,,")
    (info / "RECORD").write_text("\n".join(rows) + "\n", encoding="ascii")
    return runtime


def test_prune_keeps_dist_info(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    assert prune and len(prune) == len(set(prune))
    for entry in prune:
        assert not any(part.endswith(".dist-info") for part in entry.split("/")), entry
        if entry.startswith(build_app.SITE_PACKAGES):
            assert entry == f"{build_app.SITE_PACKAGES}/README.txt"
        assert not (build_app.UPSTREAM_INTERPRETER + "/").startswith(entry + "/"), entry
    assert "lib/python3.13/encodings/__pycache__" in prune
    # The runtime's pip goes whole, by its RECORD; any other *.dist-info is never deleted: the
    # build refuses the runtime instead.
    runtime = _runtime_like_the_pinned_one(tmp_path, prune)
    other = runtime / build_app.SITE_PACKAGES / "other-1.0.dist-info"
    other.mkdir()
    (other / "METADATA").write_text("Name: other\nVersion: 1.0\n", encoding="ascii")
    with pytest.raises(build_app.BuildError, match=r"site-packages is not empty: \['other-1.0"):
        build_app.prepare_runtime(runtime, prune)
    assert other.is_dir()
    site = runtime / build_app.SITE_PACKAGES
    assert sorted(path.name for path in site.iterdir()) == ["other-1.0.dist-info"]
    assert not list((runtime / "bin").glob("pip*"))


@pytest.mark.parametrize(
    "entry",
    [
        "lib/python3.13/site-packages/pip-26.2.2.dist-info",
        "lib/python3.13/site-packages/keyring-25.7.0.dist-info/entry_points.txt",
        # A *.dist-info folder anywhere, not only in site-packages.
        "lib/python3.13/vendored-1.0.dist-info",
        "lib/python3.13/vendor/x-1.0.dist-info/RECORD",
        "lib/python3.13/site-packages/*",
        "lib/python3.13/site-packages/keyring",
        "lib/python3.13/site-packages",
        "lib/python3.13",
        "lib",
        "bin",
        "bin/python3.13",
        "lib/python3.13/*.py",
        "lib/python3.13/turtle?.py",
        "lib/python3.13/[a]",
        "../x",
        "lib/../bin",
        "./include",
        "/include",
        "include/",
        "include # the headers",
        "lib\\pkgconfig",
    ],
)
def test_a_prune_entry_that_could_cut_too_much_is_refused(entry: str) -> None:
    with pytest.raises(build_app.PinError):
        build_app.parse_prune_list(f"include\n{entry}\n")


def test_a_prune_list_is_literal_paths_once_each() -> None:
    assert build_app.parse_prune_list("# headers\n\ninclude\n  share  \n") == ("include", "share")
    with pytest.raises(build_app.PinError, match="listed twice"):
        build_app.parse_prune_list("include\ninclude\n")


def test_prepare_runtime_on_a_runtime_like_the_pinned_one(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    runtime = _runtime_like_the_pinned_one(tmp_path, prune)
    removed = build_app.prepare_runtime(runtime, prune)
    assert removed == sorted([*prune, "pip==26.2.2"])
    assert build_app.runtime_shape_problems(runtime) == []
    assert sorted(path.name for path in (runtime / "bin").iterdir()) == ["lucille-host-runtime"]
    interpreter = runtime / build_app.INTERPRETER
    assert interpreter.read_bytes() == b"\xcf\xfa\xed\xfe interpreter"
    assert interpreter.stat().st_mode & 0o111
    # The link that stayed is now a copy of its target.
    alias = runtime / build_app.STDLIB / "osalias.py"
    assert not alias.is_symlink() and alias.read_text(encoding="ascii") == "import sys\n"
    assert build_app.symlinks_in(runtime) == [] and build_app.bytecode_in(runtime) == []
    assert not (runtime / build_app.SITE_PACKAGES / "pip").exists()


def _plant_bytecode(runtime: Path) -> list[Path]:
    """A __pycache__ folder with a .pyc in it, a stray .pyc and a stray .pyo, outside every
    prune entry; returns the four paths, sorted."""
    stdlib = runtime / build_app.STDLIB
    cache = stdlib / "json" / "__pycache__"
    cache.mkdir(parents=True)
    compiled = cache / "decoder.cpython-313.pyc"
    compiled.write_bytes(b"\xf3\r\r\n" + bytes(12))
    stray = stdlib / "legacy.pyc"
    stray.write_bytes(b"\xf3\r\r\n" + bytes(12))
    optimized = stdlib / "old.pyo"
    optimized.write_bytes(b"")
    return sorted([cache, compiled, stray, optimized])


def test_bytecode_is_found_and_refused(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    # In a prepared runtime: each one found, and each one a shape problem.
    runtime = _runtime_like_the_pinned_one(tmp_path / "prepared", prune)
    build_app.prepare_runtime(runtime, prune)
    planted = _plant_bytecode(runtime)
    assert build_app.bytecode_in(runtime) == planted
    problems = build_app.runtime_shape_problems(runtime)
    assert [problem for problem in problems if problem.startswith("bytecode: ")] == [
        f"bytecode: {path.relative_to(runtime)}" for path in planted
    ]
    # Before preparing: the build refuses the runtime, and deletes none of them.
    runtime = _runtime_like_the_pinned_one(tmp_path / "unprepared", prune)
    planted = _plant_bytecode(runtime)
    with pytest.raises(build_app.BuildError, match=r"bytecode: lib/python3\.13/json/__pycache__"):
        build_app.prepare_runtime(runtime, prune)
    assert all(path.exists() for path in planted)


def test_a_prune_entry_the_runtime_lacks_is_refused(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    runtime = _runtime_like_the_pinned_one(tmp_path, prune)
    with pytest.raises(build_app.BuildError, match="prune.txt names lib/tk8.6, which the pinned"):
        build_app.prepare_runtime(runtime, (*prune, "lib/tk8.6"))
    # Refused before anything was pruned.
    assert all((runtime / entry).exists() or (runtime / entry).is_symlink() for entry in prune)


def test_what_the_prune_list_misses_is_refused_not_deleted(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    runtime = _runtime_like_the_pinned_one(tmp_path, prune)
    (runtime / "bin" / "2to3").write_bytes(b"new in this runtime")
    (runtime / "etc").mkdir()
    with pytest.raises(build_app.BuildError) as raised:
        build_app.prepare_runtime(runtime, prune)
    assert "bin/ holds ['2to3', 'lucille-host-runtime']" in str(raised.value)
    assert "['bin', 'etc', 'lib'], not bin and lib" in str(raised.value)
    assert (runtime / "bin" / "2to3").is_file() and (runtime / "etc").is_dir()


@pytest.mark.parametrize(
    ("make", "words"),
    [
        (lambda root: (root / "dangling").symlink_to("nowhere"), "not a file inside"),
        (lambda root: (root / "outside").symlink_to("/etc/hostname"), "not a file inside"),
        (lambda root: (root / "folder").symlink_to("lib"), "not a file inside"),
    ],
)
def test_symlinks_replaced_or_refused(
    tmp_path: Path, make: Callable[[Path], object], words: str
) -> None:
    root = tmp_path / "runtime"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "file.txt").write_text("text\n", encoding="ascii")
    (root / "lib" / "file.txt").chmod(0o750)
    (root / "copy.txt").symlink_to("lib/file.txt")
    assert build_app.replace_symlinks(root) == ["copy.txt"]
    copy = root / "copy.txt"
    assert not copy.is_symlink() and copy.read_text(encoding="ascii") == "text\n"
    assert copy.stat().st_mode & 0o777 == 0o750
    make(root)
    with pytest.raises(build_app.BuildError, match=words):
        build_app.replace_symlinks(root)


# ---------------------------------------------------------------------------------------------
# Signing, the wrapper, the toolchain, the comparison


def _macho(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(28))
    return path


@pytest.mark.parametrize("hardened", [True, False])
def test_signing_order(tmp_path: Path, hardened: bool) -> None:
    pins = dataclasses.replace(build_app.load_pins(), adhoc_hardened_runtime=hardened)
    app = tmp_path / "Capsule.app"
    stdlib = app / build_app.RUNTIME_DIR / build_app.STDLIB
    main = _macho(app / "Contents" / "MacOS" / "Capsule")
    interpreter = _macho(app / build_app.RUNTIME_IN_APP)
    dbm = _macho(stdlib / "lib-dynload" / "_dbm.cpython-313-darwin.so")
    backend = _macho(stdlib / "site-packages" / "_cffi_backend.cpython-313-darwin.so")
    rust = _macho(stdlib / "site-packages" / "cryptography/hazmat/bindings/_rust.abi3.so")
    shallow = _macho(app / "Contents" / "Resources" / "x.dylib")
    (app / "Contents" / "Info.plist").write_bytes(b"<plist/>")
    (app / "Contents" / "Resources" / "script.py").write_text("print()\n", encoding="ascii")
    plan = build_app.signing_plan(app, pins)
    # Libraries deepest first, every one before the interpreter (even the shallow one), then the
    # interpreter, then the bundle; the app's executable only through the bundle.
    assert [argv[-1] for argv in plan] == [
        str(rust),
        str(dbm),
        str(backend),
        str(shallow),
        str(interpreter),
        str(app),
    ]
    assert not any(str(main) in argv for argv in plan)
    identifiers = [argv[argv.index("--identifier") + 1] for argv in plan[:-1]]
    assert identifiers == [
        "com.lucille-ai.capsule.site-packages.cryptography.hazmat.bindings._rust.abi3",
        "com.lucille-ai.capsule.lib-dynload._dbm.cpython-313-darwin",
        "com.lucille-ai.capsule.site-packages._cffi_backend.cpython-313-darwin",
        "com.lucille-ai.capsule.Resources.x",
        "com.lucille-ai.capsule.runtime",
    ]
    assert identifiers[-1] == pins.runtime_id
    assert all(name.startswith(pins.bundle_id + ".") for name in identifiers)
    assert len(set(identifiers)) == len(identifiers)
    assert "--identifier" not in plan[-1]
    for argv in plan:
        assert argv[:2] == ["codesign", "--force"]
        assert argv[argv.index("--sign") + 1] == "-"
        assert "--timestamp=none" in argv
        assert not {"--deep", "--prefix", "--entitlements", "--timestamp"} & set(argv)
        has_runtime = "--options" in argv and argv[argv.index("--options") + 1] == "runtime"
        assert has_runtime is hardened


def test_the_signing_identifier_never_takes_codesigns_derived_name(tmp_path: Path) -> None:
    pins = build_app.load_pins()
    app = tmp_path / "Capsule.app"
    path = app / build_app.RUNTIME_DIR / build_app.STDLIB / "site-packages" / "a b@c.so"
    assert (
        build_app.signing_identifier(app, path, pins)
        == "com.lucille-ai.capsule.site-packages.a-b-c"
    )
    assert (
        build_app.signing_identifier(app, app / build_app.RUNTIME_IN_APP, pins) == pins.runtime_id
    )


def test_wrapper_text() -> None:
    assert build_app.WRAPPER_TEXT == (
        "#!/bin/sh\n"
        'self=$(/bin/realpath -- "$0") || exit 1\n'
        'exec "${self%/*}/../runtime/bin/lucille-host-runtime" -I -B -X utf8 -m lucille_host'
        ' --profile mac-app "$@"\n'
    )
    assert len(build_app.WRAPPER_TEXT.splitlines()) == 3
    assert build_app.RUNTIME_FLAGS == ("-I", "-B", "-X", "utf8")
    # The wrapper runs on the app's profile, the one the checks and the Swift app use (0002 §9),
    # and the wrapper check on a Mac sees the host write that profile's pause flag through it.
    assert build_app.APP_PROFILE == check_app.APP_PROFILE == "mac-app"
    assert f'static let appProfile = "{build_app.APP_PROFILE}"' in _swift("HostCLI.swift")
    check = _python_function(_CHECK_APP.read_text(encoding="utf-8"), "check_wrapper")
    assert '_run_in([str(link), "pause"], home)' in check
    assert '(hosts / APP_PROFILE / "state" / "paused").is_file()' in check
    assert 'not (hosts / "default").exists()' in check
    # The wrapper finds the runtime from its own real path, not from dirname "$0".
    assert "dirname" not in build_app.WRAPPER_TEXT
    wrapper_dir = Path(build_app.WRAPPER_IN_APP).parent
    target = os.path.normpath(wrapper_dir / "../runtime/bin/lucille-host-runtime")
    assert target == build_app.RUNTIME_IN_APP


def test_the_wrapper_runs_the_bundled_runtime_through_a_link(tmp_path: Path) -> None:
    app = tmp_path / "Some Folder" / "Capsule.app"
    wrapper = build_app.write_wrapper(app)
    assert wrapper == app / build_app.WRAPPER_IN_APP
    assert wrapper.stat().st_mode & 0o777 == 0o755
    runtime = app / build_app.RUNTIME_IN_APP
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\nprintf 'bundled\\n'\nprintf '%s\\n' \"$@\"\n", encoding="ascii")
    runtime.chmod(0o755)
    # A runtime beside the link, where dirname "$0" would look, must never run.
    link_dir = tmp_path / "bin"
    decoy = link_dir.parent / "runtime" / "bin" / "lucille-host-runtime"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("#!/bin/sh\necho decoy\n", encoding="ascii")
    decoy.chmod(0o755)
    link_dir.mkdir()
    (link_dir / "lucille-host").symlink_to(wrapper)
    done = subprocess.run(
        [str(link_dir / "lucille-host"), "status", "--json", "two words"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "bundled",
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "lucille_host",
        "--profile",
        "mac-app",
        "status",
        "--json",
        "two words",
    ]
    # A --profile the user gives the wrapper comes after its own, and the host takes the last.
    parser = build_parser()
    assert parser.parse_args(["--profile", "mac-app", "status"]).profile == "mac-app"
    given = parser.parse_args(["--profile", "mac-app", "--profile", "default", "status"])
    assert given.profile == "default"


def test_toolchain_problems() -> None:
    pins = build_app.load_pins()
    xcode = f"Xcode {pins.xcode}\nBuild version 16F6\n"
    assert build_app.toolchain_problems(pins, xcode) == []
    for text in (
        f"Xcode {pins.xcode}.1\n",
        "Xcode 26.0\nBuild version 17A324\n",
        f"Xcode {pins.xcode}0\n",
        f"xcode {pins.xcode}\n",
        "",
    ):
        problems = build_app.toolchain_problems(pins, text)
        assert problems and "tools/macos/pins.toml pins Xcode" in problems[0], text
    assert build_app.developer_dir(pins) == (
        f"/Applications/Xcode_{pins.xcode}.app/Contents/Developer"
    )
    for bad in ("16.4\nEVIL=1", "16.4/../../tmp", "latest"):
        with pytest.raises(build_app.PinError):
            build_app.developer_dir(dataclasses.replace(pins, xcode=bad))


def test_compare_reports_same_different_and_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = build_app.load_pins().app_name
    for folder in ("a", "b"):
        app = tmp_path / folder / f"{name}.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_bytes(b"<plist/>")
        payload.write_tarball(app, tmp_path / folder / build_app.unsigned_tarball_name(name), 7)
    report = build_app.compare(tmp_path / "a", tmp_path / "b", name)
    assert "**SAME**" in report and "sha256 of the .tar inside" in report
    changed = tmp_path / "b" / f"{name}.app" / "Contents" / "Info.plist"
    changed.write_bytes(b"<plist>other</plist>")
    payload.write_tarball(
        changed.parents[1], tmp_path / "b" / build_app.unsigned_tarball_name(name), 7
    )
    report = build_app.compare(tmp_path / "a", tmp_path / "b", name)
    assert "**DIFFERENT**" in report and f"- `{name}.app/Contents/Info.plist`" in report
    assert build_app.main(["compare", str(tmp_path / "a"), str(tmp_path / "missing")]) == 0
    assert "**FAILED**" in capsys.readouterr().out


# The pairing screen and run supervision: the Foundation-only files they are built from, and the
# rules those keep. The self-test (macos/Tests/SelfTest.swift) checks their behaviour on a Mac.
SPEC_CLI = build_app.REPO_ROOT / "spec" / "CLI.md"
NEW_LOGIC = ("HostJSON.swift", "Pairing.swift", "Supervisor.swift", "ConnectionText.swift")


def _swift(name: str) -> str:
    return (build_app.SWIFT_DIR / name).read_text(encoding="utf-8")


def test_the_new_logic_files_are_compiled_by_the_self_test_and_draw_nothing() -> None:
    # The self-test compiles LOGIC_SOURCES only, and test_the_logic_sources_draw_nothing holds
    # each to Foundation, Darwin and Glibc; so these files can move into a package unchanged.
    assert set(NEW_LOGIC) <= set(check_app.LOGIC_SOURCES)
    for name in NEW_LOGIC:
        assert _swift_imports(_swift(name)) == {"Foundation"}, name
    # The self-test reads the host's golden fixtures, which check_app gives it.
    assert len(list(check_app.CLI_FIXTURES.glob("*.json"))) >= 50
    source = (build_app.TOOLS_DIR / "check_app.py").read_text(encoding="utf-8")
    assert '"--fixtures",\n                str(CLI_FIXTURES),' in source


def test_the_app_runs_every_verb_on_its_own_profile() -> None:
    cli = _swift("HostCLI.swift")
    assert 'static let appProfile = "mac-app"' in cli
    assert (
        "init(python: URL, home: String, profile: String = HostCLI.appProfile, "
        "timeout: TimeInterval = 30)"
    ) in cli
    assert f'{_swift_array(macos_lint.RUNTIME_ARGV)} + ["--profile", profile] + verb' in cli
    # The app itself never chooses another profile: only the self-test does. No file but
    # HostCLI.swift names a profile argument at all, however the call around it is spelled.
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"HostCLI\([^)]*\bprofile:", text), path.name
        if path.name != "HostCLI.swift":
            assert not re.search(r"\bprofile\s*:", text), path.name
            assert '"--profile"' not in text, path.name


def test_the_app_environment_has_an_empty_path() -> None:
    cli = _swift("HostCLI.swift")
    assert '"PATH": "/var/empty"' in cli
    assert cli.count('"PATH"') == 1
    for path in SWIFT_FILES:
        assert "/usr/bin:/bin" not in path.read_text(encoding="utf-8"), path.name


def _function_body(text: str, signature: str) -> str:
    start = text.index(signature)
    end = text.index("\n    }\n", start)
    return text[start:end]


def test_the_child_runner_guards_its_pipes() -> None:
    cli = _swift("HostCLI.swift")
    # One place starts every child, under one lock, and only it calls run().
    assert cli.count("try process.run()") == 1
    assert cli.count("HostCLI.spawnLock.lock()") == 1
    spawn = _function_body(cli, "    func spawn(\n")
    assert spawn.index("HostCLI.spawnLock.lock()") < spawn.index("let process = Process()")
    assert "try process.run()" in spawn
    # The ends the app keeps are close-on-exec, the lifeline's included, before anything starts.
    assert "HostCLI.closeOnExec(inPipe.fileHandleForWriting.fileDescriptor)" in spawn
    assert "_ = fcntl(fd, F_SETFD, flags | FD_CLOEXEC)" in cli
    # A short input is written into the pipe before the child starts, non-blocking, and the app's
    # end closed after: no write can meet a broken pipe or wait forever.
    assert spawn.index("writeWithoutWaiting(data") < spawn.index("try process.run()")
    assert spawn.index("try process.run()") < spawn.index("try? written?.close()")
    assert "fcntl(fd, F_SETFL, flags | O_NONBLOCK)" in cli
    assert "static let maxInputBytes = 8193" in cli
    # The handlers and both readers exist before the child does.
    for before in (
        "process.terminationHandler = {",
        "PipeDrain.start(outPipe",
        "PipeDrain.start(errPipe",
    ):
        assert spawn.index(before) < spawn.index("try process.run()"), before
    # read(2) and write(2), never the FileHandle calls that raise an exception Swift cannot catch,
    # or that split lines on more than a newline.
    assert "read(fd, raw.baseAddress, raw.count)" in cli
    for banned in (
        "availableData",
        "readDataToEndOfFile",
        "readabilityHandler",
        ".write(",
        "FileHandle.write",
        ".bytes.lines",
    ):
        assert banned not in cli, banned
    # No signal reaches a child that has ended, whose pid may be another process's now.
    lines = cli.splitlines()
    kills = [number for number, line in enumerate(lines) if re.search(r"\bkill\(", line)]
    assert kills
    for number in kills:
        assert any("isRunning" in line for line in lines[max(0, number - 4) : number]), number
    # The lifeline is closed, never written to.
    assert "case lifeline" in cli and "func closeLifeline()" in cli


def _matches(patterns: tuple[str, ...], text: str) -> list[str]:
    """The patterns that match ``text``."""
    return [pattern for pattern in patterns if re.search(pattern, text)]


# Ways Swift code could read the pasteboard. The user pastes into a field, through the field; the
# app never reads the pasteboard itself (macOS 15.4 and later may ask the user about that).
_READS_THE_PASTEBOARD = (
    r"NSPasteboard",
    r"UIPasteboard",
    r"generalPasteboard",
    r"pasteboardItems",
    r"\breadObjects\s*\(",
    r"\bstring\s*\(\s*forType\s*:",
    r"\bdata\s*\(\s*forType\s*:",
    r"\bpropertyList\s*\(\s*forType\s*:",
)


@pytest.mark.parametrize(
    "line",
    [
        "let pasteboard = NSPasteboard.general",
        "let text = NSPasteboard.general.string(forType: .string)",
        "pasteboard.readObjects(forClasses: [NSString.self], options: nil)",
        "let text = pasteboard.string(forType: .string)",
        "let raw = item.data(forType: .png)",
        "let items = pasteboard.pasteboardItems",
    ],
)
def test_the_pasteboard_scan_sees_each_way(line: str) -> None:
    assert _matches(_READS_THE_PASTEBOARD, line), line


def test_the_app_never_reads_the_pasteboard() -> None:
    # Forwarding Command-V to the field is the user's own paste, and passes.
    assert not _matches(
        _READS_THE_PASTEBOARD, "NSApp.sendAction(#selector(NSText.paste(_:)), to: nil, from: self)"
    )
    for path in SWIFT_FILES:
        assert not _matches(_READS_THE_PASTEBOARD, path.read_text(encoding="utf-8")), path.name
    assert check_app.forbidden_imports(["_OBJC_CLASS_$_NSPasteboard"]) == [
        "_OBJC_CLASS_$_NSPasteboard"
    ]


_NAMES_A_TEST_HUB_OR_FILE_STORE = (r"--hub\b", r"--store-file\b")


@pytest.mark.parametrize(
    "line", ['["pair", "--code-stdin", "--hub", hub, "--json"]', 'verb.append("--store-file")']
)
def test_the_test_hub_scan_sees_each_way(line: str) -> None:
    assert _matches(_NAMES_A_TEST_HUB_OR_FILE_STORE, line), line


def test_the_app_never_names_a_test_hub_or_a_file_store() -> None:
    # The host takes a loopback hub only when a command's own --hub names it, and --store-file
    # keeps the pair outside the Keychain: both are for tests, which the self-test may use, and the
    # app never does (decisions/0001, spec/CLI.md section 7).
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not _matches(_NAMES_A_TEST_HUB_OR_FILE_STORE, text), path.name


def _swift_strings(text: str) -> list[str]:
    """Every string literal in a Swift source: multi-line ones first, then raw and plain ones."""
    found: list[str] = []
    rest = text
    for block in re.findall(r'"""\n(.*?)\n\s*"""', text, re.DOTALL):
        found.append(block)
    rest = re.sub(r'"""\n.*?\n\s*"""', "", rest, flags=re.DOTALL)
    found += re.findall(r'#"(.*?)"#', rest)
    rest = re.sub(r'#".*?"#', "", rest)
    found += re.findall(r'"((?:[^"\\\n]|\\.)*)"', rest)
    return found


def test_the_swift_string_scan_sees_each_form() -> None:
    text = 'let a = "one \\"two\\""\nlet b = #"three "four""#\nlet c = """\n    five\n    """\n'
    assert _swift_strings(text) == ["    five", 'three "four"', 'one \\"two\\"']


def test_every_word_the_app_says_makes_no_claim_privacy_forbids() -> None:
    from tests.test_catalog import _must_never_claim

    phrases = _must_never_claim()
    for name in ("ConnectionText.swift", "HostText.swift"):
        assert len(_swift_strings(_swift(name))) > 50, name
    # Every file, not only the two that hold most of the words: the menu's own lines and the
    # folder notes are written where they are shown.
    for path in SWIFT_FILES:
        name = path.name
        for words in _swift_strings(path.read_text(encoding="utf-8")):
            for phrase in phrases:
                found = re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", words, re.IGNORECASE)
                assert found is None, (name, phrase, words)


def _spec_verb_codes() -> dict[str, set[str]]:
    """The error codes each verb the app runs can give: its row in spec/CLI.md section 7's table,
    and the codes every verb can give, from the sentence before it."""
    text = SPEC_CLI.read_text(encoding="utf-8")
    section = text.split("## 7. Verbs", 1)[1].split("\n## ", 1)[0]
    sentence = re.search(r"one of the codes every verb can give: (.*?)\.\n", section, re.DOTALL)
    assert sentence is not None
    every = set(re.findall(r"`([a-z_]+)`", sentence.group(1)))
    assert every == {"usage", "internal", "bad_profile", "unsupported_platform", "os_error"}
    rows: dict[str, set[str]] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        verb = re.match(r"`([^`]+)`", cells[0])
        assert verb is not None, line
        rows[verb.group(1)] = set(re.findall(r"`([a-z_]+)`", cells[-1])) | every
    wanted = {
        "status",
        "forget",
        "pair --code-stdin [--hub URL] [--store-file]",
        "pair status",
        "pair confirm",
        "pair reject",
        "run --json-events [--exit-on-stdin-eof] [--allow-downgrade]",
    }
    assert wanted <= set(rows), sorted(rows)
    return {verb: rows[verb] for verb in wanted}


def _spec_error_codes() -> set[str]:
    text = SPEC_CLI.read_text(encoding="utf-8")
    section = text.split("## 10. Error codes", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE))


def test_every_error_code_the_app_meets_has_words() -> None:
    words = _swift("ConnectionText.swift")
    refusals = _swift_string_table(words, "refusals")
    app_bugs = _swift_string_set(words, "appBugCodes")
    redraw = _swift_string_set(_swift("Pairing.swift"), "redrawCodes")
    assert redraw == {"waiting", "sas_ready"}
    handled = set(refusals) | app_bugs | redraw
    codes = _spec_verb_codes()
    met = set().union(*codes.values())
    assert met - handled == set(), sorted(met - handled)
    # Each code has one meaning here, and none is a word the contract does not have.
    assert not set(refusals) & app_bugs and not (set(refusals) | app_bugs) & redraw
    assert handled <= _spec_error_codes(), sorted(handled - _spec_error_codes())
    # The code window comes back only for refusals that have words.
    again = _swift_string_set(_swift("Pairing.swift"), "newCodeCodes")
    assert again <= set(refusals)
    # A code expiring, the time to compare running out, and a leaked code read apart.
    after = re.search(r'static let expiredAfterSAS = "([^"]+)"', words)
    assert after is not None
    assert len({refusals["expired"], after.group(1), refusals["leaked"]}) == 3
    # held covers a pair still being set up, not only a connected one.
    assert "or one being set up" in refusals["held"]
    assert refusals["busy"] == "An earlier attempt is still finishing. Try again in a minute."


def test_every_stopped_reason_is_read() -> None:
    # Every reason run's stopped object can carry (spec/CLI.md section 6) is a case the app reads,
    # and has the exit code the contract gives it.
    text = SPEC_CLI.read_text(encoding="utf-8")
    table = text.split("### `stopped`: why `run` ended", 1)[1].split("\n### ", 1)[0]
    reasons: dict[str, int] = {}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[2].isdigit():
            for reason in re.findall(r"`([a-z_]+)`", cells[0]):
                reasons[reason] = int(cells[2])
    assert len(reasons) == 15, reasons
    json = _swift("HostJSON.swift")
    reader = _function_body(json, "    private static func readStopReason(")
    exit_codes = _function_body(json, "    var exitCode: Int32? {")
    for reason in reasons:
        assert f'case "{reason}":' in reader, reason
    by_code = re.findall(r"case (\.[^:]+):\n\s+return (\d+)", exit_codes)
    assert {int(code) for _, code in by_code} == set(reasons.values())


def test_the_app_stops_run_with_the_ladder_the_design_gives() -> None:
    supervisor = _swift("Supervisor.swift")
    for pin in (
        "static let crashWindow: TimeInterval = 600",
        "static let crashLimit = 5",
        "static let quitBound: TimeInterval = 330",
        "static let logoutBound: TimeInterval = 20",
        "static let afterKill: TimeInterval = 40",
        "static let afterStopped: TimeInterval = 10",
        "static let stateSilence: TimeInterval = 120",
        "static let hubStale: TimeInterval = 120",
    ):
        assert pin in supervisor, pin
    # run is started in one place, which only a confirmed status read reaches. These count text,
    # so a moved start gets past them: they are a quick check, and the self-test's walk of the
    # supervisor is the guard.
    assert supervisor.count("spawn(now: now)") == 2
    reader = _function_body(supervisor, "    private mutating func statusRead(")
    assert reader.count("return spawn(now: now)") == 2
    assert reader.index("case .confirmed:") < reader.index("spawn(now: now)")
    assert supervisor.count("[.spawnRun, .terminationGuard(true)]") == 1
    # resume runs only when the user asks.
    assert supervisor.count("[.runResume]") == 1
    assert "case .userResume:\n            return [.runResume]" in supervisor


def test_the_compare_page_keeps_its_safety_properties() -> None:
    # Neither panel hides when the user looks at Lucille's page in another app.
    for name in ("PairingPanel.swift", "ProgressPanel.swift"):
        assert "panel.hidesOnDeactivate = false" in _swift(name), name
    panel = _swift("PairingPanel.swift")
    # The compare page gives neither button a key equivalent, and starts with nothing focused, so
    # Return and Space answer nothing; Connect, on the code page, is the only default button.
    compare = _swift_function(panel, "    private func comparePage(")
    assert "keyEquivalent" not in compare
    assert panel.count('keyEquivalent = "\\r"') == 1
    assert 'connect.keyEquivalent = "\\r"' in panel
    shown = _swift_function(panel, "    func showCompare(")
    assert "panel.initialFirstResponder = nil" in shown
    assert "_ = panel.makeFirstResponder(nil)" in shown
    # No timer in the default mode, which stops while a note, a menu or the wait to quit runs.
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bTimer\s*\(|scheduledTimer", text), path.name


def test_every_close_of_the_pairing_window_reaches_the_flow() -> None:
    # On macOS 15 the title bar's close button, pressed through accessibility, closed the window
    # without going through GatePanel's performClose(_:), and the flow went on taking the compare
    # page for open, so the menu never offered Finish Connecting… again. The panel is the window's
    # delegate: before a user's close it tells the flow and answers no, the flow hiding the window
    # once it agrees; a close that asked nobody still tells the flow.
    panel = _swift("PairingPanel.swift")
    assert "final class PairingPanel: NSObject, NSWindowDelegate {" in panel
    assert "panel.delegate = self" in _swift_function(panel, "    override init() {")
    should = _swift_function(panel, "    func windowShouldClose(_ sender: NSWindow) -> Bool {")
    assert should.count("onClose?()") == 1
    assert should.index("onClose?()") < should.index("return false")
    assert "return true" not in should
    closing = _swift_function(panel, "    func windowWillClose(_ notification: Notification) {")
    assert closing.index("guard page != .none else {") < closing.index("onClose?()")
    # performClose(_:) still asks the flow and never closes the window itself.
    gate = _swift_function(panel, "    override func performClose(_ sender: Any?) {")
    assert "onCloseAsked?()" in gate and "super" not in gate
    # Nothing closes the panel but AppKit on the user's behalf: the flow's hide() orders it out.
    assert "panel.close()" not in panel
    assert "panel.orderOut(nil)" in _swift_function(panel, "    func hide() {")


def test_modal_loops_never_start_inside_a_main_queue_callout() -> None:
    # A modal loop started inside a main-queue callout (a dispatch source's handler, a main-queue
    # block, a main-actor task) keeps the main queue from being served until it ends: no timer,
    # no task and no child's event, the reply to quit included. So every note, question, Open panel
    # and quit starts in a RunLoopTurn, and SIGTERM is not handled on the main queue at all. These
    # read the source; the pairing check's steps with a note up are what show it on a Mac.
    connection = _swift("Connection.swift")
    model = _swift("HostModel.swift")
    assert "RunLoop.main.perform(inModes: [.common]) {" in connection
    assert "RunLoopTurn.perform {" in _swift_function(connection, "    private func pumpSoon(")
    quit = _swift_function(connection, "    func quitChosen() {")
    assert quit.index("RunLoopTurn.perform") < quit.index("terminate(nil)")
    assert (
        sum(path.read_text(encoding="utf-8").count("terminate(nil)") for path in SWIFT_FILES) == 1
    )
    for chosen in ("    func disconnectChosen() {", "    func stopConnectingChosen() {"):
        assert "RunLoopTurn.perform" in _swift_function(connection, chosen), chosen
    assert "RunLoopTurn.perform" in _swift_function(model, "    func addFolder() {")
    adds = _swift_function(model, "    private func addRoot(")
    assert "await RunLoopTurn.ask({ self.refuseWholeDisk() })" in adds
    assert "await RunLoopTurn.ask({ self.confirmBroadFolder(.holdsHome) })" in adds
    # The modal loops there are, each reached only from one of those turns (or from AppKit's own
    # applicationShouldTerminate, for the quit question).
    assert connection.count("runModal()") == 4
    assert model.count("runModal()") == 4
    for path in SWIFT_FILES:
        if path.name not in ("Connection.swift", "HostModel.swift"):
            assert "runModal()" not in path.read_text(encoding="utf-8"), path.name
    reply = _swift_function(connection, "    private func replyIfDone() {")
    assert "RunLoopTurn.perform" in reply and "DispatchQueue.main.async" not in reply
    # SIGTERM: a source on a queue of its own, which stops the children and exits.
    assert "makeSignalSource(signal: SIGTERM, queue: queue)" in connection
    assert "makeSignalSource(signal: SIGTERM, queue: .main)" not in connection
    assert 'private static let queue = DispatchQueue(label: "signals"' in connection


# --- The macOS checks of pairing and run: check_app.py ui and pairing --------------------------


def _python_function(source: str, name: str) -> str:
    """The source of the function ``name`` in ``source``."""
    import ast

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            found = ast.get_source_segment(source, node)
            assert found is not None, name
            return found
    raise AssertionError(f"no function {name}")


_CHECK_APP = build_app.TOOLS_DIR / "check_app.py"


def test_the_checks_use_the_apps_own_profile() -> None:
    # The ui and pairing checks read and change the host's state on the profile the app runs
    # every verb on, as the app does.
    assert f'static let appProfile = "{check_app.APP_PROFILE}"' in _swift("HostCLI.swift")
    assert check_app._profile_folder() == (
        Path.home() / "Library" / "Application Support" / "lucille-host" / check_app.APP_PROFILE
    )
    source = _CHECK_APP.read_text(encoding="utf-8")
    ui = _python_function(source, "check_ui")
    assert 'flag = _profile_folder() / "state" / "paused"' in ui
    # The code window comes last: after the whole disk, before the app is stopped.
    assert ui.index("_drive_whole_disk(") < ui.index(
        "_drive_code_window(checker, python_in(app), screens, (out_log, err_log))"
    )
    for name in (
        "_op_state",
        "_roots",
        "_drive_abilities",
        "_drive_folders",
        "_drive_whole_disk",
        "_drive_code_window",
        "_pair_state",
    ):
        body = _python_function(source, name)
        assert not re.search(r"(?<!\w)_host\(", body), name
    assert '["--profile", APP_PROFILE, *args]' in _python_function(source, "_app_host")
    # The pairing check and the probe run the host as the app does: the app's flags
    # (build_app.RUNTIME_FLAGS, through host_argv), on the app's profile.
    assert 'return host_argv(self.python, ["--profile", APP_PROFILE, *args])' in source
    assert 'host_argv(python, ["--profile", APP_PROFILE, *args]),' in _python_function(
        source, "_probe_one"
    )
    assert '"-I", "-B", "-m", "lucille_host"' not in source


def test_the_pairing_checks_look_for_the_apps_own_words() -> None:
    text = _swift("ConnectionText.swift")
    words = {
        "connectItem": check_app.CONNECT_ITEM,
        "finishItem": check_app.FINISH_ITEM,
        "disconnectItem": check_app.DISCONNECT_ITEM,
        "notConnected": check_app.NOT_CONNECTED,
        "connecting": check_app.CONNECTING,
        "connected": check_app.CONNECTED,
        "codeTitle": check_app.CODE_TITLE,
        "codeSteps": check_app.CODE_WORDS[0],
        "codePasteHow": check_app.CODE_WORDS[1],
        "codeWarning": check_app.CODE_WORDS[2],
        "codeHint": check_app.CODE_HINT,
        "connectButton": check_app.CONNECT_BUTTON,
        "compareTitle": check_app.COMPARE_TITLE,
        "compareQuestion": check_app.COMPARE_QUESTION,
        "theyMatch": check_app.THEY_MATCH,
        "confirmedTitle": check_app.CONFIRMED_TITLE,
        "quitQuestionTitle": check_app.QUIT_QUESTION,
        "quitQuestionText": check_app.QUIT_TEXT,
        "quitButton": check_app.QUIT_BUTTON,
        "finishing": check_app.FINISHING,
        "stopNow": check_app.STOP_NOW,
        "stoppingNow": check_app.STOPPING_NOW,
        "pausedTitle": check_app.PAUSED_TITLE,
        "resumeButton": check_app.RESUME_BUTTON,
        "disconnectQuestionTitle": check_app.DISCONNECT_TITLE,
        "disconnectButton": check_app.DISCONNECT_BUTTON,
        "disconnectedTold": check_app.DISCONNECTED_TOLD,
    }
    for name, said in words.items():
        assert f'static let {name} = "{_named_in_swift(said)}"' in text, name
    assert f'"bad_code": "{check_app.BAD_CODE}",' in text
    assert check_app.CONNECT_ITEM in check_app.MENU_ITEMS
    # The lines the app writes on standard error as run exits (RunSupervisor.describe), which the
    # pairing check reads.
    supervisor = _swift("Supervisor.swift")
    assert 'how = "run exited \\(code)"' in supervisor
    assert 'how = "run ended by signal \\(number)"' in supervisor
    assert 'return how + " (" + (stopped?.words ?? "no stopped object") + ")"' in supervisor
    assert 'return "drained, stdin_end"' in _swift("HostJSON.swift")
    assert check_app.DRAINED_LINE == "run exited 0 (drained, stdin_end)"
    assert check_app.SIGNALED_LINE == "run ended by signal"
    # Every word the checks hand AppleScript can be quoted for it.
    assert f'static let quitApp = "{_named_in_swift(check_app.QUIT_ITEM)}"' in _swift(
        "HostText.swift"
    )
    for said in [*words.values(), "OK", "Cancel", check_app.QUIT_ITEM]:
        assert check_app._quoted(said) == f'"{said}"'
    # The text the ui check pastes passes the app's own check on a code, and so would the one it
    # leaves on the pasteboard, which the app must never read by itself.
    pairing = _swift("Pairing.swift")
    assert 'static let codePrefix = "swbp1_"' in pairing
    # The whole code is then at most 8192 characters, and with its newline fits the input a child
    # gets.
    assert "static let codeBodyLength = 100...8186" in pairing
    assert "static let maxInputBytes = 8193" in _swift("HostCLI.swift")
    for code in (check_app.PASTED_TEXT, check_app.PASTEBOARD_SENTINEL):
        assert re.fullmatch(r"swbp1_[A-Za-z0-9_-]{100,8186}", code), code
    assert check_app.PASTED_TEXT != check_app.PASTEBOARD_SENTINEL


def test_the_applescript_the_checks_build_names_what_it_looks_for() -> None:
    script = check_app._target("button", check_app.THEY_MATCH)
    # Each window's own elements, as `button "OK" of window 1` finds a note's: a walk through
    # `entire contents` found nothing in the code window on macOS 15.
    assert (
        'if exists (button "They Match" of theWindow) then '
        'set theTarget to button "They Match" of theWindow\n'
    ) in script
    assert script.endswith('error "no button named They Match on the screen:" & held\nend if\n')
    assert "role of every UI element of theWindow" in script
    assert "if exists (text field 1 of theWindow) then" in check_app._target("text field")
    assert "entire contents" not in _CHECK_APP.read_text(encoding="utf-8").replace(
        "does not walk ``entire contents``", ""
    )
    for unquotable in ('say "this"', "back\\slash"):
        with pytest.raises(ValueError):
            check_app._quoted(unquotable)


def test_reading_every_text_says_what_the_windows_hold_when_it_finds_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts: list[str] = []
    answers = iter([(True, ""), (True, " [Connect to Lucille: AXGroup, AXButton]")])

    def fake(body: str, timeout: float = 30) -> tuple[bool, str]:
        scripts.append(body)
        return next(answers)

    monkeypatch.setattr(check_app, "_system_events", fake)
    assert check_app._texts() == []
    # Each window's own elements, never a walk through `entire contents`.
    assert "set found to found & (value of every static text of theWindow)" in scripts[0]
    assert "entire contents" not in scripts[0]
    # Nothing found: it asks what the windows hold, and says so.
    assert scripts[1] == check_app.WINDOWS_HOLD + "return held"
    assert "AXGroup" in capsys.readouterr().out


def test_the_pairing_check_imports_the_harness_only_when_it_runs() -> None:
    # Every other check runs where neither Switchboard nor the test harness is installed, and this
    # suite imports check_app wherever it runs.
    import ast
    import subprocess

    source = _CHECK_APP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    top: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module.split(".")[0])
    assert not {"tests", "switchboard"} & top, top
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    importing = {
        node.name: {
            inner.module
            for inner in ast.walk(node)
            if isinstance(inner, ast.ImportFrom) and inner.module
        }
        for node in functions
        if any(isinstance(inner, ast.Import | ast.ImportFrom) for inner in ast.walk(node))
    }
    assert importing == {
        "check_pairing": {"switchboard.client", "tests._lucille", "tests._process", "tests._run"}
    }
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\nimport tools.macos.check_app\n"
            "loaded = {name.split('.')[0] for name in sys.modules}\n"
            "print(sorted(loaded & {'switchboard', 'tests'}))",
        ],
        cwd=build_app.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, "[]\n"), done.stderr


def test_the_probe_never_fails_the_job(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The probe prints what the app's verbs do on the runner and gates nothing: whatever it meets,
    # a program that cannot be started included, it says so and returns.
    def refused(*args: object, **kwargs: object) -> None:
        raise OSError("no such program")

    monkeypatch.setattr("subprocess.run", refused)
    check_app.check_probe(Path("/nonexistent/Capsule.app"))
    assert "no such program" in capsys.readouterr().out


def test_the_pairing_check_says_when_its_harness_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run with an interpreter that lacks Switchboard or the test harness, it fails and says which
    # interpreter it needs, before it touches the app or the host. (None in sys.modules makes an
    # import of that name fail.)
    monkeypatch.setitem(sys.modules, "switchboard.client", None)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("ran a program"))
    checker = check_app.Checker()
    check_app.check_pairing(tmp_path / "Capsule.app", checker, tmp_path / "check")
    assert checker.failures == ["the pairing check finds Switchboard and the test harness"]
    assert "suite --copy-to" in capsys.readouterr().out


# --- The Open panel, on every macOS the jobs run: check_app.py ui -----------------------------
# On macOS 26 and 27 (the smoke jobs, 2026-11-02) `button "Allow Capsule to Use This Folder" of
# window 1` was not found, where macOS 15 finds it. The ui check now looks for the panel's buttons
# in every window, sheet and group, and presses Return, the panel's default button, only when the
# prompt is nowhere; what the app shows next is checked as before.

_PLACES = ("theWindow", "theSheet", "theGroup", "theInner")


def _looks_for(script: str, name: str) -> bool:
    return all(
        f'if exists (button "{name}" of {place}) then set theTarget to button "{name}" of '
        f"{place}\n" in script
        for place in _PLACES
    )


def test_choosing_in_the_open_panel_looks_in_every_window_sheet_and_group() -> None:
    script = check_app.CHOOSE_IN_PANEL
    assert _looks_for(script, check_app.OPEN_PROMPT)
    for walk in (
        "repeat with theWindow in windows\n",
        "repeat with theSheet in sheets of theWindow\n",
        "repeat with theInner in groups of theSheet\n",
        "repeat with theGroup in groups of theWindow\n",
        "repeat with theGroup in splitter groups of theWindow\n",
        "repeat with theInner in groups of theGroup\n",
    ):
        assert walk in script, walk
    # Never only window 1, and never a walk through `entire contents`.
    assert "window 1" not in script and "entire contents" not in script
    # Found: clicked, and nothing else is pressed.
    clicked = script.index('click theTarget\ndelay 2\nreturn "clicked"\n')
    assert script.count("click theTarget") == 1
    assert script.index(f'button "{check_app.OPEN_PROMPT}"') < clicked


def test_return_is_pressed_only_when_neither_the_prompt_nor_what_follows_is_found() -> None:
    script = check_app.CHOOSE_IN_PANEL
    clicked = script.index('return "clicked"')
    # One of the app's own windows that follow the panel already up: nothing is pressed, and the
    # caller checks that window.
    for after in check_app.AFTER_THE_PANEL:
        assert _looks_for(script, after), after
        assert script.index(f'button "{after}"') > clicked
    past = script.index('return "already past the Open panel: it shows "')
    # Otherwise: what the windows hold, then Return, once, and it says so.
    held = script.index(check_app.WINDOWS_HOLD)
    pressed = script.index("set frontmost to true\nkey code 36\n")
    assert clicked < past < held < pressed
    assert script.count("key code") == 1
    assert script.endswith(
        f'return "pressed Return, the default button: no button named " & '
        f'"{check_app.OPEN_PROMPT}" & ", and the app\'s windows held" & held'
    )
    # What follows the panel is the app's own: the name dialog's button, and the broad-folder and
    # whole-disk notes' (App/HostModel.swift).
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    for after in check_app.AFTER_THE_PANEL:
        assert f'alert.addButton(withTitle: "{after}")' in model, after
    assert set(check_app.AFTER_THE_PANEL) >= {"Add Anyway", check_app.WHOLE_DISK_BUTTONS[0]}


def test_what_the_windows_hold_names_their_buttons_and_sheets() -> None:
    hold = check_app.WINDOWS_HOLD
    assert "role of every UI element of theWindow" in hold
    assert "name of every button of theWindow" in hold
    assert "repeat with theSheet in sheets of theWindow\n" in hold
    # Each part in a try of its own: a window that cannot say one thing still says the rest.
    lines = hold.splitlines()
    assert lines.count("try") == lines.count("end try") == 3


@pytest.mark.parametrize(
    ("answer", "worked", "note"),
    [
        ((True, "clicked"), True, False),
        ((True, "already past the Open panel: it shows Add Folder"), True, True),
        (
            (True, "pressed Return, the default button: no button named x [Add a Folder: AXGroup]"),
            True,
            True,
        ),
        ((False, "execution error: no process"), False, False),
    ],
)
def test_going_to_a_folder_chooses_through_the_panel_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    answer: tuple[bool, str],
    worked: bool,
    note: bool,
) -> None:
    scripts: list[str] = []
    answers = iter([(True, ""), answer])

    def fake(body: str, timeout: float = 30) -> tuple[bool, str]:
        scripts.append(body)
        return next(answers)

    monkeypatch.setattr(check_app, "_system_events", fake)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    folder = Path("/Users/runner/lucille-host-ui-check/My Project Files")
    done = check_app._go_to_folder(folder, tmp_path, "5-open-panel.png")
    # Go to Folder, typed; then the panel script, as it is.
    assert f'keystroke "{folder}"\n' in scripts[0]
    assert scripts[1] == check_app.CHOOSE_IN_PANEL
    assert done[0] is worked
    printed = capsys.readouterr().out
    assert (f"note: choosing {folder} in the Open panel: {answer[1]}" in printed) is note
    if not worked:
        assert done[1] == f"choosing the folder: {answer[1]}"


def test_the_panel_checks_read_every_window_sheet_and_group() -> None:
    source = _CHECK_APP.read_text(encoding="utf-8")
    # The checks that the Open panel came back, or did not, read its buttons wherever they sit.
    for name in ("_drive_folders", "_drive_whole_disk"):
        body = _python_function(source, name)
        assert "OPEN_PROMPT in buttons" in body, name
        reads = re.findall(r"(\w+), buttons = (_\w+)\(([^)]*)\)\n", body)
        # Every read of buttons after which the Open panel's prompt is looked for.
        for found, function, _ in reads:
            after = body.split(f"{found}, buttons = {function}(", 1)[1].split("checker.expect", 2)
            if "OPEN_PROMPT" in after[1]:
                assert function == "_panel_buttons", (name, found, function)
    folders = _python_function(source, "_drive_folders")
    disk = _python_function(source, "_drive_whole_disk")
    assert folders.count("worked, buttons = _panel_buttons()") == 1
    assert disk.count("worked, buttons = _panel_buttons()") == 1
    assert disk.count("still, buttons = _panel_buttons()") == 1
    # No check looks for the prompt in window 1 alone any more.
    assert 'button "{OPEN_PROMPT}" of window 1' not in source
    assert "_in_each_place(" in _python_function(source, "_panel_buttons")
