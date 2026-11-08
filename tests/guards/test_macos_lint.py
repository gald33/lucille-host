"""tools/guards/macos_lint.py reports each planted violation and passes the repository."""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools.guards import macos_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "guards" / "macos_lint.py"
PRIVACY_TEXT = (REPO_ROOT / "PRIVACY.md").read_text(encoding="utf-8")

APP = "macos/App/Example.swift"
TEST = "macos/Tests/Example.swift"
PROCESS = "macos/App/HostCLI.swift"
BUILD = "tools/macos/build_app.py"
TOOL = "tools/macos/check_app.py"
PINS = "tools/macos/pins.toml"
WORKFLOW = ".github/workflows/example.yml"
STRINGS = "macos/App/en.lproj/Localizable.strings"
PLIST = "macos/App/Info.plist"

# The one file that may start a process, as the lint requires it: the bundled runtime by its
# path in the app, and the flags as one array literal.
HOST_CLI = textwrap.dedent(
    """\
    import Foundation

    struct HostCLI {
        static func python(inApp app: URL) -> URL {
            app.appendingPathComponent("Contents/Resources/runtime/bin/lucille-host-runtime")
        }

        func arguments(for verb: [String]) -> [String] {
            ["-I", "-B", "-X", "utf8", "-m", "lucille_host"] + verb
        }

        func run(_ verb: [String]) {
            let process = Process()
            process.arguments = arguments(for: verb)
        }
    }
    """
)

# build_app.py as the lint reads it: the plist functions and the set of keys the build refuses.
BUILD_APP = textwrap.dedent(
    '''\
    """The build. It makes no .pkg, runs no launchctl, and writes no CFBundleURLTypes."""

    FORBIDDEN_PLIST_KEYS = frozenset({"NSServices", "CFBundleURLTypes", "CFBundleURLSchemes"})


    def folder_usage(app_name):
        """What macOS shows. Never "Verified"."""
        return f"{app_name} lets Lucille use only the folders you choose."


    def info_plist(pins, versions):
        """The plist. It is not "audited" by this function."""
        return {"CFBundleName": pins.app_name, "NSHumanReadableCopyright": "MIT License"}
    '''
)

PINS_TEXT = textwrap.dedent(
    """\
    [app]
    name = "Capsule"
    bundle_id = "com.lucille-ai.capsule"
    category = "public.app-category.utilities"
    """
)

GOOD_WORKFLOW = textwrap.dedent(
    """\
    on: push
    jobs:
      build:
        runs-on: macos-15
        steps:
          - run: uv run --no-project python tools/macos/build_app.py build --build-number 1
          - run: hdiutil attach build/macos/Capsule.dmg
    """
)


def phrases() -> list[str]:
    return macos_lint.must_not_claim_phrases(PRIVACY_TEXT)


def swift_rules(text: str, rel: str = APP) -> set[str]:
    return {v.rule for v in macos_lint.check_swift(text, rel, phrases())}


def python_rules(text: str, rel: str = TOOL) -> set[str]:
    return {v.rule for v in macos_lint.check_python(text, rel, phrases())}


def workflow_rules(text: str) -> set[str]:
    return {v.rule for v in macos_lint.check_workflow(text, WORKFLOW)}


def pins_rules(text: str) -> set[str]:
    return {v.rule for v in macos_lint.check_pins(text, PINS, phrases())}


def resource_rules(text: str, rel: str = STRINGS) -> set[str]:
    return {v.rule for v in macos_lint.check_resource(text, rel, phrases())}


# --- Each rule, planted ----------------------------------------------------------------------

DENIED_SWIFT = [
    # decisions/0002 §20's list, as Swift spells each name.
    "let task = URLSession.shared.dataTask(with: url)",
    "let c = NWConnection(host: host, port: port, using: .tcp)",
    "let l = try NWListener(using: .tcp)",
    "let b = NWBrowser(for: descriptor, using: .tcp)",
    "import Network",
    "@_exported import Network",
    "import struct Network.NWEndpoint",
    "import WebKit",
    "let view = WKWebView()",
    "let c = NSXPCConnection(serviceName: name)",
    "let l = NSXPCListener.anonymous()",
    "let c = xpc_connection_create(nil, nil)",
    "bootstrap_look_up(port, name, &service)",
    "let s = CFSocketCreate(nil, 0, 0, 0, 0, nil, nil)",
    "let s: NSStream? = nil",
    "Stream.getStreamsToHost(withName: h, port: 80, inputStream: &i, outputStream: &o)",
    "let input = InputStream(url: url)",
    "let output = OutputStream(toMemory: ())",
    "DistributedNotificationCenter.default().post(name: n, object: nil)",
    "let script = NSAppleScript(source: text)",
    "let f = posix_spawn",
    "system(command)",
    'let shell = "/bin/sh"',
    'let shell = "/bin/zsh"',
    'let shell = "/bin/bash"',
    'let tool = "/usr/bin/env"',
    'let tool = "osascript"',
    # The scans test_macos_build.py used to run, ported here: other ways to start a process.
    "let t = NSTask()",
    "_ = fork()",
    "_ = vfork()",
    "execv(path, argv)",
    "execve(path, argv, envp)",
    "popen(command, mode)",
    'NSWorkspace.shared.open(URL(fileURLWithPath: "/"))',
    "app.openApplication(at: url, configuration: c)",
    "let s = OSAScript(source: text)",
    "let t = try NSUserUnixTask(url: url)",
    "let t = try NSUserScriptTask(url: url)",
    "dlopen(nil, 0)",
    "let f = dlsym(handle, name)",
    "task.launchPath = path",
    # ... and other ways to use the network, or to read a URL that is not a file.
    "let request = URLRequest(url: url)",
    "NSURLConnection(request: request, delegate: nil)",
    "NSURLDownload(request: request, delegate: nil)",
    "CFStreamCreatePairWithSocketToHost(nil, host, 80, &read, &write)",
    "CFReadStreamOpen(stream)",
    "CFWriteStreamOpen(stream)",
    "let m = CFHTTPMessageCreateRequest(nil, method, url, version)",
    "let service = NetService(domain: d, type: t, name: n)",
    "let fd = socket(AF_INET, SOCK_STREAM, 0)",
    "bind(fd, address, size)",
    "listen(fd, 5)",
    "connect(fd, address, size)",
    "accept(fd, nil, nil)",
    "getaddrinfo(host, port, &hints, &result)",
    "let h = gethostbyname(host)",
    "try? String(contentsOf: remote, encoding: .utf8)",
    "var parts = URLComponents()",
    # Every URL(string:) outside the one opener, a System Settings page included.
    'let url = URL(string: "x-apple.systempreferences:com.apple.preference.security")',
    "let url = URL(string: text)",
    "let url = URL( string : text)",
    "let url = NSURL(string: text)",
    'try? Data(contentsOf: URL(string: "https://example.com")!)',
    # A Mach service or a distributed-objects port is a listener too (M1).
    "let port = NSMachPort()",
    "let p = NSSocketPort(tcpPort: 0)",
    "let p = NSMessagePort()",
    "let p = CFMessagePortCreateLocal(nil, name, callback, nil, nil)",
    "let c = NSConnection(receivePort: p, sendPort: nil)",
    # The app keeps no settings of its own and reads nothing from Switchboard's environment.
    "UserDefaults.standard.set(true, forKey: key)",
    'let key = "SWITCHBOARD_HUB"',
    # A comment counts: the denylist reads the whole text.
    "// URLSession would be simpler",
    # Every NSUser*Task, not only the two first listed.
    "let t = try NSUserAppleScriptTask(url: url)",
    "let t = try NSUserAutomatorTask(url: url)",
    # URL(string:) by its initializer's name.
    "let url = URL.init(string: text)",
    "let url = URL . init ( string : text)",
    # The C spelling of the distributed notification center, and Launch Services' openers.
    "let center = CFNotificationCenterGetDistributedCenter()",
    "LSOpenCFURLRef(url as CFURL, nil)",
    "LSOpenFromURLSpec(&spec, nil)",
    # A comment between a name and its call does not hide the call.
    "connect/**/(fd, address, size)",
    "let fd = socket /* tcp */ (AF_INET, SOCK_STREAM, 0)",
    "try? String(contentsOf/* the file */: remote, encoding: .utf8)",
    "let url = URL(/* text */string: text)",
]

