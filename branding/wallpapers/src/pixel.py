#!/usr/bin/env python3
"""GraveyardOS pixel-art wallpapers, black and white: a cemetery and a lone tomb.

    python3 branding/wallpapers/src/pixel.py branding/wallpapers

Writes one Plasma wallpaper package per scene (<Name>/metadata.json,
contents/images = day, contents/images_dark = night, screenshot.jpg).
Each scene is drawn on a small grid (480x270 for 16:9, 320x200 for 16:10)
with ordered (Bayer) dithering, then enlarged without smoothing: 1920x1080,
3840x2160 and 2560x1600. Deterministic (fixed seeds). Needs Pillow.
"""
import json
import math
import os
import random
import sys

from PIL import Image, ImageDraw

BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
HERE = os.path.dirname(os.path.abspath(__file__))

# The brand's tomb (branding/graveyard-icon.svg, 32x32 pixel art).
LOGO = set()
_svg = open(os.path.join(HERE, "..", "..", "graveyard-icon.svg")).read()
for _m in __import__("re").finditer(r'<rect x="(\d+)" y="(\d+)" width="1" height="1"', _svg):
    LOGO.add((int(_m[1]), int(_m[2])))


def palette(dark):
    """Greys of a scene: night (dark) or misty day."""
    if dark:
        return dict(sky0=0, sky1=40, star=235, moon=232, moon2=208, far=26, ground=14, rim=58,
                    tomb=128, tomb_hi=170, tomb_lo=82, mark=60, fence=8, tree=6, fog=70, accent=250)
    return dict(sky0=250, sky1=205, star=255, moon=255, moon2=238, far=178, ground=120, rim=150,
                tomb=60, tomb_hi=92, tomb_lo=34, mark=150, fence=40, tree=48, fog=236, accent=0)


