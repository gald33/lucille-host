"""The Mac app's icons (tools/macos/icons.py and macos/Icons/): the committed PNGs, the AppIcon.icns
the build packs from them, the menu bar glyphs it copies beside it, and the checks the build and the
``bundle`` check make on them. All of it runs anywhere: the build renders nothing and runs no
program to make the icons.
"""

from __future__ import annotations

import hashlib
import plistlib
import shutil
import stat
import struct
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest

from tools.guards._common import REPO_ROOT, repo_files
from tools.macos import build_app, icons

SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Everything macos/Icons holds.
ICON_FILES = {
    "README.md",
    "render.js",
    "app.svg",
    "app-16.svg",
    "menubar.svg",
    "menubar-paused.svg",
    "menubar-attention.svg",
    "app-16.png",
    "app-32.png",
    "app-64.png",
    "app-128.png",
    "app-256.png",
    "app-512.png",
    "app-1024.png",
    "MenuBarTemplate.png",
    "MenuBarTemplate@2x.png",
    "MenuBarPausedTemplate.png",
    "MenuBarPausedTemplate@2x.png",
    "MenuBarAttentionTemplate.png",
    "MenuBarAttentionTemplate@2x.png",
}
APP_SIZES = (16, 32, 64, 128, 256, 512, 1024)
# The menu bar glyphs, in the order the app picks them: running, paused, needs attention.
GLYPHS = ("MenuBarTemplate", "MenuBarPausedTemplate", "MenuBarAttentionTemplate")
GLYPH_FILES = (
    ("MenuBarTemplate.png", 18),
    ("MenuBarTemplate@2x.png", 36),
    ("MenuBarPausedTemplate.png", 18),
    ("MenuBarPausedTemplate@2x.png", 36),
    ("MenuBarAttentionTemplate.png", 18),
    ("MenuBarAttentionTemplate@2x.png", 36),
)
# AppIcon.icns, entry by entry: its type, its pixels, and the PNG it is made from. The 16 pixel
# image is the hand-drawn master, in is32 (24-bit RGB) and s8mk (its 8-bit mask); the 32 pixel one
# is in il32 and l8mk. Every other entry is a whole PNG. The 16 point entry for Retina screens,
# ic11, is the 32 from app.svg. There is no PNG icp4, icp5 or icp6: macOS is reported to draw PNG
# data in those types scrambled in an app's icon (Wikipedia, "Apple Icon Image format", "Known
# issues", item 3, read 2026-11-09).
ICNS_TABLE = (
    ("is32", 16, "app-16.png"),
    ("s8mk", 16, "app-16.png"),
    ("il32", 32, "app-32.png"),
    ("l8mk", 32, "app-32.png"),
    ("ic07", 128, "app-128.png"),
    ("ic08", 256, "app-256.png"),
    ("ic09", 512, "app-512.png"),
    ("ic10", 1024, "app-1024.png"),
    ("ic11", 32, "app-32.png"),
    ("ic12", 64, "app-64.png"),
    ("ic13", 256, "app-256.png"),
    ("ic14", 512, "app-512.png"),
)
# Each 24-bit RGB type, and the 8-bit mask type that goes with it.
MASKS = {"is32": "s8mk", "il32": "l8mk"}
# The slot macOS gives each type: its size in points and its scale, and the file ``iconutil -c
# iconset`` names for it, icon_<P>x<P>.png, or icon_<P>x<P>@2x.png at scale 2. Its image is points
# x scale pixels square. A mask fills no slot: it goes with its RGB entry. That iconutil names
# is32 and s8mk's file icon_16x16.png, and il32 and l8mk's icon_32x32.png, is inferred: no run has
# yet read an icon file with these types.
ICNS_SLOTS = {
    "is32": (16, 1),
    "il32": (32, 1),
    "ic07": (128, 1),
    "ic08": (256, 1),
    "ic09": (512, 1),
    "ic10": (512, 2),
    "ic11": (16, 2),
    "ic12": (32, 2),
    "ic13": (128, 2),
    "ic14": (256, 2),
}
# The slots whose image must be 24-bit RGB and a mask, not PNG: the 16 and 32 point slots at 1x.
RGB_SLOTS = ((16, 1), (32, 1))
ICONSET_NAMES = {
    "is32": "icon_16x16.png",
    "il32": "icon_32x32.png",
    "ic07": "icon_128x128.png",
    "ic08": "icon_256x256.png",
    "ic09": "icon_512x512.png",
    "ic10": "icon_512x512@2x.png",
    "ic11": "icon_16x16@2x.png",
    "ic12": "icon_32x32@2x.png",
    "ic13": "icon_128x128@2x.png",
    "ic14": "icon_256x256@2x.png",
}
# Seen: the files iconutil wrote from the icon file of PR 29's head, which held these ten types and
# icp6 (CI run 42620168506: job macos-app on macOS 15.8.2, and the smoke jobs on 26.7.2 and 27.0.1
# wrote the same), as its log lists them. The log names files, not types: which type gave which
# name is inferred. icp6 gave icon_48x48.png (inferred; the grounds are in icons.ICNS_SLOTS).
SEEN_IN_RUN_42620168506 = (
    "icon_128x128.png",
    "icon_128x128@2x.png",
    "icon_16x16.png",
    "icon_16x16@2x.png",
    "icon_256x256.png",
    "icon_256x256@2x.png",
    "icon_32x32.png",
    "icon_32x32@2x.png",
    "icon_48x48.png",
    "icon_512x512.png",
    "icon_512x512@2x.png",
)
# The files the bundle check wanted from iconutil at 8d3949d, PR 30's head, when PNG icp4 and icp5
# filled the 16 and 32 point slots at 1x, in its order. is32 and il32 fill those slots now, and the
# check wants the same ten.
ICONSET_FILES_AT_8D3949D = [
    ("icon_16x16.png", 16),
    ("icon_32x32.png", 32),
    ("icon_128x128.png", 128),
    ("icon_256x256.png", 256),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256@2x.png", 512),
]


# --- Helpers that write PNG and icns files, independent of icons.py ---------------------------


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _filtered(kind: int, row: bytes, prev: bytes, bpp: int) -> bytes:
    out = bytearray()
    for i, value in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        predictor = (0, a, b, (a + b) // 2, _paeth(a, b, c))[kind]
        out.append((value - predictor) & 0xFF)
    return bytes(out)


def _png(
    width: int,
    height: int,
    rows: list[bytes],
    *,
    filters: list[int] | None = None,
    depth: int = 8,
    colour: int = 6,
    interlace: int = 0,
    extra: bytes = b"",
    after: bytes = b"",
) -> bytes:
    """A PNG of ``rows`` (each a raw scanline), each row filtered as ``filters`` says (0 for
    all if None). ``extra`` goes between IHDR and IDAT, ``after`` after IEND."""
    bpp = max(1, {6: 4, 2: 3, 3: 1}.get(colour, 4) * depth // 8)
    filters = filters or [0] * height
    data = bytearray()
    prev = bytes(len(rows[0]))
    for kind, row in zip(filters, rows, strict=True):
        data += bytes([kind]) + _filtered(kind, row, prev, bpp)
        prev = row
    header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, interlace)
    body = _chunk(b"IHDR", header) + extra + _chunk(b"IDAT", zlib.compress(bytes(data)))
    return SIGNATURE + body + _chunk(b"IEND", b"") + after


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    assert data[:8] == SIGNATURE
    found, i = [], 8
    while i < len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        kind, body = data[i + 4 : i + 8], data[i + 8 : i + 8 + length]
        assert struct.unpack(">I", data[i + 8 + length : i + 12 + length])[0] == zlib.crc32(
            kind + body
        )
        found.append((kind, body))
        i += 12 + length
    assert i == len(data)
    return found


def _icns_entries(data: bytes) -> list[tuple[bytes, bytes]]:
    """Each entry's type and body, read by this test's own parser."""
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data)
    found, i = [], 8
    while i < len(data):
        kind = data[i : i + 4]
        (length,) = struct.unpack(">I", data[i + 4 : i + 8])
        assert length >= 8 and i + length <= len(data)
        found.append((kind, data[i + 8 : i + length]))
        i += length
    return found


