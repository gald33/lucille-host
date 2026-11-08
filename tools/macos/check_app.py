"""Checks on the built Mac app. CI runs each one on a macOS runner after build_app.py.

    uv run --no-project python tools/macos/check_app.py CHECK [--out build/macos] [options]

Each check prints what it saw and exits 1 if anything is wrong (2 on a usage error, and off a Mac).
The app is ``<out>/<name>.app``; every check but ``dmg``, ``unpack-dmg``, ``handoff-check``,
``library-snapshot`` and ``places`` refuses to run without it. Only the copy at
``/Applications/<name>.app`` starts anything (decisions/0002 §13): ``install`` puts one there, and
``launch``, ``ui`` and ``pairing`` open that copy and refuse to run without it. The interpreter
always runs as the app runs it, ``lucille-host-runtime -I -B -X utf8``
(:data:`build_app.RUNTIME_FLAGS`), except the bytecode program, which adds ``-S`` and ``-W ignore``
(:data:`build_app.BYTECODE_FLAGS`).

The bundle as built:

- ``bundle``: no symbolic link in the bundle; its Mach-O files are exactly the expected five
  (:data:`RUNTIME_MACHO` and the app's executable); the app and every one of them is signed ad
  hoc by this build, not merely by the linker, with no Team ID and the identifier
  ``build_app.signing_identifier`` gave it (:func:`codesign_problems`).
- ``bytecode``: the bundle holds one checked-hash ``.pyc`` beside each ``.py`` of the runtime's
  library, and no other bytecode (``build_app.bytecode_layout_problems``); the bundled interpreter,
  running the build's own program, finds each one byte for byte what it compiles from its ``.py``
  now, with its magic number and its source's hash in the header, and that run ends as the program
  ends and counts at least one ``.py`` (``build_app.bytecode_check_problems``); and ``--version``,
  run as the app runs it with ``-v``, loads each of its modules in the bundle from its ``.pyc`` and
  compiles none from source (:func:`loaded_from`). It writes nothing beside the app, so a person can
  also run it on a copy they downloaded, on the mounted disk image (not yet tried on a Mac).
- ``record`` (M10): each wheel MANIFEST.json names, the host's among them, is found again and
  hashed; the installed distributions are exactly those; and every file in ``site-packages`` is
  byte for byte a file of its wheel's ``RECORD`` (``payload.record_problems``, the build's own
  check), apart from the ``.pyc`` files the build writes, which ``bytecode`` checks, and the
  Mach-O files, which codesign rewrites in place: those are compared after
  ``codesign --remove-signature`` on both copies, and that comparison is reported, not failed.
  A copy codesign could not strip is named, not compared.
- ``version`` (M11, with ``--tag`` or ``--build-number``): Info.plist's two version strings are
  the version rule's, and MANIFEST.json, the bundled host's ``--version`` and the pins agree.
- ``links``: every library a Mach-O file in the bundle loads is macOS's own, or resolves, the
  way dyld resolves ``@loader_path``, ``@executable_path`` and ``@rpath``, to a file that exists
  inside the bundle; every run path resolves inside the bundle (:func:`resolve_links`). It prints
  where each one resolves. No Mach-O file says it needs a newer macOS than
  ``LSMinimumSystemVersion``; no file in the bundle holds the build folder's path; and the app's
  own executable imports no function from :data:`FORBIDDEN_IMPORTS` and the lists after it, which
  start processes, open network connections, or open, register, drive or hand something to
  another application, and links none of :data:`FORBIDDEN_LIBRARIES`.
  That last part reads only what the executable imports by name: code that reaches such a
  function another way (through the Objective-C runtime, say) is not seen.
- ``signature``: ``codesign --verify --deep --strict`` passes on the app, and ``codesign --verify
  --strict`` on each Mach-O file in it; each is signed ad hoc as ``bundle`` says, with the
  hardened runtime exactly when the pins ask for it (and, when they do not, with no
  ``library-validation`` flag either); nothing carries entitlements. Then the same
  for the copy at ``/Applications/<name>.app``, once ``install`` has put it there. CI runs it again
  after every other check, so a run that wrote into either copy fails it.
- ``wrapper``: on a writable copy of the app, the Terminal wrapper ``Resources/bin/lucille-host``
  runs the host through a link to it from another folder (and never a runtime beside the link),
  on the app's profile, not on ``default``; neither it nor the interpreter run with the app's
  flags writes anything into the copy (:func:`file_listing` before and after). Every ``.pyc`` the
  build wrote is valid, so a run without ``-B`` would write none of them again: the copy's
  ``.pyc`` of ``lucille_host/__init__.py`` is removed first, so that each run compiles that module
  from source. Then, as the control, the same run without ``-B`` writes a ``.pyc`` there again,
  so the check can see a write.

On the bundle made read-only (CI runs ``chmod -R a-w`` first):

- ``host``: ``--version`` prints this checkout's version, through the wrapper too; ``ops list``
  works; a ``roots`` add, list and remove round trip works, and so does ``pause`` then ``resume``;
  a home folder is refused without ``--allow-home-or-root``; the dependencies' native code loads
  with the app's flags in force, under the hardened runtime only if the pins ask for it (ad hoc
  builds are signed without it, decisions/0002 §7). HOME is a new empty folder, and
  PYTHONHOME and PYTHONPATH point nowhere, which isolated mode must ignore.
- ``environment`` (M6): a sitecustomize, a usercustomize, a ``.pth`` file, a startup file and a
  shadowing ``lucille_host`` are planted where PYTHONPATH, PYTHONHOME, PYTHONSTARTUP, the user
  site, PYTHONUSERBASE and the working folder would load them. For each, a control run without
  ``-I`` must load what was planted (otherwise the check proves nothing), and the same run with
  the app's flags must load nothing. PYTHONSTARTUP is read only by an interactive interpreter, so
  no run the app makes (``-m lucille_host``) could load it even without ``-I``; its control runs
  ``-i``. With PYTHONPYCACHEPREFIX set, which would make the importer read ``.pyc`` files from
  another tree, the interpreter names the prefix without ``-I`` and none with the app's flags. Then
  ``--version``, ``status`` and the native probe run with every one of those set, from the planted
  folder, and load nothing; the probe asserts there is no pycache prefix. And no ``.pth`` file in
  ``site-packages`` is missing from the installed ``RECORD`` files (``record`` compares those with
  the wheels' own).
- ``readonly`` (M8): the bundle cannot be written; ``--version``, ``status`` and ``status
  --json`` work from it, and the wrapper too; no file in the bundle changed, and no ``.pyc``
  appeared or went; then ``codesign --verify --strict``, and ``--deep``, still pass.
- ``swift``: compiles the app's Swift files that do not draw anything with a test driver
  (``macos/Tests/SelfTest.swift``), or takes the one ``--selftest`` names, and runs it against
  the app. It makes the calls the menu makes, through the same code; reads the host's golden
  fixtures through the app's JSON reader and its pairing, supervision and Set Up state machines;
  and drives the child runner with fake children, a shell script in place of the interpreter.
- ``relocate``: copies the app to another folder, whose path has a space in it, and runs ``host``
  and ``swift`` on the copy.
- ``install [--replace | --restore]``: copies the read-only bundle to ``/Applications/<name>.app``
  with ``ditto``, makes the copy read-only, and runs ``readonly`` on it (M8). A copy of this build
  already there is only held to M8 again. It leaves any other copy there alone and fails, unless
  ``--replace``: that sets it aside in the home folder (:func:`set_aside_path`), says where, and
  installs. ``--restore`` puts back what was there before: it removes the installed copy and puts
  the one set aside back, or, with none set aside, removes the installed copy if it is this build.
  It never sets a copy aside over one set aside before. ``placement``, ``launch``, ``ui`` and
  ``pairing`` refuse to run unless the copy in ``/Applications`` is this build
  (:func:`is_this_build`), so a copy an earlier build left there never answers for this one.
- ``places [--record-only]``: the two System Settings pages the app's one opener may show
  (:data:`SETTINGS_PANES`, decisions/0002 §20), each opened by the check with ``/usr/bin/open``,
  not by the app: System Settings starts and shows a window with the page's title. It prints the
  names of System Settings' windows and takes a screenshot of each, ``places-<n>.png``, then quits
  System Settings by its name, sending it no key; it quits it before each page too, saying so if it
  was running before the check. It needs no app. ``--record-only``, which the
  smoke jobs pass, prints what it would have checked and fails nothing: the pages' addresses and
  titles are not yet seen on a Mac, and the first runs settle them.
- ``placement``: only the copy at ``/Applications/<name>.app`` starts anything. The control first:
  that copy, opened, starts the host on the app's profile, which the check sees, and says where its
  menu is (with no confirmed pair on the profile; with one, it says neither). Then, one at a time
  with no other copy running, the build's copy, and a copy named ``<name> 2.app`` in
  ``/Applications`` with the copy at the app's own name there and then moved aside: for 10 seconds
  each starts no host process on the app's profile and changes nothing in the profile's folder, and
  says why it can't run and how to fix that. The build's copy offers only Show Applications Folder
  and Quit, in either order, which the check prints as read, and Show Applications Folder quits it
  and shows a Finder window named Applications, whose name the check prints and, with
  ``--require-driven``, requires, and which it closes if it opened it; the renamed copy offers only
  Quit, and quits when Quit is pressed. A button is pressed
  only once the check has read it on the note. The renamed copy is removed and the other put back,
  whatever happens. Reading the notes needs System Events, as ``ui`` does (``--require-driven``
  makes not being able to a failure). Screenshots ``0-placement-*.png`` go to ``check/screens``.
- ``launch``: opens the copy in ``/Applications`` the way Finder does, checks that it is still
  running 10 seconds later, then stops it.
- ``ui``: opens the copy in ``/Applications`` and drives it as a user would, through System Events:
  reads the note that says what the app is for and where the menu is, when the app opens with no
  confirmed pair (with one, it shows none) and when it is opened again; reads the menu bar item's
  name and the menu; chooses Pause Lucille, then Resume Lucille, and reads the name again; switches
  an ability on and off; adds a folder through the Open panel and the name dialog, with a name the
  app must correct first, and removes it from the Folders submenu; chooses the home folder, which
  the app asks about first, picks Choose a Smaller Folder once, then adds it; chooses the Data
  volume, and then ``/`` through a link to it, which the app refuses each time with Choose a
  Smaller Folder and Cancel and no button that adds the folder, and adds neither. The Open panel's
  buttons are looked for in every window, sheet and group, since where they sit differs between
  macOS versions; where its prompt is nowhere, Return, its default button, chooses
  (:data:`CHOOSE_IN_PANEL`), and what the app shows next is checked as before. Then, with a
  code-shaped text on the pasteboard, it chooses Connect to Lucille…: the code window opens with
  its field empty, and the field stays empty once it has the focus, so the app read nothing from
  the pasteboard; a typed word gets the hint; Command-V, typed through System Events, pastes
  exactly what was copied; Connect hands it to the host, which refuses it as no code, and the app
  says so in its own words. Last, best effort and failing nothing, it adds a folder it makes in
  Documents, typing its path only once it has read the Open panel's button: if macOS asks, it
  prints the question and, where it names the app or its interpreter and is the one window of the
  process that draws it, answers Don't Allow, reads that the app says macOS is blocking it,
  presses Open Privacy Settings and prints what System Settings shows; if macOS does not ask, it
  says so; either way, each on its own, it presses the app's Cancel if the folder is still being
  checked, ends a ``roots add`` of the app's profile still running 30 seconds later, says when a
  question of macOS's is left on screen, removes the folder, quits System Settings if it started
  it, and asks ``tccutil reset`` to take back what macOS was asked. It ends, clean-up included,
  inside the ui step's limit, :data:`UI_STEP_LIMIT` (:func:`_drive_documents_prompt`). It checks
  each step against the host's own state, on the app's own profile (:data:`APP_PROFILE`). It uses
  the real HOME and leaves the host as it found it. Where macOS does not let System Events drive
  other apps, it says so and drives nothing, which is a failure with ``--require-driven`` (the
  macos-app job passes it; the smoke jobs record whether it could drive). Screenshots go to
  ``check/screens``.
- ``library-snapshot --to F`` and ``library-diff --before F`` (M12): ``~/Library`` is walked
  before ``launch`` and after ``ui``, and again before and after ``pairing`` (``Developer`` and
  ``Android`` skipped, no link followed). A
  new, changed or removed path whose name holds the bundle id, the app's name or "lucille" and
  that ``tools/macos/library-allowlist.txt`` does not allow is a failure; every other change is
  printed. The state folder of the app's profile must be among the changes, the control that the
  walk saw the host's writes at all; ``--control-run`` makes those writes itself (``pause``, then
  ``resume``, on the app's profile), for a job where ``ui`` may not have driven the app. A write
  to a path with an unrelated name is not caught. The snapshots are never uploaded.
- ``startup``: the median and fastest of ten runs of ``--version`` and ``status --json`` from the
  bundle as shipped, with its checked-hash ``.pyc`` files, then from a writable copy with every
  ``.pyc`` removed (decisions/0002 §6). It prints a table, and gates nothing but that both run.
- ``suite --copy-to DIR``: the host's whole test suite on a copy of the app's runtime: the dev
  and conformance dependencies are installed into the copy only, by hash; the host's wheel and
  agent-switchboard are installed again, by MANIFEST.json's sha256, so that their console
  scripts sit beside the copy's interpreter, as the suite needs; then pytest runs with it.
- ``pairing``: end to end, against a Switchboard hub it starts on a loopback port and a fake
  Lucille (``tests/_lucille.py``). It needs Switchboard's hub and the test harness, which it
  imports only when it runs: the macos-app job runs this script with the interpreter of the copy
  ``suite`` made, the app's runtime with the suite's dependencies and Switchboard's hub beside it
  (the smoke jobs have no such copy, and do not run it). It pairs the app's profile through the
  command line, as a test harness, with ``--hub`` and ``--store-file`` (which the app never
  passes), and leaves the SAS pending. Then it drives the app: the pairing window, opened from
  the menu, shows the number Lucille's side shows, Return does nothing there, and They Match
  confirms; the app starts ``run``, which reads the room Lucille offers; once the menu says
  Connected, the Set Up window opens by itself, with no Add back page (the app saw no
  ``policy_reset``, the harness having paired), on Folders, and its switch for ``files.sha256``
  turns it on and off again, through ``ops``; Ready's box "Open <name> when you log in" is
  checked, and Done closes the window and asks macOS to open the app at login: the app says on
  standard error which state macOS gave, and its words agree with it (what macOS answers an
  ad-hoc build is not known, so agreement is the gate, and ``sudo -n sfltool dumpbtm`` is printed,
  never gated); each later launch is paired, and shows no note about the menu bar, and says the
  login item's state, with Keep It Off chosen if the app says it won't start at login; a
  ``files.sha256``
  request on a folder added through the command line is answered; ``run`` killed with SIGKILL is
  started again after the back-off; ``run`` drains when the app itself is killed; the app's own
  Quit asks first, shows the finishing panel, and waits for ``run`` to drain; Stop What's Running
  stops a request and pauses the host; a request that needs a newer release brings one update
  notice, which no later request brings back, and Prepare to Update asks nothing, lets ``run``
  drain and quits;
  Stop now pauses the host, which the app never undoes on its own; Disconnect drains ``run``, then
  forgets the pair, takes the login item away and says so when there was one, and the menu's
  Settings goes. It drives the copy in ``/Applications``.
  The app's profile, if there was one, is set aside first and put back at the end. Screenshots go
  to ``check/screens``.
- ``gatekeeper``: prints what ``spctl`` says about the app and the disk image. It never fails: an
  ad hoc signature is refused until the app is signed with a Developer ID and notarized.
- ``dmg``: ``hdiutil verify`` passes, and the mounted image holds the app, with a valid signature,
  and a link to ``/Applications``.
- ``probe``: prints what the verbs the app is about to use do on this runner, as the app runs
  them. It gates nothing.

Moving the bytes between jobs (the smoke jobs build nothing):

- ``selftest --to F``: compiles the Swift self-test to ``F``, with the pinned Xcode.
- ``handoff``: copies the disk image beside the self-test in ``<out>/handoff`` and writes the
  cache key and both files' sha256 to ``--github-output`` (:func:`handoff_outputs`).
- ``handoff-check``: refuses unless ``<out>/handoff`` (or ``--from``) holds exactly that disk
  image and that self-test, by the sha256 the macos-app job gave.
- ``unpack-dmg``: copies the app out of that disk image into ``<out>``, and the image beside it.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import fnmatch
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

if __package__ in (None, ""):  # run as a script: make ``tools.macos`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.macos import build_app, payload  # noqa: E402

TESTS_DIR: Final = build_app.MACOS_DIR / "Tests"
ALIAS_VECTORS: Final = TESTS_DIR / "alias-vectors.json"
# The app's Swift files that use Foundation only, so a test driver can run them without a screen.
LOGIC_SOURCES: Final = (
    "Alias.swift",
    "HostCLI.swift",
    "HostText.swift",
    "HostJSON.swift",
    "Pairing.swift",
    "Supervisor.swift",
    "ConnectionText.swift",
    "RequestLog.swift",
    "Placement.swift",
    "Setup.swift",
    "LoginRules.swift",
    "PlaceRules.swift",
)
# The host's golden fixtures (spec/CLI.md section 11), which the self-test reads through the
# app's JSON reader and its pairing, supervision and Set Up state machines.
CLI_FIXTURES: Final = build_app.REPO_ROOT.joinpath(
    "src", "lucille_host", "spec", "cli", "v1", "fixtures"
)
# Where macOS keeps its own libraries. Most of them exist only in the system's shared cache, so
# the links check does not look for them on disk. Every other library must be in the bundle.
SYSTEM_LIBRARY_PREFIXES: Final = ("/usr/lib/", "/System/Library/")
# The load commands that name a library a Mach-O file loads.
LOAD_COMMANDS: Final = frozenset(
    {
        "LC_LOAD_DYLIB",
        "LC_LOAD_WEAK_DYLIB",
        "LC_REEXPORT_DYLIB",
        "LC_LAZY_LOAD_DYLIB",
        "LC_LOAD_UPWARD_DYLIB",
    }
)
# What the app's own executable must not import: functions that start a process or open a
# network connection, and the classes that do either; and Launch Services' and the Apple Event
# Manager's C calls, which open, register or drive another application. HostCLI.swift starts the
# interpreter with Foundation's Process (NSTask), which imports none of these names. NSWorkspace is
# not refused here: the app's one opener (SystemPlaces.swift) calls it, and macos_lint keeps it to
# that file and to one call that opens one of a closed list of places (decisions/0002 §20).
FORBIDDEN_IMPORTS: Final = frozenset(
    {
        "_fork",
        "_vfork",
        "_execl",
        "_execle",
        "_execlp",
        "_execv",
        "_execve",
        "_execvp",
        "_execvP",
        "_fexecve",
        "_system",
        "_popen",
        "_socket",
        "_socketpair",
        "_bind",
        "_listen",
        "_accept",
        "_connect",
        "_getaddrinfo",
        "_gethostbyname",
        "_dlopen",
        "_dlsym",
    }
)
FORBIDDEN_IMPORT_PREFIXES: Final = (
    "_posix_spawn",
    "_nw_",
    "_CFSocket",
    "_CFStream",
    "_CFReadStream",
    "_CFWriteStream",
    "_CFHTTP",
    "_CFHost",
    "_CFNetService",
    "_xpc_connection",
    "_LSOpen",
    "_LSSetDefault",
    "_LSRegister",
    "_AESend",
    "_OBJC_CLASS_$_NSURLSession",
    "_OBJC_CLASS_$_NSURLConnection",
    "_OBJC_CLASS_$_NSURLDownload",
    "_OBJC_CLASS_$_NSAppleScript",
    "_OBJC_CLASS_$_NSAppleEventDescriptor",
    "_OBJC_CLASS_$_NSSharingService",
    "_OBJC_CLASS_$_NSUserScriptTask",
    "_OBJC_CLASS_$_NSUserUnixTask",
    "_OBJC_CLASS_$_NSUserAppleScriptTask",
    "_OBJC_CLASS_$_NSNetService",
    "_OBJC_CLASS_$_NSStream",
    "_OBJC_CLASS_$_NSInputStream",
    "_OBJC_CLASS_$_NSOutputStream",
    "_OBJC_CLASS_$_NSSocketPort",
    "_OBJC_CLASS_$_NSXPCConnection",
    "_OBJC_CLASS_$_NSXPCListener",
    "_OBJC_CLASS_$_WKWebView",
    # The app never reads the pasteboard: the user pastes into a field, through the field.
    "_OBJC_CLASS_$_NSPasteboard",
)
# Swift names, as they appear inside a mangled symbol: Foundation's URLSession and URLRequest, and
# Network's connection and listener types.
FORBIDDEN_SWIFT_NAMES: Final = ("10URLSession", "10URLRequest", "12NWConnection", "10NWListener")
FORBIDDEN_LIBRARIES: Final = (
    "/Network.framework/",
    "/CFNetwork.framework/",
    "/NetworkExtension.framework/",
    "/WebKit.framework/",
)
# The words that the ui check looks for (tests/test_macos_build.py checks each against its source):
# each listed ability's name and what Lucille gets from it, the core's `summary` and `sends`
# (src/lucille_host/ops, spec/CLI.md section 8); and, as in App/HostText.swift and
# App/HostModel.swift, the note the app shows when it opens and the sentence in it on what the app
# is for (the same sentence the Info.plist gives macOS), the menu's line while paused, the menu bar
# item's name, and the Open panel's button. The app's name in them comes from tools/macos/pins.toml,
# which the app reads back from its Info.plist.
APP_NAME: Final = build_app.load_pins().app_name
ABILITY_TITLES: Final = {"files.sha256": "Check a file's fingerprint"}
ABILITY_DETAILS: Final = {
    "files.sha256": (
        "Lucille gets a code that identifies the file's exact contents, not the contents."
    )
}
WELCOME_TITLE: Final = f"{APP_NAME} is in your menu bar"
PURPOSE: Final = build_app.folder_usage(APP_NAME)
PAUSED_NOTE: Final = "Lucille's requests are turned away until you resume."
STATUS_ITEM_NAMES: Final = (APP_NAME, f"{APP_NAME}, paused")
OPEN_PROMPT: Final = f"Allow {APP_NAME} to Use This Folder"
# What the app says when the whole disk is chosen, and the only buttons that note has. The ui
# check chooses the two folders of the whole disk that the design names: the Data volume by its
# path, and ``/`` through a link to it, so that Go to Folder is given a path that names one
# folder, as everywhere else in the check.
WHOLE_DISK_TITLE: Final = f"{APP_NAME} can't use your whole disk"
WHOLE_DISK_TEXT: Final = "This folder holds every user's files on "
WHOLE_DISK_BUTTONS: Final = ("Choose a Smaller Folder", "Cancel")
WHOLE_DISK_FOLDERS: Final = (Path("/System/Volumes/Data"), Path("/"))
# The start of a button that would add the folder or allow it: Add Anyway, Add Folder, and the
# Open panel's own button.
ADDING_BUTTONS: Final = ("Add", "Allow")
# The profile the app runs every verb on (``HostCLI.appProfile``): its own folders, abilities,
# pause flag, Keychain item and run lock, apart from a command-line host's ``default``. The ui and
# pairing checks read and change the host's state on this profile, as the app does.
APP_PROFILE: Final = build_app.APP_PROFILE
# Loads the native code of the runtime dependencies, the way the host will; and checks that the
# app's flags leave --check-hash-based-pycs at default, so that the interpreter checks the source
# hash in each checked-hash .pyc's header against its .py when it imports it, and leave no pycache
# prefix, so that it reads .pyc files only beside their sources (0002 §6).
NATIVE_PROBE: Final = """\
import sys
import _imp
import _cffi_backend
import keyring.backends.macOS
import lucille_host._switchboard
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
X25519PrivateKey.generate().public_key()
assert sys.flags.isolated == 1, sys.flags
assert sys.flags.dont_write_bytecode == 1, sys.flags
assert sys.flags.utf8_mode == 1, sys.flags
assert _imp.check_hash_based_pycs == "default", _imp.check_hash_based_pycs
assert sys.pycache_prefix is None, sys.pycache_prefix
print("native code loaded from", sys.prefix)
"""
# The runs of the host that the environment check makes with every variable it plants set, as
# (what to call it, the arguments after the interpreter and its flags).
HOST_RUNS: Final = (
    ("--version", ("-m", "lucille_host", "--version")),
    ("status", ("-m", "lucille_host", "status")),
    ("the native probe", ("-c", NATIVE_PROBE)),
)
# Every Mach-O file the runtime holds, relative to it: the interpreter, the one extension the
# pruned runtime keeps (lib-dynload/_dbm), and the two the wheels bring (cffi's backend and
# cryptography's Rust extension). The app's own executable is the fifth Mach-O file of the bundle.
# A new one is a change to review, so the bundle check refuses it until it is listed here.
RUNTIME_MACHO: Final = (
    build_app.INTERPRETER,
    f"{build_app.STDLIB}/lib-dynload/_dbm.cpython-313-darwin.so",
    f"{build_app.SITE_PACKAGES}/_cffi_backend.cpython-313-darwin.so",
    f"{build_app.SITE_PACKAGES}/cryptography/hazmat/bindings/_rust.abi3.so",
)
# Under --out: what the macos-app job hands to the smoke jobs (the disk image and the compiled
# self-test), and where the ~/Library snapshots go, outside check/, which CI uploads.
HANDOFF_DIR: Final = "handoff"
SELFTEST_NAME: Final = "selftest"
# M12: the paths under ~/Library the app may change, the folders the walk skips (Xcode's and the
# Android SDK's, which the runner's own tools write), and the state folder of the app's profile,
# whose change shows that the walk saw the host's writes.
LIBRARY_ALLOWLIST: Final = build_app.TOOLS_DIR / "library-allowlist.txt"
LIBRARY_SKIPPED: Final = ("Developer", "Android")
LIBRARY_CONTROL: Final = f"Application Support/lucille-host/{APP_PROFILE}/state"
# The suite needs the host's console script and Switchboard's beside the interpreter
# (tests/test_cli.py, tests/test_run_process.py); the app ships no console script.
SUITE_SCRIPTS: Final = ("lucille-host", "switchboard")
SWITCHBOARD_DISTRIBUTION: Final = "agent-switchboard"
# Start-up time: what is timed, and how many times after one warm-up run.
STARTUP_VERBS: Final = (("--version", ("--version",)), ("status --json", ("status", "--json")))
STARTUP_RUNS: Final = 10

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUN_NUMBER = re.compile(r"[1-9][0-9]{0,19}")
_CACHE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,200}")
_WHEEL_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.whl")
_CODE_FLAGS = re.compile(r"\bflags=0x[0-9a-f]+\(([^)]*)\)")


class CheckError(ValueError):
    """A check cannot go on: what it was given or found is not what it needs."""


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
    return app / build_app.RUNTIME_IN_APP


def _shown(done: subprocess.CompletedProcess[str]) -> str:
    return f"exit {done.returncode}\nstdout: {done.stdout!r}\nstderr: {done.stderr!r}"


def host_argv(python: Path, args: Sequence[str]) -> list[str]:
    """The command line that runs the host's CLI as the app runs it: the interpreter with the
    app's flags (``-I -B -X utf8``), ``-m lucille_host``, then ``args``."""
    return [str(python), *build_app.RUNTIME_FLAGS, "-m", "lucille_host", *args]


def _home_env(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": str(home)}


def _host(python: Path, home: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the host's CLI as the app does, with a fixed environment."""
    env = {
        **_home_env(home),
        "PYTHONHOME": str(home / "no-such-python-home"),
        "PYTHONPATH": str(home / "no-such-python-path"),
    }
    argv = host_argv(python, args)
    return subprocess.run(
        argv,
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
            [str(python), *build_app.RUNTIME_FLAGS, "-c", NATIVE_PROBE],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(home)},
            cwd=home,
            timeout=120,
            check=False,
        )
        checker.expect(
            done.returncode == 0 and "native code loaded" in done.stdout,
            "cffi, cryptography, keyring's macOS backend and switchboard load, with -I -B -X utf8",
            _shown(done),
        )

        wrapper = app / build_app.WRAPPER_IN_APP
        done = subprocess.run(
            [str(wrapper), "--version"],
            capture_output=True,
            text=True,
            env=_home_env(home),
            cwd=home,
            timeout=120,
            check=False,
        )
        checker.expect(
            done.returncode == 0 and done.stdout == f"lucille-host {version}\n",
            f"the wrapper {build_app.WRAPPER_IN_APP} runs it too",
            _shown(done),
        )


def compile_selftest(exe: Path) -> bool:
    """Compile the Swift self-test (the app's Foundation-only files and ``SelfTest.swift``) to
    ``exe``, with whatever Xcode ``DEVELOPER_DIR`` names; whether it compiled."""
    pins = build_app.load_pins()
    exe.parent.mkdir(parents=True, exist_ok=True)
    sources = [str(build_app.SWIFT_DIR / name) for name in LOGIC_SOURCES]
    sources.append(str(TESTS_DIR / "SelfTest.swift"))
    built = subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-target",
            f"arm64-apple-macos{pins.deployment_target}",
            "-o",
            str(exe),
            *sources,
        ],
        check=False,
    )
    return built.returncode == 0 and exe.is_file()


def check_swift(app: Path, checker: Checker, work: Path, selftest: Path | None = None) -> None:
    """Run the Swift self-test against ``app``: ``selftest`` if given (compiled once, by the
    macos-app job, and run as it is by the smoke jobs), else one compiled now."""
    if selftest is None:
        exe = work / SELFTEST_NAME
        if not checker.expect(compile_selftest(exe), "the Swift self-test compiles"):
            return
    else:
        exe = selftest
        if not checker.expect(exe.is_file(), f"the compiled self-test {exe} is there"):
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
                "--fixtures",
                str(CLI_FIXTURES),
            ],
            # The pairing checks give status, pair status and run a minute each, in case macOS's
            # Keychain is slow to answer under the temporary HOME.
            timeout=600,
            check=False,
        )
    checker.expect(done.returncode == 0, "the Swift self-test passes")


def make_writable(root: Path) -> None:
    """Give the owner write permission on ``root`` and everything under it, links aside: a copy of
    the read-only bundle (``ditto`` keeps its modes) can then be removed."""
    if not root.is_symlink():
        root.chmod(root.stat().st_mode | stat.S_IWUSR)
    for folder, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            path = Path(folder) / name
            if not path.is_symlink():
                path.chmod(path.lstat().st_mode | stat.S_IWUSR)


def check_relocate(app: Path, checker: Checker, work: Path, selftest: Path | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="lucille-host-moved-") as tmp:
        try:
            moved = Path(tmp) / "Some Other Folder" / "Moved Copy.app"
            moved.parent.mkdir()
            subprocess.run(["ditto", str(app), str(moved)], check=True)
            print(f"copied the app to {moved}")
            check_host(moved, checker)
            check_swift(moved, checker, work, selftest)
        finally:
            make_writable(Path(tmp))


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def minimum_macos_versions(commands: Sequence[str]) -> list[str]:
    """The oldest macOS each slice of a Mach-O file says it runs on, from ``otool -l``'s lines:
    ``minos`` of each macOS ``LC_BUILD_VERSION``, or ``version`` of an ``LC_VERSION_MIN_MACOSX``."""
    found = []
    command = None
    platform = None
    for line in commands:
        words = line.split()
        if len(words) < 2:
            continue
        if words[0] == "cmd":
            command = words[1]
            platform = None
        elif command == "LC_BUILD_VERSION" and words[0] == "platform":
            platform = words[1]
        elif command == "LC_BUILD_VERSION" and words[0] == "minos" and platform in ("1", "MACOS"):
            found.append(words[1])
        elif command == "LC_VERSION_MIN_MACOSX" and words[0] == "version":
            found.append(words[1])
    return found


def newer_than(version: str, limit: str) -> bool:
    """Whether macOS ``version`` is later than ``limit``; an unreadable version counts as later."""
    try:
        theirs, ours = _version(version), _version(limit)
    except ValueError:
        return True
    width = max(len(theirs), len(ours))
    return theirs + (0,) * (width - len(theirs)) > ours + (0,) * (width - len(ours))


def forbidden_imports(symbols: Sequence[str]) -> list[str]:
    """The symbols in ``symbols`` (what ``nm -u`` prints) that the app's executable must not
    import."""
    return [
        symbol
        for symbol in symbols
        if symbol in FORBIDDEN_IMPORTS
        or symbol.startswith(FORBIDDEN_IMPORT_PREFIXES)
        or any(name in symbol for name in FORBIDDEN_SWIFT_NAMES)
        or ("10contentsOf" in symbol and "3URLV" in symbol)
    ]


def _otool(flag: str, path: Path) -> list[str]:
    return subprocess.run(
        ["otool", flag, str(path)], capture_output=True, text=True, check=True
    ).stdout.splitlines()


def load_commands(commands: Sequence[str]) -> tuple[list[str], list[str]]:
    """What a Mach-O file loads, from ``otool -l``'s lines: the libraries its load commands name
    (:data:`LOAD_COMMANDS`), and its run paths (``LC_RPATH``), each in order and once. Its own
    install name (``LC_ID_DYLIB``) and dyld's (``LC_LOAD_DYLINKER``) are not libraries it loads."""
    libraries: list[str] = []
    rpaths: list[str] = []
    command = None
    for line in commands:
        key, _, value = line.strip().partition(" ")
        value = value.strip()
        if key == "cmd":
            command = value
            continue
        if key == "name" and command in LOAD_COMMANDS:
            found = libraries
        elif key == "path" and command == "LC_RPATH":
            found = rpaths
        else:
            continue
        name = value.rsplit(" (offset ", 1)[0]
        if name not in found:
            found.append(name)
    return libraries, rpaths


def _expand(path: str, loader: Path, executable: Path) -> Path | None:
    """``path`` with a leading ``@loader_path`` or ``@executable_path`` replaced, and ``..``
    resolved, as dyld reads it; None for any other form."""
    for token, folder in (("@loader_path", loader.parent), ("@executable_path", executable.parent)):
        if path == token or path.startswith(token + "/"):
            return Path(os.path.normpath(f"{folder}{path[len(token) :]}"))
    return None


def _shown_in(app: Path, path: Path) -> str:
    return str(path.relative_to(app)) if path.is_relative_to(app) else str(path)


