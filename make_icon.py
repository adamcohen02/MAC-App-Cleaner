#!/usr/bin/env python3
"""
Draws the MacBook Optimizer app icon (a little broom) and writes:

    assets/broom_1024.png   full-size master
    assets/broom.png        512 px copy (used for the Dock icon and README)
    assets/AppIcon.icns     macOS app icon

Usage:  python3 make_icon.py        (needs: pip install pillow)

On a Mac, the .icns is built with Apple's `iconutil` for best quality; elsewhere
Pillow's built-in ICNS writer is used.
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
S = 2048          # draw at 2x, then downsample for smooth edges
U = S / 1024.0    # 1024-space -> drawing-space


def u(v):
    return v * U


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def squircle_mask(size, inset, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (u(inset), u(inset), size - u(inset), size - u(inset)), radius=u(radius), fill=255)
    return m


def background():
    top, bottom = (150, 120, 255), (86, 52, 214)
    grad = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(grad)
    for y in range(S):
        d.line([(0, y), (S, y)], fill=lerp(top, bottom, y / S))
    mask = squircle_mask(S, 100, 185)
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bg.paste(grad, (0, 0), mask)
    # soft top highlight
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hl).ellipse((u(140), u(-380), u(884), u(380)), fill=(255, 255, 255, 38))
    hl.putalpha(ImageChops.multiply(hl.getchannel("A"), mask))
    return Image.alpha_composite(bg, hl), mask


def drop_shadow(mask):
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    solid = Image.new("RGBA", (S, S), (20, 10, 60, 120))
    sh.paste(solid, (0, int(u(14))), mask)
    return sh.filter(ImageFilter.GaussianBlur(u(14)))


def broom_layer():
    """Broom drawn upright (handle up, bristles down), centred on x=512."""
    k = 0.80
    cx = 512

    def P(x, y):  # 1024-space (relative to broom centre line) -> drawing space
        return (u(cx + x * k), u(y * k + 60))

    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # handle
    d.rounded_rectangle((*P(-24, 120), *P(24, 640)), radius=u(22 * k), fill=(222, 164, 98, 255))
    d.rounded_rectangle((*P(-24, 120), *P(-6, 640)), radius=u(10 * k), fill=(240, 196, 142, 255))
    d.ellipse((*P(-9, 150), *P(9, 168)), fill=(120, 78, 40, 255))  # hang hole

    # bristle head (drawn before the band so the band overlaps its top)
    head = [P(-118, 640), P(118, 640), P(196, 905), P(-196, 905)]
    d.polygon(head, fill=(244, 196, 78, 255))
    # shading on the right side of the head
    d.polygon([P(40, 640), P(118, 640), P(196, 905), P(70, 905)], fill=(226, 172, 52, 255))
    # bristle lines
    for i in range(-5, 6):
        x_top = i * 20
        x_bot = i * 34
        d.line([P(x_top, 665), P(x_bot, 900)], fill=(196, 140, 34, 255), width=int(u(5 * k)))
    # ragged tips
    tips = []
    n = 12
    for i in range(n + 1):
        x = -196 + (392 * i / n)
        tips.append(P(x, 905 + (22 if i % 2 else 0)))
    d.polygon([P(-196, 905), *tips, P(196, 905)], fill=(244, 196, 78, 255))
    for i in range(1, n, 2):
        x = -196 + (392 * i / n)
        d.line([P(x, 905), P(x, 927)], fill=(196, 140, 34, 255), width=int(u(4 * k)))

    # binding band
    d.polygon([P(-84, 600), P(84, 600), P(124, 676), P(-124, 676)], fill=(58, 58, 92, 255))
    d.polygon([P(-84, 600), P(-52, 600), P(-86, 676), P(-124, 676)], fill=(96, 96, 140, 255))
    d.line([P(-102, 640), P(102, 640)], fill=(32, 32, 60, 255), width=int(u(4 * k)))

    return layer.rotate(-35, resample=Image.BICUBIC, center=(S / 2, S / 2))


def star(draw, cx, cy, r, alpha=255):
    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rad = r if i % 2 == 0 else r * 0.26
        pts.append((u(cx) + math.cos(ang) * u(rad), u(cy) + math.sin(ang) * u(rad)))
    draw.polygon(pts, fill=(255, 255, 255, alpha))


def build():
    bg, mask = background()
    img = Image.alpha_composite(Image.new("RGBA", (S, S), (0, 0, 0, 0)), drop_shadow(mask))
    img = Image.alpha_composite(img, bg)

    broom = broom_layer()
    # slight shadow under the broom
    bs = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bs.paste((30, 15, 90, 110), (int(u(10)), int(u(14))), broom.getchannel("A"))
    img = Image.alpha_composite(img, bs.filter(ImageFilter.GaussianBlur(u(10))))
    img = Image.alpha_composite(img, broom)

    fx = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fx)
    star(fd, 250, 262, 62)
    star(fd, 342, 176, 30, 220)
    star(fd, 168, 392, 26, 200)
    star(fd, 808, 760, 34, 230)
    for (x, y, r, a) in ((236, 704, 15, 210), (196, 748, 10, 160), (278, 758, 8, 130)):
        fd.ellipse((u(x - r), u(y - r), u(x + r), u(y + r)), fill=(255, 255, 255, a))
    img = Image.alpha_composite(img, fx)
    return img.resize((1024, 1024), Image.LANCZOS)


def write_icns(master, path):
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = os.path.join(tmp, "AppIcon.iconset")
            os.makedirs(iconset)
            for base in (16, 32, 128, 256, 512):
                master.resize((base, base), Image.LANCZOS).save(os.path.join(iconset, f"icon_{base}x{base}.png"))
                master.resize((base * 2, base * 2), Image.LANCZOS).save(
                    os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
            subprocess.run(["iconutil", "-c", "icns", iconset, "-o", path], check=True)
        return "iconutil"
    master.save(path, format="ICNS", sizes=[(s, s) for s in sizes])
    return "Pillow"


def main():
    os.makedirs(ASSETS, exist_ok=True)
    master = build()
    master.save(os.path.join(ASSETS, "broom_1024.png"))
    master.resize((512, 512), Image.LANCZOS).save(os.path.join(ASSETS, "broom.png"))
    how = write_icns(master, os.path.join(ASSETS, "AppIcon.icns"))
    print(f"Wrote assets/broom_1024.png, assets/broom.png, assets/AppIcon.icns (via {how})")


if __name__ == "__main__":
    main()
