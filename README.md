# Janus

Janus is an AI acceptance-and-release agent for local service jobs.

## Core flow

1. The provider creates a job from plain text.
2. AI turns it into a structured job card with a checklist and required evidence.
3. The customer receives a link and authorizes payment via PayPal Orders v2 (`AUTHORIZE`). Funds are held, not charged.
4. The provider uploads evidence.
5. AI checks the evidence against the checklist, writes a gap report with a recommended action, and produces an acceptance packet.
6. The customer approves → `CAPTURE`, or requests a fix / disputes → `VOID`.

This repo is the backend only: no UI. You drive it with a CLI (`python -m eid ...`) or a small JSON HTTP API (`python -m eid serve`).

## Quick start (offline, no keys)

```bash
git clone https://github.com/levithefirst/janus && cd janus
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m eid demo                    # full loop, complete evidence -> approve -> CAPTURE
python -m eid demo --evidence-file examples/evidence_partial.json   # gaps -> dispute -> VOID
pytest -q                             # 18 tests: state machine, both outcomes, HTTP API, PayPal + Claude request shapes
```

With no credentials set, Janus runs both integrations in **mock** mode. The PayPal mock simulates the buyer approving the order. The AI mock uses simple keyword rules and never looks at files. This tests the state machine and the plumbing, not judgement.

## Running against PayPal sandbox and Claude

```bash
cp .env.example .env
# fill in PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (developer.paypal.com > Apps & Credentials > Sandbox)
# and ANTHROPIC_API_KEY
python -m eid demo
```

The mode is chosen per integration: `EID_PAYPAL=sandbox|mock`, `EID_AI=claude|mock`. If you leave them unset, Janus uses the real integration whenever its credentials are present. You can mix them, for example real Claude with mock PayPal.

In sandbox mode, step 3 prints a PayPal approval URL. Open it, log in with a **sandbox Personal (buyer)** account (developer.paypal.com > Testing Tools > Sandbox Accounts) and approve. The demo checks the order every 5 seconds (for up to `--wait`, 600 s by default) and authorizes it once the buyer has approved. You can see the authorization, capture or void in the sandbox merchant account's activity.

If you also run `python -m eid serve`, the PayPal return URL (`EID_BASE_URL/paypal/return`) authorizes the order automatically as soon as the buyer is redirected back.

The AI uses `claude-opus-5-5` (change it with `EID_MODEL`) with structured outputs, so the job card and the gap report are schema-validated JSON. The server-side refusal fallback is enabled (`fallbacks: "default"`), so a declined request is retried on a fallback model. Photos (`png/jpg/gif/webp`), PDFs and text files you attach as evidence are sent to Claude so it can look at them.

## End-to-end test, step by step (CLI)

```bash
python -m eid create --file examples/job.txt             # 1+2: prints job id, checklist, required evidence
python -m eid pay <job_id>                               # 3a: creates the order, prints the customer link
python -m eid authorize <job_id> --wait 600              # 3b: authorizes after the buyer approves
python -m eid evidence <job_id> --json examples/evidence_complete.json   # 4
#   or one item at a time: --req E2 --kind photo --file path.jpg --note "..."
python -m eid packet <job_id>                            # 5: gap report + acceptance packet (Markdown)
python -m eid approve <job_id>                           # 6: CAPTURE
python -m eid dispute <job_id> --reason "..." [--fix]    # 6: or VOID
python -m eid show <job_id> [--json]                     # inspect state at any point
python -m eid list
```

With Claude, the evidence requirement ids (`E1`, `E2`, ...) can differ from the ones in the example evidence files, so check `create`'s output and edit the `requirement_id`s. The provider's `requirement_id` is only a hint: the AI judges what the evidence actually shows. The photos in `examples/photos/` are labelled placeholders, so expect Claude to say so in its report.

## HTTP API

`python -m eid serve` starts the API on `127.0.0.1:8000`. Every endpoint is listed in `eid/server.py`.

```bash
curl -s localhost:8000/jobs -d '{"text": "Mount a 65in TV on drywall, hide the cables. $150"}'
curl -s -X POST localhost:8000/jobs/<id>/payment       # -> customer_link, approve_url
# customer opens GET /jobs/<id>/pay -> redirected to PayPal -> back to /paypal/return -> authorized
curl -s localhost:8000/jobs/<id>/evidence -d '{"items":[{"requirement_id":"E1","kind":"photo","file":"/abs/path.jpg","note":"..."}]}'
curl -s -X POST localhost:8000/jobs/<id>/packet
curl -s localhost:8000/jobs/<id>/packet                # Markdown packet
curl -s localhost:8000/jobs/<id>/approve -d '{}'       # or /dispute -d '{"reason":"...","kind":"fix"}'
```

Errors come back as JSON: 409 for a step that isn't allowed in the current state, 400 for bad input, 502 when PayPal or the AI fails.

## State machine

```
created ─► payment_pending ─► authorized ─► evidence_submitted ⇄ packet_ready ─┬─► approved ─► captured
 (job card)  (order + link)    (funds held)    (provider can add more after        └─► disputed ─► voided
                                                a gap report, before a decision)
```

Every change goes through `eid/state.py`, and out-of-order calls raise `TransitionError`. You can't approve before a packet exists, capture twice, or capture a voided authorization. The customer decides, not the AI, so they can approve even when the AI recommends more evidence. If a capture or void call to PayPal fails, the job stays in `approved` or `disputed`, and running the same command again retries it. Every PayPal call that changes something sends a `PayPal-Request-Id`, so retries are idempotent.

## Storage and logs

- `data/jobs/<job_id>/job.json` holds the full job: card, payment ids, evidence, gap report, packet, decision, and an event history.
- `data/jobs/<job_id>/evidence/` has copies of the uploaded files (with sha256 hashes in `job.json`), and `acceptance_packet.md` is the packet.
- `data/eid.log.jsonl` is an append-only log of every PayPal call (path, HTTP status, PayPal status, `PayPal-Debug-Id`), every AI request and decision (model, stop reason, tokens, recommended action, gaps), and every state change. The same lines are printed to stderr.
- `data/mock_paypal.json` holds mock-mode PayPal state.

## Layout

```
eid/state.py     state machine
eid/service.py   one method per step of the loop
eid/paypal.py    Orders v2 sandbox client + offline mock
eid/ai.py        the three AI tasks (Claude + mock) and their prompts
eid/models.py    job card / gap report / evidence schemas
eid/store.py     JSON file store
eid/cli.py       CLI + `demo`
eid/server.py    HTTP API (stdlib only)
examples/        sample job text, evidence files, placeholder photos
tests/           pytest suite (runs fully offline)
```

## Notes and limits

- Sandbox only. PayPal authorizations have a 3-day honor period and expire after 29 days. Janus does not reauthorize.
- No auth or multi-tenancy. Anyone who can reach the API can act on any job.
- Evidence files are given as paths on the machine running Janus. There is no upload endpoint.