def resolve_links(
    app: Path,
    file: Path,
    executable: Path,
    names: tuple[Sequence[str], Sequence[str]],
    executable_rpaths: Sequence[str] = (),
    is_file: Callable[[Path], bool] = Path.is_file,
) -> tuple[list[str], list[str]]:
    """Where each library ``file`` loads would come from, and what is wrong with any of them.

    ``names`` is what :func:`load_commands` read from ``file``, and ``executable_rpaths`` the run
    paths of ``executable``, the program that loads it (the app's own executable, or the
    interpreter). As dyld does, ``@loader_path`` is ``file``'s folder, ``@executable_path`` is
    ``executable``'s, and ``@rpath`` tries ``file``'s run paths, then ``executable``'s. A library
    from macOS (:data:`SYSTEM_LIBRARY_PREFIXES`) is not looked for on disk. Any other library must
    resolve to a file that exists inside ``app``, and each of ``file``'s run paths to a folder
    inside it: a ``..`` that leaves the bundle, an absolute path anywhere else, and a library the
    build removed are each a problem. dyld would also try the run paths of whatever loaded
    ``file``; this does not, so it can refuse, but never pass, a library only those would find.

    Returns one line for each library and run path, saying where it resolves, and the problems.
    """
    libraries, rpaths = names
    shown: list[str] = []
    problems: list[str] = []
    search: list[Path] = []
    for rpath in rpaths:
        where = _expand(rpath, file, executable)
        if where is None:
            problems.append(f"run path {rpath}: not a path inside the app")
        elif not where.is_relative_to(app):
            problems.append(f"run path {rpath}: {where} is outside the app")
        else:
            search.append(where)
            shown.append(f"run path {rpath} -> {_shown_in(app, where)}")
    for rpath in executable_rpaths:
        where = _expand(rpath, executable, executable)
        if where is not None and where.is_relative_to(app) and where not in search:
            search.append(where)
    for name in libraries:
        if name.startswith(SYSTEM_LIBRARY_PREFIXES):
            shown.append(f"{name} (macOS)")
            continue
        if name.startswith("@rpath/"):
            if not search:
                problems.append(f"{name}: no run path inside the app to look in")
                continue
            rest = name.removeprefix("@rpath/")
            candidates = [Path(os.path.normpath(folder / rest)) for folder in search]
        else:
            where = _expand(name, file, executable)
            candidates = [] if where is None else [where]
        outside = [path for path in candidates if not path.is_relative_to(app)]
        found = next((path for path in candidates if is_file(path)), None)
        if not candidates:
            problems.append(f"{name}: not from macOS, and not a path inside the app")
        elif outside:
            problems.append(f"{name}: {outside[0]} is outside the app")
        elif found is None:
            tried = ", ".join(_shown_in(app, path) for path in candidates)
            problems.append(f"{name}: no such file in the app (tried {tried})")
        else:
            shown.append(f"{name} -> {_shown_in(app, found)}")
    return shown, problems


def check_imports(executable: Path, checker: Checker) -> None:
    """The app's own executable imports nothing that starts a process or opens a connection."""
    done = subprocess.run(
        ["nm", "-u", "-j", str(executable)], capture_output=True, text=True, check=False
    )
    symbols = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    checker.expect(
        done.returncode == 0 and bool(symbols),
        f"nm lists the {len(symbols)} symbols the app's executable imports",
        _shown(done),
    )
    print("\n".join(symbols))
    bad = forbidden_imports(symbols)
    checker.expect(
        not bad,
        "the app's executable imports nothing that starts a process or opens a connection",
        "\n".join(bad),
    )
    libraries = [
        name
        for name in load_commands(_otool("-l", executable))[0]
        if any(lib in name for lib in FORBIDDEN_LIBRARIES)
    ]
    checker.expect(
        not libraries,
        "the app's executable links no networking or web framework",
        "\n".join(libraries),
    )


