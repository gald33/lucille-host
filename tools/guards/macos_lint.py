"""Mac lint: what the Mac app's sources may say and do (decisions/0002 §20).

The Mac app is a menu bar app that starts one program, the host, from the Python runtime inside
its own bundle, and does nothing else on its own: it opens no network connection and no listener,
reads nothing from the pasteboard, handles no URL scheme, installs no daemon and ships in no
installer package. These rules keep that visible in the source. The rule name of each finding is
in brackets.

In every Swift file of the repository (``*.swift``, the self-test included), over the whole text,
comments included; over the code with its comments blanked, so that a comment inside a call
(``connect/**/(``) does not hide it; and over each string literal decoded, so that an escape
(``https:\\u{2F}/``) does not hide a URL or a name:

- [swift-denylist] none of the names in :data:`DENIED_SWIFT`: the list of 0002 §20 as Swift
  spells it (``URLSession``, ``NW*``, ``import ... Network``, ``WebKit`` and ``WK*``,
  ``NSXPCConnection``, ``NSXPCListener``, ``xpc_*``, ``bootstrap_*``, ``CFSocket``, ``NSStream``,
  ``InputStream`` and ``OutputStream``, ``getStreamsToHost``, ``DistributedNotificationCenter``,
  ``NSAppleScript``, ``posix_spawn``, ``system(``, ``/bin/sh``, ``/bin/zsh``, ``/bin/bash``,
  ``/usr/bin/env``, ``osascript``) and its C spelling ``CFNotificationCenterGetDistributedCenter``;
  every other way to start a process (``NSTask``, ``fork``, ``exec*``, ``popen``, ``NSWorkspace``,
  ``LSOpen*``, ``openApplication``, ``OSAScript``, ``NSUser*Task``, ``dlopen``, ``dlsym``,
  ``launchPath``); every other way to use the network, to listen, or to read a URL that is not a
  file (``URLRequest``, ``NSURLConnection``, ``NSURLDownload``, ``CF*Stream``, ``CFHTTP``,
  ``NetService``, ``socket(``, ``bind(``, ``listen(``, ``connect(``, ``accept(``,
  ``getaddrinfo``, ``gethostbyname``, ``URLComponents``, ``contentsOf:`` other than
  ``append(contentsOf:`` and ``insert(contentsOf:``, and every ``URL(string:`` or
  ``URL.init(string:``, with no exception); a Mach or distributed-objects port (``NSMachPort``,
  ``NSSocketPort``, ``NSMessagePort``, ``CFMessagePort*``, ``NSConnection``), which is a listener
  too; ``UserDefaults``, because the app keeps no settings of its own; and ``SWITCHBOARD_``,
  because the app passes the host no Switchboard setting. One exception, for tests only: in a
  Swift file under a ``Tests`` folder, :data:`TEST_SHEBANG` alone on the first line of a
  multi-line string literal, the only literal on the line it opens, is not reported
  (:func:`allowed_test_shebangs`). The self-test
  writes such a string to a file and starts it through the app's own child runner, as a
  stand-in for the interpreter; nothing under ``Tests`` is in the app. Every other shell, and
  ``/bin/sh`` anywhere else in that file or in that script, is still reported.
- [process] ``Process`` only in :data:`PROCESS_FILE`, that exact path (``ProcessInfo`` is another
  name). That file must start the bundled runtime: outside its comments it holds the string
  literal :data:`RUNTIME_IN_APP` and the array literal of :data:`RUNTIME_ARGV`. A tree that has
  Swift files but not that one is a finding, so moving the file without moving this constant
  fails. The change that adds the Swift package moves :data:`PROCESS_FILE` to the Kit file that
  starts the host; only one path is ever allowed.
- [pasteboard-read] no read of a pasteboard (:data:`PASTEBOARD_READS`): ``string(forType:``,
  ``data(forType:``, ``propertyList(forType:``, ``readObjects(``, ``pasteboardItems``,
  ``availableType(from:``, ``canReadObject(``, ``canReadItem(``, ``NSPasteboardReading``,
  ``PasteButton``, ``onPasteCommand``, ``pasteDestination``, ``readSelection(from:``, and no read
  of what is dropped on the app (``draggingPasteboard``, ``onDrop(``, ``dropDestination(``). The
  user pastes a code into a text field, which reads it (0002 §11.3). Writing, for Copy
  Diagnostics (``clearContents()``, ``setString(``, ``writeObjects(``), is allowed.
- [http] no ``http://``, ``https://``, ``ws://``, ``wss://`` or ``ftp://``, in any case.

In what the app shows:

- [must-not-claim] no phrase from the list in ``PRIVACY.md`` under "What this project must never
  claim" (:func:`must_not_claim_phrases`; fewer than :data:`MIN_PHRASES` and the lint fails
  closed), as a whole word in any case, with each run of white space read as one space
  (:func:`claims`), in:

  - each string literal of every Swift file outside a ``Tests`` folder, decoded (escapes, raw and
    multi-line strings), an interpolation read as one placeholder character and each literal
    inside it read on its own. Code and comments are not read: Swift's ``private`` keyword is not
    a claim;
  - each string constant, docstrings left out, of the functions :data:`PLIST_FUNCTIONS` in
    :data:`PLIST_SOURCE`, which render ``Info.plist``, the constant parts of an f-string
    included; a function named there that is missing is [parse];
  - each string value of the ``[app]`` table of :data:`PINS_FILE`, from which ``Info.plist`` takes
    the app's name and category;
  - each line of any ``*.plist``, ``*.entitlements``, ``*.strings`` or ``*.xcstrings`` file.

  A literal that is exactly a sentence in :data:`ALLOWED_SENTENCES` (one that negates a phrase,
  such as "That is not a review...") is not reported, and an entry that matches no literal in
  the tree is itself a finding. The list starts empty.

In what the app is, and how it is delivered:

- [url-scheme] no ``CFBundleURLTypes`` or ``CFBundleURLSchemes`` (:data:`URL_SCHEME_KEYS`),
  unconditionally: in Swift, where a handler for a URL (``onOpenURL``, ``kAEGetURL``,
  ``open <name>: [URL]`` or ``Array<URL>``) is refused too; in any plist, entitlements or strings
  file; in any string constant (docstrings left out) of a Python file under ``tools/macos/``,
  constants joined with ``+`` and an f-string whose every part is constant read as the one string
  they make; and in any string of a workflow. The one exception is an element of the set
  assigned to ``FORBIDDEN_PLIST_KEYS`` in :data:`PLIST_SOURCE`, the keys the build refuses, which
  must hold both keys; a check that needs a key's name refers to it through that constant. A URL
  scheme comes with the release that adds the core's nonce check (0002 §11.4 and §11.5), and that
  release changes this rule.
- [daemon] no ``LaunchDaemon``, ``SMAppService.daemon``, ``.daemon(plistName:``, ``SMJobBless``,
  ``SMJobSubmit``, ``AuthorizationExecuteWithPrivileges`` or ``launchctl``, in the same places
  (no handler rule). ``SMAppService.agent``, the login item's fallback, is allowed.
- [pkg] no installer package: no file or folder in the repository named ``*.pkg`` or ``*.mpkg``,
  and no ``.pkg``, ``.mpkg``, ``pkgbuild``, ``productbuild``, ``productsign`` or ``installer``
  with ``-pkg`` later on its line, in Swift, in a string constant of a Python file under
  ``tools/macos/`` (docstrings left out) or in a workflow. Markdown is not read: decisions/0002
  discusses ``.pkg``.
- [parse] a Swift file the lexer cannot read (an unterminated string, comment or interpolation,
  an unknown escape, a regex literal), a Python file that does not parse, a workflow or pins file
  that does not parse, a pins file with no ``[app]`` table, a missing plist function, or a
  ``PRIVACY.md`` without its list: the lint fails closed.

Files are found as the other guards find them (git's list, untracked files included; outside a
work tree, a walk).

What this lint cannot see: a phrase split by an interpolation or an f-string placeholder; a
phrase, a URL or a name split across Swift literals joined with ``+``, or across Python strings
that are not all constants; an invisible or look-alike character inside a phrase or a name (a
zero-width space, a Cyrillic letter); a function used without being called where it is named
(``let make = socket``, then ``make(...)``); a name built at run time, an Objective-C runtime
lookup (``NSClassFromString``) or a selector named by a string; shell quoting inside a word of a
workflow (``pkg'build'``); a bare ``/regex/`` literal, which it reads as code; whether the process
file passes the runtime's path and flags to the ``Process`` it starts (only that both are written
there, outside comments; the self-test passes another path on purpose); and anything outside the
files listed above, such as a dependency's source or what a workflow downloads. It catches
accidents, not a determined author (0002 §20).
"""

