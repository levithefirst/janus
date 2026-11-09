"""PayPal webhooks: verified, deduplicated by event id, and safe in any arrival order.

A webhook is treated as a hint to look again, never as the truth. After the signature is verified
and the event id is claimed, Janus finds the threshold and calls `reconcile`, which reads the
authorization from PayPal and moves the threshold forward only along the state machine. So:

- duplicates are dropped (the event id is claimed with O_EXCL, atomically, across processes);
- out-of-order events are harmless: a stale event re-reads the same current PayPal state;
- an event can never capture: reconcile only records what PayPal already did (rule 5).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .logs import log_event
from .state import TERMINAL

if TYPE_CHECKING:
    from .service import Janus

EVENT_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")


class Inbox:
    """Webhook event ids already processed. A claim is an atomic create-if-absent in the storage backend."""

    def __init__(self, store):
        from .kv import Backend, FileBackend
        self.kv = store if isinstance(store, Backend) else FileBackend(Path(store))

    def claim(self, event_id: str) -> bool:
        return self.kv.add(f"webhooks/{event_id}.seen")

    def release(self, event_id: str) -> None:
        self.kv.delete(f"webhooks/{event_id}.seen")


def _related(resource: dict) -> set[str]:
    ids = {resource.get("id"), resource.get("custom_id"), resource.get("invoice_id")}
    rel = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    ids |= {rel.get("order_id"), rel.get("authorization_id"), rel.get("capture_id")}
    for unit in resource.get("purchase_units") or []:
        ids |= {unit.get("custom_id"), unit.get("reference_id")}
    return {i for i in ids if isinstance(i, str) and i}


def find_threshold(j: "Janus", event: dict) -> Optional[str]:
    ids = _related(event.get("resource") or {})
    for t in j.store.all():
        p = t.payment
        mine = {t.id, p.order_id, p.authorization_id, *p.previous_authorization_ids, *p.previous_order_ids,
                p.renewal.order_id if p.renewal else None, *(c.capture_id for c in p.captures)}
        if ids & {x for x in mine if x}:
            return t.id
    return None


def handle(j: "Janus", headers: dict[str, str], raw_body: bytes) -> tuple[int, dict]:
    if not j.paypal.verify_webhook(headers, raw_body, j.config.paypal_webhook_id):
        log_event("security", "webhook_rejected", reason="bad signature")
        j.audit.append("webhook_rejected", None, "webhook", "signature verification failed")
        return 401, {"error": "signature verification failed"}
    try:
        event = json.loads(raw_body)
    except ValueError:
        return 400, {"error": "body is not JSON"}
    eid, etype = str(event.get("id", "")), str(event.get("event_type", ""))
    if not EVENT_ID.match(eid):
        return 400, {"error": "bad event id"}
    if not j.inbox.claim(eid):
        log_event("paypal", "webhook_duplicate", event_id=eid, event_type=etype)
        return 200, {"event_id": eid, "duplicate": True}
    try:
        tid = find_threshold(j, event)
        if tid is None:
            j.audit.append("webhook_unmatched", None, "webhook", f"{etype}: no matching threshold", event_id=eid)
            return 200, {"event_id": eid, "matched": False}
        before = j.get(tid).state
        after = before if before in TERMINAL else j.reconcile(tid).state
        j.audit.append("webhook", tid, "webhook", f"{etype}: re-read PayPal, {before.value} -> {after.value}",
                       event_id=eid, event_type=etype)
        return 200, {"event_id": eid, "threshold_id": tid, "state": after.value}
    except Exception:
        j.inbox.release(eid)  # let PayPal's retry be processed
        raise
