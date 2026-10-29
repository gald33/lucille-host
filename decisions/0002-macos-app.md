# 0002: Loft, the Mac app (draft)

Author: lucille-host maintainers. First written 2026-10-29. Status: **draft.** A first, smaller build exists
in [`macos/`](../macos/README.md); "What is built today", below, says what it is. Everything else in this
record is planned. It extends [0001](0001-host-design.md) and changes it where §21 says so. A pull request
that changes this design updates this record in the same pull request.

**The goal.** A person who has never opened Terminal installs the host on a Mac, connects it to their
Lucille account, chooses what it may do, and can stop or remove it, without a terminal, without installing
Python, and without Homebrew. Mac first; nothing here may block a Windows or Linux version later (§24).

**Conventions.**
- The conventions of 0001 apply: "Lucille policy as of <date>; not proven by this repository" marks what
  Lucille's side does, "sb" cites agent-switchboard 2.6.0, and "Not decided yet" marks open questions,
  listed again at the end.
- "Apple" cites Apple's developer documentation, read on 2026-10-29.
- "Not yet tested on a Mac" marks a statement that rests on documentation or reasoning and is settled only
  by the hardware tests in §19.
- "Measured" means run on 2026-10-29 in a Linux container, not on a Mac.
- Every macOS dialog quoted here is quoted from documentation or reports, not seen on a Mac.
- "Seen in CI" means run on 2026-10-29 on a GitHub-hosted macOS runner, by the first build's workflow.
- **The app is named Loft** ("Loft, by Lucille" where the brand helps), with the bundle identifier
  `com.lucille-ai.loft`. Where no name is needed, this record says "the app". Only what a user sees and the
  app's identifiers carry the name: the package, the command (`lucille-host`), its folders and profiles, and
  this repository keep theirs.

## What is built today

The first build is in [`macos/`](../macos/README.md), and its README is the reference for it. It is not a
release, and it is for people developing the app.
- **`Loft.app`**, a menu bar app for Apple silicon Macs with macOS 15 or later, with no main window and no
  Dock icon; and **`Loft-<version>-<commit>-adhoc-macOS-arm64.dmg`**, a disk image that holds it and a link
  to Applications.
- **Inside it**, a python-build-standalone CPython 3.13 at `Contents/Resources/python`, pinned in
  `macos/runtime.toml` by URL and sha256, with the host's wheel built from the checkout and the exact wheels
  `uv.lock` pins, installed by hash.
- **The app drives the host only through its command line**, as `python3 -I -B -m lucille_host <verb>`, with an
  argv and a fixed environment. It reads the host's text output and its pause flag, writes none of the host's
  files, and opens no network connection.
- **The menu** shows the version and "Not connected to Lucille", and offers Pause and Resume, the abilities
  (the operations, switched with `ops enable` and `ops disable`), Add a Folder…, the folders with Remove, and
  Quit Loft. "Connect to Lucille (coming soon)" is disabled, because the host cannot pair yet.
- **It is signed ad hoc**, with the hardened runtime and no entitlements, and it is not notarized, so macOS
  refuses to open it normally.
- **The `macos-app` workflow** builds it on GitHub's `macos-15` runner, runs each check in
  `macos/check_app.py` and the host's whole test suite on the trimmed runtime, and uploads the disk image as
  a 7-day artifact when every check passed.

Not built yet: pairing, running in the background and opening at login, the JSON contract (§8), the Welcome
and main windows, updates, **Remove Loft…** (section 17), Developer ID signing and notarization, ripgrep, and
the release jobs. The first build also departs from this record in these ways, among others;
`macos/README.md`, under "Choices made for the first build", says why for most of them:
- the interpreter is the runtime's own `python3`, not renamed, and signed ad hoc with
  `--prefix com.lucille-ai.loft.` but no identifier of the planned form, `com.lucille-ai.loft.runtime`; the
  `signature` check prints the identifier it gets (§6). The runtime ships hash-checked `.pyc` files (§6);
- the command line runs with `PATH=/usr/bin:/bin`, and without `-X utf8` (§6, §9);
- the app opens from any folder, where the planned app runs only from `/Applications/Loft.app` (§13);
- the app uses the host's `default` profile, the one a `pipx` install uses, not its own `mac-app` profile
  (§9);
- there is no `MANIFEST.json`. The build writes `Contents/Resources/build-manifest.json` instead, with the
  commit, the host's exact version, the host wheel's name and sha256, the runtime's file name, URL and
  sha256, the name and version of each installed distribution, what the build removed from the runtime, the
  `swiftc` version and the kind of signature. It lacks the sha256 of every installed wheel that §6 lists. The
  host's exact version is also in the `Info.plist` key `LucilleHostVersion`, and `CFBundleVersion` repeats
  the release numbers (§6, §7);
- the words for each ability and the suggested folder names are in the Swift app, not in the core (§8, §12,
  §13);
- the `Info.plist` has five folder usage keys, not six (no file providers), each with the sentences on what
  Loft is for, which macOS is meant to show under its folder questions (§12; not yet tested on a Mac);
- the app asks about `/` and the Data volume (`/System/Volumes/Data`) as it asks about the home folder, and
  adds either one after **Add Anyway**, where the planned app refuses both outright (§12). Here the first
  build allows more than this record;
- the build scripts and their pins are in `macos/`, not `tools/macos/`, and the checks on the Swift sources
  are in `tests/test_macos_build.py` and `check_app.py links`, not in a `macos_lint` guard (§20);
- the workflow is `macos-app.yml`, not a job in `ci.yml`, and it uploads the disk image on pushes and on the
  repository's own pull requests, not only on a manual run (§18).

Where this record says what the app does, it describes the planned app.

## 1. What the Mac app is

- **One disk image, one app.** A signed, notarized and stapled `.dmg` holds `Loft.app`. The user
  drags it into Applications.
- **The app is two parts.**
  - A small native menu-bar app, written in Swift. It shows windows and a menu, and it calls macOS.
  - The host itself: the release's own wheel, byte for byte as on PyPI, with the exact dependencies of the
    release's lock, inside an embedded CPython.
- **The app adds no power to the host.** It adds no operation, no protocol message, no listening port and
  no network connection of its own. Lucille can ask a Mac app for exactly what it can ask a `pipx` install
  for. The privacy claim in `PRIVACY.md` does not change (§21 lists two added sentences about macOS).
- **The Python core keeps every guard.** No GUI code enters `src/lucille_host/`. The runtime import
  allowlist does not grow. The core gains verbs and flags (§8, §10, §11, §15, §16 and section 17), all pure
  Python, all tested on Linux.
- **The command line stays.** A technical user can still install with `pipx` or `uv tool`, or run the host
  inside the app from Terminal (§6). The app keeps its own profile, so a `pipx` install on the same Mac is a
  separate host with its own pairing, folders, Keychain item and version record (§9).
- **English only in the first version.** All copy is English, from the core and the app. Localization, and
  right-to-left layout, are not decided yet.

## 2. Who it is for, and what "done" means

The user owns a Mac with Apple silicon and an administrator account, which is the macOS default for the
first user. They use Safari or Chrome and the Finder. They are signed in to Lucille. They are not expected
to verify anything; `VERIFYING.md` is for a technical friend, a reviewer or a journalist.

The app is done when:
1. **No Terminal.** Every step from Lucille's Computers page to "Lucille ran something on this Mac" happens in
   the browser, the Finder, the app or a macOS dialog.
2. **About 12 actions** take a first-time user from the Computers page to connected, with one folder and one
   operation on, in 10 minutes or less.
3. **No macOS dialog is a surprise.** The app or Lucille's page announces each one, just before it appears.
4. **Every mistake can be undone** inside the app or on Lucille's page (§3.3), except the two the table says
   are not: an unusable Keychain, and a bad release.
5. **Stopping is one click** in the menu bar. **Removing is one menu item** and leaves no secret behind.
6. **Three people who have never used Terminal** connect, add one folder and turn on one operation, watched
   and not helped. Every place they stall is fixed or explained on screen first (§19).

## 3. What the user does

### 3.1 The final form

1. On Lucille's Computers page: **Download Loft for Mac**. The link points at this repository's
   GitHub Releases, never at a copy on Lucille's servers (Lucille policy as of 2026-10-29; not proven by this
   repository). The page states "A Mac with Apple silicon (any M-series chip, or MacBook Neo), macOS 15 or
   later" and how to check.
2. The user opens the download. A Finder window shows the app, an arrow and the Applications folder. They
   drag one onto the other. (A standard account may need an administrator's name and password here; that is
   the Finder protecting Applications, not this app.)
3. They open Loft. macOS asks once whether to open an app downloaded from the internet; Lucille's page
   shows a picture of that dialog beforehand. They click **Open**.
4. **Welcome**: what the app does, in the claim's own terms, and "Nothing runs until you connect and
   confirm." One button: **Connect this Mac to Lucille**. The app says: "Your browser will open Lucille. If it
   asks to open Loft, click Open."
5. Lucille's connect page, signed in: "Connect a computer to your Lucille account (a•••@example.com)? Only
   continue if you just clicked Connect in Loft on this Mac." The page never submits on its own. The
   user clicks **Connect**; the browser asks "Open Loft?"; they click **Open** (Lucille policy as of
   2026-10-29 for the page; not proven by this repository).
6. The app: "Connecting to the Lucille account a•••@example.com. Is this your account?" Then: "Type the
   8-digit number shown on Lucille's page." The user types it. The app answers at once: "Matches. Now click
   **They match** on Lucille's page." (§11.2; that Lucille's page shows the number and a **They match** button
   is Lucille policy as of 2026-10-29; not proven by this repository.)
7. The user clicks **They match** on Lucille's page (Lucille policy as of 2026-10-29; not proven by this
   repository). The app shows **Connected** once Lucille's work room arrives.
8. **Folders**: **Choose a Folder…** opens the standard Open panel. The app suggests a short name, which the
   user may change (§12).
9. **What Lucille may do**: one switch per operation, all off (§13).
10. **Finish**: "Open Loft when you log in" (on). "macOS will show a notice that Loft can now
    open when you log in. That is expected. You can change it later in System Settings › General › Login
    Items & Extensions." **Done.**

From then on an icon in the menu bar shows the state and offers Pause, Stop what's running, Activity, and
Quit (§14).

### 3.2 The first form, for the repository owner's own Mac