def _icns(entries: list[tuple[bytes, bytes]]) -> bytes:
    body = b"".join(kind + struct.pack(">I", 8 + len(png)) + png for kind, png in entries)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def _source(name: str) -> bytes:
    return (icons.ICONS_DIR / name).read_bytes()


def _planes(name: str) -> tuple[bytes, bytes, bytes, bytes]:
    """The red, green, blue and alpha of a committed PNG, each a plane of its pixels in row
    order."""
    _, _, rows = icons.png_pixels(_source(name))
    flat = b"".join(rows)
    return flat[0::4], flat[1::4], flat[2::4], flat[3::4]


def _unpack(data: bytes, length: int, channels: int) -> list[bytes]:
    """This test's own reader of the icns run-length scheme, from the pseudocode in Wikipedia's
    "Apple Icon Image format", "Compression" (read 2026-11-09): a byte N under 0x80 is followed by
    N + 1 bytes to copy; any other is followed by one byte to repeat N - 0x80 + 3 times. Each
    channel, ``length`` bytes, is packed on its own, so no run crosses into the next one, and
    nothing follows the last."""
    out, i = [], 0
    for _ in range(channels):
        channel = bytearray()
        while len(channel) < length:
            n = data[i]
            if n < 0x80:
                assert i + 2 + n <= len(data)
                channel += data[i + 1 : i + 2 + n]
            else:
                assert i + 2 <= len(data)
                channel += data[i + 1 : i + 2] * (n - 0x80 + 3)
            i += 2 + (n if n < 0x80 else 0)
        assert len(channel) == length
        out.append(bytes(channel))
    assert i == len(data)
    return out


def _rgba(width: int, height: int, pixel: tuple[int, int, int, int]) -> list[bytes]:
    return [bytes(pixel) * width for _ in range(height)]


def _with_pixel(name: str, x: int, y: int, pixel: tuple[int, int, int, int]) -> bytes:
    """A committed glyph, written again with one pixel changed."""
    width, height, rows = icons.png_pixels(_source(name))
    changed = bytearray(rows[y])
    changed[4 * x : 4 * x + 4] = bytes(pixel)
    rows[y] = bytes(changed)
    return _png(width, height, rows)


def _visible_pixel(name: str) -> tuple[int, int]:
    width, height, rows = icons.png_pixels(_source(name))
    for y, row in enumerate(rows):
        for x in range(width):
            if row[4 * x + 3] == 255:
                return x, y
    raise AssertionError(f"{name} shows no opaque pixel")


# --- The committed files ---------------------------------------------------------------------


def test_the_folder_holds_exactly_the_icon_files() -> None:
    # The folder as git lists it, as the guards read it: what is tracked, and what is new and not
    # ignored. So the .DS_Store the Finder may write here when a person opens the folder, which
    # .gitignore ignores, is not counted, and cannot be added by mistake.
    assert icons.ICONS_DIR == build_app.MACOS_DIR / "Icons"
    folder = icons.ICONS_DIR.relative_to(REPO_ROOT).as_posix() + "/"
    found = [
        name
        for name in repo_files(REPO_ROOT, untracked=True, symlinks=True)
        if name.startswith(folder)
    ]
    assert {name.removeprefix(folder) for name in found} == ICON_FILES
    assert all(
        (REPO_ROOT / name).is_file() and not (REPO_ROOT / name).is_symlink() for name in found
    )
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".DS_Store" in ignored
    assert icons.source_problems() == []


@pytest.mark.parametrize("size", APP_SIZES)
def test_each_app_png_is_rgba_of_its_size_with_no_other_chunk(size: int) -> None:
    data = _source(f"app-{size}.png")
    chunks = _chunks(data)
    kinds = [kind for kind, _ in chunks]
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    assert set(kinds[1:-1]) == {b"IDAT"}
    # Width, height, 8 bits, RGBA (6), compression 0, filter 0, not interlaced.
    assert struct.unpack(">IIBBBBB", chunks[0][1]) == (size, size, 8, 6, 0, 0, 0)
    assert icons.png_header(data) == (size, size)
    assert icons.APP_SIZES == APP_SIZES


@pytest.mark.parametrize(("name", "size"), GLYPH_FILES)
def test_each_glyph_is_black_with_alpha_only_and_not_empty(name: str, size: int) -> None:
    assert icons.GLYPHS == GLYPHS
    assert icons.GLYPH_FILES == GLYPH_FILES
    assert icons.GLYPH_POINTS == 18
    data = _source(name)
    assert {kind for kind, _ in _chunks(data)} == {b"IHDR", b"IDAT", b"IEND"}
    width, height, rows = icons.png_pixels(data)
    assert (width, height) == (size, size)
    pixels = [row[i : i + 4] for row in rows for i in range(0, len(row), 4)]
    assert len(pixels) == size * size
    assert all(pixel[:3] == b"\0\0\0" for pixel in pixels)
    assert any(pixel[3] == 255 for pixel in pixels)
    assert any(0 < pixel[3] < 255 for pixel in pixels)
    assert icons.glyph_problems(name, data) == []


def test_the_icon_file_has_twelve_entries_in_order_from_the_committed_pngs() -> None:
    assert icons.APP_ICON == "AppIcon" and icons.ICNS_FILE == "AppIcon.icns"
    # Written out here, so that a change to ICNS_ENTRIES or ICNS_MASKS fails this test.
    assert icons.ICNS_ENTRIES == ICNS_TABLE
    assert icons.ICNS_MASKS == MASKS
    assert len({source for _, _, source in ICNS_TABLE}) == 7
    masks = set(MASKS.values())
    for kind, _, _ in ICNS_TABLE:
        assert icons.entry_format(kind) == (
            "rgb" if kind in MASKS else "mask" if kind in masks else "png"
        ), kind
    data = icons.icns_bytes()
    assert data == icons.icns_bytes()
    entries = _icns_entries(data)
    assert len(entries) == 12
    assert [kind.decode("ascii") for kind, _ in entries] == [kind for kind, _, _ in ICNS_TABLE]
    for (kind, pixels, source), (_, body) in zip(ICNS_TABLE, entries, strict=True):
        if kind in MASKS:
            # 24-bit RGB: the red of every pixel, then the green, then the blue, each channel
            # packed on its own, with no header (only it32 has one), and the colour as the PNG
            # holds it, not multiplied by its alpha. This test's own reader unpacks it.
            red, green, blue, _ = _planes(source)
            assert not body.startswith(SIGNATURE), kind
            assert _unpack(body, pixels * pixels, 3) == [red, green, blue], kind
            assert icons.unpack_channels(body, pixels * pixels, 3) == [red, green, blue], kind
            # Packed, and not as long as unpacked RGB, which a reader might take it for.
            assert len(body) < 3 * pixels * pixels, kind
            # Nor starting with four zero bytes, which libicns and the Rust icns crate skip at the
            # start of any 24-bit RGB entry (seen in their source, read 2026-11-09).
            assert not body.startswith(bytes(4)), kind
        elif kind in masks:
            # The mask: each pixel's alpha, in row order, not packed.
            assert body == _planes(source)[3], kind
            assert len(body) == pixels * pixels, kind
        else:
            assert body == _source(source), kind
            assert struct.unpack(">II", body[16:24]) == (pixels, pixels), kind
    by_kind = {kind.decode("ascii"): body for kind, body in entries}
    # The hand-drawn master is in is32 and s8mk, and in no entry as a PNG. app-32.png is a PNG in
    # one entry, ic11, the 16 point icon on a Retina screen, and app-64.png in one, ic12.
    assert [kind for kind, body in by_kind.items() if body == _source("app-16.png")] == []
    assert [kind for kind, body in by_kind.items() if body == _source("app-32.png")] == ["ic11"]
    assert [kind for kind, body in by_kind.items() if body == _source("app-64.png")] == ["ic12"]
    # Both small images have pixels that are partly clear and not black, so a colour multiplied
    # by its alpha would differ from the PNG's.
    for name in ("app-16.png", "app-32.png"):
        pixels = zip(*_planes(name), strict=True)
        assert any(0 < a < 255 and (r, g, b) != (0, 0, 0) for r, g, b, a in pixels), name
    # No PNG icp4, icp5 or icp6, and a PNG in the eight ic entries alone.
    assert not {b"icp4", b"icp5", b"icp6"} & {kind for kind, _ in entries}
    assert [kind.decode("ascii") for kind, body in entries if body.startswith(SIGNATURE)] == [
        "ic07",
        "ic08",
        "ic09",
        "ic10",
        "ic11",
        "ic12",
        "ic13",
        "ic14",
    ]
    assert len(data) == 8 + sum(8 + len(body) for _, body in entries)
    # No table of contents and no version entry: nothing but the PNGs' pixels.
    assert b"TOC " not in {kind for kind, _ in entries} and b"icnV" not in dict(entries)
    assert icons.icns_problems(data) == []


