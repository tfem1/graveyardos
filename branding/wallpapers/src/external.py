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
import random
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
    "onebit": ("https://opengameart.org/sites/default/files/1-bit_pixel_art_graveyard_pack.zip", "Fava Beans",
               "https://opengameart.org/content/1-bit-graveyard-pixel-art-asset-pack", "CC0-1.0"),
}

# SHA-256 of each download, as first fetched (inspect prints them)
PINS = {
    "larry-graveyard1": "515e293086c42dee344c3771f1505bf14ab2eadd0ae2c7f3fd284bf612bc8c8e",
    "larry-graveyard2": "017dd73f0a181f0a0c22875c474021292a53626f5d401875b03a17ed1c99c024",
    "gothicvania": "992f3792b49391da4d97c29da47c8f3d2f438ca10d7db03f9a3166d655600871",
    "onebit": "b880c311d63dc3ecb6e506137efd347c655df9e26b4a5d0e0ebdaeaa016393ef",
}


def fetch(work, name):
    url = SOURCES[name][0]
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, os.path.basename(url))
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
        k = min(1.0, (840 if len(images) <= 2 else 300) / max(im.width, 1), (480 if len(images) <= 2 else 200) / max(im.height, 1))
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


def onebit(work, gw, gh, seed=7):
    """The 1-bit pack's buildings and statues in a row on a night ground,
    under a pixel moon and stars."""
    rng = random.Random(seed)
    c = Image.new("L", (gw, gh), 0)
    px = c.load()
    ground = int(gh * 0.80)
    for _ in range(gw // 3):
        x, y = rng.randrange(gw), rng.randrange(int(gh * 0.6))
        px[x, y] = rng.choice((120, 180, 255))
    r = gh // 9
    mx, my = int(gw * 0.80), int(gh * 0.22)
    for y in range(my - r, my + r + 1):
        for x in range(mx - r, mx + r + 1):
            if (x - mx) ** 2 + (y - my) ** 2 <= r * r:
                px[x, y] = 235
    for x in range(gw):
        px[x, ground] = 255
    names = ["GRAVE-Mausoleum_A.png", "NOTABLE-Angel Statue.png", "GRAVE-Mausoleum_C.png", "NOTABLE-Main Gate_A_Closed.png",
             "NOTABLE-Skull Statue.png", "GRAVE-Mausoleum_E.png", "NOTABLE-Pavilion_A.png", "GRAVE-Mausoleum_B.png"]
    n = max(3, min(len(names), gw // 72))
    order = names[:n]
    sp = gw // n
    for i, name in enumerate(order):
        im = grey(member(work, "onebit", "/" + name))
        x = i * sp + (sp - im.width) // 2
        c.paste(im.convert("L"), (x, ground - im.height + 1), im.getchannel("A"))
    # in front, a row of the pack's small headstones and crosses
    small = [(m, im) for m, im in pictures(fetch(work, "onebit"))
             if "/Environment/" in m and im.width <= 32 and im.height <= 32
             and "Gravestone" in m.rsplit("/", 1)[-1]]
    if gw == 480:
        print("   small headstones:", ", ".join(m.rsplit("/", 1)[-1] for m, _ in small) or "none")
    if small:
        y = ground + int(gh * 0.12)
        x = 6
        while x < gw - 20:
            m, im = small[rng.randrange(len(small))]
            g = grey(im)
            c.paste(g.convert("L"), (x, y - im.height), g.getchannel("A"))
            x += im.width + rng.randint(6, 14)
    return c


WALLPAPERS = {
    # name: (scene, title, description, sources)
    "Gothic-Cemetery": (gothic, "Gothic cemetery", "A moonlit graveyard in pixel art", ["gothicvania"]),
    "One-Bit-Cemetery": (onebit, "One-bit cemetery", "Mausoleums, a gate and statues under the moon, in 1-bit pixel art", ["onebit"]),
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
        inspect(sys.argv[2])
    else:
        build(sys.argv[2], sys.argv[3])
