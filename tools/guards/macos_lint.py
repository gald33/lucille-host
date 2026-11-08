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
  other ways to start a process, to load code, or to open a URL, or to open, end, find,
  register, drive or hand something to another application, or to ask macOS for more rights
  (``NSTask``, ``fork``, ``exec*``, ``popen``, ``NSWorkspace`` outside :data:`OPEN_FILE`,
  ``LSOpen*``, ``LSSetDefault*``, ``LSRegister*``, ``AESend*``, ``NSAppleEventDescriptor``,
  ``openApplication``, ``openURL``, ``OpenURLAction``, ``Link(``, ``.link``, a link attribute
  by its own name (``NSLink``, ``LinkAttribute``) or an attribute named by a string
  (``NSAttributedString.Key(``), links made of the text a view shows
  (``isAutomaticLinkDetectionEnabled``, ``isAutomaticDataDetectionEnabled``,
  ``enabledTextCheckingTypes``, ``checkText(in:``, ``checkTextInDocument``), rich text read from
  data, whose links are links too (``html:``, ``rtf:``, ``rtfd:``, ``docFormat:``, ``withRTF:``,
  ``readRTFD``, ``documentAttributes``, ``documentType``, ``DocumentReadingOptionKey``,
  ``NSAttributedString(data:`` or ``(url:``), ``markdown:``, a key SwiftUI reads as Markdown
  (``LocalizedStringKey``, ``LocalizedStringResource``, ``LocalizationValue``, ``Text(.init(``),
  a Markdown link inside a string literal (:data:`MARKDOWN_LINKS`), ``NSSharingService``,
  ``ShareLink``, ``forceTerminate``, ``runningApplications(withBundleIdentifier``,
  ``NSRunningApplication(processIdentifier``, ``requestAuthorization``,
  ``FileManager(authorization:``, ``OSAScript``, ``NSUser*Task``, ``dlopen``, ``dlsym``,
  ``.load()``, ``loadAndReturnError``, ``principalClass``, ``launchPath``); a button pressed by
  code, not by a click (``performClick``), and a function reached by its symbol or put in
  another's place (``_silgen_name``, ``_cdecl``, ``_dynamicReplacement``); other ways to use the
  network, to listen, or to read or make a URL that is not a file (``URLRequest``,
  ``NSURLConnection``, ``NSURLDownload``, ``CF*Stream``, ``CFHTTP``, ``NetService``, ``socket(``,
  ``bind(``, ``listen(``, ``connect(``, ``accept(``, ``getaddrinfo``, ``gethostbyname``,
  ``URLComponents``, a URL found in text (``NSDataDetector``, ``NSTextChecking*``,
  ``NSSpellChecker``), ``contentsOf:`` other than ``append(contentsOf:`` and
  ``insert(contentsOf:``, ``URL(dataRepresentation:``, ``URL(absoluteURLWithDataRepresentation:``,
  ``CFURLCreateWithString``, ``CFURLCreateWithBytes``, ``CFURLCreateAbsoluteURLWithBytes``,
  ``NSURL(scheme:``, an ``.init(string:`` whose type is left out, after a symbol or a keyword,
  and every ``URL(string:`` or ``URL.init(string:`` outside :data:`OPEN_FILE`, with every other
  way there to URL's initializers that these rules spell (:data:`URL_INITIALIZERS`; the opener's
  file is held to its design, below): ``NSURL`` at all, which the app's code does not use, since
  its initializers, a subclass's and the pasteboard's make a URL from a string by names no rule
  here lists; URL's type as a value or as a type, or called from inside parentheses, or a member
  type of URL or of a type that holds it, which can be URL itself, NSURL or a parser of text
  (``URL.self``, ``(URL).self``, ``(URL)(string:``, ``URL.Type``, ``URL.ValueType``,
  ``URL.ReferenceType``, ``URL.ParseStrategy``, ``[URL].Element``, ``Optional<URL>.Wrapped``),
  with the member type's name right after its dot or with white space on both sides of it, so
  that a sentence that ends with "URL." is not one (Swift's lexer refuses white space after a
  dot that has none before it); ``URL.init`` not called, which from a string is
  ``init(string:)``; ``type(of:``, which for a URL is URL's own type; ``.self.init(string:``,
  on any type taken as a value; a generic parameter, ``Self`` or an associated type made URL
  (``where T == URL``, ``associatedtype A = URL``), whose name then reaches URL's initializers;
  and a URL parsed or expanded from text (``strategy: .url``, ``URL(template:``)); a Mach or
  distributed-objects port (``NSMachPort``, ``NSSocketPort``, ``NSMessagePort``,
  ``CFMessagePort*``, ``NSConnection``), which is a listener too; ``UserDefaults``, because the
  app keeps no settings of its own; and ``SWITCHBOARD_``, because the app passes the host no
  Switchboard setting. One exception, for tests only: in a Swift file under a ``Tests`` folder,
  :data:`TEST_SHEBANG` alone on the first line of a multi-line string literal, the only literal
  on the line it opens, is not reported (:func:`allowed_test_shebangs`). The self-test
  writes such a string to a file and starts it through the app's own child runner, as a
  stand-in for the interpreter; nothing under ``Tests`` is in the app. Every other shell, and
  ``/bin/sh`` anywhere else in that file or in that script, is still reported.
- [process] ``Process`` only in :data:`PROCESS_FILE`, that exact path (``ProcessInfo`` is another
  name). That file must start the bundled runtime: outside its comments it holds the string
  literal :data:`RUNTIME_IN_APP` and the array literal of :data:`RUNTIME_ARGV`. A tree that has
  Swift files but not that one is a finding, so moving the file without moving this constant
  fails. The change that adds the Swift package moves :data:`PROCESS_FILE` to the Kit file that
  starts the host; only one path is ever allowed.
- [open] the app's one opener, which shows the user a place in macOS from a click (0002 §20), is
  :data:`OPEN_FILE`, that exact path; no other file names the class it calls. Everywhere, no
  ``Privacy_AllFiles``: the app never opens Full Disk Access. Outside :data:`OPEN_FILE`, no
  ``x-apple.systempreferences``, in any case, comments included; and, over the code with comments
  blanked (:data:`OPENER_NAMES`), the opener's type only in a call, ``SystemPlaces.open(``, so
  that tests/test_macos_build.py finds every call by its text and reads where each one sits; no
  ``extension`` of ``SystemPlaces``, ``SystemPlace``, ``URL``, ``NSURL``, ``Optional``,
  ``Equatable`` or ``Sendable``, no ``typealias`` that names one of them, and no new
  declaration of ``URL``, ``NSURL`` or ``SystemPlaces``, which could change what the opener's
  code makes or opens; and no backtick, its strings included, since a backticked word is a name
  these rules do not read (the app's code has none; a comment may). In
  :data:`OPEN_FILE`, over its text with comments blanked unless said: ``NSWorkspace`` once in the
  whole text, comments included, as ``NSWorkspace.shared.open(<a name>)``, and no
  ``configuration:`` or ``withApplicationAt``; each ``URL(string:`` or ``URL.init(string:`` takes
  one string literal and nothing else, whose value, decoded, is an entry of :data:`OPENABLE`, and
  each entry is the value of exactly one of them; every other URL it makes is
  ``URL(fileURLWithPath: "/Applications", isDirectory: true)``, once, with nothing taken from it
  or added to it (no ``filePath:``, ``relativeTo:``, ``appending*`` or ``deleting*``); every
  string literal in it, decoded, is an entry of :data:`OPENABLE` or ``/Applications``; its code
  is :data:`OPEN_DESIGN`, token for token (:func:`design_tokens`: each one-line string literal one
  token, white space aside), so that it has one function, one call, as its last statement, and no
  URL kept from anything else (a property, another object, another overload) reaches the call;
  and its strings are :data:`OPEN_LITERALS`, in that order, so that each place opens its own
  address. A tree without :data:`OPEN_FILE` is clean: opening nothing is safe.
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
  (no handler rule). ``SMAppService.agent`` is not used by the app (a build test refuses it).
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
there, outside comments; the self-test passes another path on purpose); what
``NSWorkspace.shared.open`` does with the URL it is given, and whether the opener is called only
from a click (tests/test_macos_build.py reads each call, and each use of the closure Set Up's
window calls it through); an action sent by code rather than by a click (``sendAction``,
``perform``), which this lint does not refuse, since the app's windows send their own; an
extension, in another file, of a protocol or a superclass of a type the opener's code names
(``Hashable``, ``NSObject``), other than those [open] refuses and one whose ``where`` makes
``Self`` URL; a URL decoded from data by a ``Decoder`` or an unarchiver (``URL(from:``,
``NSKeyedUnarchiver``, or a type of the app's own that holds one: ``URL.self`` and ``[URL].self``
are refused); URL's type reached by a name these rules cannot tie to it, and then called
(``C.Element(string:``, ``M.init(string:``): an associated type that a constraint makes URL
(``C.Element``, where ``C: Collection<URL>``), or a type that code of the app's own returns or
keeps as a value (``T.self``, in a generic function or type); the URL parse strategy, ``.url``,
other than after ``strategy:``; with its type left out, an attribute named by a string
(``.init(rawValue: name)``), rich text read from data (``.init(data:``) or a key that a view
other than ``Text`` takes (``Toggle(.init(line), isOn: on)``); rich text read by ``read(from:``;
a way to open something that no name here spells;
``SMAppService.openSystemSettingsLoginItems()``, the login item's own call, which opens System
Settings with no URL and is not this rule's; and anything outside the files listed above, such as
a dependency's source or what a workflow downloads. It catches accidents, not a determined author
(0002 §20).
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
# The app's one opener (decisions/0002 §20): the one Swift file that may name NSWorkspace and make a
# URL from a string, to show the user a place in macOS from a click. It may open the two System
# Settings pages here, and the Applications folder. The spellings are the best known for macOS 13
# and later, not yet seen on a Mac: check_app.py places opens each and says what it shows, and its
# SETTINGS_PANES holds the same two (tests/test_macos_build.py checks).
OPEN_FILE: Final = "macos/App/SystemPlaces.swift"
DATE_AND_TIME_PAGE: Final = "x-apple.systempreferences:com.apple.Date-Time-Settings.extension"
FILES_AND_FOLDERS_PAGE: Final = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"
)
OPENABLE: Final[frozenset[str]] = frozenset({DATE_AND_TIME_PAGE, FILES_AND_FOLDERS_PAGE})
# The one other place the opener shows, as a file URL.
APPLICATIONS_FOLDER: Final = "/Applications"
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
_OPEN_URL = "opens a URL"
_FINDS_AN_APP = "finds another application to act on"
_APPLE_EVENT = "drives another application with an Apple event"
_SHARES = "hands what it shares to another application"
_LOADS = "loads code"
_URL_FROM_DATA = "makes a URL that is not a file"
_LINK = "makes a link, which opens a URL when it is clicked"
_RICH_TEXT = "reads rich text, whose links open a URL when they are clicked"
_MARKDOWN_KEY = "makes a key SwiftUI reads as Markdown, whose links open a URL when clicked"
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
    (r"\bLSOpen\w*", "opens files, URLs or applications"),
    (r"openApplication", _SPAWN),
    # Other ways to open a URL: SwiftUI's, a link, and Markdown, whose links open what they name
    # when clicked (a Markdown link inside a string literal is check_swift's: MARKDOWN_LINKS); a
    # link attribute by its own name, or an attribute named by a string; links made of the text a
    # view shows; rich text read from data, whose links are links too; and a key that SwiftUI reads
    # as Markdown, made from text or with its type left out for Text.
    (r"openURL", _OPEN_URL),
    (r"OpenURLAction", _OPEN_URL),
    (r"\bLink\s*\(", _OPEN_URL),
    (r"\.link\b", _LINK),
    (r"\bNSLink\w*|\bLinkAttribute\b", _LINK),
    (
        r"\bNSAttributedString\s*\.\s*Key\s*(?:\.\s*init\s*)?\(",
        "names an attribute by a string, which can be a link's",
    ),
    (
        r"[aA]utomatic(?:Link|Data)Detection|\benabledTextCheckingTypes\b"
        r"|\bcheckText\s*\(\s*in\s*:|\bcheckTextIn(?:Document|Selection)\b",
        "makes links of the text a view shows, which open a URL when they are clicked",
    ),
    (
        r"\b(?:html|rtfd?|docFormat|rtfdFileWrapper|withRTFD?)\s*:|\breadRTFD\b"
        r"|\bdocumentAttributes\b|\b[dD]ocumentType\b|\bDocumentReadingOptionKey\b",
        _RICH_TEXT,
    ),
    (
        r"\b(?:NS\w*AttributedString|NSTextStorage)\s*(?:\.\s*init\s*)?\(\s*(?:data|url)\s*:",
        _RICH_TEXT,
    ),
    (r"markdown\s*:", "reads Markdown, whose links open a URL when they are clicked"),
    (r"\bLocalized(?:StringKey|StringResource)\b|\bLocalizationValue\b", _MARKDOWN_KEY),
    (r"\bText\s*\(\s*\.\s*init\s*\(", _MARKDOWN_KEY),
    # Other ways to end, find, register or drive another application, to hand it something, or to
    # act with more rights.
    (r"forceTerminate", "ends another application"),
    (r"runningApplications\s*\(\s*withBundleIdentifier", _FINDS_AN_APP),
    (r"NSRunningApplication\s*\(\s*processIdentifier", _FINDS_AN_APP),
    (r"\bLSSetDefault\w*", "makes an application the default for a URL scheme or a file type"),
    (r"\bLSRegister\w*", "registers an application with Launch Services"),
    (r"\bAESend\w*", _APPLE_EVENT),
    (r"NSAppleEventDescriptor", _APPLE_EVENT),
    (r"NSSharingService", _SHARES),
    (r"\bShareLink\b", _SHARES),
    (r"requestAuthorization", "asks macOS for more rights"),
    (r"FileManager\s*\(\s*authorization\s*:", "acts on files with more rights"),
    # A button pressed by code, not by a click; and a function reached by its symbol, or put in
    # another's place, whatever name the code calls it by.
    (r"performClick", "presses a button by code, not by a click"),
    (r"\b_silgen_name\b", "reaches a function by its symbol"),
    (r"\b_cdecl\b", "reaches a function by its symbol"),
    (r"\b_dynamicReplacement\b", "puts a function in another's place"),
    (r"OSAScript", _SCRIPT),
    (r"NSUser\w*Task", "runs a script or a program"),
    (r"\bdlopen\b", _LOADS),
    (r"\bdlsym\b", _LOADS),
    # A bundle's code, loaded.
    (r"\.load\s*\(\s*\)", _LOADS),
    (r"loadAndReturnError", _LOADS),
    (r"principalClass", _LOADS),
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
    # A URL found in text, by a data detector or a text checker asked for links.
    (r"\bNSDataDetector\b", "finds a URL that is not a file in text"),
    (r"\bNSTextChecking\w*|\bNSSpellChecker\b", "checks text, which can find a URL in it"),
    (r"(?<!append\()(?<!insert\()\bcontentsOf\s*:", "reads a URL, which may not be a file"),
    (r"URL\s*\(\s*(?:absoluteURLWithDataRepresentation|dataRepresentation)\s*:", _URL_FROM_DATA),
    (r"\bCFURLCreate(?:WithString|WithBytes|AbsoluteURLWithBytes)\b", _URL_FROM_DATA),
    (r"NSURL\s*\(\s*scheme\s*:", _URL_FROM_DATA),
    # An initializer whose type is left out (`let url: URL? = .init(string: text)`, or after a
    # keyword, `return .init(string: text)`); never a type's own, such as `CodeField(string:` or
    # `NSAttributedString.init(string:`.
    (
        r"(?:(?<![\w\s])|(?<=\breturn)|(?<=\bin)|(?<=\btry)|(?<=\bawait)|(?<=\byield))"
        r"\s*\.\s*init\s*\(\s*string\s*:",
        "makes a URL that is not a file, unnamed",
    ),
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
# What only the opener may do: [swift-denylist] everywhere else, as before it was the opener's.
_OPENERS: Final = _patterns(
    (r"NSWorkspace", f"opens files, URLs or applications outside {OPEN_FILE}"),
    (
        r"URL\s*(?:\.\s*init\s*)?\(\s*string\s*:",
        f"makes a URL that is not a file outside {OPEN_FILE}",
    ),
)
# Every other way to URL's or NSURL's initializers, [swift-denylist] outside the opener too, whose
# own file its design holds: NSURL at all; URL's type as a value or a type, or called from inside
# parentheses, or a member type of URL or of a type that holds it, which can be URL itself
# (`URL.ValueType`, `[URL].Element`), NSURL (`URL.ReferenceType`) or a parser of text
# (`URL.ParseStrategy`); URL's initializer not called; a value's type; init(string:) on a type
# taken as a value; a generic parameter, Self or an associated type made URL; and a URL parsed or
# expanded from text. A member type's name is right after its dot, or has white space on both
# sides of the dot, so that a sentence that ends with "URL." is not one: Swift's lexer refuses
# white space after a dot that has none before it.
_MEMBER = r"(?:\s*\.(?:\s*self\b|[A-Z_]\w*)|\s+\.\s+[A-Z_]\w*)"
_URL_TYPE = f"which can reach a URL made from a string, outside {OPEN_FILE}"
_MADE_URL = f"whose initializers its name then reaches, outside {OPEN_FILE}"
URL_INITIALIZERS: Final = _patterns(
    (r"\bNSURL\b", f"is NSURL, whose initializers make a URL from a string, outside {OPEN_FILE}"),
    (
        r"\bURL\b(?:\s*[?!>\])])*" + _MEMBER,
        f"takes URL's type, a member type of URL, or one of a type that holds it, {_URL_TYPE}",
    ),
    (
        r"\bURL\s*[,:][^\n<>\[\]()]*[>\]](?:\s*[?!>\])])*" + _MEMBER,
        f"takes URL's type, a member type of URL, or one of a type that holds it, {_URL_TYPE}",
    ),
    (
        r"\bURL\s*\)[\s)]*\(\s*string\s*:",
        f"calls URL's type in parentheses with a string, outside {OPEN_FILE}",
    ),
    (
        r"\bURL[\s)]*\.\s*init\b(?!\s*\()",
        f"is URL's initializer as a value, from a string init(string:), outside {OPEN_FILE}",
    ),
    (r"\btype\s*\(\s*of\s*:", f"takes a value's type, which for a URL is URL's own, {_URL_TYPE}"),
    (
        r"\.\s*self\s*\.\s*init\s*\(\s*string\s*:",
        f"calls init(string:) on a type taken as a value, which can be URL, outside {OPEN_FILE}",
    ),
    (
        r"==\s*(?:\w+\s*\.\s*)?URL\b(?!\s*[.(])|(?<![.\w])URL\s*==",
        f"makes a generic parameter URL, {_MADE_URL}",
    ),
    (r"\bassociatedtype\b[^\n]*\bURL\b", f"makes an associated type URL, {_MADE_URL}"),
    (
        r"\bstrategy\s*:\s*\.\s*url\b",
        f"parses a URL that is not a file from text, outside {OPEN_FILE}",
    ),
    (
        r"\bURL\s*(?:\.\s*init\s*)?\(\s*template\s*:",
        f"expands a URL that is not a file from text, outside {OPEN_FILE}",
    ),
)
# [open]: Full Disk Access everywhere; a System Settings page outside the opener.
FULL_DISK_ACCESS: Final = _patterns(
    (r"Privacy_AllFiles", "names Full Disk Access, which the app never opens")
)
SETTINGS_PAGES: Final = _patterns(
    (r"(?i)x-apple\.systempreferences", f"names a System Settings page outside {OPEN_FILE}")
)
# [open] outside the opener, over code with its comments blanked: the opener's type only in a call,
# so that a build test finds every call by its text; no extension, new name or new declaration of a
# type the opener's code names, which could change what that code makes or opens; and no backtick,
# which makes a name of any word, and so would hide one from these patterns.
_OPENER_TYPES: Final = r"(?:SystemPlaces?|URL|NSURL|Optional|Equatable|Sendable)"
OPENER_NAMES: Final = _patterns(
    (
        r"\bSystemPlaces\b(?!\.open\()",
        f"names the opener other than in a call, SystemPlaces.open(, outside {OPEN_FILE}",
    ),
    (
        r"\bextension\s+(?:\w+\s*\.\s*)?" + _OPENER_TYPES + r"\b",
        f"extends a type the opener's code names, outside {OPEN_FILE}",
    ),
    (
        r"\btypealias\b[^=]*=\s*[^\n]*\b" + _OPENER_TYPES + r"\b",
        f"gives another name to a type the opener's code names, outside {OPEN_FILE}",
    ),
    (r"`", f"is a backtick, which can hide a name the opener's code uses, outside {OPEN_FILE}"),
    (
        r"\b(?:func|var|let|struct|class|enum|actor|protocol|typealias)\s+(?:URL|NSURL|SystemPlaces)\b",
        f"declares again a name the opener's code uses, outside {OPEN_FILE}",
    ),
)
# A Markdown link inside a string literal, decoded: SwiftUI's Text shows a literal's links, and
# opens what they name when clicked.
MARKDOWN_LINKS: Final = _patterns(
    (r"\[[^\]\n]*\]\([^)\n]*\)", "is a Markdown link, which SwiftUI opens when it is clicked")
)
# The opener's one call, the URLs it may make, and what may not be asked of them.
_OPEN_CALL = re.compile(r"NSWorkspace\s*\.\s*shared\s*\.\s*open\s*\(\s*[A-Za-z_]\w*\s*\)")
_OPEN_ASKS: Final = _patterns(
    (r"\bconfiguration\s*:", "opens with a configuration, which can start an application"),
    (r"\bwithApplicationAt\b", "opens with another application"),
    (r"\bfilePath\s*:", "makes a file URL that is not the Applications folder"),
    (r"\brelativeTo\s*:", "makes a URL relative to another"),
    (r"\bappending\w*", "adds to a URL"),
    (r"\bdeleting\w*", "takes part of a URL away"),
)
_URL_FROM_STRING = re.compile(r"URL\s*(?:\.\s*init\s*)?\(\s*string\s*:\s*")
_ANY_URL = re.compile(r"\bURL\s*(?:\.\s*init\s*)?\(")
_APPLICATIONS_URL = re.compile(
    r"URL\s*\(\s*fileURLWithPath\s*:\s*"
    + re.escape(f'"{APPLICATIONS_FOLDER}"')
    + r"\s*,\s*isDirectory\s*:\s*true\s*\)(?!\s*[.?!\[])"
)
_CLOSE_CALL = re.compile(r"\s*\)")
# The opener's code as designed, token by token (:func:`design_tokens`: comments blanked, each
# one-line string literal read as ``""``, white space aside), and the value of each of its strings,
# in order. Any other code there is a finding, so that nothing but the places written there reaches
# the one call: no second function, no URL kept from a property, another object or another file's
# extension, and each place opens its own address. A change to the file changes these, in review.
OPEN_DESIGN: Final = """\
import AppKit
@MainActor
enum SystemPlaces {
    static func open(_ place: SystemPlace) -> Bool {
        let url: URL?
        switch place {
        case .dateAndTime:
            url = URL(string: "")
        case .filesAndFolders:
            url = URL(string: "")
        case .applicationsFolder:
            url = URL(fileURLWithPath: "", isDirectory: true)
        }
        guard let url else {
            return false
        }
        return NSWorkspace.shared.open(url)
    }
}
"""
OPEN_LITERALS: Final = (DATE_AND_TIME_PAGE, FILES_AND_FOLDERS_PAGE, APPLICATIONS_FOLDER)
_ONE_LINE_LITERAL = re.compile(r'(#*)"(?:[^"\\\n]|\\.)*"\1')
_TOKEN = re.compile(r'""|[A-Za-z_]\w*|\S')
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
    """The regex rules of :data:`SWIFT_RULES`; [process] outside :data:`PROCESS_FILE`; and
    [open]'s everywhere, with what only the opener may do, and :data:`URL_INITIALIZERS`, refused
    outside :data:`OPEN_FILE`."""
    found: list[Violation] = []
    for rule, patterns in SWIFT_RULES:
        found += _regex_findings(text, rel, rule, patterns, first_line)
    if rel != PROCESS_FILE:
        found += _regex_findings(text, rel, "process", _PROCESS, first_line)
    found += _regex_findings(text, rel, "open", FULL_DISK_ACCESS, first_line)
    if rel != OPEN_FILE:
        found += _regex_findings(text, rel, "swift-denylist", _OPENERS, first_line)
        found += _regex_findings(text, rel, "swift-denylist", URL_INITIALIZERS, first_line)
        found += _regex_findings(text, rel, "open", SETTINGS_PAGES, first_line)
    return found


def _opener_findings(text: str, scan: SwiftScan, rel: str) -> list[Violation]:
    """[open] in :data:`OPEN_FILE`: one call that opens, of a closed list of places, each URL
    made here from a literal (the module docstring says which)."""
    found: list[Violation] = []
    code = scan.code

    def line_of(source: str, index: int) -> int:
        return source.count("\n", 0, index) + 1

    def finding(line: int, message: str) -> None:
        found.append(Violation(rel, line, "open", message))

    # One NSWorkspace in the whole text, comments included, and that one the call.
    named = [match.start() for match in re.finditer("NSWorkspace", text)]
    if not named:
        finding(1, "the opener's one call, NSWorkspace.shared.open(<a name>), is not here")
    for index in named[1:]:
        finding(line_of(text, index), "NSWorkspace a second time: the opener has one call")
    for match in re.finditer("NSWorkspace", code):
        if _OPEN_CALL.match(code, match.start()) is None:
            finding(
                line_of(code, match.start()),
                "NSWorkspace other than as NSWorkspace.shared.open(<a name>), its one call",
            )
    for pattern, why in _OPEN_ASKS:
        for match in pattern.finditer(code):
            finding(line_of(code, match.start()), f"{match[0]!r} {why}")
    # Each URL from a string: one literal, an entry of OPENABLE, and nothing else.
    scanner = _Scanner(code)
    used: dict[str, int] = {}
    made: set[int] = set()
    for match in _URL_FROM_STRING.finditer(code):
        line = line_of(code, match.start())
        made.add(match.start())
        at = match.end()
        hashes = 0
        while code.startswith("#", at + hashes):
            hashes += 1
        value: str | None = None
        if code.startswith('"', at + hashes):
            try:
                at = scanner.string(at, hashes)
                value = scanner.literals[-1][1]
            except SwiftSyntaxError:
                value = None
        if value is None or _CLOSE_CALL.match(code, at) is None:
            finding(line, "a URL made from something other than one string literal")
        elif value not in OPENABLE:
            finding(line, f"{value[:80]!r} is not a place the opener may open (OPENABLE)")
        else:
            used[value] = used.get(value, 0) + 1
    for entry in sorted(OPENABLE):
        if used.get(entry, 0) != 1:
            count = used.get(entry, 0)
            finding(1, f"{entry!r} must be opened by exactly one URL here, not {count}")
    # The Applications folder, once; no other URL.
    folder = list(_APPLICATIONS_URL.finditer(code))
    made |= {match.start() for match in folder}
    if len(folder) != 1 or code.count("fileURLWithPath") != 1:
        finding(1, f'URL(fileURLWithPath: "{APPLICATIONS_FOLDER}", isDirectory: true) once, alone')
    for match in _ANY_URL.finditer(code):
        if match.start() not in made:
            finding(line_of(code, match.start()), "a URL the opener may not make")
    # No string but the places'.
    for line, literal in scan.literals:
        if literal not in OPENABLE and literal != APPLICATIONS_FOLDER:
            finding(line, f"{literal[:80]!r} is a string the opener has no use for")
    # The code is the design's, token for token, and its strings are the design's, in order: so
    # each place opens its own address, and nothing else reaches the call.
    tokens = design_tokens(code)
    designed = [token for _, token in design_tokens(OPEN_DESIGN)]
    for index, (line, token) in enumerate(tokens):
        if index >= len(designed) or token != designed[index]:
            wanted = repr(designed[index]) if index < len(designed) else "nothing more"
            finding(line, f"{token!r} where the opener's design (OPEN_DESIGN) has {wanted}")
            break
    else:
        if len(tokens) < len(designed):
            finding(
                tokens[-1][0] if tokens else 1,
                f"the opener ends where its design (OPEN_DESIGN) goes on with "
                f"{designed[len(tokens)]!r}",
            )
    lexer = _Scanner(text)
    lexer.code_until(0, interpolation=False)
    strings = tuple(value for _, value in lexer.literals)
    if strings != OPEN_LITERALS:
        said = ", ".join(repr(value[:80]) for value in strings)
        finding(1, f"the opener's strings, in order, are not OPEN_LITERALS: ({said})")
    return found


def design_tokens(code: str) -> list[tuple[int, str]]:
    """The tokens of Swift ``code`` whose comments are blanked, each with its line: each one-line
    string literal is one ``""``, each name one token, and each other character but white space
    one. :data:`OPEN_DESIGN` is read the same way."""
    flat = _ONE_LINE_LITERAL.sub('""', code)
    return [(flat.count("\n", 0, match.start()) + 1, match[0]) for match in _TOKEN.finditer(flat)]


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
    if rel != OPEN_FILE:
        found += _regex_findings(_blanked(scan.code, spans), rel, "open", OPENER_NAMES)
    for line, value in scan.literals:
        if line in shebangs and value.startswith(TEST_SHEBANG + "\n"):
            value = " " * len(TEST_SHEBANG) + value[len(TEST_SHEBANG) :]
        found += _swift_rule_findings(value, rel, line)
        found += _regex_findings(value, rel, "swift-denylist", MARKDOWN_LINKS, line)
    if rel == OPEN_FILE:
        found += _opener_findings(text, scan, rel)
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
