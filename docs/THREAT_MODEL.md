# Threat model

Scope: the `janus` backend on branch `hardening`. Every attack, its severity and status is in
[ATTACKS.md](ATTACKS.md). This page explains what we protect, from whom, and where the trust boundaries are.

## Vocabulary

| Term in the brief | Term in the code | Meaning |
|---|---|---|
| job | threshold (`th_...`) | one condition-governed payment |
| customer | buyer | the payer; authorizes on PayPal, consents, may release or dispute |
| provider | seller | the payee; states the condition, may refund, may re-upload evidence |
| checklist item | requirement (`R1`...) | one testable part of the compiled condition |

## What we protect, in order

1. **The customer's money:** never captured without their consent to the exact terms, never after
   a void or expiry, never twice, never because they went silent.
2. **The provider's money:** a verified condition is captured before the authorization expires,
   and a capture that PayPal performed is never lost by a crash.
3. **Integrity of decisions:** every money decision has a recorded reason that cannot be edited
   without detection.
4. **Access:** each party can only act as themselves, on their own jobs, in their own tenant.
5. **Personal data:** notes, photos, GPS, emails and phone numbers stay out of logs and URLs.

## Actors

| Actor | Can | Wants (if hostile) |
|---|---|---|
| Customer | authorize, consent, upload evidence, release, dispute, accept amendments | the item without paying (force a void) |
| Provider | create, allocate, amend, refund, re-upload evidence | to be paid without meeting the condition (force a capture) |
| Third party with a leaked link | whatever that link's role allows | money, data |
| Network attacker | see and replay traffic, forge webhooks | trigger state changes |
| The AI model | read listing, condition, evidence | nothing, but it can be wrong, overconfident, or manipulated by text in notes and photos |
| PayPal | settle money, send webhooks | not hostile; can be slow, retry, reorder, or fail |
| Operator / host | run the service, read disk | not hostile; disk may leak; the process may crash mid-call |

## Trust boundaries

```
 customer browser ──HTTPS──┐                     ┌──> PayPal REST (sandbox)   [authoritative for money]
 provider / tenant API ────┤   janus server      │
 PayPal webhooks ──────────┤   (rules, policy,   ├──> Claude API              [untrusted output]
                           │    state machine)   │
 cron: janus tick ─────────┘         │           └──> local disk: data/       [trusted, may leak]
                                     v
                        audit.jsonl (hash chain), thresholds/<id>/threshold.json (CAS, flock)
```

1. **HTTP in.** Every route resolves a role from a capability token (HMAC, 256 bits) or refuses with
   404. Bodies are untrusted: the submitter's role comes from the token, not the body.
2. **AI out.** The model's verdict is advice. Code decides money: `policy.decide` (confidence bar,
   self-consistency), then `rules.check_capture` (consent, expiry, actor, dispute, amendment), right
   before PayPal is called.
3. **Evidence in.** Notes and any text inside photos are data. Photos are checked for quality,
   reuse and capture time before the model sees them.
4. **PayPal in.** Webhooks are signature-verified and then only trigger a fresh read from PayPal.
   PayPal's own record is the truth; events are hints.
5. **Disk.** Thresholds are written with compare-and-set under an exclusive lock. Terminal thresholds
   are refused at the store. Tokens and secrets are never written. Decisions also go to a
   hash-chained, append-only audit log.

## Key design decisions

- **Two independent gates before money moves.** The policy decides what should happen; the rules
  decide what may. A bug in one is caught by the other (`tests/test_rules.py` breaks the policy on
  purpose).
- **Write intent before acting.** The exact capture (request id, amount, final flag) is saved before
  PayPal is called, and the request id is derived from the job and action. A crash at any point is
  recovered by replaying the same request: PayPal returns the original result instead of acting twice.
- **Silence never pays.** Only a verified condition the customer consented to, or the customer
  themselves, can capture. The scheduler and webhooks cannot. Near expiry, the default is to void.
- **Consent is bound to terms.** Consent stores a hash of condition, price, requirements and
  allocations. Any change, including an amendment, needs a new consent.
- **Deny by default, and quietly.** Missing job and wrong token look identical (404).

## Assumptions

- TLS terminates in front of the server (a reverse proxy). The server itself speaks HTTP.
- `data/` is on a local POSIX filesystem (`flock` and `O_EXCL` semantics). Not NFS.
- One host, or several processes on one host. Rate limits are per process.
- The host clock is NTP-synced. All times are UTC.
- PayPal sandbox behaves as its documentation describes. Three behaviours are still unverified live:
  void after a partial capture, reauthorization limits, and how long PayPal honours a
  `PayPal-Request-Id`. They are listed in ATTACKS.md.

## Out of scope for this branch

- UI changes (only what security required: tokens on links, signed image URLs, no referrer).
- Live PayPal runs (no sandbox keys yet), and live Claude runs (no API key yet).
- Disputes after capture (PayPal's own dispute process covers them; `refund_capture` exists in the
  client but is not wired to a product flow).
- Multi-host deployment, key rotation tooling, external anchoring of the audit log.
