"""Pages around the job: My jobs (with activity), Profile and Help. Same shell and styles as web_jobs.

Janus has no user accounts: a job is reached by its secret link, and the link's token is the credential. "My jobs"
is the set of jobs this browser holds a valid token for. Opening a job link adds that job's token to an HttpOnly
cookie (the wallet); every listed job is re-checked against its token on each request, so a browser only ever
sees jobs whose link it was given.
"""
from __future__ import annotations

import html
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable, NamedTuple, Optional

from .state import HOLDS_FUNDS, TERMINAL, State
from .store import NotFound

if TYPE_CHECKING:
    from .service import Janus
    from .store import Threshold

e = html.escape
WALLET = "janus_jobs"
WALLET_MAX = 30


class Held(NamedTuple):
    t: "Threshold"
    role: str      # "buyer" (customer) or "seller" (provider)
    token: str


# --- the wallet cookie: "tid~token|tid~token", newest first ---

def wallet_items(raw: Optional[str]) -> list[tuple[str, str]]:
    out = []
    for part in (raw or "").split("|")[:WALLET_MAX]:
        tid, _, token = part.partition("~")
        if tid and token:
            out.append((tid, token))
    return out


def wallet_add(raw: Optional[str], tid: str, token: str) -> str:
    items = [(i, k) for i, k in wallet_items(raw) if not (i == tid and k == token)]
    return "|".join(f"{i}~{k}" for i, k in [(tid, token)] + items[:WALLET_MAX - 1])


def held_jobs(j: "Janus", raw: Optional[str]) -> list[Held]:
    """Jobs this browser may see: each token is verified again; unknown jobs and bad tokens are dropped."""
    out = []
    for tid, token in wallet_items(raw):
        try:
            t = j.get(tid)
        except NotFound:
            continue
        role = j.role_for(t, token)
        if t.kind == "job" and role in ("buyer", "seller") and token == j.token(t, role):
            out.append(Held(t, role, token))
    return out


# --- money and status, from the job's own fields ---

def captured(t: "Threshold") -> Decimal:
    return sum((Decimal(c.amount) for c in t.payment.captures), Decimal("0"))


def held_now(t: "Threshold") -> Decimal:
    """What PayPal is holding right now for this job."""
    if t.state in HOLDS_FUNDS and t.payment.authorization_id:
        return Decimal(t.payment.authorized_amount or t.amount) - captured(t)
    return Decimal("0")


def released(t: "Threshold") -> Decimal:
    """What was voided back to the customer when the job closed."""
    if t.state in (State.VOIDED, State.PARTIALLY_CAPTURED) and t.payment.authorized_amount:
        return Decimal(t.payment.authorized_amount) - captured(t)
    return Decimal("0")


def badge(t: "Threshold") -> tuple[str, str]:
    """(label, tone) for a job's current state. Tone is one of the shell's banner classes."""
    s = t.state
    if s == State.VOIDED and t.outcome and t.outcome.by == "buyer":
        return "Rejected · released", "bad"
    return {
        State.CREATED: ("Hold not approved yet", "warn"),
        State.PAYMENT_PENDING: ("Hold not approved yet", "warn"),
        State.AUTHORIZED: ("PayPal holding", "info"),
        State.EVIDENCE_SUBMITTED: ("Photos in", "info"),
        State.PACKET_READY: ("Packet ready", "info"),
        State.MANUAL_REVIEW: ("Flagged for review", "warn"),
        State.HELD: ("On hold", "warn"),
        State.DISPUTED: ("Disputed", "warn"),
        State.HOLD_LAPSED: ("Hold lapsed", "warn"),
        State.CAPTURING: ("Talking to PayPal", "warn"),
        State.VOIDING: ("Talking to PayPal", "warn"),
        State.CAPTURED: ("Paid", "ok"),
        State.PARTIALLY_CAPTURED: ("Partly paid", "ok"),
        State.VOIDED: ("Released", "bad"),
        State.EXPIRED: ("Hold expired", "bad"),
    }[s]


