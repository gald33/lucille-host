# Loft, by Lucille

Loft is the Mac app for the host. It lets Lucille use only the folders you choose on this Mac, and only
for the abilities you turn on. It is a menu bar app. Inside it runs the host program, `lucille-host`,
which keeps its own name, command line and files.

## Status: a first build, not a release

This folder builds a first, small version of Loft. It is not a release and it is not ready for
anyone who is not developing it. It cannot connect to Lucille, because the host cannot pair yet. Its disk
image is not notarized, so macOS refuses to open it normally (see [The disk image from CI](#the-disk-image-from-ci)).

## What this builds

- **`Loft.app`**, a menu bar app for Apple silicon Macs with macOS 15 or later. It has no main
  window and no Dock icon.
- Inside it, a Python 3.13 runtime from [python-build-standalone](https://github.com/astral-sh/python-build-standalone),
  pinned in [`runtime.toml`](runtime.toml) by its URL and its sha256. The build refuses any other file.
- Installed into that runtime: the host's wheel, built from this checkout, and the exact dependency
  wheels `uv.lock` pins, installed by hash (`pip install --require-hashes --no-deps --only-binary :all:`).
  The host package is the same code as in `src/lucille_host/`, unchanged.
- The host's wheel is built with the build backend that [`build-constraints.txt`](build-constraints.txt)
  pins by hash (`uv build --build-constraints build-constraints.txt --require-hashes`). Those are the
  versions `pyproject.toml`'s `[build-system]` requires, and a test checks that the two agree.
- **`Loft-<version>-<commit>-adhoc-macOS-arm64.dmg`**, a disk image whose volume is named Loft. It holds
  the app and a link to the Applications folder. The file name says the signature is ad hoc and names
  the commit, so the image cannot be taken for a release asset.

## What the app does when it opens

The app has no window to show, so when it opens it says where it is: "Loft is in your menu bar". The
note first says what Loft is for, in the sentence above, and then where to click. It says so again
whenever it is opened while it runs, for someone who could not find the icon. The note offers **Quit
Loft** too, because on a crowded menu bar, or beside a notch, macOS hides icons that do not fit, and
then the menu cannot be reached.

The menu bar item is named "Loft", or "Loft, paused", for VoiceOver.

When macOS asks whether Loft may access files in Documents, Desktop, Downloads, or on a removable or
network volume, its question shows the same sentence on what Loft is for. The `Info.plist` holds it
under each of the five keys macOS reads for those places.

## What the menu does

| Item | What it runs |
|---|---|
| "Loft *version*" | `--version` |
| "Not connected to Lucille" | Nothing. The host cannot pair yet, so this Mac is never connected. When the host is paused, the line reads "Not connected to Lucille · Paused", from the pause flag the host keeps (see below), a line under it says that Lucille's requests are turned away until you resume, and the icon changes. |
| Pause Lucille / Resume Lucille | `pause` / `resume` |
| Abilities | `ops list`. Each ability shows in a few plain words, such as "Check a file's fingerprint", with a checkmark when it is enabled, and a sentence under it says what Lucille gets: "Lucille gets a code that identifies one file's exact contents, not the contents." Choosing one runs `ops enable` or `ops disable`. `host.sleep`, which is for testing, is not listed. Nor is `host.describe`; while it is on, the menu says what it shares once connected. |
| Add a Folder… | See below. |
| Folders | `roots list`. Each folder has a **Remove** item, which runs `roots remove <alias>`. |
| Connect to Lucille (coming soon) | Nothing. It is disabled. |
| Quit Loft | Quits the app. |

**Add a Folder…** opens the standard Open panel, whose button reads "Allow Loft to Use This Folder".
If the folder chosen is the home folder, holds it, or is the whole disk, the app first says which, and
what is inside, with **Choose a Smaller Folder** as the default button; that button opens the Open panel
again. Then the app asks for the name Lucille will see, suggesting one made from the folder's
name that no other folder uses: written in Latin letters where macOS can, so that `日本語` gives
`ri-ben-yu`, with `folder-` put first when it starts with a digit (`2026 Taxes` gives
`folder-2026-taxes`), and `-2`, `-3` added when it is taken. A name the host would refuse is turned into
one it accepts, and shown again before anything is added.

The same dialog says what macOS may ask next. For a folder in Documents, Desktop or Downloads, or on
another drive, it says that macOS may ask whether Loft can access it, and to click Allow. For a
folder in iCloud Drive or another cloud folder, it says that such folders may not work yet.

Then the app runs `roots add <alias> <path>`, with `--allow-home-or-root` only if the user chose Add
Anyway. If that takes more than a moment (a network drive, or a question from macOS), a small window with
a **Cancel** button shows; Cancel stops the run. On success the app says that once this Mac is
connected, Lucille can use the folder for the abilities the user turns on. If the host found no folder
there but the app can see one, or macOS refuses the app too, the app says that macOS is blocking Loft
from the folder, and where in System Settings to allow it.

The menu reads the host's state again each time it opens. When the host cannot be read, the menu says
so ("Couldn't read your folders") instead of showing an empty list, and a short line at the top names the
problem; choosing it shows the details. When a verb fails, the app says why in a plain sentence for each
refusal it knows from the host's command line (a name already in use, the 64-folder limit, the host's own
folder, an unreadable settings file, and the others in `App/HostText.swift`), and shows the host's own
words as a detail. A refusal it does not know reads "Something went wrong in Loft's helper program",
with the host's words as the detail.

Pause, Resume, the ability switches and Remove show the same window with a **Cancel** button when they
take more than a moment. A run the app gave up on (see below) may still hold the lock the host takes to
change its settings, and then every change to folders or abilities waits for it. While such a run is
still going, the menu says that a folder check is stuck, and a change that times out or is cancelled
says so too, rather than blaming its own folder.

## How the app talks to the host

- The app drives the host only through the host's command line. The one program it starts is the
  interpreter inside the bundle, as
  `Contents/Resources/python/bin/python3 -I -B -m lucille_host <verb> ...`:
  - `-I` is isolated mode: no `PYTHON*` variables, no user site-packages, and no current folder on the
    import path.
  - `-B` writes no `.pyc` file, so nothing is written inside the signed app.
- The arguments go as an argv array, never through a shell.
- The environment is fixed. It holds `HOME`, `USER`, `LOGNAME`, `TMPDIR`, `PATH=/usr/bin:/bin` and
  `LANG`, and nothing else from the app's own environment.
- Each run has a time limit: 10 minutes for `roots add`, which may wait on a question from macOS or on
  a slow drive, and 30 seconds for every other verb. A run that passes it, or that the user cancels, gets
  SIGTERM, then SIGKILL 2 seconds later. A process that still has not ended 2 seconds after that (one
  stuck on a stalled network drive cannot end) is left behind, and the app stops waiting for it. The app
  counts it as stuck until it ends.
- The app writes none of the host's files. It reads one, the pause flag at
  `~/Library/Application Support/lucille-host/default/state/paused`, to show whether the host is paused.
- The app opens no network connection and listens on nothing.

All of this is in [`App/HostCLI.swift`](App/HostCLI.swift). Two checks keep it so, and neither is a
proof:

- `tests/test_macos_build.py` reads the Swift sources. It fails if any file but `HostCLI.swift` names
  Foundation's `Process`, if any file uses one of the other ways to start a process it lists (`posix_spawn`,
  `fork`, `exec`, `NSWorkspace`, ...) or one of the ways to reach the network it lists (`URLSession`,
  Network's types, sockets, `URL(string:)`, reading `contentsOf:` a URL, ...), and if a file imports
  anything but Foundation, AppKit and SwiftUI (Foundation and the C library alone, for the files the
  self-test runs).
- `check_app.py links` reads what the built app's executable imports (`nm -u`). It fails on the C
  functions that start a process or open a socket, on the classes that do either, and on a link to
  Network, CFNetwork or WebKit.

Both match names. Code written to get past them can: through the Objective-C runtime, say, which looks a
class up by a string. Review of every change is what stands behind them.

## Building it on a Mac

You need an Apple silicon Mac with macOS 15 or later, Xcode or the Xcode command line tools, `uv`, and
network access to GitHub and PyPI. From the repository's root:

```sh
uv run --no-project python macos/build_app.py build
```

The app and the disk image land in `build/macos/`, which git ignores. Then run the checks CI runs:

```sh
for check in host swift relocate links signature launch ui gatekeeper dmg; do
  uv run --no-project python macos/check_app.py "$check"
done
```

On your own Mac, `ui` needs your terminal to have Accessibility permission (System Settings, Privacy &
Security, Accessibility), and it takes over the screen for about two minutes. It changes your own host's
settings while it runs (it pauses, switches an ability, adds folders) and puts them back at the end.
Without that permission it drives nothing and says so; with `--require-driven`, as CI runs it, that is a
failure.

`uv run --no-project python macos/build_app.py plan` runs on any system. It checks the pins and the build
constraints, and writes the `Info.plist` the app would get, without running any other program.

[`build_app.py`](build_app.py) says what each build step does, and [`check_app.py`](check_app.py)
says what each check proves.

## What the build removes from the runtime

The app never uses these, so the build removes them from python-build-standalone's files:

- every program in `bin/` except `python3`, including pip's;
- `include/` and `share/`;
- everything in `lib/` except `python3.13/`: `libpython3.13.dylib`, since `python3` links Python
  statically, the Tcl/Tk libraries, and the pkg-config files;
- from the standard library, `tkinter`, `_tkinter`, `idlelib`, `turtle`, `turtledemo`, `ensurepip`
  and `config-3.13-darwin`;
- pip itself, after it has installed the wheels.

CI runs the host's whole test suite on the trimmed runtime. A module the host needs cannot go missing
without a test failing.

## Signing

The build signs every Mach-O file in the bundle, innermost first, and then the bundle. The signature
is ad hoc (`codesign -s -`), with the hardened runtime and no entitlements. It never uses `--deep` to
sign. CI checks the following on every build:

- `codesign --verify --deep --strict` passes on the app after the host has run from the bundle, so the
  runs wrote nothing into it;
- `codesign --verify --strict` passes on each Mach-O file in the bundle on its own. The interpreter and
  the dependencies' native libraries are under `Contents/Resources`, where the bundle's signature may
  seal them as plain files and not check their own signatures, so this is checked separately;
- every Mach-O file has the hardened runtime flag;
- the dependencies' native code (cffi, cryptography, and keyring's macOS backend) loads under the
  hardened runtime with no entitlement.

A signature that macOS trusts needs a Developer ID certificate and notarization. This build does
neither. Whether the native code still loads with no entitlement under a Developer ID signature is
not tested yet, and nor is whether Apple's notary service accepts code under `Contents/Resources`.
There is reason to expect that it does: BeeWare's Briefcase signs the Mach-O files it puts under
`Contents/Resources` the same way, inside out, and its apps are notarized.

## What CI checks

The `macos-app` workflow builds the app on a GitHub-hosted Apple silicon runner (`macos-15`) and runs
each check in [`check_app.py`](check_app.py). Its token can only read the repository, and it uses no
secret. A check that finds a problem fails the build.

| Check | What it shows |
|---|---|
| `host` | The interpreter in the bundle runs the host in isolated mode, with nonsense in `PYTHONHOME` and `PYTHONPATH`: `--version`, `ops list`, a `roots` round trip, `pause` and `resume`, and the refusal of a home folder. The dependencies' native code loads. |
| `swift` | The app's own Swift code that runs the command line and reads its output works against the real command line, through the same calls the menu makes: the folder name suggestions and corrections, the plain sentence for each refusal, the time limit and Cancel. |
| `relocate` | A copy of the app in another folder, with a space in its path, passes `host` and `swift`. |
| `links` | Every library a Mach-O file loads is macOS's own (`/usr/lib/`, `/System/Library/`), or resolves to a file that exists inside the bundle. `@loader_path`, `@executable_path` and `@rpath` are resolved as dyld resolves them, `..` included, so a path that climbs out of the bundle, and a library the build removed, both fail. Every run path must resolve inside the bundle. The log shows where each library and run path resolves. No Mach-O file needs a newer macOS than the app's minimum, and no file holds the build folder's path. The app's executable imports nothing on the lists above. |
| `signature` | The signature is valid after the runs above, on the app and on each Mach-O file in it; every Mach-O file has the hardened runtime, and nothing has entitlements. |
| `launch` | The app opens the way Finder opens it, and is still running 10 seconds later. |
| `ui` | Driven through System Events as a user would: the app says what it is for and where its menu is when it opens, and again when it is opened while it runs; the menu bar item's title or description, which VoiceOver reads as its name, is "Loft" (its tooltip does not count); the menu shows the version, "Not connected to Lucille" and every item, and lists abilities in plain words, each with its sentence on what Lucille gets; Pause Lucille and Resume Lucille change the host's pause flag, the status line and the menu bar item's name; an ability switches on, shows a checkmark, and switches off; Add a Folder… goes through the Open panel and the name dialog, where a typed name the host would refuse comes back corrected, and adds the folder; Remove in the Folders submenu removes it; the home folder brings up the question first, Choose a Smaller Folder goes back to the Open panel, and Add Anyway adds it. Each step is checked against what the host's command line then reports. With `--require-driven`, as CI runs it, a machine where System Events cannot drive the app fails the check. Screenshots are kept as the artifact `loft-macos-screenshots`. |
| `gatekeeper` | Prints `spctl`'s verdict. It is "rejected", as expected for an ad hoc signature, and does not fail the build. |
| `dmg` | Exactly one disk image was built, its name ends `-adhoc-macOS-arm64.dmg`, and it verifies, mounts, and holds the app, with a valid signature, and a link to `/Applications`. |
| test suite | The host's whole test suite passes on the trimmed runtime. |

## What CI does not check

CI adds its folders under the home folder, where macOS asks nothing. These need a Mac and a person, and
none has been done yet:

- **A folder in Documents, Desktop or Downloads.** Whether macOS asks at all after the Open panel,
  whether its question names Loft rather than `python3` and shows Loft's sentence on what it is for,
  and whether the folder is added after Allow. After Don't Allow, whether the app says that macOS is
  blocking it, and whether System Settings, Privacy & Security, Files & Folders then shows a switch
  under Loft.
- **A folder on an external or network drive, and one in iCloud Drive.**
- **A stalled network drive.** Whether Cancel works, and whether the menu then says a folder check is
  stuck.
- **VoiceOver.** Whether it reads the menu bar item as "Loft", and "Loft, paused".
- **A full menu bar, or one beside a notch,** where macOS hides the icon.

## The disk image from CI

The `macos-app` workflow uploads the disk image as the artifact
`loft-adhoc-macos-arm64-dmg-<event>-<commit>`, and keeps it for 7 days. It does so only when every
check passed.

This workflow's own copy skips the upload for a pull request opened from a fork. That does not stop a
fork: its pull request runs the fork's copy of the workflow, as well as the fork's build code, and that
copy can upload anything, under any name. So an artifact on a fork's pull request run is the fork's build.
The repository's Actions setting "Approval for running fork pull request workflows from contributors",
at its strictest, makes every such run wait for a maintainer. It is a setting, which no file here can
enforce or show. Take a disk image only from a run on a push to this repository, or on a pull request
from one of its own branches; the artifact's name says which event and which commit built it.

**It is ad hoc signed and not notarized**, and its name ends `-adhoc-macOS-arm64.dmg`. When it is
downloaded with a browser, macOS refuses to open the app normally, because it cannot verify who made it.
It is for people developing the app, on their own Mac. Do not give it to anyone else, and do not tell
anyone to work around the warning.

## Not done yet

- **Developer ID signing and notarization.** They need the project's Apple Developer account, which does
  not exist yet. Until then, no build is fit for other people. The first task once it exists is to send
  this exact layout to Apple's notary service and read its log, even if it passes; the signature then
  needs a secure timestamp (`--timestamp`, not `--timestamp=none`). If the notary objects to code under
  `Contents/Resources`, there are two ways out, and both are work. One is a `Python.framework` layout
  under `Contents/Frameworks`, as Briefcase uses. The other moves each Mach-O file, the interpreter and
  every extension module, to `Contents/Frameworks` or `Contents/Helpers`, fixes its load paths with
  `install_name_tool`, and leaves a symbolic link at its old place, so that Python still finds each
  module in its package. That second way needs the build's rule against symbolic links in the bundle
  relaxed for exactly those links. Moving the whole runtime would not do: those folders are for code
  only, and Apple's signing tools take a folder with a dot in its name, such as `python3.13` or a
  `.dist-info`, for a bundle.
- **Pairing.** The host has no `pair` verb yet, so "Connect to Lucille" is disabled.
- **Running in the background.** The host has no long-running loop yet, so the app starts nothing that
  keeps running, and it does not open at login.
- **Updates.** There are none. A new version is a new disk image.
- **Uninstall.** Dragging the app to the Trash removes it. The host's own folder,
  `~/Library/Application Support/lucille-host`, stays.
- **Intel Macs.** The locked `cryptography` publishes no Intel macOS wheel, so the app is Apple silicon only.
- **A Welcome window and a main window.** The design has a Welcome window that leads through
  connecting, choosing a folder and turning on an ability, and a main window with the status, folders,
  abilities, activity and settings. This build has the menu, and a note that says where it is.
- **An Open Privacy Settings button.** When macOS blocks a folder, the app says where to go in System
  Settings, but cannot open it: that needs `NSWorkspace`, which the checks above keep out of the app.
- An app icon, an activity view, and a machine-readable output from the host's command line, which the
  app would read instead of its text.

## Choices made for the first build

The design may change each of these.

- **The app runs the runtime's own `python3`.** The design proposes a small native launcher that embeds
  Python instead, so that every host process is one named, signed program, which the Keychain and the
  privacy prompts recognise, and so that the bundle holds no general-purpose interpreter. Chosen for the
  first build; the design may change it.
- **Python lives in `Contents/Resources/python`.** If notarization objects to code there, see "Not done
  yet" for the two ways out.
- **The app is named Loft, with the bundle identifier `com.lucille-ai.loft`.** Only the app has the new
  name: the host program inside is still `lucille-host`, with its folder at
  `~/Library/Application Support/lucille-host`. The identifier must not change after the first signed
  release, because changing it makes macOS ask the user again about every folder and Keychain item.
- **`CFBundleShortVersionString` holds the version's release numbers** (`0.1.0` for `0.1.0.dev0`), because
  Apple allows only three numbers there. The exact version is in the `LucilleHostVersion` key, and in the
  menu.
- **macOS 15 or later, Apple silicon only.**
- **The app reads the host's text output and the pause flag.** The design adds a versioned JSON output
  and a status file to the host's command line; until then, `App/HostText.swift` follows the text, and
  the Swift self-test checks it against the real command line on every build.
- **The words for each ability are in the app** (`App/HostText.swift`). The design moves them into the
  host's catalog, so that every app shows the same reviewed words. A test fails when the catalog has an
  operation the app neither names nor leaves out of the menu.
- **The app asks about a broad folder before the user names it**, when it can tell from the path (the home
  folder, a folder above it, or `/`). The host still decides; when it refuses a folder the app did not
  recognise, the app asks then.
- **`.pyc` files are hash-checked**, so a copied app with new file times still uses them, and Python
  never needs to write one.
- **`PATH` is `/usr/bin:/bin`** for the host's command line.
- **The disk image is a plain `hdiutil` image**, with no background picture or window layout.

These depart from the design's screens. Each was chosen for the first build; the design may change it.

- **The name step is one dialog, with the name in a field the user can change.** The design shows the
  name in a confirmation sheet, with a separate Change name… button.
- **The Open panel opens where macOS chooses**, not in Documents.
- **What macOS may ask is said in the name dialog**, just before the run that makes macOS ask, not in a
  screen before the Open panel.
- **There is no Open Privacy Settings button** (see "Not done yet").
- **The pause icon replaces the app's icon**, a pause sign in place of the computer, where the design adds
  a pause badge to the app's icon.
- **Pausing shows no note.** The design confirms Pause with "Paused. Lucille's requests will be turned
  away until you resume."; this build says so in the menu, under the status line, while paused.
- **A name in a script macOS cannot write in Latin letters gives `folder`**, and on a system whose
  Foundation lacks ICU's transforms (the Swift self-test does not run on one today) every non-Latin name
  does.
- **The app says where its menu is each time it opens.** It keeps no settings of its own, so it cannot
  tell a first launch from a later one.