PLANTED: list[tuple[str, str, str]] = [
    *[("swift-denylist", APP, line) for line in DENIED_SWIFT],
    # The denylist holds in the tests' Swift and in the one process file too.
    ("swift-denylist", TEST, "let task = URLSession.shared.dataTask(with: url)"),
    ("swift-denylist", PROCESS, HOST_CLI + "let t = NSTask()\n"),
    ("swift-denylist", PROCESS, HOST_CLI.replace("let process", "process.launchPath = p\nlet p")),
    # Process outside the one file, in code or in a comment.
    ("process", APP, "let p = Process()"),
    ("process", APP, "let p = Process.init()"),
    ("process", APP, "try Process.run(URL(fileURLWithPath: path), arguments: [])"),
    ("process", APP, "/// Starts a Process."),
    ("process", TEST, "let p = Process()"),
    ("process", "macos/Sources/CapsuleKit/HostCLI.swift", HOST_CLI),
    # The one file without the bundled runtime's path or the flags, or with them only in a comment.
    (
        "process",
        PROCESS,
        HOST_CLI.replace("runtime/bin/lucille-host-runtime", "python/bin/python3"),
    ),
    ("process", PROCESS, HOST_CLI.replace('"-X", "utf8", ', "")),
    ("process", PROCESS, HOST_CLI.replace('"-B", ', "")),
    ("process", PROCESS, HOST_CLI.replace('"-m", "lucille_host"', '"-m", "lucille_host", "-v"')),
    (
        "process",
        PROCESS,
        HOST_CLI.replace(
            'app.appendingPathComponent("Contents',
            '// "Contents/Resources/runtime/bin/lucille-host-runtime"\n'
            '            app.appendingPathComponent("Contents/Resources/python/bin/python3")\n'
            '            // ("Contents',
        ),
    ),
    (
        "process",
        PROCESS,
        HOST_CLI.replace(
            '["-I", "-B", "-X", "utf8", "-m", "lucille_host"] + verb',
            '/* ["-I", "-B", "-X", "utf8", "-m", "lucille_host"] */ ["-I"] + verb',
        ),
    ),
    # A read of the pasteboard, or of what is dropped on the app.
    ("pasteboard-read", APP, "let text = NSPasteboard.general.string(forType: .string)"),
    ("pasteboard-read", APP, "let data = pasteboard.data(forType: .png)"),
    ("pasteboard-read", APP, "let urls = pasteboard.propertyList(forType: .fileURL)"),
    ("pasteboard-read", APP, "pasteboard.readObjects(forClasses: [NSString.self], options: nil)"),
    ("pasteboard-read", APP, "for item in pasteboard.pasteboardItems ?? [] {}"),
    ("pasteboard-read", APP, "let type = pasteboard.availableType(from: [.string])"),
    ("pasteboard-read", APP, "pasteboard.canReadObject(forClasses: classes, options: nil)"),
    ("pasteboard-read", APP, "pasteboard.canReadItem(withDataConformingToTypes: types)"),
    ("pasteboard-read", APP, "extension Thing: NSPasteboardReading {}"),
    ("pasteboard-read", APP, "PasteButton(payloadType: String.self) { _ in }"),
    ("pasteboard-read", APP, "view.onPasteCommand(of: [.text]) { _ in }"),
    ("pasteboard-read", APP, "view.pasteDestination(for: String.self) { _ in }"),
    ("pasteboard-read", APP, "func readSelection(from pboard: NSPasteboard) -> Bool"),
    ("pasteboard-read", APP, "let board = sender.draggingPasteboard"),
    ("pasteboard-read", APP, "view.onDrop(of: [.fileURL], isTargeted: nil) { _ in true }"),
    ("pasteboard-read", APP, "view.dropDestination(for: URL.self) { _, _ in true }"),
    # ... with a comment between the name and the call.
    ("pasteboard-read", APP, "let text = NSPasteboard.general.string/**/(forType: .string)"),
    # An http(s) URL anywhere in the Swift text, a comment included.
    ("http", APP, 'let site = "https://example.com"'),
    ("http", APP, 'let site = "http://example.com"'),
    ("http", APP, 'let hub = "wss://hub.example"'),
    ("http", APP, 'let f = "ftp://example.com"'),
    ("http", APP, "// See HTTPS://example.com"),
    ("http", TEST, 'let site = "https://example.com"'),
    # A literal whose value is a network URL, however it is escaped.
    ("http", APP, 'let site = "https:\\u{2F}/example.com"'),
    ("http", APP, 'let site = "h\\u{74}tps://example.com"'),
    ("http", APP, 'let site = #"wss:\\#u{2F}/hub.example"#'),
    # A must-not-claim phrase in an app string: plain, multi-line, raw, nested, escaped.
    ("must-not-claim", APP, 'let note = "Your data never leaves your device."'),
    ("must-not-claim", APP, 'let note = "Everything here stays private"'),
    ("must-not-claim", APP, 'let note = """\n    It is end-to-end\n    encrypted.\n    """'),
    ("must-not-claim", APP, 'let note = #"Sandboxed, and "safe to enable anything""#'),
    ("must-not-claim", APP, 'let note = "\\(flag ? "Read-only" : "Open")"'),
    ("must-not-claim", APP, 'let note = "Dele\\u{74}ed"'),
    ("must-not-claim", APP, 'let note = "The log is tamper-proof log, really: Tamper-proof log"'),
    (
        "must-not-claim",
        "macos/Sources/CapsuleKit/Words.swift",
        'static let line = "Passwords and keys never leave"',
    ),
    # A URL scheme, or code that handles one, in Swift, a comment included.
    ("url-scheme", APP, 'let key = "CFBundleURLTypes"'),
    ("url-scheme", APP, "// CFBundleURLSchemes"),
    ("url-scheme", APP, "view.onOpenURL { url in open(url) }"),
    (
        "url-scheme",
        APP,
        "manager.setEventHandler(self, andSelector: s, forEventClass: c,"
        " andEventID: AEEventID(kAEGetURL))",
    ),
    ("url-scheme", APP, "func application(_ application: NSApplication, open urls: [URL]) {}"),
    # ... whatever its internal name, as an array or an Array<URL>, a comment in it or not.
    ("url-scheme", APP, "func application(_ app: NSApplication, open u: [URL]) {}"),
    ("url-scheme", APP, "func application(_ app: NSApplication, open urls: Array<URL>) {}"),
    ("url-scheme", APP, "func application(_ app: NSApplication, open/**/urls: [URL]) {}"),
    # A daemon, or anything that runs as root.
    ("daemon", APP, 'let service = SMAppService.daemon(plistName: "x.plist")'),
    ("daemon", APP, 'let service = SMAppService .daemon(plistName: "x.plist")'),
    ("daemon", APP, 'let path = "/Library/LaunchDaemons/x.plist"'),
    ("daemon", APP, "SMJobBless(kSMDomainSystemLaunchd, label, auth, &error)"),
    ("daemon", APP, "SMJobSubmit(kSMDomainSystemLaunchd, job, auth, &error)"),
    ("daemon", APP, "AuthorizationExecuteWithPrivileges(auth, tool, [], args, nil)"),
    ("daemon", APP, 'let tool = "launchctl"'),
    ("daemon", APP, 'typealias S = SMAppService\nlet s = S.daemon/**/(plistName: "x.plist")'),
    ("daemon", APP, 'let tool = "launch\\u{63}tl"'),
    # An installer package.
    ("pkg", APP, 'let name = "Capsule.pkg"'),
    ("pkg", APP, 'let name = "Capsule.MPKG"'),
    ("pkg", APP, 'let tool = "pkgbuild"'),
    ("pkg", APP, 'let tool = "productbuild"'),
    ("pkg", APP, 'let tool = "productsign"'),
    ("pkg", APP, 'let argv = ["installer", "-pkg"]; let line = "installer -pkg x -target /"'),
    ("pkg", APP, 'let line = "installer -target / -pkg ./bundle"'),
    ("pkg", APP, 'let tool = "product\\u{62}uild"'),
]


def _planted_id(case: tuple[str, str, str]) -> str:
    rule, rel, text = case
    return f"{rule}:{rel.split('/')[-1]}:{text.splitlines()[-1][:40]}"


@pytest.mark.parametrize("case", PLANTED, ids=[_planted_id(case) for case in PLANTED])
def test_each_planted_swift_violation_is_reported(case: tuple[str, str, str]) -> None:
    rule, rel, text = case
    assert rule in swift_rules(text, rel), text


PLANTED_PYTHON: list[tuple[str, str, str]] = [
    # In the tools, a string constant (not a docstring) that names a URL scheme, a daemon or a .pkg.
    ("url-scheme", TOOL, 'KEYS = {"CFBundleURLTypes": []}\n'),
    ("url-scheme", TOOL, 'def check(plist):\n    return "CFBundleURLSchemes" in plist\n'),
    # The constant parts of an f-string count.
    ("url-scheme", TOOL, 'def key(n):\n    return f"CFBundleURLTypes{n}"\n'),
    ("daemon", TOOL, 'ARGV = ["launchctl", "load", "x.plist"]\n'),
    ("daemon", TOOL, 'FOLDER = "/Library/LaunchDaemons"\n'),
    ("pkg", TOOL, 'ARGV = ["pkgbuild", "--root", "out", "x"]\n'),
    ("pkg", TOOL, 'ARGV = ["productbuild", "--component", "a", "b"]\n'),
    ("pkg", TOOL, 'NAME = f"{app}.pkg"\n'),
    # Constants joined with +, and an f-string whose every part is constant, are read joined.
    ("url-scheme", TOOL, 'KEY = "CFBundleURL" + "Types"\n'),
    ("url-scheme", TOOL, "KEY = f\"CFBundleURL{'Types'}\"\n"),
    ("daemon", TOOL, 'FOLDER = "/Library/Launch" + "Daemons"\n'),
    ("pkg", TOOL, 'TOOL = "product" + "build"\n'),
    ("pkg", TOOL, 'TOOL = ("pkg" + "build")\n'),
    # In build_app.py, CFBundleURLTypes anywhere but in FORBIDDEN_PLIST_KEYS.
    (
        "url-scheme",
        BUILD,
        BUILD_APP.replace('"MIT License"}', '"MIT License", "CFBundleURLTypes": []}'),
    ),
    # ... and FORBIDDEN_PLIST_KEYS must refuse both URL-scheme keys.
    ("url-scheme", BUILD, BUILD_APP.replace('"CFBundleURLTypes", ', "")),
    ("url-scheme", BUILD, BUILD_APP.replace(', "CFBundleURLSchemes"', "")),
    ("url-scheme", BUILD, BUILD_APP.replace("FORBIDDEN_PLIST_KEYS = ", "OTHER_KEYS = ")),
    # A must-not-claim phrase in the plist functions, an f-string's constant part included.
    ("must-not-claim", BUILD, BUILD_APP.replace('"MIT License"', '"Verified"')),
    ("must-not-claim", BUILD, BUILD_APP.replace("choose.", "choose. Audited.")),
    ("must-not-claim", BUILD, BUILD_APP.replace("choose.", "choose, and stays private.")),
    ("must-not-claim", BUILD, BUILD_APP.replace('"MIT License"', '"End-to-end " + "encrypted"')),
    # A plist function that is missing, and a file that does not parse.
    ("parse", BUILD, BUILD_APP.replace("def info_plist(", "def plist(")),
    ("parse", BUILD, BUILD_APP.replace("def folder_usage(", "def usage(")),
    ("parse", TOOL, "def broken(:\n"),
]


