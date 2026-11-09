"""Job-mode pages: one mobile-first page per job that both sides open by link, plus the provider's create page.

Styled with the landing page's tokens (cream/ink, Figtree + Instrument Serif, light and dark), and it reads
the same `janus-theme` choice from localStorage, so the app and the landing page look like one site.

Who sees what is decided by the token in the link: `?k=` is the provider, `?t=` is the customer.
"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING
from urllib.parse import quote

from . import rules
from .state import DECIDABLE, State

if TYPE_CHECKING:
    from .service import Janus
    from .store import Threshold

e = html.escape

CSS = """
:root{--bg:#F3F0E8;--bg-elev:#FFFCF6;--ink:#161513;--muted:#5E5952;--line:rgba(22,21,19,.12);--btn:#161513;
--btn-ink:#F3F0E8;--ok:#1d6b3a;--okbg:rgba(29,107,58,.10);--bad:#a3271b;--badbg:rgba(163,39,27,.09);
--warn:#8a5a00;--warnbg:rgba(138,90,0,.10);--shadow:0 24px 60px rgba(22,21,19,.12)}
[data-theme=dark]{--bg:#0C0C0B;--bg-elev:#161614;--ink:#F3F0E8;--muted:#A8A299;--line:rgba(243,240,232,.14);
--btn:#F3F0E8;--btn-ink:#0C0C0B;--ok:#6fd39a;--okbg:rgba(111,211,154,.12);--bad:#ff8f80;--badbg:rgba(255,143,128,.12);
--warn:#f0c25c;--warnbg:rgba(240,194,92,.12);--shadow:0 24px 60px rgba(0,0,0,.4)}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:16px/1.55 Figtree,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:620px;margin:0 auto;padding:18px 16px 64px}
.top{display:flex;align-items:center;justify-content:space-between;margin:0 0 22px}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;font-weight:600}
.brand svg{width:28px;height:28px}.theme{background:none;border:1px solid var(--line);color:var(--ink);
border-radius:999px;padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}
h1{font:400 38px/1.08 "Instrument Serif",Georgia,serif;margin:6px 0 10px;letter-spacing:-.3px}
h1 em{font-style:italic}h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
color:var(--muted);margin:0 0 12px}.lead{color:var(--muted);margin:0 0 20px}
.card{background:var(--bg-elev);border:1px solid var(--line);border-radius:18px;padding:18px;margin:0 0 14px}
label{display:block;font-weight:600;font-size:14px;margin:14px 0 6px}
input,textarea{width:100%;font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);
border-radius:12px;padding:12px}textarea{min-height:96px;resize:vertical}
.btn{display:block;width:100%;border:0;border-radius:999px;padding:15px 18px;margin-top:12px;font:600 16px/1 Figtree,
system-ui,sans-serif;background:var(--btn);color:var(--btn-ink);cursor:pointer;text-align:center;text-decoration:none}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}.btn[disabled]{opacity:.45}
.row{display:flex;gap:10px}.row>*{flex:1}.muted{color:var(--muted)}.small{font-size:13px}
.state{display:inline-block;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
border:1px solid var(--line);border-radius:999px;padding:4px 10px}
.banner{border-radius:16px;padding:16px 18px;margin:0 0 14px;font-weight:600}.banner p{font-weight:400;margin:6px 0 0}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}
.warn{background:var(--warnbg);color:var(--warn)}.info{background:var(--bg-elev);border:1px solid var(--line)}
ul.items{list-style:none;margin:0;padding:0}ul.items li{padding:12px 0;border-top:1px solid var(--line)}
ul.items li:first-child{border-top:0}.chip{display:inline-block;font-size:12px;font-weight:700;border-radius:8px;
padding:2px 8px;margin-right:8px}.c-met{background:var(--okbg);color:var(--ok)}
.c-not_met{background:var(--badbg);color:var(--bad)}.c-cannot_tell{background:var(--warnbg);color:var(--warn)}
.c-todo{border:1px solid var(--line);color:var(--muted)}
.photos{display:grid;grid-template-columns:1fr 1fr;gap:10px}.photos figure{margin:0}
.photos img{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:12px;background:var(--line);display:block}
.photos figcaption{font-size:12px;color:var(--muted);margin-top:4px}
dl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:0;font-size:14px}dt{color:var(--muted)}
dd{margin:0;overflow-wrap:anywhere;font-family:ui-monospace,Menlo,monospace;font-size:13px}
.copy{display:flex;gap:8px}.copy input{font-size:13px}.copy .btn{width:auto;margin:0;padding:12px 16px}
ol.audit{list-style:none;margin:0;padding:0;font-size:13px;max-height:360px;overflow:auto}
ol.audit li{padding:8px 0;border-top:1px solid var(--line);display:grid;grid-template-columns:2.4em 1fr;gap:2px 8px}
ol.audit .h{font-family:ui-monospace,Menlo,monospace;color:var(--muted);grid-column:2;font-size:11px}
.flag{border-left:3px solid var(--warn);padding:6px 0 6px 12px;margin:8px 0}
#busy{display:none}.err{color:var(--bad);font-weight:600;margin-top:10px}
"""

THEME = """(function(){try{var t=localStorage.getItem("janus-theme");if(!t)t=matchMedia("(prefers-color-scheme: dark)")
.matches?"dark":"light";document.documentElement.setAttribute("data-theme",t)}catch(e){}})();"""

JS = """
const B=document.body.dataset.base||'',TK=document.body.dataset.token||'',JID=document.body.dataset.job||'';
function toggleTheme(){const r=document.documentElement,t=r.getAttribute('data-theme')==='dark'?'light':'dark';
  r.setAttribute('data-theme',t);try{localStorage.setItem('janus-theme',t)}catch(e){}}
