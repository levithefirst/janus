"""Tokens, per-route authorization, tenant isolation, signed photo URLs, rate limits, secrets and PII."""
import json
import math
from datetime import timedelta

import pytest

from conftest import LISTING, PHOTOS, authorized, make_config, with_exif
from janus import clock
from janus.security import RateLimiter, Security, SecurityConfigError, load_secret, parse_keys
from janus.server import API_ROLES
from janus.service import Janus
from test_server import b64, create, pay, serve, tokens

TENANT_A, TENANT_B = "A" * 30, "B" * 30
ADMIN = "Z" * 30


@pytest.fixture
def env(tmp_path):
    j = Janus(make_config(tmp_path, tenant_keys=f"acme:{TENANT_A},beta:{TENANT_B}", admin_token=ADMIN))
    httpd, call = serve(j)
    yield j, call
    httpd.shutdown()


# ---------- tokens ----------

def test_tokens_have_at_least_128_bits_and_differ_per_role(janus):
    t = authorized(janus)
    b, s = janus.token(t, "buyer"), janus.token(t, "seller")
    assert len(b) * 6 >= 128 and len(b) == 43 and b != s       # 256-bit HMAC, base64url
    assert janus.role_for(t, b) == "buyer" and janus.role_for(t, s) == "seller"
    assert janus.role_for(t, b[:-1] + ("A" if b[-1] != "A" else "B")) is None


def test_token_is_bound_to_threshold_and_tenant():
    sec = Security(b"k" * 32)
    assert sec.token("public", "th_1", "buyer") != sec.token("public", "th_2", "buyer")
    assert sec.token("acme", "th_1", "buyer") != sec.token("beta", "th_1", "buyer")


def test_weak_secrets_and_keys_are_refused():
    with pytest.raises(SecurityConfigError):
        load_secret("short", allow_ephemeral=False)
    with pytest.raises(SecurityConfigError):
        load_secret("", allow_ephemeral=False)
    with pytest.raises(SecurityConfigError):
        parse_keys("acme:tooshort")
    with pytest.raises(SecurityConfigError):
        Security(b"k" * 32, admin_token="short")


def test_threshold_ids_are_not_guessable_and_not_credentials(janus):
    t = authorized(janus)
    assert len(t.id) == 3 + 16
    assert janus.role_for(t, t.id) is None


# ---------- authorization on every route ----------

def test_every_api_action_checks_the_role(env):
    j, call = env
    tid, bt, st = create(call)
    for action, roles in API_ROLES.items():
        for who, tok in (("none", None), ("buyer", bt), ("seller", st), ("other", "x" * 43)):
            status, *_ = call("POST", f"/api/t/{tid}/{action}", {}, token=tok)
            if who not in roles:
                assert status == 404, (action, who, status)   # not even "forbidden": nothing to probe
            else:
                assert status != 404, (action, who, status)


@pytest.mark.parametrize("path", ["/t/{id}", "/t/{id}/pay", "/api/t/{id}", "/api/t/{id}/packet"])
def test_reads_need_a_token(env, path):
    _, call = env
    tid, bt, _ = create(call)
    assert call("GET", path.format(id=tid))[0] == 404
    assert call("GET", path.format(id=tid) + "?t=" + "x" * 43)[0] == 404


def test_evidence_submitter_comes_from_the_token_not_the_body(env):
    j, call = env
    tid, bt, st = create(call)
    pay(call, tid, bt)
    call("POST", f"/api/t/{tid}/evidence", {"photo_b64": b64(with_exif("received_match.jpg")), "by": "buyer",
                                             "verify": False}, token=st)
    assert j.get(tid).evidence[-1].by == "seller"


def test_paypal_return_without_cookie_reveals_nothing(env):
    j, call = env
    tid, bt, _ = create(call)
    call("GET", f"/t/{tid}/pay?t={bt}")
    order = j.get(tid).payment.order_id
    status, html, h = call("GET", f"/paypal/return?token={order}")
    assert status == 200 and bt not in html and "Location" not in h   # authorized, but no link handed out
    status, _, h = call("GET", f"/paypal/return?token={order}", cookie=f"janus_{tid}={'x' * 43}")
    assert status == 200 and "Location" not in h                      # a forged cookie gets nothing either


def test_list_needs_a_key(env):
    _, call = env
    assert call("GET", "/api/thresholds")[0] == 401


# ---------- tenants ----------