from __future__ import annotations

import ast
import bisect
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

if __package__ in (None, ""):  # run as a script: make ``tools.guards`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.guards._common import Violation, parse, repo_files, run_guard  # noqa: E402
from tools.guards.workflow_lint import workflow_files  # noqa: E402

# The one Swift file that may start a process. The change that adds the Swift package moves it to
# the Kit file that starts the host.
PROCESS_FILE: Final = "macos/App/HostCLI.swift"
# The bundled runtime, by its path in the app, and the arguments before the verb: the same as
# tools/macos/build_app.py's RUNTIME_IN_APP and RUNTIME_FLAGS (tests/test_macos_build.py checks).
RUNTIME_IN_APP: Final = "Contents/Resources/runtime/bin/lucille-host-runtime"
RUNTIME_ARGV: Final = ("-I", "-B", "-X", "utf8", "-m", "lucille_host")
# The functions that render Info.plist, their file, and the pins they take values from.
PLIST_SOURCE: Final = "tools/macos/build_app.py"
PLIST_FUNCTIONS: Final = ("info_plist", "folder_usage")
PINS_FILE: Final = "tools/macos/pins.toml"
TOOLS_PREFIX: Final = "tools/macos/"
# The name of the set of keys the build refuses: the one place a URL-scheme key may be named.
FORBIDDEN_PLIST_KEYS: Final = "FORBIDDEN_PLIST_KEYS"
URL_SCHEME_KEYS: Final = ("CFBundleURLTypes", "CFBundleURLSchemes")
PRIVACY_FILE: Final = "PRIVACY.md"
MUST_NOT_CLAIM_HEADING: Final = "## What this project must never claim"
MIN_PHRASES: Final = 15
# Whole literals that may hold a phrase because they negate it, each with its reason in review.
ALLOWED_SENTENCES: Final[frozenset[str]] = frozenset()
RESOURCE_SUFFIXES: Final = (".plist", ".entitlements", ".strings", ".xcstrings")
# The one shell a test may name, and where (decisions/0002 §20): alone on the first line of a
# multi-line string literal in a Swift file under a Tests folder, the script the self-test writes
# and starts through HostCLI as a stand-in for the interpreter.
TEST_SHEBANG: Final = "#!/bin/sh"
# What an interpolation, \(...), reads as inside a decoded literal.
PLACEHOLDER: Final = "\ufffc"
LINT_FILE: Final = "tools/guards/macos_lint.py"