@pytest.mark.parametrize("case", PLANTED_PYTHON, ids=[_planted_id(c) for c in PLANTED_PYTHON])
def test_each_planted_tool_violation_is_reported(case: tuple[str, str, str]) -> None:
    rule, rel, text = case
    assert rule in python_rules(text, rel), text


PLANTED_WORKFLOWS = [
    ("daemon", "      - run: sudo launchctl bootstrap system x.plist\n"),
    ("daemon", "      - run: cp x.plist /Library/LaunchDaemons/\n"),
    ("pkg", "      - run: pkgbuild --root out Capsule.pkg\n"),
    ("pkg", "      - run: sudo installer -pkg Capsule.pkg -target /\n"),
    ("pkg", "      - run: productsign --sign x a b\n"),
    ("pkg", "      - run: sudo installer -target / -pkg ./bundle\n"),
    ("pkg", "      - name: Capsule.pkg\n        run: echo\n"),
    ("url-scheme", '      - run: /usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" x\n'),
]


@pytest.mark.parametrize(("rule", "step"), PLANTED_WORKFLOWS)
def test_each_planted_workflow_violation_is_reported(rule: str, step: str) -> None:
    assert rule in workflow_rules(GOOD_WORKFLOW + step)


def test_a_workflow_finding_names_its_line() -> None:
    found = macos_lint.check_workflow(GOOD_WORKFLOW + PLANTED_WORKFLOWS[0][1], WORKFLOW)
    assert [(v.path, v.line, v.rule) for v in found] == [(WORKFLOW, 8, "daemon")]


PLANTED_RESOURCES = [
    ("must-not-claim", STRINGS, '"welcome" = "Every entry goes to a tamper-proof log.";\n'),
    ("url-scheme", PLIST, "<dict>\n  <key>CFBundleURLTypes</key>\n</dict>\n"),
    ("daemon", PLIST, "<string>/Library/LaunchDaemons/x.plist</string>\n"),
    ("must-not-claim", PLIST, "<string>Lucille cannot see your files</string>\n"),
    ("must-not-claim", "macos/App/Localizable.xcstrings", '{"strings": {"a": "Verified"}}\n'),
]


@pytest.mark.parametrize(("rule", "rel", "text"), PLANTED_RESOURCES)
def test_each_planted_resource_violation_is_reported(rule: str, rel: str, text: str) -> None:
    assert rule in resource_rules(text, rel)


def test_a_claim_in_the_pins_is_reported() -> None:
    assert pins_rules(PINS_TEXT) == set()
    assert "must-not-claim" in pins_rules(PINS_TEXT.replace('"Capsule"', '"Audited Capsule"'))
    found = macos_lint.check_pins(
        PINS_TEXT.replace('"Capsule"', '"Private Capsule"'), PINS, phrases()
    )
    assert [(v.line, v.rule) for v in found] == [(2, "must-not-claim")]


@pytest.mark.parametrize(
    "text",
    [
        "[app\n",
        '[python]\nversion = "3.13"\n',
        'app = "Capsule"\n',
    ],
)
def test_pins_the_lint_cannot_read_are_reported(text: str) -> None:
    assert "parse" in pins_rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "on: push\njobs: [unclosed\n",
        "- just\n- a list\n",
    ],
)
def test_a_workflow_that_does_not_parse_is_reported(text: str) -> None:
    assert "parse" in workflow_rules(text)


# --- What must not be reported ---------------------------------------------------------------

CLEAN_SWIFT = [
    "let info = ProcessInfo.processInfo.environment",
    "kill(process.processIdentifier, SIGKILL)",
    'let image = NSImage(systemSymbolName: "folder", accessibilityDescription: nil)',
    "view.append(contentsOf: scalars)",
    "view.insert(contentsOf: scalars, at: 0)",
    "Binding(get: { ability.isOn }, set: { model.setAbility(ability, on: $0) })",
    'let root = URL(fileURLWithPath: "/")',
    'let path = "/usr/bin:/bin"',
    "#if canImport(Darwin)\nimport Darwin\n#endif",
    "private let lock = NSLock()",
    "fileprivate var count = 0",
    'let title = "\\(appName) is in your menu bar"',
    'let raw = #"a \\ backslash and "quotes""#',
    'let words = "Lucille gets the names you gave your folders."',
    # Writing to the pasteboard, for Copy Diagnostics, is allowed.
    "NSPasteboard.general.clearContents()",
    "NSPasteboard.general.setString(text, forType: .string)",
    "NSPasteboard.general.writeObjects([text as NSString])",
    # A phrase in a comment is not a string the app shows.
    "// Never say: Your data never leaves your device.",
    '/* Never say "private" */ let x = 1',
    # An agent is not a daemon, so the lint allows it; the app's one login item is the app
    # itself, mainApp (decisions/0002 §9), and tests/test_macos_build.py refuses .agent( there.
    'let service = SMAppService.agent(plistName: "x.plist")',
    "let service = SMAppService.mainApp",
    "let name = NSApplication.ActivationPolicy.accessory",
    "let package = packageName",
    # Opening a file the user chose is not a URL handler; a comment is blanked, not removed.
    "func open(_ url: URL) {}",
    "model.open(folder)",
    "let urls = panel.urls",
    "let total = count /* connect later */ + 1",
]


@pytest.mark.parametrize("line", CLEAN_SWIFT)
def test_what_the_app_does_is_not_reported(line: str) -> None:
    assert swift_rules(line) == set(), line


def test_the_process_file_as_required_is_clean() -> None:
    assert swift_rules(HOST_CLI, PROCESS) == set()
    # Whitespace inside the array literal and a trailing comma are the same literal.
    spread = HOST_CLI.replace(
        '["-I", "-B", "-X", "utf8", "-m", "lucille_host"]',
        '[\n            "-I", "-B",\n            "-X", "utf8",\n'
        '            "-m", "lucille_host",\n        ]',
    )
    assert swift_rules(spread, PROCESS) == set()


def test_the_tests_strings_are_not_held_to_the_claims() -> None:
    # The self-test checks what the app says, and may spell a phrase to check that it is absent.
    assert swift_rules('let banned = "private"', TEST) == set()
    assert swift_rules('let banned = "private"', APP) == {"must-not-claim"}
    assert swift_rules('let banned = "private"', "macos/Tests/CapsuleKitTests/Words.swift") == set()


# The stand-in the self-test writes and starts through HostCLI in place of the interpreter: a
# script whose first line is the one shell a test may name (decisions/0002 §20).
STAND_IN = (
    "func checkChild(home: String) {\n"
    '    let text = """\n'
    "        #!/bin/sh\n"
    "        shift 8\n"
    '        /bin/cat "$2"\n'
    '        exit "$3"\n'
    "\n"
    '        """\n'
    '    write(text, to: home + "/fake-host")\n'
    "}\n"
)


def test_a_test_may_write_a_stand_in_script_for_the_interpreter() -> None:
    assert macos_lint.TEST_SHEBANG == "#!/bin/sh"
    assert swift_rules(STAND_IN, TEST) == set()
    assert swift_rules(STAND_IN, "macos/Tests/CapsuleKitTests/Child.swift") == set()
    scan = macos_lint.swift_scan(STAND_IN)
    spans, lines = macos_lint.allowed_test_shebangs(STAND_IN, scan)
    assert [STAND_IN[start:end] for start, end in spans] == ["#!/bin/sh"]
    assert lines == {2}


