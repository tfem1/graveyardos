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
import urllib.parse
import urllib.request
import zipfile

from PIL import Image, ImageOps

SOURCES = {
    # name: (url, author, page, license)
    "larry-graveyard1": ("https://opengameart.org/sites/default/files/Graveyard1.png", "LarryIRL",
                         "https://opengameart.org/content/seamless-2d-graveyard-background", "CC0-1.0"),
    "larry-graveyard2": ("https://opengameart.org/sites/default/files/Graveyard2.png", "LarryIRL",
                         "https://opengameart.org/content/seamless-2d-graveyard-background", "CC0-1.0"),
    "gothicvania": ("https://opengameart.org/sites/default/files/gothicvania-cemetery-files_1.zip",
                    "Luis Zuno (ansimuz)", "https://opengameart.org/content/gothicvania-cemetery-pack", "CC0-1.0"),
    "church": ("https://opengameart.org/sites/default/files/gothicvania%20church%20files.zip",
               "Luis Zuno (ansimuz)", "https://opengameart.org/content/gothicvania-church-pack", "CC0-1.0"),
    "town": ("https://opengameart.org/sites/default/files/gothicvania-town-files.zip",
             "Luis Zuno (ansimuz)", "https://opengameart.org/content/gothicvania-town", "CC0-1.0"),
    "swamp": ("https://opengameart.org/sites/default/files/gothicvania_swamp_files.zip",
              "Luis Zuno (ansimuz)", "https://opengameart.org/content/gotthicvania-swamp", "CC0-1.0"),
}

# SHA-256 of each download, as first fetched (inspect prints them)
PINS = {
    "larry-graveyard1": "515e293086c42dee344c3771f1505bf14ab2eadd0ae2c7f3fd284bf612bc8c8e",
    "larry-graveyard2": "017dd73f0a181f0a0c22875c474021292a53626f5d401875b03a17ed1c99c024",
    "gothicvania": "992f3792b49391da4d97c29da47c8f3d2f438ca10d7db03f9a3166d655600871",
    "church": "c4c78b80905022d3325212b58271044bdd4b92d1f7724fe94aee0107ba3c8169",
    "town": "11c32d7416814e1832c502e76426623065792c3d95a8514be42e59fe6b0b0651",
    "swamp": "a400c0a7393ca8ae73142adef9dd970d7cd561bb54852d18678843c3b2eef6a3",
}


def fetch(work, name):
    url = SOURCES[name][0]
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, urllib.parse.unquote(os.path.basename(url)))
    if not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": "graveyardos-wallpapers"})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            f.write(r.read())
    got = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if PINS.get(name) and got != PINS[name]:
        raise SystemExit(f"{name}: sha256 {got}, expected {PINS[name]}")
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
        k = min(1.0, (840 if len(images) <= 4 else 300) / max(im.width, 1), (480 if len(images) <= 4 else 200) / max(im.height, 1))
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


def inspect(work, only=None):
    os.makedirs(work, exist_ok=True)
    allims = []
    for name in only or SOURCES:
        path = fetch(work, name)
        print(f"== {name}: {os.path.basename(path)} sha256 {hashlib.sha256(open(path, 'rb').read()).hexdigest()}")
        for m, im in pictures(path):
            if (im.width * im.height < 64 * 64 or im.width < 160) and m:
                continue  # small sprites: not backgrounds
            if m and any(k in m.lower() for k in ("sprite", "gif", "hero", "enemy", "player", "fx")):
                continue
            print(f"   [{len(allims)}] {m or '(the picture)'} {im.width}x{im.height}")
            allims.append(im)
    show(sheet(allims))


def grey(im):
    """Luminance, keeping transparency."""
    return Image.merge("LA", (im.convert("L"), im.getchannel("A")))


def member(work, src, suffix):
    for m, im in pictures(fetch(work, src)):
        if m.endswith(suffix):
            return im
    raise SystemExit(f"{src}: no {suffix}")


def gothic(work, gw, gh):
    """GothicVania's three loopable layers (moonlit sky, mountains, graveyard),
    tiled across and standing on the bottom edge; the sky carried up."""
    env = "demo/assets/environment/"
    layers = [grey(member(work, "gothicvania", env + n)) for n in ("bg-moon.png", "bg-mountains.png", "bg-graveyard.png")]
    lw, lh = layers[0].size
    top = layers[0].crop((0, 0, lw, 1)).resize((1, 1), Image.BOX).getpixel((0, 0))[0]
    canvas = Image.new("LA", (gw, gh), (top, 255))
    x0 = (gw - lw) // 2 % lw - lw  # centred: the moon in the middle
    for im in layers:
        for x in range(x0, gw, lw):
            canvas.paste(im, (x, gh - lh), im)
    return ImageOps.autocontrast(canvas.convert("L"), cutoff=0.5)