Patterns = tuple[tuple[re.Pattern[str], str], ...]


def _patterns(*pairs: tuple[str, str]) -> Patterns:
    return tuple((re.compile(pattern), why) for pattern, why in pairs)


_NETWORK = "uses the network"
_SPAWN = "starts a process"
_LISTEN = "is a listener"
_SCRIPT = "runs a script"
DENIED_SWIFT: Final = _patterns(
    # decisions/0002 §20.
    (r"URLSession", _NETWORK),
    (r"\bNW[A-Z]\w*", _NETWORK),
    (r"\bimport\b[^\n]*\bNetwork\b", _NETWORK),
    (r"WebKit", "loads web content"),
    (r"\bWK[A-Z]\w*", "loads web content"),
    (r"NSXPCConnection", "talks to another process"),
    (r"NSXPCListener", _LISTEN),
    (r"\bxpc_\w+", "talks to another process"),
    (r"\bbootstrap_\w+", "looks up or offers a Mach service"),
    (r"CFSocket", _NETWORK),
    (r"NSStream", _NETWORK),
    (r"\b(?:Input|Output)Stream\b", _NETWORK),
    (r"getStreamsToHost", _NETWORK),
    (r"DistributedNotificationCenter", "talks to other processes"),
    (r"CFNotificationCenterGetDistributedCenter", "talks to other processes"),
    (r"NSAppleScript", _SCRIPT),
    (r"posix_spawn", _SPAWN),
    (r"\bsystem\s*\(", _SPAWN),
    (r"/bin/(?:sh|zsh|bash)\b", "names a shell"),
    (r"/usr/bin/env", "finds a program on PATH"),
    (r"osascript", _SCRIPT),
    # Other ways to start a process.
    (r"\bNSTask\b", _SPAWN),
    (r"\bv?fork\s*\(", _SPAWN),
    (r"\bf?exec[lv]p?[eP]?\s*\(", _SPAWN),
    (r"\bpopen\s*\(", _SPAWN),
    (r"NSWorkspace", "opens files, URLs or applications"),
    (r"\bLSOpen\w*", "opens files, URLs or applications"),
    (r"openApplication", _SPAWN),
    (r"OSAScript", _SCRIPT),
    (r"NSUser\w*Task", "runs a script or a program"),
    (r"\bdlopen\b", "loads code"),
    (r"\bdlsym\b", "loads code"),
    (r"launchPath", _SPAWN),
    # Other ways to use the network, to listen, or to read a URL that is not a file.
    (r"URLRequest", _NETWORK),
    (r"NSURLConnection", _NETWORK),
    (r"NSURLDownload", _NETWORK),
    (r"CFStream", _NETWORK),
    (r"CFReadStream", _NETWORK),
    (r"CFWriteStream", _NETWORK),
    (r"CFHTTP", _NETWORK),
    (r"NetService", _NETWORK),
    (r"\bsocket\s*\(", _NETWORK),
    (r"\bbind\s*\(", _LISTEN),
    (r"\blisten\s*\(", _LISTEN),
    (r"\bconnect\s*\(", _NETWORK),
    (r"\baccept\s*\(", _LISTEN),
    (r"getaddrinfo", _NETWORK),
    (r"gethostbyname", _NETWORK),
    (r"URLComponents", "builds a URL that is not a file"),
    (r"(?<!append\()(?<!insert\()\bcontentsOf\s*:", "reads a URL, which may not be a file"),
    (r"URL\s*(?:\.\s*init\s*)?\(\s*string\s*:", "makes a URL that is not a file"),
    # A Mach or distributed-objects port is a listener too.
    (r"\bNSMachPort\b", _LISTEN),
    (r"\bNSSocketPort\b", _LISTEN),
    (r"\bNSMessagePort\b", _LISTEN),
    (r"\bCFMessagePort\w*", _LISTEN),
    (r"\bNSConnection\b", _LISTEN),
    # The app keeps no settings of its own, and passes the host no Switchboard setting.
    (r"UserDefaults", "keeps settings outside the host's own files"),
    (r"SWITCHBOARD_", "passes the host a Switchboard setting"),
)
_PROCESS: Final = _patterns((r"\bProcess\b", f"starts a process outside {PROCESS_FILE}"))
_RUNTIME_ARGV_LITERAL = re.compile(
    r"\[\s*" + r"\s*,\s*".join(re.escape(f'"{arg}"') for arg in RUNTIME_ARGV) + r"\s*,?\s*\]"
)
PASTEBOARD_READS: Final = _patterns(
    (r"\.string\s*\(\s*forType\s*:", "reads the pasteboard's text"),
    (r"\.data\s*\(\s*forType\s*:", "reads the pasteboard's data"),
    (r"propertyList\s*\(\s*forType\s*:", "reads a property list from the pasteboard"),
    (r"readObjects\s*\(", "reads objects from the pasteboard"),
    (r"pasteboardItems", "reads the pasteboard's items"),
    (r"availableType\s*\(\s*from\s*:", "reads what the pasteboard holds"),
    (r"canReadObject\s*\(", "reads what the pasteboard holds"),
    (r"canReadItem\s*\(", "reads what the pasteboard holds"),
    (r"NSPasteboardReading", "reads objects from the pasteboard"),
    (r"\bPasteButton\b", "reads what is pasted"),
    (r"onPasteCommand", "reads what is pasted"),
    (r"pasteDestination", "reads what is pasted"),
    (r"readSelection\s*\(\s*from\b", "reads a Services selection"),
    (r"draggingPasteboard", "reads what is dropped"),
    (r"\bonDrop\s*\(", "reads what is dropped"),
    (r"\bdropDestination\s*\(", "reads what is dropped"),
)
HTTP: Final = _patterns((r"(?i)\b(?:https?|wss?|ftp)://", "is a network URL"))
URL_SCHEME: Final = _patterns(
    *((re.escape(key), "declares a URL scheme") for key in URL_SCHEME_KEYS)
)
URL_HANDLERS: Final = _patterns(
    (r"onOpenURL", "handles a URL"),
    (r"kAEGetURL", "handles a URL"),
    (r"\bopen\s+\w+\s*:\s*(?:\[\s*URL\s*\]|Array\s*<\s*URL\s*>)", "handles a URL"),
)
DAEMON: Final = _patterns(
    (r"LaunchDaemon", "installs a daemon"),
    (r"SMAppService\s*\.\s*daemon", "registers a daemon"),
    (r"\.daemon\s*\(\s*plistName\s*:", "registers a daemon"),
    (r"SMJobBless", "installs a privileged helper"),
    (r"SMJobSubmit", "submits a launchd job"),
    (r"AuthorizationExecuteWithPrivileges", "runs a program as root"),
    (r"launchctl", "drives launchd"),
)
PKG: Final = _patterns(
    (r"(?i)\.m?pkg\b", "names an installer package"),
    (r"\bpkgbuild\b", "builds an installer package"),
    (r"\bproductbuild\b", "builds an installer package"),
    (r"\bproductsign\b", "signs an installer package"),
    (r"\binstaller\b[^\n]*?\s-pkg\b", "installs an installer package"),
)
_PKG_NAME = re.compile(r"(?i).+\.m?pkg")
# The rules each kind of file is held to, besides [must-not-claim] and [parse].
SWIFT_RULES: Final = (
    ("swift-denylist", DENIED_SWIFT),
    ("pasteboard-read", PASTEBOARD_READS),
    ("http", HTTP),
    ("url-scheme", URL_SCHEME + URL_HANDLERS),
    ("daemon", DAEMON),
    ("pkg", PKG),
)
STRING_RULES: Final = (("url-scheme", URL_SCHEME), ("daemon", DAEMON), ("pkg", PKG))
RESOURCE_RULES: Final = (("url-scheme", URL_SCHEME), ("daemon", DAEMON))