class Canvas:
    def __init__(self, w, h, fill=0):
        self.w, self.h = w, h
        self.im = Image.new("L", (w, h), fill)
        self.px = self.im.load()
        self.d = ImageDraw.Draw(self.im)

    def set(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[x, y] = c

    def rect(self, x0, y0, x1, y1, c):
        if x1 >= x0 and y1 >= y0:
            self.d.rectangle([x0, y0, x1, y1], fill=c)

    def dither(self, x, y, v, a, b):
        """Value v between greys a and b, as a or b by the Bayer matrix."""
        t = 0 if a == b else (v - a) / (b - a)
        return b if t * 16 > BAYER[y % 4][x % 4] + 0.5 else a

    def gradient(self, y0, y1, c0, c1, levels=7):
        steps = [round(c0 + (c1 - c0) * i / (levels - 1)) for i in range(levels)]
        for y in range(max(0, y0), min(self.h, y1)):
            t = (y - y0) / max(1, y1 - y0 - 1)
            k = t * (levels - 1)
            lo = min(int(k), levels - 2)
            for x in range(self.w):
                self.px[x, y] = self.dither(x, y, k - lo, 0, 1) and steps[lo + 1] or steps[lo]

    def disc(self, cx, cy, r, c):
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + r * 0.6:
                    self.set(x, y, c)

    def fog(self, y0, y1, c, density, rng):
        ph = rng.uniform(0, 6)
        for y in range(max(0, y0), min(self.h, y1)):
            fall = 1 - abs((y - (y0 + y1) / 2) / ((y1 - y0) / 2))
            for x in range(self.w):
                d = density * fall * (0.6 + 0.4 * math.sin(x / 23 + ph + y / 9))
                if d * 16 > BAYER[y % 4][x % 4] + 0.5:
                    self.px[x, y] = c


def stars(c, rng, n, ymax, col):
    for _ in range(n):
        x, y = rng.randrange(c.w), rng.randrange(max(1, ymax))
        c.set(x, y, col)
        if rng.random() < 0.08:  # a few bright ones, as crosses
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                c.set(x + dx, y + dy, col)


def moon(c, cx, cy, r, p, rng):
    c.disc(cx, cy, r, p["moon"])
    for _ in range(3 + r // 12):  # a few small craters
        a, d = rng.uniform(0, 6.28), rng.uniform(r * 0.15, r * 0.7)
        c.disc(int(cx + d * math.cos(a)), int(cy + d * math.sin(a)), max(1, r // rng.randint(8, 12)), p["moon2"])


def hill(c, base, amp, period, ph, col, rim=None, rng=None, tufts=0):
    """Ground from a wavy line down; returns its height at each x."""
    top = []
    for x in range(c.w):
        y = int(base + amp * math.sin(x / period + ph) + amp * 0.35 * math.sin(x / (period / 3.1) + ph * 2))
        top.append(y)
        c.rect(x, y, x, c.h - 1, col)
        if rim is not None:
            c.set(x, y, rim)
    if rng:
        for _ in range(tufts):
            x = rng.randrange(c.w)
            c.set(x, top[x] - 1, rim)
            if rng.random() < 0.5:
                c.set(x + 1, top[x] - 2, rim)
    return top


def tomb(c, x, base, kind, w, h, p, rng, col=None):
    """A headstone standing on `base`, centred on x."""
    body, hi, lo = (col, col, col) if col is not None else (p["tomb"], p["tomb_hi"], p["tomb_lo"])
    x0, x1 = x - w // 2, x - w // 2 + w - 1
    top = base - h
    if kind == "round":
        r = w // 2
        c.rect(x0, top + r, x1, base, body)
        for dy in range(r + 1):
            half = int(math.sqrt(max(0, r * r - (r - dy) ** 2)) + 0.5)
            c.rect(x - half, top + dy, x - half + 2 * half - (1 if w % 2 == 0 else 0), top + dy, body)
    elif kind == "cross":
        t = max(2, w // 3)
        c.rect(x - t // 2, top, x - t // 2 + t - 1, base, body)
        arm = top + max(2, h // 4)
        c.rect(x0, arm, x1, arm + t - 1, body)
        x0, x1 = x - t // 2, x - t // 2 + t - 1
    elif kind == "obelisk":
        c.rect(x0, top + w, x1, base, body)
        for dy in range(w):
            half = (dy * w) // (2 * w)
            c.rect(x - half, top + dy, x - half + max(0, 2 * half - 1), top + dy, body)
    else:  # "slab": a block with its corners cut
        c.rect(x0, top + 1, x1, base, body)
        c.rect(x0 + 1, top, x1 - 1, top, body)
    if col is None:
        # light on the left, shadow on the right, a mark, a plinth
        for yy in range(top, base + 1):
            row = [xx for xx in range(x0 - 1, x1 + 2) if 0 <= xx < c.w and 0 <= yy < c.h and c.px[xx, yy] == body]
            if row:
                c.set(row[0], yy, hi)
                c.set(row[-1], yy, lo)
        if kind in ("round", "slab") and h >= 8:
            my = top + max(3, h // 3)
            c.rect(x - max(1, w // 4), my, x + max(1, w // 4) - (1 if w % 2 == 0 else 0), my, p["mark"])
            c.rect(x - max(1, w // 6), my + 2, x + max(1, w // 6) - (1 if w % 2 == 0 else 0), my + 2, p["mark"])
        c.rect(x0 - 1, base, x1 + 1, base + 1, lo)


def logo(c, x, base, k, col):
    """The brand's tomb, k grid pixels per logo pixel, standing on base."""
    xs = [a for a, _ in LOGO]
    ys = [b for _, b in LOGO]
    w, h = (max(xs) - min(xs) + 1) * k, (max(ys) - min(ys) + 1) * k
    ox, oy = x - w // 2 - min(xs) * k, base - h - min(ys) * k
    for a, b in LOGO:
        c.rect(ox + a * k, oy + b * k, ox + a * k + k - 1, oy + b * k + k - 1, col)


def fence(c, y_base, h, col, x0=0, x1=None, step=9):
    x1 = c.w - 1 if x1 is None else x1
    for x in range(x0, x1 + 1, step):
        c.rect(x, y_base - h, x, y_base, col)
        c.set(x, y_base - h - 1, col)
        c.set(x - 1, y_base - h, col)
        c.set(x + 1, y_base - h, col)
    for ry in (y_base - h + 3, y_base - 3):
        c.rect(x0, ry, x1, ry, col)


def tree(c, x, y, ang, length, depth, col, rng):
    """A dead tree: branches, thinner and shorter as they split."""
    if depth == 0 or length < 2:
        return
    x2 = x + length * math.cos(ang)
    y2 = y - length * math.sin(ang)
    c.d.line([(x, y), (x2, y2)], fill=col, width=max(1, depth - 1))
    for da in (rng.uniform(0.25, 0.6), -rng.uniform(0.25, 0.6)):
        if rng.random() < 0.9:
            tree(c, x2, y2, ang + da, length * rng.uniform(0.6, 0.78), depth - 1, col, rng)


# --- scenes --------------------------------------------------------------------

def graveyard_night(w, h, dark):
    p, rng = palette(dark), random.Random(11)
    c = Canvas(w, h)
    c.gradient(0, int(h * 0.72), p["sky0"], p["sky1"])
    if dark:
        stars(c, rng, w // 3, int(h * 0.55), p["star"])
    moon(c, int(w * 0.22), int(h * 0.24), max(8, h // 11), p, random.Random(3))
    hill(c, int(h * 0.60), h * 0.03, w / 5.0, 1.3, p["far"])
    # far tombs, small silhouettes on the far hill
    for i in range(14):
        x = int((i + 0.5) * w / 14 + rng.uniform(-6, 6))
        tomb(c, x, int(h * 0.60 + h * 0.03 * math.sin(x / (w / 5.0) + 1.3)) + 1,
             rng.choice(["round", "cross", "slab"]), 4, rng.randint(5, 8), p, rng, col=p["far"] + (14 if dark else -24))
    top = hill(c, int(h * 0.74), h * 0.025, w / 3.0, 0.4, p["ground"], p["rim"], rng, tufts=w // 4)
    tree(c, int(w * 0.83), top[int(w * 0.83)], math.pi / 2 + 0.08, h * 0.16, 6, p["tree"], random.Random(5))
    # two rows of headstones
    for row, (n, size) in enumerate(((11, 1.0), (8, 1.35))):
        for i in range(n):
            x = int((i + 0.5 + (0.5 if row else 0)) * w / n * (0.98 if row else 1)) + rng.randint(-4, 4)
            if row and abs(x - w * 0.83) < 18:
                continue
            x = max(6, min(w - 7, x))
            base = top[x] + (2 if row == 0 else int(h * 0.09))
            kind = rng.choice(["round", "round", "cross", "slab", "obelisk"])
            ww = int(rng.choice([8, 10, 12]) * size) // 2 * 2
            hh = int(rng.randint(12, 18) * size) + (8 if kind == "obelisk" else 0)
            tomb(c, x, base, kind, ww, hh, p, rng)
    c.fog(int(h * 0.70), int(h * 0.86), p["fog"], 0.42, rng)
    fence(c, h - 4, int(h * 0.07), p["fence"])
    return c.im


def lone_tomb(w, h, dark):
    p, rng = palette(dark), random.Random(21)
    c = Canvas(w, h)
    c.gradient(0, h, p["sky0"], p["sky1"])
    if dark:
        stars(c, rng, w // 4, int(h * 0.6), p["star"])
    moon(c, w // 2, int(h * 0.42), int(h * 0.27), p, random.Random(8))
    top = hill(c, int(h * 0.80), h * 0.035, w / 2.2, 4.6, p["ground"], p["rim"], rng, tufts=w // 5)
    logo(c, w // 2, top[w // 2] + 2, max(2, h // 70), p["accent"] if not dark else 12)
    # a few flowers at its foot
    for dx in (-9, -6, 7, 10):
        x = w // 2 + dx
        c.set(x, top[x] - 1, p["tomb_hi"])
        c.set(x, top[x] - 2, p["star"] if dark else p["tomb_lo"])
    c.fog(int(h * 0.84), h, p["fog"], 0.25, rng)
    return c.im


SCENES = {
    "Graveyard-Night": (graveyard_night, "Graveyard by night",
                        "A pixel-art cemetery: headstones in rows, an iron fence, a dead tree, fog; moonlight or misty day"),
    "Lone-Tomb": (lone_tomb, "Lone tomb",
                  "The GraveyardOS tomb alone on its hill, before a huge moon"),
}
# grid size -> the screen sizes made from it (integer enlargements)
GRIDS = {(480, 270): [(1920, 1080), (3840, 2160)], (320, 200): [(2560, 1600)]}


def main():
    out = sys.argv[1]
    for name, (scene, title, desc) in SCENES.items():
        d = os.path.join(out, name)
        for dark, sub in ((False, "images"), (True, "images_dark")):
            os.makedirs(os.path.join(d, "contents", sub), exist_ok=True)
            for (gw, gh), sizes in GRIDS.items():
                im = scene(gw, gh, dark)
                for sw, sh in sizes:
                    im.resize((sw, sh), Image.NEAREST).save(os.path.join(d, "contents", sub, f"{sw}x{sh}.png"), optimize=True)
        prev = Image.open(os.path.join(d, "contents", "images_dark", "1920x1080.png")).convert("RGB")
        prev.resize((400, 225), Image.NEAREST).save(os.path.join(d, "contents", "screenshot.jpg"), quality=92)
        meta = {"KPlugin": {"Authors": [{"Name": "GraveyardOS"}], "Description": desc + " (pixel art, black and white)",
                            "Id": name, "License": "CC-BY-SA-4.0", "Name": title}}
        with open(os.path.join(d, "metadata.json"), "w") as f:
            json.dump(meta, f, indent=4)
            f.write("\n")
        print("wrote", d)


if __name__ == "__main__":
    main()
