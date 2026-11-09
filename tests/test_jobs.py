"""Job mode: provider's before/after photos, an explaining acceptance packet, and only the customer decides."""
from datetime import timedelta

import pytest

from conftest import make_config
from eval.scenes import ROOMS, draw, jpeg
from janus import clock
from janus.ai import AIError, MockAI
from janus.rules import RuleViolation, check_capture
from janus.service import Janus, JanusError, OverrideRequired
from janus.state import State, TransitionError

DESC = "Patch the hole in the living room wall. Paint the patch to match the wall."


def job(j, price="40.00"):
    t = j.create_job("Patch wall hole", price, DESC)
    j.consent(t.id, channel="api")
    j.create_payment(t.id)
    return j.authorize(t.id)


def before(room="sage", seed=1, **kw):
    return jpeg(draw(ROOMS[room], "hole", False, seed=seed), taken=clock.utcnow() - timedelta(hours=1), **kw)


def after(room="sage", seed=2, done=True, **kw):
    taken = kw.pop("taken", clock.utcnow())
    return jpeg(draw(ROOMS[room], "hole", done, seed=seed, shift=12, light=1.05), taken=taken, **kw)


def packet(j, t, b=None, a=None):
    j.submit_evidence(t.id, b or before(), by="seller", phase="before")
    j.submit_evidence(t.id, a or after(), by="seller", phase="after")
    return j.verify(t.id)


def auth(j, t):
    return j.paypal.auth(j.get(t.id).payment.authorization_id)


@pytest.fixture
def j(tmp_path):
    return Janus(make_config(tmp_path))


# ---------- the packet ----------

def test_job_card_and_hold(j):
    t = j.create_job("Patch wall hole", "40", DESC)
    assert t.kind == "job" and t.amount == "40.00" and len(t.compiled.requirements) == 2
    t = j.consent(t.id)
    assert t.consents[-1].kind == "hold"
    j.create_payment(t.id)
    assert j.authorize(t.id).state == State.AUTHORIZED


def test_clean_packet_explains_each_item_and_moves_no_money(j):
    t = job(j)
    t = j.submit_evidence(t.id, before(), by="seller", phase="before")
    assert t.state == State.AUTHORIZED                    # a before photo never triggers a packet
    t = j.submit_evidence(t.id, after(), by="seller", phase="after")
    assert t.state == State.EVIDENCE_SUBMITTED
    t = j.verify(t.id)
    rec = t.last_verdict
    assert t.state == State.PACKET_READY and rec.action == "recommend_accept" and rec.blocking_flags == []
    assert all(c.result == "met" and c.confidence is not None and c.observation for c in rec.verdict.checks)
    assert rec.verdict.scene_match == "same" and rec.verdict.edit_signs == "none"
    assert not t.payment.captures and auth(j, t)["status"] == "CREATED"
    with pytest.raises(RuleViolation, match="customer_decides"):
        check_capture(t, "janus", 0.85)


def test_only_the_provider_uploads_marked_photos(j):
    t = job(j)
    with pytest.raises(JanusError):
        j.submit_evidence(t.id, before(), by="buyer", phase="before")
    with pytest.raises(JanusError):
        j.submit_evidence(t.id, before(), by="seller")


# ---------- the customer decides ----------

def test_accept_captures_in_full(j):
    t = job(j)
    packet(j, t)
    t = j.accept(t.id)
    assert t.state == State.CAPTURED and [c.amount for c in t.payment.captures] == ["40.00"]
    assert t.payment.captures[0].final and t.outcome.by == "buyer"


def test_partial_captures_part_and_voids_the_rest(j):
    t = job(j)
    packet(j, t)
    t = j.partial(t.id, "15.00")
    assert t.state == State.PARTIALLY_CAPTURED and t.outcome.action == "partial" and "15.00" in t.outcome.reason
    assert [(c.amount, c.final) for c in t.payment.captures] == [("15.00", False)]
    a = auth(j, t)
    assert a["captured"] == "15.00" and a["status"] == "VOIDED"
    kinds = [e["kind"] for e in j.audit.entries(t.id)]
    assert kinds.index("partial_capture") < kinds.index("void_requested") < kinds.index("partially_captured")
    assert f"janus-{t.id}-void" in {e["data"].get("request_id") for e in j.audit.entries(t.id)}