def link(j: "Janus", h: Held) -> str:
    return f"{j.config.mount}/t/{h.t.id}?{'t' if h.role == 'buyer' else 'k'}={h.token}"


# --- the job's status timeline, from its audit trail ---

FINAL = {State.CAPTURED: "Captured", State.PARTIALLY_CAPTURED: "Partially captured, rest voided",
         State.VOIDED: "Voided", State.EXPIRED: "Hold expired"}


def timeline(t: "Threshold", entries: list[dict]) -> list[tuple[str, Optional[str], str]]:
    """[(label, timestamp or None, "done" | "now" | "todo")]: Created > Authorized > Evidence > Packet > Decision >
    outcome. A lapsed hold puts the job back at the authorization step."""
    def last(*kinds: str, **match) -> Optional[str]:
        hits = [x["ts"] for x in entries if x.get("kind") in kinds
                and all((x.get("data") or {}).get(k) == v for k, v in match.items())]
        return hits[-1] if hits else None
    lapsed = t.state == State.HOLD_LAPSED
    steps = [("Created", last("job_created")),
             ("Hold lapsed: approve a new hold" if lapsed else "Authorized (held)",
              None if lapsed else last("authorized", "renewed")),
             ("Evidence submitted", last("evidence_added", phase="after")),
             ("AI packet ready", last("packet")),
             ("Decision", last("customer_accepted", "customer_partial", "customer_rejected")),
             (FINAL.get(t.state, "Captured / partially captured / voided"),
              last(t.state.value) if t.state in FINAL else None)]
    if t.state in FINAL:  # the job ended (a decision, or PayPal or the scheduler closed it): every step is history
        return [(lbl, ts, "done") for lbl, ts in steps]
    out, current = [], False
    for lbl, ts in steps:
        if ts and not current and not (lapsed and lbl.startswith("Hold lapsed")):
            out.append((lbl, ts, "done"))
        else:
            out.append((lbl, None, "todo" if current else "now"))
            current = True
    return out


# --- activity: read-only, from the audit trail ---

def describe(entry: dict, role: str) -> Optional[tuple[str, str]]:
    """(text, tone) for an audit entry worth showing, written for the viewer's role; None to skip it."""
    kind, data = entry.get("kind"), entry.get("data") or {}
    me = role == "buyer"
    if kind == "authorized":
        return ("You approved the PayPal hold" if me else "Your customer approved the PayPal hold"), "info"
    if kind == "renewed":
        return ("You approved a new PayPal hold" if me else "Your customer approved a new PayPal hold"), "info"
    if kind == "evidence_added" and data.get("phase") in ("before", "after"):
        who = "The provider" if me else "You"
        return f"{who} uploaded the {data['phase']} photo", "info"
    if kind == "packet":
        if data.get("blocking_flags"):
            return ("Packet flagged for review: your decision is needed" if me
                    else "Packet flagged for review: your customer decides"), "warn"
        return ("AI packet ready: your decision is needed" if me
                else "AI packet ready: your customer decides"), "info"
    return {
        "captured": ("Paid: PayPal captured the hold", "ok"),
        "partially_captured": ("Partly paid: part captured, the rest released", "ok"),
        "voided": ("Hold released: nothing was charged", "bad"),
        "expired": ("The PayPal hold expired: nothing was charged", "bad"),
        "hold_lapsed": ("The PayPal hold lapsed before a decision", "warn"),
    }.get(kind)


def activity(items: Iterable[tuple[Held, list[dict]]], limit: int = 20) -> list[dict]:
    out = []
    for h, entries in items:
        for entry in entries:
            d = describe(entry, h.role)
            if d:
                out.append({"ts": entry["ts"], "seq": entry.get("seq", 0), "job": h.t.id, "title": h.t.listing.title, "text": d[0],
                            "tone": d[1], "role": h.role})
    return sorted(out, key=lambda x: (x["ts"], x["seq"]), reverse=True)[:limit]  # seq orders events in one second


# --- pages ---

def when(ts: Optional[str]) -> str:
    return (ts or "")[:16].replace("T", " ")


