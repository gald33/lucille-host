# Lucille Host for Mac

## Status: a first build, not a release

This folder builds a first, small version of the Mac app. It is not a release and it is not ready for
anyone who is not developing it. It cannot connect to Lucille, because the host cannot pair yet. Its disk
image is not notarized, so macOS refuses to open it normally (see [The disk image from CI](#the-disk-image-from-ci)).

## What this builds

- **`Lucille Host.app`**, a menu bar app for Apple silicon Macs with macOS 15 or later. It has no window
  and no Dock icon.
- Inside it, a Python 3.13 runtime from [python-build-standalone](https://github.com/astral-sh/python-build-standalone),
  pinned in [`runtime.toml`](runtime.toml) by its URL and its sha256. The build refuses any other file.
- Installed into that runtime: the host's wheel, built from this checkout, and the exact dependency
  wheels `uv.lock` pins, installed by hash (`pip install --require-hashes --no-deps --only-binary :all:`).
  The host package is the same code as in `src/lucille_host/`, unchanged.
- The host's wheel is built with the build backend that [`build-constraints.txt`](build-constraints.txt)
  pins by hash (`uv build --build-constraints build-constraints.txt --require-hashes`). Those are the
  versions `pyproject.toml`'s `[build-system]` requires, and a test checks that the two agree.
- **`Lucille-Host-<version>-<commit>-adhoc-macOS-arm64.dmg`**, a disk image that holds the app and a link
  to the Applications folder. The name says the signature is ad hoc and names the commit, so the image
  cannot be taken for a release asset.

## What the menu does

| Item | What it runs |
|---|---|
| "Lucille Host *version*" | `--version` |
| "Not connected to Lucille" | Nothing. The host cannot pair yet, so this Mac is never connected. When the host is paused, the line reads "Not connected to Lucille · Paused", from the pause flag the host keeps (see below), and the icon changes. |
| Pause Lucille / Resume Lucille | `pause` / `resume` |
| Abilities | `ops list`. Each ability shows in a few plain words, such as "Check a file's fingerprint", with a checkmark when it is enabled. Choosing one runs `ops enable` or `ops disable`. `host.sleep`, which is for testing, is not listed. Nor is `host.describe`; while it is on, the menu says what it shares once connected. |
| Add a Folder… | See below. |
| Folders | `roots list`. Each folder has a **Remove** item, which runs `roots remove <alias>`. |
| Connect to Lucille (coming soon) | Nothing. It is disabled. |
| Quit Lucille Host | Quits the app. |

**Add a Folder…** opens the standard Open panel. If the folder chosen is the home folder, holds it, or is
the whole disk, the app first says which, and what is inside, with **Choose a Smaller Folder** as the
default button; that button opens the Open panel again. Then the app asks for the name Lucille will see,
suggesting one made from the folder's name that no other folder uses (`taxes`, then `taxes-2`). A name the
host would refuse is turned into one it accepts, and shown again before anything is added. Then the app
runs `roots add <alias> <path>`, with `--allow-home-or-root` only if the user chose Add Anyway. If that
takes more than a moment (a network drive, say), a small window with a **Cancel** button shows; Cancel
stops the run. On success the app says that Lucille can now use the folder.

The menu reads the host's state again each time it opens. When the host cannot be read, the menu says
so ("Couldn't read your folders") instead of showing an empty list, and a short line at the top names the
problem; choosing it shows the details. When a verb fails, the app says why in a plain sentence for each
refusal the host's command line gives (a name already in use, the 64-folder limit, the host's own folder,
an unreadable settings file), and shows the host's own words as a detail.

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
- Each run has a 30-second limit. A run that passes it, or that the user cancels, gets SIGTERM, then
  SIGKILL 2 seconds later. A process that still has not ended 2 seconds after that (one stuck on a stalled
  network drive cannot end) is left behind, and the app stops waiting for it.
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

## What CI checks

The `macos-app` workflow builds the app on a GitHub-hosted Apple silicon runner (`macos-15`) and runs
each check in [`check_app.py`](check_app.py). Its token can only read the repository, and it uses no
secret. A check that finds a problem fails the build.

| Check | What it shows |
|---|---|
| `host` | The interpreter in the bundle runs the host in isolated mode, with nonsense in `PYTHONHOME` and `PYTHONPATH`: `--version`, `ops list`, a `roots` round trip, `pause` and `resume`, and the refusal of a home folder. The dependencies' native code loads. |
| `swift` | The app's own Swift code that runs the command line and reads its output works against the real command line, through the same calls the menu makes: the folder name suggestions and corrections, the plain sentence for each refusal, the time limit and Cancel. |
| `relocate` | A copy of the app in another folder, with a space in its path, passes `host` and `swift`. |
| `links` | No Mach-O file names a library outside macOS or the bundle, or needs a newer macOS than the app's minimum, and no file holds the build folder's path. The app's executable imports nothing on the lists above. |
| `signature` | The signature is valid after the runs above, on the app and on each Mach-O file in it; every Mach-O file has the hardened runtime, and nothing has entitlements. |
| `launch` | The app opens the way Finder opens it, and is still running 10 seconds later. |
| `ui` | Driven through System Events as a user would: the menu shows the version, "Not connected to Lucille" and every item, and lists abilities in plain words; Pause Lucille and Resume Lucille change the host's pause flag and the status line; an ability switches on, shows a checkmark, and switches off; Add a Folder… goes through the Open panel and the name dialog, where a typed name the host would refuse comes back corrected, and adds the folder; Remove in the Folders submenu removes it; the home folder brings up the question first, Choose a Smaller Folder goes back to the Open panel, and Add Anyway adds it. Each step is checked against what the host's command line then reports. With `--require-driven`, as CI runs it, a machine where System Events cannot drive the app fails the check. Screenshots are kept as the artifact `lucille-host-macos-screenshots`. |
| `gatekeeper` | Prints `spctl`'s verdict. It is "rejected", as expected for an ad hoc signature, and does not fail the build. |
| `dmg` | Exactly one disk image was built, its name ends `-adhoc-macOS-arm64.dmg`, and it verifies, mounts, and holds the app, with a valid signature, and a link to `/Applications`. |
| test suite | The host's whole test suite passes on the trimmed runtime. |

## The disk image from CI

The `macos-app` workflow uploads the disk image as the artifact `lucille-host-adhoc-macos-arm64-dmg`, and
keeps it for 7 days. It does so only when every check passed, and never for a pull request opened from a
fork, whose build code is the fork's. **It is ad hoc signed and not notarized**, and its name ends
`-adhoc-macOS-arm64.dmg`. When it is downloaded with a browser, macOS refuses to open the app normally,
because it cannot verify who made it. It is for people developing the app, on their own Mac. Do not give
it to anyone else, and do not tell anyone to work around the warning.

## Not done yet

- **Developer ID signing and notarization.** They need the project's Apple Developer account, which does
  not exist yet. Until then, no build is fit for other people. The first task once it exists is to send
  this exact layout to Apple's notary service and read its log, even if it passes; the signature then
  needs a secure timestamp (`--timestamp`, not `--timestamp=none`). If the notary objects to code under
  `Contents/Resources`, the runtime moves to `Contents/Frameworks` and the interpreter to
  `Contents/Helpers`, as Apple's guide to embedding nonstandard code describes.
- **Pairing.** The host has no `pair` verb yet, so "Connect to Lucille" is disabled.
- **Running in the background.** The host has no long-running loop yet, so the app starts nothing that
  keeps running, and it does not open at login.
- **Updates.** There are none. A new version is a new disk image.
- **Uninstall.** Dragging the app to the Trash removes it. The host's own folder,
  `~/Library/Application Support/lucille-host`, stays.
- **Intel Macs.** The locked `cryptography` publishes no Intel macOS wheel, so the app is Apple silicon only.
- An app icon, an activity view, and a machine-readable output from the host's command line, which the
  app would read instead of its text.

## Choices made for the first build

The design may change each of these.

- **The app runs the runtime's own `python3`.** The design proposes a small native launcher that embeds
  Python instead, so that every host process is one named, signed program, which the Keychain and the
  privacy prompts recognise, and so that the bundle holds no general-purpose interpreter. Chosen for the
  first build; the design may change it.
- **Python lives in `Contents/Resources/python`.** If notarization objects to code there, it moves to
  `Contents/Frameworks`.
- **The bundle identifier `com.lucille-ai.host` is a placeholder.** It must be final before the first
  signed release, because changing it later makes macOS ask the user again about every folder and
  Keychain item.
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
