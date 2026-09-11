"""Generate ui/app-icon.png (1024x1024) without third-party libraries, then run
`npx tauri icon app-icon.png` to produce the platform icon set in src-tauri/icons."""

import struct
import zlib
from pathlib import Path

SIZE = 1024
BG = (12, 18, 32)
RING = (46, 204, 113)  # green ring = liquidity on both sides
CORE = (241, 196, 15)  # amber core = RXD


def pixel(x: int, y: int) -> tuple[int, int, int, int]:
    cx = cy = SIZE / 2
    r = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    if r > 470:
        return (*BG, 0)
    if 300 < r < 400:
        return (*RING, 255)
    if r <= 200:
        return (*CORE, 255)
    return (*BG, 255)


def main() -> None:
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # filter type none
        for x in range(SIZE):
            raw.extend(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    Path(__file__).with_name("app-icon.png").write_bytes(png)
    print("wrote app-icon.png")


if __name__ == "__main__":
    main()
