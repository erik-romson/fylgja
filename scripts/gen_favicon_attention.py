"""Generate ``src/fylgja/api/static/favicon-attention.png``: the tab icon with an attention dot.

Reads the committed 64x64 RGBA ``favicon.png`` and draws a filled circle (radius 13, centre
(49, 15), ``#C77700``) with a 2 px white ring on it. Standard library only; run it by hand:

    uv run --frozen python scripts/gen_favicon_attention.py

Tests import :func:`render` and never call :func:`main`.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

__all__ = ["CENTRE", "DOT", "RADIUS", "SIZE", "main", "render"]

STATIC = Path(__file__).resolve().parent.parent / "src" / "fylgja" / "api" / "static"
SIZE = 64
CENTRE = (49, 15)
RADIUS = 13
RING = 2
DOT = (0xC7, 0x77, 0x00, 0xFF)
WHITE = (0xFF, 0xFF, 0xFF, 0xFF)
_MAGIC = b"\x89PNG\r\n\x1a\n"
_BPP = 4


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    if not png.startswith(_MAGIC):
        raise ValueError("not a PNG")
    out: list[tuple[bytes, bytes]] = []
    at = len(_MAGIC)
    while at < len(png):
        (length,) = struct.unpack(">I", png[at : at + 4])
        out.append((png[at + 4 : at + 8], png[at + 8 : at + 8 + length]))
        at += 12 + length
    return out


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _decode(png: bytes) -> list[bytearray]:
    """The rows (RGBA bytes) of a 64x64, 8-bit RGBA, non-interlaced PNG."""
    chunks = _chunks(png)
    header = dict(chunks)[b"IHDR"]
    width, height, depth, colour, _compression, _filter, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if (width, height, depth, colour, interlace) != (SIZE, SIZE, 8, 6, 0):
        raise ValueError("expected a 64x64, 8-bit RGBA, non-interlaced PNG")
    raw = zlib.decompress(b"".join(data for kind, data in chunks if kind == b"IDAT"))
    stride = width * _BPP
    rows: list[bytearray] = []
    previous = bytearray(stride)
    at = 0
    for _ in range(height):
        mode = raw[at]
        line = bytearray(raw[at + 1 : at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - _BPP] if i >= _BPP else 0
            up = previous[i]
            corner = previous[i - _BPP] if i >= _BPP else 0
            if mode == 1:
                line[i] = (line[i] + left) & 0xFF
            elif mode == 2:
                line[i] = (line[i] + up) & 0xFF
            elif mode == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif mode == 4:
                line[i] = (line[i] + _paeth(left, up, corner)) & 0xFF
            elif mode != 0:
                raise ValueError(f"unknown PNG filter {mode}")
        rows.append(line)
        previous = line
    return rows


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def render(source: bytes) -> bytes:
    """``source`` (the 64x64 RGBA ``favicon.png``) with the attention dot drawn on it."""
    rows = _decode(source)
    cx, cy = CENTRE
    for y in range(SIZE):
        for x in range(SIZE):
            squared = (x - cx) ** 2 + (y - cy) ** 2
            if squared > RADIUS**2:
                continue
            inner = (RADIUS - RING) ** 2
            colour = DOT if squared <= inner else WHITE
            rows[y][x * _BPP : (x + 1) * _BPP] = bytes(colour)
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (
        _MAGIC
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def main() -> None:
    out = render((STATIC / "favicon.png").read_bytes())
    (STATIC / "favicon-attention.png").write_bytes(out)
    print(f"wrote favicon-attention.png ({len(out)} bytes)")


if __name__ == "__main__":
    main()
