"""The decision packet: everything a person needs to audit one threshold, as Markdown.

For each checklist item it shows the AI's own reasoning (the observation), its result and
confidence, the allocation and whether that money was captured or released. It also shows the bar
the verdict had to clear and why, the integrity flags, every consent, and the PayPal record.
Notes and other submitter text are untrusted: they are escaped, never rendered as Markdown.
"""
from __future__ import annotations

import re

from .store import Threshold

_MD = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")


def esc(text: str) -> str:
    return _MD.sub(r"\\\1", (text or "").replace("\n", " "))


def build(t: Threshold) -> str:
    v = t.last_verdict
    out = [f"# Decision packet: {esc(t.listing.title)}", "",
           f"**Threshold** {t.id} | **State** {t.state.value} | **Amount** {t.amount} {t.currency}", "",
           "## Condition", f"> {esc(t.condition)}", "", esc(t.compiled.summary), ""]
    if t.compiled.not_verifiable:
        out += [f"A photo cannot prove: {esc('; '.join(t.compiled.not_verifiable))}", ""]

    checks = {c.requirement_id: c for c in v.verdict.checks} if v else {}
    out += ["## Checklist", "", "| Item | Must | Amount | Status | Result | Confidence | AI reasoning |",
            "|---|---|---|---|---|---|---|"]
    for r in t.compiled.requirements:
        c = checks.get(r.id)
        conf = c.confidence if c and c.confidence is not None else (v.verdict.confidence if v else None)
        out.append(f"| {r.id} {esc(r.statement)} | {'yes' if r.critical else 'no'} | {t.allocations.get(r.id, '-')} | "
                   f"{t.item_status.get(r.id, '-')} | {c.result if c else '-'} | "
                   f"{f'{conf:.2f}' if conf is not None else '-'} | {esc(c.observation) if c else 'not yet judged'} |")
    out.append("")

    if v:
        out += ["## Latest verdict",
                f"- Verdict: **{v.verdict.verdict}**, confidence {v.verdict.confidence:.2f} (source: {v.source})",
                f"- Bar to move money: {v.min_confidence:.2f} (base plus evidence trust penalty)",
                f"- Decision: **{v.action.upper()}**: {esc(v.reason)}",
                f"- Evidence from: {v.evidence_by}; trust flags: {', '.join(v.trust_flags) or 'none'}",
                f"- Summary: {esc(v.verdict.summary)}"]
        if v.verdict.next_step:
            out.append(f"- Next step: {esc(v.verdict.next_step)}")
        out.append("")
        if len(t.verdicts) > 1:
            out += ["Earlier verdicts:"] + [f"- {x.at}: {x.verdict.verdict} {x.verdict.confidence:.2f} -> "
                                            f"{x.action} ({esc(x.reason)})" for x in t.verdicts[:-1]] + [""]

    out += ["## Evidence"]
    for e in t.evidence:
        q = e.quality or {}
        out.append(f"- **{e.id}** by {e.by} at {e.submitted_at}: quality {'ok' if q.get('ok', True) else 'FAILED'}"
                   f", EXIF {'yes' if (e.exif or {}).get('present') else 'no'}, flags: {', '.join(e.flags) or 'none'}"
                   f", note: `{esc(e.note)[:200] or '(none)'}`")
    if not t.evidence:
        out.append("- none")
    out += ["", "## Consents"] + ([f"- {c.at}: {c.kind} by {c.by} via {c.channel}, amount {c.amount}, "
                                   f"terms {c.terms_hash[:12]}, auto-capture on evidence from {', '.join(c.evidence_from)}"
                                   for c in t.consents] or ["- none"])
    if t.amendment:
        out.append(f"- Amendment waiting for the customer: price {t.amendment.price}, "
                   f"condition {esc(t.amendment.condition or '(unchanged)')}")
    p = t.payment
    out += ["", "## PayPal", f"- Order {p.order_id}, authorization {p.authorization_id} ({p.authorization_status}), "
                             f"authorized {p.authorized_at}, expires {p.expiration_time}"]
    out += [f"- Capture {c.capture_id}: {c.amount} {'final' if c.final else 'partial'} "
            f"(items {', '.join(c.items) or 'all'}; request {c.request_id})" for c in p.captures]
    if t.outcome and t.state.value in ("captured", "voided", "expired"):
        out += ["", f"## Outcome", f"**{t.outcome.action.upper()}** by {t.outcome.by}: {esc(t.outcome.reason)}"]
    return "\n".join(out) + "\n"
