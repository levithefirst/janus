"""Web app + JSON API (stdlib only). Run: python -m janus serve

Pages (phone-friendly):
GET  /                        seller: describe the item and the condition -> buyer link + seller link
GET  /t/{id}                  buyer link: authorize on PayPal, photograph the item, see the outcome
GET  /t/{id}?k=SELLER_KEY     seller view of the same page (can refund while funds are held)
GET  /t/{id}/pay              shows the terms (records nothing, safe for link previews)
POST /t/{id}/pay              form: terms hash; records consent for those terms, then redirects to PayPal
GET  /paypal/return?token=..  PayPal sends the buyer back here -> authorize -> /t/{id}

JSON API:
POST /api/thresholds          {title, price, condition, description?, currency?, photo_b64?}
GET  /api/t/{id}              threshold state (seller key never included)
POST /api/t/{id}/payment      create order -> {buyer_link, approve_url, order_id}
POST /api/t/{id}/authorize    authorize after buyer approval (if the return URL was not reachable)
POST /api/t/{id}/evidence     {photo_b64, note?, verify?: true}  -> verdict + capture/void/hold
POST /api/t/{id}/verify       re-run verification on submitted evidence
POST /api/t/{id}/release      buyer releases held funds (capture)
POST /api/t/{id}/refund       {key} seller refunds (void)
POST /api/t/{id}/settle       retry a capture/void that failed with a retryable PayPal error
POST /api/t/{id}/refresh      read the authorization status from PayPal (detects expiry)
"""
from __future__ import annotations

import base64
import binascii
import html
import json
import re
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from . import clock
from .ai import AIError
from .logs import log_event
from . import rules
from .rules import RuleViolation
from .paypal import PayPalError
from .service import Janus, JanusError, OverrideRequired, StaleTermsError
from .state import HOLDS_FUNDS, State, TransitionError
from .security import RateLimiter
from . import web_jobs
from .store import NotFound, Threshold

STATIC = Path(__file__).parent / "static"
MAX_BODY = 20 * 1024 * 1024
ID = re.compile(r"th_[0-9a-f]{16}")

# Who may call what. Anything not listed here is refused. Unknown threshold or wrong token -> 404,
# so ids cannot be probed. The evidence submitter is whoever the token says, never the request body.
BUYER, SELLER, ADMIN = "buyer", "seller", "admin"
API_ROLES = {
    "consent": {BUYER}, "payment": {BUYER}, "authorize": {BUYER}, "release": {BUYER}, "dispute": {BUYER},
    "withdraw_dispute": {BUYER}, "accept_amendment": {BUYER}, "reject_amendment": {BUYER},
    "refund": {SELLER}, "allocations": {SELLER}, "amend": {SELLER}, "photo": {SELLER},
    "accept": {BUYER}, "partial": {BUYER}, "reject": {BUYER}, "renew": {BUYER},
    "evidence": {BUYER, SELLER}, "verify": {BUYER, SELLER}, "settle": {BUYER, SELLER}, "refresh": {BUYER, SELLER},
}
READ_ROLES = {BUYER, SELLER, ADMIN}
e = html.escape


def public(t: Threshold) -> dict:
    d = t.model_dump(mode="json", exclude={"events"})
    d["amount"], d["currency"] = t.amount, t.currency
    return d


def _b64(s: str) -> bytes:
    if not s:
        raise JanusError("photo_b64 is empty")
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    try:
        return base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        raise JanusError("photo_b64 is not valid base64")


