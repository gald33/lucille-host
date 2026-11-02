# Capsule, by Lucille

Capsule is the Mac app for the host. It lets Lucille use only the folders you choose on this Mac, and only
for the abilities you turn on. Lucille can also see which abilities are on, and the names you gave your
folders. It is a menu bar app. Inside it runs the host program, `lucille-host`, which keeps its own name,
command line and files.

## Status: a first build, not a release

This folder holds the Swift sources of a first, small version of Capsule; the scripts that build and check
it are in [`tools/macos/`](../tools/macos/). It is not a release and it is not ready for anyone who is not
developing it. It can connect this Mac to Lucille: you paste the code Lucille's page shows, then compare
the number the app shows with the one on Lucille's page. While it is open, it keeps the host running.
This way of connecting is only for the repository owner's own Mac, because anyone can make a code that
passes the host's checks ([decisions/0002](../decisions/0002-macos-app.md) §11.1). Builds stay ad hoc
signed, for that Mac. The disk image is not notarized, so macOS refuses to open it normally (see [The
disk image from CI](#the-disk-image-from-ci)).

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
  host as the app does, with the app's flags and on the app's profile, `mac-app` (a `--profile` you give it
  replaces that one), and finds the bundle from its own real path, so a link to it from a folder on `PATH`
  works too.
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

When it opens, the app also reads the host's `status`. If this Mac is connected, it starts the host's
`run` at once, paused or not, since `run` keeps the pause itself (see [Keeping the host
running](#keeping-the-host-running)). Once the note about the menu bar is closed, the app says so if the
host is paused, and only **Resume** resumes it. Then it picks up a connection left half made: it opens
the pairing window again, or says why that connection ended.

The menu bar item is named "Capsule", or "Capsule, paused", for VoiceOver. While something needs
attention, the icon is a warning sign; the name stays the same.

The `Info.plist` holds the same sentences under each of the six keys macOS reads before it asks about
Documents, Desktop, Downloads, removable and network volumes, and file providers. macOS is meant to show
them under its question. Whether it does, and whether that question names Capsule at all, is not tested yet
(see [What CI does not check](#what-ci-does-not-check)).

## What the menu does

| Item | What it runs |
|---|---|
| "Capsule *version*" | `--version` |
| The status line | Nothing. It says what `status --json` and `run`'s events last said (see the table below). While the host is paused, " · Paused" is added. |
| A line under it, when there is one | Nothing until it is chosen. It says what needs attention, or why this Mac is no longer connected. Choosing it shows a note that says what to do, with **Try Again** or **Disconnect from Lucille…** where one helps, and **Show Details** for the host's own words or the last lines `run` wrote on standard error. |
| Pause Lucille / Resume Lucille | `pause --json` / `resume --json`. While the host is paused, a line says that Lucille's requests are turned away until you resume, and the icon changes. |
| Abilities | `ops list`. Each ability shows in a few plain words, such as "Check a file's fingerprint", with a checkmark when it is enabled, and a sentence under it says what Lucille gets: "Lucille gets a code that identifies one file's exact contents, not the contents." Choosing one runs `ops enable` or `ops disable`. `host.sleep`, which is for testing, is not listed. Nor is `host.describe`; while it is on, the menu says what it shares: "Lucille sees which abilities are on and your folders' names", once connected. |
| Add a Folder… | See below. |
| Folders | `roots list`. Each folder has a **Remove** item, which runs `roots remove <alias>`. |
| Connect to Lucille… | Opens the pairing window (see [Connecting to Lucille](#connecting-to-lucille)). Shown while this Mac has no connection. |
| Finish Connecting… | Opens the pairing window again, on a connection being set up whose window was closed. |
| Stop Connecting… | Asks first, then runs `pair reject --json`. Shown while a connection is being set up, or once it expired or can no longer be finished. |
| Disconnect from Lucille… | Asks first, then lets `run` finish and stop, then runs `forget --json` (see [Disconnect](#disconnect)). Shown while this Mac is connected. |
| Quit Capsule | Quits the app. While `run` runs, it asks first (see [Quit, log out, and paused](#quit-log-out-and-paused)). |

The status line is one of these:

| Line | When |
|---|---|
| Not connected to Lucille | This Mac has no connection, or its connection ended. |
| Connecting to Lucille… | A connection is being set up, or this Mac confirmed it and Lucille has not yet. |
| Connected, waiting for Lucille | `run` runs, and reads no work room yet. |
| Connected to Lucille | `run` reads a work room Lucille offered. |
| Capsule's helper program stopped. Starting it again… | `run` stopped when it should not have, and the app waits before it starts it again. |
| Finishing a task from before… | Another `run` of the app's profile is still finishing, most often the app's own from before it was opened again. |
| Finishing the current task… | A quit or a Disconnect waits for `run` to finish. |
| Needs attention | The line under it says why. |

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
app asks then, unless the folder is the whole disk. On success, while this Mac is connected or being
connected, the app says that Lucille can use the folder for the abilities the user turns on. With no
connection, it says that Lucille can use the folder only once it is added again after connecting,
because a new connection starts with no folders. If the host found no folder
there but the app can see one, or macOS refuses the app too, the app says that macOS is blocking Capsule
from the folder, and where in System Settings to allow it.

The menu reads the version, the abilities and the folders again each time it opens. It reads `status`
at launch, after each step of connecting, and when the menu opens more than 10 seconds after the last
reading while no child of the app runs: `status` opens the Keychain, where macOS may ask first. When the
host cannot be read, the menu says
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

## Connecting to Lucille

The app connects on [decisions/0001](../decisions/0001-host-design.md)'s gate: the Mac shows the
number, and you confirm only if Lucille's page shows the same number. The maintainer decided on
2026-11-02 to build this now. The typed number and the Connect flow of decisions/0002 (§11.2 and §11.4)
are proposals, and are not built.

1. **Connect to Lucille…** opens the pairing window. It says: "In Lucille, open Computers and add this
   computer. Copy the code Lucille shows, then paste it below." You paste with Command-V, or Control-click
   the box and choose Paste. The app never reads the pasteboard itself: the window passes Command-V, C,
   X, A and Z on to the field being edited, as an Edit menu would. If folders were added, or an ability
   other than `host.describe` is on, the window also says that connecting sets them aside.
2. **Connect** is on only once the text looks like a code: `swbp1_`, then 100 to 8186 characters of
   base64url (at most 8192 in all), with spaces and line breaks taken out. The app writes the code on the standard input of
   `pair --code-stdin --json`, never on its command line, and then clears the field. The host does every
   real check. Each refusal is said in a plain sentence, with the host's words as details.
3. Until Lucille answers, the window says "Waiting for Lucille…". Its **Cancel** asks whether to stop
   connecting.
4. Once the host writes `sas_pending`, the window shows the number in large type, asks "Does Lucille's
   page show this number?", and says by when to answer. **They Match** and **They Differ** stay off until
   the number is on the screen, in a window that is visible and not wholly hidden by other windows
   (macOS says only whether some part of a window shows, so a window over the number alone does not
   count). Neither is a default button and neither has the focus, so Return and Space answer
   nothing. A click on They Match counts only from 0.3 seconds after the number was drawn.
5. **They Match** first reads `status --json`. It runs `pair confirm --json` only if the same
   connection, with the same deadline, still waits for that number, and only once for each click.
   Then the app says "Confirmed on this Mac", and to confirm on Lucille's page too, and starts `run`.
   One gap is left: `pair confirm` names no connection, so a command run on profile `mac-app` from
   Terminal between the app's `status` read and its `pair confirm` is not excluded. The app's own
   verbs that open the store run one at a time, so none of them can come between.
6. **They Differ** runs `pair reject --json`. The app says that nothing was connected, whether Lucille was
   told, and that numbers that differ can mean that someone else made the code, or intercepted it. After
   They Differ, They Match stays off for that connection for as long as Capsule stays open: the app
   keeps no memory of its own. If turning the connection down failed, it is still waiting, and after
   a relaunch the window shows the number with They Match on again; choose **Stop Connecting…**
   again rather than They Match.

The window stays on the screen while you look at Lucille's page in the browser. Closing it keeps the
connection being set up, and the menu then offers **Finish Connecting…** and **Stop Connecting…**. When
the window opens again, after a relaunch too, the app reads `pair status --json` and shows the number
again. It reads `pair status` only while the window is open: every 5 seconds while it waits for Lucille,
and at the deadline. Nothing but They Match confirms a connection. The `pair` child that waits for
Lucille gets 15 minutes, then SIGINT, which keeps the connection. **Stop Connecting…** while that child
still waits gives it SIGINT, then SIGTERM after 5 seconds and SIGKILL after 3 more, and turns the
connection down once it has ended.

The compare page does not stop someone who can rewrite Lucille's page, a browser extension say: they can
show the same number there. That is one reason this way of connecting is only for the owner's Mac.

## Keeping the host running

While this Mac is connected, the app runs `run --json-events --exit-on-stdin-eof` as its child. It holds
a pipe on `run`'s standard input and never writes to it: the lifeline. If the app crashes or is
force-quit, the pipe closes, and `run` finishes what it is doing, replies, and exits (spec/CLI.md §13.4).
The app reads `run`'s events in memory only.

- **Starting.** `run` starts only after a `status --json` the app asked for says this Mac's connection is
  confirmed: at launch, after They Match, after Try Again, and before every restart. Connecting again, or
  Disconnect, drops a restart that was waiting.
- **Crashes.** A `run` that stops when it should not have is started again after 2, 4, 8 … up to 300
  seconds. At the fifth such stop in ten minutes the app stops starting it, and the line under the
  status line says "Capsule keeps stopping"; its note has **Try Again**, and **Show Details** for the last
  40 lines `run` wrote on standard error. Ten minutes of `run` being ready starts the count again. A
  signal is read as a signal, never as the exit code of the same number. An exit whose code does not fit
  its `stopped` reason counts as a crash, and so does `failed` `store_error` after `ready`.
- **Exits that are not crashes** say what they mean, in the line under the status line, and start
  nothing: no connection, with the reason; a Keychain the app cannot use; another copy of the helper
  program still running after 330 seconds; a start the host refused; a Capsule older than one that ran
  on this Mac before. A request that needs a newer host is said once, in a note.
- **Needs attention while `run` runs**, in words only: the Mac's clock, with how to set it; settings the
  host cannot read; a folder check that is stuck; Lucille not reached for two minutes; a message from
  Lucille that cannot be opened; a connection the host left; or no `state` event for two minutes, which
  the app only shows. After the Mac wakes from sleep, the two that count two minutes wait two minutes
  more.
- **Never resumed by itself.** The app resumes a paused host only when you choose Resume.

### Quit, log out, and paused

- **Quit Capsule**, from the menu or from the note about the menu bar, asks first while `run` runs and no
  Disconnect is under way: "Your Mac will stop answering Lucille until you open Capsule again." A quit
  that comes from macOS or another app (log out, restart, shut down, AppleScript, Activity Monitor)
  never asks.
- Then the app closes the lifeline and waits for `run` to finish. After half a second it shows
  "Finishing the current task…" with **Stop now (Capsule will be paused when you next open it)**.
- **Stop now**, or 330 seconds passing, runs `kill --json`. The host stops the running operation, tells
  Lucille it failed, and sets its pause flag. 40 seconds later, or 10 seconds after `run` wrote
  `stopped`, the app sends SIGKILL to `run`. That last step can leave an operation running without its
  time limit (THREAT_MODEL.md, "Known gaps").
- **Log out, restart and shut down** take the same steps, with `kill` after 20 seconds, and the panel
  says so. The app tells them from a plain quit by the reason the quit event carries.
- A `pair` child still waiting for Lucille gets SIGINT, which keeps the connection, then SIGTERM after 5
  seconds, then SIGKILL, before the app quits.
- While any child of the app runs, sudden and automatic termination are off, and an activity keeps App
  Nap away.
- **SIGTERM** to the app asks nothing and runs no `kill`. It closes the lifeline, stops a `pair` child the
  same way but sooner, and exits within a few seconds. `run` then finishes on its own. The app handles
  SIGTERM on a queue of its own, so a note that is up, or the wait to quit, does not hold it back.
- **Notes and questions hold nothing back.** Each note, question and Open panel, and the wait to quit,
  runs a modal loop, which the app starts in a run loop turn of its own, never inside a callout of the
  main queue: started inside one, it would stop every timer, every reply from the helper program and
  every restart of `run` until it closed.
- **Paused at the next launch.** After Stop now, or a log out that reached `kill`, the host is paused.
  The next launch says "Capsule is paused": "Lucille's requests are turned away until you resume. You
  paused Capsule, or a task was stopped when Capsule quit or you logged out." Only **Resume** resumes.

### Disconnect

**Disconnect from Lucille…** asks first. Then it closes the lifeline and waits for `run` to finish, as
at a quit, and only then runs `forget --json`, so a request that is running ends with its reply. If
`run` has not finished in 330 seconds, the app runs `kill`, which also pauses the host, and the note
says so. The note says whether Lucille was told. If `forget` fails, the app reads `status` again, and
starts `run` again if the connection is still there.

A quit while a Disconnect is under way asks nothing, and does not drop it: the app waits for `run` as
at any quit (20 seconds before `kill` at a log out), then runs `forget` (up to 2 minutes more), and
only then quits. It shows no note while it quits.

### More than one connection

If the app's profile holds more than one connection, the host uses neither (`several_pairs`), and the
app can end neither: `forget` and `pair reject` refuse with `several_pairs` too (spec/CLI.md §7), and
the menu offers no Disconnect. Removing the computer in Lucille ends Lucille's half; on this Mac it
most likely changes nothing, since every verb stops at the two connections before it could learn
that one ended (read in the host's code, not tried). The repair is by hand, and it deletes every
connection the app's profile holds:

1. Quit Capsule.
2. In Terminal, delete the Keychain item that keeps the app's connections (service `lucille-host`,
   account `mac-app`):

   ```sh
   security delete-generic-password -s lucille-host -a mac-app
   ```

3. In Lucille, open Computers and remove this computer, then open Capsule and connect again.

This has not been tried on a Mac: CI never makes a profile with two connections.

## How the app talks to the host

- The app drives the host only through the host's command line. The one program it starts is the
  interpreter inside the bundle, as
  `Contents/Resources/runtime/bin/lucille-host-runtime -I -B -X utf8 -m lucille_host --profile mac-app <verb> ...`:
  - `-I` is isolated mode: no `PYTHON*` variables, no user site-packages, and no current folder on the
    import path.
  - `-B` writes no `.pyc` file, so nothing is written inside the signed app.
  - `-X utf8` makes text UTF-8 whatever the locale.
  - `--profile mac-app` is the app's own profile: its own folders, abilities, Keychain item, run lock and
    version record, apart from the `default` profile a command-line install uses (see [Moving from an
    earlier build](#moving-from-an-earlier-build)).
- The arguments go as an argv array, never through a shell. A pairing code goes on standard input, never
  on the command line, and the app writes it to no log.
- The environment is fixed. It holds `HOME`, `USER`, `LOGNAME`, `TMPDIR`, `PATH=/var/empty` and `LANG`,
  and nothing else from the app's own environment. `PATH` names an empty folder, so nothing is found
  there; the host runs its operations with a `PATH` of its own. The working folder is `/`.
- The app reads JSON ([spec/CLI.md](../spec/CLI.md)) from `status`, the pairing verbs, `forget`, `kill`,
  `pause` and `resume`, and `run`'s events. It still reads the text of `--version`, `ops list` and
  `roots list`, and the failure lines of `ops`, `roots`, `pause` and `resume`.
- Two of its children live long: `pair --code-stdin --json`, while it waits for Lucille, and `run`. One
  routine starts every child, under one lock, and keeps the ends of the pipes the app holds out of every
  later child, so no other child holds `run`'s lifeline. Each child's two outputs are read on threads of
  their own. A line of `run`'s events longer than 16384 bytes, newline included, or one that ends in a
  carriage return, is dropped with a note on standard error.
- Each short run has a time limit: 10 minutes for `roots add`, which may wait on a question from macOS or
  on a slow drive; 2 minutes for `status`, `pair status`, `pair confirm`, `pair reject` and `forget`, which
  may wait on Lucille's hub or on a Keychain question; and 30 seconds for every other verb. Those five
  open the store that keeps the connection, so they run one at a time. When `status` takes more than 3
  seconds, a window says that macOS may be asking about Capsule's Keychain item, with **Cancel**. A run
  that passes its limit, or that the user cancels, gets SIGTERM, then SIGKILL 2 seconds later. A process
  that still has not ended 2 seconds after that (one stuck on a stalled network drive cannot end) is left
  behind, and the app stops waiting for it. The app counts it as stuck until it ends.
- The app writes none of the host's files, and reads none: whether the host is paused comes from
  `status`, `pause`, `resume`, `kill` and `run`'s events.
- The app opens no network connection and listens on nothing. Its child `run` talks to Lucille's hub, as
  the command line's `run` does.
- On standard error, the app writes one line each time `pair` or `run` ends: how it ended, and for `run`
  its `stopped` reason, such as `run exited 0 (drained, stdin_end)`. It never writes a code or a number.

All of this is in [`App/HostCLI.swift`](App/HostCLI.swift) and [`App/Connection.swift`](App/Connection.swift).
Two checks keep it so, and neither is a proof:

- The Mac lint, [`tools/guards/macos_lint.py`](../tools/guards/macos_lint.py), reads the Swift sources
  ([VERIFYING.md](../VERIFYING.md) lists its rules). It fails if any file but `HostCLI.swift` names
  Foundation's `Process`; if `HostCLI.swift` does not write the bundled runtime's path and the arguments
  above; if any file uses one of the other ways to start a process it lists (`posix_spawn`, `fork`,
  `exec`, `NSWorkspace`, ...) or one of the ways to reach the network it lists (`URLSession`, Network's
  types, sockets, `URL(string:)`, reading `contentsOf:` a URL, ...); and if any file reads the
  pasteboard. It lets a file name a shell in one place only: the first line of the script the self-test
  writes and starts through `HostCLI.swift`, as a stand-in for the interpreter.
  `tests/test_macos_build.py` pins the lines where `HostCLI.swift` gives that path and those arguments
  to the `Process`, and fails if a file names the pasteboard at all, passes `--hub` or `--store-file`, or
  imports anything but Foundation, AppKit and SwiftUI (Foundation and the C library alone, for the files
  the self-test runs).
- `check_app.py links` reads what the built app's executable imports (`nm -u`). It fails on the C
  functions that start a process or open a socket, on the classes that do either, on `NSPasteboard`, and
  on a link to Network, CFNetwork or WebKit.

Both match names. Code written to get past them can: through the Objective-C runtime, say, which looks a
class up by a string. Review of every change is what stands behind them.

## Moving from an earlier build

Builds before this one ran every verb on the host's `default` profile, the one a command-line install
(`pipx`, `uv tool`) uses. This build runs every verb on its own profile, `mac-app`. So:

- Folders, abilities and a pause set with an earlier build do not show in the app. They stay on disk,
  under `~/Library/Application Support/lucille-host/default`, where a command-line host on `default`
  still uses them. Connect first, then add your folders again in the app: connecting sets aside the
  folders added before it.
- A connection made from the command line on `default` stays with `default`, and the app does not see
  it. If both are used, Lucille sees two computers. Use one or the other.
- Nothing is moved, and the app says nothing about it: finding out would mean running a verb on
  `default`, whose Keychain item another program made, and macOS might ask about it.

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
for c in host probe environment readonly swift relocate launch ui gatekeeper dmg startup; do
  check "$c"
done
check suite --copy-to /tmp/capsule-check
/tmp/capsule-check/Capsule.app/Contents/Resources/runtime/bin/lucille-host-runtime -I -B \
  tools/macos/check_app.py pairing
check signature
chmod -R u+w build/macos/Capsule.app   # so that the next build can replace it
```

On your own Mac, `ui` and `pairing` need your terminal to have Accessibility permission (System
Settings, Privacy & Security, Accessibility), and they take over the screen: `ui` for about two minutes,
`pairing` for several. Without that permission they drive nothing and say so; with `--require-driven`,
as the `macos-app` job runs them, that is a failure. Both work on the app's profile, `mac-app`, for your
own HOME:

- `ui` changes the host's settings while it runs (it pauses, switches an ability, adds folders) and puts
  them back at the end.
- `pairing` quits a running Capsule, and stops at once if a host process of `mac-app` still runs. It sets
  the profile's folder aside first, says where, and puts it back at the end. If it is killed part way,
  the folder stays at that place.

`library-snapshot` and `library-diff`, which CI runs around `launch` and `ui`, read your own `~/Library`
and print what changed, which on a Mac in use is a lot. `suite --copy-to DIR` runs the host's test suite
on a copy of the app's runtime in `DIR`, with the test dependencies installed into the copy only.
`pairing` needs Switchboard's hub and the test harness, which the other checks do without, so it runs
with that copy's interpreter, as above and as CI runs it.

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
| `record` | `macos-app` | Each wheel `MANIFEST.json` names is one `uv.lock` pins (name, version, file, URL and sha256), and the host wheel's sha256 is the one the wheel step output. Each, the host's included, is found again and hashed; the installed distributions are exactly those; every file in `site-packages` is byte for byte a file of its wheel, apart from the installer's own files in each `*.dist-info` folder. Each Mach-O file is compared with its wheel's after `codesign --remove-signature` on a copy of each, and that comparison is reported in the job summary, not failed: SAME, or DIFFERENT with both sizes, or, where codesign fails on either copy, which copy and what it said, with nothing compared. |
| `version` | `macos-app` | `Info.plist`'s two version strings follow the version rule, and `MANIFEST.json`, the bundled host's `--version` and the pins agree with them. |
| `links` | `macos-app` | Every library a Mach-O file loads is macOS's own (`/usr/lib/`, `/System/Library/`), or resolves to a file that exists inside the bundle. `@loader_path`, `@executable_path` and `@rpath` are resolved as dyld resolves them, `..` included, so a path that climbs out of the bundle, and a library the build removed, both fail. Every run path must resolve inside the bundle. The log shows where each library and run path resolves. No Mach-O file needs a newer macOS than the app's minimum, and no file holds the build folder's path. The app's executable imports nothing on the lists above. |
| `signature` | both | The signature is valid, on the app and on each Mach-O file in it; each is signed ad hoc as `bundle` says, with the hardened runtime exactly when the pins ask for it, and nothing has entitlements. It runs again as the last check, so a run that wrote into the bundle fails it. |
| `wrapper` | both | On a writable copy of the app, the wrapper runs the host through a link to it from another folder, and never a runtime beside the link; it runs on the app's profile, `mac-app`, not on `default`. Neither it nor the interpreter run with the app's flags writes any bytecode; the same run without `-B` does, so the check can see one. |
| `host` | both | On the bundle made read-only (`chmod -R a-w`), the interpreter runs the host with the app's flags and nonsense in `PYTHONHOME` and `PYTHONPATH`: `--version`, through the wrapper too, `ops list`, a `roots` round trip, `pause` and `resume`, and the refusal of a home folder. The dependencies' native code loads. |
| `probe` | both | Prints what `status`, `pair status`, `pair --code-stdin` and `run` do on the runner, run as the app runs them, under a fresh HOME and under the runner's own. It gates nothing. |
| `environment` | both | A module or startup file planted where `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, the user site, `PYTHONUSERBASE` and the working folder would load it never loads with the app's flags. For each, a control run without `-I` must load it, or the check fails. `PYTHONSTARTUP` is read only by an interactive interpreter, so no run the app makes could load it, and its control runs `-i`. No `.pth` file is missing from the installed `RECORD` files. |
| `readonly` | both | The bundle cannot be written; `--version`, `status` and `status --json` work from it, and the wrapper too; no file in it changed and no bytecode appeared; then its signature still verifies. |
| `swift` | both | The app's own Swift code that runs the command line and reads its output works against the real command line, through the same calls the menu makes: the folder name suggestions and corrections, which folders are the whole disk (a link to one included), that the command line the app builds for them never has `--allow-home-or-root` and that the host then adds nothing, the plain sentence for each refusal, the time limit and Cancel; every verb on `--profile mac-app` with `PATH=/var/empty`; and, on a throwaway profile, `status`, `pair status`, `pair --code-stdin` (which refuses a text that is not a code once it has read it from standard input) and `run` with the lifeline. Against the host's golden JSON fixtures: every object read through the app's reader. The pairing flow's rules (They Match counts only on the drawn number, after a `status` read that shows the same connection and deadline; They Differ stays; `pair status` only while the window shows), and the supervisor's (the exit of every `run` fixture, the back-off, the five-crash stop, the quit and log out steps, never resuming by itself), each also walked through 40,000 seeded random steps. The child runner, against stand-in children: a signal read as a signal, the lifeline ending a stand-in `run` while a later child lives, the code on standard input and never in argv, input too long for the pipe refused, lines too long or ending in a carriage return dropped, a burst of 5000 events. `macos-app` compiles the self-test once, with the pinned Xcode, and the smoke jobs run that same program. |
| `relocate` | `macos-app` | A copy of the app in another folder, with a space in its path, passes `host` and `swift`. |
| `launch` | both | The app opens the way Finder opens it, and is still running 10 seconds later. SIGTERM then ends it within 15 seconds, though nothing has closed its note about the menu bar. A copy still running from an earlier step fails this check and the `ui` and `pairing` checks, and is then killed. |
| `ui` | both | Driven through System Events as a user would: the app says what it is for and where its menu is when it opens, and again when it is opened while it runs; the menu bar item's title or description, which VoiceOver reads as its name, is "Capsule" (its tooltip does not count); the menu shows the version, "Not connected to Lucille" and every item, and lists abilities in plain words, each with its sentence on what Lucille gets; Pause Lucille and Resume Lucille change the host's pause flag, the status line and the menu bar item's name; an ability switches on, shows a checkmark, and switches off; Add a Folder… goes through the Open panel and the name dialog, where a typed name the host would refuse comes back corrected, and adds the folder; Remove in the Folders submenu removes it; the home folder brings up the question first, Choose a Smaller Folder goes back to the Open panel, and Add Anyway adds it; the Data volume, then `/` through a link to it, each bring up the note that refuses the whole disk, with Choose a Smaller Folder, which goes back to the Open panel, and Cancel, and no button that adds the folder, and neither is added (that the note has no other button is checked in its source by the build tests). The Open panel's buttons are looked for in every window of the app, and in each sheet and group in it, since where they sit differs between macOS versions; where its prompt is nowhere, Return, its default button, chooses the folder, and what the app shows next is checked as before. Then the pairing window: with a code-shaped text on the pasteboard, Connect to Lucille… opens it with its field empty, and the field stays empty once it has the focus; a typed word gets the hint, and Connect stays off; Command-V pastes exactly the text copied; Connect hands it to the host, which refuses it as no code, and the app says so; nothing is connected, and neither text reaches the app's standard output or error. Each step is checked against what the host's command line then reports, on the app's profile. In `macos-app` (`--require-driven`), a machine where System Events cannot drive the app fails the check; the smoke jobs record whether it could, and where it could, a driven step that fails fails the check there too. Screenshots are kept in the job's logs artifact. |
| `library-snapshot`, `library-diff` | both | `~/Library` is walked before `launch` and after `ui`. A new, changed or removed path whose name holds the bundle identifier, the app's name or "lucille", and that [`tools/macos/library-allowlist.txt`](../tools/macos/library-allowlist.txt) does not allow, fails the check; every other change is printed. The state folder of the app's profile, `mac-app`, must be among the changes (in the smoke jobs, the check makes those writes itself first), or the check fails. It skips `~/Library/Developer` and `~/Library/Android`, and cannot see a write to a path whose name says nothing of the app. The snapshots are never uploaded. |
| `gatekeeper` | `macos-app` | Prints `spctl`'s verdict. It is "rejected", as expected for an ad hoc signature, and does not fail the build. |
| `dmg` | both | Exactly one disk image was built, its name ends `-adhoc-macOS-arm64.dmg`, and it verifies, mounts, and holds the app, with a valid signature, and a link to `/Applications`. |
| `startup` | `macos-app` | The median and the fastest of ten runs of `--version` and of `status --json` with no `.pyc`, as shipped; then, on a writable copy, with `checked-hash` files for the standard library, then for everything. Written to the job summary; not a gate. |
| `suite` | `macos-app` | The host's whole test suite passes on a copy of the app's runtime, with the test dependencies installed into the copy only, by hash, and the host's wheel and agent-switchboard installed again by `MANIFEST.json`'s sha256, so that their commands sit beside the copy's interpreter. |
| `pairing` | `macos-app` | It needs Switchboard's hub and the test harness, so it runs with the interpreter of the copy of the runtime that `suite` made, after it; the smoke jobs have no such copy, and do not run it. With a hub of its own and a stand-in for Lucille, a test harness connects the app's profile through the command line (`pair --code-stdin` with `--hub` and `--store-file`, which the app never passes), leaving the number to confirm. Then, driven through System Events: Finish Connecting… shows Lucille's number, once and no other; Return and Space answer nothing; They Match confirms. `run` is the app's child, on the app's profile, with a pipe on its standard input; a second `run` of the profile is refused (exit 21); `run` reads the work room Lucille offers, and the menu says "Connected to Lucille"; the app itself holds no network connection. A folder and `files.sha256` added through the command line, then one request on that folder, answered with the file's fingerprint. `run` killed with SIGKILL while the app's note about the menu bar is up is started again before the note is closed; the app killed with SIGKILL leaves `run` to finish and exit. Quit, chosen in that note during a 20-second request, asks first, shows the finishing panel with Stop now, and quits once `run` has finished and drained. Quit then Stop now, during a longer request, tells Lucille the request failed (`killed_locally`) and pauses the host; the next launch says it is paused, and it stays paused until Resume. Disconnect lets `run` finish, then forgets the connection, and says Lucille was told. The profile is set aside first and put back after, and the app's signature still verifies. Screenshots are kept with the others. |
| a second build | `macos-app` | The unsigned app is built again, in another folder, and the two tarballs are compared by sha256. The job summary says SAME, DIFFERENT or FAILED, with both digests. Not a gate. |

## What CI does not check

CI adds its folders under the home folder, where macOS asks nothing, and it connects through a test
harness with a hub of its own. These need a Mac and a person, and none has been done yet:

- **A folder in Documents, Desktop or Downloads.** Whether macOS asks at all after the Open panel,
  whether its question names Capsule rather than `lucille-host-runtime` and shows Capsule's sentence on
  what it is for, and whether the folder is added after Allow. After Don't Allow, whether the app says
  that macOS is blocking it, and whether System Settings, Privacy & Security, Files & Folders then shows a
  switch under Capsule.
- **A folder on an external or network drive, one in iCloud Drive, and one from another file provider.**
- **A stalled network drive.** Whether Cancel works, and whether the menu then says a folder check is
  stuck.
- **The app's own `pair --code-stdin` reaching the number against a hub.** In CI, a pasted text reaches
  the host on standard input, and the compare page, They Match, `run` and Disconnect run end to end with
  the harness answering the code. The app never passes `--hub`, so it reaches only Lucille's own hub.
- **The Keychain.** CI's harness keeps the connection in a file (`--store-file`). Keeping it in the
  Keychain, and what macOS asks about it on each new ad hoc build, is untested.
- **Paste on macOS 26 and 27,** and whether macOS shows a paste alert there. The `macos-app` job pastes
  on macOS 15; the smoke jobs reach the code window on macOS 26 and 27 only where System Events can drive
  the app there. Paste through the Control-click menu is not driven in CI either.
- **They Match's click time.** CI presses They Match through accessibility, which carries no event,
  so the app takes the time of the press as now. That a real click made before the number was drawn
  is refused is checked in the self-test only.
- **They Differ, then a relaunch.** That They Match stays off after They Differ holds only while
  Capsule stays open; what the window shows after a relaunch, when turning the connection down
  failed, is checked in the self-test's flow only.
- **More than one connection,** and the repair by hand ([More than one connection](#more-than-one-connection)).
- **A quit during a Disconnect,** which finishes the Disconnect first: checked in the self-test's
  supervisor only.
- **Log out, restart and shut down.** The 20-second step, how the app tells them from a plain quit, and
  that sudden and automatic termination stay off are checked in the self-test's supervisor only (see
  [Testing log out by hand](#testing-log-out-by-hand)).
- **A real Lucille, sleep and wake, and closing the lid.**
- **That no child the app starts holds `run`'s lifeline, in every order children start in.** The
  self-test shows it for one order. The one lock around every start is the argument for the rest.
- **Screens CI does not reach**, whose words and rules only the self-test checks: They Differ; a code
  from another hub; "Capsule keeps stopping" and its Show Details; every Needs attention line (the
  clock, Lucille not reached, a message that cannot be opened, a connection the host left, the helper
  program not responding, and the exits 20, 23, 24 and 25); a request that needs a newer host; the log
  out panel; the Stop Connecting question.
- **VoiceOver.** Whether it reads the menu bar item as "Capsule", and "Capsule, paused".
- **A full menu bar, or one beside a notch,** where macOS hides the icon.

### Testing log out by hand

CI cannot log out. To try it on your own Mac, connect the app, then turn on `host.sleep` for the app's
profile. It is for testing, and the menu does not list it:

```sh
/Applications/Capsule.app/Contents/Resources/bin/lucille-host --profile mac-app ops enable host.sleep
```

Change the path if the app is somewhere else. The wrapper runs the bundled runtime with the app's flags
and on the app's profile (the command names it anyway), so nothing is written into the signed app. Have
Lucille send this Mac a `host.sleep` request with `n` set to 60, and log out while it runs. Capsule should
show "Finishing the current task…", with a line saying that it stops the task after 20 seconds. Lucille
should then get the request back as `failed` `killed_locally`. When you log in and open Capsule again, it
should say it is paused, and stay paused until you choose Resume. Afterwards, run the same command with
`ops disable host.sleep`.

## The disk image from CI

The workflow `ci` uploads the disk image only from a run started by hand (its `workflow_dispatch` trigger,
from the Actions tab), and only when every check of `macos-app` passed, as the artifact
`capsule-adhoc-macos-arm64-dmg-workflow_dispatch-<commit>`, kept for 3 days. A pull request or a push
uploads no app. While it is kept, any signed-in GitHub user can download it, because this repository is
public. Every run uploads its logs and screenshots, as `capsule-macos-app-logs` and
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
- **Connecting for anyone but the owner.** The typed number (decisions/0002 §11.2) and Connect from
  the computer (§11.4) are proposals. Until one is decided and built, connecting by pasting a code is for
  the owner's own Mac only.
- **Opening at login.** The app keeps `run` running while it is open, and only then.
- **What comes after connecting**: offering again the folders and abilities a new connection set aside,
  Activity, Stop What's Running, Reset Settings, About, Copy Diagnostics, and a check of each folder at
  launch.
- **ripgrep.** The design bundles it as `Contents/Helpers/rg`, with its licence. The pins hold an empty
  ripgrep commit until then.
- **Updates.** There are none. A new version is a new disk image. When Lucille asks for a newer host,
  the app says so, and offers no Download or Prepare to Update.
- **Uninstall.** Dragging the app to the Trash removes it. The host's own folder,
  `~/Library/Application Support/lucille-host`, stays, and so does a connection: choose Disconnect from
  Lucille… first.
- **Intel Macs.** The locked `cryptography` publishes no Intel macOS wheel, so the app is Apple silicon only.
- **A Welcome window and a main window.** The design has a Welcome window that leads through
  connecting, choosing a folder and turning on an ability, and a main window with the status, folders,
  abilities, activity and settings. This build has the menu, and a note that says where it is.
- **Open Privacy Settings and Open Date & Time Settings buttons.** When macOS blocks a folder, or the
  Mac's clock is wrong, the app says where to go in System Settings, but cannot open it: that needs
  `NSWorkspace`, which the checks above keep out of the app.
- An app icon, and reading `ops` and `roots` as JSON.

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
- **The app reads JSON for connecting and running, and text for the rest.** It reads `status`, the
  pairing verbs, `forget`, `kill`, `pause`, `resume` and `run`'s events as JSON
  ([spec/CLI.md](../spec/CLI.md)). For `--version`, `ops list` and `roots list`, `App/HostText.swift`
  follows the text, and the Swift self-test checks it against the real command line on every build.
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
- **`PATH` is `/var/empty`**, as the design has it, but the working folder is `/`. The design runs the
  host's command line in the host's own state folder.
- **The `Info.plist`'s folder usage text is the two sentences on what Capsule is for** that this page
  opens with, not the shorter sentence the design proposes.
- **The disk image is a plain `hdiutil` image**, with no background picture or window layout.

These depart from the design's screens and what it says the app does. Each was chosen for the first
build; the design may change it.

- **The name step is one dialog, with the name in a field the user can change.** The design shows the
  name in a confirmation sheet, with a separate Change name… button.
- **The Open panel opens where macOS chooses**, not in Documents.
- **What macOS may ask is said in the name dialog**, just before the run that makes macOS ask, not in a
  screen before the Open panel.
- **There are no Open Privacy Settings and Open Date & Time Settings buttons** (see "Not done yet").
- **The pause icon replaces the app's icon**, a pause sign in place of the computer, where the design adds
  a pause badge to the app's icon.
- **Pausing shows no note.** The design confirms Pause with "Paused. Lucille's requests will be turned
  away until you resume."; this build says so in the menu, under the status line, while paused.
- **A name in a script macOS cannot write in Latin letters gives `folder`**, and on a system whose
  Foundation lacks ICU's transforms (the Swift self-test does not run on one today) every non-Latin name
  does.
- **The app says where its menu is each time it opens.** It keeps no settings of its own, so it cannot
  tell a first launch from a later one.
- **The Mac's gate is a click, as decisions/0001 has it.** The app shows the number, and you click They
  Match or They Differ. The design's typed number (§11.2) is a proposal; the maintainer decided on
  2026-11-02 to build 0001's gate now.
- **The note about a pause at launch names both causes**: "You paused Capsule, or a task was stopped when
  Capsule quit or you logged out." The pause flag holds only a time, and the app keeps nothing of its
  own, so it cannot tell which.
- **Quit asks first only while `run` runs, and only the app's own Quit.** A quit from macOS or another
  app, log out included, never asks.
- **Needs attention shows in the menu and in the icon**, and its note opens only when its line is chosen.
- **When `run` gives no `state` for two minutes, the app only says so.** It neither restarts it nor
  closes the lifeline.
- **An exit whose code does not fit its `stopped` reason is a crash**, and so is `failed` `store_error`
  after `ready`, where the design restarts after that without counting it: a store that fails after
  every start would otherwise restart `run` forever.
- **Disconnect lets `run` finish first**, then runs `forget`.
