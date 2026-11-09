# Eid

Eid is an AI acceptance-and-release agent for local service jobs.

## Core flow

1. The provider creates a job from plain text.
2. AI turns it into a structured job card with a checklist and required evidence.
3. The customer receives a link and authorizes payment via PayPal Orders v2 (`AUTHORIZE`). Funds are held, not charged.
4. The provider uploads evidence.
5. AI checks the evidence against the checklist, writes a gap report with a recommended action, and produces an acceptance packet.
6. The customer approves → `CAPTURE`, or requests a fix / disputes → `VOID`.

This repo is the backend only: no UI. You drive it with a CLI (`python -m eid ...`) or a small JSON HTTP API (`python -m eid serve`).

## Run the full loop in 2 minutes

Requires Python 3.10+.

```bash
git clone https://github.com/levithefirst/eid && cd eid
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # works unchanged: blank keys = offline mock mode
python -m eid demo            # steps 1-6, prints each step, ends in "Final state: captured"
```

You can fill in any of the three keys in `.env`; each one switches its integration to the real service:

| `.env` keys set | What `python -m eid demo` does |
|---|---|
| none | Both mocked. Runs offline in about a second. The PayPal mock simulates the buyer approving. The AI mock uses keyword rules and doesn't look at photos. |
| `ANTHROPIC_API_KEY` | Claude writes the job card, inspects the evidence photos, and writes the gap report and packet. Slower: three Claude calls. |
| `PAYPAL_CLIENT_ID` + `PAYPAL_CLIENT_SECRET` | Real sandbox order, authorization, and capture or void. You approve the payment once in a browser (see below). |

**Other outcomes.** By default (`--outcome auto`) the simulated customer follows the AI: approve → capture, anything else → void.

```bash
python -m eid demo --evidence-file examples/evidence_partial.json   # gaps found -> dispute -> VOID
python -m eid demo --outcome approve    # customer approves regardless of the AI -> CAPTURE
python -m eid demo --outcome fix        # customer requests a fix -> VOID
pytest -q                               # 25 offline tests
```

With Claude, the default run usually ends in **void**. The example photos in `examples/photos/` are labelled placeholders, and Claude reports that they don't prove the work. Add `--outcome approve` to see a capture.

**PayPal sandbox approval (one browser step):** step 3 prints an approval URL and waits. Open it and log in as the sandbox **Personal** account that PayPal creates for you (developer.paypal.com > Testing Tools > Sandbox Accounts > the `...personal.example.com` account > View/Edit for the password). Then click through to approve. The demo checks every 5 seconds and continues on its own. You don't need to run the server for this. The page PayPal redirects to afterwards may not load; that's expected. The authorization, capture or void then shows up in the sandbox Business account's activity.

**Configuration:** every setting is documented in `.env.example`. If you need to force a mode, set `EID_PAYPAL=sandbox|mock` or `EID_AI=claude|mock`.

**About the AI:** Claude (`claude-opus-5-5` by default, set with `EID_MODEL`) uses structured outputs, so the job card and the gap report are schema-validated JSON. The server-side refusal fallback is on (`fallbacks: "default"`). Photos (`png/jpg/gif/webp`), PDFs and text files attached as evidence are sent to Claude so it can look at them. The AI only advises; the customer's decision is what moves the money.

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

Each evidence item's `requirement_id` is the provider's hint about which requirement it covers. The AI judges what the evidence actually shows. The `evidence` command rejects ids that aren't on the job card, so with Claude, check `create`'s output first: Claude may number requirements differently from the example files. `demo` drops unknown ids instead and logs that it did.

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

Errors come back as JSON: 409 for a step that isn't allowed in the current state, 400 for bad input, 404 for an unknown job, 502 when PayPal or the AI fails. Reopening the customer link or the PayPal return page after authorizing is harmless.

## State machine

```
created ─► payment_pending ─► authorized ─► evidence_submitted ⇄ packet_ready ─┬─► approved ─► captured
 (job card)  (order + link)    (funds held)    (provider can add more after        └─► disputed ─► voided
                                                a gap report, before a decision)
```

Every change goes through `eid/state.py`, and out-of-order calls raise `TransitionError`. You can't approve before a packet exists, capture twice, or capture a voided authorization. The customer decides, not the AI, so they can approve even when the AI recommends more evidence. If a capture or void call to PayPal fails, the job stays in `approved` or `disputed`, and running the same command again retries it. Every PayPal call that changes something sends a `PayPal-Request-Id`, so retries are idempotent. Steps that change state are serialized, so two simultaneous approvals capture only once.

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

- Sandbox only. PayPal authorizations have a 3-day honor period and expire after 29 days. Eid does not reauthorize.
- No auth or multi-tenancy. Anyone who can reach the API can act on any job.
- Evidence files are given as paths on the machine running Eid. There is no upload endpoint.