def _row(j: "Janus", h: Held) -> str:
    t, (label, tone) = h.t, badge(h.t)
    who = "with your provider" if h.role == "buyer" else "with your customer"
    money = f"Held {held_now(t):.2f} · captured {captured(t):.2f} {e(t.currency)}"
    return (f'<li><a class="jobrow" href="{e(link(j, h))}"><span class="pill {tone}">{e(label)}</span>'
            f'<b>{e(t.listing.title)}</b><span class="small muted">{"Customer" if h.role == "buyer" else "Provider"} '
            f'{who} · {e(t.amount)} {e(t.currency)}</span><span class="small muted">{money} · updated '
            f'{e(when(t.updated_at))} UTC</span></a></li>')


def jobs_page(j: "Janus", held: list[Held], tab: str) -> str:
    from .web_jobs import shell
    done = [h for h in held if h.t.state in TERMINAL]
    active = [h for h in held if h.t.state not in TERMINAL]
    rows = done if tab == "completed" else active
    base = j.config.mount
    tabs = "".join(f'<a class="tab{" on" if tab == k else ""}" href="{base}/jobs?tab={k}">{label} ({n})</a>'
                   for k, label, n in (("active", "Active", len(active)), ("completed", "Completed", len(done))))
    if rows:
        listing = f'<ul class="items">{"".join(_row(j, h) for h in rows)}</ul>'
    elif held:
        listing = ('<p class="muted">Nothing here yet. ' + ("Finished jobs show up here once they are paid, "
                   "partly paid or released." if tab == "completed" else "Every job on this browser is finished; "
                   "see Completed.") + "</p>")
    else:
        listing = (f'<p class="muted">No jobs yet. Describe a job to get started, or open the job link someone '
                   f'sent you: it is added here.</p><a class="btn" href="{base}/">Describe a job</a>')
    by_key = {(h.t.id, h.role): h for h in held}
    one_view = {h.t.id: h for h in reversed(held) if h.role == "seller"} | {h.t.id: h for h in held if h.role == "buyer"}
    feed = activity((h, j.audit.entries(h.t.id)) for h in one_view.values())  # a job held as both sides: once
    ev = "".join(f'<li><a class="jobrow" href="{e(link(j, by_key[(x["job"], x["role"])]))}"><span class="pill '
                 f'{x["tone"]}">{e(x["text"])}</span><b>{e(x["title"])}</b><span class="small muted">'
                 f'{e(when(x["ts"]))} UTC</span></a></li>' for x in feed)
    feed_html = (f'<ul class="items">{ev}</ul>' if ev else
                 '<p class="muted small">Photos, packets and payments on your jobs will show up here.</p>')
    return shell(j, "My jobs · Janus", f"""<h1>My jobs</h1>
<p class="lead">Jobs you opened on this browser, as customer or provider. Each one is checked against its link.</p>
<div class="card"><div class="tabs">{tabs}</div>{listing}</div>
<div class="card"><h2>Recent activity</h2>{feed_html}</div>""")


def profile_page(j: "Janus", held: list[Held]) -> str:
    from .web_jobs import shell
    n = {r: [h for h in held if h.role == r] for r in ("buyer", "seller")}
    roles = ", ".join(f"{label} on {len(n[r])} job{'s' if len(n[r]) != 1 else ''}"
                      for r, label in (("buyer", "Customer"), ("seller", "Provider")) if n[r]) or "None yet"

    def total(hs: list[Held]) -> str:
        sums: dict[str, Decimal] = {}
        for h in hs:
            sums[h.t.currency] = sums.get(h.t.currency, Decimal("0")) + captured(h.t)
        return ", ".join(f"{v:.2f} {c}" for c, v in sorted(sums.items())) or "0.00"
    active = sum(h.t.state not in TERMINAL for h in held)
    rows = [("Roles", roles), ("Active jobs", str(active)), ("Completed jobs", str(len(held) - active)),
            ("Paid to you (provider)", total(n["seller"])), ("Paid by you (customer)", total(n["buyer"])),
            ("PayPal", f"{j.paypal.mode} mode; each job's money is held and captured by PayPal")]
    dl = "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in rows)
    return shell(j, "Profile · Janus", f"""<h1>Profile</h1>
<div class="card"><h2>Account</h2><div class="who"><img id="pf-img" alt="" hidden><div><b id="pf-name">Checking
sign-in…</b><div class="small muted" id="pf-mail"></div></div></div>
<p class="small muted" id="pf-note" hidden><a href="/">Sign in on the Janus home page</a> to see your name here. Your jobs work without it: they open by their links.</p></div>
<div class="card"><h2>On this browser</h2><dl>{dl}</dl></div>
<script>fetch('/api/auth/session',{{credentials:'include'}}).then(r=>r.ok?r.json():{{}}).catch(()=>({{}})).then(d=>{{
const u=d&&d.user,$=id=>document.getElementById(id);if(!u){{$('pf-name').textContent='Not signed in';
$('pf-note').hidden=false;return}}$('pf-name').textContent=u.name||u.email||'Signed in';$('pf-mail').textContent=
u.name&&u.email?u.email:'';if(u.image){{$('pf-img').src=u.image;$('pf-img').hidden=false}}}})</script>""")


