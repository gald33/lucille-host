# Capsule, by Lucille

Capsule is the Mac app for the host. It lets Lucille use only the folders you choose on this Mac, and only
for the abilities you turn on. Lucille can also see which abilities are on, and the names you gave your
folders. It is a menu bar app. Inside it runs the host program, `lucille-host`, which keeps its own name,
command line and files.

## Status: a first build, not a release

This folder holds the Swift sources of a first, small version of Capsule; the scripts that build and check
it are in [`tools/macos/`](../tools/macos/). It is not a release and it is not ready for anyone who is not
developing it. It is meant to connect this Mac to Lucille: you paste the code Lucille's page shows, then
compare the number the app shows with the one on Lucille's page. Outside CI, the maintainer reported it
working once: on their own Mac, on 2026-11-09, with the disk image of run 42531994220 ([Signing](#signing)). The
try before it there, on 2026-11-08, with a build signed with the hardened runtime, failed. While it is
open, it keeps the host running.
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
- A `checked-hash` `.pyc` beside every `.py` of the runtime, written at build time by the bundled
  interpreter. When it imports one, the interpreter checks the source hash in its header against the
  `.py`, so a stale file never runs. It does not check the code: the build and CI check that each file is
  byte for byte what the bundled interpreter compiles from that source, and the signature seals the files.
  The app runs Python with `-B`, so it writes none at run time
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
- `Contents/Resources/AppIcon.icns`, the app icon, which `Info.plist` names (`CFBundleIconFile`), and
  beside it the menu bar icon, three pairs of template PNGs: `MenuBarTemplate`, `MenuBarPausedTemplate`
  and `MenuBarAttentionTemplate`, each at 18 and 36 pixels (`@2x`). The build makes them from the PNGs
  committed in [`Icons/`](Icons/) and renders nothing: it packs seven of them into the icon file and
  copies the six glyphs (see [Icons/README.md](Icons/README.md)). The app still has no Dock icon.
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
  the commit, so the image cannot be taken for a release asset. `hdiutil create` writes it as
  `build/macos/work/made.dmg`. The build copies that file to a new one, checks that the copy has the same
  sha256 and ends in a whole disk-image trailer, runs `hdiutil verify` on the copy, and only then gives the
  copy this name.

## What the app does when it opens

Only the copy at `/Applications/Capsule.app` runs (decisions/0002 §3.3, §13): with that name, that case
of each letter, and reached through no link. Any other copy says "Capsule needs to be in your Applications
folder", with where it is (on its disk image or another disk, in a folder inside Applications, in the
Applications folder in your home folder, or anywhere else), and "Quit Capsule, drag it into your
Applications folder, then open it from there." A copy in Applications under another name, such as the
Finder's Keep Both gives a second copy, says "This copy is named Capsule 2, so it can't run": with another
copy of Capsule at `/Applications/Capsule.app` (a folder whose `Info.plist` gives Capsule's bundle
identifier), it says to move the renamed one to the Trash, then drag the new Capsule into Applications
again and choose Replace; with none there, to rename it Capsule. Every other copy, that Capsule judges
not to be in Applications (on a disk image or another disk, in a folder inside Applications, in your
home folder's Applications, anywhere else), offers **Show Applications Folder** first, which opens that
folder in the Finder, so that the copy can be dragged there, and quits; a renamed copy, in Applications
already, offers **Quit** alone, as every such note offers Quit. If macOS cannot open the folder, the note comes again with "Capsule couldn't open your
Applications folder." first, and Quit alone. Its menu holds only that line and Quit Capsule. It runs no
host verb at all: every program the app starts goes through one place, which starts nothing until the
app, as it opens, says it is the copy in Applications.

The app has no window to show, so when it opens while this Mac is not connected it says where it is:
"Capsule is in your menu bar". The note first says what Capsule is for, in the two sentences this page
opens with, and then where to click. It says so again whenever it is opened while it runs, connected or
not, for someone who could not find the icon. The note offers **Quit Capsule** too, because on a crowded
menu bar, or beside a notch, macOS hides icons that do not fit, and then the menu cannot be reached.

When it opens, the app first reads the host's `status`, and the note waits for it. If this Mac is
connected, the app shows no note about the menu bar: it may have opened at login, and it goes straight on.
It starts the host's `run` at once, paused or not, since `run` keeps the pause itself (see [Keeping the
host running](#keeping-the-host-running)), and checks whether macOS will open it at login (see [Open at
Login](#open-at-login)). Once the note about the menu bar is closed, or at once while connected, the app
says so if the host is paused, and only **Resume** resumes it. Then it picks up a connection left half
made: it opens the pairing window again, or says why that connection ended.

The menu bar item is named "Capsule", or "Capsule, paused", for VoiceOver. Its icon is the capsule from
the app icon, as a template image, which macOS is meant to draw in the menu bar's own colour (not yet
seen on a Mac). While Lucille is paused, a pause badge sits at its lower right. While something needs
attention, a badge with an exclamation mark sits at its upper right; the name stays the same. No glyph
has both badges: while something needs attention, the exclamation mark takes the pause badge's place,
and the item's name and the menu still say paused.

The `Info.plist` holds the same sentences under each of the six keys macOS reads before it asks about
Documents, Desktop, Downloads, removable and network volumes, and file providers. macOS is meant to show
them under its question. Whether it does, and whether that question names Capsule at all, is not tested yet
(see [What CI does not check](#what-ci-does-not-check)).

## What the menu does

| Item | What it runs |
|---|---|
| "Capsule *version*" | `--json --version` |
| The status line | Nothing. It says what `status --json` and `run`'s events last said (see the table below). While the host is paused, " · Paused" is added. |
| A line under it, when there is one | Nothing until it is chosen. It says what needs attention, or why this Mac is no longer connected. Choosing it shows a note that says what to do, with **Try Again**, **Disconnect from Lucille…** or **Reset Settings…** (see [Reset Settings](#reset-settings)) where one helps, and **Show Details** for the host's own words or the last lines `run` wrote on standard error. |
| "Lucille needs Capsule *version* or newer" | Nothing. Shown once Lucille asked for a newer release, until the app quits, with the highest version asked for; it shows the update notice again (see [Keeping the host running](#keeping-the-host-running)). |
| Prepare to Update… | Shown with that line: lets `run` finish and stop, then quits, with no question (see [Quit, log out, and paused](#quit-log-out-and-paused)). |
| Stop What's Running | `kill --json`, which stops the process of the request that runs and pauses the host. Shown only while a request's process runs, from its `started` to its `finished`, and not while `run` drains for a quit or a Disconnect, whose own steps stop it then: Stop now during a quit, `kill` after 330 seconds during a Disconnect. Then a note says "Stopped, and paused. Resume when you're ready."; the host stays paused until Resume Lucille. |
| Pause Lucille / Resume Lucille | `pause --json` / `resume --json`. While the host is paused, a line says that Lucille's requests are turned away until you resume, and the icon changes. |
| Abilities | `ops list --json`. Each ability shows in the core's few words for it (`summary`), such as "Check a file's fingerprint", with a checkmark when the host says it is on (`enabled`), and a sentence under it, the core's `sends`, says what Lucille gets: "Lucille gets a code that identifies the file's exact contents, not the contents." Choosing one runs `ops enable` or `ops disable` with `--json`, and the checkmark then follows the host's answer, the operation as it now is. Turning off one that is already off shows no note. An ability whose replies can carry a file's contents (`sends_file_content`) is turned on only after a question, "Turn on “*its few words*”?" (the core's `summary`), with what Lucille gets, **Cancel**, the default, and **I Understand, Turn On**. No operation in this release sends file contents. Operations meant for testing (`diagnostic`), such as `host.sleep`, are not listed. Nor is `host.describe`; while it is on, the menu says what it shares: "Lucille sees which abilities are on and your folders' names", once connected. |
| Set Up Folders and Abilities… | Nothing. Shown while Set Up's window was closed before its last page: it opens the window again on the page it was on (see [Set Up](#set-up)). |
| Add a Folder… | See below. |
| Folders | `roots list --json`. Each folder has a **Remove** item, which runs `roots remove <alias>` with `--json`. Removing one that is already gone shows no note. |
| Activity… | Opens the Activity window (see [Activity](#activity)), and reads `ops list --json` for the words each request is said in. |
| Settings | Shown while this Mac is connected. **Open at Login** has a checkmark while macOS will open Capsule when you log in, read again from macOS each time the menu opens; choosing it turns that on or off (see [Open at Login](#open-at-login)). **Reset Settings…** is in it too while the host cannot read its settings file (see [Reset Settings](#reset-settings)). |
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

Then the app asks for the name Lucille will see, starting from the one the host suggests
(`roots add --suggest-alias`, [spec/CLI.md](../spec/CLI.md) §9.1): made from the folder's name, with no
other folder using it, in lowercase letters without accents, `folder` for a name in another script,
with `folder-` put first when it starts with a digit (`2026 Taxes` gives `folder-2026-taxes`), and `-2`,
`-3` added when it is taken. The app asks with a limit of 10 seconds and shows no window meanwhile. If
the host gives no suggestion the app can use, the dialog starts from the app's own, made the same way
but written in Latin letters where macOS can (`日本語` gives `ri-ben-yu`), and says nothing about it. A
name the user types that the host would refuse is turned into one it accepts, and shown again before
anything is added.

The same dialog says what macOS may ask next. For a folder in Documents, Desktop or Downloads, or on
another drive, it says that macOS may ask whether Capsule can access it, and to click Allow. For a
folder in iCloud Drive or another cloud folder, it says that such folders may not work yet.

Then the app runs `roots add --json -- <alias> <path>`, where `--` keeps a path that starts with `-` from
being read as an option, with `--allow-home-or-root` before `--` only if the user chose Add Anyway, and
never for the whole disk. If that takes more than a moment (a network drive, or a question from macOS),
a small window with a **Cancel** button shows; Cancel stops the run. If the host refuses the folder as
the home folder or one that holds it (`broad_folder`), where the app could not tell from the path, the
app asks then, unless the folder is the whole disk, with the question for what the host says it is
(`kind`): the home folder itself, or a folder that holds it. If another folder has the name by then
(`alias_taken`), because something else added one between the host's suggestion and **Add Folder**, the
app shows the name dialog again, with "You already have a folder named *name*. Choose another name."
above a name no folder has. On success, while this Mac is connected or being connected, the app says
that Lucille can use the folder for the abilities the user turns on. With no connection, it says that
Lucille can use the folder only once it is added again after connecting, because a new connection
starts with no folders. If the host found no folder there (`not_a_folder`) but
the app can see one, or macOS refuses the app too, the app says that macOS is blocking Capsule from the
folder, and where in System Settings to allow it, with **Open Privacy Settings**, which opens System
Settings at Privacy & Security, Files & Folders (see [The one opener](#the-one-opener)).

The menu reads the version, the abilities and the folders again each time it opens. It reads `status`
at launch, after each step of connecting, and when the menu opens more than 10 seconds after the last
reading while no child of the app runs: `status` opens the Keychain, where macOS may ask first. When the
host cannot be read, the menu says
so ("Couldn't read your folders") instead of showing an empty list, and a short line at the top names the
problem; choosing it shows the details, and **Reset Settings…** when the problem is a settings file the
host cannot read. When a verb fails, the app says why in a plain sentence for each
refusal code it knows (a name already in use, the 64-folder limit, the host's
own folder, an unreadable settings file, and the others in `App/HostText.swift`), chosen by the `error`
object's `code`, never by its words, and shows the host's sentence (`message`) as a detail. A code it
has no sentence for reads "Something went wrong in Capsule's helper program", with the host's words as
the detail, and so does a run that wrote no `error` object, with the lines on standard error.

Pause, Resume, the ability switches and Remove show the same window with a **Cancel** button when they
take more than a moment. A run the app gave up on (see below) of a verb that takes the lock the host
takes to change its settings (of the app's short runs, `ops enable` and `ops disable`, `roots add` but
the alias suggestion, `roots remove` and `policy reset`; `pair --code-stdin` takes it too, but runs as a
long-lived child, never as a short run) may still hold that lock, and then every change to folders or
abilities waits for it. While such a run is still going, the menu says that a folder check is stuck, and
a change that times out or is cancelled says so too, rather than blaming its own folder. A run of a verb
that only reads (the alias suggestion, `--version`, `ops list`, `roots list`, `roots check`,
`policy previous`, `status` and the others) takes no lock, so one left behind holds nothing up, and the
app does not count it: no later change is blamed on it. The read's own result still shows the line that a
folder check is stuck, since it waits on a drive that stopped answering, and a change, which loads the
same settings and looks at each folder, may wait on it too.

## Reset Settings

When the host cannot read its settings file, `policy.toml` in the app's profile, the menu says
"Capsule's settings file can't be read.", and while `run` runs, its line under Needs attention says
"Capsule's settings can't be read, so it is doing nothing." The note behind either line offers **Reset
Settings…**, and that asks first: "Reset Capsule's settings?", "Capsule will set its settings file aside
and start afresh, with no folders and only the ability that tells Lucille about this Mac. Until you add
your folders and abilities again, Lucille's requests that need them are turned away.", with **Cancel**, the
default, and **Reset Settings**. That runs `policy reset --json`, with the same window and **Cancel** as
the other changes if it takes more than a moment. The host moves the file aside as `policy.invalid.toml`,
which it never reads, and writes the fresh policy in its place; a file an earlier reset set aside there is
replaced, and the policy a pairing set aside is never touched. The app then says "Settings reset":
"Capsule's settings start afresh. The old file was set aside and isn't used. Add your folders and
abilities again from Capsule's menu." It says so too when the reset was cancelled, or ran out of time,
just after the host had done it: the app reads what the host wrote before how the run ended. If the file
could be read again by then, the host refuses with `not_invalid`, which the app takes as done: "Nothing
needed resetting". If the host could not lock its settings at all (its file system refused the lock), the
note offers **Try Again**, which runs the reset again without asking again; a lock another verb holds
makes the reset wait instead, up to the time limit. A reset cancelled before the host had done it says
nothing, unless a run the app gave up on holds things up. The menu then reads the abilities and folders
again, and `status` too, unless a child of the app runs; `run` picks the fresh policy up by itself.
While this Mac is connected, the Settings menu offers **Reset Settings…** too, while the menu's line says
the file can't be read.

## Activity

The Activity window says what Lucille asked this Mac to do since Capsule opened, newest first, from the
`request` events `run` writes, each with the time Capsule first saw it: "Lucille asked to check a file's
fingerprint: “a.txt” in documents. Finished on this Mac." The words for the operation are the core's
own summary of it, from `ops list --json`, taken only from an `op` object the app reads whole: an
operation in a state this release does not know is not read at all, so it has no summary, and its
requests are said with its name in quotes ("Lucille asked for “…”"), never with words guessed for it.
The path Lucille asked for is shown in quotes, cut in the
middle past 60 characters, or as "a path that can't be shown" when it holds a control, format, line or
paragraph separator character, a quotation mark that opens or closes (a curly quote or apostrophe, a
guillemet), or another quotation mark, apostrophe or prime from a fixed list (a low-9 quote, a fullwidth
one, a double prime, and letters such as the modifier apostrophe U+02BC), which could end Capsule's own
quotes. That list is chosen by name (every character whose Unicode 15.1 name holds QUOTATION MARK or
APOSTROPHE, and the primes), so it is not every look-alike: a modifier letter double prime (U+02BA), a
ditto mark (U+3003), a Hebrew gershayim (U+05F4) and the corner brackets (U+300C to U+300F) are still
shown. The folder is named only by a valid alias. A request
turned away says why as a fact ("Turned away: that ability is off on this Mac."), with no button:
nothing in the window offers to turn an ability on. "Finished on this Mac." never means that Lucille got
the reply. A request that was still going when `run` stopped reads "Stopped when Capsule's helper
program stopped." A request read before `run` restarted and decided after it keeps its entry; one that
ended is never written again, so a request Lucille sends later with the same job id, which the host
forgets 15 minutes after the request's deadline, gets an entry of its own.

Capsule keeps this in memory only, at most 500 requests, and it is gone when Capsule quits. Nothing in the
window can be selected or copied.

**Show the Host's Log** runs `log --json --limit 200` and shows those lines as the host wrote them, each
cut at 2000 characters, under "As the host wrote it. Anything running as you can change this file.": the
log is a file in your own folder, which proves nothing, and its format is not part of the contract. It is
read again each time it is shown.

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

## Set Up

Once you click **They Match** in the pairing window, the first time the status line then says
"Connected, waiting for Lucille" or "Connected to Lucille", the app opens its Set Up window by itself.
It is one window with pages, and it stays on the screen while you work in another app. It opens only for
a connection confirmed with They Match in this run of the app: not after Capsule is opened again, and not
for one confirmed in Terminal. If you close it before its last page, the menu offers **Set Up Folders and
Abilities…**, which opens it again where it was, until you choose Done or the connection ends. Closing it
on its last page ends it, as Done does.

1. **Add back what you had before?**, only when this connection set your earlier settings aside. A new
   connection starts from the fresh policy, with no folders and only `host.describe` on, and the host
   keeps the one it replaced (decisions/0002 §11.6). The page is shown only when all of these hold: this
   run of the app started the `pair --code-stdin` child for the attempt They Match confirmed, and that
   child said it set the policy aside (`policy_reset`); `policy previous --json`, with 30 seconds, says
   the policy it found was set aside at or after the second that attempt started (an earlier one is
   another connection's, spec/CLI.md §6); and something is left to offer. An attempt runs from Connect to
   Lucille… to They Match in one pairing window: if you close that window and finish connecting from
   **Finish Connecting…**, which picks up whatever connection `status` shows waiting (one started in
   Terminal too), nothing is offered back. Otherwise the window opens on Folders, and when the host can't
   read the policy set aside (`previous_invalid`), a note says so, "Couldn't read what you had before".
   It says: "Connecting started Capsule's settings afresh. These are the folders and abilities you had
   before. Check the ones to add back. Nothing is added unless you check it." Each folder and ability has
   a box, and every box starts unchecked; nothing checks them all. The whole disk is shown with no box,
   by its spelling whatever the policy set aside marked it, and, for a folder it marked broad, by the
   disk too: "Capsule doesn't add your whole disk back, so it isn't offered." Left out, with one line
   that says some are not shown ("it is already set up, or Capsule doesn't offer it here"): a folder
   whose path holds a byte that is not UTF-8, which the host writes as U+FFFD, so the path can be shown
   but not passed back; one whose name a folder already has; and every ability that is not plainly off
   and listed as the menu lists it (one already on, one for testing, `host.describe`, one this Mac can't
   run or this release lacks, and one an update turned off, whose Review and Turn On is not built).
   **Skip**, the default, adds nothing. **Add Checked**, on only while something is checked, adds the
   checked rows back in order, through the menu's own steps. A folder: the whole disk is refused by its
   spelling before any run; the home folder, or one that holds it by its path, is asked about first, as
   the menu asks, and Choose a Smaller Folder opens the Open panel instead; then
   `roots add --json -- <alias> <path>`, with `--allow-home-or-root` only once you chose Add Anyway. If
   the host still finds the folder broad (`broad_folder`), the question comes then; the name dialog opens
   again if the name is taken; then `roots check` on it, so that a question macOS asks comes now. The
   next row starts once the host has answered for the folder. An ability goes through its switch, with
   the question first for one whose replies can carry a file's contents; the next row starts once the
   switch is turned, without waiting for the host's answer or for that question, which comes in its turn
   among the app's notes. The host makes the changes one at a time, under its settings lock.
2. **Folders Lucille may use**: your folders from `roots list`, each with what `roots check --json` (10
   minutes) says of it unless the host can open it: "macOS is blocking Capsule from this folder." (with
   **Open Privacy Settings** under it), "This folder's permissions don't let Capsule open it.",
   "This folder isn't there any more." or "Capsule couldn't open this folder.". The folders are checked
   each time the page comes, and when one is added. **Add a Folder…** is the menu's own (the Open panel,
   the questions and the name dialog), each folder has **Remove**, and **Continue** goes on.
3. **What Lucille may do**: a switch for each operation but those meant for testing and a name this
   release lacks, `host.describe` included, as it is: its position from the host's `enabled`, its words
   the core's `summary` and `sends`, off for one this Mac can't run, and the sentence that says one an
   update turned off. A switch runs `ops enable` or `ops disable` as the menu's does, with the same
   question first, and then shows what the host says. **Continue**.
4. **Capsule is ready**: "Lucille can now ask this Mac to do what is turned on, in the folders you
   added." A box, **Open Capsule when you log in**, is checked, and under it: "macOS will show a notice
   that Capsule can now open when you log in. That is expected. You can change it later in System
   Settings › General › Login Items & Extensions." **Done** closes the window, and, with the box
   checked, asks macOS to open Capsule when you log in (see [Open at Login](#open-at-login)). Closing
   the window on this page ends Set Up and asks for nothing.

Nothing in Set Up adds a folder or turns an ability on but your own click, and nothing of it is kept:
after a relaunch, nothing is offered back. The policy set aside stays where the host keeps it, and the
wrapper in the app lists it: `Contents/Resources/bin/lucille-host --profile mac-app policy previous`.

Set Up's limits in this build: its pages do not scroll, so a long offer makes a tall window. After the
home folder question's Choose a Smaller Folder, or a name dialog opened again for a name in use, the next
checked row starts while that panel or dialog is still up. Set Up follows this run's They Match, not a
connection's name: if that connection is forgotten and another is confirmed in Terminal before the app
has said Connected, and between two of the app's `status` reads, the Set Up window opens for the other
one, and may offer back what the policy set aside held, unchecked.

## Open at Login

Capsule's one login item is Capsule itself (`SMAppService.mainApp`, decisions/0002 §9): no helper, agent
or daemon. Only the copy at `/Applications/Capsule.app` asks macOS anything about it, since macOS knows
the item by Capsule's bundle identifier, which a "Capsule 2" shares (not yet tested on a Mac).

- **Turning it on** takes your click: **Done** on Set Up's last page with its box checked, or **Open at
  Login** in the Settings menu, while this Mac is connected; not once `run` has said the connection is
  gone, before the menu's `status` has caught up, nor during a Disconnect. Then the app goes by what
  macOS says. On,
  it says nothing more. Waiting for your OK: "macOS needs your OK before Capsule can open when you log
  in", with **Open Login Items Settings**, which opens System Settings there, and **Not Now**. Not
  turned on: "Capsule couldn't turn on opening at login", that you can open Capsule yourself after you
  log in, and, when macOS gave any, its own words under **Show Details**.
- **At launch, while connected**, the app reads the item's state. When macOS waits for your OK, as after
  you turn Capsule off in System Settings › General › Login Items & Extensions (not yet tested on a Mac:
  if macOS reads an item turned off there as not registered, the app says nothing), it says "Capsule
  won't start when you log in", with **Open Login Items Settings** and **Keep It Off**, which takes the
  item away so that it is not asked again (that this ends macOS's wait is not yet tested on a Mac). It
  says nothing when the item is not registered, which may be your own choice.
- **Taking it away** needs no click, since it only narrows: whenever the connection ends. That is
  Disconnect, whether or not you quit during it, after `forget` is done; `run` ending because the
  connection is gone (`not_paired`, `leaked`, `expired`, `ended` or `forgotten`), or a `status` the
  supervisor asked for that says so; Stop Connecting, once `pair reject` has forgotten the connection or
  found none, and any step of connecting that finds none (`none`); and a `status` that finds no
  connection once one was seen. A gate that can't open (`no_gate`) keeps the connection, and the item,
  until Stop Connecting, which is what the menu offers for it. A connection Lucille ends while you quit, as `run` drains, keeps the item until the next
  launch reads `status`, and that launch may itself be at login. Disconnect's note adds "Capsule won't
  open when you log in." when there was one, or how to turn it off in System Settings when it could not
  be taken away. The app calls `unregister()` only for an item that is on or waits for your OK.
- **Nothing is kept.** The state is read from macOS each time. Each time the app acts on the item, or
  checks it at launch, it writes one line on standard error with the state's name and nothing else, such
  as `login item: enabled`.

## The one opener

Three buttons show you a place in macOS, through the app's one opener (decisions/0002 §20):

- **Open Date & Time Settings**, in the note behind the menu's line that says this Mac's clock is off.
  The waiting page of the pairing window says how to set the clock in words only.
- **Open Privacy Settings**, at Privacy & Security, Files & Folders, never Full Disk Access: in the note
  that says macOS is blocking Capsule from a folder you add, and under such a folder on Set Up's Folders
  page.
- **Show Applications Folder**, in the note of a copy Capsule judges not to be in Applications, which
  a renamed one there is not (see [What the app does when it opens](#what-the-app-does-when-it-opens)).

Each opens only from your click. The places are a closed list, `SystemPlace` in
[`App/PlaceRules.swift`](App/PlaceRules.swift), which also says which problem offers which place, from
what the app itself sees: the problem `run` reports, macOS's refusal of a folder, where this copy is.
No text or request from Lucille offers one, and no request the host turned away does; the clock's place
follows the clock state `run` reports, which the hub's refusals of this Mac's writes set. Nothing read
while the app runs can make an address: it only picks one of the three. One call in
[`App/SystemPlaces.swift`](App/SystemPlaces.swift) opens them, with the addresses written there. When
macOS cannot open a place, the note comes again with "Capsule couldn't open System Settings." (or "your
Applications folder") first, and OK alone (Quit, for a copy not in Applications); it already says how
to get there by hand. For the button under a folder on Set Up's Folders page, which had no note, a note
says so first, then that macOS is blocking Capsule from the folder and how to allow it by hand, with OK
alone. The one other button that opens anything is Open at Login's **Open Login Items
Settings**, which opens System Settings through Apple's own call for it, with no address.

The addresses of the two System Settings pages are the best known for macOS 13 and later, and not yet
seen on a Mac: `check_app.py places` opens each and prints what System Settings then shows.

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
  on this Mac before.
- **A newer release.** When Lucille asks for a newer release than this one (`run`'s `host_too_old`),
  the app says "Lucille needs Capsule *version* or newer" once, however many requests ask: one note,
  which a later request does not bring back once it is closed. The version is the highest asked for when
  the note is drawn, which the app reads only as three numbers, in the form the protocol gives. The note
  says where to get it, "Get the newer Capsule from where you got this one.", and offers **Not Now**, the
  default, and **Prepare to Update**. There is no Download button. Until the app quits, the menu shows the
  note's line, with the highest version asked for, which shows the note again, and **Prepare to
  Update…**.
- **Needs attention while `run` runs**, in words: the Mac's clock, with how to set it, and **Open Date &
  Time Settings**, which opens System Settings there (see [The one opener](#the-one-opener)); settings the
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
- **Prepare to Update**, from the note about a newer release or the menu, is the same quit with no
  question, so that the Finder can then replace the app: open the new disk image, drag Capsule into
  Applications, and choose Replace.
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
  paused Capsule or chose Stop What's Running, or a task was stopped when Capsule quit or you logged
  out." Only **Resume** resumes.

### Disconnect

**Disconnect from Lucille…** asks first. Then it closes the lifeline and waits for `run` to finish, as
at a quit, and only then runs `forget --json`, so a request that is running ends with its reply. If
`run` has not finished in 330 seconds, the app runs `kill`, which also pauses the host, and the note
says so. The note says whether Lucille was told. Once the connection is gone, the login item goes too
(see [Open at Login](#open-at-login)), and the note says so when there was one. If `forget` fails, the app reads `status` again, and
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
  - `-B` writes no `.pyc` file, so nothing is written inside the signed app. It still reads the ones the
    build wrote.
  - `-X utf8` makes text UTF-8 whatever the locale.
  - `--profile mac-app` is the app's own profile: its own folders, abilities, Keychain item, run lock and
    version record, apart from the `default` profile a command-line install uses (see [Moving from an
    earlier build](#moving-from-an-earlier-build)).
- The arguments go as an argv array, never through a shell. A pairing code goes on standard input, never
  on the command line, and the app writes it to no log.
- The environment is fixed. It holds `HOME`, `USER`, `LOGNAME`, `TMPDIR`, `PATH=/var/empty` and `LANG`,
  and nothing else from the app's own environment. `PATH` names an empty folder, so nothing is found
  there; the host runs its operations with a `PATH` of its own. The working folder is `/`.
- The app reads JSON ([spec/CLI.md](../spec/CLI.md)) from every verb it runs: `--version`, `ops` and
  `roots`, `status`, the pairing verbs, `forget`, `kill`, `pause` and `resume`, `policy reset`, `run`'s
  events, for Activity, `log --limit 200` and `ops list`, and, for Set Up, `policy previous` and
  `roots check`. It decides only on the objects' fields,
  a refusal by its `code`, never on their words, which it shows. It shows the lines on standard error
  only as the details of a usage error and of a run that wrote no `error` object.
- Two of its children live long: `pair --code-stdin --json`, while it waits for Lucille, and `run`. One
  routine starts every child, under one lock, and keeps the ends of the pipes the app holds out of every
  later child, so no other child holds `run`'s lifeline. Each child's two outputs are read on threads of
  their own. A line of `run`'s events longer than 16384 bytes, newline included, or one that ends in a
  carriage return, is dropped with a note on standard error.
- Each short run has a time limit: 10 minutes for `roots add` of a folder and for `roots check`, which may
  wait on a question from macOS or on a slow drive; 10 seconds for `roots add --suggest-alias`, after
  which the name dialog starts from the app's own suggestion; 2 minutes for `status`, `pair status`,
  `pair confirm`, `pair reject` and `forget`, which may wait on Lucille's hub or on a Keychain question;
  and 30 seconds for every other verb, `policy previous` included. The five with 2 minutes open the store
  that keeps the connection, so they run one at a time. When `status` takes more than 3 seconds, a window says
  that macOS may be asking about Capsule's Keychain item, with **Cancel**. A run that passes its limit, or
  that the user cancels, gets SIGTERM, then SIGKILL 2 seconds later. A process that still has not ended 2
  seconds after that (one stuck on a stalled network drive cannot end) is left behind, and the app stops
  waiting for it. If its verb takes the host's settings lock, the app counts it as stuck until it ends.
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
  `exec`, ...) or one of the ways to reach the network it lists (`URLSession`, Network's types, sockets,
  `URL(string:)`, reading `contentsOf:` a URL, ...); and if any file reads the pasteboard. It lets a file
  name a shell in one place only: the first line of the script the self-test writes and starts through
  `HostCLI.swift`, as a stand-in for the interpreter. It lets one file, `SystemPlaces.swift`, name
  `NSWorkspace` and make a URL from a string, for one call that opens one of the places written there
  (see [The one opener](#the-one-opener)), and no other file. That file must be its design token for
  token, its addresses in their order; every other file may name it only in a call,
  `SystemPlaces.open(`, may not extend `URL` or the opener's types or give one of them another name,
  and may hold no backtick, which can make a name of any word. It also refuses the other ways to open
  a URL or another application it lists (SwiftUI's `openURL` and `Link`, a link attribute or a Markdown
  link, the sharing services, Apple events), a button pressed by code (`performClick`), a function
  reached by its symbol, and loading a bundle's code.
  `tests/test_macos_build.py` pins the lines where `HostCLI.swift` gives that path and those arguments
  to the `Process`, and fails if a file names the pasteboard at all, passes `--hub` or `--store-file`, or
  imports anything but Foundation, AppKit and SwiftUI, and ServiceManagement in `LoginItem.swift` alone
  (Foundation and the C library alone, for the files the self-test runs).
- `check_app.py links` reads what the built app's executable imports (`nm -u`). It fails on the C
  functions that start a process or open a socket, on the classes that do either, on Launch Services'
  and the Apple Event Manager's C calls, which open, register or drive another application, on the
  `NSAppleEventDescriptor` and `NSSharingService` classes, on `NSPasteboard`, and on a link to
  Network, CFNetwork or WebKit. It does not refuse `NSWorkspace`, which the one opener calls.

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

The disk image gets its name only once `hdiutil verify` has passed on it, so a build that fails while
making the disk image leaves none in `build/macos/`. A build that fails before that step leaves the
disk image of the build before it, if there was one, and `check_app.py dmg` would check that one.
`build/macos/work/` holds the build's scratch files.

In 3 of 81 CI builds (2026-10-29 to 2026-11-09), `hdiutil` refused to open the file `create` had just
written ("Resource temporarily unavailable") on every try the job made. That is why the build verifies a
copy. The logs show a `diskimages-helper` still running at the job's cleanup. That it was running during
those tries, and that the job's `create` started it, is inferred. That the helper held a lock on that one
file, which a copy does not carry, is inferred, not seen.

After `create`, the build prints whether another process holds a `flock(2)` lock on the file. When one
does, it also prints what `ps`, `lsof`, `hdiutil info`, `hdiutil verify -debug` and the disk-image log
say. When `GITHUB_ACTIONS` is `true`, as it is on GitHub's runners, such a lock, and a copy that `hdiutil
verify` refuses as busy, are also shown as warnings on the run's page. The build never ends a process,
and it runs `sudo -n lsof` only when `GITHUB_ACTIONS` is `true`. `hdiutil info` lists every disk image
attached, by path, and the disk-image log can name images too, so on your own Mac that output can name
disk images of your own: read it before you share the log. A helper left behind keeps the deleted file,
about 44 MB, until it exits (inferred).

The app, its tarball and the disk image land in `build/macos/`, which git ignores. Then run the checks CI
runs, in the order it runs them:

```sh
check() { uv run --no-project python tools/macos/check_app.py "$@"; }
check bundle
check bytecode
check record
check version --build-number 1
for c in links signature wrapper; do check "$c"; done
chmod -R a-w build/macos/Capsule.app
for c in host probe environment readonly swift relocate install places placement launch ui gatekeeper dmg startup; do
  check "$c"
done
check suite --copy-to /tmp/capsule-check
/tmp/capsule-check/Capsule.app/Contents/Resources/runtime/bin/lucille-host-runtime -I -B \
  tools/macos/check_app.py pairing
check signature
check install --restore                # puts /Applications back as install found it
chmod -R u+w build/macos/Capsule.app   # so that the next build can replace it
```

`install` puts a read-only copy of the build at `/Applications/Capsule.app`, the only place the app runs
from. `placement`, `launch`, `ui` and `pairing` open that copy, and refuse to run unless it is this build,
so that a copy an earlier build left there never answers for this one; `placement` also opens the build's
own copy, and copies of its own, to see each refuse. If `/Applications/Capsule.app` already holds this
build, `install` only checks it again. If it holds another copy, such as your own, `install` leaves it
alone and says so: `install --replace` sets that copy aside first, as `~/Capsule.app (set aside by
check_app.py install)`, says where, and installs. The `install --restore` line at the end puts back what was there
before: it removes the installed copy and puts the one set aside back, or, with none set aside, removes
the installed copy if it is this build. A copy is never set aside over one set aside before. Quit Capsule
before any of these: each stops a running Capsule first. If a run stopped before its last line and you
have built again since, `/Applications/Capsule.app` holds the earlier build, which every check above
refuses: `install --restore` removes it and puts back the copy set aside, if there is one; with none set
aside, remove the earlier build yourself (`chmod -R u+w` it, then delete it), once you have made sure it
is not a copy of your own.

`placement` also puts a copy named `Capsule 2.app` in `/Applications` for a while, and, for part of that,
moves `/Applications/Capsule.app` aside as `/Applications/.Capsule.app.set-aside-by-placement`, a name the
Finder does not show. It removes the one and puts the other back however it ends, unless it is killed part
way: then put the copy back with `mv /Applications/.Capsule.app.set-aside-by-placement
/Applications/Capsule.app`, and remove `/Applications/Capsule 2.app` (`chmod -R u+w` it first). It expects
the build's own copy to say "This copy of Capsule can't run from where it is now.", the words for a folder
that is none of the others, so run it from a checkout that is not on another disk (under `/Volumes`), in
Applications, or in the Applications folder in your home folder.

On your own Mac, `ui`, `placement` and `pairing` need your terminal to have Accessibility permission
(System Settings, Privacy & Security, Accessibility), and they take over the screen: `ui` for about two
minutes, `placement` for one or two, `pairing` for several. Without that permission they drive nothing and
say so; with `--require-driven`, as the `macos-app` job runs them, that is a failure. `places` needs it
too, to read System Settings' windows: without it, every "System Settings shows" fails, unless it is run
with `--record-only`. It quits System Settings before and after each page, even one you had open, and
says so first; it touches nothing of the app's. `placement` closes, by its close button, each Finder
window that shows after Show Applications Folder under a name that was not open before the press, and
leaves the ones you had open; where it could not read the Finder's windows before the press, it closes
none. The others work on the app's profile, `mac-app`, for your own HOME:

- `ui` changes the host's settings while it runs (it pauses, switches an ability, adds folders) and puts
  them back at the end. Its last step adds a folder it makes in Documents,
  `lucille-host-ui-check-documents`, and leaves a folder of that name alone if one is there; it types
  the folder's path only once it has read the Open panel's own button. If macOS asks, it prints the
  question, and answers Don't Allow only if the question names Capsule or its interpreter and is the
  one window of the process that draws it; afterwards it asks `tccutil reset
  SystemPolicyDocumentsFolder` to take back the answer, for Capsule's bundle identifier and its
  interpreter's alone, and prints what tccutil said; that tccutil does this without root is not yet
  seen on a Mac. If it could not, reset Capsule by hand in System Settings, Privacy & Security, Files &
  Folders. A question it did not answer, or that came after it stopped waiting, it does not close: it
  says the question is left on screen, and you answer it. If the folder is still being checked when the
  step ends, it presses Capsule's Cancel, and ends any `roots add` of `mac-app` still running 30 seconds
  later. It removes the folder and the name it added, and quits System Settings only if it opened it.
  It ends within the 15 minutes CI gives the `ui` check, counted from the check's start, its clean-up
  included: it starts only with about 7 of them left, and prints whatever of its clean-up the time
  leaves undone, so that you can do it by hand.
- `pairing` quits a running Capsule, and stops at once if a host process of `mac-app` still runs. It sets
  the profile's folder aside first, says where, and puts it back at the end. If it is killed part way,
  the folder stays at that place. Through Capsule, it also asks macOS to open Capsule when you log in
  (Set Up's Done) and takes that away again (Disconnect, or Keep It Off), and it does not put back what
  you had: the check itself only reads the login item. If yours was on, turn it on again in the
  Settings menu once Capsule is connected; if the check stops between those steps, it may stay on.

`library-snapshot` and `library-diff`, which CI runs around `launch` and `ui`, and around `pairing`, read your own `~/Library`
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
  `config-3.13-darwin`, and the three stale `.pyc` files in `encodings/__pycache__` (the build writes
  its own later);
- `site-packages/README.txt`.

[`tools/macos/prune.txt`](../tools/macos/prune.txt) lists all of these but pip, one path per line, each
group with its reason. The build refuses an entry the pinned runtime does not hold. It does not delete
what the list misses: it refuses a runtime that is not then exactly the interpreter and `lib/python3.13/`,
with an empty `site-packages`. CI runs the host's whole test suite on a copy of the trimmed runtime, so a
module the host needs cannot go missing without a test failing.

## Signing

The build signs every Mach-O file in the bundle, innermost first, then the interpreter, and then the
bundle. The signature is ad hoc (`codesign -s -`), without the hardened runtime, as
`tools/macos/pins.toml` asks, and with no entitlements. It never uses `--deep` to sign. Each file gets its
own identifier: the interpreter `com.lucille-ai.capsule.runtime`, and every other one
`com.lucille-ai.capsule.` followed by its path in the bundle.

Ad hoc builds used to have the hardened runtime. On a real Mac (2026-11-08, macOS 26.2), dyld then
refused cryptography's extension ("different Team IDs"), and the app failed to pair; that the refusal
caused the failure is inferred from the app's error. GitHub's runners had loaded the same extension, so
CI did not show this. [decisions/0002](../decisions/0002-macos-app.md) §7 has the run and the error.
The maintainer reported that on 2026-11-09, on the same Mac, the disk image of run 42531994220, the first
one without the hardened runtime that a run uploaded, loaded the extension and paired with Lucille (not
in a CI log). On the runners,
`csrutil status` says System Integrity Protection is disabled (run 42522633104). That this is why they
loaded what that Mac refused is likely, but inferred (decisions/0002 §7).

CI checks the following on every build:

- the app and every Mach-O file in it were signed ad hoc by the build, not only by the linker, with the
  identifier the build gave each and no Team ID, and the Mach-O files are exactly the expected ones;
- `codesign --verify --deep --strict` passes on the app after the host has run from the bundle, so the
  runs wrote nothing into it;
- `codesign --verify --strict` passes on each Mach-O file in the bundle on its own. The interpreter and
  the dependencies' native libraries are under `Contents/Resources`, where the bundle's signature may
  seal them as plain files and not check their own signatures, so this is checked separately;
- every Mach-O file has the hardened runtime flag exactly when the pins ask for it, so now none has,
  and none has the `library-validation` flag either;
- the dependencies' native code (cffi, cryptography, and keyring's macOS backend) loads with no
  entitlement: in the `macos-app` job on macOS 15, and in the smoke jobs on macOS 26 and 27. The runners
  also loaded it under the hardened runtime, which a real Mac then refused, so this check passing does
  not show that a person's Mac loads it.

A signature that macOS trusts needs a Developer ID certificate and notarization. This build does
neither. Whether the native code loads with no entitlement under a Developer ID signature, with the
hardened runtime, is not tested yet, and nor is whether Apple's notary service accepts code under
`Contents/Resources`.
There is reason to expect that it does: BeeWare's Briefcase signs the Mach-O files it puts under
`Contents/Resources` the same way, inside out, and its apps are notarized.

## What CI checks

The workflow `ci` (`.github/workflows/ci.yml`) builds and checks the app on every pull request and every
push to `main`. Its job `macos-app` builds the app on a GitHub-hosted Apple silicon runner (`macos-15`) and
runs each check in [`check_app.py`](../tools/macos/check_app.py). Its jobs `macos-smoke`, on `macos-26` and
on `xcode-27` (macOS 27), take the disk image and the compiled Swift self-test that `macos-app` built,
refuse them unless their sha256 is the one `macos-app` gave, and run the checks marked "both" on them; they
build nothing. The workflow's token can only read the repository, and the Mac jobs use no secret. A check
that finds a problem fails its job. On `xcode-27` alone, a failed job does not fail the workflow run
(`continue-on-error`, the maintainer's decision of 2026-11-02, decisions/0002 §18).

| Check | Where | What it shows |
|---|---|---|
| `bundle` | both | No symbolic link in the bundle. Its Mach-O files are exactly the expected five: the app's executable, the interpreter, `lib-dynload`'s `_dbm`, and the two wheel extensions. The app and each of them is signed ad hoc by the build, not only by the linker, with no Team ID and the identifier the build gave it. The icons are the ones the build makes from this checkout's `Icons/`: `Info.plist` names `AppIcon`; `AppIcon.icns` holds its ten PNGs in order, each of the pixels its row gives, which the build holds to the size of its entry type's slot, and is byte for byte the file the build writes; and the six menu bar glyphs are the committed files, black with alpha only. Then `iconutil`, macOS's own reader, must read `AppIcon.icns` into an iconset of exactly the ten files the entries' slots name, `icon_16x16.png` to `icon_512x512@2x.png`, each a PNG whose IHDR gives the pixels its name says (`icon_NxN.png` N, `icon_NxN@2x.png` 2N); what `iconutil` says, and each file with that size, are printed. Last, AppKit, through `osascript`'s JavaScript, must load each glyph from the bundle by its name with `NSBundle`'s `imageForResource:`, the search the app's `NSImage(named:)` makes (inference): 18 points square, from two files, of 18 and 36 pixels. Both steps ran in run 42620168506, in `macos-app` (macOS 15.8.2) and in the smoke jobs (26.7.2 and 27.0.1), when the `iconutil` step printed its files and checked none: on each, `iconutil` wrote the same eleven, with the same byte counts, `icon_48x48.png` among them, from an `icp6` entry that held the 64 pixel PNG (which entry gave which file is inferred: [decisions/0002](../decisions/0002-macos-app.md) §6), and the build no longer writes `icp6`. AppKit found each glyph as 18 points from 18 and 36 pixels. No run has yet held `iconutil`'s files to the names and sizes above. |
| `bytecode` | both | Each `.py` of the runtime's library has one `checked-hash` `.pyc` beside it, in `__pycache__`, and the bundle holds no other bytecode. The bundled interpreter then runs the build's own program: each `.pyc` has its magic number and its `.py`'s source hash, and is byte for byte what it compiles from that `.py`; a run that does not end as the program ends, an exception say, fails too, and so does one that counts no `.py` file. Last, `--version`, run as the app runs it with `-v`, loads its modules in the bundle from their `.pyc` and compiles none from source. It writes nothing beside the app, so it is meant to be run on a copy you downloaded too; that is not yet tried on a Mac ([The disk image from CI](#the-disk-image-from-ci)). |
| `record` | `macos-app` | Each wheel `MANIFEST.json` names is one `uv.lock` pins (name, version, file, URL and sha256), and the host wheel's sha256 is the one the wheel step output. Each, the host's included, is found again and hashed; the installed distributions are exactly those; every file in `site-packages` is byte for byte a file of its wheel, apart from the installer's own files in each `*.dist-info` folder and the `.pyc` files the build writes, one beside each `.py` a `RECORD` lists (`bytecode` checks what they hold). Each Mach-O file is compared with its wheel's after `codesign --remove-signature` on a copy of each, and that comparison is reported in the job summary, not failed: SAME, or DIFFERENT with both sizes, or, where codesign fails on either copy, which copy and what it said, with nothing compared. |
| `version` | `macos-app` | `Info.plist`'s two version strings follow the version rule, and `MANIFEST.json`, the bundled host's `--version` and the pins agree with them. |
| `links` | `macos-app` | Every library a Mach-O file loads is macOS's own (`/usr/lib/`, `/System/Library/`), or resolves to a file that exists inside the bundle. `@loader_path`, `@executable_path` and `@rpath` are resolved as dyld resolves them, `..` included, so a path that climbs out of the bundle, and a library the build removed, both fail. Every run path must resolve inside the bundle. The log shows where each library and run path resolves. No Mach-O file needs a newer macOS than the app's minimum, and no file holds the build folder's path. The app's executable imports nothing on the lists above. |
| `signature` | both | The signature is valid, on the app and on each Mach-O file in it; each is signed ad hoc as `bundle` says, with the hardened runtime exactly when the pins ask for it (and, when they do not, with no `library-validation` flag either), and nothing has entitlements. It runs again as the last check, on the build's copy and on the copy in `/Applications` that `install` made, so a run that wrote into either fails it. |
| `wrapper` | both | On a writable copy of the app, the wrapper runs the host through a link to it from another folder, and never a runtime beside the link; it runs on the app's profile, `mac-app`, not on `default`. Neither it nor the interpreter run with the app's flags writes anything into the copy. Every `.pyc` the build wrote is valid, so even without `-B` none would be written again: the check first removes the copy's `.pyc` of `lucille_host/__init__.py`, which each run then compiles from source. The same run without `-B` writes a new one in its place, so the check can see a write. |
| `host` | both | On the bundle made read-only (`chmod -R a-w`), the interpreter runs the host with the app's flags and nonsense in `PYTHONHOME` and `PYTHONPATH`: `--version`, through the wrapper too, `ops list`, a `roots` round trip, `pause` and `resume`, and the refusal of a home folder. The dependencies' native code loads. |
| `probe` | both | Prints what `status`, `pair status`, `pair --code-stdin` and `run` do on the runner, run as the app runs them, under a fresh HOME and under the runner's own. It gates nothing. |
| `environment` | both | A module or startup file planted where `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, the user site, `PYTHONUSERBASE` and the working folder would load it never loads with the app's flags. For each, a control run without `-I` must load it, or the check fails. `PYTHONSTARTUP` is read only by an interactive interpreter, so no run the app makes could load it, and its control runs `-i`. With `PYTHONPYCACHEPREFIX` set, which would make the importer read `.pyc` files from another tree, the interpreter run with the app's flags names no prefix; without `-I` it names it. No `.pth` file is missing from the installed `RECORD` files. |
| `readonly` | both | The bundle cannot be written; `--version`, `status` and `status --json` work from it, and the wrapper too; no file in it changed, and no `.pyc` appeared or went; then its signature still verifies. |
| `swift` | both | The app's own Swift code that runs the command line and reads its output works against the real command line, through the same calls the menu makes: the host's alias suggestion (a path that starts with `-` included) and the app's own when the host gives none it can use, and the corrections of a typed name, which folders are the whole disk (a link to one included), that the command line the app builds for them never has `--allow-home-or-root` and that the host then adds nothing, the plain sentence for each refusal code, never for the host's words, the menu's own verbs read as JSON (the version, the operation `ops enable` and `ops disable` answer with, which the switch follows, a folder added and removed), the refusals that mean a change is already made (an ability already off, a folder already gone), the question asked again for a name in use, which home folder question follows a `broad_folder` refusal, the time limit and Cancel; every verb on `--profile mac-app` with `PATH=/var/empty`; and, on a throwaway profile, `status`, `pair status`, `pair --code-stdin` (which refuses a text that is not a code once it has read it from standard input) and `run` with the lifeline. Against the host's golden JSON fixtures: every object read through the app's reader. The pairing flow's rules (They Match counts only on the drawn number, after a `status` read that shows the same connection and deadline; They Differ stays; `pair status` only while the window shows), and the supervisor's (the exit of every `run` fixture, the back-off, the five-crash stop, the quit and log out steps, never resuming by itself, and Stop What's Running asking for `kill` only while `run` runs), each also walked through 40,000 seeded random steps. Set Up's (the window only after They Match here, then Connected, and never after a relaunch or a confirm in Terminal; `policy previous` read only when this run's own child for the attempt They Match confirmed wrote `policy_reset`, and offered only when set aside at or after the second that attempt started, seconds or a minute after it included; no Add back page for none, a timeout or `previous_invalid`, which gets a note; the whole disk never with a box, whatever the policy set aside marked it, nor a U+FFFD path, a folder already held, or an ability that is on, for testing, missing or lacking; a folder alias not in the host's form makes the whole object unread; a broad folder through the home question and a content-sending ability through the acknowledgement; every box unchecked, and nothing added back but a checked row with a box, once; What Lucille may do switching every operation but one for testing or lacking, `host.describe` included), walked through 40,000 seeded steps too; `policy previous --json` on a profile no pairing has used, and `roots check`, for every folder and for one. Which verbs take the host's settings lock, and that of the runs left behind only those still running whose verb takes it count as stuck. Activity's request log: one entry per request from the `request` fixture, each said in words, with its path quoted, cut, or not shown at all; a request closed as stopped when `run` ends; one written again after a restart kept in its entry, and a job id used again after its request ended given an entry of its own; at most 500, a running one kept; a path that imitates the app's own words behind a quote look-alike (U+201E, U+FF02, U+2033, U+2036) not shown, and every character of the list refused, while a straight quote and apostrophe are still shown. Reset Settings, against the real host in a temporary HOME: a settings file it cannot read is refused in words that offer Reset Settings…, `policy reset --json` sets it aside as `policy.invalid.toml` and leaves only `host.describe` on, and a second reset is `not_invalid`, which is done; the note for each way a reset can end (set aside, nothing to set aside, the lock refused, another refusal, a timeout with and without a run left stuck, a run the app gave up on, a cancel, a signal, another exit status, an exit with no `policy_reset`, a helper program that cannot start), of which only the first says the settings were reset; and a run cancelled or timed out after the host wrote `policy_reset` read as a reset. Where a copy may run, with a made-up name: only `/Applications/<name>.app`, case included, and only when the path it was opened from and the one its links lead to are both that; a copy in Applications under another name, or the same name in another case, whether it was opened by that name or reached through a link at the right one, is a renamed one, its name shown without its control and format characters and cut to 40; a disk image, a folder inside Applications, the Applications folder in the home folder, Downloads, an App Translocation folder, and a link at the right place to a copy elsewhere are each refused for where they are; a bundle with no name is never placed; every refusal has words, and a renamed copy is told to go to the Trash only while another copy of the app, whose `Info.plist` gives the same bundle identifier and which is not itself through a link or another case, is at the app's own name (not a folder with no `Info.plist`, nor another app's). The update notice's words: the version Lucille asks for, where to get the newer one, Prepare to Update, and no Download; a minimum read only as three numbers of at most 9 digits with no leading zero. Before the latch is opened, neither a short verb nor a long-lived child starts, and a stand-in interpreter never runs; a placed copy's token opens it, and that is the first thing the self-test does before it starts anything. The child runner, against stand-in children: a signal read as a signal, the lifeline ending a stand-in `run` while a later child lives, the code on standard input and never in argv, input too long for the pipe refused, lines too long or ending in a carriage return dropped, a burst of 5000 events. `macos-app` compiles the self-test once, with the pinned Xcode, and the smoke jobs run that same program. |
| `relocate` | `macos-app` | A copy of the app in another folder, with a space in its path, passes `host` and `swift`. |
| `install` | both | A copy of the read-only bundle is put at `/Applications/Capsule.app`, made read-only in its turn, and passes `readonly` there (M8). `placement`, `launch`, `ui` and `pairing` open that copy, the only one that starts anything, and refuse to run unless it is this build: its executable and its signature's list of files are byte for byte the build's. |
| `places` | both | The two System Settings pages the app's one opener may open, each opened by the check with `/usr/bin/open`, not by the app: System Settings starts, and shows a window named "Date & Time" or "Files & Folders". The check prints the names of System Settings' windows and takes a screenshot of each, then quits System Settings by its name; it sends it no key. It quits System Settings before each page too, and says so when it was running before the check. `/usr/bin/open` is given 60 seconds, and the step's limit covers the check's own worst case for both pages. It needs no app. In `macos-app` it is a gate; the smoke jobs run it with `--record-only`, which prints and fails nothing, since neither the addresses nor the titles are yet seen on a Mac. |
| `placement` | both | First the control: the copy in `/Applications`, opened, starts the host on the app's profile, which the check sees, and says where its menu is (while the profile holds no confirmed connection; with one, it says neither). Then, one at a time with no other copy running, the build's own copy, and a copy named `Capsule 2.app` in `/Applications`, with `/Applications/Capsule.app` there and then moved aside: each says "Capsule needs to be in your Applications folder" or "This copy is named Capsule 2, so it can't run", with the words for where it is (to the Trash only while the other copy is there; to be renamed while it is not), starts no host process of the app's profile for 10 seconds, and changes nothing in the profile's folder. The build's own copy offers only Show Applications Folder and Quit, in either order (the check prints the order System Events gives, which is not yet seen on a Mac); Show Applications Folder quits it (where it does not, the check prints what the note then says), and the Finder then shows a window named Applications, whose name the check prints and, in `macos-app` only, requires (it is not yet seen on a Mac; one of that name open before proves nothing, and the check says so); the check closes each Finder window under a name that was not open before the press, and leaves those open before (and, where it could not read them before the press, closes none). The renamed copy offers only Quit, and quits when Quit is pressed. Whatever happens, the renamed copy is removed and the other put back. Reading the notes needs System Events, as `ui` does: `macos-app` requires it (`--require-driven`); the smoke jobs record whether they could, and watch the host processes and the profile's folder either way. How the check looks for a host process of the app's profile, whose command line has `lucille_host --profile mac-app` followed by a space or its end (a pattern with no backslash, which Linux's `pgrep` reads, seen, and Apple's is not yet seen to; the test suite fails where `pgrep` reads the process table with a plain pattern but not with this one): two listers, `pgrep -f` and `ps`, each on a thread of its own and called again 0.05 seconds after its last call ended, so that one held never holds back the other. A lister that fails, writes on standard error, gives something that is not its list, or takes more than 5 seconds is blind, and is never read as having seen none; the same holds for the `pgrep -x` that tells the check whether the app runs. A lister reads the process table somewhere within its call, so a stretch with no reading runs from the start of one reading to the end of the next. A refusing copy's 10 seconds count from its watch's first call, after the check has seen the copy run (it asks every half second, and that wait is not counted). They pass only if `ps` read the process table at least once, and no stretch with no reading was longer than 2 seconds; otherwise the check fails once, says when it was blind and why, and does not judge whether the copy started anything, unless a reading saw a host process or a child of the copy, or that the copy no longer runs. `ps` is needed because Apple's `pgrep -f` matches a process's name instead where sysmond gives no command line for it, and may then find none and say nothing. Each stretch with no reading longer than 0.35 seconds is printed on a line of its own, with its own length, recorded, not a gate; in a refusing copy's watch, one longer than 2 seconds is printed instead where it fails the watch. With both listers slow at once, a stretch runs up to about twice a call's time and the 0.05 seconds between calls. The 2 seconds are set from run 42681859860, this check's first run on all three images. With no lister error there, the longest stretch with no reading was 0.30 to 0.37 seconds (macos-15), 0.24 to 0.30 (macos-26) and 0.32 to 0.41 (xcode-27) in the refusing copies' watches, and 0.29, 0.36 and 0.95 seconds in the controls', where a call took up to 0.71 seconds (xcode-27) while the app launched; a limit of 0.35 seconds, which this check had before, failed two refusing copies (`renamed-alone`, on macos-15 and xcode-27) in whose watches no reading saw a host process or a child; that they started nothing is inferred. Two seconds is more than twice the longest, 0.95. What the limit no longer proves: a burst of host processes shorter than 2 seconds, between two readings, could be missed. In that run the app's launch, from the first host process a lister saw to when the check saw the end of its first status read, took 0.38, 0.50 and 0.95 seconds (macos-15, macos-26, xcode-27), well under 2 seconds. So what a refusing copy's watch shows against a launch is its own line, not the limit: its lister errors, and its longest stretch with no reading by `ps` alone, which the line gives, not as a gate. A host process that ran inside the watch for longer than that stretch had a whole `ps` reading inside its run, by how stretches are counted (a test holds this), and that reading would have counted it, as a child of the copy if not by its command line (inferred from how `ps` lists the process table; not yet seen in a refusing copy's watch). A shorter one could be missed. The longest stretch over both listers says less: it counts `pgrep`'s readings too, and a `pgrep` reading of none can be wrong (above). In a refusing copy's watch, a child of a running copy of the app counts as started, whatever its command line shows; the waits for no host process to run count only host processes, since the app is stopped first. Each watch prints how many calls each lister made, its longest call, its longest stretch with no reading, its longest with no reading by `ps` alone (not a gate), and its lister errors. The control's gate is 30 seconds from its watch's first call, and only a host process that a lister matches by its command line passes it; a child of the app whose command line does not match is recorded, with when it was first seen, and does not count. The control opens the copy with `open --stdout --stderr`, which keeps the app's standard output and error (`control-app-stdout.txt`, `control-app-stderr.txt`, whose last 5000 characters it prints). The failures below came with a plain `open`; whether this launch changes how often they come is not known. It prints when each lister first saw a host process, when the app wrote `login item: ` (once its first status read has ended, with a confirmed connection or with no pair), and whether `/bin/ps` and `/usr/bin/pgrep` link libsysmon. After a passing gate it watches up to 5 seconds more, until both listers have seen a host process, the app has written that line and each lister's last reading saw none. Its line then records, not as a gate, when each lister last saw a host process and the burst from the first sighting to the last, so that the 2 seconds can be checked against real launches (with a host that runs on, as `run` does while the profile holds a confirmed connection, the burst is only as long as the watch, and the line says so); it prints each stretch with no reading longer than 0.35 seconds, and `note: the control passed, but` for a lister that never saw one or was blind, so that passing runs too can show whether one lister misses what the other sees. With no host process seen 5 seconds after the open, it takes a screenshot (`0-placement-control-5s.png`), lists the app's processes and their children (`control-ps.txt`) and samples the app for a second (`control-sample.txt`), without pausing the listers. When the gate fails, it watches on to 60 seconds, for the record only, saves the unified log (`control-log.txt`), and names the case: a child of the app was seen inside the gate, but no host process the gate matches; the app's first status read had ended inside the gate, but no reading saw a host process; the first host process, or the end of the first status read, came only after the gate; or none of these by the end of the watch. Four smoke jobs failed the control with no host process seen in 30 seconds, though the check then read the app's note 33.5 to 34.6 seconds after its `open starts` line: 128519100657 (macOS 27, 2026-11-07), 128825202511 (macOS 27, 2026-11-08), 129217473371 (macOS 27, 2026-11-09) and 129292750857 (macOS 26, 2026-11-09). Their logs cannot tell apart listers that missed the app's first host processes from a start of the app's host held for about 30 seconds; both are inferred. The 30 seconds stay until a failure with this record says which; the notes of passing runs can point to the first. A longer wait would not help if the listers were blind, since an app with no connection starts no host process after its first few until a menu opens (inferred from the code). And nothing here allows such a hold: the note waits for the first status read (see [What the app does when it opens](#what-the-app-does-when-it-opens)). |
| `launch` | both | The copy in `/Applications` opens the way Finder opens it, and is still running 10 seconds later. SIGTERM then ends it within 15 seconds, though nothing has closed its note about the menu bar. A copy still running from an earlier step fails this check and the `ui` and `pairing` checks, and is then killed. |
| `ui` | both | The copy in `/Applications`, driven through System Events as a user would: the app says what it is for and where its menu is when it opens with no confirmed connection (with one, it says nothing about it, and the check presses nothing), and again when it is opened while it runs; the menu bar item's title or description, which VoiceOver reads as its name, is "Capsule" (its tooltip does not count); the menu shows the version, "Not connected to Lucille" and every item, and lists abilities in plain words, each with its sentence on what Lucille gets; Pause Lucille and Resume Lucille change the host's pause flag, the status line and the menu bar item's name; an ability switches on, shows a checkmark, and switches off; Add a Folder… goes through the Open panel and the name dialog, where a typed name the host would refuse comes back corrected, and adds the folder; Remove in the Folders submenu removes it; the home folder brings up the question first, Choose a Smaller Folder goes back to the Open panel, and Add Anyway adds it; the Data volume, then `/` through a link to it, each bring up the note that refuses the whole disk, with Choose a Smaller Folder, which goes back to the Open panel, and Cancel, and no button that adds the folder, and neither is added (that the note has no other button is checked in its source by the build tests). The Open panel's buttons are looked for in every window of the app, and in each sheet and group in it, since where they sit differs between macOS versions; where its prompt is nowhere, Return, its default button, chooses the folder, and what the app shows next is checked as before. Then Activity…: its window says what it keeps and for how long, its one button is Show the Host's Log, and that shows the host's own log lines as it wrote them; its close button closes it (that nothing in it can be selected is checked in its source by the build tests). Then the pairing window: with a code-shaped text on the pasteboard, Connect to Lucille… opens it with its field empty, and the field stays empty once it has the focus; a typed word gets the hint, and Connect stays off; Command-V pastes exactly the text copied; Connect hands it to the host, which refuses it as no code, and the app says so; nothing is connected, and neither text reaches the app's standard output or error. Then Reset Settings: the check writes a settings file the host cannot read; the menu says so, its note offers Reset Settings…, which asks first, and the question's Reset Settings sets the file aside as `policy.invalid.toml`, leaves only `host.describe` on and no folder, and says "Settings reset", whose OK runs nothing more; the settings file, and any file an earlier reset set aside, are put back as they were afterwards, whatever happened. Last, best effort and never a gate, a folder in Documents: the check makes one, in a child with a time limit, and adds it, typing its path only once it has read the Open panel's own button; if macOS asks, it prints the question and its buttons and, where the question names Capsule or its interpreter and is the one window of the process that draws it, answers Don't Allow, reads that the app says macOS is blocking Capsule from the folder, presses Open Privacy Settings and prints what System Settings shows; if macOS does not ask, it prints "no macOS prompt on this runner". Then, each on its own: where the app still checks the folder it presses Capsule's Cancel, and it ends any `roots add` of the app's profile still running 30 seconds later, so that none holds the profile's lock for the checks after it; it prints what `UserNotificationCenter` still shows, and says when a question is left on screen, which it closes no other way; it quits System Settings if it opened it, removes the folder, and asks `tccutil reset` to take back the answer macOS asked for, printing what tccutil said. It ends, its clean-up included, inside the ui step's limit, counted from the check's start (`UI_STEP_LIMIT`, which `tests/test_ci_workflow.py` holds to the workflow's), and prints whatever of its clean-up it then leaves undone. Each step but that last is checked against what the host's command line then reports, on the app's profile. In `macos-app` (`--require-driven`), a machine where System Events cannot drive the app fails the check; the smoke jobs record whether it could, and where it could, a driven step that fails fails the check there too. Screenshots are kept in the job's logs artifact. |
| `library-snapshot`, `library-diff` | both | `~/Library` is walked before `launch` and after `ui`, and in `macos-app` again before and after `pairing`, so that what Set Up and registering the login item write under `~/Library` is held to it too (macOS keeps the item's own record elsewhere, believed to be outside `~/Library`, which no diff sees; that diff makes the profile's writes itself first, since `pairing` puts the profile back as it found it). A new, changed or removed path whose name holds the bundle identifier, the app's name or "lucille", and that [`tools/macos/library-allowlist.txt`](../tools/macos/library-allowlist.txt) does not allow, fails the check; every other change is printed. The state folder of the app's profile, `mac-app`, must be among the changes (in the smoke jobs, the check makes those writes itself first), or the check fails. It skips `~/Library/Developer` and `~/Library/Android`, and cannot see a write to a path whose name says nothing of the app. The snapshots are never uploaded. |
| `gatekeeper` | `macos-app` | Prints `spctl`'s verdict. It is "rejected", as expected for an ad hoc signature, and does not fail the build. |
| `dmg` | both | Exactly one disk image was built, its name ends `-adhoc-macOS-arm64.dmg`, and it verifies, mounts, and holds the app, with a valid signature, and a link to `/Applications`. |
| `startup` | `macos-app` | The median and the fastest of ten runs of `--version` and of `status --json` from the bundle as shipped, with its `.pyc` files; then from a writable copy with every `.pyc` removed. Written to the job summary; not a gate. |
| `suite` | `macos-app` | The host's whole test suite passes on a copy of the app's runtime, with the test dependencies installed into the copy only, by hash, and the host's wheel and agent-switchboard installed again by `MANIFEST.json`'s sha256, so that their commands sit beside the copy's interpreter. |
| `pairing` | `macos-app` | It needs Switchboard's hub and the test harness, so it runs with the interpreter of the copy of the runtime that `suite` made, after it; the smoke jobs have no such copy, and do not run it. With a hub of its own and a stand-in for Lucille, a test harness connects the app's profile through the command line (`pair --code-stdin` with `--hub` and `--store-file`, which the app never passes), leaving the number to confirm. Then, driven through System Events: Finish Connecting… shows Lucille's number, once and no other; Return and Space answer nothing; They Match confirms. `run` is the app's child, on the app's profile, with a pipe on its standard input; a second `run` of the profile is refused (exit 21); `run` reads the work room Lucille offers, and the menu says "Connected to Lucille"; the app itself holds no network connection. The Set Up window then opens by itself, within 60 seconds, with no Add back page (the harness paired, so the app saw no `policy_reset`), on Folders, which lists none; on What Lucille may do, with `files.sha256` off (`ops list --json` says so), the switch "Check a file's fingerprint" shows it off, turns it on (`ops list --json` says it is enabled, and the switch shows it on), and off again; Ready, whose box "Open Capsule when you log in" is checked and which says macOS will show a notice; and Done closes the window. What macOS answers an ad hoc build is not known, so after Done the check passes on any state, as long as the app says on standard error which one it was (`login item: enabled`, `requiresApproval`, `notRegistered` or `notFound`) and its words agree: no note while enabled; "macOS needs your OK…", closed with Not Now, while it waits; "Capsule couldn't turn on opening at login", closed with OK, otherwise. Open Login Items Settings is never pressed. `sudo -n sfltool dumpbtm`'s records that name the app are printed, never checked. A Set Up check that fails does not stop the pairing check: the window and any note about opening at login are closed and `files.sha256` turned off, and the steps after it run. Every later launch is connected: the app shows no note about the menu bar (one that shows is counted, then closed with OK, so the later steps still run), and the check presses nothing it has not seen; it says on standard error which state macOS gives the login item, and, only while it waits for your OK, "Capsule won't start when you log in", where the check chooses Keep It Off, which must close that note (left up, it would hold every later note behind it, so the check stops there) and leave the item `notRegistered` or `notFound`. A folder added through the command line, then a `files.sha256` request on it while that ability is off: the host turns it away, and Activity says "Turned away: that ability is off on this Mac." with no button but its disclosure (M18). Then `files.sha256` turned on through the command line, and a request like it, answered with the file's fingerprint, which Activity says finished. `run` killed with SIGKILL while the app's note about the menu bar is up is started again before the note is closed; the app killed with SIGKILL leaves `run` to finish and exit. Quit, chosen in that note during a 20-second request, asks first, shows the finishing panel with Stop now, and quits once `run` has finished and drained. Opened again, while no request runs, the menu does not offer Stop What's Running; during a longer request it does, and choosing it tells Lucille the request failed (`killed_locally`), says "Stopped, and paused. Resume when you're ready.", and pauses the host while `run` goes on; ten seconds later the host is still paused, and Resume Lucille, from the menu, resumes it. Opened again, Lucille asks for a release newer than this one (999.0.0, which a development build is older than): the host refuses the request `host_too_old`, and the app says "Lucille needs Capsule 999.0.0 or newer", where to get it in words, and offers only Not Now and Prepare to Update; a second request asking for 999.0.1 while that note is up brings no second note; Not Now closes it, and a third asking for 999.0.2 does not bring it back; the menu then holds "Lucille needs Capsule 999.0.2 or newer", which shows the note again with that version, and Prepare to Update…, which asks no Quit question, lets `run` finish and stop, and quits. Opened again, Quit then Stop now, during a longer request, tells Lucille the request failed (`killed_locally`) and pauses the host; the next launch says it is paused, and it stays paused until Resume. Disconnect lets `run` finish, then forgets the connection, and says Lucille was told; its note adds "Capsule won't open when you log in." exactly when the app had said the login item was on or waiting for your OK, and the app then says `login item: notRegistered` (or `notFound`). While connected, the menu holds Settings, and once disconnected it does not. It drives the copy in `/Applications`. The profile is set aside first and put back after, and the app's signature still verifies. Screenshots are kept with the others. |
| a second build | `macos-app` | The unsigned app is built again, in another folder, and the two tarballs are compared by sha256. The tarballs hold the `.pyc` files. The job summary says SAME, DIFFERENT or FAILED, with both digests. Not a gate. |

## What CI does not check

CI adds its folders under the home folder, outside the folders macOS protects, but for the `ui` check's
best-effort folder in Documents, and it connects through a test harness with a hub of its own. These
need a Mac and a person. Where the maintainer reported one done, in whole or in part, on their Mac on
2026-11-09, it says so; none of those reports is in a CI log:

- **Whether a person's Mac loads the native code of an ad hoc build without the hardened runtime, and
  pairs.** The maintainer reported it once, for cryptography: on their Mac on 2026-11-09, the disk image of run
  42531994220 imported cryptography's x25519 module, and paired ([Signing](#signing)). On Linux that
  import loads cffi's backend too (measured), so that it loaded there is inferred. That keyring's macOS
  backend loaded, for the pairing, is inferred too (see the Keychain, below). Neither was recorded. The
  runners loaded, under the hardened runtime, the extension a real Mac then refused, so a passing `host`
  or `pairing` check does not show it. It is to be run again on the first build after any change to
  signing.
- **A folder in Documents, Desktop or Downloads.** Whether macOS asks at all after the Open panel,
  whether its question names Capsule rather than `lucille-host-runtime` and shows Capsule's sentence on
  what it is for, and whether the folder is added after Allow. After Don't Allow, whether the app says
  that macOS is blocking it, and whether System Settings, Privacy & Security, Files & Folders then shows a
  switch under Capsule. The `ui` check tries a folder in Documents, best effort: a runner may ask
  nothing, and then the check says so.
- **A folder on an external or network drive, one in iCloud Drive, and one from another file provider.**
- **A stalled network drive.** Whether Cancel works, and whether the menu then says a folder check is
  stuck.
- **The app's own `pair --code-stdin` reaching the number against a hub.** In CI, a pasted text reaches
  the host on standard input, and the compare page, They Match, `run` and Disconnect run end to end with
  the harness answering the code. The app never passes `--hub`, so it reaches only Lucille's own hub.
  The maintainer reported it running once, on their Mac on 2026-11-09: the maintainer pasted a code from Lucille's
  Computers page, both screens showed the same number, and They Match was clicked on both. Lucille's
  page then showed the computer Connected.
- **The Keychain.** CI's harness keeps the connection in a file (`--store-file`). The app passes no
  `--store-file`, so the pairing of 2026-11-09 kept its connection in the Keychain; that is inferred from
  the code, not seen. What macOS asked about it then, if anything, was not recorded. What macOS asks
  about it on each new ad hoc build is untested.
- **Paste on macOS 26 and 27,** and whether macOS shows a paste alert there. The `macos-app` job pastes
  on macOS 15; the smoke jobs reach the code window on macOS 26 and 27 only where System Events can drive
  the app there. Paste through the Control-click menu is not driven in CI either. The maintainer reported a
  paste on macOS 26.2 working on 2026-11-09, on their Mac (above). Whether macOS showed a paste alert, and how the
  code was pasted, were not recorded.
- **They Match's click time.** CI presses They Match through accessibility, which carries no event,
  so the app takes the time of the press as now. That a real click made before the number was drawn
  is refused is checked in the self-test only. The maintainer reported a real click, made after the number
  was shown, accepted once, on their Mac on 2026-11-09 (above).
- **They Differ, then a relaunch.** That They Match stays off after They Differ holds only while
  Capsule stays open; what the window shows after a relaunch, when turning the connection down
  failed, is checked in the self-test's flow only.
- **More than one connection,** and the repair by hand ([More than one connection](#more-than-one-connection)).
- **A quit during a Disconnect,** which finishes the Disconnect first: checked in the self-test's
  supervisor only.
- **Log out, restart and shut down.** The 20-second step, how the app tells them from a plain quit, and
  that sudden and automatic termination stay off are checked in the self-test's supervisor only (see
  [Testing log out by hand](#testing-log-out-by-hand)).
- **Work from a real Lucille, sleep and wake, and closing the lid.** The maintainer reported pairing
  with a real Lucille once (above), and that the menu then said "Connected, waiting for Lucille": Lucille offers a computer no work
  room until it has a job for it, and makes no such job yet (Lucille policy as of 2026-11-09; not proven
  by this repository).
- **That no child the app starts holds `run`'s lifeline, in every order children start in.** The
  self-test shows it for one order. The one lock around every start is the argument for the rest.
- **Activity, beyond the requests CI sends.** Its words for `path_outside_root` and `host_too_old`, for a
  request dropped when its connection changed, for one stopped with `run`, for one written again after
  `run` restarted, and for a job id used again, and its 500-request bound, are checked in the self-test
  only. That each other refusal and each failure has a sentence of its own is checked as a table by the
  build tests only. Not reached by CI: "Reading the host's log…", "Capsule couldn't read the
  host's log.", Hide the Host's Log, an entry from an earlier day drawn with its date, and the first
  drawing before `ops list` answers, which names the operation in quotes. Not checked anywhere: a log
  line that holds an operation's output (the host writes one only with `log_results = true`).
- **Stop What's Running, beyond the one CI drives.** Not reached by CI: the note when it fails ("Capsule
  couldn't stop what's running." with "It may still be running…" or the host's own words), its race
  with the request's own end, where `kill` finds nothing running and the host is paused all the same,
  and the menu leaving it out while `run` drains (the self-test checks only that the supervisor asks for
  nothing then).
- **Reset Settings, beyond the one CI drives.** Not reached by CI: Reset Settings… from the Needs
  attention note while `run` runs, and a reset while a request runs; Cancel on the question, which runs
  nothing (checked in its source by the build tests); Cancel in the progress window, after which the app
  says nothing unless the host had already reset the settings or a run left stuck holds things up;
  "Nothing needed resetting"; Try Again after the host could not lock its settings; and the notes for a
  reset that took too long, that a run left stuck holds up, that the host refused for another reason,
  that a signal or another exit status ended, or whose helper program could not start. Their words are
  checked in the self-test only, and none of these screens has been seen on a Mac.
- **Where a copy of the app may run, beyond the copies CI makes.** CI makes a renamed copy with `ditto`,
  not with the Finder's Keep Both, and opens the build's own copy from a folder in the checkout. Not
  reached by CI: a copy opened from the disk image, one macOS opened from a folder of its own (App
  Translocation), one in a folder inside Applications or in the Applications folder in the home folder
  (their words and judgement are checked in the self-test only), and what the Finder says when a new
  copy is dragged over a running one. Nor is a refusing copy's menu (its one line, which shows the note
  again, and Quit Capsule), or the note it shows when it is opened again while it runs: CI reads only the
  first note, and quits through it. That a copy which is not placed starts nothing is checked in CI by
  its host processes and its profile's folder, and in the self-test by the latch.
- **Abilities and folders, beyond the ones CI drives.** Not reached by CI: the question before an ability
  whose replies can carry a file's contents is turned on (no operation in this release sends file
  contents; its words, and the rule that asks it, read from the host's fixture, are checked in the
  self-test only); the name dialog starting from the app's own suggestion when the host gives none it
  can use, or none within 10 seconds (the choice between them is checked in the self-test only);
  whether the dialog started from the host's suggestion or the app's own, which the `ui` check cannot
  tell apart, since for its folder both are `my-project-files` (that the dialog starts from the host's
  answer is checked in the source by the build tests, and the choice between the two in the self-test);
  the name dialog shown again after `alias_taken`, and the home folder question after the host refuses
  a home folder the app did not recognise from its path (each checked in the source by the build tests,
  and its rule against the real host in the self-test); and turning off an ability that is already off,
  or removing a folder that is already gone, which show no note (the rule is checked in the self-test,
  the wiring in the source by the build tests).
- **The update notice, beyond the one CI drives.** Not reached by CI: the notice's own **Prepare to
  Update** button (the pairing check presses Not Now, and then chooses Prepare to Update… in the menu);
  Prepare to Update while `run` is not running; and the notice on macOS 26 and 27, since the smoke jobs do
  not run the pairing check.
- **Screens CI does not reach**, whose words and rules only the self-test checks: They Differ; a code
  from another hub; "Capsule keeps stopping" and its Show Details; every Needs attention line (the
  clock, Lucille not reached, a message that cannot be opened, a connection the host left, the helper
  program not responding, and the exits 20, 23, 24 and 25); the log out panel; the Stop Connecting
  question.
- **The one opener, beyond what CI presses.** Not reached by CI: Open Date & Time Settings in the app's
  own note, since a runner's clock cannot be set wrong without breaking its connections; Open Privacy
  Settings where macOS asks nothing on the runner, and under a folder on Set Up's Folders page; and the
  notes that come again when macOS cannot open a place. What each System Settings page is called, and
  that its address opens it, `places` prints, and requires on macOS 15 only.
- **Set Up, beyond the path CI drives.** The Add back page end to end: the app's own `pair --code-stdin`
  cannot reach CI's hub, since the app never passes `--hub`, so CI's pairing sets no policy aside that
  the app saw. Its rules and what it leaves out are checked in the self-test only, and its words there
  and as source text by the build tests; none of its screens has been seen on a Mac. Not reached either:
  whether the switch on What Lucille may do then shows the host's answer rather than only the click (a
  checkbox shows its own click before the app hears of it, so CI's read of the switch after each click
  cannot tell them apart; `ops list --json` is what shows the host's answer there); whether a folder
  added back, which no Open panel chose, can be read by the host, and what macOS asks then (the Folders
  page's `roots check` says); the home folder question for a folder added back, and its Choose a Smaller
  Folder; a folder's state on the Folders page; Remove and Add a Folder… there; the note for
  `previous_invalid`; closing the window and Set Up Folders and Abilities… in the menu; Finish
  Connecting… after the pairing window was closed, which offers nothing back; and the window after a
  relaunch, where by design nothing is offered.
- **Open at Login, beyond what CI reads.** A real login, and Capsule opening at it; macOS's own notice
  that Capsule can now open when you log in; the login item turned off in System Settings, then the note
  at the next launch, "Capsule won't start when you log in", with Open Login Items Settings and Keep It
  Off, on a Mac; what Login Items & Extensions shows after the app is deleted instead of disconnected;
  what macOS answers an ad hoc build on the owner's own Mac; and the Settings menu itself: CI reads that
  the menu holds Settings while connected and not after Disconnect, but does not open it or choose Open
  at Login, so as to send no key press it cannot place, and Reset Settings… from it is checked as source
  text only. If the pairing check stops before Disconnect, the runner's login item stays registered:
  only the app can take `mainApp` away, and the runner is thrown away after the job. The
  maintainer reported that on 2026-11-09 the Settings menu showed Open at Login ticked. The menu ticks it only
  while macOS says the login item is enabled (`Connection.swift`), so macOS said so there; that is
  inferred from the code, not seen in a log. A real login, and macOS's notice, were not recorded.
- **A run left behind.** CI makes no process outlive SIGKILL (that takes a stalled network drive, which
  CI does not set up), so that only a run of a verb that takes the settings lock is counted as stuck is
  checked through the rule and the count the self-test runs (`HostCLI.takesPolicyLock`,
  `HostCLI.lockHolders`) and the wiring the build tests read, not with a live stuck run.
- **VoiceOver.** Whether it reads the menu bar item as "Capsule", and "Capsule, paused".
- **A full menu bar, or one beside a notch,** where macOS hides the icon.
- **How the icons look.** The app icon in the Finder, on the disk image, in the app's notes, in Login
  Items and in macOS's own questions; whether macOS 26 and 27 draw it inside a tile of their own; and
  the menu bar glyph at 1x and at 2x, in a light and a dark menu bar. Where it can drive the app, the
  `ui` check takes a screenshot of the screen, `1-menu-bar.png`, that should show the glyph; nothing
  reads what it shows. The `bundle` check has AppKit load the glyphs from the bundle, but the running
  app's own lookup is not checked. `bundle` also has `iconutil` read `AppIcon.icns`, and fails unless it
  writes exactly the ten files the entries' slots name, each a PNG of its name's size: a gate in
  `macos-app` and on `macos-26`; on `xcode-27` it runs too, but that job does not fail the workflow run.
  No run has yet held `iconutil`'s files to that rule, so the sizes it writes are not yet seen. Nor is
  whether `iconutil` keeps an entry's own size or scales it to its slot; if it scales, a file of the
  right size says nothing of its entry, and the build's slot rule (`icons.slot_problems`) is what holds
  each entry to its slot. That the Finder, and macOS's other places for the app icon, read the file into
  the same slots as `iconutil` is inferred, not checked. A public note on the format reports that macOS
  draws PNG `icp4` and `icp5` entries scrambled in an app's icon (Wikipedia, "Apple Icon Image
  format", its list of known issues); the 16 and 32 point icons are the ones to look at for that. macOS
  may also keep showing an earlier icon for a while, from its own cache.

These two need no person, only a CI run in which it happens:

- **A real `diskimages-helper` holding the file `hdiutil create` wrote.** On macos-latest, and in the
  `suite` check on macos-15, a test holds that file with `flock` against the real `hdiutil` and checks
  that the build hands on a verified copy, and another records, as a pytest warning, what `hdiutil
  verify` answers for a file held so. Whether the helper's own hold is of that kind has not been seen.
  The build's line after `create` says whether another process holds a lock on the file that `flock(2)`
  sees. "has no lock on it" does not rule out a hold of another kind, or an `fcntl` lock, if macOS does
  not make the two conflict (not checked).
- **A helper left holding the copy.** A `diskimages-helper` left by the passing `verify` of the copy
  would hold the file the build hands on, and `check_app.py dmg`'s `verify` and `attach` of it would be
  refused as before (inferred, not seen). Against it: in each of the 78 CI builds from 2026-10-29 to
  2026-11-09 that saw no refusal, `dmg` verified and mounted, minutes later, the very file the build's
  own `verify` had passed (in run 42522633104, 5.5 minutes later).

### Testing log out by hand

CI cannot log out. To try it on your own Mac, connect the app, then turn on `host.sleep` for the app's
profile. It is for testing, and the menu does not list it:

```sh
/Applications/Capsule.app/Contents/Resources/bin/lucille-host --profile mac-app ops enable host.sleep
```

The app runs only from there. The wrapper runs the bundled runtime with the app's flags
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

To check the `.pyc` files of a copy you downloaded, mount the disk image. Then, from a checkout of the
commit its `MANIFEST.json` names, run:

```sh
uv run --no-project python tools/macos/check_app.py bytecode --out /Volumes/Capsule
```

It runs the bundled interpreter and writes nothing. It cannot show that the `.py` files or the interpreter
are the ones the build used. `codesign --verify --deep --strict` shows whether any file changed since the
app was signed, and an ad hoc signature does not say who signed it. Whether macOS lets the interpreter of
a downloaded, quarantined copy run from Terminal is not yet tested on a Mac.

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
- **What comes after connecting**: About, Copy Diagnostics, and a check of each folder at launch and
  after Add a Folder… in the menu (Set Up's Folders page checks them).
- **Set Up's rough edges** (see [Set Up](#set-up)): its pages do not scroll; the next row it adds back
  does not wait for an ability's answer, nor for a panel or dialog the menu's own steps reopened; and it
  follows this run's They Match, not a connection's name.
- **Activity in words for the log.** Activity says the live `request` events in words, and shows the
  host's log as it was written: the contract has no typed history to say it in words from.
- **ripgrep.** The design bundles it as `Contents/Helpers/rg`, with its licence. The pins hold an empty
  ripgrep commit until then.
- **Updates.** There are none, and the app looks for none. A new version is a new disk image. When Lucille
  asks for a newer host, the app says so once, in one note, with where to get it in words, and offers
  Prepare to Update, which quits so that the Finder can replace the app. There is no Download button.
- **Uninstall.** Dragging the app to the Trash removes it. The host's own folder,
  `~/Library/Application Support/lucille-host`, stays, and so does a connection: choose Disconnect from
  Lucille… first.
- **Intel Macs.** The locked `cryptography` publishes no Intel macOS wheel, so the app is Apple silicon only.
- **A Welcome window and a main window.** The design has a Welcome window that leads through
  connecting, choosing a folder and turning on an ability, and a main window with the status, folders,
  abilities, activity and settings. This build has the menu, and a note that says where it is.
- **A dark or tinted app icon.** The icon's tile is cream in every appearance.

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
- **Operations meant for testing are not listed at all.** The design hides them behind "Show diagnostic
  operations"; the menu has no such item, and leaves out every operation whose `diagnostic` is true.
- **`host.describe` is not listed either.** While it is on, the menu says what it shares in the app's own
  words ("Lucille sees which abilities are on and your folders' names"), where the design shows it with
  its switch and the core's words, like every other operation.
- **When the host suggests no alias the app can use, the app suggests its own** (`App/Alias.swift`), with
  no word about it. It writes a name in another script in Latin letters where macOS can, where the host
  would suggest `folder`. The design has the app ask the core only.
- **The app asks about the home folder before the user names it**, when it can tell from the path (the
  home folder, or a folder above it). The host still decides; when it refuses a folder the app did not
  recognise, such as `/System/Volumes/Data/Users`, which holds the home folder through the Data volume,
  the app asks then, with the question for what the host says the folder is.
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
- **Pausing shows no note.** The design confirms Pause with "Paused. Lucille's requests will be turned
  away until you resume."; this build says so in the menu, under the status line, while paused.
- **A name in a script macOS cannot write in Latin letters gives `folder`**, and on a system whose
  Foundation lacks ICU's transforms (the Swift self-test does not run on one today) every non-Latin name
  does.
- **The app says where its menu is each time it opens while this Mac is not connected,** and each time
  it is opened again while it runs. It keeps no settings of its own, so it cannot tell a first launch
  from a later one: a connected launch, which may be at login, shows no note.
- **Open at Login is in a Settings submenu, shown while connected**, with Reset Settings… while the
  settings file can't be read. Disconnect from Lucille… stays in the main menu, where decisions/0002 §14
  puts it under Settings.
- **The login note at launch offers Open Login Items Settings and Keep It Off,** where decisions/0002
  §3.3 had Turn Back On: macOS waits for your OK in System Settings, which only you can give there, and
  Keep It Off takes the item away, so the app need keep nothing to stop asking (that taking it away
  ends macOS's wait for your OK is not yet tested on a Mac).
- **The Mac's gate is a click, as decisions/0001 has it.** The app shows the number, and you click They
  Match or They Differ. The design's typed number (§11.2) is a proposal; the maintainer decided on
  2026-11-02 to build 0001's gate now.
- **The note about a pause at launch names every cause**: "You paused Capsule or chose Stop What's
  Running, or a task was stopped when Capsule quit or you logged out." The pause flag holds only a time,
  and the app keeps nothing of its own, so it cannot tell which.
- **Quit asks first only while `run` runs, and only the app's own Quit.** A quit from macOS or another
  app, log out included, never asks.
- **Needs attention shows in the menu and in the icon**, and its note opens only when its line is chosen.
- **When `run` gives no `state` for two minutes, the app only says so.** It neither restarts it nor
  closes the lifeline.
- **An exit whose code does not fit its `stopped` reason is a crash**, and so is `failed` `store_error`
  after `ready`, where the design restarts after that without counting it: a store that fails after
  every start would otherwise restart `run` forever.
- **Disconnect lets `run` finish first**, then runs `forget`.
- **Reset Settings… is a button in the note behind the line that says the settings file can't be read**,
  not an item of a Settings submenu, as the design has it: this build has no Settings submenu yet.