@pytest.mark.parametrize("amount", ["40.00", "41", "0", "-5", "abc"])
def test_partial_amount_must_be_positive_and_below_the_hold(j, amount):
    t = job(j)
    packet(j, t)
    with pytest.raises(JanusError):
        j.partial(t.id, amount)
    assert j.get(t.id).state == State.PACKET_READY and not j.get(t.id).payment.captures


def test_reject_voids(j):
    t = job(j)
    packet(j, t)
    t = j.reject(t.id, "not finished")
    assert t.state == State.VOIDED and not t.payment.captures and t.outcome.by == "buyer"


def test_customer_can_decide_before_any_packet(j):
    t = job(j)
    assert j.reject(t.id).state == State.VOIDED


def test_threshold_actions_are_not_job_actions(janus):
    from conftest import authorized
    t = authorized(janus)
    with pytest.raises(JanusError):
        janus.accept(t.id)


# ---------- flags, override, manual review ----------

def test_reused_photo_flags_and_needs_a_logged_override(j):
    first = job(j)
    reused = after(seed=9)
    packet(j, first, a=reused)
    t = job(j)
    t = packet(j, t, a=reused)
    rec = t.last_verdict
    assert t.state == State.MANUAL_REVIEW and any(f.startswith("reused_photo") for f in rec.blocking_flags)
    with pytest.raises(OverrideRequired):
        j.accept(t.id)
    with pytest.raises(OverrideRequired):
        j.partial(t.id, "10")
    assert not j.get(t.id).payment.captures and auth(j, t)["status"] == "CREATED"
    t = j.accept(t.id, override_reason="I watched the work being done")
    assert t.state == State.CAPTURED
    o = [c for c in t.consents if c.kind == "override"][0]
    assert o.verdict_index == len(t.verdicts) - 1 and o.reason == "I watched the work being done"
    entry = [e for e in j.audit.entries(t.id) if e["kind"] == "override"][0]
    assert entry["actor"] == "buyer" and entry["data"]["overridden_flags"] == rec.blocking_flags


def test_flagged_packet_can_always_be_rejected_without_override(j):
    t = job(j)
    b = before()
    t = packet(j, t, b=b, a=b)  # the "after" is the before photo again
    assert any(f.startswith("after_matches_before") for f in t.last_verdict.blocking_flags)
    assert j.reject(t.id).state == State.VOIDED


def test_rule_refuses_capture_without_override_even_if_service_is_bypassed(j):
    t = job(j)
    b = before()
    packet(j, t, b=b, a=b)
    x = j.get(t.id)
    from janus.store import Consent
    from janus import rules
    x.consents.append(Consent(kind="release", by="buyer", at="now", terms_hash=rules.terms_hash(x), amount="40.00",
                              channel="api"))
    with pytest.raises(RuleViolation, match="flags_need_override"):
        check_capture(x, "buyer", 0.85)


def test_override_is_bound_to_the_packet_it_was_given_for(j):
    t = job(j)
    b = before()
    packet(j, t, b=b, a=b)
    x = j.get(t.id)
    from janus.store import Consent
    from janus import rules
    for kind, idx, reason in (("override", 99, "x"), ("override", len(x.verdicts) - 1, " "), ("release", None, "x")):
        y = x.model_copy(deep=True)
        y.consents += [Consent(kind=kind, by="buyer", at="now", terms_hash=rules.terms_hash(y), amount="40.00",
                               channel="api", verdict_index=idx, reason=reason),
                       Consent(kind="release", by="buyer", at="now", terms_hash=rules.terms_hash(y),
                               amount="40.00", channel="api")]
        with pytest.raises(RuleViolation):
            check_capture(y, "buyer", 0.85)


def test_wrong_room_goes_to_manual_review(j):
    t = job(j)
    t = packet(j, t, a=after(room="blue"))
    assert t.state == State.MANUAL_REVIEW and t.last_verdict.verdict.scene_match in ("different", "unclear")


def test_editor_software_flags(j):
    t = job(j)
    t = packet(j, t, a=after(software="Adobe Photoshop 25.1 (Windows)"))
    assert t.state == State.MANUAL_REVIEW
    assert any(f.startswith("edited_software") for f in t.last_verdict.blocking_flags)
    assert "likely_edited" in t.last_verdict.blocking_flags


