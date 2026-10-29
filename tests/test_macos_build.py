"""The parts of macos/build_app.py and macos/check_app.py that run anywhere: the pins, the
sha256 gate, the hashed build backend, Info.plist, the disk image's name, the parsers the checks
use, the workflow's gates, and the rules the Swift sources keep. The build itself runs on a Mac,
in the macos-app workflow.
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
import yaml

import lucille_host
from lucille_host import policy
from lucille_host.ops import OPERATIONS
from macos import build_app, check_app
from tools.guards import ast_lint

PINS_TEXT = build_app.PINS_FILE.read_text(encoding="utf-8")
CONSTRAINTS_TEXT = build_app.BUILD_CONSTRAINTS.read_text(encoding="utf-8")
SWIFT_FILES = sorted(build_app.SWIFT_DIR.glob("*.swift"))
WORKFLOW = build_app.REPO_ROOT / ".github" / "workflows" / "macos-app.yml"


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


def test_the_app_imports_only_apples_ui_frameworks() -> None:
    assert SWIFT_FILES
    for path in SWIFT_FILES:
        assert _swift_imports(path.read_text(encoding="utf-8")) <= _APP_IMPORTS, path.name


# Ways Swift code can start a process. Only HostCLI.swift may use Foundation's Process, and it
# may use nothing else on this list.
_STARTS_A_PROCESS = (
    r"\bProcess\b",
    r"\bNSTask\b",
    r"posix_spawn",
    r"\bv?fork\s*\(",
    r"\bf?exec[lv]p?[eP]?\s*\(",
    r"\bsystem\s*\(",
    r"\bpopen\s*\(",
    r"NSWorkspace",
    r"openApplication",
    r"NSAppleScript",
    r"OSAScript",
    r"NSUserScriptTask",
    r"NSUserUnixTask",
    r"\bdlopen\b",
    r"\bdlsym\b",
    r"launchPath",
    r"/bin/sh\b",
    r"/bin/zsh\b",
)
# Ways Swift code can open a network connection, listen, or read a URL that is not a file.
_USES_THE_NETWORK = (
    r"URLSession",
    r"URLRequest",
    r"NSURLConnection",
    r"NSURLDownload",
    r"\bNW[A-Z]\w*",
    r"CFSocket",
    r"CFStream",
    r"CFReadStream",
    r"CFWriteStream",
    r"CFHTTP",
    r"\bInputStream\b",
    r"\bOutputStream\b",
    r"getStreamsToHost",
    r"NetService",
    r"NSXPCConnection",
    r"\bsocket\s*\(",
    r"\bbind\s*\(",
    r"\blisten\s*\(",
    r"\bconnect\s*\(",
    r"\baccept\s*\(",
    r"getaddrinfo",
    r"gethostbyname",
    r"URL\(\s*string\s*:",
    r"URLComponents",
    r"https?://",
    r"(?<!append\()(?<!insert\()\bcontentsOf\s*:",
    r"WKWebView",
)


def _matches(patterns: tuple[str, ...], text: str) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text)]


@pytest.mark.parametrize(
    "line",
    [
        "let p = Process()",
        "let p = Process.init()",
        "try Process.run(URL(fileURLWithPath: path), arguments: [])",
        "let t = NSTask()",
        "let f = posix_spawn",
        "_ = fork()",
        "execv(path, argv)",
        "system(command)",
        'NSWorkspace.shared.open(URL(fileURLWithPath: "/"))',
        "dlopen(nil, 0)",
    ],
)
def test_the_process_scan_sees_each_way(line: str) -> None:
    assert _matches(_STARTS_A_PROCESS, line), line


@pytest.mark.parametrize(
    "line",
    [
        "let task = URLSession.shared.dataTask(with: url)",
        'try? Data(contentsOf: URL(string: "https://example.com")!)',
        "try? String(contentsOf: remote, encoding: .utf8)",
        "let c = NWConnection(host: host, port: port, using: .tcp)",
        "let l = try NWListener(using: .tcp)",
        "let fd = socket(AF_INET, SOCK_STREAM, 0)",
        "bind(fd, address, size)",
        "listen(fd, 5)",
        "CFStreamCreatePairWithSocketToHost(nil, host, 80, &read, &write)",
        "NSURLConnection(request: request, delegate: nil)",
    ],
)
def test_the_network_scan_sees_each_way(line: str) -> None:
    assert _matches(_USES_THE_NETWORK, line), line


def test_the_scans_pass_what_the_app_does() -> None:
    for line in (
        "view.append(contentsOf: scalars)",
        "let info = ProcessInfo.processInfo.environment",
        "kill(process.processIdentifier, SIGKILL)",
        "Binding(get: { ability.isOn }, set: { model.setAbility(ability, on: $0) })",
        'URL(fileURLWithPath: "/")',
    ):
        assert not _matches(_USES_THE_NETWORK, line), line
    assert not _matches(_STARTS_A_PROCESS[1:], "let info = ProcessInfo.processInfo"), "ProcessInfo"


def test_the_app_starts_only_the_embedded_interpreter() -> None:
    assert SWIFT_FILES
    for path in SWIFT_FILES:
        found = _matches(_STARTS_A_PROCESS, path.read_text(encoding="utf-8"))
        expected = [r"\bProcess\b"] if path.name == "HostCLI.swift" else []
        assert found == expected, (path.name, found)
    cli = (build_app.SWIFT_DIR / "HostCLI.swift").read_text(encoding="utf-8")
    assert '"Contents/Resources/python/bin/python3"' in cli
    assert '["-I", "-B", "-m", "lucille_host"]' in cli


def test_the_app_uses_no_network() -> None:
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not _matches(_USES_THE_NETWORK, text), path.name
        for needle in ("UserDefaults", "SWITCHBOARD_"):
            assert needle not in text, (path.name, needle)


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


def test_the_disk_image_name_says_it_is_ad_hoc() -> None:
    name = build_app.dmg_name("0.1.0", "4466aba0c0de")
    assert name == "Lucille-Host-0.1.0-4466aba0c0de-adhoc-macOS-arm64.dmg"
    assert name.endswith(build_app.DMG_SUFFIX)
    assert name != "Lucille-Host-0.1.0-macOS-arm64.dmg"
    for commit in ("", "HEAD", "4466ABA", "4466ab", "../x"):
        with pytest.raises(build_app.BuildError):
            build_app.dmg_name("0.1.0", commit)


def test_only_this_scripts_disk_images_are_found(tmp_path: Path) -> None:
    ours = tmp_path / build_app.dmg_name("0.1.0", "abcdef1")
    ours.write_bytes(b"")
    (tmp_path / "Lucille-Host-0.1.0-macOS-arm64.dmg").write_bytes(b"")
    (tmp_path / "other.dmg").write_bytes(b"")
    assert build_app.disk_images(tmp_path) == [ours]


def _steps() -> list[dict[str, object]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["app"]["steps"]


def test_the_workflow_uploads_the_disk_image_only_from_a_good_build_of_this_repository() -> None:
    uploads = [
        step for step in _steps() if str(step.get("with", {}).get("path", "")).endswith(".dmg")
    ]
    assert len(uploads) == 1
    upload = uploads[0]
    condition = str(upload["if"])
    assert "success()" in condition and "cancelled" not in condition
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    assert upload["with"]["path"] == f"build/macos/*{build_app.DMG_SUFFIX}"
    assert "adhoc" in upload["with"]["name"]


def test_the_workflow_requires_the_menu_to_be_driven() -> None:
    runs = [str(step.get("run", "")) for step in _steps()]
    ui = [run.strip() for run in runs if "check_app.py ui" in run]
    assert ui == ["uv run --no-project python macos/check_app.py ui --require-driven"]


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
    ]
    assert check_app.forbidden_imports(allowed + refused) == refused