def layered(work, src, layers, gw, gh, sky=None):
    """Loopable layers (zip members, back to front) tiled across the grid,
    standing on its bottom edge, centred; above them the top colour of the
    first. A layer may be a list of members, used in turn along the row."""
    ims = [[grey(member(work, src, m)) for m in (l if isinstance(l, list) else [l])] for l in layers]
    first = ims[0][0]
    top = first.crop((0, 0, first.width, 1)).resize((1, 1), Image.BOX).getpixel((0, 0))[0] if sky is None else sky
    canvas = Image.new("LA", (gw, gh), (top, 255))
    for row in ims:
        lw = row[0].width
        x = (gw // 2 - lw // 2) % lw - lw
        i = 0
        while x < gw:
            im = row[i % len(row)]
            canvas.paste(im, (x, gh - im.height), im)
            x += im.width
            i += 1
    return ImageOps.autocontrast(canvas.convert("L"), cutoff=0.5)


def town(work, gw, gh):
    env = "PNG/environment/layers/"
    return layered(work, "town", [env + "background.png", env + "middleground.png"], gw, gh)


def swamp(work, gw, gh):
    env = "Evironment/"
    return layered(work, "swamp", [[env + "mid-layer-01.png", env + "mid-layer-02.png"], env + "trees.png"], gw, gh)


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def eye_bitmap():
    """The owner's Eye of Horus (branding/fastfetch/graveyard.txt, braille):
    every braille dot one pixel, 2 x 4 per character."""
    lines = [l for l in open(os.path.join(REPO, "branding", "fastfetch", "graveyard.txt"), encoding="utf-8").read().split("\n") if l]
    w, h = max(len(l) for l in lines) * 2, len(lines) * 4
    im = Image.new("L", (w, h), 0)
    px = im.load()
    dots = [(0, 0, 0), (1, 0, 1), (2, 0, 2), (3, 1, 0), (4, 1, 1), (5, 1, 2), (6, 0, 3), (7, 1, 3)]
    for r, line in enumerate(lines):
        for c, ch in enumerate(line):
            b = ord(ch) - 0x2800
            if 0 <= b < 256:
                for bit, dx, dy in dots:
                    if b >> bit & 1:
                        px[c * 2 + dx, r * 4 + dy] = 255
    return im


def stamp(canvas, mask, x, y, ink=255, outline=0):
    """A one-colour shape with a one-pixel outline, pixel exact."""
    from PIL import ImageFilter
    ring = mask.filter(ImageFilter.MaxFilter(3))
    canvas.paste(outline, (x, y), ring)
    canvas.paste(ink, (x, y), mask)


def eye(work, gw, gh):
    """GraveyardOS Cemetery with the Eye of Horus on the moon, and the name
    above it, in pixels."""
    from PIL import ImageDraw, ImageFont
    c = gothic(work, gw, gh)
    e = eye_bitmap()
    size = 22 if gh >= 270 else 18
    font = ImageFont.truetype(os.path.join(REPO, "branding", "sddm", "graveyard", "fonts", "Rubik-800.ttf"), size)
    text = Image.new("L", (gw, size + 8), 0)
    d = ImageDraw.Draw(text)
    d.fontmode = "1"  # no smoothing: pixel letters
    d.text((gw // 2, 2), "GraveyardOS", font=font, fill=255, anchor="mt")
    # the eye on the moon (the moon layer stands on the bottom edge), the name above
    ey = gh - 224 + 92 - e.height // 2
    ty = max(4, ey - size - 10)
    stamp(c, text, 0, ty)
    stamp(c, e, gw // 2 - e.width // 2, ey)
    return c


WALLPAPERS = {
    # name: (scene, title, description, sources)
    "GraveyardOS-Cemetery": (gothic, "GraveyardOS cemetery", "A moonlit graveyard in pixel art", ["gothicvania"]),
    "GraveyardOS-Town": (town, "GraveyardOS town", "An old town and its church under a cloudy sky, in pixel art", ["town"]),
    "GraveyardOS-Eye": (eye, "GraveyardOS eye", "The GraveyardOS name over the Eye of Horus on the moon of a cemetery, in pixel art",
                        ["gothicvania"]),
    "Haunted-Swamp": (swamp, "Haunted swamp", "A haunted swamp forest, a face in its tree, in pixel art", ["swamp"]),
}
GRIDS = {(480, 270): [(1920, 1080), (3840, 2160)], (320, 200): [(2560, 1600)]}


def build(work, out):
    for name, (scene, title, desc, srcs) in WALLPAPERS.items():
        d = os.path.join(out, name, "contents", "images")
        os.makedirs(d, exist_ok=True)
        for (gw, gh), sizes in GRIDS.items():
            im = scene(work, gw, gh)
            for sw, sh in sizes:
                im.resize((sw, sh), Image.NEAREST).save(os.path.join(d, f"{sw}x{sh}.png"), optimize=True)
        Image.open(os.path.join(d, "1920x1080.png")).convert("RGB").resize((400, 225), Image.NEAREST).save(
            os.path.join(out, name, "contents", "screenshot.jpg"), quality=92)
        meta = {"KPlugin": {
            "Authors": [{"Name": SOURCES[s][1]} for s in srcs],
            "Description": f"{desc}, black and white. From {'; '.join(SOURCES[s][2] for s in srcs)} ({SOURCES[srcs[0]][3]})",
            "Id": name, "License": SOURCES[srcs[0]][3], "Name": title}}
        with open(os.path.join(out, name, "metadata.json"), "w") as f:
            json.dump(meta, f, indent=4)
            f.write("\n")
        print("wrote", name)
    show(sheet([Image.open(os.path.join(out, n, "contents", "images", "1920x1080.png")).convert("RGBA")
                for n in WALLPAPERS], width=1700))


if __name__ == "__main__":
    if sys.argv[1] == "inspect":
        inspect(sys.argv[2], sys.argv[3:] or None)
    else:
        build(sys.argv[2], sys.argv[3])
