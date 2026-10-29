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
- **`Lucille-Host-<version>-macOS-arm64.dmg`**, a disk image that holds the app and a link to the
  Applications folder.

## What the menu does

| Item | What it runs |
|---|---|
| "Lucille Host *version*" | `--version` |
| "Paused" or "Not paused" | Nothing: it reads the pause flag the host keeps (see below). |
| Pause Lucille / Resume Lucille | `pause` / `resume` |
| Abilities | `ops list`. Each operation has a checkmark when it is enabled. Choosing one runs `ops enable` or `ops disable`. |
| Add a Folder… | Opens the standard Open panel, suggests a short name (the alias) from the folder's name, lets the user change it, then runs `roots add <alias> <path>`. If the host refuses because the folder is the home folder, holds it, or is `/`, the app asks the user, and adds `--allow-home-or-root` only if the user confirms. |
| Folders | `roots list`. Each folder has a **Remove** item, which runs `roots remove <alias>`. |
| Connect to Lucille (coming soon) | Nothing. It is disabled. |
| Quit Lucille Host | Quits the app. |

The menu reads the host's state again each time it opens. When a verb fails, the app shows the host's
own reason in an alert, without the program name or a usage summary.

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
- Each run has a 30-second limit, after which the app stops it.
- The app writes none of the host's files. It reads one, the pause flag at
  `~/Library/Application Support/lucille-host/default/state/paused`, to show whether the host is paused.
- The app opens no network connection and listens on nothing.

All of this is in [`App/HostCLI.swift`](App/HostCLI.swift). It is the only Swift file that starts a
process, and `tests/test_macos_build.py` checks that.

## Building it on a Mac

You need an Apple silicon Mac with macOS 15 or later, Xcode or the Xcode command line tools, `uv`, and
network access to GitHub and PyPI. From the repository's root:

```sh
uv run --no-project python macos/build_app.py build
```

The app and the disk image land in `build/macos/`, which git ignores. Then run the checks CI runs:

```sh
for check in host swift relocate links signature launch gatekeeper dmg; do
  uv run --no-project python macos/check_app.py "$check"
done
```

`uv run --no-project python macos/build_app.py plan` runs on any system. It checks the pins, and writes
the `Info.plist` the app would get, without running any other program.

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

- `codesign --verify --deep --strict` passes after the host has run from the bundle, so the runs wrote
  nothing into it;
- every Mach-O file has the hardened runtime flag;
- the dependencies' native code (cffi, cryptography, and keyring's macOS backend) loads under the
  hardened runtime with no entitlement.

A signature that macOS trusts needs a Developer ID certificate and notarization. This build does
neither.

## The disk image from CI

The `macos-app` workflow uploads the disk image as the artifact `lucille-host-macos-arm64-dmg` and keeps
it for 7 days. **It is ad hoc signed and not notarized.** When it is downloaded with a browser, macOS
refuses to open the app normally and says it cannot check it for malicious software. It is for people
developing the app, on their own Mac. Do not give it to anyone else, and do not tell anyone to work
around the warning.

## Not done yet

- **Developer ID signing and notarization.** They need the project's Apple Developer account, which does
  not exist yet. Until then, no build is fit for other people.
- **Pairing.** The host has no `pair` verb yet, so "Connect to Lucille" is disabled.
- **Running in the background.** The host has no long-running loop yet, so the app starts nothing that
  keeps running, and it does not open at login.
- **Updates.** There are none. A new version is a new disk image.
- **Uninstall.** Dragging the app to the Trash removes it. The host's own folder,
  `~/Library/Application Support/lucille-host`, stays.
- **Intel Macs.** The locked `cryptography` publishes no Intel macOS wheel, so the app is Apple silicon only.
- An app icon, a plain sentence for each ability, an activity view, and a machine-readable output from
  the host's command line, which the app would read instead of its text.

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
- **`.pyc` files are hash-checked**, so a copied app with new file times still uses them, and Python
  never needs to write one.
- **`PATH` is `/usr/bin:/bin`** for the host's command line.
- **The disk image is a plain `hdiutil` image**, with no background picture or window layout.