# ---------------------------------------------------------------- HTML

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#16161a;--muted:#5d5d66;--line:#e3e1dc;--accent:#0b5cff;
--ok:#0f7a3d;--okbg:#e6f4ec;--bad:#b42318;--badbg:#fdecea;--hold:#8a5a00;--holdbg:#fff4dc;--logo-eye:#f6f5f2}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121215;--card:#1b1b20;--ink:#f1f0ec;
--muted:#a3a2ab;--line:#2c2c33;--accent:#6b9bff;--ok:#5fd394;--okbg:#12291c;--bad:#ff8a80;--badbg:#2e1513;
--hold:#ffc95c;--holdbg:#2c2210;--logo-eye:#121215}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:16px 16px 48px}
header{display:flex;align-items:center;gap:10px;margin:4px 0 18px}header svg{width:34px;height:34px}
header b{font-size:18px;letter-spacing:.2px}header span{color:var(--muted);font-size:14px}
h1{font-size:26px;line-height:1.2;margin:8px 0 6px}h2{font-size:15px;text-transform:uppercase;
letter-spacing:.06em;color:var(--muted);margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:0 0 14px}
.muted{color:var(--muted)}.small{font-size:14px}img.photo{width:100%;border-radius:10px;display:block;
background:var(--line)}
label{display:block;font-weight:600;margin:12px 0 4px}input,textarea{width:100%;font:inherit;color:inherit;
background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px}
textarea{min-height:76px}
.btn{display:block;width:100%;text-align:center;border:0;border-radius:12px;padding:14px;font:600 16px/1.2
inherit;background:var(--accent);color:#fff;text-decoration:none;cursor:pointer;margin-top:12px}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.btn[disabled]{opacity:.5}
.banner{border-radius:14px;padding:16px;margin:0 0 14px;font-weight:600}
.banner p{font-weight:400;margin:6px 0 0}.b-ok{background:var(--okbg);color:var(--ok)}
.b-bad{background:var(--badbg);color:var(--bad)}.b-hold{background:var(--holdbg);color:var(--hold)}
.b-info{background:var(--card);border:1px solid var(--line)}
ul.reqs{list-style:none;padding:0;margin:0}ul.reqs li{padding:8px 0;border-top:1px solid var(--line)}
ul.reqs li:first-child{border-top:0}.tag{display:inline-block;font-size:12px;font-weight:700;
border-radius:6px;padding:1px 7px;margin-right:6px}
.t-met{background:var(--okbg);color:var(--ok)}.t-not_met{background:var(--badbg);color:var(--bad)}
.t-cannot_tell{background:var(--holdbg);color:var(--hold)}.t-crit{background:var(--line);color:var(--ink)}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0;font-size:14px}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere;font-family:ui-monospace,Menlo,monospace}
.row{display:flex;gap:10px}.row>*{flex:1}.copy{display:flex;gap:8px}.copy input{font-size:14px}
.copy button{flex:0 0 auto;width:auto;margin:0;padding:10px 14px}
.thumbs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}
.thumbs figure{margin:0}.thumbs figcaption{font-size:12px;color:var(--muted)}
#busy{display:none}.spin{display:inline-block;width:16px;height:16px;border:2px solid currentColor;
border-right-color:transparent;border-radius:50%;animation:s 1s linear infinite;vertical-align:-3px}
@keyframes s{to{transform:rotate(360deg)}}
"""

JS = """
async function shrink(file){ // phone photos -> max 1600px JPEG, so uploads are small and fast
  const url=URL.createObjectURL(file); const img=new Image();
  await new Promise((ok,err)=>{img.onload=ok;img.onerror=err;img.src=url});
  const s=Math.min(1,1600/Math.max(img.naturalWidth,img.naturalHeight));
  const c=document.createElement('canvas'); c.width=Math.round(img.naturalWidth*s); c.height=Math.round(img.naturalHeight*s);
  c.getContext('2d').drawImage(img,0,0,c.width,c.height); URL.revokeObjectURL(url);
  return c.toDataURL('image/jpeg',0.85);
}
function preview(input,id){const f=input.files[0];const el=document.getElementById(id);
  if(f&&el){el.src=URL.createObjectURL(f);el.style.display='block'}}
async function post(url,body){const h={'Content-Type':'application/json'};
  const tk=document.body.dataset.token;if(tk)h['X-Janus-Token']=tk;
  const r=await fetch(url,{method:'POST',headers:h,
  body:JSON.stringify(body||{})});const d=await r.json().catch(()=>({error:'bad response'}));
  if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d}
function busy(on,msg){const b=document.getElementById('busy');if(!b)return;b.style.display=on?'block':'none';
  if(msg)b.querySelector('span.msg').textContent=msg;document.querySelectorAll('button').forEach(x=>x.disabled=on)}
function fail(err){busy(false);alert(err.message||err)}
async function act(url,body,msg){try{busy(true,msg);await post(url,body);location.reload()}catch(e){fail(e)}}
function copy(id){const i=document.getElementById(id);i.select();navigator.clipboard&&navigator.clipboard.writeText(i.value)}
"""


def page(title: str, body: str, refresh: int = 0, token: str = "") -> str:
    logo = (STATIC / "logo.svg").read_text()
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<meta name="referrer" content="no-referrer">
<title>{e(title)}</title><link rel="icon" href="/logo.svg"><style>{CSS}</style></head>
<body data-token="{e(token)}"><main><header>{logo}<div><b>Janus Threshold</b><br><span>Describe what must be true. Janus verifies it.
PayPal moves the money.</span></div></header>{body}
<div id="busy" class="banner b-info"><span class="spin"></span> <span class="msg">Working…</span></div>
</main><script>{JS}</script></body></html>"""


