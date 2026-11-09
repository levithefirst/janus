"""Evidence integrity: is this photo usable, original to this job, and taken when it should have been?

Runs on the ORIGINAL upload bytes, before Janus re-encodes the photo (which strips metadata).

- Quality gate: resolution, sharpness (edge variance) and brightness. A failing photo goes straight
  to HOLD with a retake instruction; the AI is not asked.
- Reuse: a 64-bit perceptual hash (dHash) of every evidence photo goes into a cross-job index. A
  near-duplicate of a photo from another job, or of this job's own listing photo, forces a HOLD.
- EXIF: capture time and GPS. Missing EXIF raises the confidence bar a little. EXIF that is present
  and plausible changes nothing: it can never lower the bar or pass anything on its own. A capture time
  before the authorization, or in the future, forces a HOLD. GPS far from a declared delivery location,
  or inconsistent across a job's photos, raises the bar.
- Notes: control characters stripped, angle brackets neutralised so a note cannot close the <note> tag
  it is wrapped in, length capped; instruction-like notes force a HOLD for a human to look at.

The result is a `Trust`: extra confidence needed on top of the base threshold, plus reasons to hold.
"""
from __future__ import annotations

import io
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .clock import iso, parse, utcnow

MIN_SHORT_SIDE = 480
MIN_PIXELS = 300_000
MIN_SHARPNESS = 40.0      # variance of the edge image on a 512 px copy
MIN_BRIGHTNESS = 35.0
REUSE_MAX_DISTANCE = 6    # Hamming distance out of 64 bits
EXIF_SKEW_WITH_OFFSET = timedelta(minutes=10)
EXIF_SKEW_NO_OFFSET = timedelta(hours=14)   # EXIF times usually carry no zone: allow any zone
GPS_MAX_KM = 50.0
PENALTY_NO_EXIF = 0.05
PENALTY_EXIF_TIME_AMBIGUOUS = 0.02
PENALTY_GPS = 0.10
MAX_BAR = 0.99

INJECTION = re.compile("|".join([
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|rules|prompts?)",
    r"system\s*prompt", r"you\s+are\s+now\b", r"\bas\s+an\s+ai\b", r"respond\s+with\b",
    r"\b(?:verdict|confidence|recommended_action|result)\s*[:=]",
    r"</?\s*(?:note|system|instructions?|assistant|user)\s*>",
    r"\b(?:set|mark|return)\s+(?:the\s+|it\s+|this\s+|all\s+)?(?:verdict|confidence|requirements?|items?)\b",
    r"\bmark\s+(?:it|this|all|everything)\s+as\s+(?:met|satisfied|passed)",
    r"\bapprove\s+(?:this|the)\s+(?:payment|capture|release)",
]), re.I)


# ---------------- notes ----------------