def test_camera_firmware_software_tag_is_not_an_edit(j):
    t = job(j)
    t = packet(j, t, a=after(software="HDR+ 1.0.540104767zd"))
    assert t.state == State.PACKET_READY


def test_pasted_and_smoothed_area_flags_without_any_editor_tag(j):
    """A pixel edit saved with camera EXIF and no Software tag: the noise check catches it, not the AI."""
    from eval.scenes import paste_from
    base = draw(ROOMS["sage"], "hole", False, seed=1)
    donor = draw(ROOMS["blue"], "hole", True, seed=9, layout=900)
    t = packet(j, job(j), a=jpeg(paste_from(base, donor), taken=clock.utcnow()))
    assert t.state == State.MANUAL_REVIEW
    assert any(f.startswith("pixel_inconsistency") for f in t.last_verdict.blocking_flags)
    assert t.evidence[-1].quality["noise"]["smooth_region"] > 0.1


def test_noise_check_ignores_genuine_photos_clipped_areas_and_noiseless_images():
    from PIL import Image, ImageDraw
    from janus.evidence import SMOOTH_REGION_MAX, noise_consistency
    assert noise_consistency(draw(ROOMS["blue"], "shelf", True, seed=5))["smooth_region"] < SMOOTH_REGION_MAX
    img = draw(ROOMS["sage"], "hole", True, seed=6)
    ImageDraw.Draw(img).rectangle([100, 100, 400, 300], fill=(255, 255, 255))   # a blown-out window
    assert noise_consistency(img)["smooth_region"] < SMOOTH_REGION_MAX
    flat = noise_consistency(Image.new("RGB", (960, 720), (150, 150, 150)))       # no noise to compare
    assert flat["smooth_region"] == 0.0


def test_exif_time_flags(j):
    t = job(j)
    t = packet(j, t, a=after(taken=clock.utcnow() - timedelta(days=3)))
    flags = t.last_verdict.blocking_flags
    assert any(f.startswith("after_taken_before_authorization") for f in flags)
    assert any(f.startswith("after_older_than_before") for f in flags)


def test_missing_before_photo_flags(j):
    t = job(j)
    j.submit_evidence(t.id, after(), by="seller", phase="after")
    t = j.verify(t.id)
    assert t.state == State.MANUAL_REVIEW and "no_before_photo" in t.last_verdict.blocking_flags


def test_ai_unavailable_falls_back_to_manual_review(tmp_path):
    class Down(MockAI):
        def verify_job(self, *a, **k):
            raise AIError("verify_job: Claude API error: overloaded")
    j = Janus(make_config(tmp_path), ai=Down())
    t = job(j)
    t = packet(j, t)
    assert t.state == State.MANUAL_REVIEW and t.last_verdict.source == "ai_unavailable"
    assert "no_ai_packet" in t.last_verdict.blocking_flags
    with pytest.raises(OverrideRequired):
        j.accept(t.id)
    assert j.accept(t.id, override_reason="looked myself").state == State.CAPTURED


def test_provider_reupload_makes_a_new_packet_and_old_override_does_not_carry(j):
    t = job(j)
    b = before()
    packet(j, t, b=b, a=b)
    j.submit_evidence(t.id, after(seed=7), by="seller", phase="after")
    t = j.verify(t.id)
    assert len(t.verdicts) == 2
    # the stale after==before photo is still part of the evidence, so the new packet is still flagged
    assert t.state == State.MANUAL_REVIEW


# ---------- crash recovery between the partial capture and the void ----------

def test_crash_after_partial_capture_recovers_the_void(tmp_path, monkeypatch):
    j = Janus(make_config(tmp_path))
    t = job(j)
    packet(j, t)
    monkeypatch.setattr(j.paypal, "void_authorization", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        j.partial(t.id, "10.00")
    assert j.get(t.id).state == State.VOIDING and len(j.get(t.id).payment.captures) == 1
    monkeypatch.undo()
    j2 = Janus(make_config(tmp_path))
    [x] = j2.recover()
    assert x.state == State.PARTIALLY_CAPTURED and x.outcome.action == "partial"
    assert len(j2.paypal._db()["captures"]) == 1 and auth(j2, x)["status"] == "VOIDED"