Until the project's Apple Developer membership exists, the repository owner tries ad-hoc signed builds on
their own Mac only (§18). macOS refuses to open such a build normally; the owner opens it once per build
through System Settings › Privacy & Security › **Open Anyway**, or by removing its quarantine attribute.
Each new ad-hoc build is a new code identity to macOS, so its Keychain and folder prompts come again with
every build. Nobody else is given one, and nobody is told to work around the warning.

The first signed builds go only to the repository owner, as draft releases that are never published (§18).
They pair by pasting the code: on Lucille's page **Add computer**, **Copy**, then the user pastes it into the
app's code field (⌘V, or Edit › Paste). The app never reads the pasteboard itself (§11.3). The typed number of
step 6 is the same; there is no account check, because such a code carries no account hint. That form is
never published, because anyone can make a code that passes the host's code checks (§11.1).

### 3.3 Mistakes, and how they are undone

| Mistake | What the user sees | Fix, with no Terminal |
|---|---|---|
| Opened the app from the disk image, Downloads, or anywhere but Applications | "Loft needs to be in your Applications folder" | Drag it there; **Show Applications Folder** opens the Finder (§6) |
| Cancelled the browser's "Open Loft?" | Lucille's page: "Didn't open? Try again" (Lucille policy as of 2026-10-29; not proven by this repository) | Re-sends the link; the Connect request lives 20 minutes |
| Closed the browser tab mid-connect | App: "Waiting for Lucille…" with **Start Over** | A new Connect |
| Typed the wrong number | "That doesn't match. Type the number Lucille's page shows." After 3 tries: "Nothing was connected. In Lucille, click They differ, then start again." | **Start Over** |
| Clicked **Don't Allow** on a folder prompt | "macOS is blocking Loft from this folder" | **Open Privacy Settings** (§12) |
| Chose a folder that is too broad | A second sheet, safe choice as default (§12) | **Choose a Smaller Folder** |
| Moved, renamed or unplugged a chosen folder | "The folder documents can't be found" (from `roots check`, at launch and in Folders) | **Choose Again**, which removes the alias and adds the folder from the Open panel. A different folder later created at the old path is not detected (not decided yet, §12) |
| Turned off the login item in System Settings | On next open: "Loft won't start when you log in" | **Turn Back On** |
| Quit the app and forgot | Lucille's page shows the computer as not answering (Lucille policy as of 2026-10-29; not proven by this repository) | Open the app |
| Paused and forgot, or a task was stopped at Quit or log out (§9) | A pause badge on the icon; at launch, "Loft is paused" | **Resume** |
| The Mac's clock is off by more than 300 s | "Your Mac's clock is off by 7 minutes, so Lucille can't reach it" | **Open Date & Time Settings** |
| The settings file became invalid | "Loft's settings can't be read, so it is doing nothing" | **Reset Settings** (`policy reset`, §10): folders and operations start from the fresh policy |
| The Keychain is locked or unusable (`run` exits 23) | "macOS's Keychain isn't available to Loft" | **Try Again**; if it persists, restart the Mac, then **Copy Diagnostics** to ask for help. Not always undone in the app |
| An update turned an operation off | "Search inside files was turned off because the update changed it" | **Review and Turn On** |
| Lucille needs a newer host | "Lucille needs Loft 0.3.0 or newer" (the minimum comes from Lucille's request; Lucille policy as of 2026-10-29; not proven by this repository) | **Download**, which opens the release page (§16) |
| Chose **Keep Both** when dragging an update, making "Loft 2" | "This copy is named Loft 2, so it can't run" | Move Loft 2 to the Trash, then drag the new app again and choose **Replace** |
| A release turned out bad, and the older one refuses to start after it (§16) | "This Loft is older than the one you used before" | None in the app: the only fix for a bad release is the next release |
| Removed the computer on Lucille's page | "This Mac was disconnected in Lucille" | **Connect Again** |
| Dragged the app to the Trash instead of Remove | Nothing on the Mac; Lucille's page explains (section 17) | Reinstall, then **Remove Loft…** |

## 4. Packaging

**What we chose.** One `.dmg` with one app and an Applications link, signed, notarized and stapled. No
install-time code runs. The bundle is assembled by a Python build script, held to the repository's AST lint,
from a SwiftPM-built executable and a runtime tree (§6).

**What we did not choose, and why.**
- **A `.pkg` installer.** It needs a second certificate (Developer ID Installer), and it normally needs an
  administrator password: a package can declare an install into the user's home folder instead, but reports
  say that is unreliable (Apple's installer-dev list; not yet tested on a Mac). With an administrator
  password its scripts run as root, which the host refuses (0001: it refuses uid 0). It brings no
  uninstaller.
- **Briefcase.** A second framework, in beta, with its own launcher and support package, whose failures
  cannot be debugged without a Mac.
- **PyInstaller, py2app, Nuitka.** A frozen executable finds imports by heuristic (keyring finds its macOS
  backend through an entry point), adds a prebuilt bootloader as a trust root, and no longer compares file by
  file with the PyPI wheel. Verification gets harder; surprises appear only on a Mac.
- **A Python GUI (rumps, Toga, PyObjC).** `objc`, `AppKit` and `ctypes` are outside the runtime import
  allowlist, and registering a login item from Python fails the `register()` lint.
- **A web UI** (local server, Electron, Tauri). A local server breaks "opens no listening port"; the others
  bring a browser engine and a network stack.
- **The Mac App Store and the App Sandbox.** The sandbox moves state into a container, needs a bookmark for
  every folder, and allows updates only through the store.
- **A Homebrew cask.** Not for this user. It may come later for technical users.

## 5. Which Macs

**What we chose** (decided on 2026-10-29). Apple silicon only, macOS 15 or later (`LSMinimumSystemVersion`
15.0, `arm64` only). That covers every M-series Mac and the MacBook Neo, whose A18 Pro chip is Apple silicon
but not M-series (Apple launched it on 2026-03-04 with macOS 26; not yet tested on one). CI builds on macOS 15 and runs the built app
on macOS 15, 26 and 27; the first build's workflow runs on macOS 15 only. GitHub's hosted macOS 27 image
has the label `xcode-27`: since that image, GitHub
keys a new macOS image by its major Xcode version, not by its macOS version ("Each image supports one major
Xcode version rather than targeting a specific operating system version", GitHub changelog, 2026-10-15,
which calls it generally available; the runner-images README of 2026-10-29 still marks it preview; its
image of 2026-10-22 reports macOS 27.0.1). Only what needs a person (§19) is tested by hand.

**Why.**
- `cryptography` publishes no Intel macOS wheel since 49.0.0; the pinned 50.0.2 has arm64 wheels only (PyPI,
  read 2026-10-29).
- An Intel build would need `cryptography` 48.0.1, which misses a security fix in 50.0.0, or a build from
  source that upstream no longer supports.
- macOS 26 is the last macOS for Intel Macs. macOS 15, 26 and 27 are the versions Apple still patches.

An Intel Mac refuses the app with macOS's own message. **Not decided yet:** Intel support.

## 6. Inside the bundle

```
Loft.app/Contents/
  Info.plist
  MacOS/Loft                           the Swift menu-bar app (arm64)
  Helpers/rg                           ripgrep, built from pinned source (§13), from the first public release
  Resources/
    MANIFEST.json                      versions; sha256 of the runtime tarball, of every wheel installed,
                                       of the release wheel; the Swift source commit
    bin/lucille-host                   a three-line sh wrapper for Terminal users (a resource)
    runtime/                           python-build-standalone CPython 3.13, pinned, pruned
      bin/lucille-host-runtime         the interpreter, renamed
      lib/python3.13/                  stdlib, lib-dynload, site-packages
    licenses/                          CPython, ripgrep and every dependency's licence text
```

- **The runtime** is python-build-standalone's `aarch64-apple-darwin` `install_only_stripped` CPython 3.13.
  Its URL and sha256 are pinned in one file; the build refuses any other bytes. Its interpreter links Python
  statically, so the bundle holds no `libpython3.13.dylib` (seen in CI: the first build removes it, and its
  `links` check, which fails on a library the build removed, passes).
- **The wheels** are installed unchanged, with hashes required and binaries only: the release wheel, and the
  release lock's pins for macOS. Measured: the locked dependencies install for `aarch64-apple-darwin` on Linux,
  22 pins, 18 MB, with exactly two arm64 Mach-O extension files (`cryptography`'s Rust extension and `cffi`'s
  backend). So everything but the Swift binary can be assembled and inspected on Linux.
- **Nothing is generated that a wheel does not contain**, except what the installer itself writes. Console
  scripts are deleted, and no `.pyc` is shipped (below). So every file under `site-packages` is byte for byte
  a file of a wheel's `RECORD`, except the installer-written files of each `*.dist-info` folder:
  `INSTALLER`, `REQUESTED` and `direct_url.json` where written, and `RECORD` itself, which the installer
  rewrites. Those are named, and each rewritten `RECORD` is compared line by line with the wheel's own. No
  `*.dist-info` folder is removed: keyring finds its macOS backend through an entry point.
- **Pruning** removes what the host never runs (headers, the static library, tests, `idlelib`, `tkinter`,
  `ensurepip`, Tcl/Tk). The list is a reviewed file. CI runs the host's whole test suite on a copy of the
  pruned runtime, so a wrong cut fails a pull request.
- **No symbolic link** is left inside the bundle, unless the fallback below is needed. Each one in the
  runtime is replaced by its target or removed. A build test asserts none remain.
- **The interpreter runs as** `lucille-host-runtime -I -B -X utf8 -m lucille_host`. `-I` ignores every
  `PYTHON*` variable and the user's site folder. `-B` stops `.pyc` writes, which would break the bundle's
  seal.
- **No `.pyc` under `site-packages`.** A precompiled file is in no wheel's `RECORD`, and an
  `unchecked-hash` one is loaded without being checked against its source, so the bytes the interpreter
  ran would be the bytes nothing verified. Without them every module is compiled in memory at each start.
  The cost is start-up time, measured on the first build that ships no `.pyc` (the first build, see "What is built
  today", ships hash-checked `.pyc` files, so it measures nothing here). If it is too slow, the fallback is
  `checked-hash` files that `verify_app.py` recompiles with the bundled interpreter and compares. **Not
  decided yet** until that measurement. (The runtime's own files are whatever its pinned tarball holds.)
- **It is renamed** so that Activity Monitor and any macOS prompt name it, not `python3.13`, and it has its own
  signing identifier (`com.lucille-ai.loft.runtime`).
- **Mach-O files under `Contents/Resources`.** Apple's placement table ("Placing Content in a Bundle") puts
  libraries in `Contents/Frameworks` and a helper tool in `Contents/MacOS` or `Contents/Helpers`, and warns
  that incorrectly placed code "might cause problems during notarization". The interpreter is a helper tool,
  so this layout departs from the table twice. The first signed build tests it. If Apple refuses it, there
  are two ways out (`macos/README.md`, "Not done yet"): a `Python.framework` layout under
  `Contents/Frameworks`, as Briefcase uses; or every extension moves into `Contents/Frameworks` and the
  interpreter into `Contents/Helpers`, beside `rg`, with `install_name_tool` edits and a symbolic link left at
  each old place so that Python still finds each module, which relaxes the rule below for exactly those
  links. The first signed build also tests the fallback, so it is known to work before it is needed. **Not
  decided yet** until that build.
- **Size**: the dependencies are 18 MB (measured). The first build's disk image is 37.9 MB (seen in CI); it
  ships hash-checked `.pyc` files and the runtime's own interpreter, so a release will differ.

**What we did not choose, and why.**
- **A small launcher embedding Python in place of the renamed interpreter.** It would make the Keychain
  identity (§7) usable only for running the host. Today any process running as the user can run the bundled
  interpreter with its own script and read the host's Keychain items without a prompt, exactly as with a
  `pipx` install. Same-user code is out of scope (THREAT_MODEL.md), so this buys defence in depth only, at
  the cost of C code and a link step against the runtime that is not yet tested. **Not decided yet**; it
  would keep the same signing identifier so that existing Keychain items stay readable.

## 7. Signing and notarization

- **One Developer ID Application certificate.** No Installer certificate.
- **Inside out, never `--deep`:** every library and extension, deepest first; then the interpreter with its
  own identifier; then `rg`; then the bundle. On a Developer ID build, each with `--timestamp --options
  runtime`. Verification may be deep: `codesign --verify --strict --deep`.
- **Developer ID builds: hardened runtime on every executable. No entitlements.** No
  `disable-library-validation`, no `get-task-allow`, no sandbox. Every library is signed by the same team,
  so library validation should pass (not yet tested on a Mac; the first candidate settles it). One
  question stays open until the first signed build: whether the interpreter needs
  `allow-unsigned-executable-memory`. No Python file in `cryptography`, `switchboard` or `keyring` imports
  `cffi` (measured), but `cryptography`'s Rust extension initialises a cffi module. If the signed build
  proves the entitlement is needed, it goes on the interpreter only, with the reason. **Not decided yet.**
- **Whether ad-hoc builds keep the hardened runtime is not decided yet.** The first build (see "What is built
  today") is signed ad hoc **with** the hardened runtime and no entitlements.
  - Why it was first expected to fail: under the hardened runtime, library validation lets a process load
    only code "signed by Apple or signed with the same Team ID as the main executable" (Apple, "Disable
    Library Validation Entitlement"). An ad-hoc signature has no Team ID, and a reported ad-hoc,
    hardened-runtime app aborted at launch with dyld's "mapping process and mapped file (non-platform) have
    different Team IDs" (dotnet/macios#26609, macOS 26.6.2). This bundle loads the `lib-dynload`
    modules it imports and two wheel extensions, so it was expected to fail the same way, and an earlier
    draft of this record kept the hardened runtime out of ad-hoc builds for that reason.
  - Seen in CI: on GitHub's `macos-15` runner (macOS 15.7.9) the first build's check loads `cffi`'s backend,
    `cryptography`'s Rust extension and keyring's macOS backend under the hardened runtime. So the failure
    above did not occur there. It has not been run on macOS 26 or 27.
  - **Library validation against a Team ID is exercised only on Developer ID-signed candidates.** Whether
    this bundle loads under it is not decided yet, until the first candidate runs. (Not chosen: a CI-only
    `disable-library-validation` entitlement on ad-hoc builds. It rests on that entitlement being honoured
    on an ad-hoc signature, which no source read establishes.)
- **One notarization: the disk image.** The app is signed, the disk image is built from it and signed, and
  only the disk image is notarized and stapled. Apple: "If you distribute your product using nested
  containers, only notarize the outermost container" ("Packaging Mac Software for Distribution"), and the
  notary service "generates a ticket for the top-level file … as well as for each nested file"
  ("Customizing the notarization workflow"). Whether a first launch of the copied app then works with no
  network is not yet tested on a Mac; the first candidate tests it. Only if it fails is the app notarized
  and stapled too, before the disk image is built, which costs a second notary round per build. The
  notarization log is kept with every build, success or not.
- **Identifiers are permanent.** The bundle identifier (`com.lucille-ai.loft`), the interpreter's identifier and the Team ID
  decide macOS's privacy grants, the Keychain's trust and the login item (§16 cites the sources). Changing any
  of them after release makes macOS ask every user again.
- **One version number, for a build from a tag.** For a release or a candidate built from a `v*` tag,
  `CFBundleShortVersionString` and `CFBundleVersion` both equal `lucille_host.__version__`, which equals the
  tag. Such a build refuses a suffix: under the protocol's version comparison, `0.1.0+mac1` would count as
  older than `0.1.0` for `min_host_version`. A Mac-only fix ships as a new host patch release, on PyPI too.
- **Any other build** (a pull request, or a candidate from `main`, where `__version__` is a development
  version such as `0.1.0.dev0`) maps it to numbers macOS accepts: `CFBundleShortVersionString` from its
  release part (`0.1.0`), and `CFBundleVersion` from the workflow's run number. `MANIFEST.json` records the
  real `__version__`. Such a build is never a release; the host's own version checks read `__version__`, not
  `Info.plist`.

## 8. How the app talks to the host

**What we chose.** The app calls the host's own CLI verbs and reads their output. Nothing else.
- **A versioned JSON contract.** A global `--json` flag makes every verb write one JSON object per line on
  standard output, each with `"v": 1` and a `"type"`. Standard error stays human. The prose is
  `spec/CLI.md`; the schemas and golden fixtures ship in the wheel, beside the protocol's. Readers ignore an
  unknown `type`, so the contract can grow.
- **The app never parses human text and never writes the host's files.** Every change goes through a verb.
- **Secrets never go on a command line.** A pairing code goes to `pair --code-stdin` on standard input. The
  typed number goes to `pair confirm` on standard input.
- **URLs come from the core.** Lucille's web origin, the releases page and the hub are constants in the core,
  beside the hub's. The Swift source holds no `https://` literal.
- **Copy that every shell needs comes from the core:** each operation's plain-language summary and what it
  sends (§13), alias suggestions (§12), the claim text, and the uninstall steps (section 17).
- **The app decides nothing from prose.** Where it must branch on the core's answer (which switch is on,
  whether an operation sends file contents), the contract carries a field for it.

**What the core gains** (all pure Python, no new import):

| Verb or flag | What it does |
|---|---|
| `--json` on every verb | the contract above |
| `ops list` fields `summary`, `sends`, `diagnostic` | plain-language text, kept **outside** each entry's definition hash, so changing copy never turns an operation off and does not change `CATALOG_VERSION` |
| `ops list` fields `enabled` and `sends_file_content` (booleans) | beside `state`, so a switch's position never depends on reading a state string (today's text merges "enabled but unavailable" and "disabled and unavailable" into one `unavailable`), and the acknowledgement of §13 depends on a field, not on the words of `sends` |
| `roots add --suggest-alias` | a valid, unused alias from a folder's name |
| `roots check [<alias>]` | opens the folder and reads one entry: `ok`, `denied_by_macos` (EPERM), `denied` (EACCES) or `missing`. Sends nothing anywhere |
| `policy reset` | moves an invalid policy file aside and writes the fresh policy (§10) |
| `policy previous` | lists, read only, the folders and operations of the policy a new pair set aside (§11.6) |
| `pair --code-stdin`, `pair status`, `pair confirm`, `pair reject` | non-interactive pairing (§11) |
| `pair begin` | the Connect request (§11.4) |
| `run --json-events --exit-on-stdin-eof` | supervision (§9) |
| `status`, `log --limit N`, `forget` | as 0001 lists them, with JSON |
| `uninstall-data --yes`, `diagnostics` | section 17, §15 |

**What we did not choose, and why.** XPC, a unix socket or a local HTTP port between app and host: each is a
listener, or needs `socket` or PyObjC in the runtime. Pipes from a parent to its own child are not a listener.
A status file the host writes for the app: the event stream of `run` already carries the state, and one
channel is easier to test.

## 9. How the host keeps running

**What we chose.** The host runs as a child of the menu-bar app:

```
login ─► Loft (menu bar; an "Open at Login" item)
            │ starts, with an argv, a built environment, and a pipe on standard input it holds
            ▼
         lucille-host-runtime -I -B -X utf8 -m lucille_host --json run --json-events --exit-on-stdin-eof
            │ standard output: JSON events to the app, kept in memory only
            │ outbound HTTPS to the one hub (unchanged)
            ▼
         catalog executables, with the core's own child environment
```

- **The host runs only while the icon is in the menu bar.** That is a rule a user can read: quit, and it is
  off. The converse does not hold: the icon stays while the computer is not paired, before its gate opens,
  after a crash loop stops, and after `run` exits 20, 23 or 25, and the icon's state says which.
- **Start.** The app starts `run` only when the computer is paired and its local gate is open. Before that,
  nothing runs in the background.
- **Open at login: on by default, with a switch** (decided on 2026-10-29). The app registers itself as a
  login item (`SMAppService.mainApp`) when onboarding finishes, and unregisters whenever the pair ends. The
  finish screen announces macOS's notice. A switch in Settings turns it off. The command line is unchanged:
  nothing starts `lucille-host run` at login.
- **The lifeline.** `--exit-on-stdin-eof` makes `run` drain, exactly as on its first SIGTERM, when its standard
  input closes. If the app crashes or is force-quit, the kernel closes the pipe and the host finishes its
  operation, replies, and exits. No host runs unseen.
- **One host per profile.** `run` holds `state/run.lock` (`os.lockf`). A relaunched app, or a `run` from
  Terminal, finds it held and says "already running".
- **The app's own profile.** The app runs every verb with `--profile mac-app`, and so does the Terminal
  wrapper inside it (§6). A `pipx` install on the same Mac keeps the `default` profile: its own folder under
  `~/Library/Application Support/lucille-host`, its own Keychain item, its own `run.lock` and its own record
  of the last version run (§16). Without this, an older `pipx` host would refuse to start after the app ran
  a newer one (exit 25), or the app would refuse because of the command line's version, and a Keychain item
  the signed interpreter created would likely prompt when the `pipx` Python reads it (not yet tested on a
  Mac). The cost: a Mac with both is two computers to Lucille, each paired on its own. README says to use
  one. Coexistence is tested by hand (§19).
- **Exit codes of `run`**, in `spec/CLI.md`: 0 stopped on purpose (drained, forgotten, ended by Lucille); 20
  not paired, or the local gate not open; 21 already running; 23 keyring unusable or insecure; 24 refused
  environment (`SWITCHBOARD_*`, uid 0); 25 a downgrade (§16); anything else is a crash. A `stopped` event with
  a reason comes before every planned exit.
- **Crashes.** The app restarts `run` with backoff (2, 4, 8 … 300 s). After five crashes in ten minutes it
  stops and shows "Loft keeps stopping", with the last lines of standard error behind a disclosure.
- **Quit.** The menu asks first: "Your Mac will stop answering Lucille until you open Loft again." The
  app then closes the pipe and shows "Finishing the current task…" with **Stop now**, which runs `kill`.
  `kill` also sets the pause flag (`controls.py`), so the sheet says so: "Stop now (Loft will be
  paused when you next open it)".
- **Logout, restart, shutdown.** The same, with the drain capped at 20 s. At the cap the app runs `kill`, so the
  operation's process group is killed and the reply `failed killed_locally` is still posted. Only a force-quit
  of the whole session leaves the known gap of an operation running without its time limit
  (THREAT_MODEL.md, "Known gaps").
- **After a kill at Quit or log out**, the host is paused at the next launch, which the user did not choose
  after a log out. The app does not resume it on its own: a pause nobody asked for fails safe. The next
  launch says "Loft is paused. This happens when a task is stopped at Quit or log out." with
  **Resume**.
- **The child's environment** is built, never copied: `HOME`, `USER`, `LOGNAME`, `TMPDIR` from the session,
  `LANG=en_US.UTF-8`, `PATH=/var/empty`, and the working directory inside the host's own state folder. agent-
  switchboard 2.6.0 runs `git` through `PATH` and treats a failure as "no value" (sb `client.py:117-131`); on a
  Mac without the developer tools, `/usr/bin/git` is a stub that opens an install dialog. An empty `PATH`
  makes that fail quietly. The real guarantee is a test that pairing and the read loop start no process
  (§20, M13); this is a second belt.
- **macOS's background rules.** Apple, "Managing ongoing background processes in your Mac": "The background
  termination rules don't apply to apps which have a visible presence on screen, such as an icon in the
  upper right part of the menu bar", and processes an app starts "need to end when an app quits or need to
  give people the capability to stop them". This model takes the stricter half: the child ends when the app
  quits. If a user hides the icon (macOS 26's menu-bar settings) and turns off the app's background
  activity (System Settings; Apple's page names no macOS version for that control), macOS may end the app;
  opening it again shows Settings (not yet tested on a Mac).
- **App Nap** should not throttle the child, which is not an app (not yet tested on a Mac). **Sleep and
  wake** are 0001's: the app adds nothing.

**What we did not choose, and why.**
- **A launch agent** (`SMAppService.agent`, the shape Apple documents for a process that outlives its app).
  It would keep the host running after the user quits the icon, which needs a second set of stop commands to
  explain. launchd's `ExitTimeOut` can cut a 300 s drain. A registration made from a disk image or a moved copy
  keeps the old path (reported, open with Apple). Whether macOS holds the app responsible for an agent's
  file access is likely but not established. It stays the fallback if the hardware tests (§19) show the child
  model failing.

## 10. Local policy: reloaded when it changes

This changes 0001's "Local policy and upgrades".
- **Reload on change.** Before each decision, `run` compares the policy file's inode, modification time and
  size with what it loaded, and reloads if any changed. A folder the user removes, or an operation turned off,
  applies to the next request. No restart, no signal.
- **An invalid file still fails closed**, but `run` no longer exits: it acts on nothing, reports
  `policy_invalid`, and resumes when the file is valid again. `policy reset` moves an invalid file aside and
  writes the fresh policy.

## 11. Pairing

### 11.1 What a code check cannot establish

- The host's code checks are "names Lucille's hub" and "carries no token" (0001, "Pairing"). The managed hub's
  token is published (sb `config.py:109,127`), so **anyone with the public library can make a code that passes
  both.**
- The short authentication string (SAS) proves that two screens share one pairing. It never proves that the
  other end is the user's Lucille account. Against a code an attacker made, the two numbers agree, because the
  attacker is honestly the other end.
- A code's note is plain JSON inside the code and is **not** part of what the SAS covers: the transcript binds
  only the initiator, the responder and the commitment (sb `pairing.py:464-467`), and `encode` writes the note
  as is (`:539-540`). Anyone who intercepts a code can read its note.

So a "paste a code" app, published, would be a ready-made tool for a scam: a page makes a code, tells the user
to paste it, and shows the matching number. The design answers that in three parts: the computer's gate is
typed (§11.2), the final form redeems only a code the computer asked for (§11.4), and the paste form is never
published (§3.2, §18).

### 11.2 The computer's gate is the typed one

This changes 0001's "The host's gate", for every shell, the command line included.
- **The user types Lucille's number into the computer.** The host never shows its own SAS before its gate
  opens. `pair confirm` reads the digits on standard input (spaces and `-` ignored) and compares them in
  constant time.
- A wrong entry leaves the gate shut. **Three wrong entries forget the pair**, and the host says to start
  again in Lucille.
- **Digits typed before the host has the SAS** (the pair still `waiting`, before a resolve has returned the
  SAS) are refused with "Not ready yet: wait until Lucille's page shows the number", and **do not count** as
  a wrong entry.
- **The deadline** is still about 24 h, but 0001 counts it "from when the SAS is first shown on the host",
  which no longer happens. It now counts from the first resolve that returns the SAS to the host.
- **0001's other gate lines change too.** "`pair status` shows the SAS again" is struck: `pair status`
  shows no digits before the gate opens. The interactive `n` answer is struck: the prompt reads digits, and
  a user who does not want the pair runs `pair reject`. `pair reject`, three wrong entries, and the deadline
  passing each forget the pair. Until the gate opens, the host posts nothing, touches nothing, accepts
  nothing and runs nothing (spec/PROTOCOL.md §16, unchanged).
- Lucille's own gate keeps its form: Lucille's page shows the number, and the user clicks **They match** there
  (Lucille policy as of 2026-10-29; not proven by this repository).

**Why typed on the computer.** Suppose an attacker can read the page or the hand-off (a malicious browser
extension, say). It takes Lucille's code, learns its note, and redeems it itself: Lucille ↔ attacker. It makes
its own code with the same note and hands it to the computer: computer ↔ attacker. The two numbers now differ.
If the computer's gate were a click, the user could open it toward the attacker, and Lucille's later mismatch
could not reach the computer over a pair the attacker holds. With the number typed into the computer, the
computer's gate stays shut. A careless **They match** on Lucille then exposes only Lucille's side (job
arguments and forged results), never the user's files.

### 11.3 The first form: paste

For the repository owner's own Mac only (§3.2). The app shows a code field; the user pastes into it
(⌘V, or Edit › Paste). The app never reads the pasteboard itself: Apple's `NSPasteboard.AccessBehavior`
(macOS 15.4 and later) makes the general pasteboard ask on a programmatic read, and reports conflict on
whether macOS 26 ships that alert (not yet tested on a Mac), while a paste the user makes is their own
act. The app checks the `swbp1_` prefix and a plausible length, and passes the code to
`pair --code-stdin --json`. The host does every real check. After accepting, `pair --code-stdin --json`
waits as 0001's interactive `pair` does (a resolve every 5 s and at once on a pairing note, resending an
answer that has not reached the hub), writes one event per state change, and exits once the SAS is pending
(exit 0) or the pairing reached a terminal state. If the app is relaunched meanwhile, it calls
`pair status --json`, which resolves once and resends, every 5 s while the state is `waiting`. Each refusal
maps to one sentence ("This code isn't from Lucille. Nothing was
connected."). The two terminal messages of 0001 stay distinct: "This code may have been used by someone else.
Remove this computer in Lucille and start again" and "The code expired. Click Add computer again."

### 11.4 The final form: Connect from the computer

The host redeems only a code that answers a Connect request it made. That is OAuth's `state` rule (RFC 8252
§8.9) applied to a pairing code, and it lives in the Python core, so the command line and any future shell get
it.
1. `lucille-host pair begin` makes a 128-bit nonce `N`, keeps only its sha256, a 20-minute expiry and an
   unused flag in the state folder (0600, written atomically), and prints the connect URL: Lucille's web origin
   (a core constant), path `/computers/connect`, with `N`, a format version and the kind of shell asking
   (`c=app` or `c=cli`) in the fragment. One request is outstanding at a time; a new `begin` replaces it.
   **No computer name is sent**: the Mac's name is often the person's own, and `PRIVACY.md` does not list
   it. The user names the computer on Lucille's page.
2. The app opens that URL in the default browser. That is the user's browser, not a connection of the app.
3. Lucille's page (signed in) starts the pairing with the code's note set to
   `lucille-host/1 n=<N> who=<masked account>`. For `c=app` it hands the code back through the URL scheme
   (§11.5) and shows no copyable code unless the user asks for it under "Having trouble?", with a warning.
   For `c=cli` it fires no link and shows the code with plain instructions: paste it into the terminal where
   `lucille-host pair` is waiting (Lucille policy as of 2026-10-29; not proven by this repository). The note
   stays in both sides' pair records: Lucille's library saves it with the pending pairing and the host's
   saves it as the peer's note (sb `pairing.py:1451-1462`, `:1604-1605`). That is harmless: `N` is single-use
   and already burned on the host, and the account is masked.
4. `pair` runs today's hub and token checks, then parses the note against a strict grammar, and refuses unless
   the nonce's hash matches the stored one (constant time), is unexpired and unused. It marks the request used,
   and fsyncs, **before** `pairing.accept`. Otherwise it refuses with "This connection wasn't started on this
   Mac. Nothing was connected", with no store write and no spawn.
5. The app shows the masked account and asks "Is this your account?", then asks for the typed number (§11.2).
   The mask is a weak check (§11.7).
6. **`pair` refuses while a pair is held.** The user disconnects first.

From the release that adds `pair begin`, the core refuses every code without its own nonce, in every shell.
`lucille-host pair` with no code runs `begin` with `c=cli`, prints the connect URL, and reads the code the
page shows on standard input. The conformance suite's and the end-to-end tests' fake Lucille must do what
Lucille does: read the nonce from `pair begin` and put it in the note.

### 11.5 The URL scheme

- **Scheme `com.lucille-ai.loft:`** (reverse-DNS, private-use), path `/pair?code=<code>`. It is registered only
  in builds whose core has the nonce check.
- **The handler acts only while a Connect is pending** (`pair status` reports one). Any other link is ignored,
  and the app's window shows "A website tried to connect this Mac to Lucille. Nothing was connected." The app
  sends no notification, which would need a permission prompt.
- **A link can only deliver a code to `pair`.** No URL can start a Connect, add a folder, turn on an operation or
  change a setting. The handler's type has no reference to the folder or operation runners (§20, M3).
- The code goes to the core on standard input, capped at 2 KiB.

### 11.6 Every new pair starts from the fresh policy

This changes 0001. The policy file is per profile, not per pair, so a second pairing would inherit the first's
folders and operations. When `pair` accepts a new code, the core moves the current policy to
`policy.previous.toml` (in its own folder) and starts from the fresh one: only `host.describe`, no folders.
The app then reads that file through `policy previous --json` and offers its folders and operations as
**unchecked** boxes; a box the user checks becomes an ordinary `roots add` or `ops enable`. This covers every
way a pair ends: Disconnect, removal on Lucille's side, rejection, the deadline, a deleted Keychain item.
`THREAT_MODEL.md`'s "only `host.describe` enabled after pairing" becomes true for every pairing.

### 11.7 What is left

- **Live social engineering** that gets a user to read out a code or a number.
- **A login forgery** that signs the user's browser into an attacker's Lucille account before they click
  Connect. The nonce cannot catch it, because the code is genuinely Lucille's. The masked account (§11.4 step 5)
  catches only a careless forger: the mask (first character, `•••`, domain) carries almost nothing, so an
  attacker who signs up with an address of the same first letter at the same common domain shows the same
  hint, for free. Showing the full address on the user's own Mac would close that, at the cost of the address
  sitting in the code's note (readable by whoever intercepts the code), the browser's history and the
  computer's pair record; a Lucille signing key in the release (§11.8) would close it too. Both are not
  decided yet. Until then, this residual is open, and Lucille's own sign-in protection is Lucille's to keep.
- **Another app on the Mac claiming the scheme.** Which app macOS picks is undefined. That is same-user code,
  out of scope. If it redeems the code, the real app has no number to type, and Lucille's gate catches it.
- **Browser history** may keep the scheme URL with the code (not yet tested on a Mac). A code is good for 15
  minutes and is worthless once collected.
- **A compromised Lucille** receives the nonce and can make any code. It is the intended peer anyway, and the
  local policy bounds it, as before.

### 11.8 What we did not choose, and why

- **The computer starts the pairing and the user enters a code on Lucille.** That direction is the classic
  device-code phishing shape, and it reverses who starts a pairing.
- **Signing in to Lucille from the app.** It would put a Lucille credential on the computer; the pair is meant
  to be the only one.
- **A Lucille signing key pinned in each release, signing codes.** New cryptography; it would close the login
  forgery above. Not decided yet.
- **Universal links.** They stop another app claiming the link, not a drive-by link, so the nonce stays the
  mechanism. They could come later.
- **A QR code.** It does not suit a single computer whose screen shows both ends.

### 11.9 The accept order in the spec

The order in which the host runs its accept checks on a room offer is settled, as
[spec/PROTOCOL.md](../spec/PROTOCOL.md) §6 and 0001 ("The read loop") state it: the host accepts the offer, then
checks that the room's hub is the one its pairing code named and that the offer carries no hub token, and
leaves the room at once if either fails. The hub check compares the two URLs as spec §6 states; this record
does not change it. Nothing in this record moves a check or changes their order: the nonce is checked when a
code is redeemed, before any pair exists, and §6's checks run on room offers after it.

A room the host leaves this way is not replaced (spec §9), so the computer runs nothing until it is paired
again. The app shows that state, and one **Connect Again** button pairs again; as after any new pair, the
previous folders and operations come back as unchecked boxes (§11.6).

## 12. Folders and macOS privacy

- **Choose a Folder…** first says what macOS may ask ("macOS may ask whether Loft can access files in
  your Documents folder. Click Allow."), then opens the standard Open panel: folders only, one at a time,
  starting at Documents.
- The app asks the core for an alias (`roots add --suggest-alias`), shows "Lucille will know this folder as
  **documents**. It never learns where the folder is on your Mac", lets the user change it, then runs
  `roots add -- <alias> <path>` and `roots check <alias>`.
- **Broad folders.** If the core refuses because the folder is the home folder or holds it, a second sheet says
  "This gives Lucille your whole home folder, including Documents, Desktop, Downloads and your Library. Most
  people choose one folder instead." Its default button is **Choose a Smaller Folder**; only
  **Allow My Whole Home Folder** passes `--allow-home-or-root`. The app refuses `/` and the Data volume
  (`/System/Volumes/Data`) outright; the command line keeps them behind its flag.
- **Who macOS holds responsible: an inference, not yet tested on a Mac.** No Apple documentation read
  describes how macOS's privacy system picks the process it holds responsible. Qt's write-up ("The curious
  case of the responsible process") reports that "permissions are inherited by child processes": a child
  inherits its parent's responsibility unless it disclaims it. From that, we infer that the app is the
  responsible process for what the host and its children read, so a prompt would carry the app's name and
  usage text and the decision would be recorded for the app. One report points the
  other way for a similar shape: a child with its own Developer ID signature, started from an app bundle,
  drew prompts in its own name, and a Full Disk Access grant to the app did not help (a public bug report
  against another vendor's developer tool, 2026; its cause is unconfirmed, and that child sat outside the
  bundle). Our interpreter has its own
  signing identifier, so the first candidate records which name a prompt carries when the child, not the
  app, touches Documents.
- **What the Open panel grants.** Apple's usage-description keys say: "The user implicitly grants your app
  access to a file in the Documents folder when selecting the file in an Open or Save panel … Your app can
  access that file right away and any time in the future." That is said of a file. That choosing a folder
  grants its contents, and that the grant reaches the child, are inferences no source read establishes. That
  is why `roots check` runs right after the add, so any prompt or refusal happens during setup, not at an
  unpredictable request from Lucille, and why the first candidate runs `roots check` on a Documents subfolder
  again after the app is relaunched (not yet tested on a Mac).
- `denied_by_macos` shows "macOS is blocking Loft from this folder" with **Open Privacy Settings**.
  Lucille still sees `path_outside_root`; the protocol does not change.
- **Cloud folders.** iCloud Drive and other cloud-provider folders may be denied even after the panel. The app
  warns when a folder is under `~/Library/Mobile Documents` or `~/Library/CloudStorage`.
- **Removing a folder** removes it from the policy, which is what stops access. The app never says it revokes
  macOS's grant; it cannot.
- **A moved, renamed or unplugged folder** shows as `missing` in `roots check`, at launch and in Folders, with
  **Choose Again**. A different folder later made at the same path would be taken as the same root; whether
  `roots add` should record the folder's identity so `roots check` reports that is not decided yet.
- **Usage descriptions** for Documents, Desktop, Downloads, removable and network volumes and file providers
  are in the app's `Info.plist`: "Loft runs the operations you turned on, only inside folders you added.
  macOS asks once before it can open this location." The first build has five of these keys, all but file
  providers, each with the sentences on what Loft is for: "Loft lets Lucille use only the folders you choose
  on this Mac, and only for the abilities you turn on. Lucille can also see which abilities are on, and the
  names you gave your folders." The second sentence is there because `host.describe` is on by default (§13).
  Whether macOS shows these words, and under which name, is not yet tested on a Mac (see "Who macOS holds
  responsible", above).
- **No Full Disk Access, ever.** It is system-wide and covers far more than the user's folders.

## 13. Operations

- **One switch per operation, all off,** from `ops list --json`: the switch's position from `enabled`, the
  words from the core's `summary` and `sends`. For `files.sha256`: "Check a file's fingerprint. Lucille gets a
  code that identifies the file's exact contents, not the contents."
- **`host.describe` is on by default**: it is the catalog's only default-enabled operation
  (`ops/__init__.py`), and `ops disable host.describe` turns it off like any other. The app shows its real
  state, with: "Tells Lucille this Mac's host version and which operations and folder names are on."
- **An operation whose `sends_file_content` is true needs an acknowledgement** before its switch turns on. For
  `files.search`: "Matching lines are sent to Lucille, where Lucille's assistant and its AI provider can read
  them." **[I understand, turn on]**.
- **Diagnostic operations** (`host.sleep`) are hidden behind "Show diagnostic operations".
- **Turned off by an update** (0001, "Upgrades") shows once after the update, with **Review and Turn On**.
- **The app never suggests turning something on because Lucille asked.** A `not_enabled` refusal appears in
  Activity as a fact, with no button. A compromised Lucille must not be able to nudge the user through the app.
- **Search on a stock Mac.** `files.search` needs `rg`, which a Mac without Homebrew lacks. The app ships
  ripgrep, built from a pinned source commit on the release runner, signed with the app, at
  `Contents/Helpers/rg`. The catalog lists `/Applications/Loft.app/Contents/Helpers/rg` among its macOS
  paths from the first release that has `files.search`, so shipping the binary later changes no definition hash
  and turns nothing off. This is why the app must live at exactly `/Applications/Loft.app`: it refuses
  to run anywhere else and asks to be moved there. That path holds the app's name, so after that release the
  name cannot change without a catalog change (0001, "Upgrades": an enabled entry that changes turns off).

## 14. Stop controls

| Menu item | Verb | Shows |
|---|---|---|
| Pause Lucille | `pause` | A pause badge, and a note: "Paused. Lucille's requests will be turned away until you resume." Works offline. |
| Resume Lucille | `resume` | |
| Stop What's Running | `kill` | Shown only while an operation runs. Afterwards: "Stopped, and paused. Resume when you're ready." |
| Quit Loft | close the pipe (drain); **Stop now** runs `kill`, which also pauses | §9 |
| Disconnect from Lucille… (Settings) | `forget` | "Disconnected. Lucille was told." or "Lucille wasn't reachable. In Lucille, open Computers and remove this Mac." |
| Reset Settings… (Settings) | `policy reset` | Shown when the settings file is invalid (§10) |
| Remove Loft… (Settings) | section 17 | |

Nothing Lucille sends can unpause the host. The app offers no remote control of any kind.

## 15. Status, activity and diagnostics

- **The menu header** shows the latest state: Connected, Paused, Not connected, or Needs attention (clock skew,
  hub unreachable since a time, policy invalid, Keychain unavailable).
- **Activity** reads `log --limit 200` and the live `request` events, in words: "10:42. Lucille asked for a
  fingerprint of report.pdf in documents. Done." It is kept in memory only.
- **The app writes no log of its own.** The child's standard error goes to an in-memory buffer shown under
  Details. Any system log line from the app carries state names only: no paths, arguments or output.
- **No automatic crash reports and no analytics.** Any upload would be a destination the claim does not list.
- **Copy Diagnostics** is the user's act. The core's `diagnostics` verb writes versions, macOS version,
  `status` with aliases only, and the last 20 audit lines with each path rewritten to
  `<alias>/<relative path>`. Never output bodies, keys, codes, room ids or absolute paths. The redaction runs in
  the core, so every shell redacts the same way. The screen says the redaction is best effort.
- **About** shows the app and host versions, the signing Team ID, the sha256 of `MANIFEST.json`, the claim
  verbatim from `status` (whose controls sentence names both shells from the release that adds the app,
  §21, so it never tells a user of the app to open Terminal), and a link to `VERIFYING.md`. It says:
  "Notarized by Apple: Apple scanned it for known malware. That is not a review of what it does." It never
  says "verified".

## 16. Updates

**What we chose: no auto-update, and no update check.** The app makes no network connection, so it never looks
for updates.
- **The host refuses with `host_too_old`.** `run` emits it as an event, and the app shows "Lucille needs
  Loft 0.3.0 or newer" with **Download**, which opens the releases page in the browser. Lucille's page can show
  the same from the job's refusal (Lucille policy as of 2026-10-29; not proven by this repository).
- **Updating, not yet tested on a Mac.** The Finder stops you deleting an app that is open (Rogue Amoeba,
  "The Finder Really Should Prevent Moving Running Applications", 2019), and users replacing a running app
  report "The operation can't be completed because the item … is in use" (Mozilla's support forum, for
  Firefox). The app runs from login, so a
  drag over it would usually be refused. And the new copy cannot quit the old one, because it refuses to run
  anywhere but `/Applications/Loft.app` (§13), which the old copy still occupies. So the app does it:
  **Download** opens the release page in the browser, and the app then shows "When the download has
  finished, click **Prepare to Update**. Loft will finish its current task and quit. Then open the
  download, drag Loft into Applications, and choose **Replace**." **Prepare to Update** drains, like
  Quit, and quits. The hardware tests record what the Finder says when the new app is dragged over a
  running old one (§19).
- **What an update keeps.** The same Team ID and identifiers. For macOS's privacy grants: Apple TN3127, "A
  privilege, like microphone access, acquired by an existing version of your app is still available to a
  new version", because the new version satisfies the original designated requirement, which checks the
  identifier and the Team ID. For the Keychain: an item is tagged with the designated requirement of the app
  that created it, and a later version must satisfy it (Apple's Quinn, developer forums thread 98484, who
  adds that his understanding may be out of date). keyring's macOS backend uses the `SecItem` calls without
  choosing a keychain (keyring 25.7.0, read), and `SecItem` "defaults to targeting the file-based keychain";
  Apple adds that "The file-based keychain is on the road to deprecation" (TN3137): a future risk to this
  continuity. The same
  profile folder, so the pairing, folders and enabled operations carry over, except entries an update
  turned off.
- **Downgrades are refused.** `run` records the version it last ran as in its state folder. A `run` older than
  that refuses to start (exit 25), and the app says "This Loft is older than the one you used before.
  Download the newest." `run --allow-downgrade` overrides it, from Terminal only. An older signed release can be
  a vulnerable one. So a bad release cannot be undone by going back in the app: the only fix is the next
  release. The record is per profile, so the app's and a `pipx` install's do not interfere (§9).

**What we did not choose, and why.** An updater (Sparkle). "No auto-update" is a stated bound on a malicious
release (THREAT_MODEL.md; 0001 says "There is no auto-update"). An update channel is the strongest attack
path on every installed copy. **Not decided yet.** If one is ever added, it lives in the Swift app, never in the Python process; it checks and
asks, never installs on its own; its feed is signed, its signature failures never expire, it ships no delta
updates; its feed is on this repository's GitHub Pages; its signing key is held offline by the repository
owner; `PRIVACY.md` says what the check sends before it ships; and the install waits for the drain.

## 17. Removing

- **Disconnect from Lucille…** runs `forget` and shows whether Lucille was told. The app stays, and the
  login item is removed.
- **Remove Loft…**
  1. Explains: "This disconnects this Mac from Lucille, deletes Loft's saved connection and settings
     from this Mac, and moves the app to the Trash. Lucille keeps results it already received for up to 24
     hours, and its database backups are not blanked (Lucille policy as of 2026-10-29; not proven by this
     repository; see PRIVACY.md)." Offers **Save a Copy of the Activity Log…**.
  2. Pauses, then drains, with **Stop Now**.
  3. `forget`: leaves the pair's rooms, deletes the pair, tells Lucille best effort.
  4. `uninstall-data --yes` (new core verb), for the app's own profile only: deletes every Keychain item the
     host's stores own for that profile, then that profile's folders. It refuses while `run` holds its lock,
     and while a pair is held ("Disconnect first: run `forget`"), so a pair is never dropped without Lucille
     being told when it can be. A `pipx` install's `default` profile is left alone.
  5. Unregisters the login item, moves the app to the Trash, and says whether Lucille was told.

  Steps 1 to 4 are platform-neutral Python, so a Windows or Linux shell wraps the same verbs.
- **Dragging the app to the Trash** leaves the Keychain item (a dormant credential, same-user scope), the
  host's folder in `~/Library/Application Support/lucille-host`, the login entry, and Lucille's half of the
  pair. Deleting an app does not unregister its login registration (Apple's Quinn, developer forums thread
  738958, testing a launch agent on macOS 13), so the entry may stay listed in System Settings › General ›
  Login Items & Extensions; without the app it has nothing to start (not yet tested on a Mac). **Remove
  Loft…** unregisters it. Lucille's page says: "If you removed Loft from your Mac, Remove this
  computer here" (Lucille policy as of 2026-10-29; not proven by this repository). Reinstalling and choosing
  **Remove Loft…** clears the rest.
- The app never says that Lucille deleted its copy.

## 18. Release pipeline

All on GitHub-hosted runners. Every `uses:` is pinned by commit SHA. No third-party signing or notarization
action: the signing steps use Apple's own tools and `gh`, which the images ship.

| Workflow | Trigger | What it produces |
|---|---|---|
| `ci.yml` | every pull request and push to `main` | Linux tests, the Swift library's tests on Linux, a macOS build signed ad hoc (it may carry the hardened runtime, which the first build does; §7) with every check of §19, UI snapshots. No signing secret. No app is uploaded except on a manual run, and then ad-hoc signed only. |
| `macos-candidate.yml` | manual, on `main` or a `v*` tag, in environment `macos-release` | A signed, notarized candidate, uploaded only to a **draft** release that is never published, then checked by a separate job that holds no secret. Draft releases are visible only to people with write access. |
| `release.yml` | a protected `v*` tag | The wheel on PyPI (environment `pypi`, as 0001), then the Mac jobs below, then one immutable GitHub Release. |

Today the first build has its own workflow, `macos-app.yml`, beside `ci.yml`. It runs on pushes to its
branch, on pull requests that touch `macos/`, `src/`, `pyproject.toml`, `uv.lock` or the workflow, and by
hand. It uploads the ad-hoc disk image as a 7-day artifact whenever every check passed, except for a pull
request from a fork (`macos/README.md`, "The disk image from CI"). The plan above uploads one only on a
manual run.

**The Mac jobs in `release.yml`.**
1. `macos-payload` (macOS 15, no secrets): downloads the exact wheel PyPI published and checks its sha256;
   assembles the runtime; builds the Swift app and `rg`; runs §19's checks; tars the **unsigned** app; attests
   the tarball; outputs its sha256.
2. `macos-payload-repro` (macOS 15, no secrets): the same build, independently, compared by sha256.
   "Reproducible" is claimed for the unsigned payload only while this is green, and never for the signed disk
   image.
3. `macos-sign` (macOS 15, environment `macos-release`, which needs the repository owner's approval):
   - **runs no package manager, no compiler and no test harness, and no code but Apple's tools, `gh`, and one
     reviewed standard-library Python script;**
   - checks the tarball's sha256; creates a temporary keychain with a password generated in the job; writes the
     API key to `$RUNNER_TEMP` (0600); deletes both in an `always()` step;
   - signs inside out; checks that every Mach-O is signed with the runtime flag and the Team ID and that no
     executable has entitlements;
   - builds and signs the disk image from the signed app, notarizes and staples the disk image, and keeps
     the notarization log (the app is notarized and stapled first only if the offline first-launch test of
     §7 requires it);
   - attests the final, stapled disk image (stapling changes the bytes);
   - passes it on as a workflow artifact kept one day, with its sha256 as a job output. These are the bytes the
     release publishes minutes later.
4. `macos-verify` (macOS 15, macOS 26, and macOS 27 on the `xcode-27` image; no secrets): checks the sha256;
   adds a quarantine attribute as a browser would; `spctl` and `stapler validate`; installs into
   `/Applications`; `--version` equals the tag; the
   wheel inside equals PyPI's; runs the end-to-end test and the conformance suite against the bundled host;
   and, from the second release on, pairs with the previous release's app and reads the Keychain item with the
   new one, with a timeout, so an unexpected Keychain prompt fails the job.
5. `github-release` (Linux, `contents: write`, no package manager): checks each asset's sha256 against the jobs'
   outputs, uploads the wheel, the sdist, the disk image, `MANIFEST.json` and `SHA256SUMS`, and publishes the
   release, immutable.

**When a Mac job fails after PyPI published.** PyPI publishes first, so a version can exist on PyPI with no
disk image. The rule:
- A GitHub release is marked latest only if it carries the disk image. A version whose Mac jobs failed is
  published without it and with `make_latest` false ("Specifies whether this release should be set as the
  latest release", GitHub's REST reference), so `releases/latest/download/Loft-macOS-arm64.dmg`
  keeps naming the last release that has one.
- The Mac fix ships as the next patch release, on PyPI and as a disk image together. A failed version is never
  rebuilt under the same number.
- Lucille's minimum host version never names a version with no disk image (Lucille policy as of 2026-10-29;
  not proven by this repository), so a Mac user is never told to update to a build that does not exist.
- Lucille's download button also links the releases page, for the case where the direct link fails.

**Three kinds of build, and who gets each.**
- **Ad-hoc builds** come from `ci.yml`, signed with no certificate. Until the project's Apple Developer
  membership exists they are the only app builds, and the repository owner tries them on their own Mac only
  (§3.2). They are never offered to anyone else. macOS refuses to open one without an explicit override, and
  each is a new code identity to macOS.
- **Candidates** are for the repository owner: the first signed builds, the paste form (§3.2), and acceptance
  runs before a release. They never leave the draft. Their automated checks (the bundled interpreter's
  imports under the hardened runtime, and the Keychain) run in a separate job that holds no secret and reads
  the draft, never in the signing job, which runs no code but Apple's tools, `gh` and one reviewed script.
  Whether a job token with read access to contents can download a draft asset is not yet tested ("Only users
  with push access will receive listings for draft releases", GitHub's REST reference). If it cannot, that
  job gets write access to contents, holds no signing secret, and the residual is named: a compromised locked
  dependency could use that token.
- **The first public disk image** ships only in a release whose core has the Connect binding (§11.4), and only
  once the release that adds a rebuild job and `lucille-host verify` exists (0001: "Before anyone other than the
  repository owner pairs a computer"). The asset name is fixed, `Loft-macOS-arm64.dmg`, so a link to the
  latest release always works.

**The environment `macos-release`** holds the Developer ID certificate and its password, and the App Store
Connect API key, its id and issuer id, as environment secrets, never repository-wide. Deployments are limited to
`main` and `v*` tags. A release then needs two approvals from the repository owner: `pypi` and `macos-release`.

## 19. Testing without a Mac

**On Linux, in seconds** (any contributor, and `ubuntu-latest`):
- every verb's `--json` output against the schemas and fixtures; the lifeline, lock and exit codes; reload on
  change; `--suggest-alias`; `roots check`'s mapping (EACCES by permissions, EPERM by a stub); the nonce, the
  typed gate, refuse-while-held, the fresh policy on a new pair, `uninstall-data`, `diagnostics`;
- the M13 test: an audit hook records process creation across pair, resolve, touch and a request;
- the build script's pure logic: `Info.plist` rendering, version rules, the prune plan, signing order, the
  manifest, and the Linux-side assembly of `site-packages`;
- the Swift library's tests (Foundation only), driving the **real** Python CLI;
- the guards (§20).

**On GitHub-hosted macOS, every pull request** (about 10 minutes).

Built today, by the first build's `macos-app` workflow (`macos/README.md`, "What CI checks"):
- the Swift build, and a bundle assembled with the real pinned runtime, signed ad hoc with the hardened
  runtime (§7: whether ad-hoc builds keep the hardened runtime is not decided yet; library validation
  against a Team ID is exercised only on signed candidates);
- the host's whole test suite against a copy of the bundled, trimmed runtime;
- the checks in `macos/check_app.py`, among them the app's Swift code against the real command line, and the
  menu driven through System Events, with screenshots kept as a CI artifact.

Planned:
- the conformance suite against the bundled host (a `--host-command` option names it);
- end to end through the bundle: a local hub on 127.0.0.1, a fake Lucille, pair, typed number, an offered room,
  `files.sha256` on a temporary folder, reply, `kill`, `pause`, `forget`;
- the Keychain for real, through a temporary default keychain;
- the bundle made read-only (`chmod -R a-w`) for the run, then `codesign --verify --strict` again;
- `lsof` sampling: no listener anywhere, no connection from the app, only the hub from the host;
- a snapshot of `~/Library` before and after, diffed against an allowlist;
- crafted scheme URLs (option-shaped codes, a 1 MiB string, paths shaped like folder or operation commands): the
  policy digest is unchanged and no verb but `pair` ran;
- UI snapshots: each screen state rendered to PNG in light and dark, kept as CI artifacts for review. The
  first build's screenshots show only the states the `ui` check drives to, in whatever appearance the
  runner has.

**On the signed build** (§18), on macOS 15, 26 and 27 (`xcode-27`): signing checks, library validation and
the hardened runtime for the first time, notarization logs, Gatekeeper's view with quarantine, the
end-to-end test through the signed bundle, Keychain continuity between releases.

**On a Mac, by hand.** Runners cannot answer a privacy, Keychain or Login Items prompt, cannot show what a
first-time user sees after a real browser download, and cannot show what the menu bar does when a person
hides an icon. Everything else, macOS 27 included, runs on runners (the lifeline, Quit's drain and a
force-quit's included, through M14's test). So a person runs, on macOS 26 and 27, only what needs a person or
real hardware: the first-launch dialog after a Safari and a Chrome download; a first launch with no network;
adding Documents, an iCloud Drive folder and an external disk, and which name each prompt carries; the
Keychain and Login Items prompts, if any; a lid close; dragging the next build over a **running** old app,
recording the Finder's message, then the update flow of §16; hiding the menu-bar icon; the app beside a
`pipx` install; and deleting the app instead of Remove, recording what Login Items & Extensions lists
afterwards. Stop now, Quit, Disconnect and Remove are run once by hand as well, to see the screens a user
sees. A MacBook Neo is a target no test has run on yet. A virtual machine restored to a snapshot between
privacy tests is the reliable way to repeat them.

**With people.** Before anyone but the repository owner is offered the app: three people who have never used
Terminal, given only a link to Lucille's Computers page, watched and not helped. Recorded: time to connected,
every stall, every dialog they hesitated on. Pass: all three connect, add one folder and turn on one operation
in 10 minutes or less.

## 20. Guards and repository hygiene

- **`tools/guards/macos_lint.py`** (new, tested like the others; `VERIFYING.md` says what it reads). The
  first build has no such guard: `tests/test_macos_build.py` reads its Swift sources for some of the same
  names, and `check_app.py links` reads what the built executable imports (`macos/README.md`). The guard:
  - in `macos/**/*.swift`: no `URLSession`, `NWConnection`, `NWListener`, `import Network`, `WebKit`,
    `NSXPCConnection`, `NSXPCListener`, `CFSocket`, `NSStream`, `DistributedNotificationCenter`,
    `NSAppleScript`, `posix_spawn`, `system(`, `/bin/sh`, `/bin/zsh`;
  - `Process` only in the one Swift file that starts the host, and only with the bundled runtime and an argv;
  - no read of `NSPasteboard` (the user pastes into a field, §11.3; writing, for Copy Diagnostics, is allowed);
  - no `http://` or `https://` literal in Swift; `x-apple.systempreferences:` allowed;
  - no phrase from `PRIVACY.md`'s must-not-claim list in the app's strings or `Info.plist` template, with an
    explicit allowlist for sentences that negate one ("That is not a review…");
  - `CFBundleURLTypes` only together with the core's nonce check;
  - like every guard here, it catches accidents, not a determined author.
- **The workflow lint** gains `macos-26` and `xcode-27`, and drops `macos-14` after that image retires on
  2026-11-02. Since GitHub now keys a new macOS image by its Xcode version, the next macOS will most likely
  arrive under an `xcode-*` label, not as `macos-28` or as `macos-latest` moving (an inference from GitHub's
  statement): the lint's label list is reviewed when GitHub announces an image.
- **`.gitignore`** gains `.build/` and `.swiftpm/`, so SwiftPM's object files never trip the compiled-file rule.
- **The build tooling** (`tools/macos/`) is Python under the repository's AST lint; only its `codesign`, `xcrun`,
  `hdiutil` and `security` calls need a Mac. One pins file holds the runtime URL and sha256, the ripgrep commit,
  the Xcode version and the identifiers; the lint checks `Info.plist` against it and against `__version__`.
- **The private-string check** reads Swift and plist files like any tracked file.

## 21. What changes in existing documents

- **0001 "Pairing"**: the host's gate is the typed number (§11.2); `pair` reads the code on standard input,
  refuses while a pair is held, and, from the Connect release, refuses a code without its own nonce (§11.4); a
  new pair starts from the fresh policy (§11.6).
- **0001 "Local policy and upgrades"**: reload on change, and idle instead of exit (§10).
- **0001 "CLI verbs"**: the verbs and flags of §8.
- **0001 "Packaging, releases and verification"**: the Mac app as a second channel beside PyPI, built from the
  same wheel. "There is no auto-update" stays.
- **0001 and THREAT_MODEL.md, the pairing-code threats**: against a code an attacker made, the SAS agrees and
  does not help. Until the Connect release, the bound is that the paste form is not published. From it, the
  nonce refuses such a code in every shell, and the residual is the login forgery of §11.7.
- **THREAT_MODEL.md, who the host trusts**: adds the embedded runtime (pinned by sha256), ripgrep (pinned
  commit), and Apple's notarization as a second trust root for a first install. The malicious-release threat adds
  the Developer ID key and the `macos-release` environment. The question of how a release carries exact
  dependency pins to an install is answered for the Mac app: they are inside it.
- **THREAT_MODEL.md, a known gap**: a first install trusts the page that linked the download. A page that links
  another developer's notarized app cannot be told apart by someone who checks nothing. The README and the About
  window name the Team ID.
- **PRIVACY.md** gains two sentences: "The Loft app connects to nothing itself; only the host inside it
  connects, and only to the hub its pairing code named. Lucille's connect page opens in your browser when you
  click Connect." and "macOS may contact Apple to check the app's signature and notarization; that is Apple's
  behaviour, not this project's."
- **PRIVACY.md's controls sentences** name both shells, since About shows the claim verbatim, for example
  "a log on your computer (`lucille-host log`; Activity in Loft shows the latest entries)", "Pause
  in Loft, or `lucille-host pause`", "Stop What's Running, or `lucille-host kill`", "Disconnect in
  Loft, or `lucille-host forget`". README's copy of the claim and CLAIMS.md's entries for those
  sentences change in the same pull request.
- **CLAIMS.md and its guard tests**, from the Connect release: "A pairing completes only in answer to a
  Connect this computer asked for", as a guard entry mapped to the nonce tests. It is not a sentence of the
  claim, and Lucille still starts every pairing: the user adds the computer on Lucille's page (0001,
  "Pairing", step 1).
- **README.md**: "Install on a Mac", naming the download, the Team ID and the signer's name, and the rule
  "Until this README names a release, a disk image called Loft did not come from this repository."
  It says to use either the app or a `pipx` install on one Mac, not both (§9).
- **VERIFYING.md**, a new section "The Mac app":
  1. `shasum -a 256` against `SHA256SUMS`, and `gh release verify-asset`.
  2. `gh attestation verify Loft-macOS-arm64.dmg --repo gald33/lucille-host --signer-workflow
     gald33/lucille-host/.github/workflows/release.yml --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners`.
  3. Apple's view: `spctl -a -t open --context context:primary-signature -vv` and `xcrun stapler validate` on
     the image; `codesign -dv --verbose=4` (Team ID) on the app.
  4. The Python inside: `tools/macos/verify_app.py "/Applications/Loft.app"` downloads the listed wheels
     from PyPI, checks their hashes against the release's lock, and compares every file under `site-packages`
     with the wheels' `RECORD`, listing any file that came from none. The installer-written files of each
     `*.dist-info` (`INSTALLER`, `REQUESTED`, `direct_url.json`, and the rewritten `RECORD`) are named, and
     each `RECORD` is compared line by line with the wheel's own; any `.pyc` under `site-packages` is listed
     as a file from no wheel. The release wheel carries its PEP 740 attestation, as today.
  5. What this cannot establish: the Swift app, the runtime and ripgrep beyond review and their pins; what
     Apple's notarization looked for; whether the copy in Applications is still the one verified after a same-user
     process touched it (`codesign --verify --deep --strict` detects a changed sealed file).

## 22. Mac invariants and their tests

These join 0001's "Host invariants and their tests".

| # | Invariant | Test |
|---|---|---|
| M1 | No listener: neither the app nor the host opens a listening socket, a Mach service or an XPC listener | `lsof` on the end-to-end run; `macos_lint` |
| M2 | The app makes no network connection; the host connects only to its hub | `lsof` sampling against a loopback hub; `macos_lint` |
| M3 | Nothing from outside the keyboard changes policy: a link can only deliver a code to `pair`, only while a Connect is pending | a Swift unit test on the handler's type; crafted URLs leave the policy digest unchanged |
| M4 | From the Connect release, the host redeems only a code answering its own pending request | core tests: no nonce, wrong, reused, expired; each refused before `pairing.accept`, with no store write and no spawn |
| M5 | The host's gate opens only on Lucille's number, typed; it never shows its own before | core tests: wrong entry, three wrong entries forget, right entry opens; `pair status` carries no digits before the gate opens |
| M6 | Nothing in the environment changes what the bundled runtime loads | `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP` and a user site set: a planted module never loads; no `.pth` file ships beyond the wheels' `RECORD`. On the ad-hoc pull-request bundle (which may carry the hardened runtime, §7) and again on every signed build in `macos-verify` |
| M7 | Developer ID builds: hardened runtime, no entitlements, every Mach-O signed by the team | signing-job checks, on signed builds only: an ad-hoc build may carry the hardened runtime (§7) but has no Team ID, so library validation against a Team ID is checked only on signed builds |
| M8 | The bundle is never written at run time | the read-only end-to-end run; `codesign --verify` after. On the ad-hoc pull-request bundle and on every signed build |
| M9 | Nothing runs as root, at install or at run time | no `.pkg`, no daemon (`macos_lint`); uid 0 refused (0001) |
| M10 | The wheel inside is PyPI's; every file under `site-packages` is a file of a wheel's `RECORD`, except the named installer-written files, and no `.pyc` is there | build and post-release checks; `verify_app.py` |
| M11 | A build from a tag has one version number; any other build records its real version in `MANIFEST.json` | build guard; `macos_lint` |
| M12 | The app writes nothing outside the host's own folders except what macOS writes for any app | the `~/Library` diff |
| M13 | Pairing and the read loop start no process | the audit-hook test, on Linux and macOS |
| M14 | No host outlives its app unseen | close the pipe: `run` drains and exits 0 |
| M15 | One host per profile | a second `run` exits 21 |
| M16 | A policy change applies to the next decision | remove a folder while `run` runs; the next request for it is refused `path_outside_root` |
| M17 | Every new pair starts from the fresh policy | pair, enable, forget, pair again: only `host.describe`, no folders |
| M18 | The app never offers to widen access in answer to Lucille | review, and a UI snapshot of a `not_enabled` entry with no button |

## 23. Threats the Mac app adds or bounds

| Threat | What bounds it | What is left |
|---|---|---|
| A website fires the app's link | the handler acts only while a Connect is pending; the core refuses a code without its own nonce | none known beyond §11.7 |
| A scam that hands the user a code made with the public library | the paste form is never published; from the Connect release, the nonce refuses it in every shell | a user talked into reading out codes |
| A code intercepted on its way to the app | the computer's gate is typed (§11.2) | a careless **They match** on Lucille exposes Lucille's side, not files |
| A login forgery into an attacker's Lucille account | the masked account before the typed number, which catches only a careless forger (§11.7) | any attacker who signs up with an address of the same first letter at the same common domain; a user who does not read the hint |
| Compromised build or signing path (runner image, action, dependency, runtime tarball) | hosted runners; SHA-pinned actions; sha256-pinned runtime, wheels and ripgrep; a signing job that runs only Apple's tools and one reviewed script; the owner's approval | a malicious runner image or Xcode; provenance is SLSA build level 2 |
| A leaked Developer ID key or API key | environment secrets, used only in the signing job, deleted at its end; certificate revocation; notarization's audit trail | software signed under this project's name until revocation |
| A Lucille page that links a different app | the link targets this repository's releases; README and About name the Team ID | a user who checks nothing |
| Apple | trusted for Gatekeeper only; copy never says "verified" | Apple can revoke the certificate or a ticket |
| macOS prompts at a moment the user did not cause | `roots check` during setup; the inference, not yet tested on a Mac, that the app is the responsible process (§12) | a prompt in the interpreter's own name, or a folder whose grant does not reach the child (§12) |
| Same-user malware | out of scope, as before; hardened runtime and library validation are defence in depth, never claimed | it can run the bundled interpreter and read the host's Keychain items, run verbs, and rewrite the log |

## 24. Windows and Linux

- **The seam is the CLI contract**, not the Swift code. Any shell starts `run --json-events
  --exit-on-stdin-eof` as its child, reads events and calls verbs. The lifeline works over pipes on every OS.
- **The runtime build is generic.** The pins file is keyed by target triple, and `uv pip install
  --python-platform <triple>` already accepts Windows and Linux triples.
- **Logic every shell needs is in the core:** alias rules, operation copy, the claim, URLs, the nonce, the typed
  gate, the fresh policy, reload on change, downgrade refusal, `uninstall-data` and `diagnostics`. A second shell
  is views plus OS calls.
- **The core's Windows port is its own work.** `os.lockf`, SIGHUP, process groups and the `O_NOFOLLOW` walk are
  POSIX, and `os_family: windows` is a protocol change Lucille must accept first. A Windows shell would be a
  signed per-user installer with a per-user startup entry and keyring's Windows backend. Not designed here.
- **Linux** users are mostly served by `pipx`, plus a `systemd --user` unit the core could write later. From
  the Connect release, a command-line user pairs through the same connect page: `lucille-host pair` asks with
  `c=cli`, and the page shows the code with plain instructions instead of firing a link (§11.4).
- **Not reused:** the Swift code. It is small on purpose.

## 25. Delivery order

1. **The contract and the pipeline.** `--json` for the built verbs, `roots check`, alias suggestion, operation
   copy; the Swift app without pairing (Welcome, Folders, Operations, Pause, About); the build tooling, CI and
   the candidate workflow. Exit: CI green on Linux and macOS, and a notarized candidate in a draft release that
   settles the layout, library validation under the hardened runtime, the entitlement, the offline first
   launch and the Keychain questions. That candidate needs the project's Apple Developer membership; it does
   not gate step 2's ad-hoc builds on the repository owner's Mac.
2. **The repository owner's Mac.** With the release that adds pairing: the paste form, the typed gate,
   supervision, open at login, Activity, Quit, Disconnect, the fresh policy. Ad-hoc builds on the owner's Mac
   until the project's Apple Developer membership exists (§3.2), then candidates only.
3. **The first public disk image.** Connect from the computer, the URL scheme, Remove, Copy Diagnostics,
   bundled ripgrep, the Mac jobs in `release.yml`, `verify_app.py`, the reproducibility job. Gated on the test
   with three people (§19) and on the rebuild job and `lucille-host verify` (0001).
4. **Later.** An updater, if ever decided; Windows; a Linux tray shell.

## Not decided yet

- A trademark search on the name Loft, before the first signed build, after which its identifiers cannot
  change (§7), and before the catalog lists the bundled `rg` path, which contains it (§13).
- Whether notarization accepts Mach-O files under `Contents/Resources`, or they move to `Contents/Frameworks`
  and the interpreter to `Contents/Helpers` (§6).
- Whether shipping no `.pyc` makes start-up too slow, and so whether `checked-hash` files with a recompiling
  `verify_app.py` replace it (§6).
- Whether the bundle loads under library validation and the hardened runtime, which only a Developer
  ID-signed candidate exercises (§7).
- Whether ad-hoc builds keep the hardened runtime, which the first build carries and loads under on a
  macOS 15 runner (§7).
- Whether the interpreter needs `allow-unsigned-executable-memory` under the hardened runtime (§7).
- Whether a first launch works with no network when only the disk image is notarized and stapled (§7).
- Whether macOS holds the app responsible for its child's file access, which name a prompt carries, and
  whether an Open-panel grant of a folder reaches its contents and the child (§12).
- Whether `roots add` records a folder's identity, so a different folder later made at the same path is
  reported (§12).
- What the Finder does when the new app is dragged over a running old one (§16).
- The Keychain's file-based implementation, which keyring's macOS backend uses and Apple calls "on the road
  to deprecation" (§16).
- Localization and right-to-left layout; the first version is English only (§1).
- Whether the Keychain's delete-then-add on each save, about 288 times a day from the 5-minute touch, ever
  prompts under the signed interpreter, and what it costs.
- macOS 27's treatment of a menu-bar app whose icon the user hides (§9).
- Whether each browser passes the scheme URL's query intact, and whether it keeps the URL in history (§11.7).
- Lucille's web origin for the connect page, which becomes a core constant.
- Intel Macs (§5).
- An updater (§16).
- A launcher embedding Python in place of the renamed interpreter (§6).
- A Lucille signing key in the release, or the full account address shown on the Mac, against the login
  forgery the mask does not stop (§11.7, §11.8).
- The exact wording of each macOS dialog, which the app's announcements quote.
