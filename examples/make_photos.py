"""Regenerate the synthetic demo photos: python examples/make_photos.py

They are drawings, good enough to run the loop offline and to show Claude a clear match vs a
clear mismatch. For the recorded demo, use real phone photos of a real item instead.
"""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).parent / "photos"
W, H = 900, 700


def star(d, cx, cy, r, fill):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=fill)


def scene(bg_top, bg_bottom, table, mug, x=0, y=0, scale=1.0, logo=True, chip=False, seed=1):
    rnd = random.Random(seed)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for row in range(H):  # wall gradient, then table
        t = row / H
        c = tuple(int(bg_top[i] * (1 - t) + bg_bottom[i] * t) for i in range(3))
        d.line([(0, row), (W, row)], fill=table if row > H * 0.62 else c)
    cx, cy = W // 2 + x, int(H * 0.52) + y
    bw, bh = int(250 * scale), int(300 * scale)
    d.ellipse([cx - bw // 2 - 30, cy + bh // 2 - 25, cx + bw // 2 + 60, cy + bh // 2 + 35], fill=(60, 50, 45))
    shade = tuple(max(0, v - 45) for v in mug)
    d.ellipse([cx + bw // 2 - 40, cy - bh // 4, cx + bw // 2 + 90, cy + bh // 4], outline=shade, width=int(28 * scale))
    d.rectangle([cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2], fill=mug)
    d.rectangle([cx + bw // 4, cy - bh // 2, cx + bw // 2, cy + bh // 2], fill=shade)
    d.ellipse([cx - bw // 2, cy - bh // 2 - 30, cx + bw // 2, cy - bh // 2 + 30], fill=shade)
    d.ellipse([cx - bw // 2 + 14, cy - bh // 2 - 20, cx + bw // 2 - 14, cy - bh // 2 + 20], fill=(40, 25, 20))
    d.ellipse([cx - bw // 2, cy + bh // 2 - 30, cx + bw // 2, cy + bh // 2 + 30], fill=mug)
    if logo:
        star(d, cx - int(25 * scale), cy + int(10 * scale), int(70 * scale), (250, 250, 245))
    if chip:
        d.polygon([(cx - bw // 2 + 40, cy - bh // 2 - 8), (cx - bw // 2 + 95, cy - bh // 2 - 4),
                   (cx - bw // 2 + 70, cy - bh // 2 + 40)], fill=(235, 230, 220))
        d.line([(cx - bw // 2 + 70, cy - bh // 2 + 40), (cx - bw // 2 + 50, cy + 30)], fill=(30, 30, 30), width=4)
    px = img.load()
    for _ in range(W * H // 6):  # sensor noise so it isn't flat vector art
        i, j = rnd.randrange(W), rnd.randrange(H)
        r, g, b = px[i, j]
        n = rnd.randint(-14, 14)
        px[i, j] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)))
    return img


def main():
    OUT.mkdir(exist_ok=True)
    red = (196, 30, 40)
    scene((236, 236, 240), (210, 210, 216), (205, 200, 195), red).save(OUT / "listing_red_mug.jpg", quality=90)
    scene((226, 222, 214), (204, 198, 188), (168, 158, 148), red, x=-60, y=10, scale=0.9, seed=2) \
        .save(OUT / "received_match.jpg", quality=88)
    scene((226, 222, 214), (204, 198, 188), (168, 158, 148), (35, 80, 190), x=40, scale=0.95, logo=False,
          chip=True, seed=3).save(OUT / "received_mismatch.jpg", quality=88)
    dark = scene((226, 222, 214), (204, 198, 188), (168, 158, 148), red, x=-200, y=120, scale=0.8, seed=4)
    dark = dark.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * 0.17))
    dark.save(OUT / "received_unclear.jpg", quality=80)
    print("wrote", *sorted(p.name for p in OUT.glob("*.jpg")))


if __name__ == "__main__":
    main()