@pytest.mark.parametrize(
    ("rel", "text", "line"),
    [
        # The app never names a shell, there or anywhere.
        (APP, STAND_IN, 3),
        (PROCESS, HOST_CLI + STAND_IN, 3 + HOST_CLI.count("\n")),
        # Another shell, or the shebang with arguments, in a test.
        (TEST, STAND_IN.replace("#!/bin/sh", "#!/bin/bash"), 3),
        (TEST, STAND_IN.replace("#!/bin/sh", "#!/bin/zsh"), 3),
        (TEST, STAND_IN.replace("#!/bin/sh", "#!/bin/sh -e"), 3),
        # The shebang anywhere but alone on the literal's first line.
        (TEST, STAND_IN.replace("#!/bin/sh\n        shift 8", "shift 8\n        #!/bin/sh"), 4),
        (TEST, STAND_IN.replace("        #!/bin/sh\n", "        # #!/bin/sh\n"), 3),
        (TEST, 'let text = "#!/bin/sh\\nexit 0\\n"\n', 1),
        (TEST, '// starts #!/bin/sh\nlet text = """\n    exit 0\n    """\n', 1),
        # The shell named again inside the script, or beside it.
        (TEST, STAND_IN.replace("shift 8", "exec /bin/sh -c x"), 4),
        (TEST, STAND_IN.replace("shift 8", "/bin/sh"), 4),
        (TEST, STAND_IN + 'let shell = "/bin/sh"\n', STAND_IN.count("\n") + 1),
    ],
)
def test_a_shell_outside_the_stand_in_shebang_is_reported(rel: str, text: str, line: int) -> None:
    found = macos_lint.check_swift(text, rel, phrases())
    shells = [v for v in found if v.rule == "swift-denylist" and "names a shell" in v.message]
    assert shells, text
    assert line in {v.line for v in shells}, (line, shells)


# The cases above spell the shell plainly, so the rules over the whole text report it whatever the
# allowance decides. Here the only text that names it is a literal spelled with an escape (\u{68}
# is "h"), or the one line the allowance would blank, so only the allowance's own checks keep
# these reported.
@pytest.mark.parametrize(
    ("text", "line"),
    [
        # A one-line literal is no stand-in, however it spells the shebang.
        ('let text = "#!/bin/s\\u{68}\\nexit 0\\n"\n', 1),
        # The shebang alone on its line, after a literal that line does not open with """.
        ('let text = "#!/bin/s\\u{68}\\nx" /*\n#!/bin/sh\n*/\n', 2),
        # The shebang on the literal's first line, with more script after it through an escape.
        ('let text = """\n    #!/bin/sh\\nexit 0\n    """\n', 2),
        # The shebang with no script after it.
        ('let text = """\n    #!/bin/sh\n    """\n', 2),
        # A one-line literal on the line a stand-in opens on: the allowance is the stand-in's
        # alone, so the line holds no other literal.
        ('let a = "#!/bin/s\\u{68}\\nx"; let b = """\n#!/bin/sh\nexit 0\n"""\n', 1),
        # The quotes that end the line are in a comment, so they open no stand-in.
        ('let a = "#!/bin/s\\u{68}\\nx" /* """\n#!/bin/sh\n*/\n', 1),
    ],
)
def test_a_shebang_the_allowance_does_not_cover_is_reported(text: str, line: int) -> None:
    found = macos_lint.check_swift(text, TEST, phrases())
    assert line in {v.line for v in found if "names a shell" in v.message}, found


def test_a_shell_escaped_inside_the_stand_in_is_reported() -> None:
    # Only the decoded script names the shell again; the allowance blanks its shebang alone.
    text = STAND_IN.replace("shift 8", "exec /bin/s\\u{68} -c x")
    found = macos_lint.check_swift(text, TEST, phrases())
    assert any("names a shell" in v.message for v in found), found


def test_every_stand_in_in_a_test_file_is_allowed() -> None:
    text = STAND_IN + STAND_IN.replace("checkChild", "checkOther")
    assert swift_rules(text, TEST) == set()
    spans, lines = macos_lint.allowed_test_shebangs(text, macos_lint.swift_scan(text))
    assert [text[start:end] for start, end in spans] == ["#!/bin/sh", "#!/bin/sh"]
    assert lines == {2, 12}


def test_a_stand_in_may_have_spaces_after_its_opening_quotes() -> None:
    # The lexer reads spaces and a tab after the opening """ as the same literal.
    text = STAND_IN.replace('let text = """\n', 'let text = """ \t\n')
    assert macos_lint.swift_scan(text).literals == macos_lint.swift_scan(STAND_IN).literals
    assert swift_rules(text, TEST) == set()


def test_a_stand_in_the_lexer_cannot_read_is_reported() -> None:
    # With no literals read there is no allowance: the shebang is a shell like any other.
    found = macos_lint.check_swift(STAND_IN + 'let s = "open\n', TEST, phrases())
    assert "parse" in {v.rule for v in found}
    assert 3 in {v.line for v in found if "names a shell" in v.message}, found


def test_swift_the_lexer_cannot_read_is_still_checked_by_the_rules() -> None:
    found = macos_lint.check_swift('let s = URLSession.shared\nlet t = "open\n', APP, phrases())
    assert {(1, "swift-denylist"), (2, "parse")} <= {(v.line, v.rule) for v in found}


def test_the_build_and_its_docstrings_are_clean() -> None:
    assert python_rules(BUILD_APP, BUILD) == set()
    # Comments are not string constants.
    assert python_rules("# launchctl, .pkg and CFBundleURLTypes are refused\nX = 1\n") == set()
    assert python_rules('"""No .pkg, no launchctl."""\n\n\nclass A:\n    """No .pkg."""\n') == set()
    # The plist functions' docstrings may say what the plist never says.
    assert "must-not-claim" not in python_rules(BUILD_APP, BUILD)


def test_the_good_workflow_is_clean() -> None:
    assert workflow_rules(GOOD_WORKFLOW) == set()


# --- The Swift lexer -------------------------------------------------------------------------

P = "\ufffc"

LEXED = [
    ('let s = "x"', [(1, "x")]),
    ('/* a /* "not" */ "still not" */ let s = "x"', [(1, "x")]),
    ('// "not a string"\nlet s = "y"', [(2, "y")]),
    ('let s = "a // b /* c"', [(1, "a // b /* c")]),
    ('let s = #"a "b" \\n"#', [(1, 'a "b" \\n')]),
    ('let s = ##"a "# b"##', [(1, 'a "# b')]),
    ('let s = """\n    one\n      two\n    """', [(1, "one\n  two")]),
    ('let s = """\n    one \\\n    two\n\n    """', [(1, "one two\n")]),
    ('let s = #"""\n  a "" b\n  """#', [(1, 'a "" b')]),
    ('let s = "a \\(f("b)")) c"', [(1, "b)"), (1, f"a {P} c")]),
    ('let s = "a \\(f(x, (y))) c"', [(1, f"a {P} c")]),
    ('let s = #"a \\#(x) b \\(y)"#', [(1, f"a {P} b \\(y)")]),
    ('let s = "\\t\\n\\"\\\\\\0\\\'\\u{41}\\u{1F600}"', [(1, "\t\n\"\\\0'A\U0001f600")]),
    ('let a = "1"\nlet b = """\n  2\n  """\nlet c = "3"', [(1, "1"), (2, "2"), (5, "3")]),
    ('let s = """\n  a \\(f("b")) c\n  """', [(2, "b"), (1, f"a {P} c")]),
    ("#if os(macOS)\nlet x = 1\n#endif", []),
]


@pytest.mark.parametrize(("text", "literals"), LEXED)
def test_the_lexer_reads_each_form(text: str, literals: list[tuple[int, str]]) -> None:
    assert sorted(macos_lint.swift_scan(text).literals) == sorted(literals)


def test_the_lexer_blanks_comments_and_keeps_lines() -> None:
    text = 'let a = 1 // Process()\n/* URLSession\n */ let b = "// kept"\n'
    code = macos_lint.swift_scan(text).code
    assert len(code) == len(text) and code.count("\n") == text.count("\n")
    assert "Process" not in code and "URLSession" not in code
    assert 'let b = "// kept"' in code


UNREADABLE_SWIFT = [
    'let s = "open',
    'let s = "one\nline"',
    "/* never closed",
    "/* nested /* once */ only",
    'let s = """\n  never closed\n',
    'let s = """text on the opening line\n  """',
    'let s = """\n  text\n  more """',
    'let s = """\n    in\n  under-indented\n    """',
    'let s = "\\(open"',
    'let s = "\\(f("x")"',
    'let s = "\\q"',
    'let s = "\\u{110000}"',
    'let s = "\\u{D800}"',
    'let s = "\\u{}"',
    "let r = #/a+b/#",
]


@pytest.mark.parametrize("text", UNREADABLE_SWIFT)
def test_swift_the_lexer_cannot_read_is_reported(text: str) -> None:
    with pytest.raises(macos_lint.SwiftSyntaxError):
        macos_lint.swift_scan(text)
    found = macos_lint.check_swift(text, APP, phrases())
    assert "parse" in {v.rule for v in found}


# --- The phrases, from PRIVACY.md ------------------------------------------------------------


def test_phrases_come_from_privacy_md() -> None:
    found = phrases()
    section = PRIVACY_TEXT.split("## What this project must never claim", 1)[1].split("\n## ")[0]
    assert found == [" ".join(p.split()) for p in re.findall(r'"([^"]+)"', section)]
    assert len(found) >= macos_lint.MIN_PHRASES == 15
    assert {
        "private",
        "Your data never leaves your device",
        "Lucille cannot see your files",
    } <= set(found)
    # A phrase PRIVACY.md wraps across lines is one phrase.
    assert "A hacked Lucille cannot read your files" in found


