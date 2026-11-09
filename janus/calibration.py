"""A small calibration harness for the verifier's confidence. Plainly: 12 synthetic cases is small.

It answers three questions on a labelled set:
1. Does confidence track correctness? (accuracy per confidence bucket, Brier score, expected
   calibration error over 3 bins)
2. Would the policy have moved money wrongly at the configured bar? (unsafe captures and voids:
   the number that matters, and the one this harness is for)
3. How often does it hold on cases a person would call clear? (cost of caution)

Each case is scored twice: on the bare model, and after the evidence quality gate that runs in front
of the model in the real pipeline. Both numbers are reported, so neither layer hides the other. Run: python -m janus calibrate [--labels FILE].
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Optional

from .constants import DEFAULT_MIN_CONFIDENCE
from .models import EvidenceItem, Listing, Verdict
from .policy import decide

BUCKETS = [(0.0, 0.5), (0.5, DEFAULT_MIN_CONFIDENCE), (DEFAULT_MIN_CONFIDENCE, 1.0001)]


def transform(raw: bytes, spec: Optional[str]) -> bytes:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    for step in filter(None, (spec or "").split(",")):
        name, _, arg = step.partition(":")
        if name == "rotate":
            img = img.rotate(float(arg), expand=True, fillcolor=(200, 196, 190))
        elif name == "crop":
            f = float(arg)
            w, h = img.size
            dx, dy = int(w * (1 - f) / 2), int(h * (1 - f) / 2)
            img = img.crop((dx, dy, w - dx, h - dy))
        elif name == "brightness":
            img = ImageEnhance.Brightness(img).enhance(float(arg))
        elif name == "mirror":
            img = ImageOps.mirror(img)
        elif name == "grey":
            img = ImageOps.grayscale(img).convert("RGB")
        elif name == "blur":
            img = img.filter(ImageFilter.GaussianBlur(float(arg)))
        elif name == "hue":
            hsv = img.convert("HSV")
            h, s, v = hsv.split()
            shift = int(float(arg) * 256 / 360)
            img = Image.merge("HSV", (h.point(lambda x: (x + shift) % 256), s, v)).convert("RGB")
        else:
            raise ValueError(f"unknown transform {name}")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def run(ai, labels_path: Path, root: Path, work: Path, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> dict:
    spec = json.loads(labels_path.read_text())
    lst = json.loads((root / spec["listing"]).read_text())
    work.mkdir(parents=True, exist_ok=True)
    listing_photo = (root / spec["listing"]).parent / lst["photo"]
    (work / "listing.jpg").write_bytes(listing_photo.read_bytes())
    listing = Listing(title=lst["title"], description=lst["description"], price=lst["price"],
                      currency=lst.get("currency", "USD"), photos=["listing.jpg"])
    compiled = ai.compile_condition(lst["condition"], listing, work)
    rows = []
    for case in spec["cases"]:
        name = f"{case['id']}.jpg"
        raw = transform((root / case["photo"]).read_bytes(), case.get("transform"))
        (work / name).write_bytes(raw)
        from .evidence import analyze
        gate = analyze(raw)
        ev = [EvidenceItem(id="V1", file=name, media_type="image/jpeg", phash=gate["phash"],
                           quality={"ok": gate["quality_ok"], "sharpness": gate["sharpness"]})]
        if gate["quality_ok"]:
            v = ai.verify(compiled, listing, ev, work)
        else:  # the real pipeline never calls the model on a gate hard-fail; neither does the harness
            v = Verdict(verdict="uncertain", confidence=0.0, checks=[], summary="quality gate: not judged",
                        next_step="Retake the photo.")
        action = decide(v, compiled, min_confidence).action
        label = case["label"]
        rows.append({"id": case["id"], "label": label, "verdict": v.verdict, "confidence": round(v.confidence, 3),
                     "correct": v.verdict == label, "action": action,
                     "unsafe": (action == "capture" and label != "met") or (action == "void" and label != "not_met"),
                     "needless_hold": action == "hold" and label in ("met", "not_met"),
                     "gate_blocked": not gate["quality_ok"]})
    return summarize(rows, min_confidence)


def summarize(rows: list[dict], min_confidence: float) -> dict:
    n = len(rows)
    brier = sum((r["confidence"] - (1.0 if r["correct"] else 0.0)) ** 2 for r in rows) / n
    buckets, ece = [], 0.0
    for lo, hi in BUCKETS:
        b = [r for r in rows if lo <= r["confidence"] < hi]
        if b:
            acc = sum(r["correct"] for r in b) / len(b)
            conf = sum(r["confidence"] for r in b) / len(b)
            ece += len(b) / n * abs(acc - conf)
            buckets.append({"range": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(b), "accuracy": round(acc, 3),
                            "mean_confidence": round(conf, 3)})
    return {"n": n, "min_confidence": min_confidence, "accuracy": round(sum(r["correct"] for r in rows) / n, 3),
            "brier": round(brier, 4), "ece": round(ece, 4), "buckets": buckets,
            "unsafe_settlements": sum(r["unsafe"] for r in rows),
            "unsafe_after_gate": sum(r["unsafe"] and not r["gate_blocked"] for r in rows),
            "gate_holds_on_clear_cases": sum(r["gate_blocked"] and r["label"] != "uncertain" for r in rows),
            "needless_holds": sum(r["needless_hold"] for r in rows), "rows": rows}


def report(result: dict, mode: str) -> str:
    out = [f"# Calibration report ({mode})", "",
           f"**{result['n']} cases. This is a small, synthetic set**: it catches regressions and gross",
           "miscalibration; it does not certify accuracy. Mock-AI numbers say nothing about Claude.", "",
           f"- Accuracy: {result['accuracy']:.0%}",
           f"- Unsafe settlements at bar {result['min_confidence']:.2f}, bare model: {result['unsafe_settlements']} "
           "(capture when not met, or void when not clearly failed)",
           f"- Unsafe settlements after the quality gate (the real pipeline): **{result['unsafe_after_gate']}**",
           f"- Clear cases the quality gate held: {result['gate_holds_on_clear_cases']}",
           f"- Needless holds on clear cases: {result['needless_holds']}",
           f"- Brier score: {result['brier']} (0 is perfect)  |  ECE (3 bins): {result['ece']}", "",
           "| Confidence | n | Accuracy | Mean confidence |", "|---|---|---|---|"]
    out += [f"| {b['range']} | {b['n']} | {b['accuracy']:.0%} | {b['mean_confidence']:.2f} |" for b in result["buckets"]]
    out += ["", "| Case | Label | Verdict | Confidence | Action | Gate | |", "|---|---|---|---|---|---|---|"]
    out += [f"| {r['id']} | {r['label']} | {r['verdict']} | {r['confidence']:.2f} | {r['action']} | "
            f"{'blocked' if r['gate_blocked'] else 'pass'} | "
            f"{'UNSAFE' if r['unsafe'] else ('hold' if r['needless_hold'] else 'ok')} |" for r in result["rows"]]
    return "\n".join(out) + "\n"
