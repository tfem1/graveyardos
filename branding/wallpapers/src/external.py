#!/usr/bin/env python3
"""Wallpapers from free pixel art found online (CC0), made black and white.

    python3 branding/wallpapers/src/external.py inspect WORKDIR
    python3 branding/wallpapers/src/external.py build WORKDIR OUTDIR

`inspect` downloads every source below, lists what the archives hold and
prints a contact sheet of their pictures (base64 PNG between GRAVEYARD-SCREEN
markers, as iso/screenshot.py does), to choose the layers of each wallpaper.
`build` makes the Plasma packages of WALLPAPERS: the chosen pictures stacked
back to front, in greys, enlarged without smoothing by a whole factor, then
tiled or cropped to 16:9 and 16:10.

Run in CI (the network of the development container reaches package
registries only). Needs Pillow.
"""
import base64
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile

from PIL import Image

SOURCES = {
    # name: (url, author, page, license)
    "larry-graveyard1": ("https://opengameart.org/sites/default/files/Graveyard1.png", "LarryIRL",
                         "https://opengameart.org/content/seamless-2d-graveyard-background", "CC0-1.0"),
    "larry-graveyard2": ("https://opengameart.org/sites/default/files/Graveyard2.png", "LarryIRL",
                         "https://opengameart.org/content/seamless-2d-graveyard-background", "CC0-1.0"),
    "gothicvania": ("https://opengameart.org/sites/default/files/gothicvania-cemetery-files_1.zip",
                    "Luis Zuno (ansimuz)", "https://opengameart.org/content/gothicvania-cemetery-pack", "CC0-1.0"),
    "onebit": ("https://opengameart.org/sites/default/files/1-bit_pixel_art_graveyard_pack.zip", "Fava Beans",
               "https://opengameart.org/content/1-bit-graveyard-pixel-art-asset-pack", "CC0-1.0"),
}

# name -> (title, description, [(source, member in its zip or "", ...)] back to front)
WALLPAPERS = {}


def fetch(work, name):
    url = SOURCES[name][0]
    path = os.path.join(work, os.path.basename(url))
    if not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": "graveyardos-wallpapers"})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            f.write(r.read())
    return path


def pictures(path):
    """(member name, PIL image) of a PNG or of every PNG in a zip."""
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for m in sorted(z.namelist()):
                if m.lower().endswith((".png", ".gif")) and "__MACOSX" not in m:
                    try:
                        yield m, Image.open(io.BytesIO(z.read(m))).convert("RGBA")
                    except Exception as e:  # noqa: BLE001
                        print(f"   {m}: {e}")
    else:
        yield "", Image.open(path).convert("RGBA")


def sheet(images, width=1600):
    """Thumbnails in rows, each labelled by its index."""
    thumbs = []
    for im in images:
        k = min(1.0, 300 / max(im.width, 1), 200 / max(im.height, 1))
        t = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.NEAREST)
        bg = Image.new("RGBA", t.size, (255, 0, 255, 255))
        bg.alpha_composite(t)
        thumbs.append(bg.convert("RGB"))
    rows, row, x, h = [], [], 0, 0
    for t in thumbs:
        if x + t.width + 8 > width and row:
            rows.append((row, h)); row, x, h = [], 0, 0
        row.append(t); x += t.width + 8; h = max(h, t.height)
    if row:
        rows.append((row, h))
    out = Image.new("RGB", (width, sum(h + 8 for _, h in rows) or 1), (40, 40, 40))
    y = 0
    for row, h in rows:
        x = 0
        for t in row:
            out.paste(t, (x, y)); x += t.width + 8
        y += h + 8
    return out


def show(im):
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    text = base64.b64encode(buf.getvalue()).decode()
    print("GRAVEYARD-SCREEN-BEGIN")
    for i in range(0, len(text), 76):
        print(text[i:i + 76])
    print("GRAVEYARD-SCREEN-END")


def inspect(work):
    os.makedirs(work, exist_ok=True)
    allims = []
    for name in SOURCES:
        path = fetch(work, name)
        print(f"== {name}: {os.path.basename(path)} sha256 {hashlib.sha256(open(path, 'rb').read()).hexdigest()}")
        for m, im in pictures(path):
            if im.width * im.height < 64 * 64 and m:
                continue  # small sprites: not backgrounds
            print(f"   [{len(allims)}] {m or '(the picture)'} {im.width}x{im.height}")
            allims.append(im)
    show(sheet(allims))


def grey(im):
    """Luminance, keeping transparency."""
    a = im.getchannel("A")
    g = im.convert("L")
    return Image.merge("LA", (g, a))


def compose(work, layers, gw, gh):
    """The layers stacked on a grid gw x gh, each tiled across and anchored at the bottom."""
    canvas = Image.new("LA", (gw, gh), (0, 255))
    for src, member, scale in layers:
        path = fetch(work, src)
        im = dict(pictures(path))[member]
        im = grey(im)
        if scale != 1:
            im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
        y = gh - im.height
        for x in range(0, gw, im.width):
            canvas.alpha_composite(im, (x, y)) if hasattr(canvas, "alpha_composite") else canvas.paste(im, (x, y), im)
    return canvas.convert("L")


def build(work, out):
    for name, (title, desc, layers, grids) in WALLPAPERS.items():
        d = os.path.join(out, name, "contents", "images")
        os.makedirs(d, exist_ok=True)
        for (gw, gh), sizes in grids.items():
            im = compose(work, layers, gw, gh)
            for sw, sh in sizes:
                im.resize((sw, sh), Image.NEAREST).save(os.path.join(d, f"{sw}x{sh}.png"), optimize=True)
        Image.open(os.path.join(d, "1920x1080.png")).convert("RGB").resize((400, 225), Image.NEAREST).save(
            os.path.join(out, name, "contents", "screenshot.jpg"), quality=92)
        srcs = sorted({s for s, _, _ in layers})
        meta = {"KPlugin": {
            "Authors": [{"Name": SOURCES[s][1]} for s in srcs],
            "Description": desc + " — " + "; ".join(f"{SOURCES[s][2]} ({SOURCES[s][3]})" for s in srcs)
                           + "; made black and white for GraveyardOS",
            "Id": name, "License": SOURCES[srcs[0]][3], "Name": title}}
        with open(os.path.join(out, name, "metadata.json"), "w") as f:
            json.dump(meta, f, indent=4)
            f.write("\n")
        print("wrote", name)


if __name__ == "__main__":
    if sys.argv[1] == "inspect":
        inspect(sys.argv[2])
    else:
        build(sys.argv[2], sys.argv[3])
