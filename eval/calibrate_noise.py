"""Calibration set for the noise-consistency check (janus/evidence.py), kept apart from the 13 eval cases.

60 fresh synthetic after photos (random room, job, camera shift, lighting) and, for each, a paste of a
random-sized area from another photo, once blurred and once sharp. Prints the largest smooth area found on
each kind, as a share of the frame. Run: python -m eval.calibrate_noise
"""
from __future__ import annotations

import io
import random

from PIL import Image, ImageFilter

from eval.scenes import ROOMS, H, W, draw, jpeg
from janus.evidence import SMOOTH_REGION_MAX, noise_consistency


def smooth(raw: bytes) -> float:
    return noise_consistency(Image.open(io.BytesIO(raw)).convert("RGB"))["smooth_region"]


def main(n: int = 60, seed: int = 12345) -> None:
    rnd = random.Random(seed)
    rooms, jobs = list(ROOMS), ["hole", "shelf", "outlet"]
    good, blurred, sharp = [], [], []
    for i in range(n):
        room, job, s = rnd.choice(rooms), rnd.choice(jobs), 10_000 + i
        after = draw(ROOMS[room], job, True, seed=s, shift=rnd.randint(-20, 20), light=rnd.uniform(0.85, 1.15), layout=s)
        good.append(smooth(jpeg(after)))
        base = draw(ROOMS[room], job, False, seed=s + 1, layout=s)
        donor = draw(ROOMS[rnd.choice(rooms)], job, True, seed=s + 2, layout=s + 7)
        bw, bh = rnd.randint(150, 450), rnd.randint(100, 300)
        x, y = rnd.randint(0, W - bw), rnd.randint(0, H - bh)
        box = (x, y, x + bw, y + bh)
        for blur, out in ((rnd.choice([0.8, 1.0, 1.5]), blurred), (0, sharp)):
            patch = donor.crop(box)
            if blur:
                patch = patch.filter(ImageFilter.GaussianBlur(blur))
            img = base.copy()
            img.paste(patch, box[:2])
            out.append(smooth(jpeg(img)))
    print(f"threshold {SMOOTH_REGION_MAX:.3f}")
    print(f"genuine:        max {max(good):.4f}, flagged {sum(v >= SMOOTH_REGION_MAX for v in good)}/{n}")
    print(f"blurred paste:  min {min(blurred):.4f}, flagged {sum(v >= SMOOTH_REGION_MAX for v in blurred)}/{n}")
    print(f"sharp paste:    max {max(sharp):.4f}, flagged {sum(v >= SMOOTH_REGION_MAX for v in sharp)}/{n}")


if __name__ == "__main__":
    main()