async function api(path,body,method){const h={'Content-Type':'application/json'};if(TK)h['X-Janus-Token']=TK;
  const r=await fetch(B+path,{method:method||(body===undefined?'GET':'POST'),headers:h,body:body===undefined?undefined:JSON.stringify(body)});
  const d=await r.json().catch(()=>({error:'bad response ('+r.status+')'}));if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d}
function busy(on,msg){const b=document.getElementById('busy');if(b){b.style.display=on?'block':'none';
  if(msg)b.querySelector('.msg').textContent=msg}document.querySelectorAll('button').forEach(x=>x.disabled=on)}
function fail(err,id){busy(false);const el=document.getElementById(id||'err');if(el){el.textContent=err.message||err}else alert(err.message||err)}
async function photoData(file){ // send the original (keeps EXIF) when small enough for a serverless request
  if(file.size<=3*1024*1024){return await new Promise((ok,no)=>{const r=new FileReader();r.onload=()=>ok(r.result);r.onerror=no;r.readAsDataURL(file)})}
  const url=URL.createObjectURL(file),img=new Image();await new Promise((ok,no)=>{img.onload=ok;img.onerror=no;img.src=url});
  const s=Math.min(1,1600/Math.max(img.naturalWidth,img.naturalHeight)),c=document.createElement('canvas');
  c.width=Math.round(img.naturalWidth*s);c.height=Math.round(img.naturalHeight*s);c.getContext('2d').drawImage(img,0,0,c.width,c.height);
  URL.revokeObjectURL(url);return c.toDataURL('image/jpeg',0.86)}
function copy(id){const i=document.getElementById(id);i.select();navigator.clipboard&&navigator.clipboard.writeText(i.value)}
async function act(path,body,msg,errId){try{busy(true,msg);await api(path,body);location.reload()}catch(e){fail(e,errId)}}
async function upload(phase){const f=document.getElementById('f-'+phase).files[0];if(!f){fail(new Error('Choose a photo first'),'err-photo');return}
  try{busy(true,'Uploading the '+phase+' photo…');await api('/api/t/'+JID+'/photo',{phase:phase,photo_b64:await photoData(f),
    note:(document.getElementById('n-'+phase)||{}).value||''});location.reload()}catch(e){fail(e,'err-photo')}}
function overrideReason(){const c=document.getElementById('ov');if(!c)return '';
  return c.checked?(document.getElementById('ov-why').value||'').trim():''}
async function decide(kind){const body={override_reason:overrideReason()};
  if(kind==='partial'){body.amount=document.getElementById('amt').value}
  if(kind==='reject'){if(!confirm('Reject the work and release the hold? You will not be charged.'))return;
    body.reason=(document.getElementById('why')||{}).value||''}
  if(kind==='accept'&&!confirm('Pay the full amount to the provider?'))return;
  await act('/api/t/'+JID+'/'+kind,body,'Telling PayPal…','err-decide')}
