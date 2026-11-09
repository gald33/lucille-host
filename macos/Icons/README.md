# Capsule's icons

This folder holds the app icon and the menu bar icon, as SVG masters and as the PNGs the build reads.
The maintainer chose this icon on 2026-11-09.

## What the icon shows

A capsule lies on its side, made of two halves that fit together, with a visible seam between them. The
ink half on the left is the person's Mac. It is the larger half, and it holds a socket. The orange half on
the right is Lucille. It reaches across the seam only through a short tongue that fits that socket. The
app icon draws the capsule on a cream tile.

The menu bar icon is the same capsule as a template glyph: black on transparent, which macOS is meant to
tint to suit the menu bar. While Lucille is paused, a pause badge sits at its lower right. While
something needs attention, a badge with an exclamation mark sits at its upper right. No glyph has both
badges: while something needs attention, the exclamation mark takes the pause badge's place, and the
item's name and the menu still say paused. Each badge cuts a thin ring out of the capsule, so the
capsule does not move between states.

## The files

| File | What it is |
|---|---|
| `app.svg` | The app icon's master, 1024 pixels square. |
| `app-16.svg` | A master for 16 pixels, drawn by hand, with the capsule on whole pixels. |
| `app-16.png` | From `app-16.svg`. |
| `app-32.png` to `app-1024.png` | From `app.svg`: 32, 64, 128, 256, 512 and 1024 pixels square. |
| `menubar.svg`, `menubar-paused.svg`, `menubar-attention.svg` | The menu bar icon's masters, 18 points square. |
| `MenuBarTemplate.png`, `MenuBarTemplate@2x.png` | From `menubar.svg`, 18 and 36 pixels. |
| `MenuBarPausedTemplate.png`, `MenuBarPausedTemplate@2x.png` | From `menubar-paused.svg`. |
| `MenuBarAttentionTemplate.png`, `MenuBarAttentionTemplate@2x.png` | From `menubar-attention.svg`. |
| `render.js` | Renders the 13 PNGs from the masters. |

The PNGs are the source of truth. The build renders nothing: `tools/macos/icons.py` reads these PNGs,
packs seven of them into `AppIcon.icns`, and copies the six glyphs beside it in `Contents/Resources`.
Nothing in the build or in CI runs `render.js`.

`AppIcon.icns` holds ten images, one for each of the ten slots its entry types are read into, by the
names `iconutil` gives its files: 16, 32, 128, 256 and 512 points, each at 1x and at 2x for a Retina
screen. Each image is its slot's points times its scale in pixels, and the build refuses one that is not.
The 16 pixel image comes from `app-16.png`, the hand-drawn master. Every other size comes from `app.svg`,
so the 16 point icon on a Retina screen, 32 pixels, is the large master's. The large master's capsule
sits on a grid of 32 pixels per unit at 1024, so at 32 pixels the capsule lands on whole pixels.

`app-64.png` is the 32 point icon on a Retina screen, and nothing else. The build once also put it in an
eleventh entry, of type `icp6`. In CI (run 42620168506, on macOS 15, 26 and 27 alike), `iconutil` read
that file into eleven files: the ten slots above, and `icon_48x48.png`, by its name a 48 pixel slot
(inferred; its size was not printed). Its log names files, not entry types, so that `icp6` gave
`icon_48x48.png` is inferred too: public lists of the types give `icp6` as 48 pixels and `ic12` as the
32 point icon at 2x, and the byte counts in the log fit that
([decisions/0002](../../decisions/0002-macos-app.md) §6).

The app shows the menu bar icon with `NSImage(named:)`. A name that ends in `Template` makes macOS treat
the image as a template, and macOS loads the `@2x` file for a Retina screen.

## Rendering them again

Change a master, then render all 13 PNGs again and commit them with it. Install Playwright in a folder
outside this checkout, with the Chromium it drives, and point `NODE_PATH` at its `node_modules`:

```sh
(cd /path/outside/the/checkout && npm install playwright@1.56.1 && npx playwright install chromium)
NODE_PATH=/path/outside/the/checkout/node_modules node macos/Icons/render.js macos/Icons
```

`npx playwright install chromium` fetches the Chromium that Playwright drives. Installing the package may
not fetch it by itself (not checked).

Do not run `npm install` inside the checkout. The guards read untracked files too
(`tools/guards/_common.py`), so a `node_modules` folder here would be checked as if it were part of the
repository.

The committed PNGs were rendered with Playwright 1.56.1 and its Chromium 141.0.7390.37, on Linux. Another
version may give other bytes. No test holds the bytes: the tests and the build refuse new PNGs only where
one breaks a rule below, such as a chunk other than IHDR, IDAT and IEND, a glyph pixel that is not black,
or the wrong size. New bytes that keep the rules pass, and the app's icon changes with them.

## What the tests hold

`tests/test_macos_icons.py` holds this folder, as git lists it, to exactly these 20 files. Git ignores
the `.DS_Store` the Finder may write here, so it does not count. Each PNG must be 8-bit RGBA, not
interlaced, with only the chunks IHDR, IDAT and IEND, and the size its name says. Every pixel of a glyph
must be black, with only its alpha varying, and no glyph may be empty. The tests also hold the layout of
`AppIcon.icns`, which PNG each of its images comes from, and that each image is the size of its slot. The
build refuses these files unless they pass the same rules, and the `bundle` check of
`tools/macos/check_app.py` holds the built app to them. On a Mac, that check also has `iconutil` read
`AppIcon.icns` into exactly the ten files of its slots, each a PNG of the size its name says, and has
AppKit load each glyph from the built app by its name, as two files of 18 and 36 pixels. Both ran in CI
in run 42620168506, on macos-15 and in the smoke jobs on macOS 26 and 27, before `iconutil`'s files were
checked. No run has yet held `iconutil`'s files to their names and sizes.

## Not done

No dark or tinted app icon is designed. The tile is cream in every appearance. How the icons look on a Mac
is not yet seen: see "What CI does not check" in [`macos/README.md`](../README.md).
