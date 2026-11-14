#!/usr/bin/env python3
"""The GRUB theme's images: the tomb logo (from branding/graveyard-icon.svg,
pixel art, scaled without smoothing) and the 9 pieces of the selected item's
box (white, rounded corners). Pure Python (zlib PNG writer).

    python3 branding/grub/make-images.py
"""
import os
import re
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "graveyard")


def png(path, w, h, pixel):
    rows = b"".join(b"\0" + bytes(c for x in range(w) for c in pixel(x, y)) for y in range(h))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


# the logo: every 1x1 rect of the 32x32 SVG, 4 pixels per pixel
svg = open(os.path.join(HERE, "..", "graveyard-icon.svg")).read()
on = {}
for m in re.finditer(r'<rect x="(\d+)" y="(\d+)" width="1" height="1" fill="#([0-9A-Fa-f]{6})"', svg):
    on[(int(m[1]), int(m[2]))] = bytes.fromhex(m[3])
S = 4
png(os.path.join(OUT, "logo.png"), 32 * S, 32 * S,
    lambda x, y: (tuple(on[(x // S, y // S)]) + (255,)) if (x // S, y // S) in on else (0, 0, 0, 0))

# the selected item: a white box with corners of radius R
R = 6


def corner(cx, cy):
    def pixel(x, y):
        # coverage of the quarter disc centred on (cx, cy), 4x4 samples
        n = sum(1 for i in range(4) for j in range(4)
                if (x + (i + 0.5) / 4 - cx) ** 2 + (y + (j + 0.5) / 4 - cy) ** 2 <= R * R)
        return (255, 255, 255, n * 255 // 16)
    return pixel


white = lambda x, y: (255, 255, 255, 255)  # noqa: E731
for name, (w, h, f) in {
    "select_nw.png": (R, R, corner(R, R)), "select_ne.png": (R, R, corner(0, R)),
    "select_sw.png": (R, R, corner(R, 0)), "select_se.png": (R, R, corner(0, 0)),
    "select_n.png": (1, R, white), "select_s.png": (1, R, white),
    "select_w.png": (R, 1, white), "select_e.png": (R, 1, white), "select_c.png": (1, 1, white),
}.items():
    png(os.path.join(OUT, name), w, h, f)
# the terminal's box (GRUB's command line, messages): a thin white line
B = 2
line = lambda x, y: (255, 255, 255, 255)  # noqa: E731
black = lambda x, y: (0, 0, 0, 255)  # noqa: E731
for name, (w, h, f) in {
    "term_nw.png": (B, B, line), "term_ne.png": (B, B, line), "term_sw.png": (B, B, line), "term_se.png": (B, B, line),
    "term_n.png": (1, B, line), "term_s.png": (1, B, line), "term_w.png": (B, 1, line), "term_e.png": (B, 1, line),
    "term_c.png": (1, 1, black),
}.items():
    png(os.path.join(OUT, name), w, h, f)
print("wrote", sorted(os.listdir(OUT)))
