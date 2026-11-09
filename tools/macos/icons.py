"""The Mac app's icons (decisions/0002 §6), from the PNGs committed in ``macos/Icons/``.

The build renders nothing and runs no program to make them. It checks the PNGs
(:func:`source_problems`), packs seven of them into ``Contents/Resources/AppIcon.icns``
(:func:`icns_bytes`), and copies the six menu bar glyphs beside it (:func:`write_icons`).
``build_app.py`` and the ``bundle`` check of ``check_app.py`` hold the built app to the same files
(:func:`bundle_problems`).

``AppIcon.icns`` has the layout of Apple's icon files: the four bytes ``icns``, the file's length,
then one entry after another: its type, its length with its own 8 byte header, and its data. Eight
entries hold a whole PNG. The 16 and 32 pixel images are each in two entries instead: 24-bit RGB,
packed with the icns run-length scheme (``is32``, ``il32``; :func:`rgb_entry`), and an 8-bit mask
of the alpha (``s8mk``, ``l8mk``; :func:`mask_entry`). That is because macOS is reported to draw
PNG data in the 16 and 32 pixel types scrambled when the file is an app's icon (the grounds are at
:data:`ICNS_MASKS`). This module writes no table of contents and no version entry, so the file
depends on the PNGs alone. Each entry type but a mask has a slot of its own, by the names
``iconutil`` gives its files (:data:`ICNS_SLOTS`), and each entry's image must be its slot's size
(:func:`slot_problems`). A mask fills no slot: it comes directly after its RGB entry. How macOS
reads the file is seen only on a Mac: the ``bundle`` check has ``iconutil`` read it into an
iconset, and requires exactly the files the entries' slots name, each a PNG of its slot's size.
Run 42636263428 held ``iconutil``'s files to that rule on macOS 15.8.2, 26.7.2 and 27.0.1, with PNG
``icp4`` and ``icp5``; no run has yet read a file with the RGB and mask types. Whether macOS draws
each entry is not seen.

A menu bar glyph is a template image: every pixel black, only its alpha varying. Its name ends in
``Template``, which makes ``NSImage(named:)`` treat it as a template, and ``<name>@2x.png`` is the
same glyph for a Retina screen. The ``bundle`` check has AppKit load each one from the built app
by its name.
"""

from __future__ import annotations

import plistlib
import shutil
import struct
import zlib
from pathlib import Path
from typing import Final, Literal
from xml.parsers.expat import ExpatError