def test_the_icon_file_depends_on_the_pngs_alone(tmp_path: Path) -> None:
    copy = tmp_path / "Icons"
    shutil.copytree(icons.ICONS_DIR, copy)
    for path in copy.iterdir():
        path.chmod(0o600)
    assert icons.icns_bytes(copy) == icons.icns_bytes()
    assert icons.source_problems(copy) == []


def test_each_entry_is_the_size_of_the_slot_macos_gives_its_type() -> None:
    # Written out here, so that a change to ICNS_SLOTS fails this test.
    assert icons.ICNS_SLOTS == ICNS_SLOTS
    for kind, (points, scale) in ICNS_SLOTS.items():
        assert icons.iconset_name(points, scale) == ICONSET_NAMES[kind], kind
    assert icons.RGB_SLOTS == RGB_SLOTS
    # The ten names are ten that iconutil wrote in CI; the eleventh was icp6's.
    assert sorted(ICONSET_NAMES.values()) == sorted(
        set(SEEN_IN_RUN_42620168506) - {"icon_48x48.png"}
    )
    # Each entry but a mask fills a slot. A mask fills none: it comes directly after its RGB
    # entry, with the same pixels, from the same PNG.
    masks = set(MASKS.values())
    assert [kind for kind, _, _ in ICNS_TABLE if kind not in masks] == list(ICNS_SLOTS)
    assert not masks & set(ICNS_SLOTS)
    for at, (kind, pixels, source) in enumerate(ICNS_TABLE):
        if kind in masks:
            rgb = next(rgb for rgb, mask in MASKS.items() if mask == kind)
            assert ICNS_TABLE[at - 1] == (rgb, pixels, source), kind
            continue
        points, scale = ICNS_SLOTS[kind]
        assert pixels == points * scale, kind
        # The 16 and 32 point slots at 1x are filled by RGB and a mask, and only those.
        assert (kind in MASKS) == ((points, scale) in RGB_SLOTS), kind
        if kind in MASKS:
            assert ICNS_TABLE[at + 1] == (MASKS[kind], pixels, source), kind
    # The files iconutil is to write: each entry's name, with the pixels its name says. They are
    # the ten the check wanted before, when PNG icp4 and icp5 filled the 1x slots of 16 and 32
    # points. That iconutil gives is32 and il32 the names icp4 and icp5 had is inferred.
    assert icons.iconset_files() == [
        (ICONSET_NAMES[kind], pixels) for kind, pixels, _ in ICNS_TABLE if kind in ICNS_SLOTS
    ]
    assert icons.iconset_files() == ICONSET_FILES_AT_8D3949D
    assert icons.slot_problems() == []
    assert not {"icp4", "icp5", "icp6"} & set(icons.ICNS_SLOTS)


_TWELVE = list(ICNS_TABLE)
_SLOTS = dict(ICNS_SLOTS)
_IS32, _S8MK, _IL32, _L8MK = _TWELVE[:4]
# Each table the build refuses, with what it says. The build checks the table with the PNGs
# (check_icon_sources), which it does before it writes anything.
SLOT_FAULTS: list[tuple[str, list[tuple[str, int, str]], dict[str, tuple[int, int]], str]] = [
    (
        "icp6 back, with the 64 pixel PNG, in its 48 pixel slot",
        [*_TWELVE[:4], ("icp6", 64, "app-64.png"), *_TWELVE[4:]],
        {**_SLOTS, "icp6": (48, 1)},
        "icp6 holds a 64 pixel image, but its slot, icon_48x48.png, is 48 pixels",
    ),
    (
        "icp6 back, with no slot",
        [*_TWELVE[:4], ("icp6", 64, "app-64.png"), *_TWELVE[4:]],
        _SLOTS,
        "icp6 has no slot in ICNS_SLOTS",
    ),
    (
        "ic12 holding the 128 pixel PNG",
        [
            ("ic12", 128, "app-128.png") if kind == "ic12" else (kind, n, s)
            for kind, n, s in _TWELVE
        ],
        _SLOTS,
        "ic12 holds a 128 pixel image, but its slot, icon_32x32@2x.png, is 64 pixels",
    ),
    (
        "ic10 holding the 512 pixel PNG",
        [
            ("ic10", 512, "app-512.png") if kind == "ic10" else (kind, n, s)
            for kind, n, s in _TWELVE
        ],
        _SLOTS,
        "ic10 holds a 512 pixel image, but its slot, icon_512x512@2x.png, is 1024 pixels",
    ),
    (
        "a type twice",
        [*_TWELVE, ("ic07", 128, "app-128.png")],
        _SLOTS,
        "ic07 is in ICNS_ENTRIES twice",
    ),
    (
        "two types in one slot",
        _TWELVE,
        {**_SLOTS, "ic11": (32, 1)},
        "il32 and ic11 both fill icon_32x32.png",
    ),
    (
        "icp4 back as a PNG, in its old slot",
        [("icp4", 16, "app-16.png"), *_TWELVE[2:]],
        {**_SLOTS, "icp4": (16, 1)},
        "icp4 fills icon_16x16.png with a PNG, but that slot takes 24-bit RGB and a mask",
    ),
    (
        "a PNG of another type in the 32 point slot at 1x",
        [_IS32, _S8MK, ("ic05", 32, "app-32.png"), *_TWELVE[4:]],
        {**_SLOTS, "ic05": (32, 1)},
        "ic05 fills icon_32x32.png with a PNG, but that slot takes 24-bit RGB and a mask",
    ),
    (
        "the 16 point slot at 1x left empty",
        _TWELVE[2:],
        _SLOTS,
        "no entry fills icon_16x16.png, which takes 24-bit RGB and a mask",
    ),
    (
        "a mask without its RGB entry",
        [_S8MK, *_TWELVE[2:]],
        _SLOTS,
        "s8mk is not directly after is32, the entry it is the mask of",
    ),
    (
        "a mask before its RGB entry",
        [_S8MK, _IS32, *_TWELVE[2:]],
        _SLOTS,
        "is32 is not directly followed by its mask, s8mk",
    ),
    (
        "an RGB entry without its mask",
        [_IS32, *_TWELVE[2:]],
        _SLOTS,
        "is32 is not directly followed by its mask, s8mk",
    ),
    (
        "each RGB entry with the other's mask",
        [_IS32, _L8MK, _IL32, _S8MK, *_TWELVE[4:]],
        _SLOTS,
        "l8mk is not directly after il32, the entry it is the mask of",
    ),
    (
        "a mask of another size",
        [_IS32, ("s8mk", 32, "app-16.png"), *_TWELVE[2:]],
        _SLOTS,
        "s8mk is a 32 pixel mask, but is32, the entry it is the mask of, is 16 pixels",
    ),
    (
        "a mask from another PNG",
        [_IS32, ("s8mk", 16, "app-32.png"), *_TWELVE[2:]],
        _SLOTS,
        "s8mk is made from app-32.png, but is32, the entry it is the mask of, from app-16.png",
    ),
    (
        "a mask with a slot of its own",
        _TWELVE,
        {**_SLOTS, "s8mk": (16, 1)},
        "s8mk is a mask and fills no slot, but ICNS_SLOTS gives it one",
    ),
]


