"""Minimal JSON HTTP API (stdlib only). Run: python -m eid serve

POST /jobs                    {"text": "...", "amount"?: "180", "currency"?: "USD"}
GET  /jobs                    list jobs
GET  /jobs/{id}               full job state
POST /jobs/{id}/payment       create PayPal order -> customer link
GET  /jobs/{id}/pay           customer link: redirects to PayPal approval
GET  /paypal/return?token=..  PayPal redirects here after approval -> authorize
GET  /paypal/cancel
POST /jobs/{id}/authorize     authorize manually (if the return URL was not reachable)
POST /jobs/{id}/evidence      {"items": [{"requirement_id", "kind", "note", "file"}]}
POST /jobs/{id}/packet        run AI gap check + acceptance packet
GET  /jobs/{id}/packet        acceptance packet as text/markdown
POST /jobs/{id}/approve       {"note"?: "..."}            -> capture
POST /jobs/{id}/dispute       {"reason": "...", "kind"?: "dispute"|"fix"} -> void
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .ai import AIError
from .logs import logger
from .paypal import PayPalError
from .service import Eid, EidError
from .state import TransitionError
from .store import JobNotFound


def make_handler(eid: Eid):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep stderr for eid's own log lines
            pass

        def _send(self, status: int, body, content_type="application/json"):
            data = (json.dumps(body, indent=2, default=str) if content_type == "application/json" else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, url: str):
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise EidError("request body must be JSON")

        def _dispatch(self, method: str):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p]
            try:
                self._route(method, parts, q)
            except JobNotFound as e:
                self._send(404, {"error": f"job not found: {e}"})
            except TransitionError as e:
                self._send(409, {"error": str(e)})
            except EidError as e:
                self._send(400, {"error": str(e)})
            except (PayPalError, AIError) as e:
                self._send(502, {"error": str(e)})
            except Exception as e:  # never drop the connection without a response
                logger.exception("unhandled error on %s %s", method, self.path)
                self._send(500, {"error": f"internal error: {type(e).__name__}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _route(self, method, parts, q):
            job = lambda: eid.get(parts[1]).model_dump(mode="json")  # noqa: E731
            if method == "POST" and parts == ["jobs"]:
                b = self._body()
                j = eid.create_job(b.get("text", ""), b.get("amount"), b.get("currency"))
                return self._send(201, j.model_dump(mode="json"))
            if method == "GET" and parts == ["jobs"]:
                return self._send(200, [{"id": j.id, "state": j.state.value, "title": j.card.title,
                                         "amount": j.amount, "currency": j.currency} for j in eid.store.all()])
            if parts[:1] == ["paypal"] and method == "GET":
                if parts[1:] == ["return"]:
                    if not q.get("token"):
                        raise EidError("missing ?token= (the PayPal order id)")
                    j = eid.store.find_by_order(q["token"])
                    j = eid.authorize(j.id)
                    return self._send(200, f"Payment of {j.amount} {j.currency} authorized for '{j.card.title}'. "
                                           "Nothing is charged until you approve the finished work.\n", "text/plain")
                if parts[1:] == ["cancel"]:
                    return self._send(200, "Payment cancelled. You can reopen the link to try again.\n", "text/plain")
            if len(parts) >= 2 and parts[0] == "jobs" and re.fullmatch(r"job_[0-9a-f]+", parts[1]):
                action = parts[2] if len(parts) > 2 else None
                if method == "GET" and action is None:
                    return self._send(200, job())
                if method == "GET" and action == "pay":
                    j = eid.get(parts[1])
                    if j.payment.authorization_id:
                        return self._send(200, f"Payment for '{j.card.title}' is already authorized "
                                               f"(job is {j.state.value}).\n", "text/plain")
                    j = eid.create_payment(parts[1])
                    if not j.payment.approve_url:
                        raise EidError("PayPal returned no approval link")
                    return self._redirect(j.payment.approve_url)
                if method == "GET" and action == "packet":
                    j = eid.get(parts[1])
                    if not j.packet_markdown:
                        raise EidError("no packet yet")
                    return self._send(200, j.packet_markdown + "\n", "text/markdown")
                if method == "POST":
                    b = self._body()
                    if action == "payment":
                        j = eid.create_payment(parts[1])
                        return self._send(200, {"customer_link": eid.customer_link(j),
                                                "approve_url": j.payment.approve_url, "order_id": j.payment.order_id})
                    handlers = {
                        "authorize": lambda: eid.authorize(parts[1]),
                        "evidence": lambda: eid.submit_evidence(parts[1], b.get("items", [])),
                        "packet": lambda: eid.generate_packet(parts[1]),
                        "approve": lambda: eid.approve(parts[1], b.get("note", "")),
                        "dispute": lambda: eid.dispute(parts[1], b.get("reason", ""), b.get("kind", "dispute")),
                    }
                    if action in handlers:
                        return self._send(200, handlers[action]().model_dump(mode="json"))
            self._send(404, {"error": "not found"})

    return Handler


def serve(eid: Eid, host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(eid))
    print(f"Eid API on http://{host}:{port}  (paypal={eid.paypal.mode}, ai={eid.ai.mode})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
