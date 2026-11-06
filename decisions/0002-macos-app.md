# 0002: Capsule, the Mac app (draft)

Author: lucille-host maintainers. First written 2026-10-29. Status: **draft.** A first, smaller build exists:
its Swift sources in [`macos/`](../macos/README.md), its build and check scripts in `tools/macos/`, and its CI
jobs in `.github/workflows/ci.yml`; "What is built today", below, says what it is. Everything else in this
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
- "Seen in CI" means run on 2026-10-29 on a GitHub-hosted macOS runner, by the first build's workflow,
  `macos-app.yml`, which the `macos-app` and `macos-smoke` jobs of `ci.yml` have since replaced (§18). A
  result from those jobs names its run.
- **The app's working name is Capsule** ("Capsule, by Lucille" where the brand helps), with the bundle
  identifier `com.lucille-ai.capsule`. The name may still change before the first signed build, after which
  the identifiers cannot (§7); it is checked first ("Not decided yet"). Where no name is needed, this record
  says "the app". Only what a user sees and the app's identifiers carry the name: the package, the command
  (`lucille-host`), its folders and profiles, and this repository keep theirs.
- **"Proposed, pending the maintainer's decision"** marks a part of this design that is not decided. Nothing
  builds on it until it is decided. Two parts carry it: the typed gate (§11.2) and the Connect flow (§11.4).
  Until they are decided, 0001's pairing gate stands: the host shows the SAS, and the user types `y` if it
  matches Lucille's page. The app built today pairs on that gate: it shows the SAS, and the user clicks
  **They Match** ("What is built today").

## What is built today

The first build's Swift sources are in [`macos/`](../macos/README.md), whose README is the reference for
it, and its build and check scripts are in `tools/macos/`. It is not a release, and it is for people
developing the app. Its builds stay ad hoc signed, for the repository owner's own Mac (§3.2).
- **`Capsule.app`**, a menu bar app for Apple silicon Macs with macOS 15 or later, with no main window and no
  Dock icon; and **`Capsule-<version>-<commit>-adhoc-macOS-arm64.dmg`**, a disk image that holds it and a link
  to Applications.
- **Inside it**, a python-build-standalone CPython 3.13 at `Contents/Resources/runtime`, pinned in
  `tools/macos/pins.toml` by URL and sha256, with its own pip uninstalled, pruned by the reviewed list
  `tools/macos/prune.txt`, and its interpreter renamed `lucille-host-runtime` (§6). Into it, `uv` installs the
  exact wheels `uv.lock` pins for macOS, downloaded from the lock's URLs and checked against its sha256, and
  then the host's wheel, built from the checkout, all by hash. Nothing is precompiled: the app holds no
  `.pyc`. Beside the runtime are the wrapper `Resources/bin/lucille-host` for Terminal users, the licence
  texts in `Resources/licenses/`, and `Resources/MANIFEST.json` (§6).
- **The app drives the host only through its command line**, as
  `lucille-host-runtime -I -B -X utf8 -m lucille_host --profile mac-app <verb>`, with an argv and a fixed
  environment (`PATH=/var/empty`). It runs every verb on its own profile, `mac-app` (§9); builds before it
  used `default`, and nothing moves from one to the other. It reads JSON (§8) from `status`, the pairing
  verbs, `forget`, `kill`, `pause`, `resume` and `run`'s events, and the text of `--version`, `ops list` and
  `roots list`. It reads and writes none of the host's files, and opens no network connection.
- **Pairing by paste, on 0001's gate** (§11.3). The maintainer decided on 2026-11-02 to build it now on
  [0001](0001-host-design.md)'s gate, not the typed one. The user pastes the code; the app passes it to
  `pair --code-stdin --json` on standard input, shows the SAS from `sas_pending`, and the user clicks
  **They Match** (`pair confirm --json`) only if Lucille's page shows the same number, or **They Differ**
  (`pair reject --json`). The typed gate (§11.2) and the Connect flow (§11.4) stay proposed, and are not
  built.
- **Supervision of `run`** (§9): `run --json-events --exit-on-stdin-eof` as the app's child, with the
  lifeline; the exit table; restarts with back-off, and the stop after five crashes in ten minutes; Quit and
  logout with the drain ladder and **Stop now**; paused at the next launch after a `kill`, and never resumed
  by the app on its own; **Disconnect from Lucille…**, which drains `run` before `forget --json`.
- **The menu** shows the version and a status line read from `status --json` and `run`'s events, with a line
  for what needs attention, and offers Pause and Resume, the abilities (the operations, switched with
  `ops enable` and `ops disable`), Add a Folder…, the folders with Remove, the pairing items (Connect to
  Lucille…, Finish Connecting…, Stop Connecting…, Disconnect from Lucille…), and Quit Capsule.
- **It is signed ad hoc**, with the hardened runtime and no entitlements, each Mach-O file with its own
  identifier and the interpreter with `com.lucille-ai.capsule.runtime` (§7). It is not notarized, so macOS
  refuses to open it normally.
- **`ci.yml`** builds it on every pull request and every push to `main`. Its `macos-app` job builds on
  GitHub's `macos-15` runner, runs each check in `tools/macos/check_app.py` and the host's whole test suite
  on a copy of the bundled runtime, then, with that copy's interpreter, an end-to-end check of pairing and
  supervision with a local hub and a stand-in for Lucille; its `macos-smoke` jobs run the same disk image
  on macOS 26 and 27, without the end-to-end check (§18, §19). Logs and UI snapshots are uploaded on every
  run. The disk image is uploaded only by a manual run whose every check passed, and kept 3 days.

Not built yet: the typed gate and the Connect flow (§11.2, §11.4; proposed), opening at login, Activity,
the restore list after a new pairing (§11.6, `policy previous`), Stop What's Running, Reset Settings
(`policy reset`, §10), Download and Prepare to Update (§16), the app's use of the JSON contract for `ops`
and `roots` (§8), the Welcome and main windows, updates, **Remove Capsule…** (section 17), Copy
Diagnostics (§15), Developer ID signing and notarization, ripgrep (`Helpers/rg`; the pins hold an empty
ripgrep commit until then, and `Resources/licenses/` has no ripgrep licence), and the release jobs. The
first build also departs from this record in these ways, among others; `macos/README.md`, under "Choices
made for the first build", says why for most of them:
- the command line runs with `PATH=/var/empty`, but with `/` as its working directory, not the host's own
  state folder (§9);
- the app opens from any folder, where the planned app runs only from `/Applications/Capsule.app` (§13);
- the words for each ability and the suggested folder names are in the Swift app, not in the core (§8, §12,
  §13);
- the `Info.plist`'s six folder usage keys hold the sentences on what Capsule is for, not the sentence §12
  plans (§12; whether macOS shows either is not yet tested on a Mac);
- the app refuses outright, besides `/` and the Data volume, the two folders between them, `/System` and
  `/System/Volumes`, which hold the Data volume (§12). Here the first build allows less than this record;
- the computer's gate is 0001's click, not the typed number of §11.2, which is proposed (above). The buttons
  count only once the number is drawn in a window not wholly hidden, and **They Match** runs `pair confirm`
  only after a `status` read shows the same pair and deadline still pending. `pair confirm` names no pair,
  so a command run on profile `mac-app` from Terminal between that read and the confirm is not excluded;
  the app's own store verbs run one at a time. After **They Differ**, They Match stays off for that pair
  only while the app stays open: it keeps no memory of its own. A click gate does not protect the
  computer's side against an attacker who can rewrite Lucille's page, which §11.2 is for: one more reason
  the paste form stays on the owner's Mac (§11.1);
- a profile that holds more than one pair (`several_pairs`) gets no Disconnect: `forget` refuses it too.
  The app says it cannot end them, and `macos/README.md` gives the repair by hand (§9);
- the app reads `pair status --json` only while its pairing window is open (§11.3);
- the Quit question is asked only for the app's own Quit, and only while `run` lives and no Disconnect is
  under way; a quit by Apple event, a logout included, never asks. A quit during a Disconnect finishes it:
  `forget` runs once `run` has drained, before the app replies (§9);
- when no `state` arrives for 120 s, the app only says so; it neither restarts `run` nor closes the
  lifeline (§9);
- the clock's fix is said in words, not offered as an **Open Date & Time Settings** button (§3.3), because
  the Mac lint and the build's checks keep `NSWorkspace` out of the app (§20).

Where this record says what the app does, it describes the planned app.

## 1. What the Mac app is

- **One disk image, one app.** A signed, notarized and stapled `.dmg` holds `Capsule.app`. The user
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

1. On Lucille's Computers page: **Download Capsule for Mac**. The link points at this repository's
   GitHub Releases, never at a copy on Lucille's servers (Lucille policy as of 2026-10-29; not proven by this
   repository). The page states "A Mac with Apple silicon (any M-series chip, or MacBook Neo), macOS 15 or
   later" and how to check.
2. The user opens the download. A Finder window shows the app, an arrow and the Applications folder. They
   drag one onto the other. (A standard account may need an administrator's name and password here; that is
   the Finder protecting Applications, not this app.)