def sanitize_note(note: str, limit: int = 1000) -> str:
    note = unicodedata.normalize("NFKC", note or "")
    note = "".join(ch for ch in note if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    note = note.replace("<", "‹").replace(">", "›")
    return re.sub(r"[ \t]+", " ", note).strip()[:limit]


def note_flags(note: str) -> list[str]:
    return ["instruction_like_note"] if INJECTION.search(note or "") else []


# ---------------- image analysis ----------------

def _exif(img) -> dict:
    out = {"present": False, "taken_at": None, "offset": None, "gps": None, "software": None}
    try:
        ex = img.getexif()
    except Exception:
        return out
    if not ex:
        return out
    out["present"] = True
    sw = ex.get(305)  # Software: editors write their name here
    if isinstance(sw, str) and sw.strip():
        out["software"] = sw.strip()[:80]
    sub = ex.get_ifd(0x8769) or {}
    raw = sub.get(36867) or sub.get(36868) or ex.get(306)  # DateTimeOriginal, DateTimeDigitized, DateTime
    if isinstance(raw, str):
        try:
            out["taken_at"] = datetime.strptime(raw.strip()[:19], "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    off = sub.get(36881)  # OffsetTimeOriginal, e.g. "+02:00"
    if isinstance(off, str) and re.fullmatch(r"[+-]\d{2}:\d{2}", off.strip()):
        out["offset"] = off.strip()
    gps = ex.get_ifd(0x8825) or {}
    try:
        def deg(v, ref):
            d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            return -d if ref in ("S", "W") else d
        if gps.get(2) and gps.get(4):
            out["gps"] = [round(deg(gps[2], gps.get(1, "N")), 3), round(deg(gps[4], gps.get(3, "E")), 3)]
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        pass
    return out


def dhash(img) -> str:
    g = img.convert("L").resize((9, 8))
    px = list(g.tobytes())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def analyze(raw: bytes) -> dict:
    """Quality, perceptual hash and EXIF of the original upload. Pillow is required for evidence."""
    from PIL import Image, ImageFilter, ImageOps, ImageStat
    img = Image.open(io.BytesIO(raw))
    exif = _exif(img)
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    small = img.copy()
    small.thumbnail((512, 512))
    gray = small.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    # Pillow leaves the 1px border unfiltered (raw pixel values), which swamps the variance: crop it off.
    edges = edges.crop((2, 2, edges.width - 2, edges.height - 2))
    sharp = ImageStat.Stat(edges).var[0]
    bright = ImageStat.Stat(gray).mean[0]
    reasons = []
    if min(w, h) < MIN_SHORT_SIDE or w * h < MIN_PIXELS:
        reasons.append(f"resolution {w}x{h} is too low (need {MIN_SHORT_SIDE}px on the short side)")
    if sharp < MIN_SHARPNESS:
        reasons.append(f"too blurry (sharpness {sharp:.0f} < {MIN_SHARPNESS:.0f})")
    if bright < MIN_BRIGHTNESS:
        reasons.append(f"too dark (brightness {bright:.0f} < {MIN_BRIGHTNESS:.0f})")
    return {"width": w, "height": h, "sharpness": round(sharp, 1), "brightness": round(bright, 1),
            "quality_ok": not reasons, "quality_reasons": reasons, "phash": dhash(img), "exif": exif}


def phash_of_file(path: Path) -> Optional[str]:
    try:
        from PIL import Image
        return dhash(Image.open(path).convert("RGB"))
    except Exception:
        return None


# ---------------- cross-job reuse index ----------------

class PhashIndex:
    STREAM = "phash_index"

    def __init__(self, store):
        from .kv import Backend, FileBackend
        self.kv = store if isinstance(store, Backend) else FileBackend(Path(store))

    def entries(self) -> list[dict]:
        return self.kv.stream(self.STREAM)

    def add_and_match(self, threshold_id: str, evidence_id: str, phash: str) -> list[dict]:
        """Record this hash and return near-duplicates from OTHER jobs, atomically."""
        with self.kv.lock(f"index:{self.STREAM}"):
            hits = [e for e in self.entries()
                    if e["threshold_id"] != threshold_id and hamming(e["phash"], phash) <= REUSE_MAX_DISTANCE]
            self.kv.append(self.STREAM, lambda last: {"threshold_id": threshold_id, "evidence_id": evidence_id,
                                                      "phash": phash, "at": iso()})
            return hits


# ---------------- trust ----------------

@dataclass
class Trust:
    penalty: float = 0.0
    flags: list[str] = field(default_factory=list)
    hold_reasons: list[str] = field(default_factory=list)

    def bar(self, base: float) -> float:
        return round(min(MAX_BAR, base + self.penalty), 4)


def km(a: list[float], b: list[float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def exif_time_utc(exif: dict) -> tuple[Optional[datetime], bool]:
    """(time, zone_known). Without an offset the naive time is returned as if UTC, zone_known=False."""
    if not exif.get("taken_at"):
        return None, False
    naive = datetime.fromisoformat(exif["taken_at"])
    if exif.get("offset"):
        sign = 1 if exif["offset"][0] == "+" else -1
        hh, mm = map(int, exif["offset"][1:].split(":"))
        return (naive - sign * timedelta(hours=hh, minutes=mm)).replace(tzinfo=timezone.utc), True
    return naive.replace(tzinfo=timezone.utc), False


def assess_item(item, authorized_at: Optional[str], delivery_location: Optional[list[float]]) -> tuple[float, list[str], list[str]]:
    """Penalty, flags and hold reasons for one evidence item (fields set by submit_evidence)."""
    penalty, flags, holds = 0.0, list(item.flags), []
    for f in item.flags:
        if f.startswith("reused_photo") or f == "matches_listing_photo":
            holds.append(f"photo {item.id} is a near-duplicate of another photo ({f})")
        if f == "instruction_like_note":
            holds.append(f"note on {item.id} contains instruction-like text; a person should look at it")
    exif = item.exif or {}
    if item.file:
        if not exif.get("present"):
            penalty += PENALTY_NO_EXIF
            flags.append("no_exif")
        else:
            taken, zone_known = exif_time_utc(exif)
            auth = parse(authorized_at)
            skew = EXIF_SKEW_WITH_OFFSET if zone_known else EXIF_SKEW_NO_OFFSET
            if taken is None:
                penalty += PENALTY_NO_EXIF
                flags.append("no_exif_time")
            else:
                if auth and taken < auth - skew:
                    flags.append("taken_before_authorization")
                    holds.append(f"photo {item.id} was taken before the payment was authorized")
                elif auth and not zone_known and taken < auth + skew:
                    penalty += PENALTY_EXIF_TIME_AMBIGUOUS
                    flags.append("exif_time_ambiguous")
                if taken > utcnow() + skew:
                    flags.append("taken_in_future")
                    holds.append(f"photo {item.id} has a capture time in the future")
            gps = exif.get("gps")
            if gps and delivery_location and km(gps, delivery_location) > GPS_MAX_KM:
                penalty += PENALTY_GPS
                flags.append("gps_far_from_delivery")
    return penalty, flags, holds


def assess(evidence: list, authorized_at: Optional[str], delivery_location: Optional[list[float]] = None) -> Trust:
    t = Trust()
    for item in evidence:
        p, f, h = assess_item(item, authorized_at, delivery_location)
        t.penalty = max(t.penalty, p)  # the weakest photo sets the bar; penalties do not stack per photo
        t.flags += [x for x in f if x not in t.flags]
        t.hold_reasons += h
    gps = [e.exif["gps"] for e in evidence if e.exif and e.exif.get("gps")]
    if len(gps) > 1 and max(km(a, b) for a in gps for b in gps) > GPS_MAX_KM:
        t.penalty += PENALTY_GPS
        t.flags.append("gps_inconsistent")
    return t


# ---------------- job mode: before / after ----------------

# Software tags that mean the file passed through an editor. Phone camera firmware also writes this tag
# (e.g. "iOS 17.4", "G9910XXU2BWK1"), so only names of editing tools count.
EDITORS = re.compile(r"photoshop|lightroom|gimp|affinity|pixelmator|snapseed|picsart|facetune|canva|"
                     r"luminar|photopea|fotor|polarr|vsco|airbrush|retouch|remini|faceapp|meitu|pixlr", re.I)


@dataclass
class JobCheck:
    blocking: list[str] = field(default_factory=list)   # capture needs a logged customer override
    info: list[str] = field(default_factory=list)       # shown, and raise the confidence bar
    penalty: float = 0.0


def job_flags(evidence: list, authorized_at: Optional[str]) -> JobCheck:
    """Integrity flags for a job's before and after photos. Deterministic: no AI involved."""
    jc = JobCheck()
    before = [e for e in evidence if e.phase == "before" and e.file]
    after = [e for e in evidence if e.phase == "after" and e.file]

    def block(flag: str) -> None:
        if flag not in jc.blocking:
            jc.blocking.append(flag)
    if not before:
        block("no_before_photo")
    if not after:
        block("no_after_photo")
    for e in before + after:
        for f in e.flags:
            if f.startswith("reused_photo"):
                block(f"reused_photo:{e.id}")
            elif f == "instruction_like_note":
                block(f"instruction_like_note:{e.id}")
        sw = (e.exif or {}).get("software")
        if sw and EDITORS.search(sw):
            block(f"edited_software:{e.id}")
    for a in after:
        for b in before:
            if a.phash and b.phash and hamming(a.phash, b.phash) <= REUSE_MAX_DISTANCE:
                block(f"after_matches_before:{a.id}")
    auth = parse(authorized_at)
    times = {}
    for e in before + after:
        taken, zone_known = exif_time_utc(e.exif or {})
        if taken is not None:
            times[e.id] = (taken, EXIF_SKEW_WITH_OFFSET if zone_known else EXIF_SKEW_NO_OFFSET)
            if taken > utcnow() + times[e.id][1]:
                block(f"taken_in_future:{e.id}")
    for a in after:
        if a.id in times and auth and times[a.id][0] < auth - times[a.id][1]:
            block(f"after_taken_before_authorization:{a.id}")
    b_times = [times[b.id] for b in before if b.id in times]
    for a in after:
        if a.id in times and b_times:
            latest_before = max(t for t, _ in b_times)
            if times[a.id][0] < latest_before - max(times[a.id][1], max(sk for _, sk in b_times)):
                block(f"after_older_than_before:{a.id}")
    for a in after:
        if not (a.exif or {}).get("present"):
            jc.info.append(f"no_exif:{a.id}")
            jc.penalty = max(jc.penalty, PENALTY_NO_EXIF)
    return jc