def test_tenant_isolation(env):
    j, call = env
    status, a, _ = call("POST", "/api/thresholds", LISTING, token=TENANT_A)
    status, b, _ = call("POST", "/api/thresholds", LISTING, token=TENANT_B)
    assert a["threshold"]["tenant"] == "acme" and b["threshold"]["tenant"] == "beta"
    assert [x["id"] for x in call("GET", "/api/thresholds", token=TENANT_A)[1]] == [a["id"]]
    assert call("GET", f"/api/t/{b['id']}", token=TENANT_A)[0] == 404          # other tenant's job
    assert call("POST", f"/api/t/{b['id']}/refund", {}, token=TENANT_A)[0] == 404
    assert call("GET", f"/api/t/{a['id']}", token=TENANT_A)[0] == 200          # tenant key acts as provider
    assert len(call("GET", "/api/thresholds", token=ADMIN)[1]) == 2            # admin reads all
    assert call("POST", f"/api/t/{a['id']}/refund", {}, token=ADMIN)[0] == 404  # admin cannot move money


def test_public_create_can_be_turned_off(tmp_path):
    j = Janus(make_config(tmp_path, public_create=False, tenant_keys=f"acme:{TENANT_A}"))
    httpd, call = serve(j)
    try:
        assert call("POST", "/api/thresholds", LISTING)[0] == 401
        assert call("POST", "/api/thresholds", LISTING, token=TENANT_A)[0] == 201
    finally:
        httpd.shutdown()


# ---------- signed, short-lived photo URLs ----------

def test_photo_urls_are_signed_and_expire(janus):
    sec = janus.security
    url = sec.sign_path("/t/th_1/files/evidence/V1.jpg")
    path, query = url.split("?")
    q = dict(x.split("=") for x in query.split("&"))
    assert sec.check_signed(path, q["exp"], q["sig"])
    assert not sec.check_signed("/t/th_2/files/evidence/V1.jpg", q["exp"], q["sig"])   # other file
    assert not sec.check_signed(path, str(int(q["exp"]) + 60), q["sig"])                # extended expiry
    with clock.frozen(clock.utcnow() + timedelta(minutes=6)):
        assert not sec.check_signed(path, q["exp"], q["sig"])                           # expired


def test_unsigned_photo_url_is_refused(env):
    j, call = env
    tid, bt, _ = create(call)
    assert call("GET", f"/t/{tid}/files/listing/L1.jpg")[0] == 404
    assert call("GET", f"/t/{tid}/files/listing/L1.jpg?t={bt}")[0] == 404  # a role token is not enough


# ---------- rate limits ----------

def test_rate_limit_returns_429(tmp_path):
    j = Janus(make_config(tmp_path))
    httpd, call = serve(j, RateLimiter({"read": (3, 60.0), "write": (3, 60.0), "expensive": (1, 60.0),
                                        "webhook": (3, 60.0)}))
    try:
        assert [call("GET", "/healthz")[0] for _ in range(4)] == [200, 200, 200, 429]
        assert call("POST", "/api/thresholds", LISTING)[0] == 201
        status, _, h = call("POST", "/api/thresholds", LISTING)
        assert status == 429 and int(h["Retry-After"]) >= 1
    finally:
        httpd.shutdown()


def test_rate_limiter_refills():
    rl = RateLimiter({"read": (2, 1.0)})
    assert rl.allow("ip", "read")[0] and rl.allow("ip", "read")[0] and not rl.allow("ip", "read")[0]
    assert rl.allow("other-ip", "read")[0]


# ---------- secrets and PII ----------

def test_no_secret_or_token_is_written_to_disk(env):
    j, call = env
    tid, bt, st = create(call)
    pay(call, tid, bt)
    call("POST", f"/api/t/{tid}/evidence", {"photo_b64": b64(with_exif("received_match.jpg")),
                                             "note": "call me on +44 20 7946 0958 or me@example.com"}, token=bt)
    blob = "".join(p.read_text(errors="ignore") for p in j.config.data_dir.rglob("*") if p.is_file()
                   and p.suffix in (".json", ".jsonl"))
    for secret in (bt, st, TENANT_A, ADMIN, j.config.janus_secret):
        assert secret not in blob
    logs = (j.config.data_dir / "janus.log.jsonl").read_text() + (j.config.data_dir / "audit.jsonl").read_text()
    assert "7946" not in logs and "me@example.com" not in logs


# ---------- input hardening ----------

def test_decompression_bomb_is_refused(janus):
    import io

    from PIL import Image

    from janus.service import JanusError, normalize_image
    out = io.BytesIO()
    Image.new("L", (8000, 6000)).save(out, "PNG")  # ~48 MP of zeros: a small file
    assert len(out.getvalue()) < 1_000_000
    with pytest.raises(JanusError, match="too large"):
        normalize_image(out.getvalue())


def test_submitter_text_is_escaped_in_pages(env):
    j, call = env
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    status, d, _ = call("POST", "/api/thresholds", dict(LISTING, title=evil, description=evil, condition=evil))
    bt, st = tokens(d)
    for tok, q in ((bt, "t"), (st, "k")):
        html = call("GET", f"/t/{d['id']}?{q}={tok}")[1]
        assert "<script>alert(1)" not in html and "<img src=x" not in html and "&lt;script&gt;" in html
