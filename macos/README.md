# Capsule, by Lucille

Capsule is the Mac app for the host. It lets Lucille use only the folders you choose on this Mac, and only
for the abilities you turn on. Lucille can also see which abilities are on, and the names you gave your
folders. It is a menu bar app. Inside it runs the host program, `lucille-host`, which keeps its own name,
command line and files.

## Status: a first build, not a release

This folder holds the Swift sources of a first, small version of Capsule; the scripts that build and check
it are in [`tools/macos/`](../tools/macos/). It is not a release and it is not ready for anyone who is not
developing it. It cannot connect to Lucille: the host's command line can pair (`lucille-host pair`), but
the app has no screen for it yet. Its disk image is not notarized, so macOS refuses to open it normally
(see [The disk image from CI](#the-disk-image-from-ci)).

The design of the full app is [decisions/0002-macos-app.md](../decisions/0002-macos-app.md). It says what this
build is, and what is planned after it.

## What this builds

- **`Capsule.app`**, a menu bar app for Apple silicon Macs with macOS 15 or later. It has no main
  window and no Dock icon. Its Swift sources are in [`App/`](App/).
- Inside it, at `Contents/Resources/runtime`, a Python 3.13 runtime from
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone), pinned in
  [`tools/macos/pins.toml`](../tools/macos/pins.toml) by its URL and its sha256. The build refuses any
  other file. It uninstalls the runtime's own pip, removes what
  [`tools/macos/prune.txt`](../tools/macos/prune.txt) lists (see
  [What the build removes from the runtime](#what-the-build-removes-from-the-runtime)), and renames the
  interpreter `lucille-host-runtime`, so that Activity Monitor and macOS's prompts name it rather than
  `python3.13` (not yet seen on a Mac).
- Installed into that runtime by `uv`, with hashes required, wheels only and no dependency resolved: the
  exact dependency wheels `uv.lock` pins for this Mac, each downloaded from the lock's URL and refused
  unless its sha256 is the lock's; then the host's wheel, built from this checkout, by the sha256 the build
  is given. The host package is the same code as in `src/lucille_host/`, unchanged. The build deletes what
  the installer writes that no wheel holds (its `.lock` file, the console scripts and each
  `uv_cache.json`), then refuses to go on unless every file in `site-packages` is byte for byte a file of
  its wheel, apart from the files an installer writes in each `*.dist-info` folder.
- No `.pyc` file anywhere: nothing is precompiled, and the app runs Python with `-B`, so it writes none
  either. Each start compiles the modules it imports in memory. CI measures how long that takes
  ([decisions/0002](../decisions/0002-macos-app.md) §6).
- The host's wheel is built with the build backend that
  [`tools/macos/build-constraints.txt`](../tools/macos/build-constraints.txt) pins by hash
  (`uv build --build-constraints … --require-hashes`), with no uv configuration file and no `UV_*`
  variable, either of which could turn build isolation off. Those are the versions `pyproject.toml`'s
  `[build-system]` requires, and a test checks that the two agree.
- `Contents/Resources/bin/lucille-host`, a three-line `sh` wrapper for Terminal users. It runs the bundled
  host as the app does, and finds the bundle from its own real path, so a link to it from a folder on
  `PATH` works too.
- `Contents/Resources/licenses/`, the licence texts of the Python runtime and of every installed
  distribution, one folder each.
- `Contents/Resources/MANIFEST.json`, which records what went into the app: its name, identifiers and
  version strings; the commit, the build number or tag, the Xcode version and the `swiftc` version; the
  host's exact version and its wheel's name and sha256; the runtime's file name, URL and sha256, and what
  the build removed from it; and every installed wheel's name, version, file name, URL and sha256. It
  holds no time and no signature.
- `Capsule-unsigned-macOS-arm64.tar.gz`, the app before it is signed, as a tarball whose bytes depend only
  on the files: entries sorted, each with the commit's time and owner 0. CI builds it twice and compares
  the two.
- **`Capsule-<version>-<commit>-adhoc-macOS-arm64.dmg`**, a disk image whose volume is named Capsule. It holds
  the app and a link to the Applications folder. The file name says the signature is ad hoc and names
  the commit, so the image cannot be taken for a release asset.

## What the app does when it opens

The app has no window to show, so when it opens it says where it is: "Capsule is in your menu bar". The
note first says what Capsule is for, in the two sentences this page opens with, and then where to click. It
says so again whenever it is opened while it runs, for someone who could not find the icon. The note
offers **Quit Capsule** too, because on a crowded menu bar, or beside a notch, macOS hides icons that do not
fit, and then the menu cannot be reached.

The menu bar item is named "Capsule", or "Capsule, paused", for VoiceOver.

The `Info.plist` holds the same sentences under each of the six keys macOS reads before it asks about
Documents, Desktop, Downloads, removable and network volumes, and file providers. macOS is meant to show
them under its question. Whether it does, and whether that question names Capsule at all, is not tested yet
(see [What CI does not check](#what-ci-does-not-check)).

## What the menu does

| Item | What it runs |
|---|---|
| "Capsule *version*" | `--version` |
| "Not connected to Lucille" | Nothing. The app cannot pair yet, so it always says this, even if the host's command line has paired this Mac. When the host is paused, the line reads "Not connected to Lucille · Paused", from the pause flag the host keeps (see below), a line under it says that Lucille's requests are turned away until you resume, and the icon changes. |
| Pause Lucille / Resume Lucille | `pause` / `resume` |
| Abilities | `ops list`. Each ability shows in a few plain words, such as "Check a file's fingerprint", with a checkmark when it is enabled, and a sentence under it says what Lucille gets: "Lucille gets a code that identifies one file's exact contents, not the contents." Choosing one runs `ops enable` or `ops disable`. `host.sleep`, which is for testing, is not listed. Nor is `host.describe`; while it is on, the menu says what it shares once connected. |
| Add a Folder… | See below. |
| Folders | `roots list`. Each folder has a **Remove** item, which runs `roots remove <alias>`. |
| Connect to Lucille (coming soon) | Nothing. It is disabled. |
| Quit Capsule | Quits the app. |

**Add a Folder…** opens the standard Open panel, whose button reads "Allow Capsule to Use This Folder".

The app never adds the whole disk: `/`, the Data volume (`/System/Volumes/Data`), which holds every
user's files, and the two folders between them, `/System` and `/System/Volumes`, which hold it. It
knows each by its path, as chosen or with its links followed and in any case, or as the same folder by
device and inode. If the folder chosen is one of them, the app says that it never lets Lucille use the
whole disk, with two buttons: **Choose a Smaller Folder**, the default, which opens the Open panel
again, and **Cancel**. Nothing else is offered, and the app never passes `--allow-home-or-root` for
it. The command line still adds any of them with that flag.

If the folder chosen is the home folder or holds it, the app first says which, and what is inside, with
**Choose a Smaller Folder** as the default button, and **Add Anyway**.

Then the app asks for the name Lucille will see, suggesting one made from the folder's
name that no other folder uses: written in Latin letters where macOS can, so that `日本語` gives
`ri-ben-yu`, with `folder-` put first when it starts with a digit (`2026 Taxes` gives
`folder-2026-taxes`), and `-2`, `-3` added when it is taken. A name the host would refuse is turned into
one it accepts, and shown again before anything is added.

The same dialog says what macOS may ask next. For a folder in Documents, Desktop or Downloads, or on
another drive, it says that macOS may ask whether Capsule can access it, and to click Allow. For a
folder in iCloud Drive or another cloud folder, it says that such folders may not work yet.

Then the app runs `roots add <alias> <path>`, with `--allow-home-or-root` only if the user chose Add
Anyway, and never for the whole disk. If that takes more than a moment (a network drive, or a question
from macOS), a small window with a **Cancel** button shows; Cancel stops the run. If the host refuses
the folder as the home folder or one that holds it, where the app could not tell from the path, the
app asks then, unless the folder is the whole disk. On success the app says that once this Mac is
connected, Lucille can use the folder for the abilities the user turns on. If the host found no folder
there but the app can see one, or macOS refuses the app too, the app says that macOS is blocking Capsule
from the folder, and where in System Settings to allow it.

The menu reads the host's state again each time it opens. When the host cannot be read, the menu says
so ("Couldn't read your folders") instead of showing an empty list, and a short line at the top names the
problem; choosing it shows the details. When a verb fails, the app says why in a plain sentence for each
refusal it knows from the host's command line (a name already in use, the 64-folder limit, the host's own
folder, an unreadable settings file, and the others in `App/HostText.swift`), and shows the host's own
words as a detail. A refusal it does not know reads "Something went wrong in Capsule's helper program",
with the host's words as the detail.

Pause, Resume, the ability switches and Remove show the same window with a **Cancel** button when they
take more than a moment. A run the app gave up on (see below) may still hold the lock the host takes to
change its settings, and then every change to folders or abilities waits for it. While such a run is
still going, the menu says that a folder check is stuck, and a change that times out or is cancelled
says so too, rather than blaming its own folder.

## How the app talks to the host

- The app drives the host only through the host's command line. The one program it starts is the
  interpreter inside the bundle, as
  `Contents/Resources/runtime/bin/lucille-host-runtime -I -B -X utf8 -m lucille_host <verb> ...`:
  - `-I` is isolated mode: no `PYTHON*` variables, no user site-packages, and no current folder on the
    import path.
  - `-B` writes no `.pyc` file, so nothing is written inside the signed app.
  - `-X utf8` makes text UTF-8 whatever the locale.
- The arguments go as an argv array, never through a shell.
- The environment is fixed. It holds `HOME`, `USER`, `LOGNAME`, `TMPDIR`, `PATH=/usr/bin:/bin` and
  `LANG`, and nothing else from the app's own environment. The working folder is `/`.
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

- The Mac lint, [`tools/guards/macos_lint.py`](../tools/guards/macos_lint.py), reads the Swift sources
  ([VERIFYING.md](../VERIFYING.md) lists its rules). It fails if any file but `HostCLI.swift` names
  Foundation's `Process`; if `HostCLI.swift` does not write the bundled runtime's path and the arguments
  above; and if any file uses one of the other ways to start a process it lists (`posix_spawn`, `fork`,
  `exec`, `NSWorkspace`, ...) or one of the ways to reach the network it lists (`URLSession`, Network's
  types, sockets, `URL(string:)`, reading `contentsOf:` a URL, ...). `tests/test_macos_build.py` pins the
  lines where `HostCLI.swift` gives that path and those arguments to the `Process`, and fails if a file
  imports anything but Foundation, AppKit and SwiftUI (Foundation and the C library alone, for the files
  the self-test runs).
- `check_app.py links` reads what the built app's executable imports (`nm -u`). It fails on the C
  functions that start a process or open a socket, on the classes that do either, and on a link to
  Network, CFNetwork or WebKit.

Both match names. Code written to get past them can: through the Objective-C runtime, say, which looks a
class up by a string. Review of every change is what stands behind them.

## Building it on a Mac

You need an Apple silicon Mac with macOS 15 or later, Xcode 16.4 (the version `tools/macos/pins.toml`
names; the build refuses any other), `uv`, and network access to GitHub and PyPI. The build uses
`/Applications/Xcode_16.4.app`, where GitHub's images keep it, unless `DEVELOPER_DIR` names another Xcode's
`Contents/Developer` folder. From the repository's root, build the host's wheel, then the app from that
wheel and its sha256:

```sh
uv run --no-project python tools/macos/build_app.py wheel --out build/macos/host-wheel
# It prints the wheel's path and its sha256; pass both on.
uv run --no-project python tools/macos/build_app.py build \
  --host-wheel build/macos/host-wheel/lucille_host-<version>-py3-none-any.whl \
  --host-wheel-sha256 <its sha256> --build-number 1
```

A build takes exactly one of `--build-number N`, which becomes `CFBundleVersion` (CI passes its run
number), and `--tag vX.Y.Z`, for a build from that tag, whose version must then be `X.Y.Z` with no
suffix. `assemble`, with the same arguments, stops before signing, and leaves the unsigned app and its
tarball.

The app, its tarball and the disk image land in `build/macos/`, which git ignores. Then run the checks CI
runs, in the order it runs them:

```sh
check() { uv run --no-project python tools/macos/check_app.py "$@"; }
check bundle
check record
check version --build-number 1
for c in links signature wrapper; do check "$c"; done
chmod -R a-w build/macos/Capsule.app
for c in host environment readonly swift relocate launch ui gatekeeper dmg startup signature; do
  check "$c"
done
chmod -R u+w build/macos/Capsule.app   # so that the next build can replace it
```

On your own Mac, `ui` needs your terminal to have Accessibility permission (System Settings, Privacy &
Security, Accessibility), and it takes over the screen for about two minutes. It changes your own host's
settings while it runs (it pauses, switches an ability, adds folders) and puts them back at the end.
Without that permission it drives nothing and says so; with `--require-driven`, as the `macos-app` job runs
it, that is a failure. `library-snapshot` and `library-diff`, which CI runs around `launch` and `ui`, read
your own `~/Library` and print what changed, which on a Mac in use is a lot. `suite --copy-to DIR` runs the
host's test suite on a copy of the app's runtime in `DIR`, with the test dependencies installed into the
copy only.

`uv run --no-project python tools/macos/build_app.py plan --build-number 1` runs on any system. It checks
the pins, the build constraints and the prune list, and writes the `Info.plist` the app would get, without
running any other program.

[`build_app.py`](../tools/macos/build_app.py) says what each build step does, and
[`check_app.py`](../tools/macos/check_app.py) says what each check proves.

## What the build removes from the runtime

The app never uses these, so the build removes them from python-build-standalone's files:

- pip, uninstalled whole by its own `RECORD` before anything is installed, which removes `bin/pip`,
  `bin/pip3` and `bin/pip3.13` too. It is the only `*.dist-info` folder the build removes;
- every other program in `bin/` but the interpreter, `python3.13`, which the build renames
  `lucille-host-runtime`;
- `include/` and `share/`;
- everything in `lib/` except `python3.13/`: `libpython3.13.dylib`, since the interpreter links Python
  statically, the Tcl/Tk libraries and packages, and the pkg-config files;
- from the standard library, `tkinter`, `_tkinter`, `idlelib`, `turtle`, `turtledemo`, `ensurepip` and
  `config-3.13-darwin`, and the three stale `.pyc` files in `encodings/__pycache__`;
- `site-packages/README.txt`.

[`tools/macos/prune.txt`](../tools/macos/prune.txt) lists all of these but pip, one path per line, each
group with its reason. The build refuses an entry the pinned runtime does not hold. It does not delete
what the list misses: it refuses a runtime that is not then exactly the interpreter and `lib/python3.13/`,
with an empty `site-packages`. CI runs the host's whole test suite on a copy of the trimmed runtime, so a
module the host needs cannot go missing without a test failing.

## Signing

The build signs every Mach-O file in the bundle, innermost first, then the interpreter, and then the
bundle. The signature is ad hoc (`codesign -s -`), with the hardened runtime, as
`tools/macos/pins.toml` asks, and no entitlements. It never uses `--deep` to sign. Each file gets its own
identifier: the interpreter `com.lucille-ai.capsule.runtime`, and every other one
`com.lucille-ai.capsule.` followed by its path in the bundle. CI checks the following on every build:

- the app and every Mach-O file in it were signed ad hoc by the build, not only by the linker, with the
  identifier the build gave each and no Team ID, and the Mach-O files are exactly the expected ones;
- `codesign --verify --deep --strict` passes on the app after the host has run from the bundle, so the
  runs wrote nothing into it;
- `codesign --verify --strict` passes on each Mach-O file in the bundle on its own. The interpreter and
  the dependencies' native libraries are under `Contents/Resources`, where the bundle's signature may
  seal them as plain files and not check their own signatures, so this is checked separately;
- every Mach-O file has the hardened runtime flag, exactly when the pins ask for it;
- the dependencies' native code (cffi, cryptography, and keyring's macOS backend) loads under the
  hardened runtime with no entitlement: in the `macos-app` job on macOS 15, and in the smoke jobs on
  macOS 26 and 27.

A signature that macOS trusts needs a Developer ID certificate and notarization. This build does
neither. Whether the native code still loads with no entitlement under a Developer ID signature is
not tested yet, and nor is whether Apple's notary service accepts code under `Contents/Resources`.
There is reason to expect that it does: BeeWare's Briefcase signs the Mach-O files it puts under
`Contents/Resources` the same way, inside out, and its apps are notarized.

## What CI checks

The workflow `ci` (`.github/workflows/ci.yml`) builds and checks the app on every pull request and every
push to `main`. Its job `macos-app` builds the app on a GitHub-hosted Apple silicon runner (`macos-15`) and
runs each check in [`check_app.py`](../tools/macos/check_app.py). Its jobs `macos-smoke`, on `macos-26` and
on `xcode-27` (macOS 27), take the disk image and the compiled Swift self-test that `macos-app` built,
refuse them unless their sha256 is the one `macos-app` gave, and run the checks marked "both" on them; they
build nothing. The workflow's token can only read the repository, and the Mac jobs use no secret. A check
that finds a problem fails its job.

| Check | Where | What it shows |
|---|---|---|
| `bundle` | both | No symbolic link, no `.pyc` and no `__pycache__` in the bundle. Its Mach-O files are exactly the expected five: the app's executable, the interpreter, `lib-dynload`'s `_dbm`, and the two wheel extensions. The app and each of them is signed ad hoc by the build, not only by the linker, with no Team ID and the identifier the build gave it. |
| `record` | `macos-app` | Each wheel `MANIFEST.json` names is one `uv.lock` pins (name, version, file, URL and sha256), and the host wheel's sha256 is the one the wheel step output. Each, the host's included, is found again and hashed; the installed distributions are exactly those; every file in `site-packages` is byte for byte a file of its wheel, apart from the installer's own files in each `*.dist-info` folder. Each Mach-O file is compared with its wheel's after the signature is removed from both, and that comparison is reported in the job summary, not failed. |
| `version` | `macos-app` | `Info.plist`'s two version strings follow the version rule, and `MANIFEST.json`, the bundled host's `--version` and the pins agree with them. |
| `links` | `macos-app` | Every library a Mach-O file loads is macOS's own (`/usr/lib/`, `/System/Library/`), or resolves to a file that exists inside the bundle. `@loader_path`, `@executable_path` and `@rpath` are resolved as dyld resolves them, `..` included, so a path that climbs out of the bundle, and a library the build removed, both fail. Every run path must resolve inside the bundle. The log shows where each library and run path resolves. No Mach-O file needs a newer macOS than the app's minimum, and no file holds the build folder's path. The app's executable imports nothing on the lists above. |
| `signature` | both | The signature is valid, on the app and on each Mach-O file in it; each is signed ad hoc as `bundle` says, with the hardened runtime exactly when the pins ask for it, and nothing has entitlements. It runs again as the last check, so a run that wrote into the bundle fails it. |
| `wrapper` | both | On a writable copy of the app, the wrapper runs the host through a link to it from another folder, and never a runtime beside the link. Neither it nor the interpreter run with the app's flags writes any bytecode; the same run without `-B` does, so the check can see one. |
| `host` | both | On the bundle made read-only (`chmod -R a-w`), the interpreter runs the host with the app's flags and nonsense in `PYTHONHOME` and `PYTHONPATH`: `--version`, through the wrapper too, `ops list`, a `roots` round trip, `pause` and `resume`, and the refusal of a home folder. The dependencies' native code loads. |
| `environment` | both | A module or startup file planted where `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, the user site, `PYTHONUSERBASE` and the working folder would load it never loads with the app's flags. For each, a control run without `-I` must load it, or the check fails. `PYTHONSTARTUP` is read only by an interactive interpreter, so no run the app makes could load it, and its control runs `-i`. No `.pth` file is missing from the installed `RECORD` files. |
| `readonly` | both | The bundle cannot be written; `--version`, `status` and `status --json` work from it, and the wrapper too; no file in it changed and no bytecode appeared; then its signature still verifies. |
| `swift` | both | The app's own Swift code that runs the command line and reads its output works against the real command line, through the same calls the menu makes: the folder name suggestions and corrections, which folders are the whole disk (a link to one included), that the command line the app builds for them never has `--allow-home-or-root` and that the host then adds nothing, the plain sentence for each refusal, the time limit and Cancel. `macos-app` compiles the self-test once, with the pinned Xcode, and the smoke jobs run that same program. |
| `relocate` | `macos-app` | A copy of the app in another folder, with a space in its path, passes `host` and `swift`. |
| `launch` | both | The app opens the way Finder opens it, and is still running 10 seconds later. |
| `ui` | both | Driven through System Events as a user would: the app says what it is for and where its menu is when it opens, and again when it is opened while it runs; the menu bar item's title or description, which VoiceOver reads as its name, is "Capsule" (its tooltip does not count); the menu shows the version, "Not connected to Lucille" and every item, and lists abilities in plain words, each with its sentence on what Lucille gets; Pause Lucille and Resume Lucille change the host's pause flag, the status line and the menu bar item's name; an ability switches on, shows a checkmark, and switches off; Add a Folder… goes through the Open panel and the name dialog, where a typed name the host would refuse comes back corrected, and adds the folder; Remove in the Folders submenu removes it; the home folder brings up the question first, Choose a Smaller Folder goes back to the Open panel, and Add Anyway adds it; the Data volume, then `/` through a link to it, each bring up the note that refuses the whole disk, with Choose a Smaller Folder, which goes back to the Open panel, and Cancel, and no button that adds the folder, and neither is added (that the note has no other button is checked in its source by the build tests). Each step is checked against what the host's command line then reports. In `macos-app` (`--require-driven`), a machine where System Events cannot drive the app fails the check; the smoke jobs record whether it could, and where it could, a driven step that fails fails the check there too. Screenshots are kept in the job's logs artifact. |
| `library-snapshot`, `library-diff` | both | `~/Library` is walked before `launch` and after `ui`. A new, changed or removed path whose name holds the bundle identifier, the app's name or "lucille", and that [`tools/macos/library-allowlist.txt`](../tools/macos/library-allowlist.txt) does not allow, fails the check; every other change is printed. The host's own state folder must be among the changes (in the smoke jobs, the check makes those writes itself first), or the check fails. It skips `~/Library/Developer` and `~/Library/Android`, and cannot see a write to a path whose name says nothing of the app. The snapshots are never uploaded. |
| `gatekeeper` | `macos-app` | Prints `spctl`'s verdict. It is "rejected", as expected for an ad hoc signature, and does not fail the build. |
| `dmg` | both | Exactly one disk image was built, its name ends `-adhoc-macOS-arm64.dmg`, and it verifies, mounts, and holds the app, with a valid signature, and a link to `/Applications`. |
| `startup` | `macos-app` | The median and the fastest of ten runs of `--version` and of `status --json` with no `.pyc`, as shipped; then, on a writable copy, with `checked-hash` files for the standard library, then for everything. Written to the job summary; not a gate. |
| `suite` | `macos-app` | The host's whole test suite passes on a copy of the app's runtime, with the test dependencies installed into the copy only, by hash, and the host's wheel and agent-switchboard installed again by `MANIFEST.json`'s sha256, so that their commands sit beside the copy's interpreter. |
| a second build | `macos-app` | The unsigned app is built again, in another folder, and the two tarballs are compared by sha256. The job summary says SAME, DIFFERENT or FAILED, with both digests. Not a gate. |

## What CI does not check

CI adds its folders under the home folder, where macOS asks nothing. These need a Mac and a person, and
none has been done yet:

- **A folder in Documents, Desktop or Downloads.** Whether macOS asks at all after the Open panel,
  whether its question names Capsule rather than `lucille-host-runtime` and shows Capsule's sentence on
  what it is for, and whether the folder is added after Allow. After Don't Allow, whether the app says
  that macOS is blocking it, and whether System Settings, Privacy & Security, Files & Folders then shows a
  switch under Capsule.
- **A folder on an external or network drive, one in iCloud Drive, and one from another file provider.**
- **A stalled network drive.** Whether Cancel works, and whether the menu then says a folder check is
  stuck.
- **VoiceOver.** Whether it reads the menu bar item as "Capsule", and "Capsule, paused".
- **A full menu bar, or one beside a notch,** where macOS hides the icon.

## The disk image from CI

The workflow `ci` uploads the disk image only from a run started by hand (its `workflow_dispatch` trigger,
from the Actions tab), and only when every check of `macos-app` passed, as the artifact
`capsule-adhoc-macos-arm64-dmg-workflow_dispatch-<commit>`, kept for 3 days. A pull request or a push
uploads no app. Every run uploads its logs and screenshots, as `capsule-macos-app-logs` and
`capsule-macos-smoke-<runner>-logs`, kept for 7 days.

The smoke jobs get the disk image through GitHub's Actions cache, not as an artifact: only a workflow run
can restore a cache entry, and nobody can download one (a reading of GitHub's documentation, not tested).
A pull request's run, a fork's included, can restore what a run on `main` put there; it could equally
build the same app from the same source ([decisions/0002](../decisions/0002-macos-app.md) §18).

A pull request opened from a fork runs the fork's copy of the workflow, as well as the fork's build code,
and that copy can upload anything, under any name. So an artifact on a fork's pull request run is the
fork's build. The repository's Actions setting "Approval for running fork pull request workflows from
contributors", at its strictest, makes every such run wait for a maintainer. It is a setting, which no
file here can enforce or show. Take a disk image only from a run of this repository started by hand; the
artifact's name says which event and which commit built it.

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
- **Pairing.** The host's command line can pair (`lucille-host pair`), but the app has no screen for it
  yet, so "Connect to Lucille" is disabled.
- **Running in the background.** The host has a long-running loop (`lucille-host run`), but the app
  starts nothing that keeps running, and it does not open at login.
- **ripgrep.** The design bundles it as `Contents/Helpers/rg`, with its licence. The pins hold an empty
  ripgrep commit until then.
- **Updates.** There are none. A new version is a new disk image.
- **Uninstall.** Dragging the app to the Trash removes it. The host's own folder,
  `~/Library/Application Support/lucille-host`, stays.
- **Intel Macs.** The locked `cryptography` publishes no Intel macOS wheel, so the app is Apple silicon only.
- **A Welcome window and a main window.** The design has a Welcome window that leads through
  connecting, choosing a folder and turning on an ability, and a main window with the status, folders,
  abilities, activity and settings. This build has the menu, and a note that says where it is.
- **An Open Privacy Settings button.** When macOS blocks a folder, the app says where to go in System
  Settings, but cannot open it: that needs `NSWorkspace`, which the checks above keep out of the app.
- An app icon, an activity view, and the app's use of the host's machine-readable output, which it would
  read instead of its text.

## Choices made for the first build

The design ([decisions/0002-macos-app.md](../decisions/0002-macos-app.md)) may change each of these.

- **The app runs python-build-standalone's own interpreter, renamed `lucille-host-runtime`.** A small
  native launcher that embeds Python, so that the bundle holds no general-purpose interpreter, is an
  option the design has not decided.
- **Python lives in `Contents/Resources/runtime`.** If notarization objects to code there, see "Not done
  yet" for the two ways out.
- **The app's working name is Capsule, with the bundle identifier `com.lucille-ai.capsule`.** The code
  takes both from `tools/macos/pins.toml`: `build_app.py` writes them into `Info.plist`, the Swift code
  reads the name back from there (`HostText.appName`), and `check_app.py` reads it from the pins. Outside
  the app, as in the self-test, the Swift code says "the app" instead, so the `ui` check fails if the app
  stops reading its name. A new name also changes the three artifact names in `.github/workflows/ci.yml`
  and the values `tests/test_macos_build.py` pins; those tests fail until all of them agree with the pins.
  The name may change before the first signed build. Only the app has the name: the host program inside
  is still `lucille-host`, with its folder at `~/Library/Application Support/lucille-host`. The
  identifiers must not change after the first signed release, because changing them makes macOS ask the
  user again about every folder and Keychain item.
- **`CFBundleShortVersionString` holds the version's release numbers** (`0.1.0` for `0.1.0.dev0`), because
  Apple allows only three numbers there, and `CFBundleVersion` the build number (CI's run number). The
  exact version is in `MANIFEST.json` and in the menu. A build from a `v*` tag needs a version with no
  suffix, which both keys then hold.
- **macOS 15 or later, Apple silicon only.**
- **The app reads the host's text output and the pause flag.** The host's command line now has a
  versioned JSON output ([spec/CLI.md](../spec/CLI.md)), and the design adds an event stream from the
  running host; until the app uses them, `App/HostText.swift` follows the text, and the Swift self-test
  checks it against the real command line on every build.
- **The words for each ability are in the app** (`App/HostText.swift`). The host's catalog now carries
  its own words for each operation (`ops list --json`), so that every app can show the same reviewed
  words; the app does not use them yet. A test fails when the catalog has an operation the app neither
  names nor leaves out of the menu.
- **The app asks about the home folder before the user names it**, when it can tell from the path (the
  home folder, or a folder above it). The host still decides; when it refuses a folder the app did not
  recognise, such as `/System/Volumes/Data/Users`, which holds the home folder through the Data volume,
  the app asks then.
- **The app refuses `/System` and `/System/Volumes` too.** The design refuses `/` and the Data volume
  outright. The two folders between them hold the Data volume, so the first build refuses them as well,
  where the design would ask about them as folders that hold the home folder.
- **`PATH` is `/usr/bin:/bin` and the working folder is `/`** for the host's command line. The design
  runs it with `PATH=/var/empty`, in the host's own state folder.
- **The `Info.plist`'s folder usage text is the two sentences on what Capsule is for** that this page
  opens with, not the shorter sentence the design proposes.
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