# The committed masters and PNGs. Each function reads this when it is called, so a test can point
# it elsewhere.
ICONS_DIR: Final = Path(__file__).resolve().parents[2] / "macos" / "Icons"
# Info.plist's CFBundleIconFile, and the file it names in Contents/Resources.
APP_ICON: Final = "AppIcon"
ICNS_FILE: Final = f"{APP_ICON}.icns"
RESOURCES: Final = "Contents/Resources"
# The app icon's PNGs, ``app-<N>.png``, each N pixels square.
APP_SIZES: Final = (16, 32, 64, 128, 256, 512, 1024)
# The slot each entry type the build writes is read into, but a mask's (it goes with its RGB
# type): the icon's size in points, and its scale.
# ``iconutil -c iconset`` names a slot's file icon_<P>x<P>.png, or icon_<P>x<P>@2x.png at scale 2
# (:func:`iconset_name`). By its name the file is points x scale pixels square. Seen in CI, run
# 42636263428 (PR 30, head 8d3949d), in macos-app (macOS 15.8.2) and in the smoke jobs (26.7.2 and
# 27.0.1): each of the ten files iconutil wrote had the IHDR size its name gives.
#
# Seen in CI, run 42620168506: iconutil read an AppIcon.icns of eleven PNG entries, icp4 and icp5
# where is32 and il32 are now, the eight ic types, and icp6, into eleven files, the ten names these
# slots give and icon_48x48.png, with the same names and byte counts in job macos-app (macOS
# 15.8.2) and in the smoke jobs (26.7.2 and 27.0.1). The log names files, not types, so which type
# gave which name is inferred. By pixels alone, two types that held one PNG fit each other's names:
# icon_32x32@2x.png fits icp6 and ic12 alike, since both held the 64 pixel PNG. Two things point
# to ic12 for it, and so to icp6 for icon_48x48.png, a 48 pixel slot by its name (decisions/0002
# §6). Public lists of the types give ic11 to ic14 as the @2x slots and icp6 as 48 pixels (relikd's
# icnsutil and Wikipedia's table; Pillow's reader takes icp6 as 64). And the byte counts fit:
# icon_16x16.png, which only icp4 fits, is twice its PNG, as icon_32x32.png and icon_48x48.png are
# about twice theirs, while each other file is 1.2 to 1.6 times its PNG. That is why the build
# writes no icp6.
#
# is32 and il32 fill the slots icp4 and icp5 filled in that run. That iconutil names their files
# icon_16x16.png and icon_32x32.png, as it named icp4's and icp5's, is inferred: the public lists
# give is32 and il32 as 16 and 32 pixels, but no run has yet read a file with these types. The
# bundle check of the next Mac run is the first to show it, and fails, naming the file, if not.
ICNS_SLOTS: Final = {
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
# The 24-bit RGB types the build writes, each with the 8-bit mask type that goes with it.
#
# Why, as reported in Wikipedia's article "Apple Icon Image format" (a copy read 2026-11-09),
# "Known issues", item 3: macOS 10.15.7 (likely earlier) and later draw PNG and JPEG 2000 data in
# icp4 (16x16), icp5 (32x32) and icp6 completely scrambled when the icon file is used in an
# application, though well in a standalone icns file; the same happens if the small sizes are left
# out and macOS scales a larger PNG down; and the fix is ARGB in ic04 and ic05 (macOS 11 and later;
# its item 2 reports a bug in packed ARGB) or the old 24-bit RGB with an 8-bit mask, is32 with s8mk
# and il32 with l8mk. The same item reports that on a non-Retina monitor the non-Retina 16x16 icon
# is used, so the fault shows there even when a 16x16@2x icon is given. That the 32 point icon
# works the same way, and that a Retina screen uses the @2x entries, which the article's type table
# gives as ic11 (16x16@2x) and ic12 (32x32@2x), both PNGs here, is inferred. Its item 1 reports
# that il32 beside icl8 is drawn without its mask; the build writes no icl8. Whether the report
# holds on macOS 15, 26 and 27 is not seen.
#
# What the entries hold, from the same article's "Icon types" table and its sections "RGB
# elements", "Mask elements" and "Compression": an RGB entry holds the red of every pixel, then the
# green, then the blue, each channel packed on its own (pack_channel), with no header (only it32
# starts with four zero bytes); a mask entry holds each pixel's alpha, one byte each, not packed.
# The article reports that RGB written unpacked does not show (tested there on macOS 11), so the
# build packs it. The colour is the PNG's, not multiplied by its alpha. The article does not say
# which; that macOS reads it so is inferred. The grounds, each seen in source read 2026-11-09, none
# of them macOS drawing an entry: Pillow 12.3.0's reader puts the mask in as the alpha of the RGB
# it read (IcnsImagePlugin, getimage); relikd's icnsutil (ArgbImage.rgb_data) and the Rust icns
# crate 0.5.0 (encode_rle, on RGBA image data) write the colour as it is; and the IconFamily
# class's code, as kept in UliKit (NSImage+IconRef.m, get32BitDataAtPixelSize:), undoes
# NSBitmapImageRep's premultiplication before it hands 32-bit icon data to Apple's
# SetIconFamilyData (for kThumbnail32BitData, which is it32, not is32).
ICNS_MASKS: Final = {"is32": "s8mk", "il32": "l8mk"}
# The slots whose image is 24-bit RGB with its mask, never a PNG: the 16 and 32 point icons at 1x
# (see ICNS_MASKS). Each must be filled, since a missing small size is reported to be drawn
# scrambled too, from a larger PNG scaled down.
RGB_SLOTS: Final = ((16, 1), (32, 1))
# AppIcon.icns, entry by entry: its type, its pixels, which must be its slot's points x scale (a
# mask's must be its RGB entry's), and the PNG it is made from. The 16 pixel image is the
# hand-drawn master, app-16.svg's, in is32 and s8mk; the 32 pixel one, in il32 and l8mk, and every
# other PNG are app.svg's. ic11 to ic14 are the @2x entries: ic11 is the 16 point icon on a Retina
# screen, 32 pixels, and ic12, the 32 point one, holds the 64 pixel PNG.
ICNS_ENTRIES: Final = (
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
# The menu bar glyphs, in the order HostModel.icon picks them: running, paused, needs attention.
GLYPHS: Final = ("MenuBarTemplate", "MenuBarPausedTemplate", "MenuBarAttentionTemplate")
# Their size in points, which HostModel.icon gives the image too.
GLYPH_POINTS: Final = 18
# Each glyph's two files, with their pixels.
GLYPH_FILES: Final = tuple(
    (f"{glyph}{suffix}.png", GLYPH_POINTS * scale)
    for glyph in GLYPHS
    for suffix, scale in (("", 1), ("@2x", 2))
)

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
# The icns run-length scheme (Wikipedia, "Apple Icon Image format", "Compression", read
# 2026-11-09): a control byte under 0x80 is followed by that many bytes plus one, 1 to 128, as they
# are; a control byte of 0x80 or more by one byte, repeated the control byte - 0x80 + 3 times, 3 to
# 130.
_LITERAL_MAX: Final = 128
_RUN_MIN: Final = 3
_RUN_MAX: Final = 130
# Two kinds of packed RGB that the format allows and the build writes neither of, as readers other
# than the format's own rule would read them wrongly (each seen in source read 2026-11-09):
# Pillow 12.3.0's reader takes RGB data of exactly 3 x w x h bytes as unpacked (IcnsImagePlugin,
# read_32); libicns (icns_rle24.c, icns_decode_rle24_data) and the Rust icns crate 0.5.0
# (decode_rle) skip four zero bytes at the start of any 24-bit RGB entry, not only it32's. What
# macOS does with either is not known. For two or more pixels the writer cannot start with four
# zero bytes: a literal of one byte is followed by a run, whose control byte is 0x80 or more.
_SKIPPED_START: Final = bytes(4)
# An 8 bit RGBA image, not interlaced: what Chromium wrote for the committed PNGs, and all a
# template needs.
_IHDR: Final = struct.Struct(">IIBBBBB")
_RGBA: Final = 6


class IconError(ValueError):
    """A file is not a PNG this module reads."""


def _folder(folder: Path | None) -> Path:
    return ICONS_DIR if folder is None else folder


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Each chunk's type and body, each CRC checked; nothing may follow IEND."""
    if data[:8] != PNG_SIGNATURE:
        raise IconError("not a PNG: it does not start with the PNG signature")
    found: list[tuple[bytes, bytes]] = []
    i = 8
    while i < len(data):
        if found and found[-1][0] == b"IEND":
            raise IconError(f"{len(data) - i} bytes after IEND")
        if i + 12 > len(data):
            raise IconError(f"a chunk at byte {i} is cut short")
        (length,) = struct.unpack(">I", data[i : i + 4])
        kind = data[i + 4 : i + 8]
        end = i + 12 + length
        if end > len(data):
            raise IconError(f"the {kind!r} chunk at byte {i} runs past the end")
        body = data[i + 8 : i + 8 + length]
        (crc,) = struct.unpack(">I", data[end - 4 : end])
        if crc != zlib.crc32(kind + body):
            raise IconError(f"the {kind!r} chunk at byte {i} has a bad CRC")
        found.append((kind, body))
        i = end
    return found


def _read(data: bytes) -> tuple[int, int, list[bytes]]:
    """The width, the height and the IDAT bodies of an 8 bit RGBA PNG with no other chunk."""
    chunks = _chunks(data)
    kinds = [kind for kind, _ in chunks]
    if not kinds or kinds[0] != b"IHDR":
        raise IconError("its first chunk is not IHDR")
    if kinds[-1] != b"IEND" or chunks[-1][1]:
        raise IconError("its last chunk is not an empty IEND")
    middle = kinds[1:-1]
    if not middle or set(middle) != {b"IDAT"}:
        others = sorted({kind.decode("latin-1") for kind in middle} - {"IDAT"})
        raise IconError(f"it holds chunks other than IHDR, IDAT and IEND: {others or 'no IDAT'}")
    header = chunks[0][1]
    if len(header) != _IHDR.size:
        raise IconError("its IHDR is not 13 bytes")
    width, height, depth, colour, compression, filtering, interlace = _IHDR.unpack(header)
    if (depth, colour) != (8, _RGBA):
        raise IconError(f"it is not 8 bit RGBA: bit depth {depth}, colour type {colour}")
    if (compression, filtering) != (0, 0):
        raise IconError("its compression or filter method is not 0")
    if interlace != 0:
        raise IconError("it is interlaced")
    if width == 0 or height == 0:
        raise IconError("it has no pixels")
    return width, height, [body for kind, body in chunks if kind == b"IDAT"]


def ihdr_size(data: bytes) -> tuple[int, int]:
    """The width and height a PNG's IHDR chunk gives. Reads the PNG signature and the IHDR
    chunk, which must come first, with its CRC, and nothing after them, so any other chunk, bit
    depth or colour type is allowed: it is for the PNGs ``iconutil`` writes, which this module
    did not write. Raises :class:`IconError` if ``data`` does not start that way."""
    if data[:8] != PNG_SIGNATURE:
        raise IconError("not a PNG: it does not start with the PNG signature")
    end = 8 + 8 + _IHDR.size + 4
    if len(data) < end:
        raise IconError("not a PNG: it ends before its IHDR chunk does")
    length, kind = struct.unpack(">I4s", data[8:16])
    if kind != b"IHDR" or length != _IHDR.size:
        raise IconError("not a PNG: its first chunk is not a 13 byte IHDR")
    body = data[16 : 16 + _IHDR.size]
    (crc,) = struct.unpack(">I", data[end - 4 : end])
    if crc != zlib.crc32(kind + body):
        raise IconError("not a PNG: its IHDR chunk has a bad CRC")
    width, height = struct.unpack(">II", body[:8])
    return width, height


def png_header(data: bytes) -> tuple[int, int]:
    """The width and height of a PNG that is 8 bit RGBA, not interlaced, and holds only IHDR,
    IDAT and IEND, each with a good CRC, and nothing after IEND. It does not inflate the image
    data. Raises :class:`IconError` otherwise."""
    width, height, _ = _read(data)
    return width, height


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def png_pixels(data: bytes) -> tuple[int, int, list[bytes]]:
    """A PNG :func:`png_header` accepts, as its width, its height and its rows of RGBA bytes,
    with the five row filters undone. Written for the glyphs and the 16 and 32 pixel app icons,
    at most 36 pixels square: in pure Python, a large image takes long."""
    width, height, bodies = _read(data)
    try:
        raw = zlib.decompress(b"".join(bodies))
    except zlib.error as error:
        raise IconError(f"its image data does not inflate: {error}") from None
    stride = 4 * width
    if len(raw) != height * (1 + stride):
        raise IconError(f"its image data is {len(raw)} bytes, not {height * (1 + stride)}")
    rows: list[bytes] = []
    prev = bytes(stride)
    for y in range(height):
        start = y * (1 + stride)
        kind = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        if kind > 4:
            raise IconError(f"row {y} has filter type {kind}, which PNG does not have")
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            if kind == 1:
                line[x] = (line[x] + a) & 0xFF
            elif kind == 2:
                line[x] = (line[x] + b) & 0xFF
            elif kind == 3:
                line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif kind == 4:
                c = prev[x - 4] if x >= 4 else 0
                line[x] = (line[x] + _paeth(a, b, c)) & 0xFF
        rows.append(bytes(line))
        prev = rows[-1]
    return width, height, rows


def _size_problem(name: str, data: bytes, pixels: int) -> str | None:
    try:
        width, height = png_header(data)
    except IconError as error:
        return f"{name}: {error}"
    if (width, height) != (pixels, pixels):
        return f"{name} is {width}x{height} pixels, not {pixels}x{pixels}"
    return None


def glyph_problems(name: str, data: bytes) -> list[str]:
    """What is wrong with a menu bar glyph called ``name``: it must be a PNG :func:`png_header`
    accepts, 18 pixels square, or 36 if its name ends in ``@2x.png``; every pixel black, only its
    alpha varying; and some pixel must show."""
    pixels = GLYPH_POINTS * (2 if name.endswith("@2x.png") else 1)
    problem = _size_problem(name, data, pixels)
    if problem is not None:
        return [problem]
    try:
        _, _, rows = png_pixels(data)
    except IconError as error:
        return [f"{name}: {error}"]
    for y, row in enumerate(rows):
        for x in range(pixels):
            if row[4 * x : 4 * x + 3] != b"\0\0\0":
                return [f"{name} has a pixel that is not black, at ({x}, {y})"]
    if not any(row[3::4].strip(b"\0") for row in rows):
        return [f"{name} is empty: no pixel shows"]
    return []


def entry_format(kind: str) -> Literal["png", "rgb", "mask"]:
    """How an entry of type ``kind`` holds its image: ``"rgb"`` for a 24-bit RGB type of
    :data:`ICNS_MASKS`, ``"mask"`` for the mask type that goes with one, and ``"png"`` for any
    other type, which holds a whole PNG."""
    if kind in ICNS_MASKS:
        return "rgb"
    if kind in ICNS_MASKS.values():
        return "mask"
    return "png"


def _rgb_of(mask: str) -> str:
    """The RGB type a mask type goes with."""
    return next(rgb for rgb, kind in ICNS_MASKS.items() if kind == mask)


def pack_channel(channel: bytes) -> bytes:
    """One channel of an RGB entry, packed with the icns run-length scheme. Where three or more
    bytes repeat, it writes the longest run it can, up to 130, as a control byte of 0x80 + the
    count - 3 and the byte. It gathers anything else, up to 128 bytes, into a literal: a control
    byte of the count - 1 and the bytes as they are. One or two bytes left at the end of a run
    longer than 130 go into the next literal. The same channel always packs to the same bytes.
    Raises :class:`IconError` for an empty channel, which the scheme cannot write and no image
    has."""
    if not channel:
        raise IconError("there is nothing to pack: a channel has a byte for each pixel")
    out = bytearray()
    i, end = 0, len(channel)
    while i < end:
        run = 1
        while i + run < end and run < _RUN_MAX and channel[i + run] == channel[i]:
            run += 1
        if run >= _RUN_MIN:
            out += bytes((0x80 + run - _RUN_MIN, channel[i]))
            i += run
            continue
        start = i
        while i < end and i - start < _LITERAL_MAX:
            if i + 2 < end and channel[i] == channel[i + 1] == channel[i + 2]:
                break
            i += 1
        out.append(i - start - 1)
        out += channel[start:i]
    return bytes(out)


def unpack_channels(data: bytes, length: int, count: int) -> list[bytes]:
    """``count`` channels of ``length`` bytes each, packed one after another with the icns
    run-length scheme, unpacked. Each channel is packed on its own, so a run that gives more
    bytes than its channel has left is refused, as are data that end before the last channel is
    full and bytes after it. Raises :class:`IconError`, saying where."""
    if length < 1 or count < 1:
        raise ValueError(f"a channel of {length} bytes, {count} of them: there is nothing to read")
    channels: list[bytes] = []
    i = 0
    for number in range(1, count + 1):
        channel = bytearray()
        while len(channel) < length:
            if i >= len(data):
                raise IconError(
                    f"its packed data ends in channel {number} of {count}, after "
                    f"{len(channel)} of its {length} bytes"
                )
            control = data[i]
            if control < 0x80:
                size, after = control + 1, i + 2 + control
            else:
                size, after = control - 0x80 + _RUN_MIN, i + 2
            if after > len(data):
                raise IconError(f"its packed data ends inside the run at byte {i}")
            left = length - len(channel)
            if size > left:
                raise IconError(
                    f"the run at byte {i} gives {size} bytes, but channel {number} of {count} "
                    f"has {left} left"
                )
            channel += data[i + 1 : after] if control < 0x80 else data[i + 1 : after] * size
            i = after
        channels.append(bytes(channel))
    if i != len(data):
        raise IconError(f"{len(data) - i} bytes follow its last channel")
    return channels


def _planes(rows: list[bytes]) -> tuple[bytes, bytes, bytes, bytes]:
    """The red, green, blue and alpha of RGBA rows, each a plane of the pixels in row order."""
    flat = b"".join(rows)
    return flat[0::4], flat[1::4], flat[2::4], flat[3::4]


def rgb_entry(rows: list[bytes]) -> bytes:
    """The data of an RGB entry (``is32``, ``il32``) for the image of ``rows``, the RGBA rows
    :func:`png_pixels` gives: the red of every pixel, then the green, then the blue, each channel
    packed on its own (:func:`pack_channel`), with no header, and the colour as the PNG holds it.
    Raises :class:`IconError` if that is as long as the three channels unpacked, or starts with
    four zero bytes (:data:`_SKIPPED_START`): readers that go by length or by those bytes would
    read it wrongly, and whether macOS does is not known."""
    red, green, blue, _ = _planes(rows)
    data = b"".join(pack_channel(channel) for channel in (red, green, blue))
    if len(data) == 3 * len(red):
        raise IconError(
            f"its packed 24-bit RGB is {len(data)} bytes, as long as unpacked RGB, which a "
            "reader that goes by length would take it for"
        )
    if data.startswith(_SKIPPED_START):
        raise IconError(
            "its packed 24-bit RGB starts with four zero bytes, which some readers skip as "
            "it32's header"
        )
    return data


def mask_entry(rows: list[bytes]) -> bytes:
    """The data of a mask entry (``s8mk``, ``l8mk``) for the image of ``rows``: each pixel's
    alpha, in row order, one byte each, not packed."""
    return _planes(rows)[3]


def _entry_data(kind: str, png: bytes) -> bytes:
    """What the entry of type ``kind`` holds, made from the PNG ``png``. Raises
    :class:`IconError` if an RGB or mask entry's PNG cannot be decoded, or its RGB cannot be
    packed (:func:`rgb_entry`)."""
    form = entry_format(kind)
    if form == "png":
        return png
    _, _, rows = png_pixels(png)
    return rgb_entry(rows) if form == "rgb" else mask_entry(rows)


def iconset_name(points: int, scale: int) -> str:
    """The name ``iconutil -c iconset`` gives the file of a slot: ``icon_16x16.png`` for 16 points
    at scale 1, ``icon_16x16@2x.png`` at scale 2."""
    if scale not in (1, 2):
        raise ValueError(f"an iconset slot has scale 1 or 2, not {scale}")
    return f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"


def iconset_files() -> list[tuple[str, int]]:
    """The files ``iconutil -c iconset`` is to write from ``AppIcon.icns``: for each entry of
    :data:`ICNS_ENTRIES` whose type has a slot, in that order, the slot's file name and its
    pixels, points x scale, which is what the name says (``icon_<N>x<N>.png`` is N pixels
    square, ``icon_<N>x<N>@2x.png`` 2N). A mask fills no slot and gives no file of its own: its
    RGB entry's file is the image of both (inferred: no run has yet read these types)."""
    files: list[tuple[str, int]] = []
    for kind, _, _ in ICNS_ENTRIES:
        if entry_format(kind) != "mask" and kind in ICNS_SLOTS:
            points, scale = ICNS_SLOTS[kind]
            files.append((iconset_name(points, scale), points * scale))
    return files


def slot_problems() -> list[str]:
    """What is wrong with :data:`ICNS_ENTRIES` against :data:`ICNS_SLOTS` and
    :data:`ICNS_MASKS`: each entry's type but a mask's must have a slot, its pixels must be the
    slot's points x scale, and no type and no slot may come twice. Each slot of
    :data:`RGB_SLOTS` must be filled, by an RGB type and not a PNG. Each RGB entry must be
    followed directly by its mask; a mask must have no slot, and must be of its RGB entry's
    pixels and from its PNG. :func:`icns_problems` holds each entry to its pixels and its
    PNG."""
    entries = list(ICNS_ENTRIES)
    first: dict[str, tuple[int, str]] = {}
    for kind, pixels, source in entries:
        first.setdefault(kind, (pixels, source))
    problems: list[str] = []
    seen: set[str] = set()
    filled: dict[str, str] = {}
    for at, (kind, pixels, source) in enumerate(entries):
        if kind in seen:
            problems.append(f"{kind} is in ICNS_ENTRIES twice")
            continue
        seen.add(kind)
        form = entry_format(kind)
        if form == "mask":
            rgb = _rgb_of(kind)
            if kind in ICNS_SLOTS:
                problems.append(f"{kind} is a mask and fills no slot, but ICNS_SLOTS gives it one")
            if at == 0 or entries[at - 1][0] != rgb:
                problems.append(f"{kind} is not directly after {rgb}, the entry it is the mask of")
            if rgb in first:
                rgb_pixels, rgb_source = first[rgb]
                if pixels != rgb_pixels:
                    problems.append(
                        f"{kind} is a {pixels} pixel mask, but {rgb}, the entry it is the mask "
                        f"of, is {rgb_pixels} pixels"
                    )
                if source != rgb_source:
                    problems.append(
                        f"{kind} is made from {source}, but {rgb}, the entry it is the mask of, "
                        f"from {rgb_source}"
                    )
            continue
        if form == "rgb" and (at + 1 == len(entries) or entries[at + 1][0] != ICNS_MASKS[kind]):
            problems.append(f"{kind} is not directly followed by its mask, {ICNS_MASKS[kind]}")
        if kind not in ICNS_SLOTS:
            problems.append(f"{kind} has no slot in ICNS_SLOTS")
            continue
        points, scale = ICNS_SLOTS[kind]
        name = iconset_name(points, scale)
        if pixels != points * scale:
            problems.append(
                f"{kind} holds a {pixels} pixel image, but its slot, {name}, is "
                f"{points * scale} pixels"
            )
        if (points, scale) in RGB_SLOTS and form != "rgb":
            problems.append(
                f"{kind} fills {name} with a PNG, but that slot takes 24-bit RGB and a mask"
            )
        if name in filled:
            problems.append(f"{filled[name]} and {kind} both fill {name}")
        filled.setdefault(name, kind)
    for points, scale in RGB_SLOTS:
        name = iconset_name(points, scale)
        if name not in filled:
            problems.append(f"no entry fills {name}, which takes 24-bit RGB and a mask")
    return problems


def source_problems(folder: Path | None = None) -> list[str]:
    """What is wrong with the PNGs the build reads in ``folder`` (``macos/Icons/``), and with the
    table it packs them by: each entry must fit its slot (:func:`slot_problems`), each
    ``app-<N>.png`` must be N pixels square, and each glyph must pass :func:`glyph_problems`.
    The PNG of each RGB entry must also decode, to its entry's pixels, and its RGB must pack
    (:func:`rgb_entry`), so that :func:`icns_bytes` can write the file."""
    folder = _folder(folder)
    problems = slot_problems()
    wanted = [(f"app-{size}.png", size) for size in APP_SIZES] + list(GLYPH_FILES)
    good: dict[str, bytes] = {}
    for name, pixels in wanted:
        try:
            data = (folder / name).read_bytes()
        except FileNotFoundError:
            problems.append(f"{name} is missing")
            continue
        except OSError as error:
            problems.append(f"{name} cannot be read: {error}")
            continue
        if name.startswith("app-"):
            problem = _size_problem(name, data, pixels)
            problems += [] if problem is None else [problem]
            if problem is None:
                good[name] = data
        else:
            problems += glyph_problems(name, data)
    for kind, pixels, source in ICNS_ENTRIES:
        if entry_format(kind) == "rgb" and source in good:
            problems += _rgb_source_problems(kind, pixels, source, good[source])
    return problems


def _rgb_source_problems(kind: str, pixels: int, name: str, data: bytes) -> list[str]:
    """What is wrong with ``data``, the PNG ``name`` the RGB entry ``kind`` of ``pixels`` is made
    from: it must decode, be of the entry's pixels (a table may give an entry that fits its slot
    a PNG of another size), and pack (:func:`rgb_entry`)."""
    try:
        width, height, rows = png_pixels(data)
        if (width, height) != (pixels, pixels):
            return [f"{kind} is {pixels} pixels, but {name} is {width}x{height}"]
        rgb_entry(rows)
    except IconError as error:
        return [f"{name}: {error}"]
    return []


def icns_bytes(folder: Path | None = None) -> bytes:
    """``AppIcon.icns``: ``icns``, the file's length, then each entry of :data:`ICNS_ENTRIES` in
    order, as its type, its length with its 8 byte header, and its data: its PNG's bytes, or for
    an RGB or mask entry, what :func:`rgb_entry` or :func:`mask_entry` makes from its PNG's
    pixels. Big-endian, with no table of contents and nothing that depends on the time or the
    machine. Raises :class:`OSError` if a PNG cannot be read, and :class:`IconError` if an RGB
    or mask entry's cannot be made; :func:`source_problems` names both first."""
    folder = _folder(folder)
    pngs: dict[str, bytes] = {}
    entries: list[bytes] = []
    for kind, _, source in ICNS_ENTRIES:
        if source not in pngs:
            pngs[source] = (folder / source).read_bytes()
        data = _entry_data(kind, pngs[source])
        entries.append(kind.encode("ascii") + struct.pack(">I", 8 + len(data)) + data)
    body = b"".join(entries)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def icns_problems(data: bytes, folder: Path | None = None) -> list[str]:
    """What is wrong with an icon file: its magic and its length; entries that tile it exactly;
    the types of :data:`ICNS_ENTRIES`, each once and in that order; each PNG entry a PNG of the
    pixels its row gives, which :func:`slot_problems` holds to its slot, with its source's bytes;
    each RGB entry packed RGB that :func:`unpack_channels` reads back to exactly its source's
    red, green and blue, not as long as unpacked RGB and not starting with four zero bytes; each
    mask entry exactly its source's alpha; and the whole file the one :func:`icns_bytes` writes
    from ``folder``."""
    folder = _folder(folder)
    problems: list[str] = []
    try:
        expected = icns_bytes(folder)
    except (OSError, IconError) as error:
        expected = None
        problems.append(f"the build's own file cannot be made from {folder}: {error}")
    if data[:4] != b"icns":
        problems.append(f"it does not start with 'icns' but with {data[:4]!r}")
    elif len(data) < 8:
        problems.append(f"it is {len(data)} bytes, too short for its header")
    else:
        (total,) = struct.unpack(">I", data[4:8])
        if total != len(data):
            problems.append(f"it says it is {total} bytes, but it is {len(data)}")
        problems += _entry_problems(data, folder)
    if expected is not None and data != expected:
        problems.append(f"it is not the file the build writes from {_shown(folder)}")
    return problems


def _shown(folder: Path) -> str:
    return "macos/Icons" if folder == ICONS_DIR else str(folder)


def _source_planes(
    path: Path, planes: dict[Path, tuple[bytes, bytes, bytes, bytes] | None]
) -> tuple[bytes, bytes, bytes, bytes] | None:
    """The red, green, blue and alpha planes of the PNG at ``path``, or None if it cannot be read
    or decoded, which :func:`icns_problems` has then said. Each is read once."""
    if path not in planes:
        try:
            planes[path] = _planes(png_pixels(path.read_bytes())[2])
        except (OSError, IconError):
            planes[path] = None
    return planes[path]


def _body_problems(
    kind: str,
    body: bytes,
    pixels: int,
    source: str,
    folder: Path,
    planes: dict[Path, tuple[bytes, bytes, bytes, bytes] | None],
) -> list[str]:
    """What is wrong with what the entry of type ``kind`` holds: by its form
    (:func:`entry_format`), a PNG of ``pixels`` that is its source's bytes; packed RGB that is not
    as long as unpacked RGB, does not start with four zero bytes (:data:`_SKIPPED_START`), and
    reads back to its source's red, green and blue; or its source's alpha."""
    form = entry_format(kind)
    if form == "png":
        problem = _size_problem(kind, body, pixels)
        if problem is not None:
            return [problem]
        try:
            same = body == (folder / source).read_bytes()
        except OSError:
            same = True  # icns_problems has said that the sources cannot be read
        return [] if same else [f"{kind} is not {source}"]
    count = pixels * pixels
    found = _source_planes(folder / source, planes)
    if form == "mask":
        if len(body) != count:
            return [f"{kind} is {len(body)} bytes, not {count}"]
        if found is not None and body != found[3]:
            return [f"{kind} is not the alpha of {source}"]
        return []
    if len(body) == 3 * count:
        return [f"{kind} is {len(body)} bytes, as long as unpacked RGB"]
    if body.startswith(_SKIPPED_START):
        return [f"{kind} starts with four zero bytes, which some readers skip as it32's header"]
    try:
        colour = unpack_channels(body, count, 3)
    except IconError as error:
        return [f"{kind}: {error}"]
    if found is not None and colour != list(found[:3]):
        return [f"{kind} is not the colour of {source}"]
    return []


def _entry_problems(data: bytes, folder: Path) -> list[str]:
    known = {kind: (pixels, source) for kind, pixels, source in ICNS_ENTRIES}
    planes: dict[Path, tuple[bytes, bytes, bytes, bytes] | None] = {}
    problems: list[str] = []
    seen: list[str] = []
    i = 8
    while i < len(data):
        if len(data) - i < 8:
            problems.append(f"it has {len(data) - i} bytes after the last entry")
            break
        kind = data[i : i + 4].decode("latin-1")
        (length,) = struct.unpack(">I", data[i + 4 : i + 8])
        if length < 8:
            problems.append(f"the entry at byte {i} says it is {length} bytes, less than 8")
            break
        if i + length > len(data):
            problems.append(f"the {kind!r} entry at byte {i} runs past the end of the file")
            break
        body = data[i + 8 : i + length]
        i += length
        if kind not in known:
            problems.append(f"it holds an entry of type {kind!r}, which the build does not write")
            continue
        if kind in seen:
            problems.append(f"it holds {kind} twice")
            continue
        seen.append(kind)
        pixels, source = known[kind]
        problems += _body_problems(kind, body, pixels, source, folder, planes)
    order = [kind for kind, _, _ in ICNS_ENTRIES]
    problems += [f"it has no {kind} entry" for kind in order if kind not in seen]
    if seen != [kind for kind in order if kind in seen]:
        problems.append(f"its entries are out of order: {', '.join(seen)}")
    return problems


def write_icons(resources: Path, folder: Path | None = None) -> list[Path]:
    """Write ``AppIcon.icns`` into ``resources`` and copy the six glyphs beside it, each mode
    0644. Returns the seven paths, the icon file first."""
    folder = _folder(folder)
    written = [resources / ICNS_FILE]
    written[0].write_bytes(icns_bytes(folder))
    for name, _ in GLYPH_FILES:
        shutil.copyfile(folder / name, resources / name)
        written.append(resources / name)
    for path in written:
        path.chmod(0o644)
    return written


def bundle_problems(app: Path, folder: Path | None = None) -> list[str]:
    """What is wrong with the icons in a built app: Info.plist's ``CFBundleIconFile`` must be
    :data:`APP_ICON`; ``AppIcon.icns`` must pass :func:`icns_problems`; and each glyph must be the
    committed file, byte for byte, and pass :func:`glyph_problems`."""
    folder = _folder(folder)
    problems: list[str] = []
    try:
        plist = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    # A cut-short XML file raises expat's error, which is none of the others.
    except (OSError, plistlib.InvalidFileException, ValueError, ExpatError) as error:
        problems.append(f"Info.plist cannot be read: {error}")
    else:
        named = plist.get("CFBundleIconFile") if isinstance(plist, dict) else None
        if named is None:
            problems.append("Info.plist has no CFBundleIconFile")
        elif named != APP_ICON:
            problems.append(f"Info.plist's CFBundleIconFile is {named!r}, not {APP_ICON!r}")
    icon = f"{RESOURCES}/{ICNS_FILE}"
    try:
        data = (app / icon).read_bytes()
    except FileNotFoundError:
        problems.append(f"{icon} is missing")
    else:
        problems += [f"{icon}: {problem}" for problem in icns_problems(data, folder)]
    for name, _ in GLYPH_FILES:
        rel = f"{RESOURCES}/{name}"
        try:
            data = (app / rel).read_bytes()
        except FileNotFoundError:
            problems.append(f"{rel} is missing")
            continue
        problems += glyph_problems(rel, data)
        try:
            same = data == (folder / name).read_bytes()
        except OSError as error:
            problems.append(f"{name} cannot be read from {_shown(folder)}: {error}")
            continue
        if not same:
            problems.append(f"{rel} is not the file the build copies from {_shown(folder)}")
    return problems
