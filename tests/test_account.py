"""My jobs, activity, status timeline, profile and help: what a browser may see, and how events are described."""
import re

import pytest

from conftest import make_config
from janus import web_account as wa
from janus.service import Janus
from janus.state import State
from test_job_http import approve_hold, create, photos
from test_jobs import job, packet
from test_server import serve


@pytest.fixture
def env(tmp_path):
    j = Janus(make_config(tmp_path, base_url="http://localhost:8000"))
    httpd, call = serve(j)
    yield j, call
    httpd.shutdown()


def wallet_after_opening(call, path, cookie=None):
    status, _, h = call("GET", path, cookie=cookie)
    assert status == 200
    c = h["Set-Cookie"]
    assert c.startswith(wa.WALLET + "=") and "HttpOnly" in c and "Path=/" in c
    return c.split(";")[0]


# ---------- access control: a browser sees only jobs whose link it opened ----------

def test_my_jobs_lists_only_jobs_opened_on_this_browser(env):
    j, call = env
    tid1, ct1, pt1, _ = create(call, "")
    tid2, _, pt2, _ = create(call, "")
    cookie = wallet_after_opening(call, f"/t/{tid1}?t={ct1}")
    html = call("GET", "/jobs", cookie=cookie)[1]
    assert tid1 in html and tid2 not in html and "Customer with your provider" in html
    assert call("GET", "/jobs")[1].count("/t/th_") == 0 and "No jobs yet" in call("GET", "/jobs")[1]
    both = wallet_after_opening(call, f"/t/{tid2}?k={pt2}", cookie=cookie)
    html = call("GET", "/jobs", cookie=both)[1]
    assert tid1 in html and tid2 in html and "Provider with your customer" in html


def test_forged_or_foreign_tokens_in_the_cookie_show_nothing(env):
    j, call = env
    tid1, ct1, _, _ = create(call, "")
    tid2, _, _, _ = create(call, "")
    forged = f"{wa.WALLET}={tid2}~{ct1}|{tid1}~{'A' * 43}|th_0000000000000000~{ct1}|junk"
    html = call("GET", "/jobs", cookie=forged)[1]
    assert "/t/th_" not in html and "No jobs yet" in html
    assert wa.held_jobs(j, forged.split("=", 1)[1]) == []


def test_a_tenant_key_is_never_stored_and_the_wallet_is_bounded():
    raw = None
    for n in range(wa.WALLET_MAX + 5):
        raw = wa.wallet_add(raw, f"th_{n:016x}", "tok")
    items = wa.wallet_items(raw)
    assert len(items) == wa.WALLET_MAX and items[0][0] == f"th_{wa.WALLET_MAX + 4:016x}"
    assert wa.wallet_items(wa.wallet_add(raw, items[3][0], "tok"))[0] == items[3]   # re-opening moves it first


def test_active_and_completed_tabs_follow_the_real_states(env):
    j, call = env
    tid, ct, pt, _ = create(call, "")
    approve_hold(call, "", tid, ct)
    photos(call, "", tid, pt)
    cookie = wallet_after_opening(call, f"/t/{tid}?t={ct}")
    active = call("GET", "/jobs", cookie=cookie)[1]
    assert tid in active and "Packet ready" in active and "Held 40.00 · captured 0.00" in active
    assert call("POST", f"/api/t/{tid}/partial", {"amount": "25.00"}, token=ct)[0] == 200
    lists = lambda tab: call("GET", f"/jobs?tab={tab}", cookie=cookie)[1].split("Recent activity")[0]  # noqa: E731
    active, done = lists("active"), lists("completed")
    assert tid not in active and tid in done and "Partly paid" in done and "Held 0.00 · captured 25.00" in done


# ---------- activity feed: read-only mapping of audit entries ----------