let seen=0,state=document.body.dataset.state,leaving=false;
// A form submit or navigation in flight must never be cancelled by the live refresh below: the first tap on the
// approve button saves the order a moment before the redirect to PayPal reaches the browser.
document.addEventListener('submit',e=>{leaving=true;const b=e.target.querySelector('button[type=submit]');
  if(b){b.disabled=true;b.textContent='Opening PayPal…'}});
addEventListener('pagehide',()=>{leaving=true});
addEventListener('pageshow',e=>{if(e.persisted){leaving=false;document.querySelectorAll('form button[type=submit]').forEach(b=>{b.disabled=false})}});
function row(x){const li=document.createElement('li');const s=document.createElement('b');s.textContent=x.seq;
  const t=document.createElement('span');t.textContent=x.ts.slice(11,19)+'  '+x.kind+'  ('+x.actor+')  '+(x.reason||'');
  const h=document.createElement('span');h.className='h';h.textContent='prev '+x.prev.slice(0,12)+'…  hash '+x.hash.slice(0,12)+'…';
  li.append(s,t,h);return li}
async function poll(){if(!JID||!TK||leaving)return;try{const d=await api('/api/t/'+JID+'/audit?after='+seen);const ol=document.getElementById('audit');
  if(ol){d.entries.forEach(x=>ol.prepend(row(x)))}seen=d.count;const v=document.getElementById('chain');
  if(v)v.textContent=(d.verified?'Chain intact':'CHAIN BROKEN')+' · '+d.count+' entries · head '+(d.head||'').slice(0,16)+'…';
  if(leaving)return;const s=await api('/api/t/'+JID);if(s.state!==state&&!leaving){location.reload()}}catch(e){}}
async function verifyChain(){try{const v=await api('/api/t/'+JID+'/audit/verify');
  document.getElementById('chain').textContent=(v.verified?'Verified: ':'FAILED: ')+v.message+' · head '+(v.head||'').slice(0,16)+'…'}catch(e){fail(e)}}
if(JID&&TK){poll();setInterval(poll,4000)}
"""

FLAG_TEXT = {
    "reused_photo": "This photo, or a near copy, was already used on another job.",
    "after_matches_before": "The after photo looks the same as the before photo: no visible change.",
    "edited_software": "The photo file says it was saved by an editing app.",
    "likely_edited": "The AI sees signs that a photo was edited.",
    "possible_edit": "The AI sees possible signs that a photo was edited.",
    "wrong_room": "The after photo does not look like the same place as the before photo.",
    "after_taken_before_authorization": "The after photo was taken before you approved the payment.",
    "after_older_than_before": "The after photo was taken before the before photo.",
    "taken_in_future": "A photo has a capture time in the future.",
    "no_before_photo": "There is no before photo to compare with.",
    "no_after_photo": "There is no after photo yet.",
    "no_ai_packet": "The AI was unavailable, so nothing was checked automatically.",
    "photo_quality": "The after photo is too dark, blurry or small to judge.",
    "instruction_like_note": "A note contains instruction-like text, which is suspicious.",
}


def flag_text(flag: str) -> str:
    return FLAG_TEXT.get(flag.split(":")[0], flag)


def shell(j: "Janus", title: str, body: str, token: str = "", job: str = "", state: str = "") -> str:
    logo = (__import__("pathlib").Path(__file__).parent / "static" / "logo.svg").read_text()
    base = j.config.mount
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#F3F0E8"><title>{e(title)}</title>
<link rel="icon" href="{base}/logo.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script>{THEME}</script><style>{CSS}</style></head>
<body data-base="{e(base)}" data-token="{e(token)}" data-job="{e(job)}" data-state="{e(state)}"><main>
<div class="top"><a class="brand" href="/">{logo}<span>Janus</span></a>
<button class="theme" onclick="toggleTheme()" aria-label="Switch light or dark">Theme</button></div>
{body}
<div id="busy" class="banner info"><span class="msg">Working…</span></div>
</main><script>{JS}</script></body></html>"""