@pytest.mark.parametrize(
    "text",
    [
        "# Privacy\n\nNothing here.\n",
        '## What this project must never claim\n\n- "One".\n- "Two".\n\n## Next\n\n"Three"\n',
    ],
)
def test_a_privacy_file_without_the_list_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        macos_lint.must_not_claim_phrases(text)


def test_claims_match_whole_words_in_any_case() -> None:
    words = phrases()
    assert macos_lint.claims("It is SANDBOXED.", words) == ["Sandboxed"]
    assert macos_lint.claims("It is\n   end-to-end   encrypted", words) == ["End-to-end encrypted"]
    assert macos_lint.claims("privately", words) == []
    assert macos_lint.claims("non-private", words) == []
    assert macos_lint.claims("unverified", words) == []
    assert macos_lint.claims("nothing", words) == []


def test_an_allowed_sentence_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    sentence = "That is not a review, and nothing here is audited."
    assert swift_rules(f'let note = "{sentence}"') == {"must-not-claim"}
    monkeypatch.setattr(macos_lint, "ALLOWED_SENTENCES", frozenset({sentence}))
    assert swift_rules(f'let note = "{sentence}"') == set()
    # The whole literal, not a part of it.
    assert swift_rules(f'let note = "{sentence} Really."') == {"must-not-claim"}


def test_the_allowed_sentences_start_empty() -> None:
    assert frozenset() == macos_lint.ALLOWED_SENTENCES


# --- The whole tree ----------------------------------------------------------------------------


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _clean_tree(root: Path) -> Path:
    return _tree(
        root,
        {
            "PRIVACY.md": PRIVACY_TEXT,
            PROCESS: HOST_CLI,
            BUILD: BUILD_APP,
            PINS: PINS_TEXT,
            ".github/workflows/ci.yml": GOOD_WORKFLOW,
        },
    )


def test_a_clean_tree_is_clean(tmp_path: Path) -> None:
    root = _clean_tree(tmp_path)
    assert macos_lint.check(root) == []
    assert macos_lint.lint_files(root) == sorted([PROCESS, BUILD, PINS, ".github/workflows/ci.yml"])


def test_a_tree_without_the_process_file_is_reported(tmp_path: Path) -> None:
    root = _clean_tree(tmp_path)
    (root / PROCESS).rename(root / APP)
    found = macos_lint.check(root)
    assert [(v.path, v.rule) for v in found] == [(APP, "process"), (PROCESS, "process")]


def test_a_tree_without_its_privacy_list_is_reported(tmp_path: Path) -> None:
    root = _clean_tree(tmp_path)
    (root / "PRIVACY.md").unlink()
    found = macos_lint.check(root)
    assert [(v.path, v.rule) for v in found] == [("PRIVACY.md", "parse")]


def test_an_installer_package_in_the_tree_is_reported(tmp_path: Path) -> None:
    root = _tree(
        _clean_tree(tmp_path),
        {"out/Capsule.pkg": "", "out/Old.mpkg/Contents/Info.plist": "<plist/>\n"},
    )
    found = {(v.path, v.rule) for v in macos_lint.check(root)}
    assert found == {("out/Capsule.pkg", "pkg"), ("out/Old.mpkg/Contents/Info.plist", "pkg")}


def test_each_kind_of_file_is_read(tmp_path: Path) -> None:
    root = _tree(
        _clean_tree(tmp_path),
        {
            TEST: "let p = Process()\n",
            TOOL: 'ARGV = ["launchctl"]\n',
            STRINGS: '"a" = "Sandboxed";\n',
            "macos/App/Capsule.entitlements": "<key>CFBundleURLTypes</key>\n",
            ".github/workflows/release.yaml": GOOD_WORKFLOW + "      - run: pkgbuild x\n",
        },
    )
    found = {(v.path, v.rule) for v in macos_lint.check(root)}
    assert found == {
        (TEST, "process"),
        (TOOL, "daemon"),
        (STRINGS, "must-not-claim"),
        ("macos/App/Capsule.entitlements", "url-scheme"),
        (".github/workflows/release.yaml", "pkg"),
    }


def test_an_allowed_sentence_no_literal_uses_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _clean_tree(tmp_path)
    monkeypatch.setattr(macos_lint, "ALLOWED_SENTENCES", frozenset({"Not said anywhere."}))
    found = macos_lint.check(root)
    assert [(v.path, v.rule) for v in found] == [("tools/guards/macos_lint.py", "must-not-claim")]


def test_the_process_file_is_one_path() -> None:
    # The change that adds the Swift package moves this constant to the Kit file; only one path
    # is ever allowed.
    assert macos_lint.PROCESS_FILE == "macos/App/HostCLI.swift"
    assert macos_lint.RUNTIME_IN_APP == "Contents/Resources/runtime/bin/lucille-host-runtime"
    assert macos_lint.RUNTIME_ARGV == ("-I", "-B", "-X", "utf8", "-m", "lucille_host")


# --- The one opener (decisions/0002 §20) ------------------------------------------------------

OPENER = "macos/App/SystemPlaces.swift"
# The two System Settings pages the opener may show, as tools/guards/macos_lint.py's OPENABLE
# holds them. Not yet seen on a Mac: check_app.py places opens each and says what it shows.
DATE_PANE = "x-apple.systempreferences:com.apple.Date-Time-Settings.extension"
FILES_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"

# The opener as designed: a closed list of three places, the URL of each written here, and one
# call that opens it. Its comments do not name the class they call.
SYSTEM_PLACES = textwrap.dedent(
    f"""\
    import AppKit

    /// The app's one opener: it shows the user a place in macOS, from a click.
    @MainActor
    enum SystemPlaces {{
        /// Show `place`. False when macOS could not open it.
        static func open(_ place: SystemPlace) -> Bool {{
            let url: URL?
            switch place {{
            case .dateAndTime:
                url = URL(string: "{DATE_PANE}")
            case .filesAndFolders:
                url = URL(string: "{FILES_PANE}")
            case .applicationsFolder:
                url = URL(fileURLWithPath: "/Applications", isDirectory: true)
            }}
            guard let url else {{
                return false
            }}
            return NSWorkspace.shared.open(url)
        }}
    }}
    """
)
OPEN_CALL = "        return NSWorkspace.shared.open(url)\n"
DATE_URL = f'URL(string: "{DATE_PANE}")'
FOLDER_URL = 'URL(fileURLWithPath: "/Applications", isDirectory: true)'


def _opener(old: str, new: str) -> str:
    assert SYSTEM_PLACES.count(old) == 1, old
    return SYSTEM_PLACES.replace(old, new)


def test_the_opener_as_designed_is_clean() -> None:
    assert swift_rules(SYSTEM_PLACES, OPENER) == set()
    # The literals are read decoded: a raw string that is an entry of OPENABLE is that entry.
    raw = _opener(f'"{DATE_PANE}"', f'#"{DATE_PANE}"#')
    assert swift_rules(raw, OPENER) == set()
    # The same file anywhere else is the denylist's, as before.
    assert "swift-denylist" in swift_rules(SYSTEM_PLACES, APP)


# Each way the opener may not be widened, at OPEN_FILE, with the finding that says so: each case is
# here for one of the [open] rule's checks, and its finding is that check's own words, so that
# taking that one check away fails this test, whatever else the case also breaks.
SECOND_CALL = "NSWorkspace a second time"
NOT_THE_CALL = "NSWorkspace other than as NSWorkspace.shared.open(<a name>)"
NO_CALL = "NSWorkspace.shared.open(<a name>), is not here"
CONFIGURATION = "opens with a configuration"
ANOTHER_APP = "opens with another application"
FILE_PATH = "makes a file URL that is not the Applications folder"
RELATIVE = "makes a URL relative to another"
ADDED = "adds to a URL"
TAKEN_AWAY = "takes part of a URL away"
NOT_ONE_LITERAL = "a URL made from something other than one string literal"
NOT_OPENABLE = "is not a place the opener may open (OPENABLE)"
EACH_ONCE = "must be opened by exactly one URL here"
FOLDER_ONCE = 'URL(fileURLWithPath: "/Applications", isDirectory: true) once, alone'
OTHER_URL = "a URL the opener may not make"
OTHER_STRING = "is a string the opener has no use for"
NOT_DESIGNED = "where the opener's design (OPEN_DESIGN) has"
ENDS_EARLY = "the opener ends where its design (OPEN_DESIGN) goes on"
OUT_OF_ORDER = "the opener's strings, in order, are not OPEN_LITERALS"
FULL_DISK = "names Full Disk Access"
GUARD = "        guard let url else {\n"
FILES_URL = f'URL(string: "{FILES_PANE}")'
MAILTO = "mailto:a@example.com"