def test_activity_maps_audit_entries_per_role(tmp_path):
    j = Janus(make_config(tmp_path))
    t = packet(j, job(j))
    entries = j.audit.entries(t.id)
    cust = wa.activity([(wa.Held(t, "buyer", "x"), entries)])
    prov = wa.activity([(wa.Held(t, "seller", "y"), entries)])
    texts = [x["text"] for x in cust]
    assert texts[0].endswith("your decision is needed")                      # newest first
    assert "You approved the PayPal hold" in texts and "The provider uploaded the after photo" in texts
    assert "You uploaded the before photo" in [x["text"] for x in prov]
    assert all(x["job"] == t.id and x["title"] == t.listing.title for x in cust)
    assert wa.describe({"kind": "rule_check", "data": {}}, "buyer") is None   # internal entries are not shown
    assert wa.describe({"kind": "packet", "data": {"blocking_flags": ["reused_photo:V2"]}}, "buyer")[1] == "warn"
    j.reject(t.id, "no")
    assert wa.activity([(wa.Held(t, "buyer", "x"), j.audit.entries(t.id))])[0]["text"].startswith("Hold released")


# ---------- status timeline ----------

def test_timeline_follows_the_job(tmp_path):
    j = Janus(make_config(tmp_path))
    t = j.create_job("Patch wall hole", "40", "Patch the hole. Paint it.")
    steps = wa.timeline(t, j.audit.entries(t.id))
    assert [s[2] for s in steps] == ["done", "now", "todo", "todo", "todo", "todo"]
    t = packet(j, job(j))
    steps = wa.timeline(t, j.audit.entries(t.id))
    assert [s[2] for s in steps] == ["done", "done", "done", "done", "now", "todo"] and all(s[1] for s in steps[:4])
    t = j.partial(t.id, "10.00", override_reason="ok")
    steps = wa.timeline(t, j.audit.entries(t.id))
    assert all(s[2] == "done" for s in steps) and steps[-1][0] == "Partially captured, rest voided" and steps[-1][1]


def test_timeline_after_a_lapse_points_back_at_the_hold(tmp_path):
    j = Janus(make_config(tmp_path))
    t = packet(j, job(j))
    j.paypal.expire(t.payment.authorization_id)
    t = j.refresh(t.id)
    assert t.state == State.HOLD_LAPSED
    steps = wa.timeline(t, j.audit.entries(t.id))
    assert steps[1] == ("Hold lapsed: approve a new hold", None, "now")


# ---------- job page money, other pages, friendly errors ----------

def test_job_page_shows_money_timeline_and_pay_part_numbers(env):
    j, call = env
    tid, ct, pt, _ = create(call, "")
    approve_hold(call, "", tid, ct)
    photos(call, "", tid, pt)
    html = call("GET", f"/t/{tid}?t={ct}")[1]
    assert "Janus never holds your money. PayPal holds it until you decide." in html
    assert "Janus fee</dt><dd>0.00 USD" in html and "Still held by PayPal</dt><dd>40.00 USD" in html
    assert "Hold expires on" in html and 'data-held="40.00"' in html and "partNumbers()" in html
    assert html.index("<h2>Status</h2>") < html.index("<h2>Audit trail</h2>")
    assert '<li class="now">Decision' in html
    assert "data-held" not in call("GET", f"/t/{tid}?k={pt}")[1]       # only the customer decides


def test_help_profile_nav_and_friendly_not_found(env):
    j, call = env
    status, html, _ = call("GET", "/help")
    assert status == 200 and html.count("<details>") >= 8 and "Nothing about money" in html
    status, html, _ = call("GET", "/profile")
    assert status == 200 and "/api/auth/session" in html and "Active jobs</dt><dd>0" in html
    assert all(f'href="/{p}"' in html for p in ("jobs", "help", "profile"))
    status, html, _ = call("GET", "/t/th_0123456789abcdef?t=nope")
    assert status == 404 and "This link is not valid" in html and 'href="/jobs"' in html
    assert re.search(r"<h1>Not found</h1>", html) and "not found</p>" not in html