# ---------------------------------------------------------------------------------------------
# The phrases


def must_not_claim_phrases(privacy_text: str) -> list[str]:
    """Each quoted phrase in PRIVACY.md's "What this project must never claim" section, with its
    white space folded (a phrase the file wraps is one phrase). Fails closed: a missing section,
    or fewer than :data:`MIN_PHRASES` phrases, raises ValueError."""
    if MUST_NOT_CLAIM_HEADING not in privacy_text:
        raise ValueError(f"{PRIVACY_FILE} has no section {MUST_NOT_CLAIM_HEADING[3:]!r}")
    section = privacy_text.split(MUST_NOT_CLAIM_HEADING, 1)[1].split("\n## ", 1)[0]
    phrases = [" ".join(found.split()) for found in re.findall(r'"([^"]+)"', section)]
    if len(phrases) < MIN_PHRASES:
        raise ValueError(
            f"{PRIVACY_FILE}'s must-never-claim section quotes {len(phrases)} phrases; this lint "
            f"expects at least {MIN_PHRASES}"
        )
    return phrases


def claims(text: str, phrases: Sequence[str]) -> list[str]:
    """The phrases ``text`` says, each as a whole word (not inside a longer word or a hyphenated
    one) in any case, with each run of white space in ``text`` read as one space."""
    folded = " ".join(text.split())
    return [
        phrase
        for phrase in phrases
        if re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", folded, re.IGNORECASE)
    ]


