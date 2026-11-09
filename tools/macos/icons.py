"""The Mac app's icons (decisions/0002 §6), from the PNGs committed in ``macos/Icons/``.

The build renders nothing and runs no program to make them. It checks the PNGs
(:func:`source_problems`), packs seven of them into ``Contents/Resources/AppIcon.icns``
(:func:`icns_bytes`), and copies the six menu bar glyphs beside it (:func:`write_icons`).
``build_app.py`` and the ``bundle`` check of ``check_app.py`` hold the built app to the same files
(:func:`bundle_problems`).

``AppIcon.icns`` has the layout of Apple's icon files: the four bytes ``icns``, the file's length,
then one entry per image: its type, its length with its own 8 byte header, and a whole PNG file.
This module writes no table of contents and no version entry, so the file depends on the PNGs
alone. Whether macOS reads and draws each entry is seen only on a Mac: the ``bundle`` check has
``iconutil`` read the file, and prints which files it writes.

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
from typing import Final
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
# AppIcon.icns, entry by entry: its type, its pixels, and the PNG it holds. The 16 pixel entry is
# the hand-drawn master, app-16.svg's; every other PNG is app.svg's. ic11 to ic14 are the @2x
# entries: ic11 is the 16 point icon on a Retina screen, 32 pixels.
ICNS_ENTRIES: Final = (
    ("icp4", 16, "app-16.png"),
    ("icp5", 32, "app-32.png"),
    ("icp6", 64, "app-64.png"),
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
    with the five row filters undone. Written for the glyphs, at most 36 pixels square: in pure
    Python, a large image takes long."""
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


def source_problems(folder: Path | None = None) -> list[str]:
    """What is wrong with the PNGs the build reads in ``folder`` (``macos/Icons/``): each
    ``app-<N>.png`` must be N pixels square, and each glyph must pass :func:`glyph_problems`."""
    folder = _folder(folder)
    problems: list[str] = []
    wanted = [(f"app-{size}.png", size) for size in APP_SIZES] + list(GLYPH_FILES)
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
        else:
            problems += glyph_problems(name, data)
    return problems


def icns_bytes(folder: Path | None = None) -> bytes:
    """``AppIcon.icns``: ``icns``, the file's length, then each entry of :data:`ICNS_ENTRIES` in
    order, as its type, its length with its 8 byte header, and its PNG's bytes. Big-endian, with
    no table of contents and nothing that depends on the time or the machine."""
    folder = _folder(folder)
    body = b"".join(
        kind.encode("ascii") + struct.pack(">I", 8 + len(png)) + png
        for kind, _, source in ICNS_ENTRIES
        for png in [(folder / source).read_bytes()]
    )
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def icns_problems(data: bytes, folder: Path | None = None) -> list[str]:
    """What is wrong with an icon file: its magic and its length; entries that tile it exactly;
    the types of :data:`ICNS_ENTRIES`, each once and in that order; each entry a PNG of its
    type's size, with its source's bytes; and the whole file the one :func:`icns_bytes` writes
    from ``folder``."""
    folder = _folder(folder)
    problems: list[str] = []
    try:
        expected = icns_bytes(folder)
    except OSError as error:
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


def _entry_problems(data: bytes, folder: Path) -> list[str]:
    known = {kind: (pixels, source) for kind, pixels, source in ICNS_ENTRIES}
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
        png = data[i + 8 : i + length]
        i += length
        if kind not in known:
            problems.append(f"it holds an entry of type {kind!r}, which the build does not write")
            continue
        if kind in seen:
            problems.append(f"it holds {kind} twice")
            continue
        seen.append(kind)
        pixels, source = known[kind]
        problem = _size_problem(kind, png, pixels)
        if problem is not None:
            problems.append(problem)
        else:
            try:
                same = png == (folder / source).read_bytes()
            except OSError:
                same = True  # icns_problems has said that the sources cannot be read
            if not same:
                problems.append(f"{kind} is not {source}")
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