@pytest.mark.parametrize(
    ("entries", "slots", "said"),
    [(e, s, said) for _, e, s, said in SLOT_FAULTS],
    ids=[n for n, *_ in SLOT_FAULTS],
)
def test_a_faulty_entry_table_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    entries: list[tuple[str, int, str]],
    slots: dict[str, tuple[int, int]],
    said: str,
) -> None:
    monkeypatch.setattr(icons, "ICNS_ENTRIES", tuple(entries))
    monkeypatch.setattr(icons, "ICNS_SLOTS", slots)
    problems = icons.slot_problems()
    assert any(said in problem for problem in problems), problems
    problems = icons.source_problems()
    assert any(said in problem for problem in problems), problems
    with pytest.raises(build_app.BuildError) as refused:
        build_app.check_icon_sources()
    assert said in str(refused.value)


def test_the_iconset_names_follow_iconutils_pattern() -> None:
    assert icons.iconset_name(16, 1) == "icon_16x16.png"
    assert icons.iconset_name(16, 2) == "icon_16x16@2x.png"
    assert icons.iconset_name(512, 2) == "icon_512x512@2x.png"
    with pytest.raises(ValueError, match="scale"):
        icons.iconset_name(16, 3)


def _entries() -> list[tuple[bytes, bytes]]:
    return _icns_entries(icons.icns_bytes())


def _at(kind: str) -> int:
    """Where the entry of type ``kind`` starts in the icon file."""
    i, data = 8, icons.icns_bytes()
    while data[i : i + 4] != kind.encode("ascii"):
        i += struct.unpack(">I", data[i + 4 : i + 8])[0]
    return i


def _set_length(data: bytes, at: int, length: int) -> bytes:
    return data[:at] + struct.pack(">I", length) + data[at + 4 :]


def _swapped(entries: list[tuple[bytes, bytes]], a: int, b: int) -> list[tuple[bytes, bytes]]:
    entries = list(entries)
    entries[a], entries[b] = entries[b], entries[a]
    return entries


def _body(kind: bytes) -> bytes:
    return dict(_entries())[kind]


def _with_body(kind: bytes, body: bytes) -> bytes:
    """The icon file, with the entry of type ``kind`` holding ``body`` instead."""
    return _icns([(k, body if k == kind else v) for k, v in _entries()])


def _packed(*planes: bytes) -> bytes:
    return b"".join(icons.pack_channel(plane) for plane in planes)


def _interleaved(name: str) -> tuple[bytes, bytes, bytes]:
    """A PNG's colour as R, G, B of each pixel in turn, cut into three parts of one plane each:
    what a writer that did not split the channels would pack."""
    red, green, blue, _ = _planes(name)
    flat = bytes(value for pixel in zip(red, green, blue, strict=True) for value in pixel)
    n = len(red)
    return flat[:n], flat[n : 2 * n], flat[2 * n :]