def _claim_findings(
    values: Iterable[tuple[int, str]],
    rel: str,
    phrases: Sequence[str],
    used: set[str] | None,
) -> list[Violation]:
    found: list[Violation] = []
    for line, value in values:
        folded = " ".join(value.split())
        if folded in ALLOWED_SENTENCES:
            if used is not None:
                used.add(folded)
            continue
        for phrase in claims(folded, phrases):
            found.append(
                Violation(
                    rel,
                    line,
                    "must-not-claim",
                    f"{folded[:80]!r} says {phrase!r}, which PRIVACY.md says this project must "
                    "never claim",
                )
            )
    return found


def _regex_findings(
    text: str, rel: str, rule: str, patterns: Patterns, first_line: int = 1
) -> list[Violation]:
    found: list[Violation] = []
    for pattern, why in patterns:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + first_line
            found.append(Violation(rel, line, rule, f"{match[0]!r} {why}"))
    return found


# ---------------------------------------------------------------------------------------------
# Swift


class SwiftSyntaxError(ValueError):
    """Swift text the lexer cannot read. The lint reports it rather than guess."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


@dataclass(frozen=True)
class SwiftScan:
    """What :func:`swift_scan` read: each string literal, decoded, with the line it starts on,
    and the text with every comment blanked (each of its characters but a newline a space)."""

    literals: tuple[tuple[int, str], ...]
    code: str


_ESCAPES: Final = {"0": "\0", "\\": "\\", "t": "\t", "n": "\n", "r": "\r", '"': '"', "'": "'"}
_UNICODE_ESCAPE = re.compile(r"\{([0-9A-Fa-f]{1,8})\}")
_CONTINUATION = re.compile(r"[ \t]*\n")


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.code = list(text)
        self.literals: list[tuple[int, str]] = []
        self.starts = [0, *(match.end() for match in re.finditer("\n", text))]

    def line(self, index: int) -> int:
        return bisect.bisect_right(self.starts, index)

    def blank(self, start: int, end: int) -> None:
        for index in range(start, end):
            if self.code[index] != "\n":
                self.code[index] = " "

    def code_until(self, i: int, *, interpolation: bool) -> int:
        """Read code from ``i``: at the top level to the end, inside ``\\(`` to its ``)``, whose
        index it returns."""
        text = self.text
        n = len(text)
        start = i
        depth = 0
        while i < n:
            c = text[i]
            if text.startswith("//", i):
                end = text.find("\n", i)
                end = n if end < 0 else end
                self.blank(i, end)
                i = end
            elif text.startswith("/*", i):
                i = self.block_comment(i)
            elif c in '#"':
                hashes = 0
                while i + hashes < n and text[i + hashes] == "#":
                    hashes += 1
                after = text[i + hashes] if i + hashes < n else ""
                if after == '"':
                    i = self.string(i, hashes)
                elif after == "/" and hashes:
                    raise SwiftSyntaxError(self.line(i), "a regex literal, which is not read here")
                else:
                    i += max(hashes, 1)
            else:
                if interpolation and c == "(":
                    depth += 1
                elif interpolation and c == ")":
                    if depth == 0:
                        return i
                    depth -= 1
                i += 1
        if interpolation:
            raise SwiftSyntaxError(self.line(start), "an interpolation \\( is not closed")
        return n

    def block_comment(self, i: int) -> int:
        text = self.text
        start = i
        depth = 0
        while i < len(text):
            if text.startswith("/*", i):
                depth += 1
                i += 2
            elif text.startswith("*/", i):
                depth -= 1
                i += 2
                if depth == 0:
                    self.blank(start, i)
                    return i
            else:
                i += 1
        raise SwiftSyntaxError(self.line(start), "a /* comment is not closed")

    def string(self, i: int, hashes: int) -> int:
        """Read the string literal at ``i`` (``hashes`` ``#`` first), record it, and return the
        index after it."""
        text = self.text
        n = len(text)
        line = self.line(i)
        i += hashes
        multiline = text.startswith('"""', i)
        if multiline:
            end = text.find("\n", i + 3)
            if end < 0 or text[i + 3 : end].strip(" \t"):
                raise SwiftSyntaxError(line, 'a multi-line string starts on the line after """')
            i = end + 1
            close = '"""' + "#" * hashes
        else:
            i += 1
            close = '"' + "#" * hashes
        escape = "\\" + "#" * hashes
        # The literal's source lines; joined[k] is True when line k continues line k - 1.
        lines: list[list[str]] = [[]]
        joined = [False]
        while True:
            if i >= n:
                raise SwiftSyntaxError(line, "a string literal is not closed")
            if text.startswith(close, i):
                i += len(close)
                break
            c = text[i]
            if c == "\n":
                if not multiline:
                    raise SwiftSyntaxError(line, "a string literal is not closed on its line")
                lines.append([])
                joined.append(False)
                i += 1
                continue
            if not text.startswith(escape, i):
                lines[-1].append(c)
                i += 1
                continue
            j = i + len(escape)
            kind = text[j] if j < n else ""
            if kind == "(":
                lines[-1].append(PLACEHOLDER)
                i = self.code_until(j + 1, interpolation=True) + 1
            elif kind == "u":
                match = _UNICODE_ESCAPE.match(text, j + 1)
                point = int(match.group(1), 16) if match is not None else -1
                if match is None or point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                    raise SwiftSyntaxError(self.line(i), "a \\u{...} escape is not valid")
                lines[-1].append(chr(point))
                i = match.end()
            elif kind in _ESCAPES:
                lines[-1].append(_ESCAPES[kind])
                i = j + 1
            elif multiline and (continued := _CONTINUATION.match(text, j)) is not None:
                lines.append([])
                joined.append(True)
                i = continued.end()
            else:
                raise SwiftSyntaxError(self.line(i), f"an unknown escape \\{kind}")
        value = _multiline_value(lines, joined, line) if multiline else "".join(lines[0])
        self.literals.append((line, value))
        return i


def _multiline_value(lines: list[list[str]], joined: list[bool], line: int) -> str:
    """A multi-line literal's value: its lines, each without the closing delimiter's indentation."""
    indent = "".join(lines[-1])
    if indent.strip(" \t"):
        raise SwiftSyntaxError(line, 'a multi-line string\'s closing """ does not start its line')
    value = ""
    for index, chars in enumerate(lines[:-1]):
        text = "".join(chars)
        if text.startswith(indent):
            text = text[len(indent) :]
        elif text.strip(" \t"):
            raise SwiftSyntaxError(
                line, "a line of a multi-line string is indented less than its closing delimiter"
            )
        else:
            text = ""
        value += text if index == 0 else ("" if joined[index] else "\n") + text
    return value


def swift_scan(text: str) -> SwiftScan:
    """Read Swift ``text``: its string literals and its code with comments blanked.

    It reads ``//`` and nested ``/* */`` comments; ``"..."``, multi-line and raw (``#"..."#``,
    with any number of ``#``) strings; ``\\(...)`` interpolations, with code and strings of their
    own; and ``\\u{...}`` escapes. It raises :class:`SwiftSyntaxError` on what it cannot read.
    """
    scanner = _Scanner(text)
    scanner.code_until(0, interpolation=False)
    return SwiftScan(tuple(sorted(scanner.literals)), "".join(scanner.code))


def _is_test_swift(rel: str) -> bool:
    return "Tests" in rel.split("/")[:-1]


def allowed_test_shebangs(text: str, scan: SwiftScan) -> tuple[list[tuple[int, int]], set[int]]:
    """Where ``text`` holds :data:`TEST_SHEBANG` as a test may: alone on the first line of a
    multi-line string literal, whose value starts with the shebang and a newline, and which is
    the only literal that starts on its opening line, a line whose code (outside any comment)
    ends with the three quotes that open it. Returns the shebang's spans in ``text``, and the
    lines those literals start on in ``scan``; each such line holds that one literal, so the
    caller can tell it by its line. The caller allows them only in a file under a ``Tests``
    folder."""
    starts = [0, *(match.end() for match in re.finditer("\n", text))]
    opened_on: dict[int, int] = {}
    for line, _ in scan.literals:
        opened_on[line] = opened_on.get(line, 0) + 1
    spans: list[tuple[int, int]] = []
    lines: set[int] = set()
    for line, value in scan.literals:
        if not value.startswith(TEST_SHEBANG + "\n") or line >= len(starts):
            continue
        if opened_on[line] != 1:
            continue
        # Comments are blanked in scan.code, character for character, so its lines are text's.
        end = scan.code.find("\n", starts[line - 1])
        opening = scan.code[starts[line - 1] : len(scan.code) if end < 0 else end]
        if not opening.rstrip(" \t").endswith('"""'):
            continue
        end = text.find("\n", starts[line])
        raw = text[starts[line] : len(text) if end < 0 else end]
        if raw.strip(" \t") != TEST_SHEBANG:
            continue
        begin = starts[line] + raw.index(TEST_SHEBANG)
        spans.append((begin, begin + len(TEST_SHEBANG)))
        lines.add(line)
    return spans, lines


def _blanked(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """``text`` with each span's characters made spaces, so that every line keeps its number."""
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _swift_rule_findings(text: str, rel: str, first_line: int = 1) -> list[Violation]:
    """The regex rules of :data:`SWIFT_RULES`, and [process] outside :data:`PROCESS_FILE`."""
    found: list[Violation] = []
    for rule, patterns in SWIFT_RULES:
        found += _regex_findings(text, rel, rule, patterns, first_line)
    if rel != PROCESS_FILE:
        found += _regex_findings(text, rel, "process", _PROCESS, first_line)
    return found


def check_swift(
    text: str, rel: str, phrases: Sequence[str], used: set[str] | None = None
) -> list[Violation]:
    """Check one Swift file's text as if it were at ``rel``. The regex rules read the whole
    text, comments included; then, once the lexer has read it, the code with its comments
    blanked, so that a comment inside a call (``connect/**/(``) does not hide it; and each
    string literal decoded, so that an escape (``https:\\u{2F}/``) does not hide a URL or a
    name."""
    try:
        scan = swift_scan(text)
    except SwiftSyntaxError as exc:
        found = _swift_rule_findings(text, rel)
        return sorted({*found, Violation(rel, exc.line, "parse", f"cannot read: {exc.message}")})
    spans: list[tuple[int, int]] = []
    shebangs: set[int] = set()
    if _is_test_swift(rel):
        spans, shebangs = allowed_test_shebangs(text, scan)
    found = _swift_rule_findings(_blanked(text, spans), rel)
    found += _swift_rule_findings(_blanked(scan.code, spans), rel)
    for line, value in scan.literals:
        if line in shebangs and value.startswith(TEST_SHEBANG + "\n"):
            value = " " * len(TEST_SHEBANG) + value[len(TEST_SHEBANG) :]
        found += _swift_rule_findings(value, rel, line)
    if rel == PROCESS_FILE:
        if RUNTIME_IN_APP not in {value for _, value in scan.literals}:
            found.append(
                Violation(rel, 1, "process", f"the bundled runtime {RUNTIME_IN_APP!r} is not here")
            )
        if _RUNTIME_ARGV_LITERAL.search(scan.code) is None:
            argv = ", ".join(f'"{arg}"' for arg in RUNTIME_ARGV)
            found.append(
                Violation(rel, 1, "process", f"the runtime's arguments [{argv}] are not here")
            )
    if not _is_test_swift(rel):
        found += _claim_findings(scan.literals, rel, phrases, used)
    return sorted(set(found))


# ---------------------------------------------------------------------------------------------
# Python, the pins, resources and workflows


def _docstrings(tree: ast.Module) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _strings(node: ast.AST, skip: set[int]) -> Iterator[ast.Constant]:
    """Every string constant under ``node`` but those in ``skip``, f-strings' constant parts
    included."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and id(sub) not in skip:
            yield sub


def _joined(node: ast.AST, skip: set[int]) -> str | None:
    """The string ``node`` is when it is built from string constants alone: a constant not in
    ``skip``, two such joined with ``+``, or an f-string whose every part is one. Otherwise
    None."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) and id(node) not in skip else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _joined(node.left, skip), _joined(node.right, skip)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                if value.conversion != -1 or value.format_spec is not None:
                    return None
                value = value.value
            part = _joined(value, skip)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    return None


def _built_strings(node: ast.AST, skip: set[int]) -> Iterator[tuple[int, str]]:
    """``(line, value)`` for each string under ``node`` built from constants alone (``+`` of
    constants, or an f-string whose every part is constant), read as the one string it is."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp | ast.JoinedStr):
            value = _joined(sub, skip)
            if value is not None:
                yield sub.lineno, value


def _refused_keys(tree: ast.Module, skip: set[int]) -> list[ast.Constant]:
    """The URL-scheme keys among the strings assigned to ``FORBIDDEN_PLIST_KEYS``, at the top
    of the module."""
    found: list[ast.Constant] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == FORBIDDEN_PLIST_KEYS for t in targets):
            found += [key for key in _strings(value, skip) if key.value in URL_SCHEME_KEYS]
    return found


def _plist_findings(
    tree: ast.Module, rel: str, skip: set[int], phrases: Sequence[str], used: set[str] | None
) -> list[Violation]:
    """[url-scheme] for FORBIDDEN_PLIST_KEYS, and [must-not-claim] for the plist functions."""
    found: list[Violation] = []
    named = {key.value for key in _refused_keys(tree, skip)}
    missing = [key for key in URL_SCHEME_KEYS if key not in named]
    if missing:
        found.append(
            Violation(
                rel,
                1,
                "url-scheme",
                f"{FORBIDDEN_PLIST_KEYS} must refuse {', '.join(missing)}, until the release that "
                "adds the nonce check",
            )
        )
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in PLIST_FUNCTIONS
    }
    for name in PLIST_FUNCTIONS:
        if name not in functions:
            found.append(Violation(rel, 1, "parse", f"{name}(), which renders Info.plist, is gone"))
            continue
        values = [(node.lineno, node.value) for node in _strings(functions[name], skip)]
        values += _built_strings(functions[name], skip)
        found += _claim_findings(values, rel, phrases, used)
    return found


def check_python(
    text: str, rel: str, phrases: Sequence[str], used: set[str] | None = None
) -> list[Violation]:
    """Check one Python file under tools/macos/ as if it were at ``rel``."""
    tree = parse(text, rel)
    if isinstance(tree, Violation):
        return [tree]
    docstrings = _docstrings(tree)
    found: list[Violation] = []
    exempt: set[int] = set()
    if rel == PLIST_SOURCE:
        found += _plist_findings(tree, rel, docstrings, phrases, used)
        exempt = {id(key) for key in _refused_keys(tree, docstrings)}
    values = [(node.lineno, node.value, id(node) in exempt) for node in _strings(tree, docstrings)]
    values += [(line, value, False) for line, value in _built_strings(tree, docstrings)]
    for line, value, refused_key in values:
        for rule, patterns in STRING_RULES:
            if rule == "url-scheme" and refused_key:
                continue
            for pattern, why in patterns:
                match = pattern.search(value)
                if match is not None:
                    found.append(Violation(rel, line, rule, f"{match[0]!r} {why}"))
    return sorted(set(found))


def _toml_line(text: str, table: str, key: str) -> int:
    current = ""
    for number, line in enumerate(text.splitlines(), start=1):
        header = re.match(r"\s*\[([^\[\]]+)\]\s*(?:#.*)?$", line)
        if header is not None:
            current = header.group(1).strip()
        elif current == table and re.match(rf"\s*{re.escape(key)}\s*=", line):
            return number
    return 1


def check_pins(
    text: str, rel: str, phrases: Sequence[str], used: set[str] | None = None
) -> list[Violation]:
    """Check the string values of the pins' ``[app]`` table, from which Info.plist takes the
    app's name and category."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [Violation(rel, 1, "parse", f"cannot parse: {exc}")]
    app = data.get("app")
    if not isinstance(app, dict):
        return [Violation(rel, 1, "parse", "there is no [app] table")]
    values = [
        (_toml_line(text, "app", key), value)
        for key, value in app.items()
        if isinstance(value, str)
    ]
    return sorted(set(_claim_findings(values, rel, phrases, used)))


def check_resource(
    text: str, rel: str, phrases: Sequence[str], used: set[str] | None = None
) -> list[Violation]:
    """Check a plist, entitlements or strings file, line by line."""
    found: list[Violation] = []
    for rule, patterns in RESOURCE_RULES:
        found += _regex_findings(text, rel, rule, patterns)
    found += _claim_findings(enumerate(text.splitlines(), start=1), rel, phrases, used)
    return sorted(set(found))


def _yaml_strings(value: Any) -> Iterator[str]:
    """Every string in a parsed YAML document, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _yaml_strings(key)
            yield from _yaml_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _yaml_strings(item)


def _line_of(text: str, needle: str) -> int:
    """The first line holding ``needle`` (1-based), or 1. YAML parsing drops line numbers."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return 1


def check_workflow(text: str, rel: str) -> list[Violation]:
    """Check every string of one workflow, keys included, as if it were at ``rel``."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return [Violation(rel, 1, "parse", "the file is not valid YAML")]
    if not isinstance(document, dict):
        return [Violation(rel, 1, "parse", "the file is not a YAML mapping")]
    found: list[Violation] = []
    for value in _yaml_strings(document):
        for rule, patterns in STRING_RULES:
            for pattern, why in patterns:
                for match in pattern.finditer(value):
                    line = _line_of(text, match[0])
                    found.append(Violation(rel, line, rule, f"{match[0]!r} {why}"))
    return sorted(set(found))


# ---------------------------------------------------------------------------------------------
# The tree


def _is_tool(rel: str) -> bool:
    return rel.startswith(TOOLS_PREFIX) and rel.endswith(".py")


def _is_package(rel: str) -> bool:
    return any(_PKG_NAME.fullmatch(part) for part in rel.split("/"))


def lint_files(root: Path) -> list[str]:
    """Every file this lint reads or names; PRIVACY.md, which only holds the phrases, aside."""
    workflows = set(workflow_files(root))
    return [
        rel
        for rel in repo_files(root, untracked=True)
        if rel.endswith((".swift", *RESOURCE_SUFFIXES))
        or _is_tool(rel)
        or rel == PINS_FILE
        or rel in workflows
        or _is_package(rel)
    ]


def check(root: Path) -> list[Violation]:
    """Check every file this lint reads under ``root``."""
    files = lint_files(root)
    workflows = set(workflow_files(root))
    swift = [rel for rel in files if rel.endswith(".swift")]
    found: list[Violation] = []
    phrases: list[str] = []
    shown = [
        rel
        for rel in files
        if (rel.endswith(".swift") and not _is_test_swift(rel))
        or rel in (PLIST_SOURCE, PINS_FILE)
        or rel.endswith(RESOURCE_SUFFIXES)
    ]
    if shown:
        try:
            phrases = must_not_claim_phrases((root / PRIVACY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            found.append(Violation(PRIVACY_FILE, 0, "parse", f"no phrases to check: {exc}"))
    used: set[str] = set()
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        if rel.endswith(".swift"):
            found += check_swift(text, rel, phrases, used)
        elif _is_tool(rel):
            found += check_python(text, rel, phrases, used)
        elif rel == PINS_FILE:
            found += check_pins(text, rel, phrases, used)
        elif rel in workflows:
            found += check_workflow(text, rel)
        if rel.endswith(RESOURCE_SUFFIXES):
            found += check_resource(text, rel, phrases, used)
        if _is_package(rel):
            found.append(Violation(rel, 0, "pkg", "an installer package is in the repository"))
    if swift and PROCESS_FILE not in swift:
        found.append(
            Violation(
                PROCESS_FILE,
                0,
                "process",
                "the one Swift file that may start a process is missing; a file that moves "
                "takes PROCESS_FILE in this lint with it",
            )
        )
    if phrases:
        for sentence in sorted(ALLOWED_SENTENCES - used):
            found.append(
                Violation(
                    LINT_FILE,
                    0,
                    "must-not-claim",
                    f"ALLOWED_SENTENCES holds {sentence!r}, which no literal is; remove it",
                )
            )
    return sorted(set(found))


def main(argv: list[str] | None = None) -> int:
    """Script entry point: ``python tools/guards/macos_lint.py [ROOT]``."""
    return run_guard("macos_lint", sys.argv[1:] if argv is None else argv, lint_files, check)


if __name__ == "__main__":
    raise SystemExit(main())