def landing(j: Janus) -> str:
    mode = f"PayPal {j.paypal.mode} · AI {j.ai.mode}"
    return page("Janus Threshold", f"""
<h1>Get paid only when it's true.</h1>
<p class="muted">You state the condition in plain words. The buyer's payment is held by PayPal, not
charged. When the item arrives, the buyer photographs it and Janus checks the photo against your
condition: met, the payment is captured; not met, the hold is released; unclear, nothing moves until
there's better evidence.</p>
<form class="card" id="f" onsubmit="create(event)">
<h2>1 · Your listing</h2>
<label for="title">Item</label><input id="title" required value="Red ceramic mug with white star">
<label for="desc">Description</label>
<textarea id="desc">12 oz glossy red ceramic mug. White star printed on the front. No chips or cracks.</textarea>
<div class="row"><div><label for="price">Price</label><input id="price" required inputmode="decimal" value="24.00"></div>
<div><label for="cur">Currency</label><input id="cur" value="USD" maxlength="3"></div></div>
<label for="photo">Listing photo</label>
<input id="photo" type="file" accept="image/*" onchange="preview(this,'pv')">
<img id="pv" class="photo" style="display:none;margin-top:8px" alt="">
<h2 style="margin-top:20px">2 · The condition</h2>
<textarea id="cond" required>Pay only if this item matches the listing photo and description.</textarea>
<button class="btn" type="submit">Compile condition &amp; create buyer link</button>
<p class="small muted">{e(mode)}</p></form>
<script>
async function create(ev){{ev.preventDefault();const g=id=>document.getElementById(id).value;
 try{{busy(true,'Janus is compiling your condition…');const f=document.getElementById('photo').files[0];
  const d=await post('/api/thresholds',{{title:g('title'),description:g('desc'),price:g('price'),currency:g('cur'),
   condition:g('cond'),photo_b64:f?await shrink(f):null}});location.href=d.seller_link}}catch(e){{fail(e)}}}}
</script>""")


def _req_list(t: Threshold, checks: dict | None = None) -> str:
    items = []
    for r in t.compiled.requirements:
        c = (checks or {}).get(r.id)
        tag = (f'<span class="tag t-{e(c.result)}">{e(c.result.replace("_", " "))}</span>' if c
               else ('<span class="tag t-crit">must</span>' if r.critical else '<span class="tag">nice</span>'))
        obs = f'<div class="small muted">{e(c.observation)}</div>' if c else \
            f'<div class="small muted">Check: {e(r.check)}</div>'
        items.append(f"<li>{tag}<b>{e(r.id)}</b> {e(r.statement)}{obs}</li>")
    return f'<ul class="reqs">{"".join(items)}</ul>'


def _img(j: Janus, t: Threshold, rel: str | None, cap: str = "") -> str:
    if not rel:
        return ""
    src = j.security.sign_path(f"/t/{t.id}/files/{quote(rel)}")
    fig = f'<img class="photo" src="{e(src)}" alt="{e(cap)}" loading="lazy">'
    return f"<figure>{fig}<figcaption>{e(cap)}</figcaption></figure>" if cap else fig


def _upload_form(t: Threshold, label: str) -> str:
    return f"""<div class="card"><h2>{e(label)}</h2><p>{e(t.compiled.evidence_instructions)}</p>
<input id="ev" type="file" accept="image/*" capture="environment" onchange="preview(this,'evpv')">
<img id="evpv" class="photo" style="display:none;margin-top:8px" alt="">
<label for="note">Note (optional)</label><input id="note" placeholder="Anything the seller should know">
<button class="btn" onclick="send()">Send photo to Janus</button></div>
<script>async function send(){{const f=document.getElementById('ev').files[0];if(!f){{alert('Take a photo first');return}}
 try{{busy(true,'Janus is checking the photo against the condition…');await post('/api/t/{t.id}/evidence',
  {{photo_b64:await shrink(f),note:document.getElementById('note').value,verify:true}});location.reload()}}catch(e){{fail(e)}}}}</script>"""