def _premultiplied(name: str) -> tuple[bytes, bytes, bytes]:
    """A PNG's colour multiplied by its alpha, each plane."""
    *colour, alpha = _planes(name)
    return tuple(  # type: ignore[return-value]
        bytes(value * a // 255 for value, a in zip(plane, alpha, strict=True)) for plane in colour
    )


def _one_changed(name: str, channel: int) -> tuple[bytes, bytes, bytes]:
    """A PNG's red, green and blue planes, with one value of one channel (0 red, 1 green, 2
    blue) changed, at the middle pixel: so only that channel differs from the PNG's."""
    planes = list(_planes(name)[:3])
    at = len(planes[channel]) // 2
    plane = planes[channel]
    planes[channel] = plane[:at] + bytes([plane[at] ^ 0x01]) + plane[at + 1 :]
    return planes[0], planes[1], planes[2]


# Each fault, with what is said of it: a text, or, where the text needs the code under test, a
# function that gives it.
ICNS_FAULTS: list[tuple[str, Callable[[], bytes], str | Callable[[], str]]] = [
    ("wrong magic", lambda: b"icnx" + icons.icns_bytes()[4:], "does not start with 'icns'"),
    (
        "total length off by one",
        lambda: _set_length(icons.icns_bytes(), 4, len(icons.icns_bytes()) + 1),
        f"says it is {len(icons.icns_bytes()) + 1} bytes, but it is {len(icons.icns_bytes())}",
    ),
    (
        "trailing bytes",
        lambda: _set_length(icons.icns_bytes() + b"\0\0\0", 4, len(icons.icns_bytes()) + 3),
        "3 bytes after the last entry",
    ),
    (
        "an entry length under 8",
        lambda: _set_length(icons.icns_bytes(), 12, 7),
        "entry at byte 8 says it is 7 bytes",
    ),
    (
        "an entry past the end",
        lambda: _set_length(
            icons.icns_bytes(), _at("ic14") + 4, len(icons.icns_bytes()) - _at("ic14") + 1
        ),
        "runs past the end of the file",
    ),
    (
        "a missing type",
        lambda: _icns([entry for entry in _entries() if entry[0] != b"ic10"]),
        "has no ic10 entry",
    ),
    (
        "a type twice",
        lambda: _icns([*_entries(), (b"ic07", _source("app-128.png"))]),
        "holds ic07 twice",
    ),
    ("two entries swapped", lambda: _icns(_swapped(_entries(), 4, 5)), "out of order"),
    (
        "an unknown type",
        lambda: _icns([*_entries(), (b"icnV", struct.pack(">f", 1.0))]),
        "an entry of type 'icnV', which the build does not write",
    ),
    (
        "ic07 holding the 256 px PNG",
        lambda: _icns([(k, _source("app-256.png") if k == b"ic07" else v) for k, v in _entries()]),
        "ic07 is 256x256 pixels, not 128x128",
    ),
    (
        "an entry that is not a PNG",
        lambda: _icns([(k, b"not a png" if k == b"ic08" else v) for k, v in _entries()]),
        "ic08: not a PNG",
    ),
    # The 16 and 32 pixel images, as 24-bit RGB and a mask.
    # The PNG's bytes, read as packed RGB, give a run longer than the red channel has left.
    (
        "is32 holding the PNG, as icp4 did",
        lambda: _with_body(b"is32", _source("app-16.png")),
        "is32: the run at byte",
    ),
    (
        "icp4 in place of is32, holding the PNG",
        lambda: _icns(
            [(b"icp4", _source("app-16.png")) if k == b"is32" else (k, v) for k, v in _entries()]
        ),
        "an entry of type 'icp4', which the build does not write",
    ),
    (
        "is32 cut short by a byte",
        lambda: _with_body(b"is32", _body(b"is32")[:-1]),
        "is32: its packed data ends inside the run at byte",
    ),
    (
        "is32 with a byte after its blue channel",
        lambda: _with_body(b"is32", _body(b"is32") + b"\x00"),
        "is32: 1 bytes follow its last channel",
    ),
    (
        "is32 with its channels interleaved",
        lambda: _with_body(b"is32", _packed(*_interleaved("app-16.png"))),
        "is32 is not the colour of app-16.png",
    ),
    (
        "is32 with blue, green and red",
        lambda: _with_body(b"is32", _packed(*reversed(_planes("app-16.png")[:3]))),
        "is32 is not the colour of app-16.png",
    ),
    (
        "il32 multiplied by its alpha",
        lambda: _with_body(b"il32", _packed(*_premultiplied("app-32.png"))),
        "il32 is not the colour of app-32.png",
    ),
    # One value changed in one channel alone, so the colour check must look at each of the three.
    (
        "il32 with one red value changed",
        lambda: _with_body(b"il32", _packed(*_one_changed("app-32.png", 0))),
        "il32 is not the colour of app-32.png",
    ),
    (
        "is32 with one blue value changed",
        lambda: _with_body(b"is32", _packed(*_one_changed("app-16.png", 2))),
        "is32 is not the colour of app-16.png",
    ),
    (
        "il32 with one green value changed",
        lambda: _with_body(b"il32", _packed(*_one_changed("app-32.png", 1))),
        "il32 is not the colour of app-32.png",
    ),
    (
        "il32 not packed",
        lambda: _with_body(b"il32", b"".join(_planes("app-32.png")[:3])),
        "il32 is 3072 bytes, as long as unpacked RGB",
    ),
    (
        "is32 with it32's four zero bytes first",
        lambda: _with_body(b"is32", bytes(4) + _body(b"is32")),
        "is32 starts with four zero bytes, which some readers skip as it32's header",
    ),
    (
        "s8mk packed",
        lambda: _with_body(b"s8mk", icons.pack_channel(_planes("app-16.png")[3])),
        lambda: f"s8mk is {len(icons.pack_channel(_planes('app-16.png')[3]))} bytes, not 256",
    ),
    (
        "l8mk from the red channel",
        lambda: _with_body(b"l8mk", _planes("app-32.png")[0]),
        "l8mk is not the alpha of app-32.png",
    ),
    (
        "l8mk from the other image",
        lambda: _with_body(b"l8mk", _planes("app-16.png")[3] * 4),
        "l8mk is not the alpha of app-32.png",
    ),
    (
        "s8mk dropped",
        lambda: _icns([entry for entry in _entries() if entry[0] != b"s8mk"]),
        "has no s8mk entry",
    ),
    # is32 and s8mk: the order rule refuses a mask before its RGB entry as it does any two
    # entries swapped. The file check has no rule of its own for masks' order.
    ("the first two entries swapped", lambda: _icns(_swapped(_entries(), 0, 1)), "out of order"),
]


@pytest.mark.parametrize(
    ("data", "said"), [(f, s) for _, f, s in ICNS_FAULTS], ids=[n for n, *_ in ICNS_FAULTS]
)
def test_a_faulty_icon_file_is_named(
    data: Callable[[], bytes], said: str | Callable[[], str]
) -> None:
    said = said if isinstance(said, str) else said()
    problems = icons.icns_problems(data())
    assert any(said in problem for problem in problems), problems
    assert any("not the file the build writes" in problem for problem in problems), problems


# --- The icns run-length scheme, for the 24-bit RGB entries -----------------------------------

# Each channel, and what it packs to, worked by hand from the scheme in Wikipedia's "Apple Icon
# Image format", "Compression" (read 2026-11-09): a control byte c under 0x80 is followed by c + 1
# bytes as they are (1 to 128); a control byte c of 0x80 or more is followed by one byte, repeated
# c - 0x80 + 3 times (3 to 130). The writer takes the longest run it can, up to 130, where three or
# more bytes repeat, and gathers anything else, up to 128 bytes, into one literal.
PACKED: list[tuple[str, bytes, bytes]] = [
    (
        "Wikipedia's example",
        bytes.fromhex("010202030303040404040505050505"),
        bytes.fromhex("02010202800381048205"),
    ),
    ("one byte", b"\x07", b"\x00\x07"),
    ("two equal bytes", b"\x07\x07", b"\x01\x07\x07"),
    ("a run of exactly 3", b"\x07" * 3, b"\x80\x07"),
    ("a run of 4", b"\x07" * 4, b"\x81\x07"),
    ("a run of 130", b"\x07" * 130, b"\xff\x07"),
    # Past 130, the run starts again; one or two bytes left over go into a literal.
    ("a run of 131", b"\x07" * 131, b"\xff\x07\x00\x07"),
    ("a run of 132", b"\x07" * 132, b"\xff\x07\x01\x07\x07"),
    ("a run of 133", b"\x07" * 133, b"\xff\x07\x80\x07"),
    ("a run of 260", b"\x07" * 260, b"\xff\x07\xff\x07"),
    # A clear channel of 16 x 16 pixels: 130 zeros, then 126.
    ("256 zeros", bytes(256), b"\xff\x00\xfb\x00"),
    ("128 literals", bytes(range(128)), b"\x7f" + bytes(range(128))),
    ("129 literals", bytes(range(129)), b"\x7f" + bytes(range(128)) + b"\x00\x80"),
    (
        "256 literals",
        bytes(range(256)),
        b"\x7f" + bytes(range(128)) + b"\x7f" + bytes(range(128, 256)),
    ),
    ("alternating", b"\x01\x02" * 5, b"\x09" + b"\x01\x02" * 5),
    ("literals, then a run", b"\x01\x02\x05\x05\x05", b"\x01\x01\x02\x80\x05"),
    ("a run, then literals", b"\x05\x05\x05\x01\x02", b"\x80\x05\x01\x01\x02"),
    ("two equal bytes inside literals", b"\x01\x05\x05\x02", b"\x03\x01\x05\x05\x02"),
    (
        "127 literals, then a run of 3",
        bytes(range(127)) + b"\xaa" * 3,
        b"\x7e" + bytes(range(127)) + b"\x80\xaa",
    ),
    (
        "128 literals, then two equal bytes",
        bytes(range(128)) + b"\xaa\xaa",
        b"\x7f" + bytes(range(128)) + b"\x01\xaa\xaa",
    ),
    (
        "a run of 131, then literals",
        b"\x07" * 131 + b"\x01\x02",
        b"\xff\x07\x02\x07\x01\x02",
    ),
]


@pytest.mark.parametrize(
    ("channel", "packed"), [(c, p) for _, c, p in PACKED], ids=[n for n, *_ in PACKED]
)
def test_a_channel_packs_as_worked_by_hand(channel: bytes, packed: bytes) -> None:
    assert icons.pack_channel(channel) == packed
    # Each unpacks again, by this test's reader and by icons.py's.
    assert _unpack(packed, len(channel), 1) == [channel]
    assert icons.unpack_channels(packed, len(channel), 1) == [channel]


def test_an_empty_channel_is_not_packed() -> None:
    # An image has at least one pixel, so a channel is never empty, and the scheme has no way to
    # write nothing: a control byte gives at least one byte.
    with pytest.raises(icons.IconError, match="nothing to pack"):
        icons.pack_channel(b"")


def _channels() -> list[bytes]:
    """Channels of runs and literals of many lengths, the same on every run."""
    found: list[bytes] = []
    for seed in range(60):
        stream = b"".join(hashlib.sha256(bytes([seed, i])).digest() for i in range(40))
        channel = bytearray()
        at = 0
        while len(channel) < 300 and at + 3 < len(stream):
            length = 1 + stream[at] % 140
            if stream[at + 1] % 2:
                channel += bytes([stream[at + 2] % 3]) * length
            else:
                channel += bytes(
                    value % (2 + seed % 5) for value in stream[at + 3 : at + 3 + length]
                )
            at += 3 + length
        found.append(bytes(channel) or b"\x00")
    return found


def test_every_channel_comes_back_as_it_was() -> None:
    for channel in _channels():
        packed = icons.pack_channel(channel)
        assert _unpack(packed, len(channel), 1) == [channel]
        assert icons.unpack_channels(packed, len(channel), 1) == [channel]
        # At worst a control byte for every 128 bytes.
        assert len(packed) <= len(channel) + -(-len(channel) // 128)
    # Three channels, packed one after another, come back as three.
    a, b, c = _channels()[:3]
    n = min(len(a), len(b), len(c))
    data = b"".join(icons.pack_channel(x[:n]) for x in (a, b, c))
    assert icons.unpack_channels(data, n, 3) == [a[:n], b[:n], c[:n]]


@pytest.mark.parametrize("name", ["app-16.png", "app-32.png"])
def test_the_small_images_come_back_from_their_entries(name: str) -> None:
    # The 16 and 32 pixel PNGs, as their RGB and mask entries, read back pixel for pixel.
    width, height, rows = icons.png_pixels(_source(name))
    red, green, blue, alpha = _planes(name)
    rgb = icons.rgb_entry(rows)
    assert _unpack(rgb, width * height, 3) == [red, green, blue]
    assert icons.unpack_channels(rgb, width * height, 3) == [red, green, blue]
    assert icons.mask_entry(rows) == alpha
    assert len(icons.mask_entry(rows)) == width * height
    # The red of each pixel, then the green, then the blue: each packed on its own.
    assert rgb == icons.pack_channel(red) + icons.pack_channel(green) + icons.pack_channel(blue)


# Packed data the reader refuses: the data, the bytes in each channel, the channels, and what it
# says.
BAD_PACKED: dict[str, tuple[bytes, int, int, str]] = {
    "nothing": (b"", 1, 1, "its packed data ends in channel 1 of 1, after 0 of its 1 bytes"),
    "a literal with no byte": (b"\x00", 1, 1, "its packed data ends inside the run at byte 0"),
    "a literal cut short": (b"\x02\x01\x02", 3, 1, "ends inside the run at byte 0"),
    "a repeat with no byte": (b"\x80", 3, 1, "its packed data ends inside the run at byte 0"),
    "a run past the end of its channel": (
        b"\x81\x07",
        3,
        1,
        "the run at byte 0 gives 4 bytes, but channel 1 of 1 has 3 left",
    ),
    "a literal past the end of its channel": (
        b"\x00\x07\x01\x07\x07",
        2,
        1,
        "the run at byte 2 gives 2 bytes, but channel 1 of 1 has 1 left",
    ),
    "a run across two channels": (
        b"\x83\x07",
        3,
        2,
        "the run at byte 0 gives 6 bytes, but channel 1 of 2 has 3 left",
    ),
    "too few channels": (b"\x80\x07", 3, 3, "ends in channel 2 of 3, after 0 of its 3 bytes"),
    "too few bytes in the last channel": (
        b"\x80\x07\x80\x07\x00\x07",
        3,
        3,
        "ends in channel 3 of 3, after 1 of its 3 bytes",
    ),
    "one byte too many": (b"\x80\x07\x00", 3, 1, "1 bytes follow its last channel"),
    "a whole run too many": (b"\x80\x07\x80\x07", 3, 1, "2 bytes follow its last channel"),
}


@pytest.mark.parametrize("name", sorted(BAD_PACKED))
def test_the_run_length_reader_refuses(name: str) -> None:
    data, length, channels, said = BAD_PACKED[name]
    with pytest.raises(icons.IconError) as raised:
        icons.unpack_channels(data, length, channels)
    assert said in str(raised.value)


def test_the_run_length_reader_is_not_asked_for_nothing() -> None:
    # A channel of no bytes, or no channels, is a caller's mistake, not packed data that is wrong.
    with pytest.raises(ValueError, match="there is nothing to read"):
        icons.unpack_channels(b"", 0, 1)
    with pytest.raises(ValueError, match="there is nothing to read"):
        icons.unpack_channels(b"\x00\x07", 1, 0)


def _rows_as_long_as_unpacked() -> list[bytes]:
    """A 16 pixel image whose red, green and blue each pack to 256 bytes, as long as the channel
    unpacked: a run of 4 zeros (2 bytes), then 252 bytes that alternate 1 and 2 (128 and 124
    literals, 254 bytes)."""
    channel = bytes(4) + b"\x01\x02" * 126
    return [
        b"".join(bytes((value, value, value, 255)) for value in channel[y * 16 : (y + 1) * 16])
        for y in range(16)
    ]


def test_rgb_as_long_as_unpacked_rgb_is_refused(tmp_path: Path) -> None:
    # A reader may tell packed RGB from unpacked by its length: Pillow's takes RGB data of
    # 3 x width x height bytes as unpacked (seen in its IcnsImagePlugin.read_32). Whether macOS
    # does is not known, so the build refuses packed RGB of that length rather than risk it.
    rows = _rows_as_long_as_unpacked()
    channel = b"".join(rows)[0::4]
    assert len(icons.pack_channel(channel)) == 256
    with pytest.raises(icons.IconError) as raised:
        icons.rgb_entry(rows)
    said = "its packed 24-bit RGB is 768 bytes, as long as unpacked RGB"
    assert said in str(raised.value)
    # The build names the PNG that gives it, before it writes anything.
    copy = tmp_path / "Icons"
    shutil.copytree(icons.ICONS_DIR, copy)
    (copy / "app-16.png").write_bytes(_png(16, 16, rows))
    problems = icons.source_problems(copy)
    assert len(problems) == 1 and problems[0].startswith(f"app-16.png: {said}"), problems


def test_rgb_that_starts_with_four_zero_bytes_is_refused() -> None:
    # libicns (icns_decode_rle24_data) and the Rust icns crate (decode_rle) skip four zero bytes at
    # the start of any 24-bit RGB entry, not only it32's (seen in their source, read 2026-11-09).
    # Only an image of one pixel packs that way: a black pixel's red is 00 00, its green 00 00.
    with pytest.raises(icons.IconError) as raised:
        icons.rgb_entry([b"\x00\x00\x00\xff"])
    assert "its packed 24-bit RGB starts with four zero bytes" in str(raised.value)
    # Two black pixels pack to a literal of two in each channel, 01 00 00.
    assert icons.rgb_entry([b"\x00\x00\x00\xff" * 2]) == b"\x01\x00\x00" * 3
    # From two pixels on, a literal of one byte is followed by a run, whose control byte is 0x80
    # or more, so no channel of two or more bytes packs to two zero bytes and then a third.
    for channel in _channels():
        if len(channel) >= 2:
            assert icons.pack_channel(channel)[:3] != bytes(3), channel


def test_an_rgb_entry_made_from_a_png_of_another_size_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # is32 fits its slot by the table, 16 pixels, but the PNG it is made from is 32: the table
    # rule cannot see that, and the source check names it before anything is written.
    table = [
        (kind, pixels, "app-32.png" if kind in ("is32", "s8mk") else source)
        for kind, pixels, source in ICNS_TABLE
    ]
    monkeypatch.setattr(icons, "ICNS_ENTRIES", tuple(table))
    assert icons.slot_problems() == []
    said = "is32 is 16 pixels, but app-32.png is 32x32"
    assert icons.source_problems() == [said]
    with pytest.raises(build_app.BuildError) as refused:
        build_app.check_icon_sources()
    assert said in str(refused.value)


# --- The PNG reader ----------------------------------------------------------------------------


def _test_rows(width: int, height: int) -> list[bytes]:
    seed = hashlib.sha256(b"rows").digest()
    stream = b"".join(hashlib.sha256(seed + bytes([i])).digest() for i in range(height * 2))
    stride = width * 4
    return [stream[y * stride : (y + 1) * stride] for y in range(height)]


def test_the_png_reader_undoes_all_five_filters() -> None:
    rows = _test_rows(7, 10)
    for filters in ([0, 1, 2, 3, 4, 4, 3, 2, 1, 0], [4] * 10, [3] * 10):
        data = _png(7, 10, rows, filters=filters)
        assert icons.png_header(data) == (7, 10)
        assert icons.png_pixels(data) == (7, 10, rows)
    # Paeth's ties: where the byte to the left, a, and the one above and to the left, c, are as
    # near the estimate as each other, a wins. With a = 6, b (above) = 12 and c = 10, the estimate
    # is 8, a and c are 2 from it and b is 4, so the predictor is 6 and not 10.
    tied = [bytes([10] * 4 + [12] * 4), bytes([6] * 4 + [99] * 4)]
    data = _png(2, 2, tied, filters=[0, 4])
    assert data == _png(2, 2, tied, filters=[0, 4])
    assert _filtered(4, tied[1], tied[0], 4)[4:] == bytes([93] * 4)
    assert icons.png_pixels(data) == (2, 2, tied)


def _png_with_idat(idat: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
    body = _chunk(b"IHDR", header) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    return SIGNATURE + body


def _bad_crc(data: bytes) -> bytes:
    at = data.index(b"IDAT") + 6
    return data[:at] + bytes([data[at] ^ 1]) + data[at + 1 :]


GOOD_PNG = _png(2, 2, _rgba(2, 2, (0, 0, 0, 255)))
# Each PNG the reader refuses, with what it says. The last three it refuses only once it inflates
# the image data, which the header reader does not do.
BAD_PNGS = {
    "a bad signature": (b"\x89PNG\r\n\x1a\x0b" + GOOD_PNG[8:], "not a PNG"),
    "a bad CRC": (_bad_crc(GOOD_PNG), "has a bad CRC"),
    "a palette PNG": (_png(2, 2, [b"\0\0", b"\0\0"], colour=3), "not 8 bit RGBA"),
    "a 16-bit PNG": (_png(2, 2, [bytes(16), bytes(16)], depth=16), "not 8 bit RGBA"),
    "an interlaced PNG": (
        _png(2, 2, _rgba(2, 2, (0, 0, 0, 255)), interlace=1),
        "it is interlaced",
    ),
    "RGB with no alpha": (_png(2, 2, [bytes(6), bytes(6)], colour=2), "not 8 bit RGBA"),
    "a tEXt chunk": (
        _png(2, 2, _rgba(2, 2, (0, 0, 0, 255)), extra=_chunk(b"tEXt", b"a\0b")),
        "chunks other than IHDR, IDAT and IEND: ['tEXt']",
    ),
    "bytes after IEND": (GOOD_PNG + b"\0", "1 bytes after IEND"),
    "a chunk after IEND": (GOOD_PNG + _chunk(b"IEND", b""), "12 bytes after IEND"),
    "no IEND": (GOOD_PNG[:-12], "its last chunk is not an empty IEND"),
    "IDAT that is not zlib": (_png_with_idat(b"not zlib"), "does not inflate"),
    "a filter type that does not exist": (
        _png_with_idat(zlib.compress(b"\5" + bytes(8) + b"\0" + bytes(8))),
        "row 0 has filter type 5",
    ),
    "rows that are too short": (
        _png(2, 2, _rgba(1, 2, (0, 0, 0, 255))),
        "its image data is 10 bytes, not 18",
    ),
}
INFLATED = ("IDAT that is not zlib", "a filter type that does not exist", "rows that are too short")


def test_the_test_png_is_read() -> None:
    assert icons.png_pixels(GOOD_PNG) == (2, 2, _rgba(2, 2, (0, 0, 0, 255)))
    clear = _png_with_idat(zlib.compress((b"\0" + bytes(8)) * 2))
    assert icons.png_pixels(clear) == (2, 2, _rgba(2, 2, (0, 0, 0, 0)))


@pytest.mark.parametrize("name", sorted(BAD_PNGS))
def test_the_png_reader_refuses(name: str) -> None:
    data, said = BAD_PNGS[name]
    with pytest.raises(icons.IconError) as raised:
        icons.png_pixels(data)
    assert said in str(raised.value)
    if name not in INFLATED:
        with pytest.raises(icons.IconError) as raised:
            icons.png_header(data)
        assert said in str(raised.value)
    else:
        assert icons.png_header(data) == (2, 2)


# --- The IHDR reader, for the PNGs iconutil writes ------------------------------------------------


def test_the_ihdr_reader_reads_the_size_alone() -> None:
    # iconutil writes PNGs of its own, which may hold what png_header refuses. The IHDR reader
    # reads the signature and the IHDR chunk, with its CRC, and nothing after them.
    for name, (data, _) in BAD_PNGS.items():
        if name != "a bad signature":
            assert icons.ihdr_size(data) == (2, 2), name
    assert icons.ihdr_size(_png(48, 64, _rgba(48, 64, (0, 0, 0, 0)))) == (48, 64)
    assert icons.ihdr_size(_source("app-1024.png")) == (1024, 1024)
    # The signature and the 25 bytes of the IHDR chunk are enough.
    assert icons.ihdr_size(GOOD_PNG[:33]) == (2, 2)


def _ihdr_with(body: bytes, kind: bytes = b"IHDR") -> bytes:
    return SIGNATURE + _chunk(kind, body) + _chunk(b"IEND", b"")


def _bad_ihdr_crc(data: bytes) -> bytes:
    at = data.index(b"IHDR") + 4
    return data[:at] + bytes([data[at] ^ 1]) + data[at + 1 :]


IHDR = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
BAD_IHDRS = {
    "no bytes": (b"", "not a PNG: it does not start with the PNG signature"),
    "a bad signature": (b"\x89PNG\r\n\x1a\x0b" + GOOD_PNG[8:], "does not start with the PNG"),
    "text": (b"not a png at all, but long enough to hold an IHDR", "does not start with the PNG"),
    "the signature alone": (SIGNATURE, "not a PNG: it ends before its IHDR chunk does"),
    "IHDR cut short": (GOOD_PNG[:32], "not a PNG: it ends before its IHDR chunk does"),
    "IDAT first": (_ihdr_with(IHDR, b"IDAT"), "not a PNG: its first chunk is not a 13 byte IHDR"),
    "another chunk before IHDR": (
        SIGNATURE + _chunk(b"CgBI", b"\x50\x00\x20\x02") + _chunk(b"IHDR", IHDR),
        "not a PNG: its first chunk is not a 13 byte IHDR",
    ),
    "IHDR of 12 bytes": (_ihdr_with(IHDR[:12]), "its first chunk is not a 13 byte IHDR"),
    "IHDR of 14 bytes": (_ihdr_with(IHDR + b"\0"), "its first chunk is not a 13 byte IHDR"),
    "a bad IHDR CRC": (_bad_ihdr_crc(GOOD_PNG), "not a PNG: its IHDR chunk has a bad CRC"),
}


@pytest.mark.parametrize("name", sorted(BAD_IHDRS))
def test_the_ihdr_reader_refuses(name: str) -> None:
    data, said = BAD_IHDRS[name]
    with pytest.raises(icons.IconError) as raised:
        icons.ihdr_size(data)
    assert said in str(raised.value)


# --- The glyph rule ------------------------------------------------------------------------------


def test_a_glyph_that_is_not_black_on_clear_is_named() -> None:
    x, y = _visible_pixel("MenuBarTemplate.png")
    grey = _with_pixel("MenuBarTemplate.png", x, y, (128, 128, 128, 255))
    problems = icons.glyph_problems("MenuBarTemplate.png", grey)
    assert problems == [f"MenuBarTemplate.png has a pixel that is not black, at ({x}, {y})"]
    # A white pixel is refused even where it is clear: a template holds black and clear only.
    white = _with_pixel("MenuBarTemplate.png", 0, 0, (255, 255, 255, 0))
    assert icons.glyph_problems("MenuBarTemplate.png", white) == [
        "MenuBarTemplate.png has a pixel that is not black, at (0, 0)"
    ]
    empty = _png(18, 18, _rgba(18, 18, (0, 0, 0, 0)))
    assert icons.glyph_problems("MenuBarTemplate.png", empty) == [
        "MenuBarTemplate.png is empty: no pixel shows"
    ]
    small = _source("MenuBarTemplate.png")
    assert icons.glyph_problems("MenuBarTemplate@2x.png", small) == [
        "MenuBarTemplate@2x.png is 18x18 pixels, not 36x36"
    ]
    assert icons.glyph_problems("MenuBarTemplate.png", b"not a png")[0].startswith(
        "MenuBarTemplate.png: not a PNG"
    )


@pytest.mark.parametrize(
    ("name", "replace", "said"),
    [
        ("app-64.png", lambda: _source("app-32.png"), "app-64.png is 32x32 pixels, not 64x64"),
        ("app-1024.png", lambda: b"", "app-1024.png: not a PNG"),
        (
            "MenuBarPausedTemplate@2x.png",
            lambda: _source("MenuBarPausedTemplate.png"),
            "MenuBarPausedTemplate@2x.png is 18x18 pixels, not 36x36",
        ),
        (
            "MenuBarAttentionTemplate.png",
            lambda: _with_pixel("MenuBarAttentionTemplate.png", 9, 9, (40, 40, 40, 255)),
            "MenuBarAttentionTemplate.png has a pixel that is not black",
        ),
        ("app-128.png", None, "app-128.png is missing"),
    ],
)
def test_a_faulty_source_is_named(
    tmp_path: Path, name: str, replace: Callable[[], bytes] | None, said: str
) -> None:
    copy = tmp_path / "Icons"
    shutil.copytree(icons.ICONS_DIR, copy)
    if replace is None:
        (copy / name).unlink()
    else:
        (copy / name).write_bytes(replace())
    problems = icons.source_problems(copy)
    assert len(problems) == 1 and problems[0].startswith(said), problems


def test_a_small_png_that_does_not_decode_is_named(tmp_path: Path) -> None:
    # The build reads app-16.png and app-32.png down to their pixels, for is32 and s8mk, il32 and
    # l8mk. One whose header is good but whose image data does not inflate is named by the source
    # check, and the bundle check says it cannot make the file, rather than stop.
    copy = tmp_path / "Icons"
    shutil.copytree(icons.ICONS_DIR, copy)
    header = struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0)
    broken = SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", b"not zlib")
    (copy / "app-16.png").write_bytes(broken + _chunk(b"IEND", b""))
    assert icons.png_header((copy / "app-16.png").read_bytes()) == (16, 16)
    problems = icons.source_problems(copy)
    assert len(problems) == 1, problems
    assert problems[0].startswith("app-16.png: its image data does not inflate"), problems
    with pytest.raises(icons.IconError):
        icons.icns_bytes(copy)
    problems = icons.icns_problems(icons.icns_bytes(), copy)
    assert any(
        problem.startswith(f"the build's own file cannot be made from {copy}: its image data")
        for problem in problems
    ), problems


# --- Writing the icons into the bundle, and the bundle check ----------------------------------


def _fake_app(tmp_path: Path, plist: dict[str, object] | None = None) -> Path:
    app = tmp_path / "Capsule.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    plist = {"CFBundleIconFile": "AppIcon"} if plist is None else plist
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
    icons.write_icons(resources)
    return app


def test_the_build_writes_seven_files_with_the_committed_bytes(tmp_path: Path) -> None:
    resources = tmp_path / "Resources"
    resources.mkdir()
    written = icons.write_icons(resources)
    names = ["AppIcon.icns", *(name for name, _ in GLYPH_FILES)]
    assert [path.name for path in written] == names
    assert sorted(path.name for path in resources.iterdir()) == sorted(names)
    assert (resources / "AppIcon.icns").read_bytes() == icons.icns_bytes()
    for name, _ in GLYPH_FILES:
        assert (resources / name).read_bytes() == _source(name)
    for path in written:
        assert stat.S_IMODE(path.stat().st_mode) == 0o644, path.name
    first = {path.name: path.read_bytes() for path in written}
    again = icons.write_icons(resources)
    assert {path.name: path.read_bytes() for path in again} == first
    app = _fake_app(tmp_path)
    assert icons.bundle_problems(app) == []


def _flip(path: Path, at: int) -> None:
    data = bytearray(path.read_bytes())
    data[at] ^= 0x01
    path.write_bytes(bytes(data))


BUNDLE_FAULTS: list[tuple[str, Callable[[Path], object], str]] = [
    (
        "no key",
        lambda app: (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({})),
        "Info.plist has no CFBundleIconFile",
    ),
    (
        "the wrong key value",
        lambda app: (app / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIconFile": "Capsule"})
        ),
        "Info.plist's CFBundleIconFile is 'Capsule', not 'AppIcon'",
    ),
    (
        "no Info.plist",
        lambda app: (app / "Contents" / "Info.plist").unlink(),
        "Info.plist cannot be read",
    ),
    (
        "a cut-short Info.plist",
        lambda app: (app / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIconFile": "AppIcon"})[:-20]
        ),
        "Info.plist cannot be read",
    ),
    (
        "the icon file missing",
        lambda app: (app / "Contents" / "Resources" / "AppIcon.icns").unlink(),
        "Contents/Resources/AppIcon.icns is missing",
    ),
    (
        "one byte of the icon file changed",
        lambda app: _flip(app / "Contents" / "Resources" / "AppIcon.icns", -20),
        "not the file the build writes",
    ),
    (
        "a glyph missing",
        lambda app: (app / "Contents" / "Resources" / "MenuBarPausedTemplate@2x.png").unlink(),
        "Contents/Resources/MenuBarPausedTemplate@2x.png is missing",
    ),
    (
        "an @2x file swapped for its 1x file",
        lambda app: shutil.copyfile(
            icons.ICONS_DIR / "MenuBarTemplate.png",
            app / "Contents" / "Resources" / "MenuBarTemplate@2x.png",
        ),
        "MenuBarTemplate@2x.png is 18x18 pixels, not 36x36",
    ),
    (
        "a grey pixel",
        lambda app: (app / "Contents" / "Resources" / "MenuBarTemplate.png").write_bytes(
            _with_pixel("MenuBarTemplate.png", *_visible_pixel("MenuBarTemplate.png"), (90,) * 4)
        ),
        "MenuBarTemplate.png has a pixel that is not black",
    ),
]


@pytest.mark.parametrize(
    ("fault", "said"), [(f, s) for _, f, s in BUNDLE_FAULTS], ids=[n for n, *_ in BUNDLE_FAULTS]
)
def test_the_bundle_check_names_each_fault(
    tmp_path: Path, fault: Callable[[Path], object], said: str
) -> None:
    app = _fake_app(tmp_path)
    assert icons.bundle_problems(app) == []
    fault(app)
    problems = icons.bundle_problems(app)
    assert any(said in problem for problem in problems), problems


def test_a_changed_glyph_is_not_the_committed_one(tmp_path: Path) -> None:
    # Black with alpha only, the right size, and still not the bytes the build copies.
    app = _fake_app(tmp_path)
    changed = _with_pixel("MenuBarTemplate.png", 0, 0, (0, 0, 0, 10))
    (app / "Contents" / "Resources" / "MenuBarTemplate.png").write_bytes(changed)
    assert icons.bundle_problems(app) == [
        "Contents/Resources/MenuBarTemplate.png is not the file the build copies from macos/Icons"
    ]