3. They open Capsule. macOS asks once whether to open an app downloaded from the internet; Lucille's page
   shows a picture of that dialog beforehand. They click **Open**.
4. **Welcome**: what the app does, in the claim's own terms, and "Nothing runs until you connect and
   confirm." One button: **Connect this Mac to Lucille**. The app says: "Your browser will open Lucille. If it
   asks to open Capsule, click Open."
5. Lucille's connect page, signed in: "Connect a computer to your Lucille account (a•••@example.com)? Only
   continue if you just clicked Connect in Capsule on this Mac." The page never submits on its own. The
   user clicks **Connect**; the browser asks "Open Capsule?"; they click **Open** (Lucille policy as of
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
10. **Finish**: "Open Capsule when you log in" (on). "macOS will show a notice that Capsule can now
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
| Opened the app from the disk image, Downloads, or anywhere but Applications | "Capsule needs to be in your Applications folder" | Drag it there; **Show Applications Folder** opens the Finder (§6) |
| Cancelled the browser's "Open Capsule?" | Lucille's page: "Didn't open? Try again" (Lucille policy as of 2026-10-29; not proven by this repository) | Re-sends the link; the Connect request lives 20 minutes |
| Closed the browser tab mid-connect | App: "Waiting for Lucille…" with **Start Over** | A new Connect |
| Typed the wrong number | "That doesn't match. Type the number Lucille's page shows." After 3 tries: "Nothing was connected. In Lucille, click They differ, then start again." | **Start Over** |
| Clicked **Don't Allow** on a folder prompt | "macOS is blocking Capsule from this folder" | **Open Privacy Settings** (§12) |
| Chose a folder that is too broad | A second sheet, safe choice as default (§12) | **Choose a Smaller Folder** |
| Moved, renamed or unplugged a chosen folder | "The folder documents can't be found" (from `roots check`, at launch and in Folders) | **Choose Again**, which removes the alias and adds the folder from the Open panel. A different folder later created at the old path is not detected (not decided yet, §12) |
| Turned off the login item in System Settings | On next open: "Capsule won't start when you log in" | **Turn Back On** |
| Quit the app and forgot | Lucille's page shows the computer as not answering (Lucille policy as of 2026-10-29; not proven by this repository) | Open the app |
| Paused and forgot, or a task was stopped at Quit or log out (§9) | A pause badge on the icon; at launch, "Capsule is paused" | **Resume** |
| The Mac's clock is off by more than 300 s | "Your Mac's clock is off by 7 minutes, so Lucille can't reach it" | **Open Date & Time Settings** |
| The settings file became invalid | "Capsule's settings can't be read, so it is doing nothing" | **Reset Settings** (`policy reset`, §10): folders and operations start from the fresh policy |
| The Keychain is locked or unusable (`run` exits 23) | "macOS's Keychain isn't available to Capsule" | **Try Again**; if it persists, restart the Mac, then **Copy Diagnostics** to ask for help. Not always undone in the app |
| An update turned an operation off | "Search inside files was turned off because the update changed it" | **Review and Turn On** |
| Lucille needs a newer host | "Lucille needs Capsule 0.3.0 or newer" (the minimum comes from Lucille's request; Lucille policy as of 2026-10-29; not proven by this repository) | **Download**, which opens the release page (§16) |
| Chose **Keep Both** when dragging an update, making "Capsule 2" | "This copy is named Capsule 2, so it can't run" | Move Capsule 2 to the Trash, then drag the new app again and choose **Replace** |
| A release turned out bad, and the older one refuses to start after it (§16) | "This Capsule is older than the one you used before" | None in the app: the only fix for a bad release is the next release |
| Removed the computer on Lucille's page | "This Mac was disconnected in Lucille" | **Connect Again** |
| Dragged the app to the Trash instead of Remove | Nothing on the Mac; Lucille's page explains (section 17) | Reinstall, then **Remove Capsule…** |

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

**What we chose** (decided by the maintainer on 2026-10-29). Apple silicon only, macOS 15 or later (`LSMinimumSystemVersion`
15.0, `arm64` only). That covers every M-series Mac and the MacBook Neo, whose A18 Pro chip is Apple silicon
but not M-series (Apple launched it on 2026-03-04 with macOS 26; not yet tested on one). CI builds on macOS 15 and runs the built app
on macOS 15, 26 and 27: `ci.yml`'s `macos-app` job on `macos-15`, and its `macos-smoke` jobs on `macos-26`
and `xcode-27` (§18). GitHub's hosted macOS 27 image
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
Capsule.app/Contents/
  Info.plist
  MacOS/Capsule                           the Swift menu-bar app (arm64)
  Helpers/rg                           ripgrep, built from pinned source (§13), from the first public release
  Resources/
    MANIFEST.json                      versions; sha256 of the runtime tarball, of every wheel installed,
                                       of the release wheel; the commit
    bin/lucille-host                   a three-line sh wrapper for Terminal users (a resource)
    runtime/                           python-build-standalone CPython 3.13, pinned, pruned
      bin/lucille-host-runtime         the interpreter, renamed
      lib/python3.13/                  stdlib, lib-dynload, site-packages
    licenses/                          CPython, ripgrep and every dependency's licence text
```

The first build lays the bundle out like this, except `Helpers/rg` and ripgrep's licence, which come with
ripgrep.

- **The runtime** is python-build-standalone's `aarch64-apple-darwin` `install_only_stripped` CPython 3.13.
  Its URL and sha256 are pinned in one file, `tools/macos/pins.toml`, which also holds the app's name and
  identifiers, the deployment target (15.0), the Xcode version the build runs, the ripgrep commit (empty until
  ripgrep is bundled) and whether ad-hoc builds carry the hardened runtime (§7). The build refuses any other
  bytes, and a pins file with any other table or key. Its interpreter links Python statically, so the bundle
  holds no `libpython3.13.dylib` (seen in CI: the first build removes it, and its `links` check, which fails
  on a library the build removed, passes).
- **One other pinned file.** The host wheel's build backend is pinned by hash in
  `tools/macos/build-constraints.txt`, beside the pins file and not in it: `uv build` reads it as a
  requirements file (`--build-constraints … --require-hashes`), and it pins what builds the host's wheel, not
  anything the app holds. A test checks it against `pyproject.toml`'s `[build-system]`. Every uv command
  the build scripts and `check_app.py` run reads no uv configuration file (`--no-config`) and gets no
  `UV_*` variable: either could turn build isolation off, and `uv build` would then build the wheel with
  whatever build backend is installed, unpinned and never hashed, and still succeed (seen on Linux with
  uv 0.11.32, from `UV_NO_BUILD_ISOLATION=1` and from a user `uv.toml`, before the build removed both).
- **The wheels** are installed unchanged, with hashes required and binaries only: the release wheel, and the
  release lock's pins for macOS. Measured: the locked dependencies install for `aarch64-apple-darwin` on Linux,
  22 pins, 18 MB, with exactly two arm64 Mach-O extension files (`cryptography`'s Rust extension and `cffi`'s
  backend). So everything but the Swift binary can be assembled and inspected on Linux.
- **How the build installs them** (`tools/macos/payload.py`). It keeps each pin of `uv export` whose
  environment marker holds on CPython 3.13 on macOS on Apple silicon (16 of the 22 today; the other six are
  for Linux, Windows or Pythons before 3.12), picks one wheel file of `uv.lock` for each, by the order of
  tags that runtime accepts, downloads it from the lock's URL, and refuses it unless its sha256 is the
  lock's. Then
  `uv pip install --target … --python-platform aarch64-apple-darwin --only-binary :all: --require-hashes
  --no-deps --no-compile --no-index --find-links …` installs exactly those files, from a folder that holds
  nothing else, with every `UV_*` variable removed from uv's environment; then, the same way, the host's
  wheel, by the sha256 the caller passes. The same function runs on Linux and on a Mac, so the Linux tests
  run the code that ships, though not on the bytes a Mac build ships. Measured on Linux on 2026-11-02, with
  uv 0.11.32 and the real pinned runtime: 16 lock wheels and the host's wheel, `cryptography` from its
  `cp311-abi3` macOS wheel, and exactly the two Mach-O files above in `site-packages`.
- **Nothing is generated that a wheel does not contain**, except what the installer itself writes. uv also
  leaves three things that no wheel holds, and the build deletes each, with its row in `RECORD` where it has
  one: its lock file `.lock` at the top of `site-packages`, the console scripts it writes into
  `site-packages/bin/` (they start with the build machine's interpreter path), and a `uv_cache.json` in a
  `*.dist-info` folder it installed from a folder of wheels (measured with uv 0.11.32). No `.pyc` is shipped
  (below). So every file under `site-packages` is byte for byte a file of a wheel's `RECORD`, except the
  installer-written files of each `*.dist-info` folder: `INSTALLER`, `REQUESTED` and `direct_url.json` where
  written, and `RECORD` itself, which the installer rewrites. Those are named, and each rewritten `RECORD` is
  compared line by line with the wheel's own. The build checks all of this, and that the installed
  distributions are exactly the expected ones, before it signs anything, and refuses on any difference.
- **No wheel's `*.dist-info` folder is removed**: keyring finds its macOS backend through an entry point. The
  one `*.dist-info` folder the build removes is the runtime's own pip, which is no locked wheel's: it is
  uninstalled whole, by its own `RECORD` (which lists `bin/pip`, `bin/pip3` and `bin/pip3.13` too), before any
  wheel is installed, and `MANIFEST.json` records it under `runtime.removed`. The removal skips a row whose
  file is absent and checks no hash before it deletes a file, because the pinned archive's pip lacks the
  `.pyc` files its `RECORD` lists, and its scripts' first lines were rewritten; what it leaves behind, the
  check below refuses. `prune.txt` names no `*.dist-info` folder, and the build refuses one that does.
- **Pruning** removes what the host never runs: the programs other than the interpreter, headers and manual
  pages, the shared library, Tcl/Tk and its packages, pkg-config files, and, from the standard library, the
  build configuration, `ensurepip`, `idlelib`, `tkinter`, `_tkinter`, `turtle` and `turtledemo`. The list is
  the reviewed file `tools/macos/prune.txt`, one literal path per line; the build refuses a pattern, an entry
  the pinned runtime does not hold, and anything in `site-packages` but its `README.txt`. What the list
  misses is refused, not deleted: afterwards the runtime must be exactly `bin/lucille-host-runtime` and
  `lib/python3.13/`, with an empty `site-packages`, no symbolic link and no `.pyc`. CI runs the host's whole
  test suite on a copy of the pruned runtime, so a wrong cut fails a pull request.
- **No symbolic link** is left inside the bundle, unless the fallback below is needed. A link in the runtime
  to a file inside it is replaced by a copy of that file; any other link stops the build. The pinned
  runtime's nine links are all in paths `prune.txt` removes. The build refuses an app that holds a link, and
  `check_app.py bundle` checks again.
- **The interpreter runs as** `lucille-host-runtime -I -B -X utf8 -m lucille_host`. `-I` ignores every
  `PYTHON*` variable and the user's site folder. `-B` stops `.pyc` writes, which would break the bundle's
  seal. `-X utf8` makes text UTF-8 whatever the locale. The app (`macos/App/HostCLI.swift`), the wrapper and
  every check in `tools/macos/check_app.py` run it with these flags.
- **The wrapper**, `Resources/bin/lucille-host`, is three lines of `sh`. It finds its own real path with
  `/bin/realpath`, so a link to it from a folder on `PATH` still runs this bundle's interpreter and never one
  beside the link, and runs the interpreter with the same flags and on the app's profile,
  `--profile mac-app` (§9); a `--profile` given to it comes later and replaces that one. A Linux test pins
  its text. On macOS, `check_app.py wrapper` runs it through a link, on a writable copy of the app, and
  checks that it pauses the app's profile, not `default`, and writes no bytecode.
- **No `.pyc` anywhere in the app.** A precompiled file is in no wheel's `RECORD`, and an
  `unchecked-hash` one is loaded without being checked against its source, so the bytes the interpreter
  ran would be the bytes nothing verified. Without them every module is compiled in memory at each start.
  `prune.txt` also removes the three `.pyc` files the archive ships in `encodings/__pycache__` (their
  recorded source times do not match the sources', so the interpreter ignored them), so the app holds none;
  `check_app.py bundle` and `readonly` check that none appears.
  The cost is start-up time. `ci.yml`'s `macos-app` job measures it on every run, into the job summary
  (`check_app.py startup`): the median and the fastest of ten runs each of `--version` and `status --json`,
  from the bundle as shipped, then from a writable copy with `checked-hash` files for the standard library
  only, then for everything. **Seen in CI** (`macos-15`, run 41407347108, 2026-11-02), in milliseconds:

  | bytecode | `--version` median | fastest | `status --json` median | fastest |
  |---|---|---|---|---|
  | none (as shipped) | 696 | 645 | 722 | 626 |
  | `checked-hash`, standard library | 386 | 330 | 398 | 334 |
  | `checked-hash`, everything | 161 | 136 | 226 | 176 |

  So on that runner, shipping no `.pyc` cost about half a second on each start of the host against
  `checked-hash` files for everything, and about 0.3 s against them for the standard library only. The app
  pays it on every verb it runs. If that is too slow, the fallback is `checked-hash` files that
  `verify_app.py` recompiles with the bundled interpreter and compares. **Not decided yet**: the maintainer
  judges what is too slow.
- **It is renamed** so that Activity Monitor and any macOS prompt name it, not `python3.13`, and it has its own
  signing identifier (`com.lucille-ai.capsule.runtime`, from the pins; §7).
- **`MANIFEST.json`**, as built, holds: the app's name, identifiers, deployment target and two version
  strings; the commit, the tag or build number, the pinned Xcode version and `swiftc --version`; the host's
  real `__version__`, and its wheel's file name and sha256; the runtime's version, file name, URL and sha256,
  and what the build removed from it (pip, as `pip==<version>`, and each path of `prune.txt`); and every
  installed wheel's name, version, file name, URL and sha256. `ripgrep` is `null` until ripgrep is bundled. It
  holds no time and no signature: it describes the unsigned payload, which a Developer ID job would sign
  unchanged.
- **`Resources/licenses/`** holds one folder per distribution: `CPython/LICENSE.txt`, from the runtime, and
  each installed distribution's licence files from its `*.dist-info` folder. The build refuses a
  distribution that ships none.
- **M10 and the signature.** `codesign` writes each Mach-O file's signature into the file, so after signing
  the two extension files no longer equal their wheels' bytes (seen in CI, run 41407347108, 2026-11-02:
  `record` found the two signed files differ from their wheels'). So M10 (§22) holds byte for byte before
  signing: the build checks it, and writes the unsigned tarball (§18), before it signs anything. On the signed
  bundle, `check_app.py record` first holds `MANIFEST.json` to what it cannot have written itself: each wheel
  it names must be one `uv.lock` pins, by name, version, file, URL and sha256, and the host wheel's sha256
  must be the one the wheel step output. Then it checks every other file byte for byte, and compares each
  Mach-O file with its wheel's after `codesign --remove-signature` on both copies; that comparison is reported
  in the job summary, and does not fail the check. A copy codesign cannot strip is named, with what codesign
  said, and not compared. Seen in CI (run 41407347108): both files read DIFFERENT. That run's check did not
  yet look at whether codesign succeeded, so it does not show the signatures were removed. Either way, a
  DIFFERENT here cannot show the signed files hold their wheels' code; the byte-for-byte check before signing
  is what shows M10. Why they differ has not been looked into.
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
- **Size**: the dependencies are 18 MB (measured). The first build's disk image was 37.9 MB (seen in CI),
  with hash-checked `.pyc` files, which the build no longer ships; the size of the current disk image is not
  recorded here yet.

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
- **Each Mach-O file gets an explicit identifier** (`--identifier`): the interpreter the pins' `runtime_id`,
  `com.lucille-ai.capsule.runtime`, and every other file `com.lucille-ai.capsule.` followed by its path, such
  as `com.lucille-ai.capsule.site-packages.cryptography.hazmat.bindings._rust.abi3` (`signing_identifier` in
  `tools/macos/build_app.py`). codesign's `--prefix` is not used: as codesign documents it, it adds the prefix
  only to an identifier it derives with no dot in it, and `_rust.abi3` has one (not yet tested on a Mac).
  `check_app.py bundle` reads every identifier back from the signed app.
- **Developer ID builds: hardened runtime on every executable. No entitlements.** No
  `disable-library-validation`, no `get-task-allow`, no sandbox. Every library is signed by the same team,
  so library validation should pass (not yet tested on a Mac; the first candidate settles it). One
  question stays open until the first signed build: whether the interpreter needs
  `allow-unsigned-executable-memory`. No Python file in `cryptography`, `switchboard` or `keyring` imports
  `cffi` (measured), but `cryptography`'s Rust extension initialises a cffi module. If the signed build
  proves the entitlement is needed, it goes on the interpreter only, with the reason. **Not decided yet.**
- **Ad-hoc builds keep the hardened runtime.** `tools/macos/pins.toml` says so (`[signing]
  adhoc_hardened_runtime = true`), and the build signs every Mach-O file and the bundle ad hoc (`--sign -`,
  `--timestamp=none`) with `--options runtime` and no entitlements. `check_app.py signature` requires the
  runtime flag on each of them exactly when the pin asks for it, and `check_app.py bundle` refuses a file
  that only the linker signed, or that carries a Team ID.
  - Why it was first expected to fail: under the hardened runtime, library validation lets a process load
    only code "signed by Apple or signed with the same Team ID as the main executable" (Apple, "Disable
    Library Validation Entitlement"). An ad-hoc signature has no Team ID, and a reported ad-hoc,
    hardened-runtime app aborted at launch with dyld's "mapping process and mapped file (non-platform) have
    different Team IDs" (dotnet/macios#26609, macOS 26.6.2). This bundle loads the `lib-dynload`
    modules it imports and two wheel extensions, so it was expected to fail the same way, and an earlier
    draft of this record kept the hardened runtime out of ad-hoc builds for that reason.
  - Seen in CI: on GitHub's `macos-15` runner (macOS 15.7.9) the first build's check loads `cffi`'s backend,
    `cryptography`'s Rust extension and keyring's macOS backend under the hardened runtime. So the failure
    above did not occur there.
  - `ci.yml`'s `macos-smoke` jobs run the same disk image, and that native-code check (`check_app.py host`),
    on macOS 26 (`macos-26`; GitHub's runner-images README, read 2026-11-01, gives that image macOS 26.6.2,
    the version of the report above) and on macOS 27 (`xcode-27`). Seen in CI (run 41407347108,
    2026-11-02): on macOS 26.6.2 and on macOS 27.0.1 the check loads them all under the ad-hoc hardened
    runtime, and the app launches and is driven there, so the abort above did not occur either. If a later
    image aborts on library validation, the pin is set to `false`, and this paragraph records the run and
    the reason.
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
- **Identifiers are permanent.** The bundle identifier (`com.lucille-ai.capsule`), the interpreter's identifier and the Team ID
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
- **How a build is told which.** `tools/macos/build_app.py assemble` and `build` take exactly one of
  `--tag` and `--build-number`, and refuse both or neither. `ci.yml` passes `--tag` for a tag and its run
  number otherwise; it counts its own runs, so those numbers do not continue the first build's workflow's.
  The rule is tested on Linux, and `check_app.py version` checks the built app's `Info.plist` and
  `MANIFEST.json` against it (M11). The first build's `Info.plist` key `LucilleHostVersion` is gone:
  `MANIFEST.json` holds the real version.

## 8. How the app talks to the host

**What we chose.** The app calls the host's own CLI verbs and reads their output. Nothing else.
- **A versioned JSON contract.** A global `--json` flag makes every verb write one JSON object per line on
  standard output, each with `"v": 1` and a `"type"`. Standard error stays human. The prose is
  `spec/CLI.md`; the schemas and golden fixtures ship in the wheel, beside the protocol's. Readers ignore an
  unknown `type`, so the contract can grow.
- **The app never parses human text and never writes the host's files.** Every change goes through a verb.
  The first build is the exception for now: until it uses the JSON contract, which the host's command line
  now has ([spec/CLI.md](../spec/CLI.md)), it reads the verbs' human output ("What is built today";
  `macos/README.md`, "Choices made for the first build").
- **Secrets never go on a command line.** A pairing code goes to `pair --code-stdin` on standard input. With the typed gate
  (§11.2, proposed, pending the maintainer's decision), the typed number goes to `pair confirm` on standard
  input.
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
| `policy previous` | lists, read only, the folders and operations of the last policy a pairing set aside (§11.6) |
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
login ─► Capsule (menu bar; an "Open at Login" item)
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
- **Open at login: on by default, with a switch** (decided by the maintainer on 2026-10-29). The app registers itself as a
  login item (`SMAppService.mainApp`) when onboarding finishes, and unregisters whenever the pair ends. The
  finish screen announces macOS's notice. A switch in Settings turns it off. The command line is unchanged:
  nothing starts `lucille-host run` at login.
- **The lifeline.** `--exit-on-stdin-eof` makes `run` drain, exactly as on its first SIGTERM, when its standard
  input closes. If the app crashes or is force-quit, the kernel closes the pipe and the host finishes its
  operation, replies, and exits. No host runs unseen. That holds only while no other process holds the
  pipe's write end: the app gives it to no other child, and that Foundation's `Pipe` and `Process` keep it
  out of the children the app starts later (some of which never end, such as a verb stuck on a stalled
  drive) is not yet tested on a Mac. The app sets `FD_CLOEXEC` on the ends it keeps itself, under one lock
  held around every child it starts, so the question does not depend on Foundation. The Swift self-test
  checks the effect on CI's Mac for one order: it starts a stand-in `run`, then a long verb as another
  child, drops the app's write end, and sees the stand-in end while the other child lives. The hardware
  tests check that a force-quit leaves no host process.
- **One host per profile.** `run` holds `state/.run.lock` (`os.lockf`). A relaunched app, or a `run` from
  Terminal, finds it held: `run` exits 21 (`already_running`) and changes nothing.
- **The app's own profile.** The app runs every verb with `--profile mac-app`, and so does the Terminal
  wrapper inside it (§6). A `pipx` install on the same Mac keeps the `default` profile: its own folder under
  `~/Library/Application Support/lucille-host`, its own Keychain item, its own `.run.lock` and its own record
  of the last version run (§16). Without this, an older `pipx` host would refuse to start after the app ran
  a newer one (exit 25), or the app would refuse because of the command line's version, and a Keychain item
  the signed interpreter created would likely prompt when the `pipx` Python reads it (not yet tested on a
  Mac). The cost: a Mac with both is two computers to Lucille, each paired on its own. README says to use
  one. Coexistence is tested by hand (§19).
- **Exit codes of `run`**, in `spec/CLI.md` §13.3: 0 stopped on purpose (`drained`, with the `cause`) or the
  pair gone (`forgotten` here, or `ended`, by Lucille or by this computer's side); 20 not paired, or a gate that
  can never open (`no_gate`, or the SAS not confirmed by its deadline while `run` waited, `expired`); 21 already
  running; 23 keyring unusable or insecure; 24 refused environment (`SWITCHBOARD_*`, uid 0); 25 a downgrade
  (§16); 1 a failure the host met (`stopped` `failed`, with a code). A `stopped` event with a reason comes
  before every exit `run` chooses. 22 is reserved.
- **How the app reads an exit.** It reads standard output to its end, for up to about 2 s, then looks at
  how the process ended before it reads any number: 20 to 25 are also macOS signal numbers (`SIGCHLD`,
  `SIGTTIN`, `SIGTTOU`, `SIGIO`, `SIGXCPU`, `SIGXFSZ`), and `SIGXCPU` and `SIGXFSZ` end a process. A
  signal (`terminationReason` `.uncaughtSignal`) is a crash, except the one the app sent at its own last
  step below. An exit's number is checked against the `stopped` reason it read, and the reason decides
  within a number; a number that disagrees with the reason is a crash. When `stopped` is missing after 0
  or 20, the app asks `status --json` where the pair is.
  - 0 `drained` the app asked for (Quit, Prepare to Update): done. 0 `drained` it did not ask for (a
    `SIGTERM` from elsewhere, a `stdin_end` or `stdout_gone` it did not cause), one within a few seconds of
    `ready` above all: a crash. 0 `forgotten` or `ended`: Not connected, and the login item is unregistered.
  - 20: Not connected, by reason (`no_gate`: "Disconnect, then connect again"; `leaked`, `expired`:
    "Connect again"; `several_pairs`, `pair_unusable`: Needs attention, with Disconnect).
  - 21: the app asks `status --json`. While `run` is `running`, a `run` is still finishing, likely its own from
    before a relaunch, which drains once its pipe closed: "Finishing a task from before…", and it tries again
    with the back-off. Once `run` is `stopped` or `not_seen`, it starts at once. If it stays `running` past the
    drain's bound (the operation's time limit, at most 300 s, then 30 s for its reply), it says another copy of
    Capsule's host is running, and how to stop it.
  - 23: two reasons, in different words. `insecure_keyring` is permanent: Needs attention, "The Keychain
    cannot keep Capsule's pairing safely". `store_error` can pass (a locked Keychain, a Deny at a prompt):
    "Capsule cannot open its Keychain item", with Try Again. So is `status --json`'s `pair` `unreadable`
    with those codes, before the app first starts `run`. A `failed` `store_error` after `ready` restarts
    `run`, but counts toward the five crashes below, so that a store that fails after every start cannot
    restart it forever.
  - 24: Needs attention (the app builds the environment, so this is a bug). 25: §16.
  - 2 (usage) can only be the app's own mistake against the host it ships: it is not retried; Needs
    attention, with standard error.
  - 1, a signal, or an exit code the app does not know: a crash.
- **Crashes.** The app restarts `run` with backoff (2, 4, 8 … 300 s). After five crashes in ten minutes it
  stops and shows "Capsule keeps stopping", with the last lines of standard error behind a disclosure. An
  unrequested `drained` counts as a crash, so that something that keeps draining `run` (a standard input that
  is not a live pipe, say) cannot loop it.
- **Quit.** The app's own Quit (the menu, the welcome alert) asks first while `run` lives: "Your Mac will
  stop answering Lucille until you open Capsule again." A quit by Apple event (an AppleScript, Activity
  Monitor, a logout) never asks; the app tells a logout, restart or shutdown from a plain quit by the
  event's `kAEQuitReason` (not yet tested on a Mac). Each reaches the app delegate's
  `applicationShouldTerminate`, which returns `.terminateLater`, drains, and then calls
  `reply(toApplicationShouldTerminate:)`. While any child lives, `pair` included, the app turns off sudden
  and automatic termination (`ProcessInfo.disableSuddenTermination()` and `disableAutomaticTermination(_:)`),
  and turns them on again once none does, so that a logout cannot end the app without its drain (not yet
  tested on a Mac). A `pair` child gets SIGINT, which keeps the pair, then SIGTERM and SIGKILL, before the
  app replies. Every timer runs on the main queue, which the run loop serves in every mode, a menu's
  included. Every modal loop (a note, a question, the Open panel, and AppKit's wait to quit) starts in a
  turn of the main run loop of its own, never inside a main-queue callout: the main queue is serial, and a
  modal loop started inside one of its callouts would keep it from being served until the loop ended, so
  no timer, no event of `run` and no reply to the quit could arrive. Started apart, they all go on while a
  note is up and while the app waits to quit. The silence and crash windows count the time the Mac is
  awake. The drain is a ladder; the app reads both pipes until `run` exits, at every step:
  1. It closes the pipe and shows "Finishing the current task…" with **Stop now**. `run` finishes its
     operation and its reply: up to the operation's time limit, at most 300 s, then 30 s.
  2. **Stop now**, or the bound passing, runs `kill`, which kills the operation's process group and sets the
     pause flag (`controls.py`), so the sheet says so: "Stop now (Capsule will be paused when you next open
     it)". The app waits about 40 s more.
  3. Then the app sends `SIGKILL` to `run`, its own child. This is the one step that can leave the known gap
     of an operation running without its time limit (THREAT_MODEL.md, "Known gaps"). It is there because a
     drain can wait without bound: a request being decided can wait on a folder on a stalled network mount
     (spec/CLI.md §13.3), and `kill` stops only a process group.
  After `stopped`, a `run` that has not exited within about 10 s gets `SIGKILL` too, and the app shows what
  the `stopped` reason said.
- **Logout, restart, shutdown.** The same ladder, with step 2 at 20 s. At `kill` the operation's process
  group is killed and the reply `failed killed_locally` is still posted. Only a force-quit of the whole
  session, or step 3, leaves the known gap of an operation running without its time limit (THREAT_MODEL.md,
  "Known gaps").
- **After a kill at Quit or log out**, the host is paused at the next launch, which the user did not choose
  after a log out. The app does not resume it on its own: a pause nobody asked for fails safe. The next
  launch says "Capsule is paused": "Lucille's requests are turned away until you resume. You paused
  Capsule, or a task was stopped when Capsule quit or you logged out.", with **Resume**. It names both
  causes, because the pause flag holds only a time (`controls.py`), and the app keeps no memory of its own.
- **SIGTERM to the app** asks nothing and runs no `kill`: the app closes the lifeline, stops a `pair`
  child as at a quit, and exits within a few seconds. `run` then drains on its own. The app handles it on a
  dispatch queue of its own, never the main queue, which a note or the wait to quit may hold.
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

This changes 0001's "Local policy and upgrades", and is built (spec/CLI.md §13.5).
- **Reload on change.** `run` loads the policy before each decision, which covers comparing the file's inode,
  modification time and size with what it loaded, and checks each folder at use. A folder the user removes,
  or an operation turned off, applies to the next request. No restart, no signal.
- **An invalid file still fails closed**, but `run` no longer exits: it acts on nothing, reports
  `policy` `invalid` in its `state` event, and resumes when the file is valid again. `policy reset` moves an
  invalid file aside, as `policy.invalid.toml`, and writes the fresh policy; the app's Reset Settings… runs
  `policy reset --json`, and takes `not_invalid` as done.

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
published (§3.2, §18). §11.2 and §11.4 are proposed, pending the maintainer's decision.

### 11.2 The computer's gate is the typed one

**Proposed, pending the maintainer's decision.** Nothing builds on this until it is decided; 0001's gate stands
meanwhile.

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

**Built, on 0001's gate.** The maintainer decided on 2026-11-02 to build the paste form now, with 0001's gate
in place of §11.2's typed number ("What is built today"). After `sas_pending`, the app shows the number and
asks whether Lucille's page shows the same; **They Match** runs `pair confirm --json`, and **They Differ**
runs `pair reject --json`. The app reads `pair status --json` only while its pairing window is open: every
5 s while the pair is `waiting`, and at the deadline. `macos/README.md`, "Connecting to Lucille", says
what each screen does.

### 11.4 The final form: Connect from the computer

**Proposed, pending the maintainer's decision.** Nothing builds on this until it is decided. It also needs a
connect page on Lucille's side.

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

- **Scheme `com.lucille-ai.capsule:`** (reverse-DNS, private-use), path `/pair?code=<code>`. It is registered only
  in builds whose core has the nonce check.
- **The handler acts only while a Connect is pending** (`pair status` reports one). Any other link is ignored,
  and the app's window shows "A website tried to connect this Mac to Lucille. Nothing was connected." The app
  sends no notification, which would need a permission prompt.
- **A link can only deliver a code to `pair`.** No URL can start a Connect, add a folder, turn on an operation or
  change a setting. The handler's type has no reference to the folder or operation runners (§20, M3).
- The code goes to the core on standard input, capped at 2 KiB.

### 11.6 Every new pair starts from the fresh policy

This changes 0001. The policy file is per profile, not per pair, so a second pairing would inherit the first's
folders and operations. When `pair` accepts a new code, the core moves the current policy, unless it is the
fresh one already, to `policy.previous.toml` (in its own folder) and starts from the fresh one: only
`host.describe`, no folders. `pair` says "the policy is reset" when it moved one. Only then does the app read
that file through `policy previous --json` and offer its folders and operations as **unchecked** boxes; a box
the user checks becomes an ordinary `roots add` or `ops enable`. A pairing from the fresh policy moves nothing,
and the file then still holds the policy of an earlier pairing, which the app does not offer. This covers
every way a pair ends: Disconnect, removal on Lucille's side, rejection, the deadline, a deleted Keychain item.
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
again. The app shows that state, and one **Connect Again** button pairs again; as after any new pair that set
a policy aside, the previous folders and operations come back as unchecked boxes (§11.6).

## 12. Folders and macOS privacy

- **Choose a Folder…** first says what macOS may ask ("macOS may ask whether Capsule can access files in
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
- `denied_by_macos` shows "macOS is blocking Capsule from this folder" with **Open Privacy Settings**.
  Lucille still sees `path_outside_root`; the protocol does not change.
- **Cloud folders.** iCloud Drive and other cloud-provider folders may be denied even after the panel. The app
  warns when a folder is under `~/Library/Mobile Documents` or `~/Library/CloudStorage`.
- **Removing a folder** removes it from the policy, which is what stops access. The app never says it revokes
  macOS's grant; it cannot.
- **A moved, renamed or unplugged folder** shows as `missing` in `roots check`, at launch and in Folders, with
  **Choose Again**. A different folder later made at the same path would be taken as the same root; whether
  `roots add` should record the folder's identity so `roots check` reports that is not decided yet.
- **Usage descriptions** for Documents, Desktop, Downloads, removable and network volumes and file providers
  are in the app's `Info.plist`: "Capsule runs the operations you turned on, only inside folders you added.
  macOS asks once before it can open this location." The first build has all six keys
  (`NSDocumentsFolderUsageDescription`, `NSDesktopFolderUsageDescription`,
  `NSDownloadsFolderUsageDescription`, `NSRemovableVolumesUsageDescription`,
  `NSNetworkVolumesUsageDescription` and `NSFileProviderDomainUsageDescription`, each name as Apple's
  Information Property List documentation gives it, read 2026-11-01), each with the sentences on what
  Capsule is for rather than the one above: "Capsule lets Lucille use only the folders you
  choose on this Mac, and only for the abilities you turn on. Lucille can also see which abilities are on,
  and the names you gave your folders." The second sentence is there because `host.describe` is on by
  default (§13).
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
  `Contents/Helpers/rg`. The catalog lists `/Applications/Capsule.app/Contents/Helpers/rg` among its macOS
  paths from the first release that has `files.search`, so shipping the binary later changes no definition hash
  and turns nothing off. This is why the app must live at exactly `/Applications/Capsule.app`: it refuses
  to run anywhere else and asks to be moved there. That path holds the app's name, so after that release the
  name cannot change without a catalog change (0001, "Upgrades": an enabled entry that changes turns off).

## 14. Stop controls

| Menu item | Verb | Shows |
|---|---|---|
| Pause Lucille | `pause` | A pause badge, and a note: "Paused. Lucille's requests will be turned away until you resume." Works offline. |
| Resume Lucille | `resume` | |
| Stop What's Running | `kill` | Shown only while an operation runs. Afterwards: "Stopped, and paused. Resume when you're ready." |
| Quit Capsule | close the pipe (drain); **Stop now** runs `kill`, which also pauses | §9 |
| Disconnect from Lucille… (Settings) | `forget` | "Disconnected. Lucille was told." or "Lucille wasn't reachable. In Lucille, open Computers and remove this Mac." |
| Reset Settings… (Settings) | `policy reset` | Shown when the settings file is invalid (§10) |
| Remove Capsule… (Settings) | section 17 | |

Nothing Lucille sends can unpause the host. The app offers no remote control of any kind.

## 15. Status, activity and diagnostics

- **The menu header** shows the latest state: Connected, Paused, Not connected, or Needs attention (clock skew,
  hub unreachable since a time, policy invalid, Keychain unavailable). From `run`'s `state` events
  (spec/CLI.md §13.2):
  - Connected: `confirmed`, not `paused`, `policy` `ok`, `clock` not `skewed`, a room `reading`, and a
    `last_hub_contact` less than 120 s old by the app's clock. With no room and no `last_left_because`:
    "Connected, waiting for Lucille" (a computer has one standing room, which Lucille offers first).
  - Paused: `paused`.
  - Needs attention: `clock` `skewed` (with Open Date & Time Settings); `policy` `invalid` (with Reset
    Settings…), or `checking` for more than 30 s ("A folder check is stuck"); `last_left_because` set ("This Mac
    left its room because …", in words); a room `read_stuck`; `last_hub_contact` 120 s old or more ("Lucille
    can't be reached since …"); no `state` at all for 120 s ("Capsule's host is not responding").
- **Activity** reads `log --limit 200` and the live `request` events, in words: "10:42. Lucille asked for a
  fingerprint of report.pdf in documents. Finished on this Mac." It never says Lucille received a reply:
  `finished` says the request finished on the computer. Each entry carries the app's own clock when its
  event arrived, since events carry no time. It is kept in memory only. Many `host_too_old` events make one
  notice, with the highest `min_host_version` seen, cleared when the app is updated (§16).
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
  Capsule 0.3.0 or newer" with **Download**, which opens the releases page in the browser. Lucille's page can show
  the same from the job's refusal (Lucille policy as of 2026-10-29; not proven by this repository).
- **Updating, not yet tested on a Mac.** The Finder stops you deleting an app that is open (Rogue Amoeba,
  "The Finder Really Should Prevent Moving Running Applications", 2019), and users replacing a running app
  report "The operation can't be completed because the item … is in use" (Mozilla's support forum, for
  Firefox). The app runs from login, so a
  drag over it would usually be refused. And the new copy cannot quit the old one, because it refuses to run
  anywhere but `/Applications/Capsule.app` (§13), which the old copy still occupies. So the app does it:
  **Download** opens the release page in the browser, and the app then shows "When the download has
  finished, click **Prepare to Update**. Capsule will finish its current task and quit. Then open the
  download, drag Capsule into Applications, and choose **Replace**." **Prepare to Update** drains, like
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
- **Downgrades are refused.** `run` records the newest release that has started `run` on the profile in its
  state folder. A `run` older than that refuses to start (exit 25), and the app says "This Capsule is older than
  the one you used before. Download the newest." `run --allow-downgrade` overrides it, from Terminal only, and
  leaves the record as it is. An older signed release can be a vulnerable one. So a bad release cannot be undone
  by going back in the app: the only fix is the next release, a new PATCH (spec/CLI.md §13.6). The record is
  per profile, so the app's and a `pipx` install's do not interfere (§9). Only a release is recorded: an ad-hoc
  build of the next version (§3.2) on the owner's Mac does not raise it, and one of the same version as the
  release recorded is refused, which `run --allow-downgrade` in Terminal gets past for testing.

**What we did not choose, and why.** An updater (Sparkle). "No auto-update" is a stated bound on a malicious
release (THREAT_MODEL.md; 0001 says "There is no auto-update"). An update channel is the strongest attack
path on every installed copy. **Not decided yet.** If one is ever added, it lives in the Swift app, never in the Python process; it checks and
asks, never installs on its own; its feed is signed, its signature failures never expire, it ships no delta
updates; its feed is on this repository's GitHub Pages; its signing key is held offline by the repository
owner; `PRIVACY.md` says what the check sends before it ships; and the install waits for the drain.

## 17. Removing

- **Disconnect from Lucille…** runs `forget` and shows whether Lucille was told. The app stays, and the
  login item is removed.
- **Remove Capsule…**
  1. Explains: "This disconnects this Mac from Lucille, deletes Capsule's saved connection and settings
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
  Capsule…** unregisters it. Lucille's page says: "If you removed Capsule from your Mac, Remove this
  computer here" (Lucille policy as of 2026-10-29; not proven by this repository). Reinstalling and choosing
  **Remove Capsule…** clears the rest.
- The app never says that Lucille deleted its copy.

## 18. Release pipeline

All on GitHub-hosted runners. Every `uses:` is pinned by commit SHA. No third-party signing or notarization
action: the signing steps use Apple's own tools and `gh`, which the images ship.

| Workflow | Trigger | What it produces |
|---|---|---|
| `ci.yml` | every pull request and push to `main` | Linux tests, the Swift library's tests on Linux, a macOS build signed ad hoc (with the hardened runtime, §7) with every check of §19, UI snapshots. No signing secret. No app is uploaded except on a manual run, and then ad-hoc signed only. |
| `macos-candidate.yml` | manual, on `main` or a `v*` tag, in environment `macos-release` | A signed, notarized candidate, uploaded only to a **draft** release that is never published, then checked by a separate job that holds no secret. Draft releases are visible only to people with write access. |
| `release.yml` | a protected `v*` tag | The wheel on PyPI (environment `pypi`, as 0001), then the Mac jobs below, then one immutable GitHub Release. |

**Built today: the Mac jobs in `ci.yml`.** The first build's own workflow, `macos-app.yml`, is gone, and its
job is now in `ci.yml`, so there is one macOS build pipeline. It runs on every pull request and every push
to `main`, with no path filter; a new push to a pull request cancels that pull request's older run.
- **`macos-app`**, on `macos-15`, selects the pinned Xcode, builds the host's wheel and the app, signs the
  app ad hoc and builds the disk image. It runs every check of §19 on them, and the host's whole test suite
  on a copy of the bundled runtime. Then, with that copy's interpreter, which holds Switchboard's hub and the
  test harness, it runs the end-to-end check of pairing and supervision (`check_app.py pairing`) against a
  hub of its own on a loopback port and a stand-in for Lucille. Then it builds the unsigned app a second
  time, in another folder, and compares the two (below). Each step that drives the app or runs long (the
  self-test, the copy in another folder, the menu, the suite, pairing) has a time limit of its own, well
  inside the job's, so a hang fails that step and the logs and snapshots are still uploaded.
- **`macos-smoke`**, on `macos-26` and on `xcode-27`, runs the same disk image, and the Swift self-test that
  `macos-app` compiled, through the checks that need no build (§19). It builds nothing. It does not run the
  pairing check: it has no copy of the runtime with the test harness, and making one would mean installing
  the suite's dependencies there too. The probe of the host's verbs, printed only, runs in both jobs.
  **The `xcode-27` leg does not gate** (the maintainer's decision, 2026-11-02): the job is
  `continue-on-error` on that leg alone, so it still runs and shows its result, but its failure does not fail
  the workflow run (GitHub's documentation of `jobs.<job_id>.continue-on-error`; not tested here).
  `macos-26` and every other job still gate.
- Neither job holds a secret, has an `environment`, signs with anything but `-` (ad hoc), or notarizes. The
  only secret `ci.yml` names is the private-string list, in its `guards` job.

**How the smoke jobs get the bytes.** `macos-app` puts the disk image and the compiled self-test in the
Actions cache, under a key for its run and attempt, and gives the key and the sha256 of both files as job
outputs. Each smoke job restores exactly that key and, as its first step after the restore, refuses any file
whose sha256 differs. So the smoke jobs test the bytes `macos-app` built, and compile nothing with another
Xcode. This record reads a cache entry as not an upload: a person cannot download one from GitHub's web
pages or its API, and only a workflow run of this repository can restore it (a reading of GitHub's
documentation of the cache; not tested here). The residual: a pull request's run, a fork's included, can
restore entries that runs on `main` saved, while they are kept, and could upload what it restored as an
artifact of its own run. That run could equally build the same app from the same source, so the cache
exposes nothing a fork could not make itself. **The maintainer accepted this reading, with its residual, on
2026-11-02:** an Actions cache entry is not an upload.

**Uploads.** Every run uploads its logs and UI snapshots (the PNG, text and JSON files `check_app.py` writes
in `build/macos/check/`), kept 7 days; never the `~/Library` snapshots, and never the self-test program.
The ad-hoc disk image is uploaded only by a manual run (`workflow_dispatch`) whose every check passed,
kept 3 days, for the maintainer's own Mac. A fork's pull request runs the fork's copy of the workflow,
which can upload anything (`macos/README.md`, "The disk image from CI").

**The unsigned tarball.** Before it signs anything, the build writes `Capsule-unsigned-macOS-arm64.tar.gz`:
the app's files and folders in sorted order, each with the commit's time, owner and group 0 with no names,
and mode 0755 or 0644, in a gzip file with no name or time in its header. `macos-app` compares its two
builds' tarballs by the sha256 of the gzip file and of the tar inside it, and writes SAME, DIFFERENT or
FAILED to the job summary. That is recorded, not a gate, until `macos-payload-repro` below exists. Seen in
CI (`macos-15`, run 41407347108, 2026-11-02): both builds printed the gzip file's sha256
`67729981ad8b616c6335b3bbf1bc6ae4b20157797b0cba3b355d4b4c698b456e` and the tar's
`cdb1d07d7edc1c499bdafea0f3e5f0f5f5c94c081f8c5cfc9339a48938acb924`, which `compare` writes as SAME. (Its
verdict goes to the job summary, which the API does not return; the two digests are in the job's log.) That
is two builds in one job, on one machine; whether another machine gives the same bytes is what
`macos-payload-repro` will show.

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
  latest release", GitHub's REST reference), so `releases/latest/download/Capsule-macOS-arm64.dmg`
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
  repository owner pairs a computer"). The asset name is fixed, `Capsule-macOS-arm64.dmg`, so a link to the
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

**On GitHub-hosted macOS, every pull request and every push to `main`.**

Built today, by `ci.yml`'s `macos-app` job on `macos-15` (§18; `macos/README.md`, "What CI checks"), each
check from `tools/macos/check_app.py`:
- the Swift build, and a bundle assembled with the real pinned runtime, signed ad hoc with the hardened
  runtime (§7; library validation against a Team ID is exercised only on signed candidates);
- the bundle: no symbolic link and no bytecode; exactly the expected Mach-O files, each signed ad hoc by
  the build, not only by the linker, with its own identifier; the signature, again after every other check;
- M10 on the signed bundle (§6), and M11: the version rule and `MANIFEST.json`;
- the libraries each Mach-O file loads, and what the app's executable imports;
- the wrapper runs on the app's profile, and it and the app's flags, on a writable copy, write no
  bytecode;
- the bundle made read-only (`chmod -R a-w`) for the runs, then `codesign --verify --strict` again (M8);
- M6: nothing in the environment changes what the runtime loads, each vector with a control that shows it
  would have loaded without `-I`;
- the app's Swift code against the real command line, also from a copy in another folder;
- the menu driven through System Events, with screenshots kept as a CI artifact;
- a snapshot of `~/Library` before the app runs and after, diffed against an allowlist (M12; §22 says what
  it cannot see);
- the start-up time with no `.pyc` (§6), and the unsigned app built twice and compared (§18), both recorded
  in the job summary, not gates;
- the host's whole test suite against a copy of the bundled runtime, with the test dependencies installed
  into the copy only;
- end to end, with that copy's interpreter, a hub of the check's own on 127.0.0.1 and a stand-in for
  Lucille: the app's profile paired through the command line with the SAS left pending, then, driven
  through System Events, They Match on 0001's gate, `run` as the app's child reading an offered room,
  `files.sha256` answered, `run` restarted after SIGKILL, `run` drained when the app is killed, Quit with
  its drain, Stop now (`kill`, then paused at the next launch) and Disconnect (`forget` after the drain);
- a probe of the verbs the app uses, run as the app runs them, printed only.

And by its `macos-smoke` jobs on `macos-26` and `xcode-27`, on the same disk image: the disk image, the
bundle and signature checks, the wrapper, the read-only runs, M6, the Swift self-test that `macos-app`
compiled, the probe, the launch, the menu, and the `~/Library` diff; not the end-to-end check, which
needs the suite's copy of the runtime (§18). There, a machine where System Events cannot
drive the menu is recorded, not failed; `macos-app` fails it. Where it can, every driven step is checked
as in `macos-app`, and one that fails fails the job. They skip the build and these checks, which
`macos-app` ran on the same bytes: M10, M11, the libraries, the copy in another folder, the start-up time
and the test suite. So on those jobs M6's `.pth` half reads the installed `RECORD` files only, which
`macos-app` compared with the wheels'.

Planned:
- the conformance suite against the bundled host (a `--host-command` option names it);
- end to end through the bundle with the typed number (§11.2), once it is decided, and with the app's own
  `pair --code-stdin` reaching the SAS (the built check pairs through the command line, with `--hub` and
  `--store-file`, which the app never passes);
- the Keychain for real, through a temporary default keychain;
- `lsof` sampling: no listener anywhere, no connection from the app, only the hub from the host;
- crafted scheme URLs (option-shaped codes, a 1 MiB string, paths shaped like folder or operation commands): the
  policy digest is unchanged and no verb but `pair` ran;
- UI snapshots: each screen state rendered to PNG in light and dark, kept as CI artifacts for review. The
  first build's screenshots show only the states the `ui` check drives to, in whatever appearance the
  runner has.

**On the signed build** (§18), on macOS 15, 26 and 27 (`xcode-27`): signing checks, library validation
against a Team ID for the first time, notarization logs, Gatekeeper's view with quarantine, the
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

- **`tools/guards/macos_lint.py`** (built, tested like the others; `VERIFYING.md` says what it reads, and its
  docstring lists every rule). It replaced the Swift scans in the first build's `tests/test_macos_build.py`
  and kept their lists; `check_app.py links` still reads what the built executable imports. It reads every
  Swift file in the repository, the Python files and pins under `tools/macos/`, any plist, entitlements or
  strings file, and every workflow:
  - in Swift, read three ways (the text with its comments, the code with its comments blanked so that
    `connect/**/(` is still a call, and each string literal decoded so that `https:\u{2F}/` is still a
    URL): no `URLSession`, `NW*` or `import Network`, `WebKit` or `WK*`, `NSXPCConnection`,
    `NSXPCListener`, `xpc_*`, `bootstrap_*`, `CFSocket`, `NSStream`, `InputStream`, `OutputStream`,
    `DistributedNotificationCenter` (or `CFNotificationCenterGetDistributedCenter`), `NSAppleScript`,
    `posix_spawn`, `system(`, `/bin/sh`, `/bin/zsh`, `/bin/bash`, `/usr/bin/env` or `osascript`; none of the
    other ways to start a process or use the network that the first build's tests listed, nor `LSOpen*` or
    any `NSUser*Task`; no `URL(string:` or `URL.init(string:` at all; no `UserDefaults`, since the app
    keeps no settings of its own, and no `SWITCHBOARD_`;
  - one exception to the shell names, for tests only, added with the pairing screen: in a Swift file under a
    `Tests` folder, `#!/bin/sh` alone on the first line of a multi-line string literal is not reported, where
    that literal is the only one that starts on its opening line, and that line's code, outside any comment,
    ends with the quotes that open it. The self-test checks the app's child runner (`HostChild`, in
    `HostCLI.swift`) against children that end as told: a signal, a partial line, a lifeline that closes, a
    code on standard input. It writes such a child as a short `sh` script in its temporary HOME and gives that
    file to `HostCLI` in place of the interpreter, so the script is started by the app's own `Process`, the
    one the `[process]` rule allows, and the self-test starts nothing itself. A Swift stand-in would need a
    second compiled program, or a `Process` in the self-test, which the rule forbids; a Python one would need
    an interpreter, while the self-test checks the child runner with `--home` alone, no app given, and a
    shebang cannot name the bundled runtime at a path with a space, which `relocate` uses. Nothing under
    `Tests` is in the app: the self-test is compiled to a file of its own and handed to the smoke jobs beside
    the disk image, and `check_app.py bundle` holds the bundle to its five Mach-O files.
    The exception is that one line: `/bin/bash`, `/bin/zsh`, `#!/bin/sh -e`, a shebang on any other line or in
    a one-line string, a comment, `/bin/sh` anywhere else in the file or the script, and a string that starts
    on the stand-in's opening line are all still reported (`tests/guards/test_macos_lint.py` plants each);
  - `Process` only in `macos/App/HostCLI.swift`, which must write the bundled runtime's path,
    `Contents/Resources/runtime/bin/lucille-host-runtime`, and its arguments,
    `["-I", "-B", "-X", "utf8", "-m", "lucille_host"]`, outside its comments. The lint sees that they are
    written there, not that the `Process` gets them; a build test pins the lines where it does;
  - no read of a pasteboard, or of what is dropped on the app (the user pastes into a field, §11.3; writing,
    for Copy Diagnostics, is allowed by this rule). Until Copy Diagnostics is built, a build test and
    `check_app.py links` refuse `NSPasteboard` altogether; the change that adds it lifts both;
  - no `http://`, `https://`, `ws://`, `wss://` or `ftp://` in Swift. `x-apple.systempreferences:` is not
    allowed yet either: it would come through `URL(string:`, and the change that adds Open Privacy Settings
    records that exception here;
  - no phrase from `PRIVACY.md`'s must-not-claim list in the app's decoded Swift string literals (the
    self-test's aside), in the strings of the functions that render `Info.plist`, in the pins' `[app]`
    values, or in any plist or strings file. The allowlist for sentences that negate one ("That is not a
    review…") exists and is empty;
  - no `CFBundleURLTypes` or `CFBundleURLSchemes` anywhere it reads, except in the build's set of keys an
    `Info.plist` must never carry, and no handler for a URL in Swift (`onOpenURL`, `kAEGetURL`, an
    `open` parameter of type `[URL]` or `Array<URL>`, whatever its name). In the build tools, string
    constants joined with `+`, and an f-string whose every part is constant, are read as the one string
    they make. The release that adds the core's nonce check (§11.4, §11.5) changes this
    rule to allow them only together with that check;
  - no daemon (`LaunchDaemon`, `SMAppService.daemon`, `SMJobBless`, `launchctl` and the like), and no
    installer package (a `*.pkg` file, or `.pkg`, `pkgbuild`, `productbuild`, `productsign` or
    `installer` with `-pkg` later on its line, in Swift, the build tools or a workflow);
  - like every guard here, it catches accidents, not a determined author. It cannot see a phrase split by an
    interpolation; a phrase, URL or name split across Swift literals joined with `+`; an invisible or
    look-alike character inside one; a function used without being called where it is named
    (`let make = socket`, then `make(...)`); a name built at run time, or an Objective-C runtime lookup;
    or shell quoting inside a word of a workflow (`pkg'build'`).
- **The workflow lint** has `macos-26` and `xcode-27`, and no longer the three `macos-14` labels, whose image
  GitHub retires on 2026-11-02. Since GitHub now keys a new macOS image by its Xcode version, the next macOS
  will most likely arrive under an `xcode-*` label, not as `macos-28` or as `macos-latest` moving (an
  inference from GitHub's statement): the lint's label list is reviewed when GitHub announces an image.
- **`.gitignore`** has `.build/` and `.swiftpm/`, so nothing SwiftPM or Xcode writes beside a Swift package
  reaches the AST lint: a link such as `.build/debug` would trip its symbolic-link rule, and a library built
  there its compiled-file rule (`.o` files are not among that rule's suffixes). What SwiftPM writes there is
  inferred, not seen; a test plants such a tree.
- **The build tooling** (`tools/macos/`) is Python under the repository's AST lint. Its pure parts run, and
  are tested, anywhere; `build_app.py assemble` and `build` and every check in `check_app.py` need an Apple
  silicon Mac. One pins file, `tools/macos/pins.toml`, holds the runtime's URL and sha256, the app's name and
  identifiers, the deployment target, the Xcode version, the ripgrep commit and the ad-hoc signing choice;
  the host wheel's build backend is pinned by hash beside it (§6 says why it is a separate file). The
  version rule is checked by the build and by `check_app.py version` (§7, M11), not by this lint, which
  reads source and not what the build computes.
- **The private-string check** reads Swift and plist files like any tracked file.

## 21. What changes in existing documents

- **0001 "Pairing"**: if decided, the host's gate is the typed number (§11.2, proposed); `pair` reads the code on standard input,
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
- **PRIVACY.md** gains two sentences: "The Capsule app connects to nothing itself; only the host inside it
  connects, and only to the hub its pairing code named. Lucille's connect page opens in your browser when you
  click Connect." and "macOS may contact Apple to check the app's signature and notarization; that is Apple's
  behaviour, not this project's."
- **PRIVACY.md's controls sentences** name both shells, since About shows the claim verbatim, for example
  "a log on your computer (`lucille-host log`; Activity in Capsule shows the latest entries)", "Pause
  in Capsule, or `lucille-host pause`", "Stop What's Running, or `lucille-host kill`", "Disconnect in
  Capsule, or `lucille-host forget`". README's copy of the claim and CLAIMS.md's entries for those
  sentences change in the same pull request.
- **CLAIMS.md and its guard tests**, from the Connect release: "A pairing completes only in answer to a
  Connect this computer asked for", as a guard entry mapped to the nonce tests. It is not a sentence of the
  claim, and Lucille still starts every pairing: the user adds the computer on Lucille's page (0001,
  "Pairing", step 1).
- **README.md**: "Install on a Mac", naming the download, the Team ID and the signer's name, and the rule
  "Until this README names a release, a disk image called Capsule did not come from this repository."
  It says to use either the app or a `pipx` install on one Mac, not both (§9).
- **VERIFYING.md**, a new section "The Mac app":
  1. `shasum -a 256` against `SHA256SUMS`, and `gh release verify-asset`.
  2. `gh attestation verify Capsule-macOS-arm64.dmg --repo gald33/lucille-host --signer-workflow
     gald33/lucille-host/.github/workflows/release.yml --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners`.
  3. Apple's view: `spctl -a -t open --context context:primary-signature -vv` and `xcrun stapler validate` on
     the image; `codesign -dv --verbose=4` (Team ID) on the app.
  4. The Python inside: `tools/macos/verify_app.py "/Applications/Capsule.app"` downloads the listed wheels
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
| M6 | Nothing in the environment changes what the bundled runtime loads | `check_app.py environment`: with `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, a user site, `PYTHONUSERBASE` and the working folder each set to load a planted module, the runtime run with the app's flags loads none; for each, a control run without `-I` must load it, or the check fails. `PYTHONSTARTUP` is read only by an interactive interpreter, so no run the app makes (`-m lucille_host`) could load it even without `-I`; its control runs `-i`. No `.pth` file ships beyond the installed `RECORD` files, which `check_app.py record` compares with the wheels'. On the ad-hoc bundle (which carries the hardened runtime, §7) on macOS 15, 26 and 27, and again on every signed build in `macos-verify` |
| M7 | Developer ID builds: hardened runtime, no entitlements, every Mach-O signed by the team | signing-job checks, on signed builds only: an ad-hoc build carries the hardened runtime (§7) but has no Team ID, so library validation against a Team ID is checked only on signed builds |
| M8 | The bundle is never written at run time | `check_app.py readonly`, after `chmod -R a-w`: the host runs, nothing in the bundle changes, no bytecode appears, and `codesign --verify --strict` passes after; the signature is checked again after every other check; the read-only end-to-end run once it exists. On the ad-hoc bundle on macOS 15, 26 and 27, and on every signed build |
| M9 | Nothing runs as root, at install or at run time | no `.pkg`, no daemon (`macos_lint`); uid 0 refused (0001) |
| M10 | The wheel inside is PyPI's; every file under `site-packages` is a file of a wheel's `RECORD`, except the named installer-written files, and no `.pyc` is there | the build guard, byte for byte before signing (`payload.record_problems`, also run on Linux); `check_app.py record` on the signed bundle, which holds each wheel `MANIFEST.json` names to `uv.lock` and the host wheel to the sha256 the wheel step output, and where each Mach-O file is compared with its wheel's after both signatures are removed, and that comparison is reported, not failed (§6); post-release checks; `verify_app.py` |
| M11 | A build from a tag has one version number; any other build records its real version in `MANIFEST.json` | the build's version rule (`bundle_versions`, tested on Linux); `check_app.py version` on the built app |
| M12 | The app writes nothing outside the host's own folders except what macOS writes for any app | the `~/Library` diff (`check_app.py library-diff`): a changed path that names the bundle identifier, the app's name or `lucille` must match `tools/macos/library-allowlist.txt`; every other change is printed, not gated; the state folder of the app's profile (`mac-app`) must be among the changes. What it cannot see is below the table |
| M13 | Pairing and the read loop start no process | the audit-hook test, on Linux and macOS |
| M14 | No host outlives its app unseen | close the pipe: `run` drains and exits 0 |
| M15 | One host per profile | a second `run` exits 21 |
| M16 | A policy change applies to the next decision | remove a folder while `run` runs; the next request for it is refused `path_outside_root` |
| M17 | Every new pair starts from the fresh policy | pair, enable, forget, pair again: only `host.describe`, no folders |
| M18 | The app never offers to widen access in answer to Lucille | review, and a UI snapshot of a `not_enabled` entry with no button |

**What M12's check cannot see.** It is weaker than the invariant. It walks `~/Library` before the app is
launched and after the menu has been driven, and fails only on a new, changed or removed path whose name
holds the bundle identifier, the app's name or "lucille", in any case, and that the allowlist does not
allow. The allowlist starts with the host's own folder (`Application Support/lucille-host/`) and the app's
`Preferences` file, and gains a line only after a run shows the path, with the run and the reason. So it
does not catch a write to a path whose name says nothing of the app, which it only prints; nor anything
under `~/Library/Developer` or `~/Library/Android`, which it skips because the runner images keep their
own tools there; nor a folder macOS does not let it read, which it lists; nor anything outside `~/Library`;
nor what the app writes in a run that does not take the paths CI drives. The state folder of the app's
profile (`mac-app`) must appear among the changes, so a walk that saw nothing, of the wrong folder say,
fails rather than passes. On the smoke jobs, where the menu may not be driven, the check makes those writes
itself first (`pause`, then `resume`).

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

- The app's name. Capsule is a working name. A trademark search on it comes before the first signed build,
  after which its identifiers cannot change (§7), and before the catalog lists the bundled `rg` path, which
  contains it (§13).
- Whether notarization accepts Mach-O files under `Contents/Resources`, or they move to `Contents/Frameworks`
  and the interpreter to `Contents/Helpers` (§6).
- Whether shipping no `.pyc` makes start-up too slow, and so whether `checked-hash` files with a recompiling
  `verify_app.py` replace it (§6). CI measures it on every run; the maintainer judges it.
- Whether the bundle loads under library validation and the hardened runtime, which only a Developer
  ID-signed candidate exercises (§7).
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