def threshold_page(j: Janus, t: Threshold, seller: bool) -> str:
    money = f"{t.amount} {t.currency}"
    p, v = t.payment, t.last_verdict
    me = j.token(t, "seller" if seller else "buyer")
    key_q = f"?k={quote(me)}" if seller else ""
    parts, refresh = [], 0

    # -- outcome / next step banner
    if t.state in (State.CREATED, State.PAYMENT_PENDING):
        if seller:
            parts.append(f"""<div class="banner b-info">Send this link to the buyer<p>They authorize {e(money)}
on PayPal. Nothing is charged unless your condition is met.</p><div class="copy" style="margin-top:10px">
<input id="bl" readonly value="{e(j.buyer_link(t))}"><button class="btn" onclick="copy('bl')">Copy</button></div></div>""")
        else:
            parts.append(f"""<div class="banner b-info">Authorize {e(money)} with PayPal<p>PayPal holds the money.
The seller is paid only if the item you receive meets the condition below; otherwise the hold is released
and you are not charged.</p><form method="post" action="/t/{t.id}/pay?t={quote(me)}"><input type="hidden" name="terms_hash"
value="{e(rules.terms_hash(t))}"><p class="small muted">By tapping the button you agree that the seller is paid
automatically when this condition is verified.</p><button class="btn" type="submit">Authorize with PayPal</button>
</form></div>""")
            refresh = 0
    elif t.state == State.AUTHORIZED:
        parts.append(f'<div class="banner b-info">PayPal is holding {e(money)}<p>When the item arrives, '
                     'photograph it below. Janus checks the photo and settles automatically.</p></div>')
        if not seller:
            parts.append(_upload_form(t, "Photo of the item you received"))
    elif t.state == State.EVIDENCE_SUBMITTED:
        parts.append(f"""<div class="banner b-info">Photo received, not yet verified<p>Verification did not
finish (see the log). Run it again.</p><button class="btn" onclick="act('/api/t/{t.id}/verify',{{}},
'Janus is checking…')">Verify now</button></div>""")
    elif t.state == State.HELD:
        why = v.reason if v else "PayPal rejected the capture"
        nxt = f"<p><b>To settle it:</b> {e(v.verdict.next_step)}</p>" if v and v.verdict.next_step else ""
        err = f"<p>{e(p.last_error)}</p>" if p.last_error and not v else ""
        parts.append(f'<div class="banner b-hold">On hold: no money has moved<p>{e(v.verdict.summary if v else "")} '
                     f'({e(why)})</p>{nxt}{err}</div>')
        if not seller:
            parts.append(_upload_form(t, "Send a better photo"))
    elif t.state in (State.CAPTURING, State.VOIDING):
        what = "capture" if t.state == State.CAPTURING else "void"
        parts.append(f"""<div class="banner b-hold">Settling with PayPal ({what})<p>{e(p.last_error or 'In progress.')}
Retrying is safe: PayPal sees the same request id and will not act twice.</p>
<button class="btn" onclick="act('/api/t/{t.id}/settle',{{}},'Retrying…')">Retry now</button></div>""")
        refresh = 10
    elif t.state == State.CAPTURED:
        head = {"janus": "Condition met", "buyer": "Released by the buyer"}.get(t.outcome.by if t.outcome else "", "Captured")
        parts.append(f'<div class="banner b-ok">{head} · PayPal captured {e(money)}<p>'
                     f'{e(t.outcome.reason if t.outcome else "")}</p><p>Decided by: {e(t.outcome.by if t.outcome else "")}'
                     f' · Capture ID {e(p.capture_id or "(reconciled)")}</p></div>')
    elif t.state == State.VOIDED:
        head = {"janus": "Condition not met", "seller": "Refunded by the seller"}.get(t.outcome.by if t.outcome else "", "Voided")
        parts.append(f'<div class="banner b-bad">{head} · PayPal released the hold<p>'
                     f'{e(t.outcome.reason if t.outcome else "")}</p><p>The buyer was not charged. Decided by: '
                     f'{e(t.outcome.by if t.outcome else "")} · Authorization {e(p.authorization_id or "")} voided</p></div>')
    elif t.state == State.EXPIRED:
        parts.append(f'<div class="banner b-hold">Authorization expired<p>{e(t.outcome.reason if t.outcome else "")}</p></div>')

    # -- human decisions while funds are held (never available to the other party)
    if t.state in (State.AUTHORIZED, State.HELD, State.EVIDENCE_SUBMITTED):
        if seller:
            parts.append(f"""<div class="card"><h2>Seller</h2><p class="small muted">You can always give up the
payment. You can't capture it yourself: only a verified condition or the buyer can.</p>
<button class="btn ghost" onclick="if(confirm('Release the hold and refund the buyer?'))act('/api/t/{t.id}/refund',
{{}},'Voiding on PayPal…')">Refund the buyer (void)</button></div>""")
        elif t.state == State.HELD:
            parts.append(f"""<div class="card"><h2>Or decide yourself</h2><p class="small muted">If you're happy
with the item, you can release the payment to the seller.</p><button class="btn ghost"
onclick="if(confirm('Pay the seller {e(money)}?'))act('/api/t/{t.id}/release',{{}},'Capturing on PayPal…')">
Release payment to seller</button></div>""")

    # -- verdicts
    for i, rec in enumerate(reversed(t.verdicts)):
        ev = {x.id: x for x in t.evidence}
        photos = "".join(_img(j, t, ev[x].file, f"Buyer photo {x}") for x in rec.evidence_ids[-1:] if x in ev)
        listing_img = _img(j, t, t.listing.photos[0], "Listing photo") if t.listing.photos else ""
        checks = {c.requirement_id: c for c in rec.verdict.checks}
        label = "Janus verdict" if i == 0 else "Earlier verdict"
        parts.append(f"""<div class="card"><h2>{label}</h2><p><b>{e(rec.verdict.verdict.replace('_', ' ').upper())}</b>
· confidence {rec.verdict.confidence:.0%} · decision <b>{e(rec.action.upper())}</b></p>
<p class="small muted">{e(rec.reason)}</p><div class="thumbs">{listing_img}{photos}</div>{_req_list(t, checks)}</div>""")

    # -- listing + condition
    photo = _img(j, t, t.listing.photos[0]) if t.listing.photos else ""
    nv = (f'<p class="small muted">A photo can\'t prove: {e("; ".join(t.compiled.not_verifiable))}</p>'
          if t.compiled.not_verifiable else "")
    parts.append(f"""<div class="card"><h2>The item</h2>{photo}<h1>{e(t.listing.title)}</h1>
<p><b>{e(money)}</b></p><p class="muted">{e(t.listing.description)}</p></div>
<div class="card"><h2>The condition</h2><p>“{e(t.condition)}”</p><p class="small muted">Compiled by Janus:
{e(t.compiled.summary)}</p>{_req_list(t)}{nv}</div>""")

    # -- PayPal record
    if p.order_id:
        honor = ""
        if j.honor_period_over(t) and t.state in HOLDS_FUNDS:
            honor = "<p class='small' style='color:var(--hold)'>The 3-day PayPal honor period has passed; a capture may fail.</p>"
        rows = [("Mode", j.paypal.mode), ("Order", p.order_id), ("Authorization", p.authorization_id),
                ("Status (PayPal)", p.authorization_status), ("Expires", p.expiration_time),
                ("Capture", p.capture_id and f"{p.capture_id} ({p.capture_status})"),
                ("Last checked", p.checked_at)]
        dl = "".join(f"<dt>{e(k)}</dt><dd>{e(str(val))}</dd>" for k, val in rows if val)
        parts.append(f'<div class="card"><h2>PayPal record</h2><dl>{dl}</dl>{honor}</div>')
    if seller:
        parts.append(f'<p class="small muted">Seller view. Keep this link private: <a href="/t/{t.id}{key_q}">'
                     f'seller link</a> · <a href="{e(j.buyer_link(t))}">buyer link</a></p>')
    return page(f"{t.listing.title} · Janus", "".join(parts), refresh, token=me)