def home(j: "Janus") -> str:
    mode = f"PayPal {j.paypal.mode} · AI {j.ai.mode} · storage {j.kv.name}"
    return shell(j, "Janus: new job", f"""
<h1>Get paid when the work is <em>accepted</em>.</h1>
<p class="lead">Describe the job. Janus writes the checklist. Your customer approves a PayPal hold, so the money is
there before you start. You upload before and after photos; Janus checks them and your customer decides.</p>
<form class="card" onsubmit="createJob(event)">
<h2>New job</h2>
<label for="title">Job</label><input id="title" required maxlength="120" placeholder="Patch a hole in the living room wall">
<label for="desc">What you will do</label>
<textarea id="desc" required maxlength="2000" placeholder="Patch the hole by the door. Sand it smooth. Paint the patch to match the wall."></textarea>
<div class="row"><div><label for="price">Price</label><input id="price" required inputmode="decimal" placeholder="120.00"></div>
<div><label for="cur">Currency</label><input id="cur" value="USD" maxlength="3"></div></div>
<button class="btn" type="submit">Write the checklist</button><div id="err" class="err"></div>
<p class="small muted">{e(mode)}</p></form>
<script>async function createJob(ev){{ev.preventDefault();const g=id=>document.getElementById(id).value;
 try{{busy(true,'Writing the checklist…');const d=await api('/api/jobs',{{title:g('title'),description:g('desc'),
 price:g('price'),currency:g('cur')}});location.href=d.provider_link}}catch(e){{fail(e)}}}}</script>""")


def _items(t: "Threshold") -> str:
    v = t.last_verdict
    checks = {c.requirement_id: c for c in v.verdict.checks} if v else {}
    out = []
    for r in t.compiled.requirements:
        c = checks.get(r.id)
        if c:
            label = {"met": "pass", "not_met": "fail", "cannot_tell": "unclear"}[c.result]
            conf = c.confidence if c.confidence is not None else v.verdict.confidence
            detail = f'<div class="small muted">{e(c.observation.splitlines()[0][:200])} · confidence {conf:.0%}</div>'
            chip = f'<span class="chip c-{c.result}">{label}</span>'
        else:
            chip, detail = '<span class="chip c-todo">to check</span>', f'<div class="small muted">{e(r.check)}</div>'
        out.append(f"<li>{chip}{e(r.statement)}{detail}</li>")
    nv = "".join(f"<li class='small muted'>Not provable by photo: {e(x)}</li>" for x in t.compiled.not_verifiable)
    return f'<ul class="items">{"".join(out)}{nv}</ul>'


def _photos(j: "Janus", t: "Threshold") -> str:
    figs = []
    for ev_ in t.evidence:
        if ev_.file and ev_.phase:
            src = j.config.mount + j.security.sign_path(f"/t/{t.id}/files/{quote(ev_.file)}")
            flags = ", ".join(flag_text(f) for f in ev_.flags if not f.startswith("matches_listing"))
            figs.append(f'<figure><img src="{e(src)}" alt="{e(ev_.phase)} photo {e(ev_.id)}" loading="lazy">'
                        f'<figcaption>{e(ev_.phase.title())} · {e(ev_.id)}{" · " + e(flags) if flags else ""}</figcaption></figure>')
    return f'<div class="photos">{"".join(figs)}</div>' if figs else '<p class="muted small">No photos yet.</p>'


def _renew_card(j: "Janus", t: "Threshold", me: str, money: str, why: str, closable: bool = False) -> str:
    """The customer's "Approve a new hold" action: same job, checklist and photos; a new PayPal order."""
    close = ""
    if closable:
        close = """<label for="why">Or close the job</label><input id="why" maxlength="500" placeholder="Why? (optional)">
<button class="btn ghost" onclick="decide('reject')">Reject · close the job</button><div id="err-decide" class="err"></div>"""
    return f"""<div class="card"><h2>Approve a new hold</h2><p>{e(why)} To keep this job going, approve a new PayPal hold
for {e(money)}. The checklist and photos stay the same. You still decide at the end.</p>
<form method="post" action="{j.config.mount}/t/{t.id}/renew?t={quote(me)}"><input type="hidden" name="terms_hash"
value="{e(rules.terms_hash(t))}"><button class="btn" type="submit">Approve a new hold</button></form>{close}</div>"""