PLANTED_OPENER: list[tuple[str, str, str]] = [
    # A second call, or a comment that names the class: counted in the whole text.
    (
        "a second open",
        SYSTEM_PLACES + "func again(_ url: URL) -> Bool {\n    NSWorkspace.shared.open(url)\n}\n",
        SECOND_CALL,
    ),
    (
        "a comment naming it",
        _opener("/// Show `place`.", "/// Show `place`, through NSWorkspace."),
        SECOND_CALL,
    ),
    # No call at all: an opener that opens nothing is no opener.
    ("no call", _opener(OPEN_CALL, "        return true\n"), NO_CALL),
    # The class other than in its one call: another of its members, or kept under another name.
    (
        "openApplication",
        _opener(
            OPEN_CALL,
            "        NSWorkspace.shared.openApplication(at: url, configuration: c)\n"
            "        return true\n",
        ),
        NOT_THE_CALL,
    ),
    (
        "activateFileViewerSelecting",
        _opener(
            OPEN_CALL,
            "        NSWorkspace.shared.activateFileViewerSelecting([url])\n        return true\n",
        ),
        NOT_THE_CALL,
    ),
    (
        "requestAuthorization",
        _opener(
            OPEN_CALL,
            "        NSWorkspace.shared.requestAuthorization(to: .replaceFile) { _, _ in }\n"
            "        return true\n",
        ),
        NOT_THE_CALL,
    ),
    (
        "let ws = NSWorkspace.shared",
        _opener(OPEN_CALL, "        let ws = NSWorkspace.shared\n        return ws.open(url)\n"),
        NOT_THE_CALL,
    ),
    # What opens an application, asked of the call or of the URL.
    (
        "configuration:",
        _opener(
            OPEN_CALL,
            "        NSWorkspace.shared.open(url, configuration: c, completionHandler: nil)\n"
            "        return true\n",
        ),
        CONFIGURATION,
    ),
    (
        "configuration: on the next line",
        _opener("open(url)", "open(\n            url, configuration: c, completionHandler: nil)"),
        CONFIGURATION,
    ),
    (
        "withApplicationAt",
        _opener("open(url)", "open([url], withApplicationAt: a, configuration: c)"),
        ANOTHER_APP,
    ),
    ("URL(filePath:", _opener(FOLDER_URL, 'URL(filePath: "/Applications")'), FILE_PATH),
    (
        "relativeTo:",
        _opener(FOLDER_URL, FOLDER_URL.replace("true)", "true, relativeTo: nil)")),
        RELATIVE,
    ),
    ("a path added", _opener(FOLDER_URL, FOLDER_URL + ".appendingPathComponent(name)"), ADDED),
    (
        "a path taken away",
        _opener(FOLDER_URL, FOLDER_URL + ".deletingLastPathComponent()"),
        TAKEN_AWAY,
    ),
    # A URL from a string: one literal, and nothing else, or text, or joined.
    ("URL(string: text)", _opener(DATE_URL, "URL(string: text)"), NOT_ONE_LITERAL),
    ("URL.init(string: text)", _opener(DATE_URL, "URL.init(string: text)"), NOT_ONE_LITERAL),
    (
        "a URL joined",
        _opener(DATE_URL, 'URL(string: "x-apple.systempreferences:" + pane)'),
        NOT_ONE_LITERAL,
    ),
    (
        "a literal and more",
        _opener(DATE_URL, DATE_URL.replace('")', '", encodingInvalidCharacters: false)')),
        NOT_ONE_LITERAL,
    ),
    # A URL from a literal that is not an entry of OPENABLE: a System Settings page not listed, a
    # mailto URL, the folder's path as a string, or a mailto URL in a name that shadows the
    # designed one, each OPENABLE entry still used once.
    (
        "an x-apple string not in OPENABLE",
        _opener(DATE_PANE, "x-apple.systempreferences:com.apple.Network-Settings.extension"),
        NOT_OPENABLE,
    ),
    ("a mailto URL", _opener(DATE_URL, f'URL(string: "{MAILTO}")'), NOT_OPENABLE),
    ("the folder as a string", _opener(DATE_URL, 'URL(string: "/Applications")'), NOT_OPENABLE),
    (
        "a mailto URL shadowing the name",
        _opener(GUARD, f'        guard let url = URL(string: "{MAILTO}") else {{\n'),
        NOT_OPENABLE,
    ),
    # Each OPENABLE entry once: one used twice, and so the other used by no literal; or one unused.
    ("an entry twice", _opener(FILES_URL, DATE_URL), EACH_ONCE),
    (
        "an entry unused",
        _opener(f"            url = {DATE_URL}\n", "            url = nil\n"),
        EACH_ONCE,
    ),
    # The Applications folder, once, alone: another file, one from text, the folder twice, a second
    # file URL beside it, or something taken from it.
    (
        "another file",
        _opener(FOLDER_URL, 'URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")'),
        FOLDER_ONCE,
    ),
    (
        "a file from text",
        _opener(FOLDER_URL, "URL(fileURLWithPath: path, isDirectory: true)"),
        FOLDER_ONCE,
    ),
    ("the folder twice", _opener(DATE_URL, FOLDER_URL), FOLDER_ONCE),
    (
        "a second file URL",
        _opener(GUARD, "        _ = URL(fileURLWithPath: path)\n" + GUARD),
        FOLDER_ONCE,
    ),
    (
        "something taken from the folder",
        _opener(FOLDER_URL, FOLDER_URL + ".absoluteURL"),
        FOLDER_ONCE,
    ),
    # A URL made any other way.
    ("a URL from an alias", _opener(DATE_URL, "URL(resolvingAliasFileAt: alias)"), OTHER_URL),
    # A string that is not a place's.
    ("another string", _opener(GUARD, '        let note = "Opening"\n' + GUARD), OTHER_STRING),
    ("a mailto string", _opener(GUARD, f'        let _ = "{MAILTO}"\n' + GUARD), OTHER_STRING),
    # Full Disk Access, plainly or escaped, in the opener too.
    ("Privacy_AllFiles", _opener("Privacy_FilesAndFolders", "Privacy_AllFiles"), FULL_DISK),
    (
        "Privacy_AllFiles escaped",
        _opener("Privacy_FilesAndFolders", "Privacy_\\u{41}llFiles"),
        FULL_DISK,
    ),
    # Code that is not the design's: a URL kept from a property or another object; a second
    # overload that any file could call with a URL of its own; a URL taken from a member that
    # another file's extension adds; more after the design, or less.
    (
        "a URL from a property",
        _opener(
            OPEN_CALL,
            "        let home = URL.homeDirectory\n        return NSWorkspace.shared.open(home)\n",
        ),
        NOT_DESIGNED,
    ),
    (
        "a URL from another object",
        _opener(
            OPEN_CALL,
            "        let app = Bundle.main.bundleURL\n"
            "        return NSWorkspace.shared.open(app)\n",
        ),
        NOT_DESIGNED,
    ),
    (
        "a second overload",
        _opener(
            OPEN_CALL + "    }\n",
            "        return open(url)\n    }\n\n    static func open(_ url: URL) -> Bool {\n"
            + OPEN_CALL
            + "    }\n",
        ),
        NOT_DESIGNED,
    ),
    (
        "SystemPlaces.url ??",
        _opener(GUARD, "        guard let url = SystemPlaces.url ?? url else {\n"),
        NOT_DESIGNED,
    ),
    (
        "place.url ??",
        _opener(GUARD, "        guard let url = place.url ?? url else {\n"),
        NOT_DESIGNED,
    ),
    (
        ".applicationsFolder ??",
        _opener(FOLDER_URL, ".applicationsFolder ?? " + FOLDER_URL),
        NOT_DESIGNED,
    ),
    ("more after the design", SYSTEM_PLACES + "extension SystemPlaces {}\n", NOT_DESIGNED),
    # A backtick, and a # the lexer reads as code next to a literal, are tokens of their own: the
    # design reads a literal's hashes only when they close it.
    ("a backtick", _opener("let url: URL?", "let url: `URL`?"), NOT_DESIGNED),
    ("a # after a literal", _opener(DATE_URL, DATE_URL.replace('")', '"#)')), NOT_DESIGNED),
    ("less than the design", SYSTEM_PLACES.removesuffix("}\n"), ENDS_EARLY),
    # Each place its own address: the two pages swapped, which nothing else here would see.
    (
        "the pages swapped",
        SYSTEM_PLACES.replace(DATE_PANE, "\0")
        .replace(FILES_PANE, DATE_PANE)
        .replace("\0", FILES_PANE),
        OUT_OF_ORDER,
    ),
]


@pytest.mark.parametrize(
    ("what", "text", "said"), PLANTED_OPENER, ids=[what for what, _, _ in PLANTED_OPENER]
)
def test_each_way_to_widen_the_opener_is_reported(what: str, text: str, said: str) -> None:
    found = macos_lint.check_swift(text, OPENER, phrases())
    assert any(v.rule == "open" and said in v.message for v in found), (what, found)


def test_the_pages_swapped_is_found_by_their_order_alone() -> None:
    # Swapped, each page is still one OPENABLE entry used once, in the designed code: only the
    # order of the strings says that Open Date & Time Settings would open Files & Folders.
    (text,) = [text for what, text, _ in PLANTED_OPENER if what == "the pages swapped"]
    found = macos_lint.check_swift(text, OPENER, phrases())
    assert [v.message.split(":")[0] for v in found] == [OUT_OF_ORDER], found


