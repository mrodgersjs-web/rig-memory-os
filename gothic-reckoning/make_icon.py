"""
Generate icon-512.png for Gothic Reckoning — dark abyss background
with a centered ember wolf-glyph and gothic border ring. Pillow-free:
writes a raw PNG via zlib+struct (stdlib only, FDE-style).
"""
import struct
import zlib

SIZE = 512
BG = (13, 9, 8)           # --abyss
EMBER = (204, 122, 46)  # --ember
GOLD = (184, 149, 72)   # --gold


def png_chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make(path):
    raw = bytearray()
    cx, cy = SIZE // 2, SIZE // 2
    r_body, r_head = 95, 62

    for y in range(SIZE):
        raw.append(0)  # filter: none
        for x in range(SIZE):
            dx, dy = x - cx, y - cy
            dist = (dx * dx + dy * dy) ** 0.5
            in_body = dist < r_body
            in_head = dist < r_head and dy < 0
            # ears: two triangles above head
            ear_l = (abs(dx - 28) + abs(dy + 52) < 30) and dy < -20
            ear_r = (abs(dx + 28) + abs(dy + 52) < 30) and dy < -20
            # eyes: two bright dots
            eye = ((dx - 18) ** 2 + (dy + 30) ** 2 < 36) or ((dx + 18) ** 2 + (dy + 30) ** 2 < 36)
            # border ring
            ring = 190 < dist < 196
            if ring:
                raw.extend(GOLD)
            elif eye:
                raw.extend((255, 220, 180))
            elif ear_l or ear_r or in_head or in_body:
                raw.extend(EMBER)
            else:
                # subtle radial glow around center
                glow = max(0, 1 - dist / 260) * 0.10
                r = int(BG[0] + glow * EMBER[0] * 0.4)
                g = int(BG[1] + glow * EMBER[1] * 0.4)
                b = int(BG[2] + glow * EMBER[2] * 0.4)
                raw.extend((r, g, b))

    comp = zlib.compress(bytes(raw), 9)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", comp)
        + png_chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(png)
    print(f"icon: {path} ({len(png)} bytes)")


if __name__ == "__main__":
    make("/tmp/fde-repo/gothic-reckoning/public/icon-512.png")