def job_page(j: "Janus", t: "Threshold", role: str) -> str:
    money = f"{t.amount} {t.currency}"
    me = j.token(t, "seller" if role == "seller" else "buyer")
    p, v = t.payment, t.last_verdict
    parts = [f'<span class="state">{e(t.state.value.replace("_", " "))}</span>',
             f'<h1>{e(t.listing.title)}</h1><p class="lead">{e(money)} · '
             f'{"You are the provider." if role == "seller" else "You are the customer."}</p>']

    # outcome / next step
    if t.state == State.CAPTURED:
        parts.append(f'<div class="banner ok">Paid · PayPal captured {e(money)}<p>{e(t.outcome.reason if t.outcome else "")}</p></div>')
    elif t.state == State.PARTIALLY_CAPTURED:
        parts.append(f'<div class="banner ok">Partly paid<p>{e(t.outcome.reason)}</p></div>')
    elif t.state == State.VOIDED:
        parts.append(f'<div class="banner bad">Hold released · nothing was charged<p>{e(t.outcome.reason if t.outcome else "")}</p></div>')
    elif t.state == State.EXPIRED:
        parts.append('<div class="banner warn">The PayPal hold expired · nothing was charged</div>')
    elif t.state == State.HOLD_LAPSED:
        if role == "seller":
            parts.append('<div class="banner warn">The PayPal hold lapsed · nothing is held<p>Your customer has not '
                         'decided yet. They have been asked to approve a new hold for this job. The checklist and '
                         'photos stay as they are.</p></div>')
        else:
            parts.append(_renew_card(j, t, me, money, "The PayPal hold lapsed before you decided, so nothing is held "
                                     "now and nothing was charged.", closable=True))
    elif t.state in (State.CAPTURING, State.VOIDING):
        parts.append(f'<div class="banner warn">Talking to PayPal…<p>{e(p.last_error or "")}</p></div>')
    elif t.state in (State.CREATED, State.PAYMENT_PENDING):
        if role == "seller":
            parts.append(f"""<div class="card"><h2>Send this to your customer</h2><p class="small muted">They approve a PayPal
hold for {e(money)}. Nothing is charged until they accept the work.</p><div class="copy"><input id="cl" readonly
value="{e(j.buyer_link(t))}"><button class="btn" onclick="copy('cl')">Copy</button></div></div>""")
        else:
            parts.append(f"""<div class="card"><h2>Approve a hold</h2><p>PayPal holds {e(money)} on your account. The provider
is paid only when you accept the work. You can accept all of it, part of it, or reject it.</p>
<form method="post" action="{j.config.mount}/t/{t.id}/pay?t={quote(me)}"><input type="hidden" name="terms_hash"
value="{e(rules.terms_hash(t))}"><p class="small muted">By tapping the button you agree to these terms and the checklist
below.</p><button class="btn" type="submit">Approve PayPal hold</button></form></div>""")
    elif t.state in DECIDABLE:
        if role == "seller":
            nxt = ("The packet is ready. Your customer decides." if v else
                   "Upload before and after photos, then build the acceptance packet.")
        else:
            nxt = ("Read the packet and look at the photos, then decide below." if v else
                   "When the provider finishes, you will see the acceptance packet here.")
        parts.append(f'<div class="banner info">PayPal is holding {e(money)}<p>{e(nxt)}</p></div>')
        if p.needs_new_authorization:
            if role == "seller":
                parts.append('<div class="banner warn">PayPal could not extend the hold<p>Your customer has been asked '
                             'to approve a new hold. Until then the current hold stays open.</p></div>')
            else:
                parts.append(_renew_card(j, t, me, money, "PayPal could not extend the current hold. It stays open "
                                         "for now, but it will lapse before long."))

    # the packet
    if v:
        flags = "".join(f'<div class="flag">{e(flag_text(f))}</div>' for f in v.blocking_flags)
        rec = {"recommend_accept": "Janus found every checklist item done.",
               "recommend_reject": "Janus found checklist items not done.",
               "review": "Janus could not confirm the work. Look at the photos yourself."}.get(v.action, "")
        conf = f"{v.verdict.confidence:.0%}"
        parts.append(f"""<div class="card"><h2>Acceptance packet</h2><p><b>{e(rec)}</b> <span class="muted small">
Overall confidence {conf}. Janus does not move money; {"your customer decides" if role == "seller" else "you decide"}.</span></p>
{('<div class="banner warn">Flagged<p>Paying needs your explicit override.</p>' + flags + "</div>") if v.blocking_flags else ""}
{_items(t)}<p class="small muted">{e(v.verdict.summary)}</p></div>""")
    else:
        parts.append(f'<div class="card"><h2>Checklist</h2>{_items(t)}</div>')

    parts.append(f'<div class="card"><h2>Photos</h2>{_photos(j, t)}</div>')

    # provider actions
    if role == "seller" and t.state not in (State.CAPTURED, State.VOIDED, State.EXPIRED, State.CAPTURING, State.VOIDING,
                                            State.PARTIALLY_CAPTURED, State.HOLD_LAPSED):
        can_build = any(x.phase == "after" for x in t.evidence) and t.state == State.EVIDENCE_SUBMITTED
        parts.append(f"""<div class="card"><h2>Your photos</h2><p class="small muted">{e(t.compiled.evidence_instructions)}</p>
<label for="f-before">Before</label><input id="f-before" type="file" accept="image/*" capture="environment">
<button class="btn ghost" onclick="upload('before')">Upload before photo</button>
<label for="f-after">After</label><input id="f-after" type="file" accept="image/*" capture="environment">
<input id="n-after" placeholder="Note for the customer (optional)" maxlength="500" style="margin-top:8px">
<button class="btn ghost" onclick="upload('after')">Upload after photo</button><div id="err-photo" class="err"></div>
{f'<button class="btn" onclick="act(&#39;/api/t/{t.id}/verify&#39;,{{}},&#39;Checking the photos…&#39;,&#39;err-photo&#39;)">Build acceptance packet</button>' if can_build else ""}
</div>""")

    # customer decision
    if role == "buyer" and t.state in DECIDABLE and p.authorization_id:
        override = ""
        if v and v.blocking_flags:
            override = """<div class="flag"><label><input type="checkbox" id="ov" style="width:auto;margin-right:8px">
I checked the work myself and want to pay despite the flags.</label><input id="ov-why" maxlength="500"
placeholder="Why? (recorded in the audit trail)"></div>"""
        parts.append(f"""<div class="card"><h2>Your decision</h2>{override}
<button class="btn" onclick="decide('accept')">Accept · pay {e(money)}</button>
<label for="amt">Or pay part</label><div class="row"><input id="amt" inputmode="decimal" placeholder="Amount">
<button class="btn ghost" style="margin-top:0" onclick="decide('partial')">Pay part, release the rest</button></div>
<label for="why">Or reject</label><input id="why" maxlength="500" placeholder="What is wrong? (optional)">
<button class="btn ghost" onclick="decide('reject')">Reject · release the hold</button>
<div id="err-decide" class="err"></div></div>""")

    # PayPal record
    if p.order_id:
        rows = [("Mode", j.paypal.mode), ("Order", p.order_id), ("Authorization", p.authorization_id),
                ("Status", p.authorization_status), ("Expires", p.expiration_time)]
        rows += [("Earlier orders", ", ".join(p.previous_order_ids)),
                 ("Earlier authorizations", ", ".join(p.previous_authorization_ids))]
        rows += [(f"Capture {i}", f"{c.capture_id} · {c.amount} · {'final' if c.final else 'partial'}")
                 for i, c in enumerate(p.captures, 1)]
        dl = "".join(f"<dt>{e(k)}</dt><dd>{e(str(x))}</dd>" for k, x in rows if x)
        parts.append(f'<div class="card"><h2>PayPal record</h2><dl>{dl}</dl></div>')

    # live audit trail
    parts.append("""<div class="card"><h2>Audit trail</h2><p class="small muted">Every PayPal call, rule check, AI output
and decision, each linked to the previous one by its hash. Updates live.</p><p class="small" id="chain">Loading…</p>
<button class="btn ghost" onclick="verifyChain()">Verify chain</button><ol class="audit" id="audit"></ol></div>""")
    if role == "seller":
        parts.append(f'<p class="small muted">Provider view. Customer link: <a href="{e(j.buyer_link(t))}">open</a></p>')
    return shell(j, f"{t.listing.title} · Janus", "".join(parts), token=me, job=t.id, state=t.state.value)