def test_the_design_is_the_opener_as_built() -> None:
    # OPEN_DESIGN is read as the opener is: the real file and the test's copy have its tokens, and
    # its strings are the places, in the order of SystemPlace's cases.
    designed = [token for _, token in macos_lint.design_tokens(macos_lint.OPEN_DESIGN)]
    for text in (SYSTEM_PLACES, (REPO_ROOT / OPENER).read_text(encoding="utf-8")):
        code = macos_lint.swift_scan(text).code
        assert [token for _, token in macos_lint.design_tokens(code)] == designed
    assert macos_lint.OPEN_LITERALS == (DATE_PANE, FILES_PANE, "/Applications")
    assert set(macos_lint.OPEN_LITERALS[:2]) == macos_lint.OPENABLE
    # White space and a raw string are not the design's to hold: the same tokens are the same code.
    flush = "\n".join(line.strip() for line in SYSTEM_PLACES.splitlines())
    assert swift_rules(flush, OPENER) == set()


def test_an_opener_finding_names_its_line() -> None:
    text = PLANTED_OPENER[0][1]
    second = text.splitlines().index("    NSWorkspace.shared.open(url)") + 1
    found = macos_lint.check_swift(text, OPENER, phrases())
    assert second in {v.line for v in found if v.rule == "open"}, found


# What the opener may do is the opener's alone: everywhere else, every line it is allowed is still
# refused, a System Settings page or Full Disk Access is named nowhere, and each way to open, end
# or register another application that the denylist did not see is refused too.
PLANTED_OUTSIDE_OPENER: list[tuple[str, str, str]] = [
    ("swift-denylist", APP, SYSTEM_PLACES),
    ("swift-denylist", "macos/App/Opener.swift", SYSTEM_PLACES),
    ("swift-denylist", APP, "_ = NSWorkspace.shared.open(url)"),
    ("swift-denylist", APP, f"let url = {DATE_URL}"),
    ("swift-denylist", TEST, f"let url = {DATE_URL}"),
    ("open", APP, f'let pane = "{DATE_PANE}"'),
    ("open", APP, f'let pane = "{FILES_PANE}"'),
    ("open", TEST, f'let pane = "{DATE_PANE}"'),
    ("open", PROCESS, HOST_CLI + f'let pane = "{DATE_PANE}"\n'),
    ("open", APP, 'let pane = "x-apple.systempreferences\\u{3A}com.apple.preference.security"'),
    ("open", APP, 'let pane = "X-Apple.SystemPreferences:com.apple.preference.security"'),
    ("open", APP, "// x-apple.systempreferences:com.apple.preference.security"),
    ("open", APP, 'let pane = "Privacy_AllFiles"'),
    ("open", APP, 'let pane = "Privacy_\\u{41}llFiles"'),
    ("open", TEST, 'let pane = "Privacy_AllFiles"'),
    # The probe holes, everywhere, the opener included.
    ("swift-denylist", APP, "let url: URL? = .init(string: text)"),
    ("swift-denylist", APP, "let url = URL(dataRepresentation: data, relativeTo: nil)"),
    ("swift-denylist", APP, "let files = FileManager(authorization: authorization)"),
    ("swift-denylist", APP, "_ = app.forceTerminate()"),
    (
        "swift-denylist",
        APP,
        "let apps = NSRunningApplication.runningApplications(withBundleIdentifier: id)",
    ),
    ("swift-denylist", APP, "LSSetDefaultHandlerForURLScheme(scheme as CFString, id as CFString)"),
    ("swift-denylist", APP, "LSSetDefaultRoleHandlerForContentType(type, .all, id)"),
    ("swift-denylist", APP, "LSRegisterURL(url as CFURL, true)"),
    ("swift-denylist", APP, "AESendMessage(&event, &reply, mode, timeout)"),
    ("swift-denylist", APP, "center.requestAuthorization(options: [.alert]) { _, _ in }"),
    # ... with white space where the pattern allows it.
    ("swift-denylist", APP, "let url: URL? = . init ( string : text)"),
    ("swift-denylist", APP, "let url = URL ( dataRepresentation : data, relativeTo: nil)"),
    ("swift-denylist", APP, "let apps = runningApplications ( withBundleIdentifier : id)"),
    ("swift-denylist", APP, "let files = FileManager ( authorization : authorization)"),
    # An initializer with its type left out after a keyword, where a symbol does not come first.
    ("swift-denylist", APP, "func make(_ text: String) -> URL? { return .init(string: text) }"),
    ("swift-denylist", APP, "let urls: [URL?] = texts.map { text in .init(string: text) }"),
    ("swift-denylist", APP, "let url: URL? = try .init(string: text)"),
    ("swift-denylist", APP, "let url: URL? = await .init(string: text)"),
    ("swift-denylist", APP, "yield .init(string: text)"),
    # Other ways to open a URL: SwiftUI's action, a link, a link attribute, Markdown, read or in a
    # literal that SwiftUI's Text shows, plainly or raw.
    ("swift-denylist", APP, "@Environment(\\.openURL) private var openURL"),
    ("swift-denylist", APP, "let action = OpenURLAction { _ in .handled }"),
    ("swift-denylist", APP, 'Link("Terminal", destination: url)'),
    ("swift-denylist", APP, 'Link\t( "Terminal", destination: url)'),
    ("swift-denylist", APP, "let words = [NSAttributedString.Key.link: url]"),
    ("swift-denylist", APP, "let text = try AttributedString(markdown: said)"),
    ("swift-denylist", APP, 'Text("[Open](file:///System/Applications/Utilities/Terminal.app)")'),
    ("swift-denylist", APP, 'let note = #"[Open](file:///Applications)"#'),
    # Other ways to find, drive or hand something to another application.
    ("swift-denylist", APP, "NSRunningApplication(processIdentifier: pid)?.terminate()"),
    ("swift-denylist", APP, "let app = NSRunningApplication ( processIdentifier : pid)"),
    ("swift-denylist", APP, "let event = NSAppleEventDescriptor.null()"),
    ("swift-denylist", APP, "NSSharingService(named: .composeEmail)?.perform(withItems: [text])"),
    ("swift-denylist", APP, "let picker = NSSharingServicePicker(items: [url])"),
    ("swift-denylist", APP, "ShareLink(item: url)"),
    # A bundle's code, loaded.
    ("swift-denylist", APP, "_ = Bundle(url: url)?.load()"),
    ("swift-denylist", APP, "_ = Bundle(url: url)?.load ( )"),
    ("swift-denylist", APP, "try Bundle(path: path)?.loadAndReturnError()"),
    ("swift-denylist", APP, "let type = Bundle(url: url)?.principalClass"),
    # Other ways to make a URL that is not a file from data or text.
    (
        "swift-denylist",
        APP,
        "let url = URL(absoluteURLWithDataRepresentation: data, relativeTo: nil)",
    ),
    (
        "swift-denylist",
        APP,
        "let url = NSURL(absoluteURLWithDataRepresentation: data, relativeTo: nil)",
    ),
    ("swift-denylist", APP, "let url = CFURLCreateWithString(nil, text as CFString, nil)"),
    ("swift-denylist", APP, "let url = CFURLCreateWithBytes(nil, bytes, count, encoding, nil)"),
    (
        "swift-denylist",
        APP,
        "let url = CFURLCreateAbsoluteURLWithBytes(nil, bytes, count, encoding, nil, true)",
    ),
    ("swift-denylist", APP, "let url = NSURL(scheme: scheme, host: host, path: path)"),
    # A button pressed by code, not by a click; and a function reached by its symbol, or put in
    # another's place, whichever name the code calls it by.
    ("swift-denylist", APP, "button.performClick(nil)"),
    (
        "swift-denylist",
        APP,
        '@_silgen_name("$s7Capsule12SystemPlacesO4openySbAA0B5PlaceOFZ")\n'
        "func placeAtLaunch(_ place: SystemPlace) -> Bool",
    ),
    ("swift-denylist", APP, '@_cdecl("place_at_launch") func placeAtLaunch() {}'),
    (
        "swift-denylist",
        APP,
        "@_dynamicReplacement(for: open(_:))\n"
        "static func again(_ place: SystemPlace) -> Bool { true }",
    ),
    ("swift-denylist", TEST, "button.performClick(nil)"),
    # The opener's type other than in a call, which a build test would not find by its text: a
    # function reference, a call split over lines or spaced, a type alias.
    ("open", APP, "let show = SystemPlaces.open\n_ = show(.dateAndTime)"),
    ("open", APP, "_ = SystemPlaces\n    .open(.filesAndFolders)"),
    ("open", APP, "_ = SystemPlaces .open(.dateAndTime)"),
    ("open", APP, "typealias S = SystemPlaces"),
    ("open", TEST, "let show = SystemPlaces.open"),
    # An extension, in another file, of a type the opener's code names, which could add the member
    # it reads or change the initializer it calls.
    ("open", APP, "extension SystemPlaces {\n    static var url: URL? { nil }\n}"),
    ("open", APP, "extension SystemPlace {\n    var url: URL? { nil }\n}"),
    ("open", APP, "extension URL {\n    init?(string text: String, _ x: Int) { return nil }\n}"),
    ("open", APP, "extension Foundation.URL {}"),
    ("open", APP, "extension NSURL {}"),
    ("open", APP, "extension Optional where Wrapped == URL {}"),
    ("open", APP, "extension Equatable {\n    var url: URL? { nil }\n}"),
    ("open", APP, "extension Sendable {}"),
    # A new declaration of a name the opener's code uses, which could stand in for it there.
    (
        "open",
        APP,
        "func URL(fileURLWithPath path: String, isDirectory: Bool) -> Int { 0 }",
    ),
    ("open", APP, "typealias URL = NSURL"),
    ("open", APP, "struct SystemPlaces {}"),
    ("open", APP, "let URL = 0"),
    ("open", APP, "var NSURL = 0"),
    # ... or a new name for one of those types, whose extension the rules above would not see: an
    # initializer added to URL under another name would be the one the opener's URL(string:) calls.
    (
        "open",
        APP,
        "typealias PlaceAddress = URL\n"
        "extension PlaceAddress {\n"
        "    init?(string: String) {\n"
        '        self.init(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")\n'
        "    }\n"
        "}",
    ),
    ("open", APP, "typealias Address = Foundation.URL"),
    ("open", APP, "typealias Maybe = Optional\nextension Maybe where Wrapped == Foundation.URL {}"),
    ("open", APP, "typealias Maybe<T> = Swift.Optional<T>"),
    ("open", APP, "typealias Place =\n    SystemPlace"),
    ("open", TEST, "typealias Address = URL"),
    # ... or a backtick, which makes a name of any word and so hides one from the rules above. The
    # app's code has none, its strings included; a comment may.
    ("open", APP, "extension `URL` {\n    init?(string: String) { return nil }\n}"),
    ("open", APP, "extension Foundation.`URL` {}"),
    ("open", APP, "extension `SystemPlace` {}"),
    ("open", APP, "extension `Optional` where Wrapped == Foundation.URL {}"),
    ("open", APP, "func `URL`(string: String) -> Foundation.URL? { nil }"),
    ("open", APP, "let url = `URL`(string: text)"),
    ("open", APP, 'let note = "Press `Open` first."'),
    ("open", TEST, "let `URL` = 0"),
]