def message_page(title: str, msg: str, link: str = "/") -> str:
    return page(title, f'<div class="banner b-info">{e(title)}<p>{e(msg)}</p><a class="btn" href="{e(link)}">Continue</a></div>')


# ---------------------------------------------------------------- HTTP

def make_handler(j: Janus, limiter: Optional[RateLimiter] = None):
    limiter = limiter or RateLimiter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep stderr for janus's own log lines
            pass

        def _send(self, status: int, body, ctype="application/json"):
            if isinstance(body, bytes):
                data = body
            else:
                data = (json.dumps(body, indent=2, default=str) if ctype == "application/json" else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype + ("" if ctype.startswith("image/") else "; charset=utf-8"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, url: str, cookie: str = ""):
            if url.startswith("/") and not url.startswith("//"):
                url = j.config.mount + url
            self.send_response(303)
            self.send_header("Location", url)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _legacy(self, page_html: str) -> str:
            """The threshold pages use root-relative URLs; prefix them when the app is mounted (e.g. /app)."""
            m = j.config.mount
            if not m:
                return page_html
            for a in (' href="/', ' src="/', ' action="/', "post('/", "act('/"):
                page_html = page_html.replace(a, a[:-1] + m + "/")
            return page_html

        def _cookie(self, name: str) -> Optional[str]:
            from http.cookies import SimpleCookie
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie") or "")
            except Exception:
                return None
            return c[name].value if name in c else None

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise JanusError("request too large")
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise JanusError("request body must be JSON")

        def _credential(self, q: dict, body: Optional[dict] = None) -> Optional[str]:
            h = self.headers.get("X-Janus-Token") or ""
            auth = self.headers.get("Authorization") or ""
            if not h and auth.lower().startswith("bearer "):
                h = auth[7:].strip()
            return h or q.get("t") or q.get("k") or None

        def _need(self, tid: str, roles: set[str], cred: Optional[str]) -> tuple[Threshold, str]:
            t = j.get(tid)  # NotFound -> 404
            role = j.role_for(t, cred)
            if role not in roles:
                log_event("security", "denied", tid, path=urlparse(self.path).path, role=role)
                raise NotFound(tid)  # same answer as a missing threshold: nothing to probe
            return t, role

        def _bucket(self, method: str, parts: list[str]) -> str:
            if parts == ["paypal", "webhook"]:
                return "webhook"
            if method == "POST" and (parts in (["api", "thresholds"], ["api", "jobs"])
                                     or parts[-1:] in (["evidence"], ["verify"], ["photo"])):
                return "expensive"  # these call the AI
            return "write" if method == "POST" else "read"

        def _dispatch(self, method: str):
            m = j.config.mount
            if m and (self.path == m or self.path.startswith(m + "/") or self.path.startswith(m + "?")):
                self.path = self.path[len(m):] or "/"
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p]
            api = parts[:1] == ["api"] or parts[:1] == ["paypal"] and parts[1:] == ["webhook"]
            ok, wait = limiter.allow(self.client_address[0], self._bucket(method, parts))
            if not ok:
                log_event("security", "rate_limited", path=url.path, bucket=self._bucket(method, parts))
                self.send_response(429)
                self.send_header("Retry-After", str(max(1, int(wait) + 1)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "too many requests"}')
                return
            try:
                self._route(method, parts, q)
            except NotFound:
                self._err(api, 404, "not found")
            except TransitionError as ex:
                self._err(api, 409, str(ex))
            except (StaleTermsError, OverrideRequired) as ex:
                self._err(api, 409, str(ex))
            except (JanusError, RuleViolation) as ex:
                self._err(api, 400, str(ex))
            except (PayPalError, AIError) as ex:
                self._err(api, 502, str(ex))

        def _err(self, api: bool, status: int, msg: str):
            if api:
                return self._send(status, {"error": msg})
            self._send(status, self._legacy(message_page("Not found" if status == 404 else "Something went wrong", msg)),
                       "text/html")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _view(self, tid: str) -> Threshold:
            t = j.get(tid)
            if t.state in HOLDS_FUNDS and t.state not in (State.CAPTURING, State.VOIDING):
                last = clock.parse(t.payment.checked_at)
                if not last or clock.utcnow() - last > timedelta(seconds=60):
                    try:  # keep the page honest about expiry; a PayPal hiccup shouldn't break the page
                        t = j.refresh(tid)
                    except PayPalError:
                        t = j.get(tid)
            return t

        def _route(self, method, parts, q):
            if method == "GET":
                return self._get(parts, q)
            if parts == ["paypal", "webhook"]:  # authenticated by PayPal's signature
                from .webhooks import handle
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise JanusError("request too large")
                status, out = handle(j, dict(self.headers.items()), self.rfile.read(n))
                return self._send(status, out)
            if len(parts) == 3 and parts[0] == "t" and parts[2] in ("pay", "renew") and ID.fullmatch(parts[1]):
                return self._post_pay(parts[1], renew=parts[2] == "renew")
            b = self._body()
            cred = self._credential({}, b)
            if parts == ["api", "thresholds"]:
                tenant = j.security.tenant_of_key(cred)
                if tenant is None and not j.config.public_create:
                    return self._send(401, {"error": "a tenant key is required to create thresholds"})
                photo = _b64(b["photo_b64"]) if b.get("photo_b64") else None
                t = j.create(b.get("title", ""), str(b.get("price", "")), b.get("condition", ""),
                             b.get("description", ""), b.get("currency") or "USD", [photo] if photo else [],
                             tenant=tenant or "public")
                return self._send(201, {"id": t.id, "buyer_link": j.buyer_link(t),
                                        "seller_link": j.seller_link(t), "threshold": public(t)})
            if parts == ["api", "jobs"]:
                tenant = j.security.tenant_of_key(cred)
                if tenant is None and not j.config.public_create:
                    return self._send(401, {"error": "a tenant key is required to create jobs"})
                t = j.create_job(b.get("title", ""), str(b.get("price", "")), b.get("description", ""),
                                 b.get("currency") or "USD", tenant=tenant or "public")
                return self._send(201, {"id": t.id, "customer_link": j.buyer_link(t),
                                        "provider_link": j.seller_link(t), "job": public(t)})
            if parts[:2] == ["api", "t"] and len(parts) == 4 and ID.fullmatch(parts[2]) and parts[3] in API_ROLES:
                tid, action = parts[2], parts[3]
                t, role = self._need(tid, API_ROLES[action], cred)
                if action == "consent":
                    t = j.consent(tid, channel="api", evidence_from=b.get("evidence_from"))
                elif action == "renew":
                    t = j.start_renewal(tid, channel="api")
                    r = t.payment.renewal
                    return self._send(200, {"approve_url": r.approve_url if r else None,
                                            "order_id": r.order_id if r else None, "state": t.state.value})
                elif action == "payment":
                    t = j.create_payment(tid)
                    return self._send(200, {"buyer_link": j.buyer_link(t), "approve_url": t.payment.approve_url,
                                            "order_id": t.payment.order_id, "state": t.state.value})
                elif action == "evidence":
                    photo = _b64(b["photo_b64"]) if b.get("photo_b64") else None
                    t = (j.submit_and_verify if b.get("verify", True) else j.submit_evidence)(
                        tid, photo, b.get("note", ""), by=role)
                else:
                    t = {
                        "authorize": lambda: j.authorize(tid),
                        "verify": lambda: j.verify(tid),
                        "release": lambda: j.release(tid, b.get("note", ""), channel="api"),
                        "refund": lambda: j.refund(tid, b.get("note", "")),
                        "settle": lambda: j.retry_settlement(tid),
                        "refresh": lambda: j.refresh(tid),
                        "dispute": lambda: j.dispute(tid, b.get("reason", "")),
                        "withdraw_dispute": lambda: j.withdraw_dispute(tid),
                        "accept_amendment": lambda: j.accept_amendment(tid, channel="api"),
                        "reject_amendment": lambda: j.reject_amendment(tid),
                        "allocations": lambda: j.set_allocations(tid, b.get("allocations") or {}),
                        "amend": lambda: j.propose_amendment(tid, b.get("condition"), b.get("price"),
                                                             b.get("reason", "")),
                        "photo": lambda: j.submit_evidence(tid, _b64(b.get("photo_b64") or ""), b.get("note", ""),
                                                           by=role, phase=b.get("phase")),
                        "accept": lambda: j.accept(tid, b.get("override_reason", ""), channel="api"),
                        "partial": lambda: j.partial(tid, str(b.get("amount", "")), b.get("override_reason", ""),
                                                     channel="api"),
                        "reject": lambda: j.reject(tid, b.get("reason", ""), channel="api"),
                    }[action]()
                return self._send(200, public(t))
            raise NotFound("route")

        def _post_pay(self, tid: str, renew: bool = False):
            """The buyer pressed "Authorize with PayPal" (or, after a hold lapsed, "Approve a new hold"): record
            consent for the terms they were shown, then send them to PayPal."""
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                raise JanusError("request too large")
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            cred = self._credential({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})
            t, _ = self._need(tid, {BUYER}, cred)
            if not form.get("terms_hash"):
                raise JanusError("the terms are missing from the request; reload the page")
            if renew:
                t = j.start_renewal(tid, terms_hash=form["terms_hash"], channel="web")
                url = t.payment.renewal.approve_url if t.payment.renewal else None
                if not url:
                    raise PayPalError("PayPal did not return an approval link for the new hold. Try again in a moment.")
                return self._redirect(url, cookie=self._return_cookie(tid, cred))
            if t.state == State.CREATED:
                t = j.consent(tid, channel="web", terms_hash=form["terms_hash"])
            t = j.create_payment(tid)
            if t.state != State.PAYMENT_PENDING:  # already authorized, or decided: nothing to approve
                return self._redirect(j.buyer_link(t))
            if not t.payment.approve_url:
                raise PayPalError("PayPal did not return an approval link for this order. Try again in a moment.")
            return self._redirect(t.payment.approve_url, cookie=self._return_cookie(tid, cred))

        def _return_cookie(self, tid: str, cred: str) -> str:
            secure = "; Secure" if j.config.base_url.startswith("https") else ""
            return f"janus_{tid}={cred}; Path={j.config.mount}/paypal/return; Max-Age=3600; HttpOnly; SameSite=Lax{secure}"

        def _get(self, parts, q):
            if not parts:
                return self._send(200, web_jobs.home(j), "text/html")
            if parts == ["threshold"]:
                return self._send(200, self._legacy(landing(j)), "text/html")
            if parts == ["cron", "tick"]:
                # Vercel Cron sends "Authorization: Bearer <CRON_SECRET>". Without a configured secret, refuse.
                auth = self.headers.get("Authorization") or ""
                import hmac as _h
                if not j.config.cron_secret or not _h.compare_digest(auth, f"Bearer {j.config.cron_secret}"):
                    raise NotFound("route")
                from .scheduler import tick
                return self._send(200, {"actions": tick(j)})
            if parts == ["logo.svg"]:
                return self._send(200, (STATIC / "logo.svg").read_bytes(), "image/svg+xml")
            if parts == ["healthz"]:
                return self._send(200, {"ok": True})
            cred = self._credential(q)
            if parts == ["paypal", "return"]:
                # No credential travels through PayPal. Authorizing an order the buyer approved on PayPal is
                # idempotent and cannot move money, so it needs no token. The buyer's own token comes back in
                # the HttpOnly cookie set when they tapped "Authorize", and takes them back to their page.
                t = j.store.find_by_order(q.get("token", ""))
                try:
                    j.authorize(t.id, order_id=q.get("token"))
                except JanusError as ex:
                    return self._send(200, self._legacy(message_page("Not authorized yet", str(ex), "/")), "text/html")
                bt = self._cookie(f"janus_{t.id}")
                if j.role_for(t, bt) == BUYER:
                    return self._redirect(f"/t/{t.id}?t={quote(bt)}")
                return self._send(200, self._legacy(message_page(
                    "Payment authorized", "PayPal is holding the money. Go back to the link the provider sent you "
                    "to continue.", "/")), "text/html")
            if len(parts) >= 2 and parts[0] == "t" and ID.fullmatch(parts[1]):
                tid = parts[1]
                if len(parts) == 5 and parts[2] == "files" and parts[3] in ("listing", "evidence"):
                    path = f"/t/{tid}/files/{parts[3]}/{quote(parts[4])}"
                    if not j.security.check_signed(path, q.get("exp"), q.get("sig")):
                        raise NotFound(tid)
                    name = parts[4]
                    if "/" in name or name.startswith(".") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
                        raise NotFound(tid)
                    data = j.store.get_file(tid, f"{parts[3]}/{name}")
                    if data is None:
                        raise NotFound(tid)
                    ext = name.rsplit(".", 1)[-1].lower()
                    ctype = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                    return self._send(200, data, ctype)
                t, role = self._need(tid, {BUYER, SELLER}, cred)
                if len(parts) == 2:
                    t = self._view(tid)
                    if t.kind == "job":
                        return self._send(200, web_jobs.job_page(j, t, role), "text/html")
                    return self._send(200, self._legacy(threshold_page(j, t, role == SELLER)), "text/html")
                if parts[2:] == ["pay"] and role == BUYER:
                    # A GET only shows the terms. Link previews and prefetchers issue GETs, so a GET must never
                    # record consent or create an order: that is the POST below.
                    return self._redirect(j.buyer_link(t))
                raise NotFound(tid)
            if parts[:2] == ["api", "t"] and len(parts) in (3, 4, 5) and ID.fullmatch(parts[2]):
                t, _ = self._need(parts[2], READ_ROLES, cred)
                if len(parts) == 3:
                    return self._send(200, public(t))
                if parts[3:] == ["packet"]:
                    from .packet import build
                    return self._send(200, build(t), "text/markdown")
                if parts[3:] == ["audit"]:
                    after = int(q.get("after", "0") or 0) if (q.get("after") or "0").isdigit() else 0
                    entries = j.audit.entries(t.id)
                    ok, msg = j.audit.verify(t.id)
                    return self._send(200, {"threshold_id": t.id, "verified": ok, "message": msg,
                                            "head": entries[-1]["hash"] if entries else None,
                                            "count": len(entries), "entries": entries[after:]})
                if parts[3:] == ["audit", "verify"]:
                    entries = j.audit.entries(t.id)
                    ok, msg = j.audit.verify(t.id)
                    return self._send(200, {"threshold_id": t.id, "verified": ok, "message": msg,
                                            "count": len(entries), "head": entries[-1]["hash"] if entries else None,
                                            "algorithm": "sha256 over the canonical JSON of each entry without "
                                                         "'hash'; 'prev' is the previous entry's hash"})
                raise NotFound(t.id)
            if parts == ["api", "thresholds"]:
                tenant = j.security.tenant_of_key(cred)
                if tenant is None and not j.security.is_admin(cred):
                    return self._send(401, {"error": "a tenant key or admin token is required"})
                return self._send(200, [{"id": t.id, "state": t.state.value, "title": t.listing.title,
                                         "amount": t.amount, "currency": t.currency, "tenant": t.tenant}
                                        for t in j.store.all() if tenant is None or t.tenant == tenant])
            raise NotFound("route")

    return Handler


def serve(j: Janus, host: str = "127.0.0.1", port: int = 8000) -> None:
    import os

    from .scheduler import start_background, tick
    tick(j)  # recover settlements interrupted by a crash before taking traffic
    start_background(j, float(os.environ.get("JANUS_TICK_SECONDS", "300")))
    httpd = ThreadingHTTPServer((host, port), make_handler(j))
    print(f"Janus Threshold on http://{host}:{port}  (paypal={j.paypal.mode}, ai={j.ai.mode}, "
          f"public URL={j.config.base_url})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