def check_links(app: Path, checker: Checker) -> None:
    build_dir = str(build_app.REPO_ROOT)
    minimum = build_app.load_pins().deployment_target
    main = app / "Contents" / "MacOS" / build_app.load_pins().app_name
    python = python_in(app)
    files = build_app.macho_files(app)
    checker.expect(bool(files), f"{len(files)} Mach-O files in the bundle")
    commands = {path: _otool("-l", path) for path in files}
    for path in files:
        versions = minimum_macos_versions(commands[path])
        checker.expect(
            bool(versions) and not any(newer_than(version, minimum) for version in versions),
            f"{path.relative_to(app)} runs on macOS {minimum} (it asks for {versions})",
        )
        # The app's own executable runs alone; everything else is loaded by the interpreter.
        executable = main if path.parent == main.parent else python
        names = load_commands(commands[path])
        executable_rpaths = load_commands(commands.get(executable, []))[1]
        shown, problems = resolve_links(app, path, executable, names, executable_rpaths)
        problems += [
            f"{name}: names the build folder"
            for name in [*names[0], *names[1]]
            if build_dir in name
        ]
        print(f"{path.relative_to(app)} loads:")
        for line in shown:
            print(f"  {line}")
        checker.expect(
            not problems,
            f"{path.relative_to(app)} loads only from macOS and the bundle",
            "\n".join(problems),
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
    check_imports(app / "Contents" / "MacOS" / build_app.load_pins().app_name, checker)


def _codesign_display(path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["codesign", "--display", *extra, str(path)], capture_output=True, text=True, check=False
    )


def codesign_problems(text: str, *, identifier: str, hardened: bool | None) -> list[str]:
    """What is wrong with one file's signature, from what ``codesign --display --verbose=2``
    printed: it must be signed ad hoc (``Signature=adhoc``, and ``adhoc`` among the
    CodeDirectory flags) by the build itself, not only by the linker (a wheel's extension and
    swiftc's output arrive ``linker-signed``, which also reads as ad hoc); have no Team ID; and
    carry ``identifier``. With ``hardened`` True or False, the hardened runtime flag must be there
    or must not be; with False, the flag codesign names ``library-validation`` must not be there
    either (decisions/0002 §7). With None, neither is looked at. Only whole lines that start with
    a key are read, so a path that holds one in the ``Executable=`` line is not taken for it."""
    lines = text.splitlines()

    def value(key: str) -> str | None:
        found = [line[len(key) + 1 :] for line in lines if line.startswith(key + "=")]
        return found[0] if len(found) == 1 else None

    problems = []
    if value("Signature") != "adhoc":
        problems.append("it is not signed ad hoc: there is no Signature=adhoc line")
    team = value("TeamIdentifier")
    if team != "not set":
        problems.append(f"its TeamIdentifier is {team!r}, not 'not set': a Team ID is not ad hoc")
    named = value("Identifier")
    if named != identifier:
        problems.append(f"its signing identifier is {named!r}, not {identifier!r}")
    directory = [line for line in lines if line.startswith("CodeDirectory ")]
    found = _CODE_FLAGS.search(directory[0]) if len(directory) == 1 else None
    if found is None:
        problems.append("codesign shows no CodeDirectory flags")
        return problems
    flags = {flag.strip() for flag in found.group(1).split(",")}
    if "adhoc" not in flags:
        problems.append(f"its flags are {sorted(flags)}, without adhoc: it is not signed ad hoc")
    if "linker-signed" in flags:
        problems.append("it carries only the linker's signature: the build did not sign it")
    if hardened is True and "runtime" not in flags:
        problems.append("it lacks the hardened runtime, which the pins ask for")
    if hardened is False and "runtime" in flags:
        problems.append("it carries the hardened runtime, which the pins do not ask for")
    if hardened is False and "library-validation" in flags:
        problems.append("it carries library validation, which the pins do not ask for")
    return problems


def expected_macho_files(pins: build_app.Pins) -> frozenset[str]:
    """The Mach-O files the bundle holds, relative to it: the app's executable and
    :data:`RUNTIME_MACHO`."""
    runtime = {f"{build_app.RUNTIME_DIR}/{rel}" for rel in RUNTIME_MACHO}
    return frozenset({f"Contents/MacOS/{pins.app_name}", *runtime})


def macho_set_problems(found: Iterable[str], pins: build_app.Pins) -> list[str]:
    """How the Mach-O files found in the bundle (relative paths) differ from the expected ones."""
    found = set(found)
    expected = expected_macho_files(pins)
    problems = [] if found else ["the bundle holds no Mach-O file"]
    problems += [f"{rel} is not a Mach-O file in the bundle" for rel in sorted(expected - found)]
    problems += [
        f"{rel} is a Mach-O file nobody expected: review it, then list it in RUNTIME_MACHO"
        for rel in sorted(found - expected)
    ]
    return problems


def expected_identifier(app: Path, path: Path, pins: build_app.Pins) -> str:
    """The signing identifier ``path`` must carry: the bundle identifier for the app and its
    executable, which the bundle's signature covers; what build_app gave any other file."""
    if path in (app, app / "Contents" / "MacOS" / pins.app_name):
        return pins.bundle_id
    return build_app.signing_identifier(app, path, pins)


def _signed_files(app: Path, pins: build_app.Pins) -> list[Path]:
    """The app and every Mach-O file in it but its executable, which the app's signature covers."""
    main = app / "Contents" / "MacOS" / pins.app_name
    return [app, *(path for path in build_app.macho_files(app) if path != main)]


def check_bundle(app: Path, checker: Checker) -> None:
    pins = build_app.load_pins()
    links = build_app.symlinks_in(app)
    checker.expect(not links, "no symbolic link in the bundle", "\n".join(map(str, links)))
    found = [path.relative_to(app).as_posix() for path in build_app.macho_files(app)]
    problems = macho_set_problems(found, pins)
    checker.expect(
        not problems,
        f"the bundle's {len(found)} Mach-O files are exactly the expected ones",
        "\n".join(problems),
    )
    for path in _signed_files(app, pins):
        done = _codesign_display(path, "--verbose=2")
        identifier = expected_identifier(app, path, pins)
        problems = codesign_problems(done.stderr, identifier=identifier, hardened=None)
        checker.expect(
            done.returncode == 0 and not problems,
            f"{path.relative_to(app.parent)}: signed ad hoc by this build as {identifier}, "
            "with no Team ID",
            "\n".join([*problems, _shown(done)]),
        )


# The lines ``python -v`` writes when the importer has a module's code: from a ``.pyc``, whose
# path it quotes (``repr``), or compiled from source, whose path it does not (importlib's
# ``SourceLoader.get_code``).
_CODE_FROM: Final = "# code object from "


def loaded_from(text: str, root: Path) -> tuple[list[str], list[str]]:
    """``(from .pyc, from source)``: the paths under ``root`` that ``python -v`` output ``text``
    says each module's code came from, in order, as it printed them."""
    from_pyc: list[str] = []
    from_source: list[str] = []
    for line in text.splitlines():
        if not line.startswith(_CODE_FROM):
            continue
        path = line.removeprefix(_CODE_FROM)
        quoted = len(path) > 1 and path[0] in "'\"" and path[-1] == path[0]
        if quoted:
            path = path[1:-1]
        if Path(path).is_relative_to(root):
            (from_pyc if quoted else from_source).append(path)
    return from_pyc, from_source


def check_bytecode(app: Path, checker: Checker) -> None:
    """The bundle's ``.pyc`` files: where the build writes them, and checked-hash; byte for byte
    what the bundled interpreter compiles from each ``.py``; and read in place of the sources when
    the host runs as the app runs it. It writes nothing beside the app."""
    runtime = app / build_app.RUNTIME_DIR
    library = runtime / build_app.STDLIB
    tag = build_app.BYTECODE_TAG
    sources = [path for path in library.rglob("*.py") if path.parent.name != "__pycache__"]
    layout = build_app.bytecode_layout_problems(app, tag)
    checker.expect(
        bool(sources) and not layout,
        f"one checked-hash {tag} .pyc beside each of the {len(sources)} .py files of the "
        "runtime's library, and no other bytecode in the bundle",
        "\n".join(layout[:100]),
    )
    done = build_app.run_bytecode_program(runtime, "check")
    lines = done.stdout.splitlines()
    print(lines[-1] if lines else _shown(done), flush=True)
    found = build_app.bytecode_check_problems(done, tag)
    checker.expect(
        not found,
        "each .pyc has the bundled interpreter's magic number and its .py's source hash, and is "
        "byte for byte what the bundled interpreter compiles from that .py",
        "\n".join([*found[:100], *done.stderr.strip().splitlines()[-20:]]),
    )
    # The effect: -B stops writes, not reads, so the host as the app runs it loads its modules
    # from these files. -v says where each module's code came from.
    argv = [str(python_in(app)), "-v", *build_app.RUNTIME_FLAGS, "-m", "lucille_host", "--version"]
    with tempfile.TemporaryDirectory(prefix="lucille-host-bytecode-") as tmp:
        done = _run_in(argv, Path(tmp))
    from_pyc, from_source = loaded_from(done.stderr, runtime)
    checker.expect(
        done.returncode == 0 and bool(from_pyc) and not from_source,
        f"--version, run as the app runs it, loads {len(from_pyc)} of the bundle's modules from "
        "their .pyc, and compiles none from source",
        "\n".join(from_source[:50]) or f"exit {done.returncode}\n{done.stderr[-4000:]}",
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
    pins = build_app.load_pins()
    hardened = pins.adhoc_hardened_runtime
    for path in _signed_files(app, pins)[1:]:
        done = subprocess.run(
            ["codesign", "--verify", "--strict", "--verbose=2", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        checker.expect(
            done.returncode == 0,
            f"{path.relative_to(app.parent)} has a valid signature of its own",
            _shown(done),
        )
    for path in _signed_files(app, pins):
        done = _codesign_display(path, "--verbose=2")
        named = [line for line in done.stderr.splitlines() if line.startswith("Identifier=")]
        print(f"{path.relative_to(app.parent)}: {named[0] if named else 'no Identifier line'}")
        identifier = expected_identifier(app, path, pins)
        problems = codesign_problems(done.stderr, identifier=identifier, hardened=hardened)
        checker.expect(
            done.returncode == 0 and not problems,
            f"{path.relative_to(app.parent)} is signed ad hoc as {identifier}, "
            f"{'with' if hardened else 'without'} the hardened runtime, as the pins say",
            "\n".join([*problems, _shown(done)]),
        )
    for path in _signed_files(app, pins):
        done = _codesign_display(path, "--entitlements", "-", "--xml")
        checker.expect(
            done.returncode == 0 and not done.stdout.strip(),
            f"{path.relative_to(app.parent)} has no entitlements",
            _shown(done),
        )
    print(_codesign_display(app, "--verbose=4").stderr, end="")


def _verify_strict(app: Path, checker: Checker) -> None:
    for extra in ([], ["--deep"]):
        argv = ["codesign", "--verify", *extra, "--strict", "--verbose=2", str(app)]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        checker.expect(done.returncode == 0, " ".join(argv[:-1]) + " passes", _shown(done))


# ---------------------------------------------------------------------------------------------
# Writing: the wrapper on a writable copy, and the read-only bundle (M8)


def _run_in(
    argv: list[str], home: Path, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=_home_env(home),
        cwd=cwd or home,
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )


def check_wrapper(app: Path, checker: Checker) -> None:
    """The wrapper and the app's flags write nothing into a writable copy of the app."""
    version = build_app.host_version()
    with tempfile.TemporaryDirectory(prefix="lucille-host-wrapper-") as tmp:
        root = Path(tmp)
        try:
            copy = root / "Writable Copy" / app.name
            copy.parent.mkdir()
            subprocess.run(["ditto", str(app), str(copy)], check=True)
            make_writable(copy)
            home = root / "home"
            home.mkdir()
            # Every .pyc the build wrote is valid, so the interpreter would write none of them
            # again, with -B or without it. One is removed first, so that each run below imports
            # that module from source, and would write a .pyc there if it ran without -B.
            package = copy / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES / "lucille_host"
            pyc = package / "__pycache__" / f"__init__.{build_app.BYTECODE_TAG}.pyc"
            pyc.unlink(missing_ok=True)
            before = file_listing(copy)
            # A link to the wrapper from a folder that could be on PATH, with a runtime beside
            # the link that must never run: the wrapper finds the bundle from its own real path.
            link = root / "bin" / "lucille-host"
            link.parent.mkdir()
            link.symlink_to(copy / build_app.WRAPPER_IN_APP)
            decoy = root / "runtime" / "bin" / "lucille-host-runtime"
            decoy.parent.mkdir(parents=True)
            marker = root / "decoy-ran"
            decoy.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 3\n", "ascii")
            decoy.chmod(0o755)
            for args in (["--version"], ["status"], ["ops", "list"]):
                done = _run_in([str(link), *args], home)
                ok = done.returncode == 0
                if args == ["--version"]:
                    ok = ok and done.stdout == f"lucille-host {version}\n"
                checker.expect(
                    ok,
                    f"the wrapper, through a link to it, runs `{shlex.join(args)}`",
                    _shown(done),
                )
            checker.expect(
                not marker.exists(), "it ran the bundle's runtime, not one beside the link"
            )
            # The wrapper runs on the app's profile, as the app does (0002 §9), never on the
            # default profile of a command-line install.
            hosts = home / "Library" / "Application Support" / "lucille-host"
            done = _run_in([str(link), "pause"], home)
            checker.expect(
                done.returncode == 0
                and (hosts / APP_PROFILE / "state" / "paused").is_file()
                and not (hosts / "default").exists(),
                f"the wrapper runs on the app's profile, {APP_PROFILE}, not on default",
                _shown(done),
            )
            done = _run_in(host_argv(copy / build_app.RUNTIME_IN_APP, ["status"]), home)
            checker.expect(
                done.returncode == 0,
                "the interpreter runs the host with the app's flags",
                _shown(done),
            )
            changed = listing_changes(before, file_listing(copy))
            checker.expect(
                not changed,
                "with -B, none of those runs changed anything in the writable copy, though each "
                "imported lucille_host from source",
                "\n".join(changed[:50]),
            )
            # The control: the same run without -B writes a .pyc there, so the check above could
            # have seen a write.
            pyc.unlink(missing_ok=True)
            before = file_listing(copy)
            argv = [
                str(copy / build_app.RUNTIME_IN_APP),
                "-I",
                "-X",
                "utf8",
                "-m",
                "lucille_host",
                "--version",
            ]
            done = _run_in(argv, home)
            changed = listing_changes(before, file_listing(copy))
            checker.expect(
                done.returncode == 0 and bool(changed),
                "without -B the same run writes into the copy, so the check above sees a write",
                "\n".join([*changed[:50], _shown(done)]),
            )
        finally:
            make_writable(root)


def file_listing(root: Path) -> dict[str, tuple[int, int, int]]:
    """``{path: (mode, size, mtime_ns)}`` of everything under ``root``, links not followed."""
    found = {}
    for folder, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            path = Path(folder) / name
            status = path.lstat()
            found[path.relative_to(root).as_posix()] = (
                status.st_mode,
                status.st_size,
                status.st_mtime_ns,
            )
    return found


def listing_changes(
    before: Mapping[str, tuple[int, int, int]], after: Mapping[str, tuple[int, int, int]]
) -> list[str]:
    """The paths of two :func:`file_listing` results that differ, or that are in one only,
    sorted."""
    return sorted(rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel))


def _json_lines(text: str) -> list[Any]:
    try:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        return []


def check_readonly(app: Path, checker: Checker) -> None:
    """M8: the bundle cannot be written, the host runs from it, and nothing in it changes."""
    before = file_listing(app)
    held = build_app.bytecode_in(app)
    for folder in (
        app / "Contents" / "Resources",
        app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES,
    ):
        probe = folder / ".lucille-host-write-probe"
        try:
            handle = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except PermissionError:
            checker.expect(True, f"{folder.relative_to(app.parent)} cannot be written")
        else:
            os.close(handle)
            probe.unlink()
            checker.expect(
                False,
                f"{folder.relative_to(app.parent)} cannot be written",
                "the bundle is writable: make it read-only first (chmod -R a-w)",
            )
    version = build_app.host_version()
    python = python_in(app)
    with tempfile.TemporaryDirectory(prefix="lucille-host-readonly-") as tmp:
        home = Path(tmp)
        done = _host(python, home, ["--version"])
        checker.expect(
            done.returncode == 0 and done.stdout == f"lucille-host {version}\n",
            "--version runs from the read-only bundle",
            _shown(done),
        )
        done = _host(python, home, ["status"])
        checker.expect(
            done.returncode == 0 and bool(done.stdout), "status runs from it", _shown(done)
        )
        print(done.stdout, end="")
        done = _host(python, home, ["status", "--json"])
        objects = _json_lines(done.stdout)
        checker.expect(
            done.returncode == 0
            and any(isinstance(item, dict) and item.get("type") == "status" for item in objects),
            "status --json runs from it, and prints a status object",
            _shown(done),
        )
        done = _run_in([str(app / build_app.WRAPPER_IN_APP), "--version"], home)
        checker.expect(
            done.returncode == 0 and done.stdout == f"lucille-host {version}\n",
            "so does the wrapper",
            _shown(done),
        )
    changed = listing_changes(before, file_listing(app))
    checker.expect(
        not changed, "nothing in the bundle changed during the runs", "\n".join(changed[:50])
    )
    checker.expect(
        build_app.bytecode_in(app) == held, "no .pyc appeared in the bundle or went from it"
    )
    _verify_strict(app, checker)


# ---------------------------------------------------------------------------------------------
# The environment (M6)


@dataclass(frozen=True)
class EnvironmentVector:
    """One way the environment could make the runtime load something else: the variables it
    sets, the files planted for it (path: text) and the links (path: target), the markers those
    files write when they load, the arguments after the interpreter that would load them without
    ``-I`` (the control), the working folder for its runs, and a note to print."""

    name: str
    env: Mapping[str, str]
    files: Mapping[Path, str]
    markers: tuple[str, ...]
    control: tuple[str, ...]
    links: Mapping[Path, Path] = field(default_factory=dict)
    cwd: Path | None = None
    note: str = ""


def _touch(markers: Path, name: str) -> str:
    """One line of Python that creates the marker ``name``."""
    return f"import pathlib; pathlib.Path({str(markers / name)!r}).touch()\n"


def environment_vectors(
    scratch: Path,
    *,
    stdlib: Path,
    home_user_site: Path,
    userbase: Path,
    userbase_user_site: Path,
) -> list[EnvironmentVector]:
    """The vectors M6 names, each planted under ``scratch`` (the user sites are under it too:
    HOME and PYTHONUSERBASE point there). ``stdlib`` is the runtime's ``lib/python3.X``: the
    fake PYTHONHOME links to every entry of it but ``site-packages``, so that an interpreter that
    honoured PYTHONHOME would start, and load the sitecustomize planted in its own
    ``site-packages``."""
    marks = scratch / "markers"
    pythonpath = scratch / "pythonpath"
    fake_stdlib = scratch / "pythonhome" / "lib" / stdlib.name
    links = (
        {
            fake_stdlib / entry.name: entry
            for entry in sorted(stdlib.iterdir())
            if entry.name != "site-packages"
        }
        if stdlib.is_dir()
        else {}
    )
    startup = scratch / "startup.py"
    working = scratch / "working-folder"
    shadow_main = "print('a planted lucille_host')\n"
    host = ("-m", "lucille_host", "--version")
    plain = ("-c", "pass")
    return [
        EnvironmentVector(
            name="PYTHONPATH",
            env={"PYTHONPATH": str(pythonpath)},
            files={
                pythonpath / "sitecustomize.py": _touch(marks, "pythonpath-sitecustomize"),
                pythonpath / "lucille_host" / "__init__.py": _touch(marks, "pythonpath-shadow"),
                pythonpath / "lucille_host" / "__main__.py": shadow_main,
            },
            markers=("pythonpath-sitecustomize", "pythonpath-shadow"),
            control=host,
        ),
        EnvironmentVector(
            name="PYTHONHOME",
            env={"PYTHONHOME": str(scratch / "pythonhome")},
            files={
                fake_stdlib / "site-packages" / "sitecustomize.py": _touch(
                    marks, "pythonhome-sitecustomize"
                )
            },
            links=links,
            markers=("pythonhome-sitecustomize",),
            control=plain,
        ),
        EnvironmentVector(
            name="PYTHONSTARTUP",
            env={"PYTHONSTARTUP": str(startup)},
            files={startup: _touch(marks, "pythonstartup")},
            markers=("pythonstartup",),
            control=("-i",),
            note=(
                "PYTHONSTARTUP is read only by an interactive interpreter, so no run the app "
                "makes (-m lucille_host) could load it, even without -I; its control runs -i"
            ),
        ),
        EnvironmentVector(
            name="the user site",
            env={},
            files={
                home_user_site / "usercustomize.py": _touch(marks, "usersite-usercustomize"),
                home_user_site / "lucille-planted.pth": _touch(marks, "usersite-pth"),
            },
            markers=("usersite-usercustomize", "usersite-pth"),
            control=plain,
        ),
        EnvironmentVector(
            name="PYTHONUSERBASE",
            env={"PYTHONUSERBASE": str(userbase)},
            files={
                userbase_user_site / "usercustomize.py": _touch(marks, "userbase-usercustomize"),
                userbase_user_site / "lucille-planted.pth": _touch(marks, "userbase-pth"),
            },
            markers=("userbase-usercustomize", "userbase-pth"),
            control=plain,
        ),
        EnvironmentVector(
            name="the working folder",
            env={},
            files={
                working / "lucille_host" / "__init__.py": _touch(marks, "working-folder-shadow"),
                working / "lucille_host" / "__main__.py": shadow_main,
            },
            markers=("working-folder-shadow",),
            control=host,
            cwd=working,
        ),
    ]


def _plant(vectors: Sequence[EnvironmentVector]) -> None:
    for vector in vectors:
        for path, text in vector.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for path, target in vector.links.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target, target_is_directory=target.is_dir())


def _fired(
    markers: Path, argv: list[str], env: Mapping[str, str], cwd: Path
) -> tuple[subprocess.CompletedProcess[str], set[str]]:
    """Run ``argv`` with no input, and return how it ended and the markers it created."""
    for marker in markers.iterdir():
        marker.unlink()
    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=dict(env),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    return done, {marker.name for marker in markers.iterdir()}


def _user_site(python: Path, env: Mapping[str, str], cwd: Path) -> Path | None:
    argv = [str(python), "-B", "-c", "import site; print(site.getusersitepackages())"]
    done = subprocess.run(
        argv, capture_output=True, text=True, env=dict(env), cwd=cwd, timeout=120, check=False
    )
    found = done.stdout.strip()
    return Path(found) if done.returncode == 0 and found else None


def environment_check(
    python: Path,
    stdlib: Path,
    scratch: Path,
    checker: Checker,
    host_runs: Sequence[tuple[str, Sequence[str]]],
) -> None:
    """M6 with ``python``, whose standard library is ``stdlib``, planting under ``scratch``. For
    each vector: without ``-I`` (with ``-B``, so nothing is written into the runtime) its control
    must load what was planted, and with the app's flags the same run must load nothing. Then
    PYTHONPYCACHEPREFIX, which would make the importer read ``.pyc`` files from another tree:
    without ``-I`` the interpreter must say it is in force, and with the app's flags that it is
    not. Last, each of ``host_runs`` runs with the app's flags, every one of those variables set
    and from the planted working folder, and must exit 0 and load nothing."""
    home = scratch / "home"
    markers = scratch / "markers"
    elsewhere = scratch / "elsewhere"
    userbase = scratch / "userbase"
    for folder in (home, markers, elsewhere):
        folder.mkdir(parents=True)
    base = _home_env(home)
    home_site = _user_site(python, base, elsewhere)
    userbase_site = _user_site(python, {**base, "PYTHONUSERBASE": str(userbase)}, elsewhere)
    if not checker.expect(
        home_site is not None
        and home_site.is_relative_to(home)
        and userbase_site is not None
        and userbase_site.is_relative_to(userbase),
        "the runtime names a user site under HOME, and one under PYTHONUSERBASE",
        f"{home_site}, {userbase_site}",
    ):
        return
    assert home_site is not None and userbase_site is not None
    vectors = environment_vectors(
        scratch,
        stdlib=stdlib,
        home_user_site=home_site,
        userbase=userbase,
        userbase_user_site=userbase_site,
    )
    _plant(vectors)
    flags = " ".join(build_app.RUNTIME_FLAGS)
    for vector in vectors:
        if vector.note:
            print(f"note: {vector.note}")
        env = {**base, **vector.env}
        cwd = vector.cwd or elsewhere
        done, fired = _fired(markers, [str(python), "-B", *vector.control], env, cwd)
        checker.expect(
            set(vector.markers) <= fired,
            f"{vector.name}: without -I, what was planted loads ({', '.join(vector.markers)}), "
            "so this check can see it",
            f"loaded: {sorted(fired)}\n{_shown(done)}",
        )
        done, fired = _fired(
            markers, [str(python), *build_app.RUNTIME_FLAGS, *vector.control], env, cwd
        )
        checker.expect(
            not fired,
            f"{vector.name}: with {flags}, nothing planted loads",
            f"loaded: {sorted(fired)}\n{_shown(done)}",
        )
    # No file need be planted for this one: the interpreter says which tree it reads .pyc files
    # from, and the importer reads sys.pycache_prefix for each one.
    prefix = {"PYTHONPYCACHEPREFIX": str(scratch / "pycache-prefix")}
    tell = ("-c", "import sys; print(sys.pycache_prefix)")
    for argv, said, what in (
        (
            [str(python), "-B", *tell],
            prefix["PYTHONPYCACHEPREFIX"],
            "without -I, the interpreter reads .pyc files from it, so this check can see it",
        ),
        (
            [str(python), *build_app.RUNTIME_FLAGS, *tell],
            "None",
            f"with {flags}, the interpreter reads no .pyc file from it",
        ),
    ):
        done, _ = _fired(markers, argv, {**base, **prefix}, elsewhere)
        checker.expect(
            done.returncode == 0 and done.stdout == said + "\n",
            f"PYTHONPYCACHEPREFIX: {what}",
            _shown(done),
        )
    every = {**base, **prefix}
    for vector in vectors:
        every.update(vector.env)
    working = next((vector.cwd for vector in vectors if vector.cwd is not None), elsewhere)
    for label, tail in host_runs:
        done, fired = _fired(
            markers, [str(python), *build_app.RUNTIME_FLAGS, *tail], every, working
        )
        checker.expect(
            done.returncode == 0 and not fired,
            f"{label}: with {flags}, every variable set and the planted folder as the working "
            "folder, it runs and loads nothing planted",
            f"loaded: {sorted(fired)}\n{_shown(done)}",
        )


def _record_rows(record: Path) -> list[list[str]]:
    with record.open(encoding="utf-8", newline="") as handle:
        return [row for row in csv.reader(handle) if row]


def pth_problems(site: Path) -> list[str]:
    """Each ``.pth`` file under ``site`` that no installed ``RECORD`` lists."""
    listed: set[str] = set()
    for info in payload.dist_infos(site):
        record = info / "RECORD"
        if record.is_file():
            listed.update(row[0] for row in _record_rows(record))
    found = sorted(path.relative_to(site).as_posix() for path in site.rglob("*.pth"))
    return [f"{rel} is in no RECORD" for rel in found if rel not in listed]


def check_environment(app: Path, checker: Checker) -> None:
    with tempfile.TemporaryDirectory(prefix="lucille-host-environment-") as tmp:
        environment_check(
            python_in(app),
            app / build_app.RUNTIME_DIR / build_app.STDLIB,
            Path(tmp),
            checker,
            HOST_RUNS,
        )
    problems = pth_problems(app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES)
    checker.expect(
        not problems,
        "no .pth file in site-packages that an installed RECORD does not list (the record "
        "check compares those RECORD files with the wheels' own)",
        "\n".join(problems),
    )


# ---------------------------------------------------------------------------------------------
# The wheels (M10) and the version (M11)


def reference_folders(out: Path) -> list[Path]:
    """Where the macos-app job keeps the wheels it installed: the build's download cache, and
    the host wheel ``build_app.py wheel`` built and ``assemble`` copied."""
    return [out / "cache" / "wheels", out / "host-wheel", out / "work" / "host-wheel"]


def find_reference_wheels(
    manifest: Mapping[str, Any], folders: Sequence[Path]
) -> tuple[dict[str, Path], list[str]]:
    """``({file name: path}, problems)``: each wheel MANIFEST.json names, the lock's and the
    host's, found in the first of ``folders`` that holds it, and hashed again. A wheel missing,
    a name that is not a plain file name, or a sha256 that is not the manifest's is a problem."""
    wheels = manifest.get("wheels") or []
    problems = [] if wheels else ["MANIFEST.json lists no wheel"]
    host = (manifest.get("host") or {}).get("wheel") or {}
    entries = [(wheel.get("file"), wheel.get("sha256")) for wheel in wheels]
    entries.append((host.get("file"), host.get("sha256")))
    found: dict[str, Path] = {}
    for name, digest in entries:
        if not isinstance(name, str) or _WHEEL_FILE.fullmatch(name) is None:
            problems.append(f"MANIFEST.json names a wheel {name!r}, which is not a file name")
            continue
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            problems.append(f"MANIFEST.json gives {name} the sha256 {digest!r}")
            continue
        path = next((folder / name for folder in folders if (folder / name).is_file()), None)
        if path is None:
            problems.append(f"{name}: in none of {', '.join(map(str, folders))}")
            continue
        actual = payload.sha256_of(path)
        if actual != digest:
            problems.append(f"{name}: its sha256 is {actual}, but MANIFEST.json says {digest}")
            continue
        found[name] = path
    return found, problems


def lock_wheel_problems(
    manifest: Mapping[str, Any],
    lock: Mapping[tuple[str, str], Sequence[payload.LockWheel]],
    *,
    host_wheel_sha256: str,
) -> list[str]:
    """How MANIFEST.json's wheels differ from what uv.lock pins, and its host wheel from the one
    the wheel step built. Each wheel must be one ``lock`` lists for that distribution and
    version, by file name, URL and sha256; the host wheel's sha256 must be
    ``host_wheel_sha256``. MANIFEST.json and the wheels beside it are written by the same build,
    so their agreeing with each other proves nothing about where the wheels came from."""
    problems = []
    for wheel in manifest.get("wheels") or []:
        name, version = payload.normal_name(str(wheel.get("name"))), str(wheel.get("version"))
        pinned = [(pin.filename, pin.url, pin.sha256) for pin in lock.get((name, version), ())]
        if (wheel.get("file"), wheel.get("url"), wheel.get("sha256")) not in pinned:
            problems.append(
                f"{name}=={version}: {wheel.get('file')} with the sha256 {wheel.get('sha256')} "
                "is not a wheel uv.lock pins"
            )
    host = ((manifest.get("host") or {}).get("wheel") or {}).get("sha256")
    if host != host_wheel_sha256:
        problems.append(
            f"MANIFEST.json's host wheel has the sha256 {host}; the wheel step built "
            f"{host_wheel_sha256}"
        )
    return problems


def distribution_set_problems(
    installed: Mapping[str, str], manifest: Mapping[str, Any]
) -> list[str]:
    """How the installed distributions ``{name: version}`` differ from MANIFEST.json's wheels
    and the host."""
    expected = {
        payload.normal_name(wheel["name"]): wheel["version"]
        for wheel in manifest.get("wheels") or []
    }
    problems = [] if expected else ["MANIFEST.json lists no wheel"]
    expected[payload.HOST_DISTRIBUTION] = (manifest.get("host") or {}).get("version")
    for name in sorted(set(expected) | set(installed)):
        if expected.get(name) != installed.get(name):
            problems.append(
                f"{name}: installed {installed.get(name)}, MANIFEST.json says {expected.get(name)}"
            )
    return problems


def split_record_problems(
    problems: Sequence[str], macho: Iterable[str]
) -> tuple[list[str], list[str]]:
    """``payload.record_problems``' findings on the signed bundle, split into those that fail
    and those only reported: that a Mach-O file in ``macho`` (paths relative to site-packages)
    does not match its ``RECORD`` row or its wheel's bytes, because codesign rewrote it in place.
    Everything else fails, a Mach-O file that no ``RECORD`` lists included."""
    names = set(macho)
    failing, signed = [], []
    for problem in problems:
        rel, differs, _ = problem.partition(" differs from the file in ")
        if not differs:
            rel, mismatched, rest = problem.partition(" does not match its RECORD row")
            differs = mismatched if not rest else ""
        (signed if differs and rel in names else failing).append(problem)
    return failing, signed


def _stripped_comparison(
    site: Path, macho: Sequence[str], wheels: Sequence[Path]
) -> list[tuple[str, str]]:
    """Each Mach-O file in ``site`` against the same file in its wheel, after ``codesign
    --remove-signature`` on a copy of each: SAME, or DIFFERENT with both sizes. Where codesign
    fails on either copy, the verdict says which, with its exit status and the first line it
    wrote, and nothing is compared: a copy that kept its signature would read as DIFFERENT."""
    holders: dict[str, Path] = {}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                holders.setdefault(name, wheel)
    results = []
    with tempfile.TemporaryDirectory(prefix="lucille-host-record-") as tmp:
        for number, rel in enumerate(macho):
            wheel = holders.get(rel)
            if wheel is None:
                results.append((rel, "in no wheel"))
                continue
            ours, theirs = Path(tmp) / f"{number}-bundle", Path(tmp) / f"{number}-wheel"
            shutil.copyfile(site / rel, ours)
            with zipfile.ZipFile(wheel) as archive:
                theirs.write_bytes(archive.read(rel))
            results.append((rel, _strip_and_compare(ours, theirs)))
    return results


def _strip_and_compare(ours: Path, theirs: Path) -> str:
    for whose, copy in (("bundle", ours), ("wheel", theirs)):
        done = subprocess.run(
            ["codesign", "--remove-signature", str(copy)], capture_output=True, check=False
        )
        if done.returncode != 0:
            said = (done.stderr or done.stdout).decode(errors="replace").strip().splitlines()
            return (
                f"not compared: `codesign --remove-signature` failed on the {whose}'s copy "
                f"(exit {done.returncode}: {said[0] if said else 'it said nothing'})"
            )
    a, b = ours.read_bytes(), theirs.read_bytes()
    if a == b:
        return "SAME"
    return (
        f"DIFFERENT (without signatures, the bundle's copy is {len(a)} bytes "
        f"and the wheel's {len(b)})"
    )


def _summarize(summary: Path | None, text: str) -> None:
    print(text)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def _manifest(app: Path) -> dict[str, Any]:
    return json.loads((app / build_app.MANIFEST_PATH).read_text(encoding="utf-8"))


def check_record(
    app: Path, out: Path, checker: Checker, summary: Path | None, *, host_wheel_sha256: str
) -> None:
    """M10 on the signed bundle (the build checked it byte for byte before signing): every wheel
    MANIFEST.json names is one uv.lock pins and the host wheel is the one the wheel step built
    (``host_wheel_sha256``); each is here, with its sha256; the installed distributions are
    exactly those; and each site-packages file is a file of its wheel, apart from the ``.pyc``
    files the build writes, admitted by their names (``bytecode`` checks what they hold)."""
    manifest = _manifest(app)
    lock = payload.parse_lock(build_app.LOCK_FILE.read_text(encoding="utf-8"))
    problems = lock_wheel_problems(manifest, lock, host_wheel_sha256=host_wheel_sha256)
    checker.expect(
        not problems,
        "MANIFEST.json's wheels are uv.lock's, and its host wheel is the one the wheel step built",
        "\n".join(problems),
    )
    wheels, problems = find_reference_wheels(manifest, reference_folders(out))
    if not checker.expect(
        not problems,
        f"each of the {len(wheels)} wheels MANIFEST.json names is here, with its sha256",
        "\n".join(problems),
    ):
        return
    site = app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES
    installed = payload.installed_versions(site)
    problems = distribution_set_problems(installed, manifest)
    checker.expect(
        not problems,
        f"the {len(installed)} installed distributions are exactly MANIFEST.json's wheels and "
        "lucille-host",
        "\n".join(problems),
    )
    macho = [path.relative_to(site).as_posix() for path in build_app.macho_files(site)]
    problems = payload.record_problems(
        site, list(wheels.values()), bytecode_tag=build_app.BYTECODE_TAG
    )
    failing, signed = split_record_problems(problems, macho)
    checker.expect(
        not failing,
        "every site-packages file is byte for byte a file of its wheel's RECORD, apart from the "
        "installer's named files, the .pyc files the build writes, and the Mach-O files signing "
        "rewrote (M10)",
        "\n".join(failing[:100]),
    )
    compared = _stripped_comparison(site, macho, list(wheels.values()))
    lines = [
        "### M10 after signing: each Mach-O file in site-packages against its wheel's",
        "",
        "Compared after `codesign --remove-signature` on a copy of each; a copy it could not "
        "strip is named, not compared. Reported, not a gate: the build compared every file byte "
        "for byte before signing.",
        "",
        "| file | without signatures |",
        "|---|---|",
        *(f"| `{rel}` | {verdict} |" for rel, verdict in compared),
        "",
        f"{len(signed)} findings of the RECORD check were about these signed files only.",
        "",
    ]
    _summarize(summary, "\n".join(lines))


def version_problems(
    plist: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    versions: build_app.BundleVersions,
    host_version: str,
    bundled_version: str,
    pins: build_app.Pins,
    tag: str | None,
    build_number: str | None,
) -> list[str]:
    """How the built app differs from the version rule (M11): Info.plist's two strings are
    ``versions``; MANIFEST.json gives the same app, the version inputs and the host's real
    version, which the bundled host prints; and a build from a tag has one version number."""
    problems = []

    def same(what: str, got: Any, want: Any) -> None:
        if got != want:
            problems.append(f"{what} is {got!r}, not {want!r}")

    same("CFBundleShortVersionString", plist.get("CFBundleShortVersionString"), versions.short)
    same("CFBundleVersion", plist.get("CFBundleVersion"), versions.build)
    same("CFBundleIdentifier", plist.get("CFBundleIdentifier"), pins.bundle_id)
    same("LSMinimumSystemVersion", plist.get("LSMinimumSystemVersion"), pins.deployment_target)
    same("the bundled host's --version", bundled_version, f"lucille-host {host_version}\n")
    same(
        "MANIFEST.json's app",
        manifest.get("app"),
        {
            "name": pins.app_name,
            "bundle_id": pins.bundle_id,
            "runtime_id": pins.runtime_id,
            "deployment_target": pins.deployment_target,
            "short_version": versions.short,
            "bundle_version": versions.build,
        },
    )
    build = manifest.get("build") or {}
    same("MANIFEST.json's build tag", build.get("tag"), tag)
    same("MANIFEST.json's build number", build.get("build_number"), build_number)
    same("MANIFEST.json's host version", (manifest.get("host") or {}).get("version"), host_version)
    if tag is not None and not versions.short == versions.build == host_version == tag[1:]:
        problems.append(f"a build from {tag} has more than one version number")
    return problems


def check_version(app: Path, checker: Checker, tag: str | None, build_number: str | None) -> None:
    pins = build_app.load_pins()
    version = build_app.host_version()
    versions = build_app.bundle_versions(version, tag, build_number)
    plist = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    with tempfile.TemporaryDirectory(prefix="lucille-host-version-") as tmp:
        done = _host(python_in(app), Path(tmp), ["--version"])
    problems = version_problems(
        plist,
        _manifest(app),
        versions=versions,
        host_version=version,
        bundled_version=done.stdout,
        pins=pins,
        tag=tag,
        build_number=build_number,
    )
    checker.expect(
        not problems,
        f"Info.plist says {versions.short} ({versions.build}), and MANIFEST.json and the bundled "
        f"host give the host's own version, {version} (M11)",
        "\n".join(problems),
    )


# ---------------------------------------------------------------------------------------------
# ~/Library (M12)


class AllowlistError(CheckError):
    """tools/macos/library-allowlist.txt is not well formed."""


@dataclass(frozen=True)
class AllowRule:
    """One line of the allowlist: a folder and everything in it (``prefix``), or a path matched
    one segment at a time; and why the app may change it."""

    pattern: str
    prefix: bool
    reason: str


def parse_library_allowlist(text: str, *, bundle_id: str, app_name: str) -> tuple[AllowRule, ...]:
    """The rules in ``text``: one per line, a path relative to ``~/Library``, then ``#`` and the
    reason, which every line must give. ``{bundle_id}`` and ``{app_name}`` are replaced. A line
    ending ``/**`` allows that one literal folder and everything under it; any other line is
    matched one path segment at a time with ``fnmatchcase``, so ``*`` never crosses a ``/``.
    Refused: no reason, an absolute path, an empty, ``.`` or ``..`` segment, a backslash, any
    other ``{...}`` or ``**``, a pattern in a folder rule, and a line given twice."""
    rules: list[AllowRule] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{LIBRARY_ALLOWLIST.name}:{number}: {line!r}"
        entry, sep, reason = line.partition(" #")
        entry, reason = entry.strip(), reason.strip()
        if not sep or not reason:
            raise AllowlistError(f"{where}: every line ends with '#' and the reason for it")
        entry = entry.replace("{bundle_id}", bundle_id).replace("{app_name}", app_name)
        if any(char in entry for char in "{}\\"):
            raise AllowlistError(f"{where}: only {{bundle_id}} and {{app_name}}, and no backslash")
        prefix = entry.endswith("/**")
        if prefix:
            entry = entry.removesuffix("/**")
        if entry.startswith("/") or any(part in ("", ".", "..") for part in entry.split("/")):
            raise AllowlistError(f"{where}: a path relative to ~/Library, with no . or ..")
        if "**" in entry:
            raise AllowlistError(f"{where}: ** only as the last segment of a folder rule")
        if prefix and any(char in entry for char in "*?["):
            raise AllowlistError(f"{where}: a folder rule names one literal folder")
        if any(rule.pattern == entry and rule.prefix == prefix for rule in rules):
            raise AllowlistError(f"{where}: given twice")
        rules.append(AllowRule(entry, prefix, reason))
    return tuple(rules)


def library_allowed(rel: str, rules: Sequence[AllowRule]) -> bool:
    """Whether a path relative to ``~/Library`` is one the rules allow."""
    parts = rel.split("/")
    for rule in rules:
        if rule.prefix:
            if rel == rule.pattern or rel.startswith(rule.pattern + "/"):
                return True
            continue
        segments = rule.pattern.split("/")
        if len(segments) == len(parts) and all(
            fnmatch.fnmatchcase(part, segment)
            for part, segment in zip(parts, segments, strict=True)
        ):
            return True
    return False


def library_snapshot(root: Path) -> dict[str, Any]:
    """Every path under ``root`` (``~/Library``) with its kind, size and modification time, and
    what could not be read. Links are recorded, not followed; ``Developer`` and ``Android`` at
    the top are skipped."""
    entries: dict[str, list[Any]] = {}
    unreadable: list[str] = []

    def failed(exc: OSError) -> None:
        unreadable.append(f"{exc.filename}: {exc.strerror}")

    for folder, dirs, files in os.walk(root, onerror=failed):
        here = Path(folder)
        if here == root:
            dirs[:] = [name for name in dirs if name not in LIBRARY_SKIPPED]
        for name in sorted((*dirs, *files)):
            path = here / name
            try:
                status = path.lstat()
            except OSError as exc:
                failed(exc)
                continue
            if stat.S_ISLNK(status.st_mode):
                kind = "link"
            elif stat.S_ISDIR(status.st_mode):
                kind = "dir"
            elif stat.S_ISREG(status.st_mode):
                kind = "file"
            else:
                kind = "other"
            size = status.st_size if kind == "file" else 0
            entries[path.relative_to(root).as_posix()] = [kind, size, status.st_mtime_ns]
    return {"root": str(root), "entries": entries, "unreadable": sorted(unreadable)}


def library_changes(
    before: Mapping[str, Sequence[Any]], after: Mapping[str, Sequence[Any]]
) -> list[tuple[str, str]]:
    """``(path, "new" | "removed" | "changed")`` for each path that differs, sorted."""
    changes = []
    for rel in sorted(set(before) | set(after)):
        if rel not in before:
            changes.append((rel, "new"))
        elif rel not in after:
            changes.append((rel, "removed"))
        elif list(before[rel]) != list(after[rel]):
            changes.append((rel, "changed"))
    return changes


@dataclass(frozen=True)
class LibraryVerdict:
    failures: list[str]
    allowed: list[str]
    noise: list[str]
    control_seen: bool


def library_problems(
    changes: Sequence[tuple[str, str]],
    rules: Sequence[AllowRule],
    *,
    bundle_id: str,
    app_name: str,
) -> LibraryVerdict:
    """Sort the changes: those the rules allow; those whose path names the bundle id, the app's
    name or "lucille" (in any case) and that no rule allows, which fail; and the rest, which are
    printed. ``control_seen`` says whether the host's state folder is among them."""
    needles = (bundle_id.lower(), app_name.lower(), "lucille")
    failures, allowed, noise = [], [], []
    for rel, what in changes:
        line = f"{what}: {rel}"
        if library_allowed(rel, rules):
            allowed.append(line)
        elif any(needle in rel.lower() for needle in needles):
            failures.append(line)
        else:
            noise.append(line)
    control = any(
        rel == LIBRARY_CONTROL or rel.startswith(LIBRARY_CONTROL + "/") for rel, _ in changes
    )
    return LibraryVerdict(failures, allowed, noise, control)


def _library() -> Path:
    return Path.home() / "Library"


def check_library_snapshot(to: Path, checker: Checker) -> None:
    snapshot = library_snapshot(_library())
    to.parent.mkdir(parents=True, exist_ok=True)
    to.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
    checker.expect(
        bool(snapshot["entries"]),
        f"{len(snapshot['entries'])} paths under {snapshot['root']} recorded in {to}",
        "\n".join(snapshot["unreadable"][:20]),
    )


def _control_writes(app: Path, checker: Checker) -> None:
    """Make the host write the state folder of the app's profile under the real HOME, as the ui
    check does when it drives Pause and Resume: ``pause`` then ``resume`` (or the other way round,
    if it is paused, so that it is left as it was), on :data:`APP_PROFILE`."""
    python = python_in(app)
    flag = _library() / LIBRARY_CONTROL / "paused"
    verbs = (["resume"], ["pause"]) if flag.exists() else (["pause"], ["resume"])
    for verb in verbs:
        done = _app_host(python, verb)
        checker.expect(
            done.returncode == 0, f"the control: the host runs `{verb[0]}`", _shown(done)
        )


def check_library_diff(app: Path, before_file: Path, control_run: bool, checker: Checker) -> None:
    pins = build_app.load_pins()
    rules = parse_library_allowlist(
        LIBRARY_ALLOWLIST.read_text(encoding="utf-8"),
        bundle_id=pins.bundle_id,
        app_name=pins.app_name,
    )
    before = json.loads(before_file.read_text(encoding="utf-8"))
    if control_run:
        _control_writes(app, checker)
    after = library_snapshot(_library())
    checker.expect(before.get("root") == after["root"], f"both snapshots are of {after['root']}")
    verdict = library_problems(
        library_changes(before.get("entries", {}), after["entries"]),
        rules,
        bundle_id=pins.bundle_id,
        app_name=pins.app_name,
    )
    for title, lines in (
        ("allowed", verdict.allowed),
        ("not the app's, by their names (printed only)", verdict.noise),
    ):
        print(f"{len(lines)} changes {title}:")
        for line in lines[:500]:
            print(f"  {line}")
    if after["unreadable"]:
        print("not readable, so not compared: " + "; ".join(after["unreadable"][:50]))
    checker.expect(
        verdict.control_seen,
        f"the walk saw the host's own writes to ~/Library/{LIBRARY_CONTROL} (the control)",
        "without them, an empty diff would prove nothing",
    )
    checker.expect(
        not verdict.failures,
        "nothing that names the app or the host changed under ~/Library outside "
        f"{LIBRARY_ALLOWLIST.name} (M12)",
        "\n".join(verdict.failures),
    )


# ---------------------------------------------------------------------------------------------
# The hand-off to the smoke jobs, and the disk image unpacked there


def handoff_key(app_name: str, run_id: str, run_attempt: str) -> str:
    """The cache key the macos-app job saves the disk image and the self-test under."""
    for what, value in (("run id", run_id), ("run attempt", run_attempt)):
        if _RUN_NUMBER.fullmatch(value) is None:
            raise CheckError(f"the {what} must be a positive whole number, not {value!r}")
    return f"{app_name.lower().replace(' ', '-')}-adhoc-macos-arm64-{run_id}-{run_attempt}"


def handoff_outputs(key: str, dmg_sha256: str, selftest_sha256: str) -> str:
    """The lines for ``$GITHUB_OUTPUT``: the cache key and the two files' sha256. Each value is
    checked whole, so nothing can add a line."""
    if _CACHE_KEY.fullmatch(key) is None:
        raise CheckError(f"{key!r} is not a cache key")
    for digest in (dmg_sha256, selftest_sha256):
        if _SHA256.fullmatch(digest) is None:
            raise CheckError(f"{digest!r} is not a sha256")
    return f"cache_key={key}\ndmg_sha256={dmg_sha256}\nselftest_sha256={selftest_sha256}\n"


def handoff_problems(
    folder: Path, app_name: str, dmg_sha256: str, selftest_sha256: str
) -> list[str]:
    """Why ``folder`` is not exactly what the macos-app job handed on: one disk image and the
    compiled self-test, nothing else, with the sha256 that job gave for each."""
    problems = []
    for what, digest in (("the disk image", dmg_sha256), ("the self-test", selftest_sha256)):
        if _SHA256.fullmatch(digest) is None:
            problems.append(f"the sha256 given for {what}, {digest!r}, is not one")
    if not folder.is_dir():
        return [*problems, f"{folder} is not there"]
    dmgs = build_app.disk_images(folder, app_name)
    if len(dmgs) != 1:
        problems.append(f"{folder} holds {len(dmgs)} disk images, not one")
    names = {entry.name for entry in folder.iterdir()}
    extra = sorted(names - {SELFTEST_NAME, *(dmg.name for dmg in dmgs)})
    if extra:
        problems.append(f"{folder} also holds {extra}")
    selftest = folder / SELFTEST_NAME
    files = [(selftest, selftest_sha256)] + ([(dmgs[0], dmg_sha256)] if len(dmgs) == 1 else [])
    for path, digest in files:
        if path.is_symlink() or not path.is_file():
            problems.append(f"{path.name} is not a file in {folder}")
            continue
        actual = payload.sha256_of(path)
        if actual != digest:
            problems.append(f"{path.name}: its sha256 is {actual}, not {digest}")
    return problems


def write_handoff(
    out: Path, run_id: str, run_attempt: str, github_output: Path, checker: Checker
) -> None:
    """Copy the disk image beside the compiled self-test in ``<out>/handoff``, and give the
    cache key and the sha256 of both to the job's later steps and the smoke jobs."""
    pins = build_app.load_pins()
    folder = out / HANDOFF_DIR
    dmgs = build_app.disk_images(out, pins.app_name)
    selftest = folder / SELFTEST_NAME
    if not checker.expect(len(dmgs) == 1, "the build made one disk image", f"{dmgs}"):
        return
    if not checker.expect(selftest.is_file(), f"the self-test is compiled at {selftest}"):
        return
    for old in build_app.disk_images(folder, pins.app_name):
        old.unlink()
    shutil.copyfile(dmgs[0], folder / dmgs[0].name)
    key = handoff_key(pins.app_name, run_id, run_attempt)
    dmg_sha256, selftest_sha256 = payload.sha256_of(dmgs[0]), payload.sha256_of(selftest)
    text = handoff_outputs(key, dmg_sha256, selftest_sha256)
    problems = handoff_problems(folder, pins.app_name, dmg_sha256, selftest_sha256)
    if not checker.expect(
        not problems, f"{folder} holds the disk image and the self-test", "\n".join(problems)
    ):
        return
    with github_output.open("a", encoding="utf-8") as handle:
        handle.write(text)
    print(text, end="")


def check_handoff(folder: Path, dmg_sha256: str, selftest_sha256: str, checker: Checker) -> None:
    problems = handoff_problems(folder, build_app.load_pins().app_name, dmg_sha256, selftest_sha256)
    checker.expect(
        not problems,
        f"{folder} holds exactly the disk image and the self-test the macos-app job built",
        "\n".join(problems),
    )


def unpack_dmg(out: Path, source: Path, checker: Checker) -> None:
    """Copy the app out of the one disk image in ``source`` into ``out``, and the image into
    ``out`` beside it, for the checks that follow."""
    pins = build_app.load_pins()
    dmgs = build_app.disk_images(source, pins.app_name)
    if not checker.expect(len(dmgs) == 1, f"{source} holds one disk image", f"{dmgs}"):
        return
    app = app_path(out)
    if not checker.expect(not app.exists(), f"there is no app at {app} yet"):
        return
    out.mkdir(parents=True, exist_ok=True)
    if source.resolve() != out.resolve():
        for old in build_app.disk_images(out, pins.app_name):
            old.unlink()
        shutil.copyfile(dmgs[0], out / dmgs[0].name)
    with tempfile.TemporaryDirectory(prefix="lucille-host-unpack-") as tmp:
        mount = Path(tmp) / "mount"
        mount.mkdir()
        done = build_app.hdiutil(
            [
                "attach",
                "-nobrowse",
                "-readonly",
                "-noautoopen",
                "-mountpoint",
                str(mount),
                str(dmgs[0]),
            ]
        )
        if not checker.expect(done.returncode == 0, "the image mounts", _shown(done)):
            return
        try:
            done = subprocess.run(
                ["ditto", str(mount / app.name), str(app)],
                capture_output=True,
                text=True,
                check=False,
            )
            checker.expect(
                done.returncode == 0 and app.is_dir(),
                f"{app.name} is copied out of it",
                _shown(done),
            )
        finally:
            for extra in ([], ["-force"]):
                if build_app.hdiutil(["detach", *extra, str(mount)]).returncode == 0:
                    break


# ---------------------------------------------------------------------------------------------
# Start-up time, and the suite on a copy of the runtime


def timing(samples: Sequence[float]) -> tuple[float, float]:
    """The median and the fastest of ``samples`` (seconds), in milliseconds."""
    return round(statistics.median(samples) * 1000, 1), round(min(samples) * 1000, 1)


def startup_table(results: Mapping[str, Mapping[str, tuple[float, float]]]) -> str:
    """Markdown: one row per state of the bytecode, the median and fastest of each verb."""
    head = " | ".join(f"{verb} median | {verb} fastest" for verb, _ in STARTUP_VERBS)
    lines = [
        f"### Start-up time of the bundled host, ms (median and fastest of {STARTUP_RUNS} runs "
        "after one warm-up)",
        "",
        f"| bytecode | {head} |",
        "|---|" + "---|" * (2 * len(STARTUP_VERBS)),
    ]
    for label, row in results.items():
        cells = [f"{row[verb][index]:.0f}" for verb, _ in STARTUP_VERBS for index in (0, 1)]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _timed(python: Path, home: Path) -> dict[str, tuple[float, float]] | None:
    row = {}
    for verb, args in STARTUP_VERBS:
        argv = host_argv(python, args)
        samples = []
        for run in range(STARTUP_RUNS + 1):
            started = time.perf_counter()
            done = _run_in(argv, home)
            elapsed = time.perf_counter() - started
            if done.returncode != 0:
                print(_shown(done))
                return None
            if run:
                samples.append(elapsed)
        row[verb] = timing(samples)
    return row


def check_startup(app: Path, work: Path, checker: Checker, summary: Path | None) -> None:
    """Time the host from the bundle as shipped, with the checked-hash .pyc files the build
    wrote; then from a writable copy with every .pyc removed. The runs keep -B, so the copy
    stays without them. Nothing is compiled here."""
    results: dict[str, dict[str, tuple[float, float]]] = {}
    with tempfile.TemporaryDirectory(prefix="lucille-host-startup-") as tmp:
        root = Path(tmp)
        try:
            home = root / "home"
            home.mkdir()
            row = _timed(python_in(app), home)
            if not checker.expect(row is not None, "the host runs from the bundle"):
                return
            results["checked-hash (as shipped)"] = row or {}
            copy = root / app.name
            subprocess.run(["ditto", str(app), str(copy)], check=True)
            make_writable(copy)
            for path in build_app.bytecode_in(copy / build_app.RUNTIME_DIR):
                if path.name == "__pycache__" and path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            left = build_app.bytecode_in(copy)
            if not checker.expect(not left, "the copy holds no .pyc", "\n".join(map(str, left))):
                return
            row = _timed(python_in(copy), home)
            if not checker.expect(row is not None, "the host runs from the copy with no .pyc"):
                return
            results["none (a copy with every .pyc removed)"] = row or {}
        finally:
            make_writable(root)
    work.mkdir(parents=True, exist_ok=True)
    (work / "startup.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    _summarize(summary, startup_table(results))


def suite_export_argv() -> list[str]:
    """``uv export`` of everything the host's test suite needs: the dev group and the
    conformance extra, with every hash. No uv configuration file is read."""
    return [
        "uv",
        "export",
        "--no-config",
        "--frozen",
        "--no-emit-project",
        "--group",
        "dev",
        "--extra",
        "conformance",
        "--format",
        "requirements-txt",
    ]


def suite_install_argv(
    python: Path,
    requirements: Path,
    *,
    find_links: Path | None = None,
    reinstall: Sequence[str] = (),
) -> list[str]:
    """``uv pip install`` into the runtime ``python`` (a copy of the app's), hashes required,
    wheels only, no dependencies beyond the list, no configuration file; with ``find_links``,
    from that folder alone. Wheels only: a pin with no wheel fails, rather than being built by a
    build backend nothing pins. The copy is not a virtual environment, and is the suite's own, so
    it is installed into even if it were marked as managed by something else (the pinned runtime
    is not)."""
    argv = ["uv", "pip", "install", "--python", str(python), "--require-hashes", "--no-deps"]
    argv += ["--only-binary", ":all:"]
    argv += ["--link-mode", "copy", "--no-config", "--no-python-downloads"]
    argv += ["--break-system-packages"]
    if find_links is not None:
        argv += ["--no-index", "--find-links", str(find_links)]
    for name in reinstall:
        argv += ["--reinstall-package", name]
    return [*argv, "-r", str(requirements)]


def suite_reinstall_text(manifest: Mapping[str, Any]) -> str:
    """The host and agent-switchboard, each by the sha256 MANIFEST.json gives, as requirements:
    installed again into the suite's copy so that their console scripts are written there."""
    host = manifest["host"]
    hub_wheels = [
        wheel
        for wheel in manifest.get("wheels") or []
        if payload.normal_name(wheel["name"]) == SWITCHBOARD_DISTRIBUTION
    ]
    if len(hub_wheels) != 1:
        raise CheckError(f"MANIFEST.json lists {len(hub_wheels)} {SWITCHBOARD_DISTRIBUTION} wheels")
    pins = [
        (payload.HOST_DISTRIBUTION, host["version"], host["wheel"]["sha256"]),
        (SWITCHBOARD_DISTRIBUTION, hub_wheels[0]["version"], hub_wheels[0]["sha256"]),
    ]
    for name, _, digest in pins:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise CheckError(f"MANIFEST.json gives {name} the sha256 {digest!r}")
    return "".join(f"{name}=={version} --hash=sha256:{digest}\n" for name, version, digest in pins)


def check_suite(app: Path, out: Path, copy_to: Path, checker: Checker) -> None:
    """The host's whole test suite, run by a copy of the app's runtime with the dev and
    conformance dependencies installed into the copy only."""
    manifest = _manifest(app)
    wheels, problems = find_reference_wheels(manifest, reference_folders(out))
    if not checker.expect(
        not problems, "the wheels MANIFEST.json names are here", "\n".join(problems)
    ):
        return
    if copy_to.exists():
        make_writable(copy_to)
        shutil.rmtree(copy_to)
    copy_to.mkdir(parents=True)
    copy = copy_to / app.name
    subprocess.run(["ditto", str(app), str(copy)], check=True)
    make_writable(copy)
    python = python_in(copy)
    # uv's own configuration from the environment could add an index or change what it picks.
    env = payload.without_uv_settings(os.environ)
    exported = subprocess.run(
        suite_export_argv(),
        capture_output=True,
        text=True,
        cwd=build_app.REPO_ROOT,
        env=env,
        check=False,
    )
    if not checker.expect(
        exported.returncode == 0, "uv exports the dev and conformance pins", _shown(exported)
    ):
        return
    requirements = copy_to / "suite-requirements.txt"
    requirements.write_text(exported.stdout, encoding="utf-8")
    done = subprocess.run(suite_install_argv(python, requirements), env=env, check=False)
    if not checker.expect(done.returncode == 0, "they install into the copy, by hash"):
        return
    find_links = copy_to / "wheels"
    find_links.mkdir()
    for name in (
        manifest["host"]["wheel"]["file"],
        *(
            wheel["file"]
            for wheel in manifest["wheels"]
            if payload.normal_name(wheel["name"]) == SWITCHBOARD_DISTRIBUTION
        ),
    ):
        shutil.copyfile(wheels[name], find_links / name)
    again = copy_to / "reinstall.txt"
    again.write_text(suite_reinstall_text(manifest), encoding="utf-8")
    argv = suite_install_argv(
        python,
        again,
        find_links=find_links,
        reinstall=(payload.HOST_DISTRIBUTION, SWITCHBOARD_DISTRIBUTION),
    )
    done = subprocess.run(argv, env=env, check=False)
    checker.expect(
        done.returncode == 0, "the host's wheel and agent-switchboard install again, by hash"
    )
    for script in SUITE_SCRIPTS:
        path = python.parent / script
        checker.expect(
            path.is_file(), f"{script} is beside the copy's interpreter, as the suite needs"
        )
    probe = (
        "import keyring, lucille_host, switchboard\n"
        "for module in (keyring, lucille_host, switchboard):\n"
        "    print(module.__file__)\n"
    )
    done = subprocess.run(
        [str(python), "-I", "-B", "-c", probe], capture_output=True, text=True, check=False
    )
    found = done.stdout.splitlines()
    checker.expect(
        done.returncode == 0
        and len(found) == 3
        and all(line.startswith(str(copy)) for line in found),
        "the copy imports the host and its dependencies from itself",
        _shown(done),
    )
    if checker.failures:
        return
    clean = {
        key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "UV_"))
    }
    done = subprocess.run(
        [str(python), "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=build_app.REPO_ROOT,
        env=clean,
        check=False,
    )
    checker.expect(
        done.returncode == 0, "the host's whole test suite passes on the copy of the runtime"
    )


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


def _stop_earlier_copies(checker: Checker, name: str) -> bool:
    """Stop a copy of the app that is still running, with SIGTERM. One that does not stop fails the
    check, and is then killed: ``open`` would otherwise hand the next step that old copy, whose
    output goes nowhere the check reads."""
    if not _running(name):
        return True
    subprocess.run(["pkill", "-TERM", "-x", name], check=False)
    stopped = _wait_until(lambda: not _running(name), 15)
    checker.expect(stopped, "a copy of the app still running from before stops on SIGTERM")
    if not stopped:
        subprocess.run(["pkill", "-KILL", "-x", name], check=False)
        _wait_until(lambda: not _running(name), 10)
    return stopped


def installed_app_path() -> Path:
    """The copy of the app that runs: the only one that starts anything (decisions/0002 §13).
    ``install`` puts it there, and ``placement``, ``launch``, ``ui`` and ``pairing`` open it."""
    return Path("/Applications") / f"{APP_NAME}.app"


def set_aside_path() -> Path:
    """Where ``install --replace`` keeps the copy that was in /Applications before it, until
    ``install --restore`` puts it back: in the home folder, on the same disk as /Applications, and
    not in ``$TMPDIR``, which macOS empties of what has not been used for a few days."""
    return Path.home() / f"{APP_NAME}.app (set aside by check_app.py install)"


# What tells one build of the app from another: its executable, which holds the bundle's
# signature, and that signature's list of the hash of every other file in the bundle.
BUILD_IDENTITY: Final = (f"Contents/MacOS/{APP_NAME}", "Contents/_CodeSignature/CodeResources")


def is_this_build(copy: Path, app: Path) -> bool:
    """Whether ``copy`` is a copy of the build at ``app``, not a link to one: the files of
    :data:`BUILD_IDENTITY` are byte for byte the same in both. False when any cannot be read. A
    copy an earlier build put in /Applications is not, so no check reports what that copy did as
    this build's."""
    if copy.is_symlink():
        return False
    try:
        return all(
            (copy / name).read_bytes() == (app / name).read_bytes() for name in BUILD_IDENTITY
        )
    except OSError:
        return False


def _move_folder(source: Path, target: Path) -> None:
    """Rename ``source`` to ``target``. A folder made read-only (``chmod -R a-w``) may not move
    to another folder, which rewrites its ``..``: it gets its owner's write permission for the
    move, and loses it again after."""
    try:
        source.rename(target)
        return
    except PermissionError:
        if source.is_symlink():
            raise
    mode = source.stat().st_mode
    source.chmod(mode | stat.S_IWUSR)
    try:
        source.rename(target)
    finally:
        (target if target.exists() else source).chmod(mode)


def _remove_copy(path: Path) -> None:
    """Remove a copy of the app (a link to one: only the link), read-only or not."""
    if path.is_symlink():
        path.unlink()
        return
    make_writable(path)
    shutil.rmtree(path)


def check_install(
    app: Path, checker: Checker, *, replace: bool = False, restore: bool = False
) -> None:
    """Copy the read-only bundle to :func:`installed_app_path`, the one place the app runs from,
    make the copy read-only in its turn, and hold it to M8 (:func:`check_readonly`). A copy of
    this build already there is only held to M8 again. Any other copy there is left alone, unless
    ``replace``: then it is set aside first, at :func:`set_aside_path`. ``restore`` puts back what
    was there before: it removes the installed copy and puts the one set aside back, or, with none
    set aside, removes the installed copy only if it is this build. Nothing is ever set aside over
    a copy set aside before, and no copy of the app may run while one is moved."""
    target = installed_app_path()
    aside = set_aside_path()
    held = target.exists() or target.is_symlink()
    try:
        if restore and not (aside.exists() or aside.is_symlink()):
            if not checker.expect(
                held and is_this_build(target, app),
                f"{target} holds this build, which install put there over nothing",
                "nothing was set aside to put back, and install --restore removes the installed "
                "copy alone only when it is this build",
            ):
                return
            _stop_earlier_copies(checker, APP_NAME)
            _remove_copy(target)
            checker.expect(
                not (target.exists() or target.is_symlink()), f"this build is removed from {target}"
            )
            return
        if restore:
            _stop_earlier_copies(checker, APP_NAME)
            if held:
                _remove_copy(target)
            _move_folder(aside, target)
            checker.expect(
                target.exists() and not aside.exists(), f"the copy set aside is back at {target}"
            )
            return
        if held and not replace and is_this_build(target, app):
            print(f"{target} already holds this build: it is held to M8 again")
            check_readonly(target, checker)
            return
        if held:
            if not checker.expect(
                replace,
                f"{target} holds no copy yet, or install is asked to replace it",
                "it holds one, which install leaves alone: to set it aside and install over it, "
                "run check_app.py install --replace; install --restore then puts it back",
            ):
                return
            if not checker.expect(
                not (aside.exists() or aside.is_symlink()),
                f"nothing that an earlier install --replace set aside is at {aside}",
                "put that back first: check_app.py install --restore",
            ):
                return
            _stop_earlier_copies(checker, APP_NAME)
            _move_folder(target, aside)
            print(
                f"the copy that was at {target} is set aside at {aside}: "
                "check_app.py install --restore puts it back"
            )
    except OSError as exc:
        checker.expect(False, f"the copies at {target} and {aside} could be moved", f"{exc}")
        return
    done = subprocess.run(
        ["ditto", str(app), str(target)], capture_output=True, text=True, check=False
    )
    if not checker.expect(done.returncode == 0, f"ditto copies the app to {target}", _shown(done)):
        return
    done = subprocess.run(
        ["chmod", "-R", "a-w", str(target)], capture_output=True, text=True, check=False
    )
    checker.expect(done.returncode == 0, "the copy is made read-only", _shown(done))
    check_readonly(target, checker)


def check_launch(app: Path, checker: Checker) -> None:
    name = build_app.load_pins().app_name
    _stop_earlier_copies(checker, name)
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


# The menu bar app's status item, and its menu, as System Events names them.
STATUS_ITEM: Final = "menu bar item 1 of last menu bar"
MENU: Final = f"menu 1 of {STATUS_ITEM}"
LINES: Final = "set AppleScript's text item delimiters to linefeed\n"
# The menu's item that opens the code window, while the app's profile holds no pair.
CONNECT_ITEM: Final = "Connect to Lucille…"
# Activity, as in App/ConnectionText.swift (tests/test_macos_build.py checks that they agree): the
# menu's item, which is always there; its window's title, what the window says it keeps, and its
# one button, the disclosure of the host's log, with the words above the log and those for a log
# with no line. Then Stop What's Running, which the menu shows only while a request's process
# runs, the title of the note after it, and the item that resumes.
ACTIVITY_ITEM: Final = "Activity…"
ACTIVITY_TITLE: Final = "Activity"
ACTIVITY_HEADER: Final = (
    f"What Lucille asked this Mac to do since {APP_NAME} opened. {APP_NAME} keeps this only "
    "while it is open."
)
SHOW_LOG: Final = "Show the Host's Log"
LOG_CAPTION: Final = "As the host wrote it. Anything running as you can change this file."
LOG_EMPTY: Final = "The host's log is empty."
# The longest line of the host's log Activity shows, in characters; a longer one is cut, with an
# ellipsis last (App/RequestLog.swift, RequestText.longestLogLine).
LONGEST_LOG_LINE: Final = 2000
STOP_WHATS_RUNNING: Final = "Stop What's Running"
STOPPED_AND_PAUSED: Final = "Stopped, and paused. Resume when you're ready."
RESUME_ITEM: Final = "Resume Lucille"
# The items the menu shows while the app's profile holds no pair, besides its first line.
MENU_ITEMS: Final = (
    "Abilities",
    "Add a Folder…",
    "Folders",
    ACTIVITY_ITEM,
    CONNECT_ITEM,
    f"Quit {APP_NAME}",
)
# The menu's status line while the app's profile holds no pair.
NOT_CONNECTED: Final = "Not connected to Lucille"
# The app's words about connecting, as in App/ConnectionText.swift (tests/test_macos_build.py
# checks that they agree). The menu's other items and status lines.
FINISH_ITEM: Final = "Finish Connecting…"
DISCONNECT_ITEM: Final = "Disconnect from Lucille…"
CONNECTING: Final = "Connecting to Lucille…"
CONNECTED: Final = "Connected to Lucille"
# The code window: its title, the three sentences it says, the hint under a typed word that is no
# code, and its button. Then what the app says when the host refuses a text as no code (bad_code).
CODE_TITLE: Final = "Connect to Lucille"
CODE_WORDS: Final = (
    "In Lucille, open Computers and add this computer. Copy the code Lucille shows, then paste it "
    "below.",
    "To paste, press Command-V, or Control-click the box and choose Paste.",
    "Paste only a code you copied from Lucille's page yourself. Never paste one someone sent you.",
)
CODE_HINT: Final = "That doesn't look like a code from Lucille. Copy it again from Lucille's page."
CONNECT_BUTTON: Final = "Connect"
BAD_CODE: Final = "That isn't a code from Lucille. Copy it again from Lucille's page."
# A code-shaped text the ui check leaves on the pasteboard when the code window opens, which the
# app must not read; and the text it pastes, which passes the app's own check on a code (the
# prefix, then 100 to 8192 characters of base64url) and which the host refuses as no code.
PASTEBOARD_SENTINEL: Final = "swbp1_" + "S" * 120
PASTED_TEXT: Final = "swbp1_" + "A" * 120
# The compare page, and the note after They Match.
COMPARE_TITLE: Final = "Compare the numbers"
COMPARE_QUESTION: Final = "Does Lucille's page show this number?"
THEY_MATCH: Final = "They Match"
CONFIRMED_TITLE: Final = "Confirmed on this Mac"
# Set Up's window and its pages, as App/Setup.swift spells them.
SETUP_TITLE: Final = f"Set Up {APP_NAME}"
SETUP_ADD_BACK: Final = "Add back what you had before?"
SETUP_FOLDERS: Final = "Folders Lucille may use"
SETUP_NO_FOLDERS: Final = "No folders yet"
SETUP_ABILITIES: Final = "What Lucille may do"
SETUP_READY: Final = f"{APP_NAME} is ready"
SETUP_CONTINUE: Final = "Continue"
SETUP_DONE: Final = "Done"
SETUP_ITEM: Final = "Set Up Folders and Abilities…"
# Quit while ``run`` runs: the question, its button, the finishing panel and its button, and the
# panel's words once Stop now is chosen.
QUIT_ITEM: Final = f"Quit {APP_NAME}"
QUIT_QUESTION: Final = f"Quit {APP_NAME}?"
QUIT_TEXT: Final = f"Your Mac will stop answering Lucille until you open {APP_NAME} again."
QUIT_BUTTON: Final = "Quit"
FINISHING: Final = "Finishing the current task…"
STOP_NOW: Final = f"Stop now ({APP_NAME} will be paused when you next open it)"
STOPPING_NOW: Final = "Stopping…"
# The note at launch while the host is paused, and its button that resumes.
PAUSED_TITLE: Final = f"{APP_NAME} is paused"
RESUME_BUTTON: Final = "Resume"
# Disconnect: the question, its button, and the note once Lucille was told.
DISCONNECT_TITLE: Final = "Disconnect this Mac from Lucille?"
DISCONNECT_BUTTON: Final = "Disconnect"
DISCONNECTED_TOLD: Final = "Disconnected. Lucille was told."
# Reset Settings, as in App/HostText.swift and App/ConnectionText.swift (tests/test_macos_build.py
# checks that they agree): the menu's line while the host cannot read its settings file, the
# button its note offers, the question that button asks, the question's button, the note's title
# once the host has set the file aside, and the one a second reset would get. Then what the ui
# check writes as that file, which is not TOML, as in the host's own fixture for ``policy reset``.
SETTINGS_UNREADABLE: Final = f"{APP_NAME}'s settings file can't be read."
RESET_ITEM: Final = "Reset Settings…"
RESET_QUESTION: Final = f"Reset {APP_NAME}'s settings?"
RESET_BUTTON: Final = "Reset Settings"
RESET_TITLE: Final = "Settings reset"
NOTHING_TO_RESET: Final = "Nothing needed resetting"
UNREADABLE_POLICY: Final = b"this is not toml\n"
# A copy anywhere but /Applications/<name>.app, as in App/ConnectionText.swift
# (tests/test_macos_build.py checks that they agree): the title of its note, what the note says of
# a copy in a folder that is none of the others (the build's), and the steps every such note ends
# with. Then a copy in /Applications named as the Finder's Keep Both names a second one: its title,
# and its note's words with the copy at the app's own name there, and with none. Then how long
# the placement check watches a refusing copy for a host process.
PLACEMENT_TITLE: Final = f"{APP_NAME} needs to be in your Applications folder"
PLACEMENT_ELSEWHERE: Final = f"This copy of {APP_NAME} can't run from where it is now."
MOVING_STEPS: Final = (
    f"Quit {APP_NAME}, drag it into your Applications folder, then open it from there."
)
RENAMED_COPY: Final = f"{APP_NAME} 2"
RENAMED_TITLE: Final = f"This copy is named {RENAMED_COPY}, so it can't run"
RENAMED_WITH_ORIGINAL: Final = (
    f"Quit {APP_NAME}, then move {RENAMED_COPY} to the Trash. Then drag the new {APP_NAME} into "
    "your Applications folder again, and choose Replace."
)
RENAMED_ALONE: Final = (
    f"Quit {APP_NAME}. In the Finder, rename {RENAMED_COPY} to {APP_NAME}, then open it again."
)
REFUSAL_WATCH: Final = 10
# Lucille asking for a newer release than this one (decisions/0002 §16), as in
# App/ConnectionText.swift: the minimum the pairing check asks for, which a development build is
# older than (spec/PROTOCOL.md section 10), a higher one, asked for while the notice is up, and a
# higher one still, asked for once it is closed; the notice's title for each; where it says to get
# the update, in words, since there is no Download button; its two buttons, Not Now the default;
# and the menu's item while a notice is held.
UPDATE_MIN_VERSION: Final = "999.0.0"
UPDATE_HIGHER_VERSION: Final = "999.0.1"
UPDATE_HIGHEST_VERSION: Final = "999.0.2"
UPDATE_TITLE: Final = f"Lucille needs {APP_NAME} {UPDATE_MIN_VERSION} or newer"
UPDATE_HIGHER_TITLE: Final = f"Lucille needs {APP_NAME} {UPDATE_HIGHER_VERSION} or newer"
UPDATE_HIGHEST_TITLE: Final = f"Lucille needs {APP_NAME} {UPDATE_HIGHEST_VERSION} or newer"
UPDATE_WHERE: Final = f"Get the newer {APP_NAME} from where you got this one."
PREPARE_TO_UPDATE: Final = "Prepare to Update"
NOT_NOW: Final = "Not Now"
PREPARE_TO_UPDATE_ITEM: Final = "Prepare to Update…"
# The line the app writes on standard error when ``run`` exits after a drain its standard input's
# end began (``RunSupervisor.describe``), and the start of the one for a ``run`` a signal ended.
DRAINED_LINE: Final = "run exited 0 (drained, stdin_end)"
SIGNALED_LINE: Final = "run ended by signal"
# Open at Login (decisions/0002 §3.1 step 10, §3.3, §9), as App/Setup.swift and App/LoginRules.swift
# spell it (tests/test_macos_build.py checks that they agree): Ready's box and the first sentence
# of the notice under it; the line the app writes on standard error each time it acts on its login
# item or checks it at launch, with each state macOS can give; the note after Done when macOS needs
# the user's OK, the one when it couldn't, and the button that opens System Settings, which the
# checks never press; the note at launch while paired and the item waits for approval, and its
# button that takes the item away; the sentence Disconnect's note adds; and the menu's submenu
# while connected. Then the note the app shows after Done for each state, none while enabled.
OPEN_AT_LOGIN_BOX: Final = f"Open {APP_NAME} when you log in"
OPEN_AT_LOGIN_NOTICE: Final = (
    f"macOS will show a notice that {APP_NAME} can now open when you log in."
)
LOGIN_STATES: Final = ("notRegistered", "enabled", "requiresApproval", "notFound")
LOGIN_ITEM_LINE: Final = re.compile(
    r"^login item: (notRegistered|enabled|requiresApproval|notFound)$", re.MULTILINE
)
NEEDS_APPROVAL_TITLE: Final = f"macOS needs your OK before {APP_NAME} can open when you log in"
LOGIN_FAILED_TITLE: Final = f"{APP_NAME} couldn't turn on opening at login"
OPEN_LOGIN_ITEMS: Final = "Open Login Items Settings"
LAUNCH_OFF_TITLE: Final = f"{APP_NAME} won't start when you log in"
KEEP_IT_OFF: Final = "Keep It Off"
OFF_AFTER_DISCONNECT: Final = f"{APP_NAME} won't open when you log in."
SETTINGS_MENU: Final = "Settings"
LOGIN_NOTE_AFTER_DONE: Final = {
    "enabled": None,
    "requiresApproval": NEEDS_APPROVAL_TITLE,
    "notRegistered": LOGIN_FAILED_TITLE,
    "notFound": LOGIN_FAILED_TITLE,
}
# The button that closes each of those notes and does nothing else, by its title.
LOGIN_NOTE_CLOSES: Final = {NEEDS_APPROVAL_TITLE: NOT_NOW, LOGIN_FAILED_TITLE: "OK"}
# What the app says of its login item once it has none: macOS gives notRegistered, or notFound
# when it never had one to find.
LOGIN_ITEM_GONE: Final = ("notRegistered", "notFound")
# The app's one opener (decisions/0002 §20), as App/PlaceRules.swift and App/HostText.swift spell
# it (tests/test_macos_build.py checks): the placement note's button, the button of the note that
# says macOS is blocking a folder, and the first sentence of that note. Then the System Settings
# pages the opener may show, each with the title the places check expects of its window: the same
# URLs as tools/guards/macos_lint.py's OPENABLE, which this script cannot import (the lint needs
# yaml), so a build test holds that they agree. Neither the URLs nor the titles have been seen on a
# Mac yet: the places check prints what each page shows, and gates only on macos-15. Then the
# processes the checks read: System Settings, the Finder and its window on the Applications
# folder, and the one that draws macOS's question about a protected folder (not seen on a Mac yet).
SHOW_APPLICATIONS_FOLDER: Final = "Show Applications Folder"
OPEN_PRIVACY_SETTINGS: Final = "Open Privacy Settings"
BLOCKED_BY_MACOS: Final = f"macOS is blocking {APP_NAME} from this folder."
DATE_AND_TIME_PANE: Final = "x-apple.systempreferences:com.apple.Date-Time-Settings.extension"
FILES_AND_FOLDERS_PANE: Final = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"
)
SETTINGS_PANES: Final = {
    DATE_AND_TIME_PANE: "Date & Time",
    FILES_AND_FOLDERS_PANE: "Files & Folders",
}
SYSTEM_SETTINGS: Final = "System Settings"
FINDER: Final = "Finder"
APPLICATIONS_WINDOW: Final = "Applications"
TCC_PROMPT_PROCESS: Final = "UserNotificationCenter"
# The answer to macOS's question that the ui check gives, its apostrophe folded, and the privacy
# service of the Documents folder, whose answer for the app tccutil takes back afterwards.
DONT_ALLOW: Final = "Don't Allow"
DOCUMENTS_SERVICE: Final = "SystemPolicyDocumentsFolder"


def _system_events(body: str, timeout: float = 30) -> tuple[bool, str]:
    """Run AppleScript against the app's process through System Events: (worked, what it said)."""
    return _tell(build_app.load_pins().app_name, body, timeout)


def _tell(process: str, body: str, timeout: float = 30) -> tuple[bool, str]:
    """Run AppleScript against ``process`` through System Events: (worked, what it said)."""
    script = (
        f'tell application "System Events"\ntell process "{process}"\n{body}\nend tell\nend tell'
    )
    try:
        done = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"osascript did not finish within {timeout:.0f} s"
    return done.returncode == 0, (done.stdout if done.returncode == 0 else done.stderr).strip()


def _screenshot(path: Path) -> None:
    done = subprocess.run(
        ["screencapture", "-x", str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    # Flushed, so that the job log's time for the line is when the screenshot was taken.
    print(f"screenshot {path.name}: " + ("saved" if path.is_file() else _shown(done)), flush=True)


def _open_menu_and_read() -> tuple[bool, list[str]]:
    worked, text = _system_events(
        f"click {STATUS_ITEM}\ndelay 1\n{LINES}return (name of every menu item of {MENU}) as text"
    )
    return worked, text.splitlines() if worked else [text]


def _can_drive() -> bool:
    """Whether System Events can read this app's menu bar here."""
    worked, items = _system_events(
        f"{LINES}return (description of every menu bar item of last menu bar) as text"
    )
    if not worked:
        print(
            "note: System Events cannot read this app's menu bar on this machine, so the app "
            f"was not driven: {items}"
        )
        return False
    print(f"the app's status items: {items.splitlines()}")
    return True


def _status_item() -> dict[str, str]:
    """What System Events reports of the menu bar item: its title and description, which
    VoiceOver reads as its name, its name and help (the tooltip), and its width."""
    script = (
        f"set found to {{}}\nset theItem to {STATUS_ITEM}\n"
        + "".join(
            f'try\nset end of found to "{key}=" & ({key} of theItem)\nend try\n'
            for key in ("title", "description", "name", "help")
        )
        # Not "item 1 of": inside System Events, "item" names one of its own classes.
        + "try\nset {theWidth, theHeight} to size of theItem\n"
        + 'set end of found to "width=" & (theWidth as text)\nend try\n'
        + f"{LINES}return found as text"
    )
    worked, said = _system_events(script)
    print(f"the menu bar item says: {said.splitlines() if worked else said}")
    if not worked:
        return {}
    return dict(line.partition("=")[::2] for line in said.splitlines())


def _width(item: dict[str, str]) -> float:
    """The menu bar item's width in points, or 0 when System Events did not give one."""
    try:
        return float(item.get("width", "").replace(",", "."))
    except ValueError:
        return 0.0


def _named(item: dict[str, str], name: str) -> bool:
    """Whether VoiceOver reads ``name`` for the menu bar item: its title or its description. Its
    help, the tooltip, does not count."""
    return name in (item.get("title"), item.get("description"))


def _welcome(checker: Checker, shot: Path, when: str) -> None:
    """Read the note that says what the app is for and where its menu is, and choose OK."""
    texts: list[str] = []

    def shown() -> bool:
        nonlocal texts
        worked, texts = _window_list("value of every static text")
        return worked and WELCOME_TITLE in texts

    found = _wait_until(shown, 20)
    _screenshot(shot)
    checker.expect(
        found and any("by the clock" in text for text in texts),
        f"{when}, the app says where its menu is",
        f"{texts}",
    )
    checker.expect(
        found and any(PURPOSE in text for text in texts),
        f"{when}, the app says what it is for",
        f"{texts}",
    )
    worked, said = _system_events('click button "OK" of window 1')
    checker.expect(
        worked
        and _wait_until(
            lambda: WELCOME_TITLE not in _window_list("value of every static text")[1], 10
        ),
        "OK closes the note",
        said,
    )


def _drive_welcome(checker: Checker, screens: Path, app: Path, *, paired: bool) -> None:
    """The app has no window and no Dock icon, so when it opens with no confirmed pair, and
    whenever it is opened again while it runs, it says where its menu is (decisions/0002, What
    is built today). Opened while ``paired``, it goes straight on: the check sees no such note,
    and presses nothing."""
    if paired:
        shown = _wait_until(lambda: _says(WELCOME_TITLE), 10)
        checker.expect(
            not shown,
            "opened while paired, the app shows no note about its menu bar",
            f"{_texts()}" if shown else "",
        )
    else:
        _welcome(checker, screens / "1-welcome.png", "when it opens")
    done = subprocess.run(["open", str(app)], capture_output=True, text=True, check=False)
    checker.expect(done.returncode == 0, "open opens the running app again", _shown(done))
    _welcome(checker, screens / "1-welcome-again.png", "when it is opened again")


def _drive_menu(checker: Checker, screens: Path, flag: Path) -> bool:
    """Read the menu bar item's name and the menu, then choose Pause Lucille and Resume Lucille.
    False if System Events cannot open the menu on this machine."""
    version = build_app.host_version()
    item = _status_item()
    checker.expect(
        _named(item, STATUS_ITEM_NAMES[0]),
        f"the menu bar item's title or description is {STATUS_ITEM_NAMES[0]!r}",
        f"{item}",
    )
    checker.expect(
        _width(item) >= 16,
        "the menu bar item is wide enough to show its icon",
        f"{item}",
    )
    worked, shown = _open_menu_and_read()
    if not worked:
        print(f"note: System Events cannot open the menu on this machine: {shown}")
        return False
    print(f"the menu shows: {shown}")
    _screenshot(screens / "2-menu-open.png")
    worked, abilities = _system_events(
        f'{LINES}return (name of every menu item of menu 1 of menu item "Abilities" of {MENU}) '
        "as text"
    )
    listed = abilities.splitlines() if worked else []
    print(f"the Abilities submenu shows: {listed if worked else abilities}")
    _system_events("key code 53")
    checker.expect(
        shown[:1] == [f"{APP_NAME} {version}"],
        "the menu's first line names the version",
        f"{shown}",
    )
    checker.expect(
        NOT_CONNECTED in shown and "Pause Lucille" in shown,
        f"the menu says {NOT_CONNECTED!r}, and offers Pause Lucille",
        f"{shown}",
    )
    missing = [item for item in MENU_ITEMS if item not in shown]
    checker.expect(not missing, "the menu holds every item", f"missing: {missing}")
    checker.expect(
        worked
        and set(ABILITY_TITLES.values()) <= set(listed)
        and not {"host.sleep", "host.describe", *ABILITY_TITLES} & set(listed),
        "Abilities lists each ability in plain words, and not host.sleep or host.describe",
        abilities,
    )
    checker.expect(
        worked and set(ABILITY_DETAILS.values()) <= set(listed),
        "under each ability, a sentence says what Lucille gets",
        abilities,
    )
    if flag.exists():
        print("note: the host was paused before this check, so Pause and Resume were not tried")
        return True

    worked, said = _system_events(
        f'click {STATUS_ITEM}\ndelay 1\nclick menu item "Pause Lucille" of {MENU}'
    )
    checker.expect(
        worked and _wait_until(flag.exists, 15),
        "choosing Pause Lucille runs `pause`: the pause flag appears",
        said,
    )
    time.sleep(3)
    worked, shown = _open_menu_and_read()
    _screenshot(screens / "3-paused.png")
    _system_events("key code 53")
    checker.expect(
        worked
        and f"{NOT_CONNECTED} \N{MIDDLE DOT} Paused" in shown
        and PAUSED_NOTE in shown
        and "Resume Lucille" in shown
        and "Pause Lucille" not in shown,
        "the menu then says it is paused, and offers Resume Lucille",
        f"{shown}",
    )
    item = _status_item()
    checker.expect(
        _named(item, STATUS_ITEM_NAMES[1]),
        f"the menu bar item's title or description is then {STATUS_ITEM_NAMES[1]!r}",
        f"{item}",
    )
    worked, said = _system_events(
        f'click {STATUS_ITEM}\ndelay 1\nclick menu item "Resume Lucille" of {MENU}'
    )
    checker.expect(
        worked and _wait_until(lambda: not flag.exists(), 15),
        "choosing Resume Lucille runs `resume`: the pause flag goes",
        said,
    )
    return True


def _op_state(python: Path, op: str) -> str:
    """The state ``ops list`` gives one operation, for the real HOME, on the app's profile."""
    for line in _app_host(python, ["ops", "list"]).stdout.splitlines():
        name, _, state = line.partition("  ")
        if name == op:
            return state
    return ""


def _roots(python: Path) -> dict[str, str]:
    """``roots list`` for the real HOME, on the app's profile, as {alias: path}."""
    listed = _app_host(python, ["roots", "list"]).stdout.splitlines()
    return dict(line.split("  ", 1) for line in listed if "  " in line)


def _drive_abilities(checker: Checker, python: Path, screens: Path) -> None:
    """Switch an ability on and off again from the Abilities submenu, by its plain words."""
    off = [op for op in ABILITY_TITLES if _op_state(python, op) == "disabled"]
    if not off:
        print("note: no listed ability is plainly off here, so the Abilities submenu was not used")
        return
    op = off[0]
    title = ABILITY_TITLES[op]
    submenu = f'menu 1 of menu item "Abilities" of {MENU}'
    open_submenu = f'click {STATUS_ITEM}\ndelay 1\nclick menu item "Abilities" of {MENU}\ndelay 1\n'
    try:
        worked, said = _system_events(f'{open_submenu}click menu item "{title}" of {submenu}')
        checker.expect(
            worked and _wait_until(lambda: _op_state(python, op) == "enabled", 15),
            f"choosing {title!r} under Abilities runs `ops enable {op}`",
            said,
        )
        time.sleep(2)
        worked, mark = _system_events(
            f'{open_submenu}return value of attribute "AXMenuItemMarkChar" of menu item '
            f'"{title}" of {submenu}'
        )
        _screenshot(screens / "4-ability-on.png")
        _system_events("key code 53\ndelay 0.3\nkey code 53")
        checker.expect(
            worked and mark not in ("", "missing value"), f"{title!r} then has a checkmark", mark
        )
        worked, said = _system_events(f'{open_submenu}click menu item "{title}" of {submenu}')
        checker.expect(
            worked and _wait_until(lambda: _op_state(python, op) == "disabled", 15),
            f"choosing {title!r} again runs `ops disable {op}`",
            said,
        )
    finally:
        if _op_state(python, op) == "enabled":
            _app_host(python, ["ops", "disable", op])


def _ask_to_add_a_folder() -> tuple[bool, str]:
    """Choose Add a Folder… from the app's menu, which opens the Open panel."""
    return _system_events(
        f"click {STATUS_ITEM}\ndelay 1\n"
        f'click (first menu item of {MENU} whose name starts with "Add a Folder")',
        timeout=60,
    )


def _choose_folder(folder: Path, screens: Path, shot: str) -> tuple[bool, str]:
    """Choose Add a Folder…, go to ``folder`` in the Open panel and choose it. Whatever the app
    shows next is left open."""
    worked, said = _ask_to_add_a_folder()
    if not worked:
        return False, f"choosing Add a Folder: {said}"
    time.sleep(2)
    return _go_to_folder(folder, screens, shot)


def _go_to_folder(folder: Path, screens: Path, shot: str) -> tuple[bool, str]:
    """In the open Open panel, go to ``folder`` and choose it: click the panel's prompt wherever
    it sits, or, where no button by that name can be found, press Return, the panel's default
    button (:data:`CHOOSE_IN_PANEL`). What the app shows next is what the caller checks: the name
    dialog, the question about a broad folder, or the note that refuses the whole disk."""
    worked, said = _system_events(
        "set frontmost to true\n"
        'keystroke "g" using {command down, shift down}\n'
        "delay 1\n"
        f'keystroke "{folder}"\n'
        "delay 1\n"
        "key code 36\n"
        "delay 2",
        timeout=60,
    )
    if not worked:
        return False, f"going to {folder} in the Open panel: {said}"
    _screenshot(screens / shot)
    worked, said = _system_events(CHOOSE_IN_PANEL, timeout=90)
    if worked and said != "clicked":
        print(f"note: choosing {folder} in the Open panel: {said}")
    return worked, said if worked else f"choosing the folder: {said}"


def _panel_buttons() -> tuple[bool, list[str]]:
    """The names of the buttons in every window of the app, wherever they sit
    (:func:`_in_each_place`): the Open panel's among them, which on macOS 26 and 27 are not all
    buttons of ``window 1``."""
    worked, said = _system_events(
        "set found to {}\n"
        + _in_each_place("try\nset found to found & (name of every button of {place})\nend try\n")
        + f"{LINES}return found as text",
        timeout=90,
    )
    return worked, said.splitlines() if worked else [said]


def _window_list(what: str) -> tuple[bool, list[str]]:
    """``what`` of every element of one kind in the app's front window, such as ``name of every
    button``."""
    worked, said = _system_events(f"{LINES}return ({what} of window 1) as text")
    return worked, said.splitlines() if worked else [said]


def _name_field() -> tuple[bool, str]:
    return _system_events("return value of text field 1 of window 1")


def _name_dialog() -> tuple[bool, str]:
    """The name dialog's field, once the dialog is up: the app asks the host for the folder's alias
    (``roots add --suggest-alias``) before it shows it, for 10 seconds at most."""
    _wait_until(lambda: _name_field()[0], 15)
    return _name_field()


def _acknowledge(checker: Checker, words: str, shot: Path) -> None:
    """The app shows a note holding ``words``; read it and choose OK."""
    time.sleep(2)
    worked, texts = _window_list("value of every static text")
    _screenshot(shot)
    checker.expect(
        worked and any(words in text for text in texts),
        f"the app then says {words!r}",
        f"{texts}",
    )
    _system_events('click button "OK" of window 1')


def _quoted(text: str) -> str:
    """``text`` as an AppleScript string. The app's words hold no quote and no backslash."""
    if '"' in text or "\\" in text:
        raise ValueError(f"{text!r} cannot be quoted for AppleScript here")
    return f'"{text}"'


# AppleScript that sets ``held`` to each window's name and the roles of the UI elements it holds,
# for an error that says what was on the screen instead.
WINDOWS_HOLD: Final = (
    'set AppleScript\'s text item delimiters to ", "\n'
    'set held to ""\n'
    "repeat with theWindow in windows\n"
    "try\n"
    'set held to held & " [" & (name of theWindow as text) & ": " '
    "& ((role of every UI element of theWindow) as text)\n"
    "try\n"
    'set held to held & "; its buttons: " & ((name of every button of theWindow) as text)\n'
    "end try\n"
    "try\n"
    "repeat with theSheet in sheets of theWindow\n"
    'set held to held & "; a sheet: " & ((role of every UI element of theSheet) as text)\n'
    "end repeat\n"
    "end try\n"
    'set held to held & "]"\n'
    "end try\n"
    "end repeat\n"
)


def _in_each_place(probe: str) -> str:
    """AppleScript that runs ``probe``, with ``{place}`` in it replaced by each place in the app's
    windows where a control may sit: the window itself; each sheet on it, and each group in that
    sheet; each group and each split group in the window, and each group in those. AppKit lays out
    the Open panel, and the layout differs between macOS versions: on macOS 26 and 27, ``button
    ... of window 1`` did not find the panel's prompt (the smoke jobs, 2026-11-02), as it does on
    macOS 15. It walks these few levels by name, never the whole tree under a window (see
    :func:`_target`), and each loop variable is used only as the container of a lookup, never
    compared."""

    def at(place: str) -> str:
        return probe.replace("{place}", place)

    def inner(outer: str) -> str:
        return (
            "try\n"
            f"repeat with theInner in groups of {outer}\n" + at("theInner") + "end repeat\n"
            "end try\n"
        )

    def groups(kind: str) -> str:
        return (
            "try\n"
            f"repeat with theGroup in {kind} of theWindow\n"
            + at("theGroup")
            + inner("theGroup")
            + "end repeat\n"
            "end try\n"
        )

    return (
        "repeat with theWindow in windows\n" + at("theWindow") + "try\n"
        "repeat with theSheet in sheets of theWindow\n"
        + at("theSheet")
        + inner("theSheet")
        + "end repeat\n"
        "end try\n" + groups("groups") + groups("splitter groups") + "end repeat\n"
    )


def _click_in_place(name: str) -> str:
    """A probe for :func:`_in_each_place` that sets ``theTarget`` to the button named ``name``
    in the first place that holds one."""
    return (
        "if theTarget is missing value then\n"
        "try\n"
        f"if exists (button {_quoted(name)} of {{place}}) then "
        f"set theTarget to button {_quoted(name)} of {{place}}\n"
        "end try\n"
        "end if\n"
    )


def _target(kind: str, name: str | None = None) -> str:
    """AppleScript that sets ``theTarget`` to the first UI element of the class ``kind``
    (``button``, ``text field``), with the name ``name`` if one is given, in the first of the app's
    windows, in the order System Events gives them, that holds one; or stops with an error that
    names each window and the roles of what it holds. It looks among each window's own UI
    elements, as ``button "OK" of window 1`` does: the app's windows hold their views straight in
    the window, in no group (App/PairingPanel.swift, App/ProgressPanel.swift), as a note does. It
    does not walk ``entire contents``, which found nothing in the code window on macOS 15 while
    ``of window`` lookups found a note's words."""
    one = f"{kind} 1 of theWindow" if name is None else f"{kind} {_quoted(name)} of theWindow"
    said = kind if name is None else f"{kind} named {name}"
    return (
        "set theTarget to missing value\n"
        "repeat with theWindow in windows\n"
        "try\n"
        f"if exists ({one}) then set theTarget to {one}\n"
        "end try\n"
        "if theTarget is not missing value then exit repeat\n"
        "end repeat\n"
        "if theTarget is missing value then\n"
        + WINDOWS_HOLD
        + f"error {_quoted(f'no {said} on the screen:')} & held\n"
        "end if\n"
    )


def _every(kind: str, what: str) -> list[str]:
    """``what`` of every UI element of the class ``kind`` that a window of the app holds, one per
    line (``value`` of every ``static text``, say), looked for as :func:`_target` looks; empty when
    System Events cannot say, which it then prints."""
    worked, said = _system_events(
        "set found to {}\n"
        "set theProblems to {}\n"
        "repeat with theWindow in windows\n"
        "try\n"
        f"set found to found & ({what} of every {kind} of theWindow)\n"
        "on error theError\n"
        "set end of theProblems to theError\n"
        "end try\n"
        "end repeat\n"
        f"{LINES}"
        "if found is {} and theProblems is not {} then error (theProblems as text)\n"
        "return found as text"
    )
    if not worked:
        print(f"note: reading every {kind}: {said}")
        return []
    if not said.strip():
        _, held = _system_events(WINDOWS_HOLD + "return held")
        print(f"note: no {kind} in the app's windows, which hold:{held}")
    return said.splitlines()


# The buttons of what the app shows once the Open panel has chosen a folder: the name dialog, the
# question about a broad folder, and the note that refuses the whole disk.
AFTER_THE_PANEL: Final = ("Add Folder", "Add Anyway", "Choose a Smaller Folder")
# Choose the folder the Open panel shows: click its prompt wherever it sits in the app's windows
# (:func:`_in_each_place`). Where no button by that name is found, but neither is one of the
# app's own windows that come after the panel (:data:`AFTER_THE_PANEL`, which a Go to Folder that
# chose by itself would have brought up), press Return, which the panel's default button, the
# prompt, answers; and say what the windows held. Either way the caller checks what the app shows
# next, so a Return that chose nothing, or the wrong folder, fails there.
CHOOSE_IN_PANEL: Final = (
    "set theTarget to missing value\n"
    + _in_each_place(_click_in_place(OPEN_PROMPT))
    + "if theTarget is not missing value then\n"
    "click theTarget\n"
    "delay 2\n"
    'return "clicked"\n'
    "end if\n"
    + "".join(_in_each_place(_click_in_place(after)) for after in AFTER_THE_PANEL)
    + "if theTarget is not missing value then\n"
    'return "already past the Open panel: it shows " & (name of theTarget as text)\n'
    "end if\n" + WINDOWS_HOLD + "set frontmost to true\n"
    "key code 36\n"
    "delay 2\n"
    f'return "pressed Return, the default button: no button named " & {_quoted(OPEN_PROMPT)} '
    '& ", and the app\'s windows held" & held'
)


def _texts() -> list[str]:
    """Every text the app's windows show, its notes included."""
    return _every("static text", "value")


def _says(words: str) -> bool:
    return any(words in text for text in _texts())


def _press(name: str) -> tuple[bool, str]:
    """Click the button named ``name``, in whichever window of the app holds it."""
    return _system_events(_target("button", name) + 'click theTarget\nreturn "pressed"')


def _press_in_front(name: str) -> tuple[bool, str]:
    """Click the button named ``name`` of the app's front window, whose buttons the caller has
    just read there (:func:`_window_list`)."""
    return _system_events(f'click button {_quoted(name)} of window 1\nreturn "pressed"')


def _toggle(name: str) -> tuple[bool, str]:
    """Click the checkbox named ``name``, in whichever window of the app holds it."""
    return _system_events(_target("checkbox", name) + 'click theTarget\nreturn "clicked"')


def _checked(name: str) -> str:
    """Whether the checkbox named ``name`` is checked: ``1``, ``0``, or ``absent``."""
    worked, said = _system_events(
        _target("checkbox", name) + "return ((value of theTarget) as text)"
    )
    return said if worked else "absent"


def _enabled(name: str) -> str:
    """Whether the button named ``name`` is on: ``true``, ``false``, or ``absent``."""
    worked, said = _system_events(
        _target("button", name) + "return ((enabled of theTarget) as text)"
    )
    return said if worked else "absent"


def _field() -> tuple[bool, str]:
    """What the first text field in the app's windows holds."""
    return _system_events(_target("text field") + "return ((value of theTarget) as text)")


def _window_names() -> list[str]:
    worked, said = _system_events(f"{LINES}return (name of every window) as text")
    return said.splitlines() if worked else []


def _app_windows() -> str:
    """Every window of the app as System Events lists it, named or not, with what it holds, and
    whether the app is frontmost, for a failure's detail. A listing that failed says so, where
    :func:`_window_names` reads it as no window."""
    worked, said = _system_events(
        WINDOWS_HOLD + 'return "frontmost " & (frontmost as text) & ", "'
        ' & ((count of windows) as text) & " window(s):" & held'
    )
    return said if worked else f"System Events did not answer: {said}"


def _choose(start: str) -> tuple[bool, str]:
    """Open the menu and choose the item whose name starts with ``start``, which spares the
    script the ellipsis some items end with."""
    return _system_events(
        f"click {STATUS_ITEM}\ndelay 1\n"
        f"click (first menu item of {MENU} whose name starts with {_quoted(start)})",
        timeout=60,
    )


# Close the status menu (:func:`_close_menu`): one Escape, then a second before anything clicks
# the status item again. The first line sends nothing when System Events finds no menu at all.
CLOSE_MENU: Final = (
    f'if not (exists {MENU}) then return "no menu was open, so no Escape was sent"\n'
    "key code 53\n"
    "delay 1\n"
    'return "Escape sent"'
)


def _close_menu() -> None:
    """Close the status menu, right after a read that found it open: only then is it known to be
    open. An Escape sent with no menu open goes to the app's key window instead, and closes a
    panel: in CI run 42395410137 (macos-15) the one a menu screenshot sent after a click that
    showed no menu closed Set Up's window. In every screenshot taken that run by a click that came
    right after an Escape, no menu was open. So no Escape is sent but this one, and a second
    passes before the next click on the status item. System Events cannot be asked instead: in
    run 42402770764 it still found the menu after every one of 11 Escapes, while each next click
    opened the menu and read it as usual. Anything but a sent Escape is printed, as a note."""
    worked, said = _system_events(CLOSE_MENU)
    if not worked or said != "Escape sent":
        print(f"note: closing the menu: {said if worked else f'System Events: {said}'}", flush=True)


def _menu_until(
    test: Callable[[list[str]], bool], seconds: float, shot: Path | None = None
) -> tuple[bool, list[str]]:
    """Open the menu, read it and close it again, until ``test`` holds of what it shows, for at
    most ``seconds``. The menu shows what the app knew when it opened, and opening it starts a
    refresh, so one read can be a refresh behind. With ``shot``, a screenshot of the last read, the
    menu still open if that read worked. The menu is closed only after a read that worked
    (:func:`_close_menu`): the check sends no Escape of its own to close a menu it did not see."""
    deadline = time.monotonic() + seconds
    while True:
        worked, shown = _open_menu_and_read()
        held = worked and test(shown)
        last = held or time.monotonic() >= deadline
        if last and shot is not None:
            _screenshot(shot)
        if worked:
            _close_menu()
        if last:
            return held, shown
        time.sleep(3)


def _set_pasteboard(text: str) -> None:
    """Put ``text`` on the general pasteboard, as a user copying it would; empty clears it."""
    done = subprocess.run(
        ["pbcopy"], input=text, capture_output=True, text=True, timeout=30, check=False
    )
    if done.returncode != 0:
        print(f"note: pbcopy: {_shown(done)}")


def _profile_folder() -> Path:
    """Where the host keeps the app's profile, for the real HOME (src/lucille_host/paths.py)."""
    return Path.home() / "Library" / "Application Support" / "lucille-host" / APP_PROFILE


def _app_host(python: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """The host's command line for the real HOME, on the app's own profile."""
    return _host(python, Path.home(), ["--profile", APP_PROFILE, *args])


def _pair_state(python: Path) -> str | None:
    """The ``pair`` state ``status --json`` gives on the app's profile, for the real HOME."""
    done = _app_host(python, ["--json", "status"])
    try:
        found = json.loads(done.stdout.splitlines()[-1])
        return str(found["pair"]["state"])
    except (IndexError, KeyError, TypeError, ValueError):
        print(f"note: status --json: {_shown(done)}")
        return None


def _login_state(text: str) -> str | None:
    """The state of the last ``login item: <state>`` line in ``text``, as the app writes it on
    standard error each time it acts on its login item or checks it at launch
    (App/LoginItem.swift); None when there is none."""
    found = LOGIN_ITEM_LINE.findall(text)
    return found[-1] if found else None


def _login_note() -> str | None:
    """The title of the note about opening at login that the app shows after Done or the
    Settings menu's switch, if one is up: that macOS needs the user's OK, or that it couldn't."""
    for title in LOGIN_NOTE_CLOSES:
        if _says(title):
            return title
    return None


def _close_login_note() -> None:
    """Close the note about opening at login that is up, with the button that does nothing else
    (:data:`LOGIN_NOTE_CLOSES`): never Open Login Items Settings. Nothing is pressed when no such
    note is up."""
    note = _login_note()
    if note is not None:
        _press(LOGIN_NOTE_CLOSES[note])
        _wait_until(lambda: _login_note() is None, 10)


def _login_items_naming(text: str, names: Sequence[str]) -> list[str]:
    """The records of ``sfltool dumpbtm``'s listing, blocks parted by a blank line, that hold any
    of ``names``."""
    return [block for block in re.split(r"\n\s*\n", text) if any(name in block for name in names)]


def _dump_login_items() -> None:
    """Print what macOS's own list of login and background items (``sfltool dumpbtm``, which
    needs root) holds about the app: how many records name its bundle identifier or its path,
    and the first five of them. For the record only, never a gate: what macOS answers an ad-hoc
    build is not known (decisions/0002 §9). ``sudo -n`` asks for no password; where it is
    refused, where there is no sudo or no sfltool, or where it hangs, it says so and goes on."""
    argv = ["sudo", "-n", "sfltool", "dumpbtm"]
    shown = " ".join(argv)
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"note: {shown} did not run: {exc}", flush=True)
        return
    records = _login_items_naming(
        done.stdout, (build_app.load_pins().bundle_id, f"/{APP_NAME}.app")
    )
    print(f"{shown} exited {done.returncode}: {len(records)} record(s) name the app", flush=True)
    for record in records[:5]:
        print(record.strip("\n"), flush=True)
    if done.returncode != 0:
        print(f"its standard error: {done.stderr[-2000:]!r}", flush=True)


def _drive_folders(checker: Checker, python: Path, screens: Path) -> None:
    """Add a folder through the Open panel and the name dialog, correcting a name the host would
    refuse, then remove it from the Folders submenu. Then choose the home folder: the app asks
    first; Choose a Smaller Folder goes back to the Open panel, and Add Anyway adds it."""
    home = Path.home()
    folder = home / "lucille-host-ui-check" / "My Project Files"
    folder.mkdir(parents=True, exist_ok=True)
    added: list[str] = []
    try:
        worked, said = _choose_folder(folder, screens, "5-open-panel.png")
        if not checker.expect(
            worked, "Add a Folder… opens the Open panel, and a folder is chosen", said
        ):
            return
        worked, suggestion = _name_dialog()
        _screenshot(screens / "6-name-dialog.png")
        checker.expect(
            worked and suggestion == "my-project-files",
            "the name dialog then suggests my-project-files",
            suggestion,
        )
        # Delete what the field holds with Backspace, which needs no Edit menu, then type a name
        # the host would refuse.
        worked, said = _system_events(
            "set frontmost to true\n"
            "repeat 40 times\nkey code 51\nend repeat\n"
            'keystroke "My Project Files"\n'
            "delay 0.5\n"
            'click button "Add Folder" of window 1\n'
            "delay 2\n"
            "return value of text field 1 of window 1"
        )
        _screenshot(screens / "7-name-corrected.png")
        checker.expect(
            worked and said == "my-project-files",
            "a typed name the host would refuse comes back as one it accepts, not added",
            f"{said} {_roots(python)}",
        )
        checker.expect("my-project-files" not in _roots(python), "nothing is added before then")
        worked, said = _system_events('click button "Add Folder" of window 1')
        checker.expect(
            worked
            and _wait_until(lambda: _roots(python).get("my-project-files") == str(folder), 15),
            "Add Folder then runs `roots add my-project-files <the folder>`",
            f"{said} {_roots(python)}",
        )
        added.append("my-project-files")
        _acknowledge(
            checker, "Once this Mac is connected to Lucille", screens / "8-folder-added.png"
        )
        time.sleep(1)
        folders = f'menu 1 of menu item "Folders" of {MENU}'
        worked, said = _system_events(
            f'click {STATUS_ITEM}\ndelay 1\nclick menu item "Folders" of {MENU}\ndelay 1\n'
            f'click menu item "my-project-files" of {folders}\ndelay 1\n'
            f'click menu item "Remove" of menu 1 of menu item "my-project-files" of {folders}'
        )
        checker.expect(
            worked and _wait_until(lambda: "my-project-files" not in _roots(python), 15),
            "Folders, my-project-files, Remove runs `roots remove my-project-files`",
            f"{said} {_roots(python)}",
        )

        worked, said = _choose_folder(home, screens, "9-open-panel-home.png")
        if not checker.expect(worked, "the home folder can be chosen in the Open panel", said):
            return
        worked, buttons = _window_list("name of every button")
        _screenshot(screens / "10-confirm-home.png")
        checker.expect(
            worked and {"Choose a Smaller Folder", "Add Anyway"} <= set(buttons),
            "the app asks about the home folder before naming it",
            f"{buttons}",
        )
        worked, texts = _window_list("value of every static text")
        checker.expect(
            worked and any(text.startswith("This is your home folder.") for text in texts),
            "it says that this is the home folder, and what it holds",
            f"{texts}",
        )
        worked, said = _system_events('click button "Choose a Smaller Folder" of window 1\ndelay 2')
        worked, buttons = _panel_buttons()
        checker.expect(
            worked and OPEN_PROMPT in buttons and "Add Anyway" not in buttons,
            "Choose a Smaller Folder opens the Open panel again",
            f"{said} {buttons}",
        )
        _system_events("key code 53")
        time.sleep(2)
        checker.expect(
            str(home) not in _roots(python).values(), "cancelling the Open panel adds nothing"
        )

        worked, said = _choose_folder(home, screens, "11-open-panel-home.png")
        if not checker.expect(worked, "the home folder can be chosen again", said):
            return
        worked, said = _system_events('click button "Add Anyway" of window 1\ndelay 2')
        worked, alias = _name_dialog()
        checker.expect(
            worked and bool(alias) and str(home) not in _roots(python).values(),
            "Add Anyway goes on to the name dialog, and adds nothing yet",
            alias,
        )
        worked, said = _system_events('click button "Add Folder" of window 1')
        checker.expect(
            worked and _wait_until(lambda: _roots(python).get(alias) == str(home), 15),
            "Add Folder then runs `roots add` with --allow-home-or-root",
            f"{said} {_roots(python)}",
        )
        added.append(alias)
        _acknowledge(
            checker, "Once this Mac is connected to Lucille", screens / "12-home-added.png"
        )
    finally:
        for alias, path in _roots(python).items():
            if alias in added or path in (str(folder), str(home)):
                _app_host(python, ["roots", "remove", alias])
        shutil.rmtree(folder.parent, ignore_errors=True)


def _whole_disk_refused(checker: Checker, chosen: str, shot: Path) -> bool:
    """The app's front window is the note that refuses the whole disk: it has Choose a Smaller
    Folder and Cancel, and no button that adds the folder. That the note has those two buttons
    and no other is pinned in its source (``tests/test_macos_build.py``); a named button the
    window itself adds is printed, not failed. If the window is something else, such as the name
    dialog, say what it holds."""
    time.sleep(1)
    worked, buttons = _window_list("name of every button")
    _screenshot(shot)
    named = [button for button in buttons if button not in ("", "missing value")]
    print(f"the window after choosing {chosen} has the named buttons {named}")
    adding = [
        button
        for button in named
        if button not in WHOLE_DISK_BUTTONS and button.startswith(ADDING_BUTTONS)
    ]
    shown = worked and set(WHOLE_DISK_BUTTONS) <= set(named) and not adding
    checker.expect(
        shown,
        f"choosing {chosen} brings up a note with Choose a Smaller Folder and Cancel, and no "
        "button that adds it",
        f"{buttons}" if shown else f"{buttons}; a text field there holds {_name_field()[1]!r}",
    )
    worked, texts = _window_list("value of every static text")
    checker.expect(
        worked
        and WHOLE_DISK_TITLE in texts
        and any(text.startswith(WHOLE_DISK_TEXT) for text in texts),
        f"it says {WHOLE_DISK_TITLE!r}, and what the disk holds",
        f"{texts}",
    )
    return shown


def _drive_whole_disk(checker: Checker, python: Path, screens: Path) -> None:
    """Choose the Data volume, then ``/`` through a link to it: each time the app says it never
    adds the whole disk, with Choose a Smaller Folder, which goes back to the Open panel, and
    Cancel. Nothing is added."""
    before = _roots(python)
    data, disk = WHOLE_DISK_FOLDERS
    link = Path.home() / "lucille-host-ui-check-disk" / "Startup Disk"
    refused = {str(data), str(disk), str(link)}
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(disk, target_is_directory=True)
        worked, said = _choose_folder(data, screens, "13-open-panel-data.png")
        if not checker.expect(worked, f"{data} can be chosen in the Open panel", said):
            return
        if not _whole_disk_refused(checker, str(data), screens / "14-refuse-data.png"):
            return
        worked, said = _system_events('click button "Choose a Smaller Folder" of window 1\ndelay 2')
        worked, buttons = _panel_buttons()
        checker.expect(
            worked and OPEN_PROMPT in buttons and "Add Anyway" not in buttons,
            "Choose a Smaller Folder opens the Open panel again",
            f"{said} {buttons}",
        )
        worked, said = _go_to_folder(link, screens, "15-open-panel-disk.png")
        through = f"{disk}, through the link {link}"
        if not checker.expect(worked, f"{through}, can be chosen in the Open panel", said):
            return
        if not _whole_disk_refused(checker, through, screens / "16-refuse-disk.png"):
            return
        worked, said = _system_events('click button "Cancel" of window 1\ndelay 2')
        still, buttons = _panel_buttons()
        checker.expect(
            worked
            and not (still and {OPEN_PROMPT, "Add Folder", *WHOLE_DISK_BUTTONS} & set(buttons)),
            "Cancel closes the note, and opens nothing after it",
            f"{said} {buttons}",
        )
        after = _roots(python)
        checker.expect(
            after == before and not refused & set(after.values()),
            "neither is added",
            f"{after}",
        )
    finally:
        # Close whatever the app still shows, with its Cancel or else Escape (the Open panel's
        # Cancel may not be a button of window 1), and take out anything of the whole disk it
        # added.
        for _ in range(2):
            worked, _ = _system_events('click button "Cancel" of window 1\ndelay 1')
            if not worked:
                _system_events("set frontmost to true\nkey code 53\ndelay 1")
        for alias, path in _roots(python).items():
            if path in refused:
                _app_host(python, ["roots", "remove", alias])
        # The link alone, never what it leads to.
        if link.is_symlink():
            link.unlink()
        try:
            link.parent.rmdir()
        except OSError:
            pass


def _drive_code_window(checker: Checker, python: Path, screens: Path, logs: Sequence[Path]) -> None:
    """With a code-shaped text on the pasteboard, choose Connect to Lucille…: the code window opens
    with its field empty, and the field stays empty once it has the focus, so the app read nothing
    from the pasteboard. A typed word gets the hint, and Connect stays off. Command-V, typed
    through System Events, pastes exactly the text copied, which the app's own check passes;
    Connect hands it to the host on standard input, which refuses it as no code, and the app says
    so. Nothing is paired, and neither text reaches the app's standard output or error. Cancel
    closes the window, and the pasteboard is cleared."""
    pair = _pair_state(python)
    if not checker.expect(pair is not None, "status --json reads the app's profile"):
        return
    if pair != "none":
        print(f"note: the app's profile holds a pair ({pair}), so the code window was not tried")
        return
    _set_pasteboard(PASTEBOARD_SENTINEL)
    try:
        worked, said = _choose(CONNECT_ITEM.removesuffix("…"))
        opened = worked and _wait_until(lambda: CODE_TITLE in _window_names(), 15)
        _screenshot(screens / "17-code-window.png")
        if not checker.expect(
            opened, f"choosing {CONNECT_ITEM!r} opens the window {CODE_TITLE!r}", said
        ):
            return
        texts = _texts()
        checker.expect(
            all(any(words in text for text in texts) for words in CODE_WORDS),
            "it says where the code comes from, how to paste it, and never to paste one someone "
            "sent",
            f"{texts}",
        )
        checker.expect(
            _field() == (True, ""),
            "the code field opens empty: the app read nothing from the pasteboard",
            f"{_field()}",
        )
        worked, said = _system_events(
            "set frontmost to true\n" + _target("text field") + "set focused of theTarget to true"
        )
        time.sleep(1)
        checker.expect(
            worked and _field() == (True, ""),
            "it is still empty once it has the focus",
            f"{said} {_field()}",
        )
        checker.expect(
            _enabled(CONNECT_BUTTON) == "false",
            f"{CONNECT_BUTTON} is off while the field is empty",
            _enabled(CONNECT_BUTTON),
        )
        _system_events('set frontmost to true\nkeystroke "hello"')
        hinted = _wait_until(lambda: _says(CODE_HINT), 10)
        _screenshot(screens / "18-code-hint.png")
        checker.expect(
            hinted and _enabled(CONNECT_BUTTON) == "false",
            f"a typed word that is no code gets the hint {CODE_HINT!r}, and {CONNECT_BUTTON} "
            "stays off",
            f"{_texts()}",
        )
        # Take the word out with Backspace, then paste with Command-V, which the window forwards
        # to the field: the user's own paste, through the field.
        _system_events("set frontmost to true\nrepeat 20 times\nkey code 51\nend repeat")
        _set_pasteboard(PASTED_TEXT)
        _system_events('set frontmost to true\nkeystroke "v" using command down')
        pasted = _wait_until(lambda: _field() == (True, PASTED_TEXT), 10)
        _screenshot(screens / "19-code-pasted.png")
        checker.expect(
            pasted, "Command-V pastes into the field exactly the text copied", f"{_field()}"
        )
        checker.expect(
            _enabled(CONNECT_BUTTON) == "true",
            f"{CONNECT_BUTTON} is on once the field holds something shaped like a code",
            _enabled(CONNECT_BUTTON),
        )
        worked, said = _press(CONNECT_BUTTON)
        refused = worked and _wait_until(lambda: _says(BAD_CODE), 60)
        _screenshot(screens / "20-code-refused.png")
        checker.expect(
            refused,
            f"{CONNECT_BUTTON} hands it to the host, which refuses it as no code; the app says "
            f"{BAD_CODE!r}",
            f"{said} {_texts()}",
        )
        _press("OK")
        checker.expect(_pair_state(python) == "none", "the app's profile still holds no pair")
        logged = "".join(log.read_text(encoding="utf-8", errors="replace") for log in logs)
        checker.expect(
            PASTED_TEXT not in logged and PASTEBOARD_SENTINEL not in logged,
            "neither text reaches the app's standard output or standard error",
        )
    finally:
        _set_pasteboard("")
        if CODE_TITLE in _window_names():
            worked, said = _press("Cancel")
            checker.expect(
                worked and _wait_until(lambda: CODE_TITLE not in _window_names(), 10),
                "Cancel closes the code window",
                said,
            )


def _activity_texts() -> list[str]:
    """Every line the Activity window shows: its own words, then the lines of each box in it that
    scrolls (the requests, and the host's log while it shows). Empty when it is not open."""
    window = f"window {_quoted(ACTIVITY_TITLE)}"
    worked, said = _system_events(
        "set found to {}\n"
        f"try\nset found to found & (value of every static text of {window})\nend try\n"
        "try\n"
        f"repeat with theArea in scroll areas of {window}\n"
        "try\nset end of found to ((value of text area 1 of theArea) as text)\nend try\n"
        "end repeat\n"
        "end try\n"
        f"{LINES}return found as text"
    )
    return said.splitlines() if worked else []


def _activity_holds() -> str:
    """What the Activity window holds where :func:`_activity_texts` reads its boxes: the roles of
    the UI elements in each of its scroll areas, and of those in each of them. For an error, so the
    first run that cannot read a box says what macOS shows there instead (that a text view that
    cannot be selected is the scroll area's ``text area 1`` is untried on a Mac)."""
    worked, said = _system_events(
        'set AppleScript\'s text item delimiters to ", "\n'
        'set held to ""\n'
        "try\n"
        f"repeat with theArea in scroll areas of window {_quoted(ACTIVITY_TITLE)}\n"
        'set held to held & " [a scroll area: " & ((role of every UI element of theArea) as text)\n'
        "try\n"
        "repeat with theElement in UI elements of theArea\n"
        'set held to held & "; " & (role of theElement as text) & " holds: " '
        "& ((role of every UI element of theElement) as text)\n"
        "end repeat\n"
        "end try\n"
        'set held to held & "]"\n'
        "end repeat\n"
        "end try\n"
        "return held"
    )
    return f"; the window holds:{said}" if worked else f"; (System Events: {said})"


def _activity_buttons() -> list[str]:
    """The names of the Activity window's buttons, those of its title bar left out."""
    worked, said = _system_events(
        "set found to {}\n"
        f"repeat with theButton in buttons of window {_quoted(ACTIVITY_TITLE)}\n"
        "set theRole to missing value\n"
        "try\nset theRole to subrole of theButton\nend try\n"
        'if theRole is not in {"AXCloseButton", "AXMinimizeButton", "AXZoomButton", '
        '"AXFullScreenButton"} then\n'
        "set end of found to ((name of theButton) as text)\n"
        "end if\n"
        "end repeat\n"
        f"{LINES}return found as text"
    )
    return said.splitlines() if worked else [f"(System Events: {said})"]


def _close_window(title: str) -> tuple[bool, str]:
    """Click the close button of the app's window named ``title``."""
    return _system_events(
        f'click (first button of window {_quoted(title)} whose subrole is "AXCloseButton")'
    )


# Another application, as the checks of the app's one opener see it (decisions/0002 §20): System
# Settings, the Finder, and the process that draws macOS's question about a protected folder. The
# checks only read these, close a window they opened, and press a button they have just read; they
# send them no key.


def _process_windows(process: str) -> tuple[bool, list[str]]:
    """The name of every window of ``process``, another application, as System Events gives
    them; with what System Events said instead when it could not read them (``process`` not
    running, say)."""
    worked, said = _tell(process, f"{LINES}return (name of every window) as text")
    return worked, said.splitlines() if worked else [f"(System Events: {said})"]


def _windows_until(process: str, title: str, seconds: float) -> tuple[bool, list[str]]:
    """Read ``process``'s windows until one is named ``title``, for at most ``seconds``: whether
    one was, and the names last read."""
    seen: list[str] = []

    def named() -> bool:
        worked, names = _process_windows(process)
        seen[:] = names
        return worked and title in names

    return _wait_until(named, seconds), list(seen)


def _process_window_count(process: str) -> int | None:
    """How many windows ``process`` shows, one with no name included, which
    :func:`_process_windows` cannot show; None when System Events could not say."""
    worked, said = _tell(process, "return (count of windows) as text")
    return int(said) if worked and said.isdigit() else None


def _process_every(process: str, what: str) -> tuple[bool, list[str]]:
    """``what`` (``name of every button``, say) of every window of ``process``, wherever it sits
    (:func:`_in_each_place`), one per line; none when it has no window or is not running."""
    worked, said = _tell(
        process,
        "set found to {}\n"
        + _in_each_place(f"try\nset found to found & ({what} of {{place}})\nend try\n")
        + f"{LINES}return found as text",
        timeout=60,
    )
    return worked, [line for line in said.splitlines() if line] if worked else []


def _process_buttons(process: str) -> tuple[bool, list[str]]:
    """The name of every button of every window of ``process`` (:func:`_process_every`)."""
    return _process_every(process, "name of every button")


def _process_texts(process: str) -> tuple[bool, list[str]]:
    """Every static text of every window of ``process`` (:func:`_process_every`): the words of
    macOS's question, say."""
    return _process_every(process, "value of every static text")


def _press_in(process: str, name: str) -> tuple[bool, str]:
    """Click the button named ``name`` in whichever window of ``process`` holds it."""
    return _tell(
        process,
        "set theTarget to missing value\n"
        + _in_each_place(_click_in_place(name))
        + "if theTarget is missing value then error "
        + _quoted(f"no button named {name}")
        + '\nclick theTarget\nreturn "pressed"',
        timeout=60,
    )


def _close_window_of(process: str, title: str) -> tuple[bool, str]:
    """Click the close button of ``process``'s window named ``title``."""
    return _tell(
        process,
        f'click (first button of window {_quoted(title)} whose subrole is "AXCloseButton")',
    )


def _quit_by_name(name: str) -> bool:
    """Quit the application named ``name`` (System Settings), if it runs: SIGTERM, then SIGKILL
    if it still runs 15 seconds later. No key is sent, since a key goes to whichever window has
    the focus. True when nothing ran, or SIGTERM ended it."""
    if not _running(name):
        return True
    subprocess.run(["pkill", "-TERM", "-x", name], check=False)
    if _wait_until(lambda: not _running(name), 15):
        return True
    subprocess.run(["pkill", "-KILL", "-x", name], check=False)
    _wait_until(lambda: not _running(name), 10)
    return False


def _host_log(python: Path) -> list[str]:
    """The host's log on the app's profile, oldest first, as ``log --json --limit 200`` gives the
    app its lines; each cut as Activity cuts it (:data:`LONGEST_LOG_LINE`)."""
    lines: list[str] = []
    for line in _app_host(python, ["--json", "log", "--limit", "200"]).stdout.splitlines():
        try:
            found = json.loads(line)
        except ValueError:
            continue
        if isinstance(found, dict) and isinstance(found.get("text"), str):
            text = found["text"]
            if len(text) > LONGEST_LOG_LINE:
                text = text[: LONGEST_LOG_LINE - 1] + "…"
            lines.append(text)
    return lines


def _drive_activity(checker: Checker, python: Path, screens: Path) -> None:
    """Choose Activity…: its window says what it keeps, and for how long, and its one button is
    the disclosure; Show the Host's Log shows the host's own lines as it wrote them, under the
    words that say the log proves nothing. The window is then closed. That nothing in it can be
    selected, and that no request in it has a button, is checked in its source by the build
    tests; the pairing check reads its requests."""
    worked, said = _choose(ACTIVITY_ITEM.removesuffix("…"))
    opened = worked and _wait_until(lambda: ACTIVITY_TITLE in _window_names(), 15)
    _screenshot(screens / "16b-activity.png")
    if not checker.expect(
        opened, f"choosing {ACTIVITY_ITEM!r} opens the window {ACTIVITY_TITLE!r}", said
    ):
        return
    try:
        headed = _wait_until(lambda: ACTIVITY_HEADER in _activity_texts(), 10)
        checker.expect(
            headed,
            f"it says {ACTIVITY_HEADER!r}",
            f"{_activity_texts()}" + ("" if headed else _activity_holds()),
        )
        buttons = _activity_buttons()
        checker.expect(buttons == [SHOW_LOG], f"its one button is {SHOW_LOG!r}", f"{buttons}")
        worked, said = _press(SHOW_LOG)
        logged = _host_log(python)

        def shown() -> bool:
            texts = _activity_texts()
            if LOG_CAPTION not in texts:
                return False
            return any(line in texts for line in logged) if logged else LOG_EMPTY in texts

        found = worked and _wait_until(shown, 30)
        _screenshot(screens / "16c-activity-log.png")
        checker.expect(
            found,
            f"{SHOW_LOG} shows the host's log as it wrote it, under {LOG_CAPTION!r}",
            f"{said} the host's log has {len(logged)} lines; Activity: {_activity_texts()[-20:]}"
            + ("" if found else _activity_holds()),
        )
    finally:
        if ACTIVITY_TITLE in _window_names():
            worked, said = _close_window(ACTIVITY_TITLE)
            checker.expect(
                worked and _wait_until(lambda: ACTIVITY_TITLE not in _window_names(), 10),
                "its close button closes Activity",
                said,
            )


def _drive_reset(checker: Checker, python: Path, screens: Path) -> None:
    """Write a settings file the host cannot read (the check may write the host's files; the app
    never does): the menu says so in a line of its own, whose note offers Reset Settings…; that
    asks first, and the question's Reset Settings runs ``policy reset --json``, which sets the file
    aside as ``policy.invalid.toml`` and puts the fresh policy in its place, with only
    ``host.describe`` on and no folder, and the app says so; the note's OK runs nothing more. That
    Cancel is the question's default is checked in its source by the build tests. Whatever
    happens, the settings file, and a file an earlier reset set aside, are put back as they were,
    or taken away where there was none."""
    profile = _profile_folder()
    settings = profile / "policy.toml"
    aside = profile / "policy.invalid.toml"
    saved = {path: path.read_bytes() if path.is_file() else None for path in (settings, aside)}
    try:
        profile.mkdir(parents=True, exist_ok=True)
        aside.unlink(missing_ok=True)
        settings.write_bytes(UNREADABLE_POLICY)
        worked, shown = _menu_until(
            lambda shown: SETTINGS_UNREADABLE in shown,
            30,
            shot=screens / "21-settings-unreadable.png",
        )
        if not checker.expect(
            worked,
            f"with a settings file the host cannot read, the menu says {SETTINGS_UNREADABLE!r}",
            f"{shown}",
        ):
            return
        worked, said = _choose(SETTINGS_UNREADABLE)

        def buttons() -> list[str]:
            return _window_list("name of every button")[1]

        offered = worked and _wait_until(lambda: {RESET_ITEM, "OK"} <= set(buttons()), 15)
        _screenshot(screens / "22-reset-offered.png")
        if not checker.expect(
            offered,
            f"choosing it shows a note with {RESET_ITEM!r} and OK",
            f"{said} {buttons()} {_texts()}",
        ):
            return
        # The button by the start of its name, which spares the script its ellipsis, as _choose
        # does for a menu item.
        start = _quoted(RESET_ITEM.removesuffix("…"))
        worked, said = _system_events(
            f"click (first button of window 1 whose name starts with {start})\ndelay 2"
        )
        asked = worked and _wait_until(lambda: _says(RESET_QUESTION), 15)
        _screenshot(screens / "23-reset-question.png")
        shown = buttons()
        if not checker.expect(
            asked and {"Cancel", RESET_BUTTON} <= set(shown) and RESET_ITEM not in shown,
            f"{RESET_ITEM} asks first, {RESET_QUESTION!r}, with Cancel and {RESET_BUTTON}",
            f"{said} {shown} {_texts()}",
        ):
            return
        worked, said = _press(RESET_BUTTON)
        reset = worked and _wait_until(lambda: _says(RESET_TITLE), 40)
        _screenshot(screens / "24-reset-done.png")
        checker.expect(
            reset,
            f"{RESET_BUTTON} resets the settings, and the app says {RESET_TITLE!r}",
            f"{said} {_texts()}",
        )
        _press("OK")
        set_aside = aside.read_bytes() if aside.is_file() else None
        checker.expect(
            set_aside == UNREADABLE_POLICY,
            "the file the host could not read is set aside as policy.invalid.toml",
            f"{set_aside!r}",
        )
        listed = _json_lines(_app_host(python, ["--json", "ops", "list"]).stdout)
        enabled = [
            found.get("name")
            for found in listed
            if isinstance(found, dict)
            and found.get("type") == "op"
            and found.get("enabled") is True
        ]
        folders = _roots(python)
        checker.expect(
            enabled == ["host.describe"] and not folders,
            "the fresh policy has only host.describe on, and no folder",
            f"{enabled} {folders}",
        )
        worked, shown = _menu_until(lambda shown: SETTINGS_UNREADABLE not in shown, 30)
        checker.expect(
            worked, "the menu no longer says the settings file can't be read", f"{shown}"
        )
        # The note's OK only closed it. Had it run the verb again, the host would have found
        # nothing to set aside, and by now the app would say so.
        texts = _texts()
        checker.expect(
            not any(NOTHING_TO_RESET in text for text in texts),
            "the note's OK closes it, and runs nothing more",
            f"{texts}",
        )
    finally:
        # Close whatever the app still shows: the question with its Cancel, a note with its OK.
        for button in ("Cancel", "OK"):
            _press(button)
        for path, held in saved.items():
            if held is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(held)


def _documents_check_folder() -> Path:
    """The folder the ui check's last step makes in Documents, a folder macOS protects."""
    return Path.home() / "Documents" / "lucille-host-ui-check-documents"


# The host's ``roots add`` on the app's profile, as the app starts it (App/HostCLI.swift and
# HostText.addFolderArguments): what the Documents step waits on, and ends if it is left running.
ADDING_A_FOLDER: Final = f"lucille_host --profile {APP_PROFILE} --json roots add"
# How long the Documents step lets a file operation in Documents take, in a child: the check's
# own process may be asked about that folder too, and nobody would answer.
DOCUMENTS_FILE_LIMIT: Final = 15
# The ui step's limit in CI, and what check_ui keeps of it for its own end (the app stopped, 15 s
# at most, and its logs printed) and its start. The Documents step ends by what is left, its
# clean-up included, so that a step that fails nothing can never time out the ui step: that would
# fail a gated step, and kill the check before it put anything back (tests/test_ci_workflow.py
# holds the limit to ci.yml's). Its clean-up is given the last DOCUMENTS_TIDY_TIME seconds of it,
# and it starts only with DOCUMENTS_LEAST_TIME seconds more than that left.
UI_STEP_LIMIT: Final = 15 * 60
UI_STEP_END: Final = 60
DOCUMENTS_TIDY_TIME: Final = 240
DOCUMENTS_LEAST_TIME: Final = 120


# How the Documents step's notes name its clean-up.
TIDY: Final = "the Documents step's clean-up"


class _OutOfTime(Exception):
    """What :func:`_until` raises in what it stops."""


@contextlib.contextmanager
def _until(deadline: float | None) -> Iterator[None]:
    """Stop what runs inside at ``deadline``, a ``time.monotonic()`` time, by raising
    :class:`_OutOfTime` there from SIGALRM, whatever it waits on: ``subprocess.run`` kills the
    child it waits for as the exception passes. It rings once, and the timer and the handler
    before it are put back on the way out. With no deadline, it stops nothing."""
    if deadline is None:
        yield
        return
    rung: list[bool] = []

    def ring(signum: int, frame: Any) -> None:
        if not rung:
            rung.append(True)
            raise _OutOfTime("it reached the end of the time it was given")

    previous = signal.signal(signal.SIGALRM, ring)
    signal.setitimer(signal.ITIMER_REAL, max(deadline - time.monotonic(), 0.001))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _adding_a_folder() -> list[int]:
    """The host's ``roots add`` processes on the app's profile, whoever started them."""
    done = subprocess.run(
        ["pgrep", "-f", ADDING_A_FOLDER], capture_output=True, text=True, check=False
    )
    return [int(pid) for pid in done.stdout.split()]


def _in_documents(argv: list[str]) -> tuple[bool | None, str]:
    """Run ``argv``, which acts on the Documents step's folder, in a child for at most
    :data:`DOCUMENTS_FILE_LIMIT` seconds: whether it exited 0, or None when it did not finish, and
    what it said."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=DOCUMENTS_FILE_LIMIT, check=False
        )
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} did not finish within {DOCUMENTS_FILE_LIMIT} s"
    return done.returncode == 0, _shown(done)


def _names_the_app(texts: Sequence[str]) -> bool:
    """Whether a question of macOS's, by its words, is about the app or its interpreter."""
    pins = build_app.load_pins()
    names = (APP_NAME, Path(build_app.INTERPRETER).name, pins.bundle_id, pins.runtime_id)
    return any(name in text for text in texts for name in names)


def _stop_adding_a_folder() -> None:
    """Wait up to 30 seconds for no ``roots add`` of the app's profile to run, then end any still
    running, with SIGTERM and then SIGKILL, and say which: one left waiting on macOS's question
    would hold the profile's policy lock for every check after this one."""
    if _wait_until(lambda: not _adding_a_folder(), 30):
        return
    print(f"note: roots add still runs after the Documents step: {_adding_a_folder()}", flush=True)
    subprocess.run(["pkill", "-TERM", "-f", ADDING_A_FOLDER], check=False)
    if not _wait_until(lambda: not _adding_a_folder(), 10):
        subprocess.run(["pkill", "-KILL", "-f", ADDING_A_FOLDER], check=False)
        _wait_until(lambda: not _adding_a_folder(), 10)
    print(f"roots add left running after SIGTERM and SIGKILL: {_adding_a_folder()}", flush=True)


def _drive_documents_prompt(python: Path, screens: Path, deadline: float | None = None) -> None:
    """Best effort, and it fails nothing: add a folder in Documents, which macOS protects
    (decisions/0002 §20). It chooses Add a Folder…, and types the folder's path only once it has
    read the Open panel's own button. If macOS asks whether the app may use the folder, the step
    prints the question's words and buttons, and answers Don't Allow, the one button of it this
    step presses, only once it has read that button there, words that name the app or its
    interpreter, and that the process that draws the question shows that one window: the host is
    refused the folder, the app says macOS is blocking it and offers Open Privacy Settings, which
    the step presses once it has read it, and it prints the windows System Settings then shows. If
    no question comes, as on a runner where it was answered before, it says so, and closes the
    app's note by its OK, once read. Each of the app's buttons is pressed in the window it was read
    in. What macOS asks on a runner is not known, so nothing here is a gate. Whatever happens,
    each clean-up on its own: where the app still shows its Cancel (the folder still being
    checked), the step presses it, once read; a ``roots add`` of the app's profile still running
    30 seconds later is ended, so that it holds no lock; it prints the windows
    UserNotificationCenter still shows, and says so when a question of macOS's is left on screen,
    which it closes no other way than by the answer above; System Settings is quit if this step
    started it; every folder alias the host has for the folder is removed through the command
    line; the folder made here is removed; and, if macOS asked, its answer is taken back with
    ``tccutil reset``, for the app's and its interpreter's identifiers alone, and what tccutil
    said is printed. A folder of that name already there is someone's: the step leaves it, and
    adds nothing. The folder is tested for, made and removed in a child with a time limit, never
    by the check's own process. With ``deadline`` (check_ui's, from the ui step's limit), the step
    ends by then, its clean-up included (:func:`_until`): it starts only with
    :data:`DOCUMENTS_TIDY_TIME` and :data:`DOCUMENTS_LEAST_TIME` seconds left, it is stopped
    :data:`DOCUMENTS_TIDY_TIME` seconds before the deadline, and what of its clean-up the deadline
    leaves undone is printed."""
    if deadline is not None:
        left = deadline - time.monotonic()
        if left < DOCUMENTS_TIDY_TIME + DOCUMENTS_LEAST_TIME:
            print(
                f"note: too little of the ui step's time is left for the Documents step "
                f"({left:.0f} s), so it added nothing",
                flush=True,
            )
            return
    folder = _documents_check_folder()
    there, said = _in_documents(["/bin/test", "-e", str(folder)])
    if there is not False:
        why = "is there already" if there else f"could not be looked for ({said})"
        print(f"note: {folder} {why}, so the Documents step added nothing", flush=True)
        return
    made, said = _in_documents(["/bin/mkdir", "-p", str(folder)])
    if not made:
        print(f"note: the Documents step could not make {folder}: {said}", flush=True)
        return
    settings_ran = bool(_running(SYSTEM_SETTINGS))
    added = False
    asked_once = False
    closed = False
    try:
        with _until(None if deadline is None else deadline - DOCUMENTS_TIDY_TIME):
            worked, said = _ask_to_add_a_folder()
            if not worked:
                print(
                    f"note: the Documents step could not choose Add a Folder…: {said}", flush=True
                )
                return
            if not _wait_until(lambda: OPEN_PROMPT in _panel_buttons()[1], 10):
                print(
                    f"note: no Open panel with {OPEN_PROMPT!r} came, so the Documents step typed "
                    "nothing",
                    flush=True,
                )
                return
            worked, said = _go_to_folder(folder, screens, "25-documents-panel.png")
            if not worked:
                print(f"note: the Documents step could not choose {folder}: {said}", flush=True)
                return
            worked, alias = _name_dialog()
            _, buttons = _window_list("name of every button")
            print(f"the name dialog for {folder.name}: {alias!r}, with {buttons}", flush=True)
            if not worked or "Add Folder" not in buttons:
                print("note: no name dialog with Add Folder, so the Documents step added nothing")
                return
            added = True
            _press_in_front("Add Folder")
            asked: list[str] = []

            def asks() -> bool:
                worked, found = _process_buttons(TCC_PROMPT_PROCESS)
                asked[:] = found if worked else []
                return bool(asked)

            answered = False
            if not _wait_until(asks, 20):
                print("no macOS prompt on this runner", flush=True)
                _screenshot(screens / "26-documents-no-prompt.png")
            else:
                # Taken back afterwards however it went: macOS asked, so it had no answer before.
                asked_once = True
                _, question = _process_texts(TCC_PROMPT_PROCESS)
                print(f"macOS asked {question}, with the buttons {asked}", flush=True)
                _screenshot(screens / "26-documents-prompt.png")
                answer = next(
                    (name for name in asked if name.replace("’", "'") == DONT_ALLOW), None
                )
                if answer is None:
                    print(f"note: macOS's question has no {DONT_ALLOW!r}, so nothing was pressed")
                    return
                if not _names_the_app(question):
                    print(
                        "note: macOS's question names neither the app nor its interpreter, so "
                        "nothing was pressed",
                        flush=True,
                    )
                    return
                # Its words and buttons were read from every window of the process, and the
                # press finds its button in any: only with one window are they all that one's.
                count = _process_window_count(TCC_PROMPT_PROCESS)
                if count != 1:
                    shown = f"shows {count} windows, not one"
                    if count is None:
                        shown = "could not count its windows"
                    print(
                        f"note: {TCC_PROMPT_PROCESS} {shown}, so nothing was pressed: the press "
                        "might answer another question",
                        flush=True,
                    )
                    return
                answered = True
                worked, said = _press_in(TCC_PROMPT_PROCESS, answer)
                print(f"{DONT_ALLOW}: {said}", flush=True)
            if not _wait_until(lambda: _says(BLOCKED_BY_MACOS), 30 if answered else 5):
                closes = _wait_until(lambda: "OK" in _window_list("name of every button")[1], 15)
                print(f"the app then says: {_texts()}", flush=True)
                if closes:
                    closed = _press_in_front("OK")[0]
                return
            _, buttons = _window_list("name of every button")
            _screenshot(screens / "27-documents-blocked.png")
            print(f"the app says {BLOCKED_BY_MACOS!r}, with the buttons {buttons}", flush=True)
            if OPEN_PRIVACY_SETTINGS not in buttons:
                print(f"note: that note does not offer {OPEN_PRIVACY_SETTINGS!r}", flush=True)
                if "OK" in buttons:
                    closed = _press_in_front("OK")[0]
                return
            closed, said = _press_in_front(OPEN_PRIVACY_SETTINGS)
            title = SETTINGS_PANES[FILES_AND_FOLDERS_PANE]
            shown, names = _windows_until(SYSTEM_SETTINGS, title, 30)
            _screenshot(screens / "28-documents-privacy-settings.png")
            print(
                f"{OPEN_PRIVACY_SETTINGS} ({said}): System Settings windows: {names}; {title!r} "
                f"among them: {shown}",
                flush=True,
            )
    except Exception as exc:  # it fails nothing: what stopped it is printed
        print(f"note: the Documents step stopped: {exc}", flush=True)
    finally:
        _tidy_documents_step(
            python,
            folder,
            pending=added and not closed,
            added=added,
            asked=asked_once,
            settings_ran=settings_ran,
            deadline=deadline,
        )


def _tidy_documents_step(
    python: Path,
    folder: Path,
    *,
    pending: bool,
    added: bool,
    asked: bool,
    settings_ran: bool,
    deadline: float | None = None,
) -> None:
    """What :func:`_drive_documents_prompt` puts back, each part on its own, so that one that
    stops leaves the others to run, and what stopped it is printed. ``pending`` is whether the app
    may still be adding the folder, with no note the step closed; ``added``, whether the step
    pressed Add Folder; ``asked``, whether macOS asked; ``deadline``, when it must have ended: a
    part still running then is stopped, and each part after it is printed as not done."""
    left_on_screen: list[bool] = []

    def cancel() -> None:
        worked, buttons = _window_list("name of every button")
        if worked and "Cancel" in buttons:
            print(f"the app still shows {buttons}: pressing its Cancel", flush=True)
            _press_in_front("Cancel")

    def prompt_windows() -> None:
        count = _process_window_count(TCC_PROMPT_PROCESS)
        _, names = _process_windows(TCC_PROMPT_PROCESS)
        counted = "which could not be counted" if count is None else f"{count} in all"
        print(f"{TCC_PROMPT_PROCESS} then shows the windows {names}, {counted}", flush=True)
        if count:
            # Nothing here closes it: the step answers only a question it has read to be the
            # app's, alone on the screen. What macOS was asked is taken back all the same.
            left_on_screen.append(True)
            print(
                "note: a question of macOS's is left on screen, which the Documents step did not "
                "answer and does not close",
                flush=True,
            )

    def settings() -> None:
        if settings_ran:
            print(f"{SYSTEM_SETTINGS} ran before the Documents step, which leaves it", flush=True)
        else:
            _quit_by_name(SYSTEM_SETTINGS)

    def aliases() -> None:
        for alias, path in _roots(python).items():
            if path == str(folder):
                _app_host(python, ["roots", "remove", alias])

    def remove() -> None:
        removed, said = _in_documents(["/bin/rm", "-rf", str(folder)])
        if not removed:
            print(f"note: the Documents step could not remove {folder}: {said}", flush=True)

    def answers() -> None:
        if not (asked or left_on_screen):
            return
        pins = build_app.load_pins()
        # The app's and its interpreter's: which of the two macOS asks for is not yet known
        # (decisions/0002 §12). Whether tccutil does this with no root is not yet seen on a Mac.
        for identifier in (pins.bundle_id, pins.runtime_id):
            done = subprocess.run(
                ["tccutil", "reset", DOCUMENTS_SERVICE, identifier],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            print(f"tccutil reset {DOCUMENTS_SERVICE} {identifier}: {_shown(done)}", flush=True)

    parts = [("press the app's Cancel", cancel)] if pending else []
    parts += [("end roots add", _stop_adding_a_folder)]
    parts += [(f"read {TCC_PROMPT_PROCESS}'s windows", prompt_windows)] if added else []
    parts += [
        (f"quit {SYSTEM_SETTINGS}", settings),
        ("remove the folder's alias", aliases),
        ("remove the folder", remove),
    ]
    parts += [("take macOS's answer back", answers)] if added else []
    try:
        with _until(deadline):
            for what, part in parts:
                if deadline is not None and time.monotonic() >= deadline:
                    print(f"note: {TIDY} had no time left to {what}", flush=True)
                    continue
                try:
                    part()
                except Exception as exc:  # each on its own: what stopped it is printed
                    print(f"note: {TIDY} could not {what}: {exc}", flush=True)
    except _OutOfTime as exc:
        print(f"note: {TIDY} stopped: {exc}", flush=True)


def check_ui(app: Path, checker: Checker, work: Path, require_driven: bool = False) -> None:
    """Open the app and drive it the way a user would, through System Events: read the note that
    says what it is for and where its menu is, when it opens with no confirmed pair (with one, it
    shows none) and when it is opened again; read the menu; choose Pause Lucille and Resume
    Lucille, and watch the host's pause flag; switch an ability on and off; add a folder through
    the Open panel and the name dialog, and remove it from the Folders submenu; add the home
    folder, which the app asks about first; choose the Data volume, and ``/`` through a link to it,
    which the app refuses; open Activity, and the host's log in it (:func:`_drive_activity`); open
    the code window and paste into it (:func:`_drive_code_window`); reset a settings file the host
    cannot read (:func:`_drive_reset`); and, last, best effort and failing nothing, add a folder in
    Documents, and answer macOS's question about it if one comes (:func:`_drive_documents_prompt`).
    Each step but that last is checked against the host's own state, on the app's own profile. It
    uses the real HOME, and leaves the host as it found it. Where macOS does not let System Events
    drive other apps (no accessibility permission), it says so and drives nothing; with
    ``require_driven`` that is a failure. Screenshots go to ``<out>/check/screens``."""
    name = build_app.load_pins().app_name
    started = time.monotonic()
    screens = work / "screens"
    screens.mkdir(parents=True, exist_ok=True)
    flag = _profile_folder() / "state" / "paused"
    _stop_earlier_copies(checker, name)
    out_log = work / "app-stdout.txt"
    err_log = work / "app-stderr.txt"
    for log in (out_log, err_log):
        log.write_text("", encoding="utf-8")
    # Whether the app opens paired, which decides whether it says where its menu is at launch.
    paired = _pair_state(python_in(app)) == "confirmed"
    print(f"the app's profile holds a confirmed pair: {paired}", flush=True)
    done = subprocess.run(
        ["open", "--stdout", str(out_log), "--stderr", str(err_log), str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    checker.expect(done.returncode == 0, "open starts the app", _shown(done))
    if not checker.expect(_wait_until(lambda: bool(_running(name)), 15), "the app is running"):
        return
    time.sleep(5)
    try:
        driven = _can_drive()
        if driven:
            _drive_welcome(checker, screens, app, paired=paired)
            _screenshot(screens / "1-menu-bar.png")
            driven = _drive_menu(checker, screens, flag)
        if driven:
            _drive_abilities(checker, python_in(app), screens)
            _drive_folders(checker, python_in(app), screens)
            _drive_whole_disk(checker, python_in(app), screens)
            _drive_activity(checker, python_in(app), screens)
            _drive_code_window(checker, python_in(app), screens, (out_log, err_log))
            _drive_reset(checker, python_in(app), screens)
            _drive_documents_prompt(python_in(app), screens, started + UI_STEP_LIMIT - UI_STEP_END)
        elif require_driven:
            checker.expect(False, "System Events drove the menu, as --require-driven asks")
    finally:
        subprocess.run(["pkill", "-TERM", "-x", name], check=False)
        checker.expect(_wait_until(lambda: not _running(name), 15), "the app stops on SIGTERM")
        for log in (out_log, err_log):
            text = log.read_text(encoding="utf-8", errors="replace")
            print(f"the app's {log.stem.removeprefix('app-')}: {text[-5000:]!r}")


def _folder_state(folder: Path) -> tuple[bool, dict[str, tuple[int, int, int]]]:
    """Whether ``folder`` is there, and :func:`file_listing` of it."""
    return folder.exists(), file_listing(folder)


def _profile_processes_seen(seconds: float, *, first: bool) -> set[int]:
    """The host's processes on the app's profile seen in ``seconds``, asked every tenth of a
    second: a short verb may end within one. With ``first``, only until one is seen."""
    seen: set[int] = set()
    deadline = time.monotonic() + seconds
    while True:
        seen.update(_profile_processes())
        if (first and seen) or time.monotonic() >= deadline:
            return seen
        time.sleep(0.1)


def _stop_the_app(checker: Checker, what: str) -> None:
    """Stop the running copy of the app with SIGTERM, if one runs, and wait for the host's
    processes on its profile to end."""
    if _running(APP_NAME):
        subprocess.run(["pkill", "-TERM", "-x", APP_NAME], check=False)
        checker.expect(_wait_until(lambda: not _running(APP_NAME), 15), f"{what} stops on SIGTERM")
    checker.expect(
        _wait_until(lambda: not _profile_processes(), 30),
        "no host process of the app's profile runs",
        f"{_profile_processes()}",
    )


def _placement_control(checker: Checker, screens: Path, require_driven: bool) -> bool:
    """The control: the copy at :func:`installed_app_path`, opened, starts the host on the app's
    profile, which :func:`_profile_processes` sees, and says where its menu is, not where it
    should be; while the profile holds a confirmed pair, it says neither (decisions/0002, What is
    built today). Without it, seeing no host process later would prove nothing. Whether System
    Events can drive the app here; with ``require_driven``, not being able to is a failure."""
    installed = installed_app_path()
    _stop_earlier_copies(checker, APP_NAME)
    checker.expect(
        _wait_until(lambda: not _profile_processes(), 30),
        "no host process of the app's profile runs before the control",
        f"{_profile_processes()}",
    )
    paired = _pair_state(python_in(installed)) == "confirmed"
    print(f"the app's profile holds a confirmed pair: {paired}", flush=True)
    before = _folder_state(_profile_folder())
    done = subprocess.run(["open", str(installed)], capture_output=True, text=True, check=False)
    checker.expect(
        done.returncode == 0 and _wait_until(lambda: bool(_running(APP_NAME)), 15),
        f"the control: open starts the copy at {installed}",
        _shown(done),
    )
    seen = _profile_processes_seen(30, first=True)
    checker.expect(
        bool(seen),
        "the control: it starts the host on the app's profile, and the check sees it",
        "no host process of the app's profile was seen in 30 s",
    )
    time.sleep(3)
    driven = _can_drive()
    if driven:
        welcomed = _wait_until(lambda: _says(WELCOME_TITLE), 20)
        _screenshot(screens / "0-placement-control.png")
        said = (
            f"opened while paired, it says neither {WELCOME_TITLE!r} nor {PLACEMENT_TITLE!r}"
            if paired
            else f"it says {WELCOME_TITLE!r}, and not {PLACEMENT_TITLE!r}"
        )
        right = welcomed != paired and not _says(PLACEMENT_TITLE)
        checker.expect(right, said, "" if right else f"{_texts()}")
    elif require_driven:
        checker.expect(False, "System Events drove the app, as --require-driven asks")
    _stop_the_app(checker, "the copy in Applications")
    changed = _folder_state(_profile_folder()) != before
    print(f"the copy in Applications changed the app's profile folder: {changed}")
    return driven


def _refusing_copy(
    copy: Path,
    checker: Checker,
    screens: Path,
    label: str,
    words: tuple[str, str],
    driven: bool,
    *,
    shows_folder: bool = False,
    require_driven: bool = False,
) -> None:
    """Open ``copy``, which is not the copy at /Applications/<name>.app, with no other copy of
    the app running. For :data:`REFUSAL_WATCH` seconds it starts no host process on the app's
    profile and changes nothing in the profile's folder; it says ``words``, a title and a sentence
    of its note, which offers only Quit, and Quit quits it. With ``shows_folder``, for a copy the
    app judges not to be in the Applications folder, the note offers only Show Applications Folder
    and Quit, in either order, which is printed as read, and Show Applications Folder shows that
    folder and quits it (:func:`_shows_applications_folder`). A button is pressed only once the
    check has read it on the note. Screenshots ``0-placement-*``."""
    _stop_the_app(checker, "a copy of the app")
    folder = _profile_folder()
    before = _folder_state(folder)
    done = subprocess.run(["open", str(copy)], capture_output=True, text=True, check=False)
    if not checker.expect(
        done.returncode == 0 and _wait_until(lambda: bool(_running(APP_NAME)), 15),
        f"open starts {copy}",
        _shown(done),
    ):
        return
    seen = _profile_processes_seen(REFUSAL_WATCH, first=False)
    checker.expect(
        not seen and bool(_running(APP_NAME)),
        f"for {REFUSAL_WATCH} s it runs, and starts no host process of the app's profile",
        f"seen: {sorted(seen)}; still running: {_running(APP_NAME)}; now: {_profile_processes()}",
    )
    checker.expect(
        _folder_state(folder) == before,
        f"nothing in {folder} changed",
        "\n".join(listing_changes(before[1], _folder_state(folder)[1])[:50]),
    )
    title, sentence = words
    if driven:
        shown = _wait_until(lambda: _says(title), 20)
        _screenshot(screens / f"0-placement-{label}.png")
        checker.expect(
            shown and _says(sentence), f"it says {title!r}, and {sentence!r}", f"{_texts()}"
        )
        worked, buttons = _window_list("name of every button")
        offered = [SHOW_APPLICATIONS_FOLDER, QUIT_BUTTON] if shows_folder else [QUIT_BUTTON]
        # In which order System Events lists them is not yet seen on a Mac (the Swift's is the
        # build test's to hold): either is taken, and the first runs print it.
        print(f"its note's buttons, as System Events lists them: {buttons}", flush=True)
        right = worked and sorted(buttons) == sorted(offered)
        checker.expect(
            right,
            "its note offers only " + " and ".join(repr(button) for button in offered),
            "" if right else f"{buttons}",
        )
        if shows_folder:
            if worked and SHOW_APPLICATIONS_FOLDER in buttons:
                _shows_applications_folder(checker, screens, label, require_driven)
        elif worked and QUIT_BUTTON in buttons:
            worked, said = _press(QUIT_BUTTON)
            checker.expect(
                worked and _wait_until(lambda: not _running(APP_NAME), 15),
                f"{QUIT_BUTTON} quits it",
                said,
            )
    else:
        _screenshot(screens / f"0-placement-{label}.png")
        print(f"note: System Events cannot read the app's note here, so {title!r} was not read")
    _stop_the_app(checker, "it")


def _shows_applications_folder(
    checker: Checker, screens: Path, label: str, require_driven: bool
) -> None:
    """Press Show Applications Folder, which the note of a copy the app judges not to be in the
    Applications folder has just been read to offer: the app quits, and the Finder shows a window
    named Applications, whose windows the check prints. That window's name is not yet seen on a
    Mac, so only with ``require_driven`` (macos-15) is it a gate; elsewhere it is printed. Where a
    window of that name was open before, its name proves nothing, and the check says so. Each
    Finder window then under a name that was not open before the press, whatever that name, is
    closed by its own close button, and the check prints each one it closed; one under a name open
    before is left open, and where the windows before the press could not be read, none is
    closed."""
    read, before = _process_windows(FINDER)
    was_open = read and APPLICATIONS_WINDOW in before
    worked, said = _press(SHOW_APPLICATIONS_FOLDER)
    quits = worked and _wait_until(lambda: not _running(APP_NAME), 15)
    checker.expect(
        quits, f"{SHOW_APPLICATIONS_FOLDER} quits it", "" if quits else f"{said} {_texts()}"
    )
    shown, after = _windows_until(FINDER, APPLICATIONS_WINDOW, 15)
    print(f"the Finder's windows after {SHOW_APPLICATIONS_FOLDER}: {after}", flush=True)
    _screenshot(screens / f"0-placement-{label}-folder.png")
    what = f"then a Finder window named {APPLICATIONS_WINDOW!r} shows the folder"
    if was_open:
        print(
            f"note: a Finder window named {APPLICATIONS_WINDOW!r} was open before the press, so "
            "its name proves nothing here",
            flush=True,
        )
    if require_driven:
        checker.expect(shown, what, "" if shown else f"before: {before}; after: {after}")
    else:
        print(f"recorded, not a gate: {'yes' if shown else 'no'}, {what}", flush=True)
    # Read again: what a wait that timed out read last may be System Events' error. Where the
    # windows open before could not be read, none is closed, since any might be someone's.
    read_after, now = _process_windows(FINDER)
    opened = [name for name in now if name not in before] if read and read_after else []
    if not (read and read_after):
        print(f"note: the Finder's windows could not be read, so none was closed: {now}")
    for name in opened:
        try:
            _, said = _close_window_of(FINDER, name)
        except ValueError as exc:  # a name AppleScript cannot quote here
            said = str(exc)
        print(f"closing the Finder's window {name!r}, which the press opened: {said}", flush=True)


# How long the places check lets ``/usr/bin/open`` take, and the most one page can take by the
# check's own limits: quitting System Settings (SIGTERM, 15 s, then SIGKILL, 10 s) before and
# after, ``open``, 30 s for System Settings to start, 30 s for its window, then a System Events read
# of up to 30 s begun just before those end and the one more read :func:`_wait_until` makes once
# its time is up, and a screenshot of up to 30 s. CI gives the step more than two pages' worth
# (tests/test_ci_workflow.py checks).
OPEN_LIMIT: Final = 60
PLACES_PAGE_LIMIT: Final = 25 + OPEN_LIMIT + 30 + 30 + 30 + 30 + 30 + 25


def check_places(checker: Checker, work: Path, record_only: bool = False) -> None:
    """The System Settings pages the app's one opener may show (:data:`SETTINGS_PANES`,
    decisions/0002 §20), each opened by the check, not the app, with ``/usr/bin/open``, the way
    the opener hands its URL to macOS: System Settings starts, and shows a window with the title
    the page should have. It prints the names of System Settings' windows, so that the first run
    on each macOS says what each page is called, and takes a screenshot, ``places-<n>.png``. Then
    it quits System Settings by its name (:func:`_quit_by_name`), however the page went, and sends
    it no key. System Settings is quit before each page too, so that the window read is the
    page's: one already running when the check starts, someone's own, is quit, and the check says
    so first. ``open`` is given :data:`OPEN_LIMIT` seconds. It needs no app. With ``record_only``
    (the smoke jobs, on macOS 26 and 27) it prints what it would have checked, and fails nothing,
    whatever it saw or whatever stopped it: the pages' spellings and titles are not yet seen on a
    Mac, and macos-15 gates them. At worst, each page takes :data:`PLACES_PAGE_LIMIT` seconds."""
    screens = work / "screens"
    screens.mkdir(parents=True, exist_ok=True)
    if _running(SYSTEM_SETTINGS):
        print(
            f"note: {SYSTEM_SETTINGS} was running before the places check, which quits it now",
            flush=True,
        )

    def expect(ok: bool, what: str, detail: str) -> bool:
        if record_only:
            shown = f"\n{detail}" if detail else ""
            print(f"recorded, not a gate: {'yes' if ok else 'no'}, {what}{shown}", flush=True)
            return ok
        return checker.expect(ok, what, detail)

    for number, (url, title) in enumerate(SETTINGS_PANES.items(), start=1):
        started = False
        try:
            _quit_by_name(SYSTEM_SETTINGS)
            try:
                done = subprocess.run(
                    ["/usr/bin/open", url],
                    capture_output=True,
                    text=True,
                    timeout=OPEN_LIMIT,
                    check=False,
                )
                opened, said = done.returncode == 0, _shown(done)
            except subprocess.TimeoutExpired:
                opened, said = False, f"/usr/bin/open did not finish within {OPEN_LIMIT} s"
            started = opened and _wait_until(lambda: bool(_running(SYSTEM_SETTINGS)), 30)
            said = "" if started else said
            if not expect(started, f"open {url} starts System Settings", said):
                continue
            shown, names = _windows_until(SYSTEM_SETTINGS, title, 30)
            print(f"{url}: System Settings windows: {names}", flush=True)
            _screenshot(screens / f"places-{number}.png")
            expect(shown, f"System Settings shows {title!r}", "" if shown else f"{names}")
        except Exception as exc:
            if not record_only:
                raise
            print(f"recorded, not a gate: {url} could not be checked: {exc}", flush=True)
        finally:
            try:
                quits = _quit_by_name(SYSTEM_SETTINGS)
            except Exception as exc:
                if not record_only:
                    raise
                print(f"recorded, not a gate: quitting System Settings: {exc}", flush=True)
                quits = False
            if started:
                said = "" if quits else "it still ran 15 s after SIGTERM, and was killed"
                expect(quits, "System Settings quits when asked by its name, with SIGTERM", said)


def check_placement(app: Path, checker: Checker, work: Path, require_driven: bool = False) -> None:
    """Only the copy at /Applications/<name>.app starts anything (decisions/0002 §13). First the
    control: that copy, opened, starts the host (:func:`_placement_control`). Then, one at a time
    with no other copy running, ``app`` (the build's copy, in a folder of its own), and a copy in
    /Applications named as the Finder's Keep Both names a second one, with the copy at the app's
    own name there and then set aside: each says why it can't run, starts no host process and
    changes nothing in the app's profile (:func:`_refusing_copy`). The build's copy offers only
    Show Applications Folder and Quit, and Show Applications Folder shows that folder in the
    Finder and quits; the renamed copy, in that folder already, offers only Quit. Whatever
    happens, the copy set aside goes back and the renamed one is removed. Screenshots go to
    ``<out>/check/screens``."""
    screens = work / "screens"
    screens.mkdir(parents=True, exist_ok=True)
    installed = installed_app_path()
    if not checker.expect(
        installed.is_dir(), f"{installed} is there", "check_app.py install puts it there"
    ):
        return
    driven = _placement_control(checker, screens, require_driven)
    words = (PLACEMENT_TITLE, PLACEMENT_ELSEWHERE)
    outside = dict(shows_folder=True, require_driven=require_driven)
    _refusing_copy(app, checker, screens, "elsewhere", words, driven, **outside)
    renamed = installed.with_name(f"{RENAMED_COPY}.app")
    # Beside the installed copy, so that it moves within /Applications, and not ending in .app, so
    # that macOS does not take it for an app.
    aside = installed.with_name(f".{installed.name}.set-aside-by-placement")
    if not checker.expect(
        not renamed.exists() and not aside.exists(), f"neither {renamed} nor {aside} is there yet"
    ):
        return
    try:
        done = subprocess.run(
            ["ditto", str(app), str(renamed)], capture_output=True, text=True, check=False
        )
        if checker.expect(done.returncode == 0, f"ditto copies the app to {renamed}", _shown(done)):
            words = (RENAMED_TITLE, RENAMED_WITH_ORIGINAL)
            _refusing_copy(renamed, checker, screens, "renamed", words, driven)
            _stop_the_app(checker, "a copy of the app")
            _move_folder(installed, aside)
            words = (RENAMED_TITLE, RENAMED_ALONE)
            _refusing_copy(renamed, checker, screens, "renamed-alone", words, driven)
    finally:
        _stop_the_app(checker, "a copy of the app")
        if aside.exists() and not installed.exists():
            _move_folder(aside, installed)
        checker.expect(installed.is_dir() and not aside.exists(), f"{installed} is back")
        if renamed.exists():
            make_writable(renamed)
            shutil.rmtree(renamed)
        checker.expect(not renamed.exists(), f"{renamed} is removed")


# The pairing check's work room, the folder alias it adds, the file it asks the fingerprint of,
# and how long the requests run during which it quits: the first long enough for the finishing
# panel to be read before ``run`` drains, the second long enough that only Stop now ends it.
PAIRING_ROOM: Final = "work-1"
PAIRING_ALIAS: Final = "pairing-check"
PAIRING_FILE: Final = b"the file the pairing check asks the fingerprint of\n"
QUIT_DURING: Final = 20
STOP_DURING: Final = 120
# What Activity says of the check's files.sha256 request on a.txt in its folder, from the core's
# summary of the operation (App/RequestLog.swift); then of it turned away while the ability is off,
# and of it finished.
FINGERPRINT_ENTRY: Final = (
    f"Lucille asked to check a file's fingerprint: “a.txt” in {PAIRING_ALIAS}."
)
NOT_ENABLED_ENTRY: Final = "Turned away: that ability is off on this Mac."
FINISHED_ENTRY: Final = "Finished on this Mac."
# A SAS as the app shows it (spec/CLI.md section 6, ``pair``).
SAS: Final = re.compile(r"\b[0-9]{4}-[0-9]{4}\b")


class _Stop(Exception):
    """A step that every later one needs did not hold (and was counted): the check tidies up."""


@dataclass(frozen=True)
class _Harness:
    """What the pairing check takes from the test harness and from Switchboard, which
    :func:`check_pairing` imports only when it runs."""

    child: Any  # tests._process.Child
    child_env: Callable[[Path], dict[str, str]]
    until: Callable[..., Any]
    body: Callable[..., dict[str, Any]]
    fake_lucille: Any  # tests._lucille.FakeLucille
    loopback_hub: Any  # tests._process.LoopbackHub
    client: Any  # switchboard.client.Client
    hub_program: Path
    listening: re.Pattern[str]


def _value_within(get: Callable[[], Any], seconds: float) -> Any:
    """What ``get()`` returns once it is truthy, asked every second for at most ``seconds``; its
    last answer if it never is."""
    deadline = time.monotonic() + seconds
    while True:
        found = get()
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(1)


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps(pid: int, field: str) -> str:
    done = subprocess.run(
        ["ps", "-ww", "-o", f"{field}=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()


def _parent(pid: int) -> int | None:
    found = _ps(pid, "ppid")
    return int(found) if found.isdigit() else None


def _sleeping(run: int) -> bool:
    """Whether ``run`` has started the process of a ``host.sleep`` request."""
    done = subprocess.run(
        ["pgrep", "-P", str(run), "-f", "^/bin/sleep "], capture_output=True, text=True, check=False
    )
    return bool(done.stdout.split())


def _profile_processes() -> list[int]:
    """The host's processes on the app's profile, whoever started them."""
    done = subprocess.run(
        ["pgrep", "-f", f"lucille_host --profile {APP_PROFILE} "],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(pid) for pid in done.stdout.split()]


def _close_pairing_window() -> tuple[bool, str]:
    """Click the close button of the app's pairing window: the window titled as the code window
    or the compare page, or the one that holds They Match."""
    titles = "{" + ", ".join(_quoted(title) for title in (CODE_TITLE, COMPARE_TITLE)) + "}"
    return _system_events(
        "repeat with theWindow in windows\n"
        f"set held to (name of theWindow is in {titles})\n"
        "try\n"
        f"if exists (button {_quoted(THEY_MATCH)} of theWindow) then set held to true\n"
        "end try\n"
        "if held then\n"
        'click (first button of theWindow whose subrole is "AXCloseButton")\n'
        'return "closed"\n'
        "end if\n"
        "end repeat\n"
        'error "no pairing window is open"'
    )


def _pairing_window_open() -> bool:
    return bool({CODE_TITLE, COMPARE_TITLE} & set(_window_names())) or (
        _enabled(THEY_MATCH) != "absent"
    )


class _PairingCheck:
    """One run of the pairing check: a hub of its own, a fake Lucille, the harness's commands on
    the app's profile, and the app, driven one step after another (:meth:`go`)."""

    def __init__(
        self,
        app: Path,
        checker: Checker,
        work: Path,
        tmp: Path,
        harness: _Harness,
        require_driven: bool,
    ) -> None:
        self.app = app
        self.python = python_in(app)
        self.checker = checker
        self.work = work
        self.screens = work / "screens"
        self.tmp = tmp
        self.h = harness
        self.require_driven = require_driven
        self.children: list[Any] = []
        # The app's standard error, a file for each time the check opens it.
        self.logs: list[Path] = []
        # The harness's commands run for the real HOME, where the app keeps its profile.
        self.host_env = harness.child_env(Path.home())
        self.profile = _profile_folder()
        self.set_aside: Path | None = None
        # Whether the profile folder is the check's own, once anything there before was set aside:
        # only then may the check stop the profile's processes and remove the folder.
        self.owns_profile = False
        self.folder = Path.home() / "lucille-host-pairing-check"
        self.lucille: Any = None
        self.url = ""

    # --- small things ---------------------------------------------------------------------

    def need(self, ok: bool, what: str, detail: str = "") -> None:
        """Count ``what``; if it does not hold, stop: the steps after it need it."""
        if not self.checker.expect(ok, what, detail):
            raise _Stop(what)

    def start(self, argv: list[str], *, env: dict[str, str], piped: bool) -> Any:
        child = self.h.child(argv, env=env, cwd=self.tmp, piped=piped)
        self.children.append(child)
        return child

    def host_argv(self, *args: str) -> list[str]:
        """The host on the app's profile, through the app's own interpreter with the app's flags
        (:data:`build_app.RUNTIME_FLAGS`): isolated, and writing no bytecode into the bundle, whose
        signature the check verifies at its end."""
        return host_argv(self.python, ["--profile", APP_PROFILE, *args])

    def host(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.host_argv(*args),
            input="",
            capture_output=True,
            text=True,
            env=self.host_env,
            cwd=self.tmp,
            timeout=180,
            check=False,
        )

    def status(self) -> dict[str, Any]:
        done = self.host("--json", "status")
        try:
            found = json.loads(done.stdout.splitlines()[-1])
        except (IndexError, ValueError):
            print(f"note: status --json: {_shown(done)}")
            return {}
        return found if isinstance(found, dict) else {}

    def pair_state(self) -> Any:
        return self.status().get("pair", {}).get("state")

    def op_enabled(self, op: str) -> bool | None:
        """Whether ``ops list --json`` on the app's profile says ``op`` is on; None when it does
        not say."""
        done = self.host("--json", "ops", "list")
        for line in done.stdout.splitlines():
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict) and found.get("name") == op:
                enabled = found.get("enabled")
                return enabled if isinstance(enabled, bool) else None
        return None

    def run_pid(self) -> int | None:
        run = self.status().get("run", {})
        pid = run.get("pid")
        return pid if run.get("state") == "running" and isinstance(pid, int) else None

    def reading(self) -> bool:
        rooms = self.status().get("work_rooms", [])
        return any(room.get("state") == "reading" for room in rooms)

    def lifeline_ends(self, cause: str | None = None) -> int:
        """How many times the host's log says that ``run``'s lifeline ended: for ``cause``
        (``stdin_end``, ``stdout_gone``) if one is given. The log records the first cause only."""
        done = self.host("--json", "log", "--limit", "1000")
        ends = 0
        for line in done.stdout.splitlines():
            try:
                record = json.loads(json.loads(line)["text"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                isinstance(record, dict)
                and record.get("kind") == "lifeline"
                and (cause is None or record.get("event") == cause)
            ):
                ends += 1
        return ends

    def app_pid(self) -> int | None:
        pids = _running(APP_NAME)
        return int(pids[0]) if len(pids) == 1 else None

    def stderr_now(self) -> str:
        """What the app has written on standard error since the check last opened it."""
        if not self.logs:
            return ""
        return self.logs[-1].read_text(encoding="utf-8", errors="replace")

    def answer(self, job_id: str, seconds: float) -> dict[str, Any]:
        """The body of the host's reply to the request ``job_id``, once Lucille can read it."""
        reply = _value_within(lambda: self.lucille.reply(PAIRING_ROOM, job_id), seconds)
        found = reply.get("body") if isinstance(reply, dict) else None
        return found if isinstance(found, dict) else {}

    def launch(self, label: str, *, paired: bool) -> None:
        """Open the app as Finder does, its standard output and error into files of their own.
        With no confirmed pair it says where its menu is, and the check chooses OK on that note.
        ``paired``, it shows no such note (decisions/0002, What is built today), and the check
        presses nothing on one it has not seen; one that shows is counted, then closed with OK,
        so that the later steps still run. It reads which state the app says macOS gives its
        login item, and the note about that, if any (:meth:`login_at_launch`)."""
        out = self.work / f"pairing-{label}-stdout.txt"
        err = self.work / f"pairing-{label}-stderr.txt"
        for log in (out, err):
            log.write_text("", encoding="utf-8")
        self.logs.append(err)
        done = subprocess.run(
            ["open", "--stdout", str(out), "--stderr", str(err), str(self.app)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.need(
            done.returncode == 0 and _wait_until(lambda: self.app_pid() is not None, 15),
            f"open starts the app ({label})",
            _shown(done),
        )
        time.sleep(3)
        if not _can_drive():
            if self.require_driven:
                self.checker.expect(False, "System Events drove the app, as --require-driven asks")
            raise _Stop("System Events cannot drive the app here")
        if not paired:
            _welcome(
                self.checker, self.screens / f"p-{label}-welcome.png", f"when it opens ({label})"
            )
            return
        self.login_at_launch(label)
        shown = _wait_until(lambda: _says(WELCOME_TITLE), 5)
        self.checker.expect(
            not shown,
            f"opened while paired ({label}), the app shows no note about its menu bar",
            f"{_texts()}" if shown else "",
        )
        if shown:
            # Counted above. Left up, it would hold back the paused note and a pairing left
            # unfinished, which come only once it is closed: the check has just read it, so it
            # closes it with OK, as at an unpaired launch, and the later steps still run.
            _system_events('click button "OK" of window 1')
            _wait_until(lambda: not _says(WELCOME_TITLE), 10)

    def login_at_launch(self, label: str) -> None:
        """Opened while paired, the app checks its login item and says on standard error which
        state macOS gives it. Only when that is requiresApproval does it say that it won't start
        when the user logs in (decisions/0002 section 3.3), with Open Login Items Settings, which
        the check never presses, and Keep It Off, which takes the item away: the check chooses
        that, so the later launches show no such note."""
        state = _value_within(lambda: _login_state(self.stderr_now()), 60)
        self.checker.expect(
            state is not None,
            f"opened while paired ({label}), the app says on standard error which state macOS "
            "gives its login item",
            "" if state is not None else self.stderr_now()[-2000:],
        )
        print(
            f"at launch ({label}), macOS gives the app's login item the state {state}", flush=True
        )
        if state != "requiresApproval":
            noted = _wait_until(lambda: _says(LAUNCH_OFF_TITLE), 3)
            self.checker.expect(
                not noted,
                f"with the login item {state}, it shows no note about opening at login",
                f"{_texts()}" if noted else "",
            )
            return
        noted = _wait_until(lambda: _says(LAUNCH_OFF_TITLE), 15)
        _screenshot(self.screens / f"p-{label}-login-off.png")
        if not self.checker.expect(
            noted,
            f"with the login item requiresApproval, it says {LAUNCH_OFF_TITLE!r}",
            "" if noted else f"{_texts()}",
        ):
            return
        before = len(self.stderr_now())
        worked, said = _press(KEEP_IT_OFF)
        closed = worked and _wait_until(lambda: not _says(LAUNCH_OFF_TITLE), 10)
        # Left up, the note would hold every later note behind it (the app shows one at a time),
        # and the next step's note with it: the steps after this one need it closed.
        self.need(
            closed,
            f"{KEEP_IT_OFF} closes the note {LAUNCH_OFF_TITLE!r}",
            "" if closed else f"{said} {_texts()}",
        )
        after = _value_within(lambda: _login_state(self.stderr_now()[before:]), 15)
        off = after in LOGIN_ITEM_GONE
        self.checker.expect(
            off,
            f"{KEEP_IT_OFF} turns the login item off: the app says 'login item: notRegistered' "
            "(or notFound, when macOS has none to find)",
            "" if off else f"{after}",
        )

    def through_the_command_line(self, *args: str) -> None:
        """Run ``args`` on the app's profile, as a user would in Terminal; it must work."""
        done = self.host(*args)
        self.need(
            done.returncode == 0,
            f"{' '.join(args[:2])} on the app's profile, through the command line",
            _shown(done),
        )

    def read_activity(self, words: str, shot: str) -> None:
        """Choose Activity… in the menu, and read ``words`` in it."""
        worked, said = _choose(ACTIVITY_ITEM.removesuffix("…"))
        found = worked and _wait_until(lambda: any(words in text for text in _activity_texts()), 30)
        _screenshot(self.screens / shot)
        self.checker.expect(
            found,
            f"Activity says {words!r}",
            f"{said} {_activity_texts()}" + ("" if found else _activity_holds()),
        )

    def close_activity(self) -> None:
        worked, said = _close_window(ACTIVITY_TITLE)
        self.checker.expect(
            worked and _wait_until(lambda: ACTIVITY_TITLE not in _window_names(), 10),
            "its close button closes Activity",
            said,
        )

    def ask_to_quit(self, shot: str) -> None:
        worked, said = _choose(QUIT_ITEM)
        asked = worked and _wait_until(lambda: _says(QUIT_QUESTION), 15)
        _screenshot(self.screens / shot)
        self.need(asked, f"the app's own Quit asks first: {QUIT_QUESTION!r}", f"{said} {_texts()}")
        self.checker.expect(_says(QUIT_TEXT), f"it says {QUIT_TEXT!r}", f"{_texts()}")

    # --- the steps ------------------------------------------------------------------------

    def set_up(self) -> None:
        """No copy of the app runs, nor any host process of its profile; the profile, if there
        is one, is set aside; then the hub and Lucille."""
        _stop_earlier_copies(self.checker, APP_NAME)
        self.need(
            _wait_until(lambda: not _profile_processes(), 90),
            "no host process of the app's profile runs",
            f"{_profile_processes()}",
        )
        _set_pasteboard("")
        if self.profile.exists():
            aside = self.profile.with_name(f"{APP_PROFILE}.set-aside-{os.getpid()}")
            self.profile.rename(aside)
            self.set_aside = aside
            print(f"note: the app's profile is set aside at {aside} until the end")
        self.owns_profile = True
        # Lucille's client, in this process, reads no Switchboard variable and finds no signer.
        for name in [name for name in os.environ if name.startswith("SWITCHBOARD_")]:
            del os.environ[name]
        runtime = self.tmp / "lucille-runtime"
        runtime.mkdir(mode=0o700)
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
        hub_home = self.tmp / "hub-home"
        hub_home.mkdir(mode=0o700)
        hub = self.start(
            [
                str(self.h.hub_program),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--db",
                str(self.tmp / "hub.db"),
                "--log-level",
                "info",
            ],
            env=self.h.child_env(hub_home),
            piped=False,
        )

        def listening() -> str | None:
            assert hub.proc.poll() is None, hub.describe()
            found = self.h.listening.search(hub.stderr)
            return found.group(1) if found else None

        self.url = self.h.until(listening, "the hub listening", self.children)
        print(f"the hub listens at {self.url}")
        self.lucille = self.h.fake_lucille(
            self.h.loopback_hub(self.url), self.tmp / "lucille", connect=self.h.client
        )

    def pair_through_the_harness(self) -> None:
        """Pair the app's profile as a test harness does (spec/CLI.md section 7): ``pair
        --code-stdin`` with ``--hub`` and ``--store-file``, the code on standard input, Lucille
        collecting the answer. It ends once it has shown the SAS, so the SAS is pending, and the
        app must show it again before They Match counts."""
        code = self.lucille.start()
        pair = self.start(
            self.host_argv("pair", "--code-stdin", "--json", "--hub", self.url, "--store-file"),
            env=self.host_env,
            piped=True,
        )
        pair.write(code.encode() + b"\n")

        def collected() -> bool:
            assert pair.proc.poll() in (None, 0), pair.describe()
            return self.lucille.collect().state == "paired"

        self.h.until(collected, "Lucille collecting this Mac's answer", self.children)
        self.need(
            pair.wait() == 0, "the harness's pair ends once the SAS is shown", pair.describe()
        )
        said = pair.objects()
        shown = said[-1] if said else {}
        self.need(
            shown.get("state") == "sas_pending" and shown.get("sas") == self.lucille.sas(),
            "it showed the SAS Lucille's side shows",
            pair.describe(),
        )
        self.need(self.pair_state() == "sas_pending", "status then says the SAS is pending")

    def confirm_in_the_app(self) -> None:
        """Open the pairing window from the menu: it shows Lucille's number, Return and Space
        answer nothing, and They Match confirms."""
        self.launch("pairing", paired=False)
        sas = self.lucille.sas()
        # The app picks a pending pairing back up once its first note is closed, and may show the
        # compare page on its own. Close it, as a user would, and open it again from the menu.
        if _wait_until(_pairing_window_open, 15):
            _wait_until(lambda: _enabled(THEY_MATCH) == "true", 20)
            _screenshot(self.screens / "p01-compare-at-launch.png")
            print("the app showed its pairing window on its own, once its first note was closed")
            worked, said = _close_pairing_window()
            self.need(
                worked and _wait_until(lambda: not _pairing_window_open(), 10),
                "its close button closes the pairing window",
                said,
            )
        found, shown = _menu_until(
            lambda items: FINISH_ITEM in items, 60, shot=self.screens / "p02-menu-finish.png"
        )
        self.need(
            found,
            f"with the SAS pending and no pairing window, the menu offers {FINISH_ITEM!r}",
            f"{shown}",
        )
        self.checker.expect(CONNECTING in shown, f"and says {CONNECTING!r}", f"{shown}")
        worked, said = _choose(FINISH_ITEM.removesuffix("…"))
        self.need(worked, f"{FINISH_ITEM!r} can be chosen", said)

        def numbers() -> list[str]:
            return [number for text in _texts() for number in SAS.findall(text)]

        drawn = _wait_until(lambda: bool(numbers()) and _enabled(THEY_MATCH) == "true", 30)
        _screenshot(self.screens / "p03-compare.png")
        shown_numbers = numbers()
        self.need(
            drawn and shown_numbers == [sas],
            f"the pairing window shows Lucille's number, {sas}, once and no other, and They Match "
            "is on",
            f"{shown_numbers} {_texts()} {_enabled(THEY_MATCH)}",
        )
        self.checker.expect(_says(COMPARE_QUESTION), f"it asks {COMPARE_QUESTION!r}", f"{_texts()}")
        # Neither button answers Return or Space: only a click does.
        for key, code in (("Return", 36), ("Space", 49)):
            _system_events(f"set frontmost to true\nkey code {code}")
            time.sleep(4)
            self.checker.expect(
                self.pair_state() == "sas_pending",
                f"{key} answers nothing: the SAS is still pending",
            )
        worked, said = _press(THEY_MATCH)
        confirmed = worked and _wait_until(lambda: _says(CONFIRMED_TITLE), 60)
        _screenshot(self.screens / "p04-confirmed.png")
        self.need(
            confirmed,
            f"They Match confirms, and the app says {CONFIRMED_TITLE!r}",
            f"{said} {_texts()}",
        )
        _press("OK")
        self.need(self.pair_state() == "confirmed", "status then says the pair is confirmed")

    def run_reads_the_room(self) -> None:
        """The app starts ``run`` as its child, with the lifeline; a second ``run`` of the profile
        is refused; ``run`` reads the room Lucille offers, and the menu says so."""
        run = _value_within(self.run_pid, 60)
        self.need(run is not None, "the app starts run once the pair is confirmed", f"{run}")
        app = self.app_pid()
        self.checker.expect(
            _parent(run) == app, "run is the app's child", f"run {run}, parent {_parent(run)}"
        )
        command = _ps(run, "command")
        self.checker.expect(
            f"-m lucille_host --profile {APP_PROFILE} run --json-events --exit-on-stdin-eof"
            in command,
            "it runs on the app's profile, ended by the end of its standard input",
            command,
        )
        descriptors = subprocess.run(
            ["lsof", "-a", "-p", str(run), "-d", "0", "-F", "t"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.checker.expect(
            "tPIPE" in descriptors.stdout.split(),
            "its standard input is a pipe: the lifeline",
            _shown(descriptors),
        )
        second = self.start(
            self.host_argv("run", "--json-events", "--exit-on-stdin-eof"),
            env=self.host_env,
            piped=True,
        )
        refused = second.wait() == 21
        self.checker.expect(
            refused
            and second.objects() == [{"v": 1, "type": "stopped", "reason": "already_running"}]
            and _alive(run),
            "a second run of the profile is refused (exit 21), and the app's run goes on",
            second.describe(),
        )
        self.lucille.offer(PAIRING_ROOM)
        self.need(_wait_until(self.reading, 90), "run reads the room Lucille offers")
        # Where the app's windows are before the menu is opened and once it is closed again, so
        # that a window that went can be put down to the menu or to what came after it.
        print(f"the app's windows before the menu is read: {_app_windows()}", flush=True)
        found, shown = _menu_until(
            lambda items: CONNECTED in items, 60, shot=self.screens / "p05-connected.png"
        )
        print(f"and once it is closed again: {_app_windows()}", flush=True)
        self.checker.expect(found, f"the menu then says {CONNECTED!r}", f"{shown}")
        sockets = subprocess.run(
            ["lsof", "-a", "-p", str(app), "-i"], capture_output=True, text=True, check=False
        )
        self.checker.expect(
            app is not None and not sockets.stdout.strip(),
            "the app itself holds no network connection",
            sockets.stdout,
        )

    def set_up_after_connected(self) -> None:
        """Once the menu says Connected after They Match in the app, the Set Up window opens by
        itself (decisions/0002 section 3.1, steps 8 to 10), and its pages are walked
        (:meth:`set_up_pages`). A Set Up check that fails does not stop the pairing check: the
        window and any note about opening at login are closed, and files.sha256 turned off again,
        and only those facts, which the later steps need, are needed: a note left up would hold
        the menu back, and the fingerprint step's request while files.sha256 is off must still
        meet not_enabled (M18)."""
        self.set_up_pages()
        if SETUP_TITLE in _window_names():
            _close_window(SETUP_TITLE)
        _close_login_note()
        if self.op_enabled("files.sha256") is not False:
            self.host("--json", "ops", "disable", "files.sha256")
        off = self.op_enabled("files.sha256") is False
        clear = SETUP_TITLE not in _window_names() and _login_note() is None
        detail = "" if off and clear else f"{self.ops_and_windows()}"
        self.need(
            off and clear,
            "after Set Up, files.sha256 is off, and no Set Up window and no note about opening at "
            "login is open",
            detail,
        )

    def ops_and_windows(self) -> str:
        """``ops list`` on the app's profile, and what the app's windows show, for a failure's
        detail."""
        return f"{_shown(self.host('--json', 'ops', 'list'))} {_window_names()} {_texts()}"

    def set_up_pages(self) -> None:
        """Set Up's pages as CI can reach them. It has no Add back page: the harness paired
        through the command line, so the app saw no policy_reset. It opens on Folders, which lists
        none. On What Lucille may do, files.sha256's switch shows it off; clicked, the host says it
        is on, and the switch shows it on; clicked again, both say off. Then Ready, whose box
        "Open <name> when you log in" is checked and which says macOS's notice; and Done closes the
        window, after which the app says what came of opening at login (:meth:`login_after_done`).
        Each is expected; at the first one the next steps here need that fails, it returns."""
        opened = _wait_until(lambda: SETUP_TITLE in _window_names(), 60)
        # The missing window's detail only when it is missing: it takes a screenshot and reads
        # the menu, which a window that is open must not have done to it.
        missing = "" if opened else f"{_window_names()}\n{self.set_up_missing()}"
        if not self.checker.expect(
            opened, f"once connected, the app opens {SETUP_TITLE!r} by itself", missing
        ):
            return
        shown = _wait_until(lambda: _says(SETUP_FOLDERS), 15)
        _screenshot(self.screens / "p-setup-folders.png")
        if not self.checker.expect(shown, f"it opens on {SETUP_FOLDERS!r}", f"{_texts()}"):
            return
        self.checker.expect(
            not _says(SETUP_ADD_BACK),
            f"with no {SETUP_ADD_BACK!r} page: the app saw no policy_reset of its own",
            f"{_texts()}",
        )
        self.checker.expect(
            _says(SETUP_NO_FOLDERS), f"the Folders page says {SETUP_NO_FOLDERS!r}", f"{_texts()}"
        )
        worked, said = _press(SETUP_CONTINUE)
        if not self.checker.expect(
            worked and _wait_until(lambda: _says(SETUP_ABILITIES), 15),
            f"Continue goes to {SETUP_ABILITIES!r}",
            f"{said} {_texts()}",
        ):
            return
        switch = ABILITY_TITLES["files.sha256"]
        if not self.checker.expect(
            self.op_enabled("files.sha256") is False and _checked(switch) == "0",
            f"files.sha256 is off before its switch is used, and {switch!r} shows it off",
            f"switch {_checked(switch)}; {_shown(self.host('--json', 'ops', 'list'))}",
        ):
            return
        worked, said = _toggle(switch)
        if not self.checker.expect(
            worked and _wait_until(lambda: self.op_enabled("files.sha256") is True, 15),
            f"the switch {switch!r} turns files.sha256 on: ops list says it is enabled",
            f"{said} {_shown(self.host('--json', 'ops', 'list'))}",
        ):
            return
        self.checker.expect(
            _wait_until(lambda: _checked(switch) == "1", 15),
            f"and {switch!r} shows it on",
            _checked(switch),
        )
        _screenshot(self.screens / "p-setup-abilities.png")
        worked, said = _toggle(switch)
        if not self.checker.expect(
            worked and _wait_until(lambda: self.op_enabled("files.sha256") is False, 15),
            f"clicked again, {switch!r} turns it off",
            f"{said} {_shown(self.host('--json', 'ops', 'list'))}",
        ):
            return
        self.checker.expect(
            _wait_until(lambda: _checked(switch) == "0", 15),
            f"and {switch!r} shows it off",
            _checked(switch),
        )
        worked, said = _press(SETUP_CONTINUE)
        if not self.checker.expect(
            worked and _wait_until(lambda: _says(SETUP_READY), 15),
            f"Continue goes to {SETUP_READY!r}",
            f"{said} {_texts()}",
        ):
            return
        _screenshot(self.screens / "p-setup-ready.png")
        box = _checked(OPEN_AT_LOGIN_BOX)
        self.checker.expect(box == "1", f"Ready's box {OPEN_AT_LOGIN_BOX!r} is checked", box)
        noticed = _says(OPEN_AT_LOGIN_NOTICE)
        self.checker.expect(
            noticed,
            f"and Ready says {OPEN_AT_LOGIN_NOTICE!r}",
            "" if noticed else f"{_texts()}",
        )
        before = len(self.stderr_now())
        worked, said = _press(SETUP_DONE)
        closed = worked and _wait_until(lambda: SETUP_TITLE not in _window_names(), 10)
        self.checker.expect(
            closed, "Done closes the Set Up window", "" if closed else f"{said} {_window_names()}"
        )
        if closed and box == "1":
            self.login_after_done(before)

    def login_after_done(self, before: int) -> None:
        """Done, with Ready's box checked, asks macOS to open the app when the user logs in. What
        macOS answers an ad-hoc build is not known (decisions/0002 section 9), so the gate is
        agreement: the app says on standard error which state macOS gave its login item, after
        ``before``, and its words agree with that state: no note while enabled; while
        requiresApproval, that macOS needs the user's OK, closed with Not Now; otherwise, that it
        couldn't, closed with OK. Open Login Items Settings, which would open System Settings, is
        never pressed. macOS's own list of login items is printed."""
        state = _value_within(lambda: _login_state(self.stderr_now()[before:]), 15)
        _dump_login_items()
        if not self.checker.expect(
            state is not None,
            "after Done, the app says on standard error which state macOS gives its login item",
            "" if state is not None else self.stderr_now()[before:][-2000:],
        ):
            return
        print(f"after Done, macOS gives the app's login item the state {state}", flush=True)
        title = LOGIN_NOTE_AFTER_DONE[state]
        if title is None:
            _wait_until(lambda: _login_note() is not None, 5)
        else:
            _wait_until(lambda: _login_note() == title, 15)
        _screenshot(self.screens / "p-setup-after-done.png")
        note = _login_note()
        agree = note == title
        if not self.checker.expect(
            agree,
            f"after Done, the app's words agree with the state {state}: "
            + (f"it says {title!r}" if title else "no note about opening at login"),
            "" if agree else f"{_texts()}",
        ):
            return
        if note is None:
            return
        button = LOGIN_NOTE_CLOSES[note]
        worked, said = _press(button)
        closed = worked and _wait_until(lambda: _login_note() is None, 10)
        self.checker.expect(closed, f"{button} closes the note", "" if closed else said)

    def set_up_missing(self) -> str:
        """What tells apart the ways Set Up's window can be missing, for that failure's detail: a
        screenshot; every window of the app, named or not, and whether it is frontmost; the menu,
        which offers SETUP_ITEM only once the window opened and was then closed as a user closes it,
        not when it never opened, nor when the app closed it (App/Setup.swift, ``offersReopen``);
        the pair and ``run``; and what the app has written on standard error, where it says when
        Set Up was shown and closed, and by whom."""
        _screenshot(self.screens / "p-setup-missing.png")
        _, shown = _menu_until(lambda items: True, 0)
        offered = "offers" if SETUP_ITEM in shown else "does not offer"
        return (
            f"the app's windows: {_app_windows()}\n"
            f"the menu {offered} {SETUP_ITEM!r}: {shown}\n"
            f"pair {self.pair_state()}, run {self.run_pid()}\n"
            f"the app's standard error so far: {self.stderr_now()[-2000:]!r}"
        )

    def fingerprint(self) -> None:
        """A folder added through the command line on the app's profile, and a files.sha256
        request on it while that ability is off: the host turns it away, and Activity says so as a
        fact, offering nothing but its disclosure (M18). Then files.sha256 turned on through the
        command line, and a request like it, answered with the file's fingerprint, which Activity
        says finished."""
        files = self.folder / "Files"
        files.mkdir(parents=True, exist_ok=True)
        (files / "a.txt").write_bytes(PAIRING_FILE)
        self.through_the_command_line("roots", "add", "--", PAIRING_ALIAS, str(files))
        asked = {"root": PAIRING_ALIAS, "path": "a.txt"}
        refused = self.h.body(op="files.sha256", args=asked)
        self.lucille.request(PAIRING_ROOM, refused)
        answer = self.answer(refused["job_id"], 90)
        self.checker.expect(
            (answer.get("status"), answer.get("code")) == ("refused", "not_enabled"),
            "while files.sha256 is off, the host turns a request for it away: not_enabled",
            f"{answer}",
        )
        self.read_activity(f"{FINGERPRINT_ENTRY} {NOT_ENABLED_ENTRY}", "p-activity-not-enabled.png")
        buttons = _activity_buttons()
        self.checker.expect(
            buttons == [SHOW_LOG],
            f"Activity offers no button but {SHOW_LOG!r}: nothing in it turns the ability on",
            f"{buttons}",
        )
        self.close_activity()
        self.through_the_command_line("ops", "enable", "files.sha256")
        sent = self.h.body(op="files.sha256", args=asked)
        self.lucille.request(PAIRING_ROOM, sent)
        answer = self.answer(sent["job_id"], 90)
        digest = hashlib.sha256(PAIRING_FILE).hexdigest()
        self.checker.expect(
            answer.get("status") == "done" and digest in str(answer.get("stdout", "")),
            "a files.sha256 request on that folder is answered with the file's fingerprint",
            f"{answer}",
        )
        self.read_activity(f"{FINGERPRINT_ENTRY} {FINISHED_ENTRY}", "p-activity-finished.png")
        self.close_activity()

    def welcome_again(self, shot: str) -> None:
        """Open the running app again, as Finder does: it shows its note about the menu bar."""
        done = subprocess.run(["open", str(self.app)], capture_output=True, text=True, check=False)
        noted = done.returncode == 0 and _wait_until(lambda: _says(WELCOME_TITLE), 20)
        _screenshot(self.screens / shot)
        self.need(
            noted,
            f"opened again while it runs, the app says {WELCOME_TITLE!r}",
            f"{_shown(done)} {_texts()}",
        )

    def backoff(self) -> None:
        """``run`` killed with SIGKILL is a crash: the app starts it again after its back-off,
        while a note is up too. A note's modal loop must hold back none of the app's timers, and
        none of its children's events."""
        run = self.run_pid()
        self.need(run is not None, "run is running")
        self.welcome_again("p05b-note-up.png")
        os.kill(run, signal.SIGKILL)

        def started_again() -> int | None:
            pid = self.run_pid()
            return pid if pid is not None and pid != run else None

        again = _value_within(started_again, 60)
        self.need(
            again is not None,
            "killed with SIGKILL, run is started again after the back-off",
            f"{self.status().get('run')}",
        )
        self.checker.expect(
            _says(WELCOME_TITLE), "it was started again while the note was still up", f"{_texts()}"
        )
        worked, said = _press("OK")
        self.checker.expect(
            worked and _wait_until(lambda: not _says(WELCOME_TITLE), 10), "OK closes the note", said
        )
        self.checker.expect(_parent(again) == self.app_pid(), "by the app, as its child")
        self.checker.expect(
            f"{SIGNALED_LINE} 9" in self.stderr_now(),
            "the app says on standard error that a signal ended run",
            self.stderr_now()[-2000:],
        )
        self.need(_wait_until(self.reading, 90), "the new run reads the room again")

    def app_killed(self) -> None:
        """The app killed with SIGKILL leaves ``run``'s standard input with no writer: ``run``
        drains and exits (spec/CLI.md section 13.4)."""
        app, run = self.app_pid(), self.run_pid()
        self.need(app is not None and run is not None, "the app and its run are running")
        ends = self.lifeline_ends()
        os.kill(app, signal.SIGKILL)
        self.need(
            _wait_until(lambda: not _alive(run), 90),
            "once the app is killed with SIGKILL, run exits",
            f"{self.status().get('run')}",
        )
        state = self.status().get("run", {}).get("state")
        self.checker.expect(
            state == "stopped", "status then says run stopped, as after a drain", f"{state}"
        )
        self.checker.expect(
            self.lifeline_ends() > ends,
            "the host's log says run's lifeline ended (its standard input, or its standard output)",
        )

    def relaunch(self) -> None:
        self.launch("relaunched", paired=True)
        run = _value_within(self.run_pid, 60)
        self.need(
            run is not None and _parent(run) == self.app_pid(),
            "opened again, the app starts run, as its child",
            f"{run}",
        )
        self.need(_wait_until(self.reading, 90), "run reads the room again")

    def quit_and_drain(self) -> None:
        """The app's own Quit, from its note about the menu bar, while a request runs: the
        question, then the finishing panel; the app quits once ``run`` has finished the request and
        drained. The note's Quit is chosen inside the note's own modal loop, and AppKit's wait to
        quit is one too: the drain must go on inside it."""
        done = self.host("ops", "enable", "host.sleep")
        self.need(
            done.returncode == 0,
            "ops enable host.sleep on the app's profile, through the command line",
            _shown(done),
        )
        app, run = self.app_pid(), self.run_pid()
        self.need(app is not None and run is not None, "the app and its run are running")
        ends = self.lifeline_ends("stdin_end")
        sent = self.h.body(op="host.sleep", args={"n": QUIT_DURING})
        self.lucille.request(PAIRING_ROOM, sent)
        self.need(_wait_until(lambda: _sleeping(run), 60), "a host.sleep request starts")
        self.welcome_again("p06-welcome-again.png")
        worked, said = _press(QUIT_ITEM)
        asked = worked and _wait_until(lambda: _says(QUIT_QUESTION), 15)
        _screenshot(self.screens / "p06-quit-question.png")
        self.need(
            asked, f"the note's {QUIT_ITEM} asks first: {QUIT_QUESTION!r}", f"{said} {_texts()}"
        )
        self.checker.expect(_says(QUIT_TEXT), f"it says {QUIT_TEXT!r}", f"{_texts()}")
        worked, said = _press(QUIT_BUTTON)
        finishing = worked and _wait_until(lambda: _says(FINISHING), 15)
        _screenshot(self.screens / "p07-finishing.png")
        self.checker.expect(
            finishing and _enabled(STOP_NOW) == "true",
            f"while run finishes the request, the app says {FINISHING!r}, and offers {STOP_NOW!r}",
            f"{said} {_texts()}",
        )
        self.need(
            _wait_until(lambda: not _alive(app), QUIT_DURING + 120),
            "the app quits once run has drained",
        )
        answer = self.answer(sent["job_id"], 60)
        self.checker.expect(
            answer.get("status") == "done",
            "the request finished, and its reply was posted",
            f"{answer}",
        )
        self.checker.expect(not _alive(run), "run has exited")
        self.checker.expect(
            DRAINED_LINE in self.stderr_now(),
            f"the app says on standard error {DRAINED_LINE!r}",
            self.stderr_now()[-2000:],
        )
        status = self.status()
        self.checker.expect(
            status.get("run", {}).get("state") == "stopped" and status.get("paused") is False,
            "status says run stopped, and nothing paused the host",
            f"{status.get('run')}, paused {status.get('paused')}",
        )
        self.checker.expect(
            self.lifeline_ends("stdin_end") > ends,
            "the host's log says run's standard input ended",
        )

    def stop_whats_running(self) -> None:
        """Stop What's Running, from the menu, while a request runs that would outlast the check:
        ``kill`` stops the request, Lucille is told it failed, killed_locally, and the host is
        paused, while ``run`` goes on. The app resumes nothing on its own; Resume Lucille, from
        the menu, resumes. While no request runs, the menu does not offer it. The app is then
        quit, as the next step expects."""
        self.launch("stop-whats-running", paired=True)
        run = _value_within(self.run_pid, 60)
        self.need(run is not None, "opened again, the app starts run")
        self.need(_wait_until(self.reading, 90), "run reads the room again")
        found, shown = _menu_until(lambda items: CONNECTED in items, 60)
        self.checker.expect(
            found and STOP_WHATS_RUNNING not in shown,
            f"while no request runs, the menu does not offer {STOP_WHATS_RUNNING!r}",
            f"{shown}",
        )
        sent = self.h.body(op="host.sleep", args={"n": STOP_DURING})
        self.lucille.request(PAIRING_ROOM, sent)
        self.need(_wait_until(lambda: _sleeping(run), 60), "a host.sleep request starts")
        found, shown = _menu_until(
            lambda items: STOP_WHATS_RUNNING in items,
            30,
            shot=self.screens / "p07b-stop-whats-running.png",
        )
        self.need(found, f"while it runs, the menu offers {STOP_WHATS_RUNNING!r}", f"{shown}")
        worked, said = _choose(STOP_WHATS_RUNNING)
        told = worked and _wait_until(lambda: _says(STOPPED_AND_PAUSED), 30)
        _screenshot(self.screens / "p07c-stopped-and-paused.png")
        self.checker.expect(told, f"then the app says {STOPPED_AND_PAUSED!r}", f"{said} {_texts()}")
        answer = self.answer(sent["job_id"], 60)
        self.checker.expect(
            (answer.get("status"), answer.get("code")) == ("failed", "killed_locally"),
            f"{STOP_WHATS_RUNNING} runs kill: Lucille is told the request failed, killed_locally",
            f"{answer}",
        )
        self.checker.expect(self.status().get("paused") is True, "the host is then paused")
        self.checker.expect(
            self.run_pid() == run and _alive(run), "and run goes on: the same process", f"{run}"
        )
        _press("OK")
        time.sleep(10)
        self.checker.expect(
            self.status().get("paused") is True,
            "ten seconds later the host is still paused: the app resumes only when asked",
        )
        found, shown = _menu_until(
            lambda items: RESUME_ITEM in items and STOP_WHATS_RUNNING not in items,
            30,
            shot=self.screens / "p07d-paused-menu.png",
        )
        self.checker.expect(
            found,
            f"the menu offers {RESUME_ITEM!r}, and no longer {STOP_WHATS_RUNNING!r}",
            f"{shown}",
        )
        worked, said = _choose(RESUME_ITEM)
        self.need(
            worked and _wait_until(lambda: self.status().get("paused") is False, 30),
            f"{RESUME_ITEM} resumes the host",
            said,
        )
        app = self.app_pid()
        self.ask_to_quit("p07e-quit-question.png")
        _press(QUIT_BUTTON)
        self.need(_wait_until(lambda: not _alive(app), 120), "the app quits once run has drained")

    def too_old_then_prepare(self) -> None:
        """Lucille asks for a newer release than this one (a development build is older than
        every minimum, spec/PROTOCOL.md section 10): the host refuses the request host_too_old,
        and the app says so in one notice, which says in words where to get the update and offers
        only Not Now and Prepare to Update. A second request, asking for a higher one while the
        notice is up, brings no second notice. Not Now closes it, and a third request, asking for
        a higher one still, does not bring it back: the user is told once. The menu then holds
        its line, with the highest version, which opens the notice again, drawn with that
        version, and Prepare to Update…, which asks no Quit question, lets run drain, and quits.
        Stop now's step opens the app again."""
        self.launch("too-old", paired=True)
        run = _value_within(self.run_pid, 60)
        self.need(run is not None, "opened again, the app starts run")
        self.need(_wait_until(self.reading, 90), "run reads the room again")
        app = self.app_pid()
        sent = self.h.body(min_host_version=UPDATE_MIN_VERSION)
        self.lucille.request(PAIRING_ROOM, sent)
        answer = self.answer(sent["job_id"], 90)
        self.checker.expect(
            (answer.get("status"), answer.get("code")) == ("refused", "host_too_old"),
            f"a request that needs {UPDATE_MIN_VERSION} or newer is refused host_too_old",
            f"{answer}",
        )
        noted = _wait_until(lambda: _says(UPDATE_TITLE), 30)
        _screenshot(self.screens / "p07f-update-notice.png")
        self.need(noted, f"the app says {UPDATE_TITLE!r}", f"{_texts()}")
        self.checker.expect(
            _says(UPDATE_WHERE), f"it says where to get the update: {UPDATE_WHERE!r}", f"{_texts()}"
        )
        worked, buttons = _window_list("name of every button")
        self.checker.expect(
            worked and sorted(buttons) == sorted([NOT_NOW, PREPARE_TO_UPDATE]),
            f"it offers {NOT_NOW!r} and {PREPARE_TO_UPDATE!r}, and nothing else",
            f"{buttons}",
        )
        higher = self.h.body(min_host_version=UPDATE_HIGHER_VERSION)
        self.lucille.request(PAIRING_ROOM, higher)
        answer = self.answer(higher["job_id"], 90)
        self.checker.expect(
            (answer.get("status"), answer.get("code")) == ("refused", "host_too_old"),
            f"so is one that needs {UPDATE_HIGHER_VERSION}, sent while the notice is up",
            f"{answer}",
        )
        time.sleep(3)
        self.checker.expect(
            _says(UPDATE_TITLE) and not _says(UPDATE_HIGHER_TITLE),
            "it brings no second notice",
            f"{_texts()}",
        )
        worked, said = _press(NOT_NOW)
        closed = worked and _wait_until(lambda: not _says(UPDATE_TITLE), 10)
        self.checker.expect(closed, f"{NOT_NOW} closes the notice", said)
        self.checker.expect(
            not _wait_until(lambda: _says(UPDATE_HIGHER_TITLE), 5),
            "and no other notice follows it",
            f"{_texts()}",
        )
        highest = self.h.body(min_host_version=UPDATE_HIGHEST_VERSION)
        self.lucille.request(PAIRING_ROOM, highest)
        answer = self.answer(highest["job_id"], 90)
        self.checker.expect(
            (answer.get("status"), answer.get("code")) == ("refused", "host_too_old"),
            f"so is one that needs {UPDATE_HIGHEST_VERSION}, sent once the notice is closed",
            f"{answer}",
        )
        self.checker.expect(
            not _wait_until(lambda: _says(UPDATE_HIGHEST_TITLE) or _says(UPDATE_TITLE), 5),
            "it does not bring the notice back: the user is told once",
            f"{_texts()}",
        )
        found, shown = _menu_until(
            lambda items: UPDATE_HIGHEST_TITLE in items and PREPARE_TO_UPDATE_ITEM in items,
            30,
            shot=self.screens / "p07g-update-menu.png",
        )
        self.need(
            found,
            f"the menu then holds {UPDATE_HIGHEST_TITLE!r} and {PREPARE_TO_UPDATE_ITEM!r}",
            f"{shown}",
        )
        worked, said = _choose(UPDATE_HIGHEST_TITLE)
        again = worked and _wait_until(lambda: _says(UPDATE_HIGHEST_TITLE), 15)
        self.checker.expect(
            again,
            "that line shows the notice again, with the highest version",
            f"{said} {_texts()}",
        )
        _press(NOT_NOW)
        _wait_until(lambda: not _says(UPDATE_HIGHEST_TITLE), 10)
        worked, said = _choose(PREPARE_TO_UPDATE_ITEM.removesuffix("…"))
        self.need(worked, f"{PREPARE_TO_UPDATE_ITEM!r} can be chosen", said)
        asked = _wait_until(lambda: _says(QUIT_QUESTION), 5)
        _screenshot(self.screens / "p07h-prepare-to-update.png")
        self.checker.expect(
            not asked, f"{PREPARE_TO_UPDATE_ITEM} asks no {QUIT_QUESTION!r}", f"{_texts()}"
        )
        self.need(
            _wait_until(lambda: not _alive(app), 120),
            "the app quits once run has drained",
        )
        self.checker.expect(not _alive(run), "run has exited")
        self.checker.expect(
            DRAINED_LINE in self.stderr_now(),
            f"run drained first: the app says on standard error {DRAINED_LINE!r}",
            self.stderr_now()[-2000:],
        )
        status = self.status()
        self.checker.expect(
            status.get("run", {}).get("state") == "stopped" and status.get("paused") is False,
            "status says run stopped, and nothing paused the host",
            f"{status.get('run')}, paused {status.get('paused')}",
        )

    def stop_now(self) -> None:
        """Quit while a request runs that would outlast the check, then Stop now: ``kill``
        stops the request, Lucille is told, and the host is paused."""
        self.launch("stop-now", paired=True)
        run = _value_within(self.run_pid, 60)
        self.need(run is not None, "opened again, the app starts run")
        self.need(_wait_until(self.reading, 90), "run reads the room again")
        app = self.app_pid()
        sent = self.h.body(op="host.sleep", args={"n": STOP_DURING})
        self.lucille.request(PAIRING_ROOM, sent)
        self.need(_wait_until(lambda: _sleeping(run), 60), "a host.sleep request starts")
        self.ask_to_quit("p08-quit-question.png")
        _press(QUIT_BUTTON)
        offered = _wait_until(lambda: _enabled(STOP_NOW) == "true", 15)
        _screenshot(self.screens / "p09-finishing.png")
        self.need(offered, f"the finishing panel offers {STOP_NOW!r}", f"{_texts()}")
        worked, said = _press(STOP_NOW)
        self.need(worked, f"{STOP_NOW!r} can be chosen", said)
        stopping = _wait_until(lambda: _says(STOPPING_NOW), 5)
        _screenshot(self.screens / "p10-stopping.png")
        print(f"the panel said {STOPPING_NOW!r} after Stop now: {stopping}")
        self.need(_wait_until(lambda: not _alive(app), 120), "the app quits once run has stopped")
        answer = self.answer(sent["job_id"], 60)
        self.checker.expect(
            (answer.get("status"), answer.get("code")) == ("failed", "killed_locally"),
            "Stop now runs kill: Lucille is told the request failed, killed_locally",
            f"{answer}",
        )
        status = self.status()
        self.checker.expect(
            status.get("paused") is True and status.get("run", {}).get("state") == "stopped",
            "the host is then paused, and run stopped",
            f"{status.get('run')}, paused {status.get('paused')}",
        )

    def paused_at_launch(self) -> None:
        """Opened again, the app says the host is paused, and resumes it only when asked."""
        self.launch("paused", paired=True)
        asked = _wait_until(lambda: _says(PAUSED_TITLE), 30)
        _screenshot(self.screens / "p11-paused-at-launch.png")
        self.need(
            asked and _enabled(RESUME_BUTTON) == "true",
            f"opened again, the app says {PAUSED_TITLE!r}, and offers {RESUME_BUTTON}",
            f"{_texts()}",
        )
        time.sleep(10)
        self.checker.expect(
            self.status().get("paused") is True,
            "ten seconds later the host is still paused: the app resumes only when asked",
        )
        worked, said = _press(RESUME_BUTTON)
        self.need(
            worked and _wait_until(lambda: self.status().get("paused") is False, 30),
            f"{RESUME_BUTTON} resumes the host",
            said,
        )

    def disconnect(self) -> None:
        """Disconnect drains ``run``, then forgets the pair, and says whether Lucille was told.
        The login item goes with the pair (decisions/0002 section 9): the app says
        ``login item: notRegistered`` on standard error, and its note adds that it won't open at
        login exactly when it had one, as the last state it said before shows. While
        connected the menu holds Settings, and once disconnected it does not."""
        run = _value_within(self.run_pid, 60)
        self.need(run is not None, "run is running")
        found, shown = _menu_until(lambda items: DISCONNECT_ITEM in items, 60)
        self.need(found, f"while connected, the menu offers {DISCONNECT_ITEM!r}", f"{shown}")
        self.checker.expect(
            SETTINGS_MENU in shown, f"while connected, the menu holds {SETTINGS_MENU!r}", f"{shown}"
        )
        was = _login_state(self.stderr_now())
        self.checker.expect(
            was is not None,
            "before Disconnect, the app has said which state macOS gives its login item",
            "" if was is not None else self.stderr_now()[-2000:],
        )
        registered = was in ("enabled", "requiresApproval")
        worked, said = _choose(DISCONNECT_ITEM.removesuffix("…"))
        asked = worked and _wait_until(lambda: _says(DISCONNECT_TITLE), 15)
        _screenshot(self.screens / "p12-disconnect-question.png")
        self.need(asked, f"Disconnect asks first: {DISCONNECT_TITLE!r}", f"{said} {_texts()}")
        before = len(self.stderr_now())
        worked, said = _press(DISCONNECT_BUTTON)
        told = worked and _wait_until(lambda: _says(DISCONNECTED_TOLD), 120)
        _screenshot(self.screens / "p13-disconnected.png")
        self.checker.expect(told, f"then the app says {DISCONNECTED_TOLD!r}", f"{said} {_texts()}")
        sentence = _says(OFF_AFTER_DISCONNECT)
        added = told and sentence == registered
        self.checker.expect(
            added,
            f"its note adds {OFF_AFTER_DISCONNECT!r} exactly when the login item was registered: "
            f"it was {was}",
            "" if added else f"{_texts()}",
        )
        _press("OK")
        after = _value_within(lambda: _login_state(self.stderr_now()[before:]), 30)
        self.checker.expect(
            after in LOGIN_ITEM_GONE,
            "the pair gone, the app says on standard error 'login item: notRegistered' (or "
            "notFound, when macOS has none to find)",
            f"{after}",
        )
        _dump_login_items()
        self.checker.expect(not _alive(run), "run has exited")
        self.checker.expect(
            DRAINED_LINE in self.stderr_now(),
            f"run was drained before the pair was forgotten: the app says {DRAINED_LINE!r}",
            self.stderr_now()[-2000:],
        )
        self.checker.expect(self.pair_state() == "none", "the app's profile then holds no pair")
        found, shown = _menu_until(
            lambda items: NOT_CONNECTED in items and CONNECT_ITEM in items,
            60,
            shot=self.screens / "p14-not-connected.png",
        )
        self.checker.expect(
            found, f"the menu then says {NOT_CONNECTED!r}, and offers {CONNECT_ITEM!r}", f"{shown}"
        )
        self.checker.expect(
            SETTINGS_MENU not in shown, f"and no longer holds {SETTINGS_MENU!r}", f"{shown}"
        )

    def tidy(self) -> None:
        """Stop the app, the host's processes and the hub; forget whatever pair is left; put the
        profile back as it was; and check that nothing was written into the bundle."""
        if _running(APP_NAME):
            subprocess.run(["pkill", "-TERM", "-x", APP_NAME], check=False)
            self.checker.expect(
                _wait_until(lambda: not _running(APP_NAME), 15), "the app stops on SIGTERM"
            )
        if self.owns_profile and not _wait_until(lambda: not _profile_processes(), 90):
            left = _profile_processes()
            print(f"note: host processes of the app's profile still ran, and were killed: {left}")
            for pid in left:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        if self.owns_profile and self.url and self.profile.exists():
            print(f"forget, at the end: {_shown(self.host('--json', 'forget'))}")
        for child in reversed(self.children):
            child.kill()
        if self.owns_profile:
            shutil.rmtree(self.profile, ignore_errors=True)
        if self.set_aside is not None:
            try:
                self.set_aside.rename(self.profile)
                print(f"the app's profile is back at {self.profile}")
            except OSError as exc:
                self.checker.expect(
                    False, f"the app's profile is put back from {self.set_aside}", f"{exc}"
                )
        shutil.rmtree(self.folder, ignore_errors=True)
        _set_pasteboard("")
        done = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(self.app)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.checker.expect(
            done.returncode == 0,
            "the app's signature still verifies: nothing was written into the bundle",
            _shown(done),
        )
        for log in self.logs:
            text = log.read_text(encoding="utf-8", errors="replace")
            print(f"the app's standard error, {log.stem}: {text[-3000:]!r}")

    def go(self) -> None:
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("a hub of its own, and a fake Lucille", self.set_up),
            (
                "pairing through the command line, the SAS left pending",
                self.pair_through_the_harness,
            ),
            ("They Match, in the app", self.confirm_in_the_app),
            ("run reads the room Lucille offers", self.run_reads_the_room),
            ("Set Up, once connected", self.set_up_after_connected),
            ("files.sha256 on a folder", self.fingerprint),
            ("run killed while a note is up, and started again", self.backoff),
            ("the app killed", self.app_killed),
            ("the app opened again", self.relaunch),
            ("Quit from the note about the menu bar, while a request runs", self.quit_and_drain),
            ("Stop What's Running, while a request runs", self.stop_whats_running),
            ("Lucille asks for a newer release, then Prepare to Update", self.too_old_then_prepare),
            ("Quit, then Stop now", self.stop_now),
            ("paused at launch", self.paused_at_launch),
            ("Disconnect", self.disconnect),
        )
        try:
            for label, step in steps:
                print(f"--- {label}", flush=True)
                step()
        except _Stop as stop:
            print(f"the check stopped at: {stop}")
            _screenshot(self.screens / "p99-stopped.png")
        except Exception:
            self.checker.expect(False, "the pairing check ran to its end", traceback.format_exc())
            _screenshot(self.screens / "p99-stopped.png")
        finally:
            self.tidy()


def check_pairing(app: Path, checker: Checker, work: Path, require_driven: bool = False) -> None:
    """Pair the app's profile through the command line with a hub of the check's own and a fake
    Lucille, then drive the app: confirm, ``run`` and its supervision, Set Up, Activity, Quit, Stop
    What's Running, the update notice and Prepare to Update, Stop now and Disconnect
    (:class:`_PairingCheck`). ``app`` is the copy in /Applications. It uses the real HOME, where
    the app keeps its profile, and puts that profile back as it found it. Screenshots go to
    ``<out>/check/screens``."""
    # Imported here, not at the top: every other check runs where neither Switchboard nor the test
    # harness is installed (``uv run --no-project``), and tests/test_macos_build.py imports this
    # module wherever the suite runs. CI runs this check with the interpreter of the copy of the
    # runtime that ``suite`` made, which holds both.
    try:
        from switchboard.client import Client

        from tests._lucille import FakeLucille
        from tests._process import LISTENING, SWITCHBOARD, Child, LoopbackHub, child_env, until
        from tests._run import body
    except ImportError as exc:
        checker.expect(
            False,
            "the pairing check finds Switchboard and the test harness",
            f"{exc}; run it with the interpreter of the runtime's copy that suite --copy-to made",
        )
        return
    if not checker.expect(SWITCHBOARD.is_file(), f"Switchboard's hub is at {SWITCHBOARD}"):
        return
    harness = _Harness(
        child=Child,
        child_env=child_env,
        until=until,
        body=body,
        fake_lucille=FakeLucille,
        loopback_hub=LoopbackHub,
        client=Client,
        hub_program=SWITCHBOARD,
        listening=LISTENING,
    )
    (work / "screens").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lucille-host-pairing-") as tmp:
        _PairingCheck(app, checker, work, Path(tmp), harness, require_driven).go()


def check_gatekeeper(app: Path, dmgs: Sequence[Path]) -> None:
    assessments = [["spctl", "--assess", "--type", "execute", "--verbose=4", str(app)]]
    for dmg in dmgs:
        assessments.append(
            [
                "spctl",
                "--assess",
                "--type",
                "open",
                "--context",
                "context:primary-signature",
                "--verbose=4",
                str(dmg),
            ]
        )
    for argv in assessments:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        print(f"{' '.join(argv[:4])} ...: exit {done.returncode}")
        print((done.stdout + done.stderr).strip())
    print("An ad hoc signature is expected to be refused here until notarization exists.")


def check_dmg(dmgs: Sequence[Path], checker: Checker) -> None:
    if not checker.expect(
        len(dmgs) == 1, "exactly one disk image was built", f"{[dmg.name for dmg in dmgs]}"
    ):
        return
    dmg = dmgs[0]
    checker.expect(
        dmg.name.endswith(build_app.DMG_SUFFIX),
        f"its name, {dmg.name}, says that it is signed ad hoc",
    )
    print(f"{dmg.name}: {dmg.stat().st_size} bytes, sha256 {build_app.sha256_of(dmg)}")
    done = build_app.hdiutil(["verify", str(dmg)])
    checker.expect(done.returncode == 0, "hdiutil verify passes", _shown(done))
    name = build_app.load_pins().app_name
    with tempfile.TemporaryDirectory(prefix="lucille-host-dmg-") as tmp:
        mount = Path(tmp) / "mount"
        mount.mkdir()
        done = build_app.hdiutil(
            ["attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount), str(dmg)]
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
            manifest = app / build_app.MANIFEST_PATH
            if manifest.is_file():
                print(json.dumps(json.loads(manifest.read_text(encoding="utf-8")), indent=2))
        finally:
            for extra in ([], ["-force"]):
                if build_app.hdiutil(["detach", *extra, str(mount)]).returncode == 0:
                    break


PROBE_VERBS: Final = (
    ("status", ["--json", "status"], None),
    ("pair status", ["--json", "pair", "status"], None),
    ("pair --code-stdin, not a code", ["pair", "--code-stdin", "--json"], "swbp1_not-a-code\n"),
    ("run, standard input already ended", ["run", "--json-events", "--exit-on-stdin-eof"], ""),
)


def _probe_one(python: Path, home: Path, args: list[str], stdin: str | None) -> str:
    env = {
        "HOME": str(home),
        "PATH": "/var/empty",
        "LANG": "en_US.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", ""),
    }
    started = time.monotonic()
    try:
        done = subprocess.run(
            host_argv(python, ["--profile", APP_PROFILE, *args]),
            input=stdin if stdin is not None else None,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            cwd="/",
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after 60 s ({time.monotonic() - started:.1f} s)"
    took = time.monotonic() - started
    return f"{took:.1f} s, {_shown(done)[:2000]}"


def check_probe(app: Path) -> None:
    """Print what the verbs the app is about to use do on this runner. Printed only, never a gate:
    whatever the probe meets, an exception included, it prints and returns.

    Each runs as the app will run it: the app's flags, profile mac-app, PATH=/var/empty, working
    directory /. Once under a fresh temporary HOME and once under the runner's own HOME, because
    whether macOS's Keychain is usable under each is what the app's tests must be written for.
    """
    try:
        _probe(app)
    except Exception:
        print(f"note: the probe stopped: {traceback.format_exc()}")


def _probe(app: Path) -> None:
    python = python_in(app)
    for what in (["security", "list-keychains"], ["security", "default-keychain"]):
        done = subprocess.run(what, capture_output=True, text=True, check=False)
        print(f"{' '.join(what)}: {_shown(done)}")
    with tempfile.TemporaryDirectory(prefix="probe-home-") as temporary:
        for label, home in (("temporary HOME", Path(temporary)), ("runner HOME", Path.home())):
            for name, args, stdin in PROBE_VERBS:
                print(f"--- {label}: {name}")
                print(_probe_one(python, home, args, stdin), flush=True)


CHECKS: Final = (
    "bundle",
    "bytecode",
    "record",
    "version",
    "host",
    "wrapper",
    "environment",
    "readonly",
    "swift",
    "relocate",
    "links",
    "signature",
    "library-snapshot",
    "install",
    "places",
    "placement",
    "launch",
    "ui",
    "library-diff",
    "gatekeeper",
    "dmg",
    "startup",
    "suite",
    "pairing",
    "probe",
    "selftest",
    "handoff",
    "handoff-check",
    "unpack-dmg",
)
# The checks that run with no app at ``<out>``: the disk image's, those that put the app there,
# the first ~/Library snapshot, and the places check, which opens System Settings, never the app.
NEEDS_NO_APP: Final = frozenset(
    {"dmg", "unpack-dmg", "handoff-check", "library-snapshot", "places"}
)
# The checks that open the copy ``install`` put in /Applications, the only one that starts
# anything (decisions/0002 §13), and refuse to run unless it is a copy of this build: one an
# earlier build left there would answer for this one.
OPENS_INSTALLED: Final = frozenset({"launch", "ui", "pairing", "placement"})
# The options each check cannot run without, by their argparse names.
REQUIRED: Final = {
    "selftest": ("to",),
    "library-snapshot": ("to",),
    "library-diff": ("before",),
    "record": ("host_wheel_sha256",),
    "suite": ("copy_to",),
    "handoff": ("run_id", "run_attempt", "github_output"),
    "handoff-check": ("dmg_sha256", "selftest_sha256"),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_app.py", description="Check a built app.")
    parser.add_argument("check", choices=CHECKS)
    parser.add_argument(
        "--out", type=Path, default=build_app.DEFAULT_OUT, help="the folder build_app.py wrote to"
    )
    parser.add_argument(
        "--require-driven",
        action="store_true",
        help="ui, placement, pairing: fail if System Events cannot drive the app (macos-app "
        "passes this)",
    )
    parser.add_argument(
        "--record-only",
        action="store_true",
        help="places: print what each page shows, and fail nothing (the smoke jobs pass this)",
    )
    copies = parser.add_mutually_exclusive_group()
    copies.add_argument(
        "--replace",
        action="store_true",
        help="install: set aside a copy already in /Applications, and install over it",
    )
    copies.add_argument(
        "--restore",
        action="store_true",
        help="install: remove the installed copy, and put back the one --replace set aside",
    )
    parser.add_argument(
        "--selftest", type=Path, help="swift, relocate: run this compiled self-test"
    )
    parser.add_argument("--to", type=Path, help="selftest, library-snapshot: the file to write")
    parser.add_argument("--before", type=Path, help="library-diff: what library-snapshot wrote")
    parser.add_argument(
        "--control-run",
        action="store_true",
        help="library-diff: have the host write its state folder first (pause, then resume)",
    )
    parser.add_argument("--copy-to", type=Path, help="suite: the folder for the runtime's copy")
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        help="handoff-check, unpack-dmg: the hand-off folder (default OUT/handoff)",
    )
    parser.add_argument("--run-id", help="handoff: the workflow run's id")
    parser.add_argument("--run-attempt", help="handoff: the workflow run's attempt")
    parser.add_argument("--github-output", type=Path, help="handoff: append the outputs here")
    parser.add_argument("--dmg-sha256", help="handoff-check: the disk image's sha256")
    parser.add_argument("--selftest-sha256", help="handoff-check: the self-test's sha256")
    parser.add_argument(
        "--summary", type=Path, help="record, startup: also append Markdown to this file"
    )
    parser.add_argument(
        "--host-wheel-sha256", help="record: the sha256 of the host wheel the wheel step built"
    )
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument("--tag", help="version: the build was made from this v* tag")
    versions.add_argument("--build-number", help="version: the build number it was given")
    return parser


def _run_check(args: argparse.Namespace, out: Path, checker: Checker) -> None:
    app = app_path(out)
    work = out / "check"
    source = (args.source or out / HANDOFF_DIR).resolve()
    check = args.check
    if check == "bundle":
        check_bundle(app, checker)
    elif check == "bytecode":
        check_bytecode(app, checker)
    elif check == "record":
        check_record(app, out, checker, args.summary, host_wheel_sha256=args.host_wheel_sha256)
    elif check == "version":
        check_version(app, checker, args.tag, args.build_number)
    elif check == "host":
        check_host(app, checker)
    elif check == "wrapper":
        check_wrapper(app, checker)
    elif check == "environment":
        check_environment(app, checker)
    elif check == "readonly":
        check_readonly(app, checker)
    elif check == "swift":
        check_swift(app, checker, work, args.selftest)
    elif check == "relocate":
        check_relocate(app, checker, work, args.selftest)
    elif check == "links":
        check_links(app, checker)
    elif check == "signature":
        check_signature(app, checker)
        installed = installed_app_path()
        if installed.is_dir() and is_this_build(installed, app):
            check_signature(installed, checker)
        elif installed.is_dir():
            print(f"{installed} is another build, which no check opens: its signature is not read")
        else:
            print(f"no copy at {installed} yet to check: check_app.py install puts one there")
    elif check == "library-snapshot":
        check_library_snapshot(args.to.resolve(), checker)
    elif check == "install":
        check_install(app, checker, replace=args.replace, restore=args.restore)
    elif check == "places":
        check_places(checker, work, args.record_only)
    elif check == "placement":
        check_placement(app, checker, work, args.require_driven)
    elif check == "launch":
        check_launch(installed_app_path(), checker)
    elif check == "ui":
        check_ui(installed_app_path(), checker, work, args.require_driven)
    elif check == "library-diff":
        check_library_diff(app, args.before.resolve(), args.control_run, checker)
    elif check == "gatekeeper":
        check_gatekeeper(app, build_app.disk_images(out, APP_NAME))
    elif check == "dmg":
        check_dmg(build_app.disk_images(out, APP_NAME), checker)
    elif check == "startup":
        check_startup(app, work, checker, args.summary)
    elif check == "suite":
        check_suite(app, out, args.copy_to.resolve(), checker)
    elif check == "pairing":
        check_pairing(installed_app_path(), checker, work, args.require_driven)
    elif check == "probe":
        check_probe(app)
    elif check == "selftest":
        to = args.to.resolve()
        checker.expect(compile_selftest(to), f"the Swift self-test compiles to {to}")
    elif check == "handoff":
        write_handoff(out, args.run_id, args.run_attempt, args.github_output, checker)
    elif check == "handoff-check":
        check_handoff(source, args.dmg_sha256, args.selftest_sha256, checker)
    else:
        unpack_dmg(out, source, checker)


def missing_options(args: argparse.Namespace) -> list[str]:
    """The options ``args.check`` cannot run without that ``args`` lacks, as flags."""
    missing = [
        "--" + name.replace("_", "-")
        for name in REQUIRED.get(args.check, ())
        if getattr(args, name) is None
    ]
    if args.check == "version" and args.tag is None and args.build_number is None:
        missing.append("--tag or --build-number")
    return missing


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if sys.platform != "darwin":
        print("check_app.py: these checks run on macOS", file=sys.stderr)
        return 2
    missing = missing_options(args)
    if missing:
        print(f"check_app.py: {args.check} needs {', '.join(missing)}", file=sys.stderr)
        return 2
    out = args.out.resolve()
    app = app_path(out)
    checker = Checker()
    if args.check not in NEEDS_NO_APP and not checker.expect(app.is_dir(), f"{app} exists"):
        return 1
    installed = installed_app_path()
    if args.check in OPENS_INSTALLED and not checker.expect(
        installed.is_dir() and is_this_build(installed, app),
        f"{installed} is a copy of this build",
        "check_app.py install puts one there"
        if not installed.is_dir()
        else "it is another build or copy: check_app.py install --replace sets it aside and "
        "installs this one, and install --restore later puts it back",
    ):
        return 1
    try:
        _run_check(args, out, checker)
    except (CheckError, build_app.BuildError, build_app.PinError, payload.AssemblyError) as exc:
        checker.expect(False, f"{args.check} cannot go on", str(exc))
    if checker.failures:
        print(f"{len(checker.failures)} failed: " + "; ".join(checker.failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
