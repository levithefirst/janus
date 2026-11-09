"""Builds the before/after bytes for each case in labels.json, at run time (EXIF times are relative to now)."""
from __future__ import annotations

from datetime import timedelta

from eval.scenes import ROOMS, draw, jpeg, paste_from
from janus import clock


def build(case_id: str, outputs: dict[str, bytes]) -> tuple[str, bytes, bytes]:
    """(job kind, before bytes, after bytes). `outputs` holds after photos of earlier cases, for reuse cases."""
    now = clock.utcnow()
    t_before, t_after = now - timedelta(hours=2), now

    def pair(room_b, room_a, job, shift=12, light=1.05, seed=0, **after_kw):
        b = draw(ROOMS[room_b], job, False, seed=seed + 1, layout=seed)
        lay_a = seed if room_a == room_b else seed + 500   # another room in another place: another layout
        a = draw(ROOMS[room_a], job, True, seed=seed + 2, shift=shift, light=light, layout=lay_a)
        return b, a, after_kw

    if case_id == "good-hole":
        b, a, _ = pair("sage", "sage", "hole", seed=10)
        return "hole", jpeg(b, taken=t_before), jpeg(a, taken=t_after)
    if case_id == "good-shelf":
        b, a, _ = pair("blue", "blue", "shelf", shift=-10, light=0.95, seed=20)
        return "shelf", jpeg(b, taken=t_before), jpeg(a, taken=t_after)
    if case_id == "good-outlet":
        b, a, _ = pair("terracotta", "terracotta", "outlet", shift=6, light=1.0, seed=30)
        return "outlet", jpeg(b, taken=t_before), jpeg(a, taken=t_after)
    if case_id == "good-no-exif":
        b, a, _ = pair("yellow", "yellow", "hole", light=1.1, seed=40)
        return "hole", jpeg(b, taken=t_before), jpeg(a)
    if case_id == "reused-cross-job":
        b = draw(ROOMS["sage"], "hole", False, seed=51, layout=50)
        from PIL import Image
        import io
        src = Image.open(io.BytesIO(outputs["good-hole"]))
        return "hole", jpeg(b, taken=t_before), jpeg(src, taken=t_after, scale=0.9)
    if case_id == "reused-before":
        b = draw(ROOMS["blue"], "shelf", False, seed=61, layout=60)
        return "shelf", jpeg(b, taken=t_before), jpeg(b, taken=t_after)
    if case_id == "reused-cropped":
        b = draw(ROOMS["terracotta"], "outlet", False, seed=71, layout=70)
        from PIL import Image
        import io
        src = Image.open(io.BytesIO(outputs["good-outlet"]))
        return "outlet", jpeg(b, taken=t_before), jpeg(src, taken=t_after, scale=0.85)
    if case_id == "wrong-room-1":
        b, a, _ = pair("sage", "blue", "hole", seed=80)
        return "hole", jpeg(b, taken=t_before), jpeg(a, taken=t_after)
    if case_id == "wrong-room-2":
        b, a, _ = pair("yellow", "terracotta", "shelf", seed=90)
        return "shelf", jpeg(b, taken=t_before), jpeg(a, taken=t_after)
    if case_id == "wrong-room-3":
        b, a, _ = pair("blue", "sage", "outlet", seed=100)
        return "outlet", jpeg(b, taken=t_before), jpeg(a, taken=t_after)
    if case_id == "edited-photoshop":
        b, a, _ = pair("sage", "sage", "shelf", seed=110)
        return "shelf", jpeg(b, taken=t_before), jpeg(a, taken=t_after, software="Adobe Photoshop 25.1 (Windows)")
    if case_id == "edited-snapseed":
        b, a, _ = pair("terracotta", "terracotta", "hole", seed=120)
        return "hole", jpeg(b, taken=t_before), jpeg(a, taken=t_after, software="Snapseed 2.0")
    if case_id == "edited-pasted":
        b = draw(ROOMS["yellow"], "hole", False, seed=131, layout=130)
        donor = draw(ROOMS["blue"], "hole", True, seed=132, layout=630)
        return "hole", jpeg(b, taken=t_before), jpeg(paste_from(b, donor), taken=t_after)
    raise KeyError(case_id)


DESCRIPTIONS = {
    "hole": ("Patch the wall hole", "Patch the hole in the wall. Paint the patch to match the wall."),
    "shelf": ("Mount a shelf", "Mount the shelf on the wall. Put the books on the shelf."),
    "outlet": ("Replace a broken outlet", "Replace the broken outlet cover. Fit a new double socket."),
}