@pytest.mark.parametrize(
    "case", PLANTED_OUTSIDE_OPENER, ids=[_planted_id(case) for case in PLANTED_OUTSIDE_OPENER]
)
def test_what_only_the_opener_may_do_is_reported_everywhere_else(
    case: tuple[str, str, str],
) -> None:
    rule, rel, text = case
    assert rule in swift_rules(text, rel), text


@pytest.mark.parametrize(
    ("line", "named"),
    [
        ("_ = app.forceTerminate()", "forceTerminate"),
        ("let url: URL? = .init(string: text)", ".init(string:"),
        ("LSRegisterURL(url as CFURL, true)", "LSRegister"),
        ("center.requestAuthorization(options: [.alert]) { _, _ in }", "requestAuthorization"),
        ("button.performClick(nil)", "performClick"),
        ('@_silgen_name("x") func f()', "_silgen_name"),
    ],
)
def test_the_probe_holes_are_refused_in_the_opener_too(line: str, named: str) -> None:
    # The opener names the class the denylist refuses elsewhere, so a denylist finding there says
    # nothing by itself: the finding must name the hole.
    found = macos_lint.check_swift(SYSTEM_PLACES + line + "\n", OPENER, phrases())
    assert any(v.rule == "swift-denylist" and named in v.message for v in found), found


CLEAN_AROUND_THE_OPENER = [
    # A type's own initializer with a string is not a URL, nor is an attributed string.
    'let field = CodeField(string: "")',
    "shown.append(NSAttributedString(string: said, attributes: words))",
    "let title = NSAttributedString.init(string: said)",
    "let title = NSAttributedString .init(string: said)",
    "let folder = URL(fileURLWithPath: folder.path, isDirectory: true)",
    # Calling the opener is not opening: tests/test_macos_build.py holds each call to a click.
    "if !SystemPlaces.open(place) { show() }",
    # A comment may name the opener; only its code is held to the call.
    "// `SystemPlaces` opens a place, from that button's click.",
    "/* SystemPlaces.open */ let x = 1",
    "case dateAndTime",
    'let title = "Open Date & Time Settings"',
    'let steps = "Open System Settings, choose Privacy & Security, then Files & Folders."',
    "let me = NSRunningApplication.current",
    # An extension of the app's own types, and names that only start like a refused one.
    "extension Ability {}",
    "extension HostObject {}",
    "let urls: [URL] = []",
    'NavigationLink("More", destination: view)',
    "controller.loadView()",
    # A bracket and a parenthesis in words, and a call through a subscript, are not links.
    'let note = "Folders [2] (Documents)"',
    "actions[0]()",
    # A type alias of the app's own, a backtick in a comment, and an edit action the window sends
    # on its own key press.
    "typealias Handler = () -> Void",
    "// `run` acts on nothing until the file can be read again.",
    "/* `URL` */ let x = 1",
    "NSApp.sendAction(action, to: nil, from: self)",
    "override func performKeyEquivalent(with event: NSEvent) -> Bool { false }",
]


@pytest.mark.parametrize("line", CLEAN_AROUND_THE_OPENER)
def test_what_the_rest_of_the_app_does_around_the_opener_is_not_reported(line: str) -> None:
    assert swift_rules(line) == set(), line


def test_a_tree_with_the_opener_or_without_it_is_clean(tmp_path: Path) -> None:
    # Opening nothing is safe: unlike the process file, the opener is not required.
    root = _clean_tree(tmp_path)
    assert macos_lint.check(root) == []
    _tree(root, {OPENER: SYSTEM_PLACES})
    assert OPENER in macos_lint.lint_files(root)
    assert macos_lint.check(root) == []


def test_the_opener_anywhere_else_is_reported(tmp_path: Path) -> None:
    moved = "macos/App/Opener.swift"
    root = _tree(_clean_tree(tmp_path), {moved: SYSTEM_PLACES})
    found = {(v.path, v.rule) for v in macos_lint.check(root)}
    assert (moved, "swift-denylist") in found


def test_openable_is_the_list_0002_records() -> None:
    # Two System Settings pages, by the spellings 0002 §20 records, and never Full Disk Access.
    assert isinstance(macos_lint.OPENABLE, frozenset)
    assert macos_lint.OPENABLE == frozenset({DATE_PANE, FILES_PANE})
    for entry in macos_lint.OPENABLE:
        assert entry.startswith("x-apple.systempreferences:"), entry
        assert "Privacy_AllFiles" not in entry, entry
    record = (REPO_ROOT / "decisions" / "0002-macos-app.md").read_text(encoding="utf-8")
    section = record.split("\n## 20. ", 1)[1].split("\n## 21. ", 1)[0]
    for entry in sorted(macos_lint.OPENABLE):
        assert f"`{entry}`" in section, entry
    assert f"`{macos_lint.OPEN_FILE}`" in section
    assert "`NSWorkspace`" in section and "`Privacy_AllFiles`" in section
    # It no longer says the exception is still to come.
    assert "is not allowed yet either" not in " ".join(section.split())


def test_the_open_file_is_one_path() -> None:
    # Only one path is ever allowed, as for the process file; the opener at any other is the
    # denylist's.
    assert macos_lint.OPEN_FILE == "macos/App/SystemPlaces.swift"
    assert macos_lint.OPEN_FILE != macos_lint.PROCESS_FILE
    assert swift_rules(SYSTEM_PLACES, macos_lint.OPEN_FILE) == set()
    for elsewhere in (
        "macos/App/systemplaces.swift",
        "macos/Sources/CapsuleKit/SystemPlaces.swift",
        "macos/Tests/SystemPlaces.swift",
        "SystemPlaces.swift",
    ):
        assert "swift-denylist" in swift_rules(SYSTEM_PLACES, elsewhere), elsewhere


def test_the_docstring_says_the_open_rule() -> None:
    doc = " ".join((macos_lint.__doc__ or "").split())
    assert "[open]" in doc
    assert ":data:`OPEN_FILE`" in doc and ":data:`OPENABLE`" in doc
    assert "with no exception" not in doc
    assert "``SMAppService.agent``, the login item's fallback, is allowed" not in doc
    # It names what [open] refuses outside the opener, a new name and a backtick among them, and
    # what it cannot see, an action sent by code.
    for words in ("``typealias``", "no backtick", "``performClick``", "``_silgen_name``"):
        assert words in doc, words
    assert "an action sent by code" in doc


def test_repository_is_clean() -> None:
    files = macos_lint.lint_files(REPO_ROOT)
    assert {PROCESS, BUILD, PINS, ".github/workflows/ci.yml"} <= set(files)
    assert "macos/Tests/SelfTest.swift" in files
    assert macos_lint.check(REPO_ROOT) == []


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_script_exit_codes(tmp_path: Path) -> None:
    assert _run_script(str(tmp_path)).returncode == 2  # nothing to check
    root = _clean_tree(tmp_path)
    result = _run_script(str(root))
    assert result.returncode == 0, result.stdout + result.stderr
    _tree(root, {APP: "import Foundation\nlet task = URLSession.shared\n"})
    result = _run_script(str(root))
    assert result.returncode == 1
    assert f"{APP}:2: [swift-denylist]" in result.stdout