FAQ = [
    ("What does “authorized” mean?",
     "The customer approves a PayPal hold for the job's price. PayPal reserves the money on the customer's "
     "account, so the provider knows it is there, but nothing is charged."),
    ("When is my money taken?",
     "Only when the customer taps Accept (the full amount) or Pay part (the amount they enter). In a job nothing "
     "else can capture: not the AI, not the provider, not the scheduler, not a PayPal webhook. Hard rules are "
     "checked again right before every capture."),
    ("What does the AI decide?",
     "Nothing about money. Claude writes the checklist and, from the before and after photos, an acceptance "
     "packet: pass, fail or unclear for each item, with a confidence and a one-line reason. The customer decides."),
    ("What happens if I reject?",
     "Janus asks PayPal to void the hold. The customer is not charged and the money is released."),
    ("How does Pay part work?",
     "The customer enters an amount above zero and below the hold. PayPal captures that amount, then Janus voids "
     "the rest of the hold, so the remainder is released. The page shows both numbers before you confirm."),
    ("What if the hold expires?",
     "PayPal guarantees a hold for 3 days and keeps it for up to 29. Janus reauthorizes once when it can, and "
     "releases the hold about 2 days before it would expire if the customer has not decided. The job stays open: "
     "the customer can approve a new hold for the same job and checklist."),
    ("What happens to my photos?",
     "They are stored with the job. Before storing, Janus reads the capture time, camera, editing app and a "
     "location rounded to about 100 m for its fraud checks, then keeps a copy without the file's metadata. "
     "Photos are sent to Claude to write the packet and are shown on the job page through links that expire "
     "after 5 minutes."),
    ("What does a fraud flag mean?",
     "A check found something wrong, for example a photo already used on another job, an after photo that looks "
     "the same as the before photo, an editing app, a pasted-in area, or capture times that do not add up. The "
     "job goes to manual review. To pay anyway the customer must tick an override and give a reason, which is "
     "logged. Rejecting is always possible."),
    ("Who can see my job?",
     "Anyone with its link, so share it only with the other party. The customer link and the provider link are "
     "different and give different actions. My jobs only lists jobs whose link was opened on that browser."),
]


def help_page(j: "Janus") -> str:
    from .web_jobs import shell
    qa = "".join(f"<details><summary>{e(q)}</summary><p>{e(a)}</p></details>" for q, a in FAQ)
    return shell(j, "How it works · Janus", f"""<h1>How it <em>works</em></h1>
<p class="lead">The customer's money is held by PayPal, not taken. The provider is paid when the customer accepts
the work. Janus never holds your money.</p>
<div class="card"><h2>Four steps</h2><ol class="steps"><li>The provider describes the job; Janus writes the
checklist.</li><li>The customer approves a PayPal hold.</li><li>The provider uploads before and after photos; Janus
builds the acceptance packet.</li><li>The customer accepts, pays part, or rejects.</li></ol></div>
<div class="card"><h2>Questions</h2>{qa}</div>""")
