"""Synthetic before/after photos of service jobs, for tests and the eval set.

Drawings, not photographs: walls, floor, a window and a work area. Real phone photos are the only way to
measure real-world error rates; these exist so the pipeline can be exercised and regressions caught.
"""
from __future__ import annotations

import io
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

W, H = 960, 720


@dataclass(frozen=True)
class Room:
    wall: tuple[int, int, int]
    floor: tuple[int, int, int]
    window_left: bool
    trim: tuple[int, int, int] = (238, 236, 230)


ROOMS = {
    "sage": Room(wall=(150, 172, 140), floor=(120, 86, 58), window_left=True),
    "blue": Room(wall=(110, 140, 185), floor=(170, 160, 150), window_left=False),
    "terracotta": Room(wall=(196, 120, 90), floor=(70, 66, 64), window_left=True),
    "yellow": Room(wall=(225, 200, 110), floor=(140, 100, 70), window_left=False),
}


def draw(room: Room, job: str, done: bool, seed: int = 0, shift: int = 0, light: float = 1.0,
         layout: Optional[int] = None) -> Image.Image:
    """`layout` fixes where the window, door and picture are (a given room in a given house); `seed` only
    varies sensor noise. Before and after of one job share a layout; different jobs do not."""
    rnd = random.Random(seed)
    lay = random.Random(layout if layout is not None else seed)
    img = Image.new("RGB", (W, H), room.wall)
    d = ImageDraw.Draw(img)
    horizon = int(H * 0.68)
    d.rectangle([0, horizon, W, H], fill=room.floor)
    for x in range(0, W, 80):  # floor boards
        d.line([(x + shift, horizon), (x + shift - 40, H)], fill=tuple(max(0, c - 18) for c in room.floor), width=3)
    d.rectangle([0, horizon - 14, W, horizon], fill=room.trim)
    ww, wh = lay.randint(170, 260), lay.randint(170, 250)
    wx = lay.randint(30, 110) if room.window_left else W - ww - lay.randint(30, 110)
    wy = lay.randint(60, 130)
    d.rectangle([wx + shift, wy, wx + ww + shift, wy + wh], fill=(200, 225, 245), outline=room.trim, width=12)
    d.line([(wx + ww // 2 + shift, wy), (wx + ww // 2 + shift, wy + wh)], fill=room.trim, width=8)
    dx = (W - lay.randint(150, 260)) if room.window_left else lay.randint(40, 150)
    d.rectangle([dx + shift, horizon - 330, dx + 120 + shift, horizon - 14],
                fill=tuple(max(0, c - 40) for c in room.wall), outline=room.trim, width=8)
    px_, py_ = lay.randint(330, 560), lay.randint(70, 120)
    d.rectangle([px_ + shift, py_, px_ + 90 + shift, py_ + 64], outline=(60, 50, 40), width=6,
                fill=(lay.randint(40, 220), lay.randint(40, 220), lay.randint(40, 220)))
    cx, cy = W // 2 + shift, int(H * 0.42)
    if job == "hole":
        if not done:
            d.polygon([(cx - 70, cy - 40), (cx + 50, cy - 60), (cx + 80, cy + 30), (cx - 20, cy + 70), (cx - 80, cy + 20)],
                      fill=(45, 38, 32))
            for _ in range(5):
                a = (cx + rnd.randint(-80, 80), cy + rnd.randint(-60, 60))
                d.line([a, (a[0] + rnd.randint(-90, 90), a[1] + rnd.randint(-70, 70))], fill=(70, 60, 50), width=3)
        else:
            d.ellipse([cx - 95, cy - 80, cx + 95, cy + 85], fill=tuple(min(255, c + 8) for c in room.wall))
    elif job == "shelf":
        if done:
            d.rectangle([cx - 200, cy, cx + 200, cy + 22], fill=(110, 75, 45))
            for i, col in enumerate([(180, 40, 40), (40, 90, 160), (230, 210, 60), (60, 140, 80)]):
                d.rectangle([cx - 170 + i * 85, cy - 90 + i * 8, cx - 110 + i * 85, cy], fill=col)
        else:
            d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=(90, 80, 70))
    elif job == "outlet":
        d.rectangle([cx - 55, cy - 80, cx + 55, cy + 80], fill=(245, 245, 240) if done else (200, 190, 170),
                    outline=(120, 110, 100), width=4)
        if done:
            for y in (cy - 35, cy + 35):
                d.rectangle([cx - 14, y - 20, cx + 14, y + 20], fill=(60, 60, 60))
        else:
            d.polygon([(cx - 55, cy - 80), (cx + 10, cy - 20), (cx - 30, cy + 80)], fill=(30, 25, 20))
            d.line([(cx + 20, cy + 70), (cx + 70, cy + 140)], fill=(200, 40, 30), width=6)
    px = img.load()
    for _ in range(W * H // 8):  # sensor noise
        i, j = rnd.randrange(W), rnd.randrange(H)
        r, g, b = px[i, j]
        n = rnd.randint(-12, 12)
        px[i, j] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)))
    if light != 1.0:
        img = ImageEnhance.Brightness(img).enhance(light)
    return img


def paste_from(base: Image.Image, donor: Image.Image) -> Image.Image:
    """A crude pixel edit: the work area cut from another photo and pasted in, edges softened."""
    out = base.copy()
    box = (W // 2 - 220, int(H * 0.42) - 130, W // 2 + 220, int(H * 0.42) + 130)
    patch = donor.crop(box).filter(ImageFilter.GaussianBlur(1))
    out.paste(patch, box[:2])
    return out


def jpeg(img: Image.Image, taken: Optional[datetime] = None, software: Optional[str] = None,
         offset: str = "+00:00", scale: float = 1.0) -> bytes:
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    ex = Image.Exif()
    if taken is not None:
        ex[0x8769] = {36867: taken.strftime("%Y:%m:%d %H:%M:%S"), 36881: offset}
        ex[271], ex[272] = "Google", "Pixel 8"
    if software:
        ex[305] = software
    out = io.BytesIO()
    if len(ex):
        img.save(out, "JPEG", quality=90, exif=ex)
    else:
        img.save(out, "JPEG", quality=90)
    return out.getvalue()
