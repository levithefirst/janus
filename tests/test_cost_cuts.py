"""Cost cuts on the verifier: short output, cached prefix, gate skip, image budget, per-item verdicts."""
import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from conftest import PHOTOS, authorized, make_config
from janus.ai import (VERIFY_SYSTEM, ClaudeAI, ItemAnswer, ItemAnswers, MockAI, pick_evidence,
                      verdict_from_items)
from janus.models import CompiledCondition, EvidenceItem, Listing, Requirement
from janus.policy import decide
from janus.service import Janus
from janus.state import State


class FakeMessages:
    def __init__(self, parsed=None, stop_reason="end_turn", raises=None):
        self.parsed, self.stop_reason, self.raises, self.calls = parsed, stop_reason, raises, []

    def parse(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return SimpleNamespace(model="claude-opus-5-5", stop_reason=self.stop_reason, stop_details=None,
                               usage=SimpleNamespace(input_tokens=11, output_tokens=22, cache_read_input_tokens=5,
                                                     cache_creation_input_tokens=0),
                               parsed_output=self.parsed)


def ai_with(msgs, **kw):
    return ClaudeAI(client=SimpleNamespace(beta=SimpleNamespace(messages=msgs)), **kw)


COND = CompiledCondition(summary="s", evidence_instructions="e", not_verifiable=[], requirements=[
    Requirement(id="R1", statement="Red mug", check="red", critical=True),
    Requirement(id="R2", statement="White star", check="star", critical=True),
    Requirement(id="R3", statement="Glossy", check="shine", critical=False)])


def answers(*rows):
    return ItemAnswers(items=[ItemAnswer(id=i, answer=a, photo=1, confidence=c, reason="r") for i, a, c in rows])


@pytest.fixture
def setup(tmp_path):
    from janus.logs import setup as s
    s(tmp_path)
    return PHOTOS.parent, Listing(title="Mug", price="24.00", photos=["photos/listing_red_mug.jpg"])


EV = [EvidenceItem(id="V1", file="photos/received_match.jpg", media_type="image/jpeg")]


# ---- 1. short output, no thinking, truncation or garbage -> uncertain -> hold ----

def test_request_is_short_uncached_thinking_off_and_prefix_cached(setup):
    root, listing = setup
    msgs = FakeMessages(parsed=answers(("R1", "yes", 0.9), ("R2", "yes", 0.9)))
    a = ai_with(msgs)
    a.verify(COND, listing, EV, root)
    a.verify(COND, listing, EV, root)
    k1, k2 = msgs.calls
    assert k1["max_tokens"] == 300 and k1["thinking"] == {"type": "adaptive"} and k1["output_config"] == {"effort": "low"}
    assert k1["system"] == [{"type": "text", "text": VERIFY_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    assert k1["system"] == k2["system"] and VERIFY_SYSTEM.startswith("[janus verify-v2]")
    assert a.usage[-1]["input_tokens"] == 11 and a.usage[-1]["output_tokens"] == 22
    assert a.usage[-1]["cache_read_input_tokens"] == 5


@pytest.mark.parametrize("msgs", [FakeMessages(stop_reason="max_tokens"),
                                  FakeMessages(parsed=None),
                                  FakeMessages(raises=ValueError("Unterminated string"))])
def test_truncated_or_unparseable_reply_is_uncertain_and_holds(setup, msgs):
    root, listing = setup
    v = ai_with(msgs).verify(COND, listing, EV, root)
    assert v.verdict == "uncertain" and v.confidence == 0.0
    assert decide(v, COND, 0.85).action == "hold"


# ---- 5. per-item answers; code computes the verdict conservatively ----

def test_all_required_yes_is_met_with_minimum_confidence():
    v = verdict_from_items(COND, answers(("R1", "yes", 0.97), ("R2", "yes", 0.88), ("R3", "no", 0.99)))
    assert v.verdict == "met" and v.confidence == 0.88  # non-critical R3 does not block, min of R1, R2


def test_one_required_no_is_not_met():
    v = verdict_from_items(COND, answers(("R1", "no", 0.95), ("R2", "yes", 0.9)))
    assert v.verdict == "not_met" and v.confidence == 0.95
    assert decide(v, COND, 0.85).action == "void"


@pytest.mark.parametrize("rows", [[("R1", "yes", 0.99), ("R2", "unsure", 0.99)],
                                  [("R1", "yes", 0.99)],                       # R2 missing
                                  []])
def test_unsure_or_missing_required_item_is_uncertain(rows):
    v = verdict_from_items(COND, answers(*rows))
    assert v.verdict == "uncertain" and decide(v, COND, 0.85).action == "hold"


def test_low_item_confidence_still_holds_bar_unchanged():
    v = verdict_from_items(COND, answers(("R1", "yes", 0.99), ("R2", "yes", 0.84)))
    assert v.verdict == "met" and decide(v, COND, 0.85).action == "hold"


def test_duplicate_item_answers_use_the_first():
    v = verdict_from_items(COND, answers(("R1", "yes", 0.9), ("R2", "no", 0.9), ("R2", "yes", 0.99)))
    assert v.verdict == "not_met"


# ---- 4. image budget: 1024 px long edge, at most 3 photos, sharpest non-duplicate ----

def _big(tmp_path, name, size=(2400, 1800), colour=(200, 30, 30)):
    img = Image.new("RGB", size, colour)
    img.save(tmp_path / name, "JPEG")
    return name


def test_images_downscaled_to_long_edge_and_capped_at_three(tmp_path):
    from janus.logs import setup as s
    s(tmp_path)
    names = [_big(tmp_path, f"e{i}.jpg", colour=(40 * i, 30, 30)) for i in range(5)]
    _big(tmp_path, "l.jpg")
    hashes = ["0000000000000000", "00000000ffffffff", "ffffffff00000000", "ffffffffffffffff", "0f0f0f0f0f0f0f0f"]
    ev = [EvidenceItem(id=f"V{i+1}", file=n, media_type="image/jpeg", phash=hashes[i],
                       quality={"ok": True, "sharpness": float(i)}) for i, n in enumerate(names)]
    msgs = FakeMessages(parsed=answers())
    ai_with(msgs).verify(COND, Listing(title="m", price="1", photos=["l.jpg"]), ev, tmp_path)
    imgs = [b for b in msgs.calls[0]["messages"][0]["content"] if b["type"] == "image"]
    assert len(imgs) == 1 + 3  # listing + at most 3 evidence photos
    for b in imgs:
        assert max(Image.open(io.BytesIO(base64.b64decode(b["source"]["data"]))).size) == 1024


def test_small_images_are_sent_unchanged(setup):
    root, listing = setup
    msgs = FakeMessages(parsed=answers())
    ai_with(msgs).verify(COND, listing, EV, root)
    imgs = [b for b in msgs.calls[0]["messages"][0]["content"] if b["type"] == "image"]
    assert base64.b64decode(imgs[-1]["source"]["data"]) == (root / EV[0].file).read_bytes()


def test_pick_evidence_drops_duplicates_and_prefers_sharp_passing_photos():
    ev = [EvidenceItem(id="V1", file="a", phash="0000000000000000", quality={"ok": True, "sharpness": 50}),
          EvidenceItem(id="V2", file="b", phash="0000000000000001", quality={"ok": True, "sharpness": 90}),  # dup of V1
          EvidenceItem(id="V3", file="c", phash="ffff000000000000", quality={"ok": False, "sharpness": 500}),
          EvidenceItem(id="V4", file="d", phash="00000000ffffffff", quality={"ok": True, "sharpness": 60}),
          EvidenceItem(id="V5", file="e", phash="ffffffff00000000", quality={"ok": True, "sharpness": 70})]
    assert [e.id for e in pick_evidence(ev)] == ["V2", "V4", "V5"]


# ---- 3. gate hard-fail: resubmit, no model call; never skip the AI to capture ----

class NoCallAI(MockAI):
    calls = 0

    def verify(self, *a, **k):
        NoCallAI.calls += 1
        return super().verify(*a, **k)


def test_gate_hard_fail_never_calls_the_model_and_never_captures(tmp_path):
    NoCallAI.calls = 0
    j = Janus(make_config(tmp_path), ai=NoCallAI())
    t = authorized(j)
    t = j.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg")
    assert t.state == State.HELD and NoCallAI.calls == 0 and not t.payment.captures
    assert j.get(t.id).last_verdict.verdict.next_step  # tells the buyer what to resubmit


def test_calibration_harness_skips_the_model_on_gate_fail(tmp_path):
    from pathlib import Path
    from janus import calibration
    NoCallAI.calls = 0
    root = Path(__file__).resolve().parent.parent
    res = calibration.run(NoCallAI(), root / "calibration/labels.json", root, tmp_path / "w")
    gated = [r for r in res["rows"] if r["gate_blocked"]]
    assert gated and NoCallAI.calls == res["n"] - len(gated)
    assert all(r["verdict"] == "uncertain" and r["action"] == "hold" for r in gated)
