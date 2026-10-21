"""Generate a placeholder app icon (PNG) for the Tauri bundle.

Kept dependency-free (no Pillow) so it runs on a bare Python install:
a rounded green square with a lime "z" (matches the brand mark in the UI).
Run `pnpm tauri icon <png>` afterwards to produce every platform size.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 1024
BACKGROUND = (31, 117, 86)
FOREGROUND = (213, 243, 106)


def _rounded(x: int, y: int) -> bool:
    radius = int(SIZE * 0.22)
    for corner_x, corner_y in (
        (radius, radius),
        (SIZE - radius, radius),
        (radius, SIZE - radius),
        (SIZE - radius, SIZE - radius),
    ):
        if (x < radius and corner_x == radius and y < radius and corner_y == radius) or (
            x >= SIZE - radius and corner_x == SIZE - radius and y < radius and corner_y == radius
        ) or (
            x < radius and corner_x == radius and y >= SIZE - radius and corner_y == SIZE - radius
        ) or (
            x >= SIZE - radius
            and corner_x == SIZE - radius
            and y >= SIZE - radius
            and corner_y == SIZE - radius
        ):
            if (x - corner_x) ** 2 + (y - corner_y) ** 2 > radius**2:
                return False
    return True


def _in_z(x: int, y: int) -> bool:
    """A blocky 'z' glyph."""
    left, right = int(SIZE * 0.28), int(SIZE * 0.72)
    top, bottom = int(SIZE * 0.3), int(SIZE * 0.7)
    thickness = int(SIZE * 0.075)
    if not (left <= x <= right and top <= y <= bottom):
        return False
    if y <= top + thickness or y >= bottom - thickness:
        return True
    ratio = (y - top) / (bottom - top)
    centre = right - ratio * (right - left)
    return abs(x - centre) <= thickness * 0.7


def build_rows() -> list[bytes]:
    rows: list[bytes] = []
    for y in range(SIZE):
        row = bytearray([0])
        for x in range(SIZE):
            if not _rounded(x, y):
                colour = BACKGROUND
            else:
                colour = FOREGROUND if _in_z(x, y) else BACKGROUND
            row.extend(colour)
        rows.append(bytes(row))
    return rows


def write_png(path: Path) -> None:
    raw = b"".join(build_rows())

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "app-icon.png")
    write_png(target)
    print(f"wrote {target} ({target.stat().st_size} bytes)")
