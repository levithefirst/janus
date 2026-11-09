# Janus Threshold

**Describe what must be true. Janus verifies it. PayPal moves the money.**

A seller states a condition in plain language: *"Pay only if this item matches the listing photo and description."* The buyer's money is **authorized and held** by PayPal, not charged. When the item arrives, the buyer photographs it. Janus compares the photo against the condition and the result drives PayPal:

| Janus verdict | Confidence | What happens on PayPal |
|---|---|---|
| condition **met** | high | **CAPTURE**: the seller is paid |
| condition **not met** | high | **VOID**: the hold is released, the buyer is never charged |
| uncertain, or low confidence | any | **HOLD**: nothing moves until there is a better photo or a human decision |

The condition is the product. The AI changes the payment result; it doesn't just summarise.

## Run it in 2 minutes (no keys needed)

```bash
git clone https://github.com/levithefirst/janus && cd janus
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m janus demo        # same listing, three deliveries: match, mismatch, unclear
python -m janus serve       # then open http://localhost:8000 (works at phone width)
pytest -q                   # 48 tests
```

`demo` ends with a before/after table. BEFORE and AFTER are the authorization status read back from PayPal (here, the mock):

```
SCENARIO   VERDICT    CONFIDENCE  DECISION  BEFORE   AFTER     STATE     CAPTURE
match      met        0.97        CAPTURE   CREATED  CAPTURED  captured  MOCKCAP-611EC5AD29
mismatch   not_met    0.97        VOID      CREATED  VOIDED    voided    -
unclear    uncertain  0.35        HOLD      CREATED  CREATED   held      -
```

With no keys, both integrations run as offline stand-ins. **Mock PayPal** simulates the buyer's approval, uses PayPal's real error names and keeps state in `data/mock_paypal.json`. **Mock AI** really looks at the pixels, but crudely: it compares the colour signature of the buyer's photo with the listing photo and rejects dark or blurry photos. It proves the loop, not the judgement. For judgement, use Claude.

## The real thing: PayPal sandbox + Claude

### What you need

| Credential | Where to get it |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | [developer.paypal.com](https://developer.paypal.com) > Apps & Credentials > **Sandbox** tab > Create App (type Merchant). These are the *sandbox* credentials; live ones will fail OAuth. |
| A sandbox **Personal** (buyer) account | developer.paypal.com > Testing Tools > Sandbox Accounts. One is created by default; open it to see its email and password. You log in with it when approving payment. |
| A sandbox **Business** account | Created by default and linked to your app. The captured money appears here. |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) > API Keys |

```bash
cp .env.example .env      # fill in the four values above
python -m janus demo      # prints the active modes: (paypal=sandbox, ai=claude, ...)
```

### What happens, step by step

1. Janus creates a PayPal order with `intent: AUTHORIZE` (`POST /v2/checkout/orders`) and prints the approval URL.
2. Open the URL, log in as the **sandbox Personal** account and approve. The demo checks every 5 s (up to `--wait`, 600 s).
3. Janus authorizes the order (`POST /v2/checkout/orders/{id}/authorize`). The money is now **held**: authorization status `CREATED`.
4. The buyer's photo goes to Claude (`claude-opus-5-5`, vision + structured output) together with the listing photo and the compiled requirements.
5. The verdict goes through `janus/policy.py`, and Janus calls `POST /v2/payments/authorizations/{id}/capture` or `.../void`, or does nothing (hold).
6. Janus reads the authorization back (`GET /v2/payments/authorizations/{id}`) for the AFTER column.

You can check each outcome independently: log in to [sandbox.paypal.com](https://www.sandbox.paypal.com) as the Business account and look at Activity. The captured payment shows as completed, the voided one as voided.

`demo` runs three orders, so you approve three times. For the video, run `python -m janus demo --scenario match` and `--scenario mismatch` separately.

### On a phone (the real user experience)

```bash
python -m janus serve --host 0.0.0.0
cloudflared tunnel --url http://localhost:8000          # or ngrok http 8000
JANUS_BASE_URL=https://<your-tunnel>.trycloudflare.com python -m janus serve --host 0.0.0.0
```

`JANUS_BASE_URL` must be the public URL, because PayPal redirects the buyer back to `JANUS_BASE_URL/paypal/return`.

1. **Seller** opens `/`, enters the item, price, listing photo and condition, and taps *Compile condition & create buyer link*. The page shows the compiled requirements and a buyer link to copy.
2. **Buyer** opens the link, taps *Authorize with PayPal*, approves, and lands back on the page.
3. The buyer taps the photo field (it opens the camera), takes the photo, *Send photo to Janus*. The photo is shrunk on the phone before upload.
4. Both parties see the outcome on the same link: verdict, confidence, a met / not met / can't tell line per requirement, both photos side by side, and the PayPal record (order, authorization, status, capture ID).

## Recording the demo (under 3 minutes)

1. (0:00) One line: "The condition governing the payment is the product."
2. (0:15) Seller page: type the condition, show the compiled requirements.
3. (0:45) Buyer link on a phone: authorize in the PayPal sandbox and show "PayPal is holding $24.00".
4. (1:10) **Match:** photograph the real item. Show the verdict and the CAPTURED banner, then the sandbox Business account receiving the money.
5. (1:50) **Mismatch:** a second link, photograph a different item. Show the VOIDED banner, and that the buyer was never charged.
6. (2:30) Unclear photo: show HOLD and "no money has moved", plus the retake instruction.

Use real photos of a real item for the video. The images in `examples/photos/` are drawings, generated by `examples/make_photos.py` for the offline path.

## Money safety

- **Never auto-settle on low confidence.** `policy.decide` captures only if the verdict is `met`, confidence is at least `JANUS_MIN_CONFIDENCE` (0.85) and every critical requirement was checked and met. It voids only if the verdict is `not_met`, confidence is high and a critical requirement clearly failed. Anything else, including a verdict that contradicts its own per-requirement checks, is a HOLD. This is plain code, unit-tested in `tests/test_policy.py`.
- **Humans on a HOLD.** The buyer can release (capture) and the seller can refund (void). Neither can take money from the other. The seller's controls need the private seller link.
- **Authorization expiry.** Before every verification and settlement, Janus reads the authorization from PayPal. If it is `EXPIRED` (PayPal authorizations last 29 days), the threshold goes to `expired` and nothing is captured. Pages re-check at most once a minute. `python -m janus sweep` checks every open threshold. After the 3-day honor period the page warns that a capture may fail.
- **Failed captures.** Network errors, 429 and 5xx leave the threshold in `capturing`/`voiding` with the error recorded. `python -m janus settle <id>` (or *Retry now* on the page) retries without re-running the AI. A capture PayPal rejects outright (e.g. `INSTRUMENT_DECLINED`) goes to `held` with the reason, and no money has moved. `AUTHORIZATION_EXPIRED`, `AUTHORIZATION_VOIDED` and `AUTHORIZATION_ALREADY_CAPTURED` are reconciled to the true end state.
- **Idempotency.** Every create/authorize/capture/void sends a deterministic `PayPal-Request-Id` (`janus-capture-<authorization id>`, ...), so a retry replays PayPal's original answer instead of acting twice. Authorizing twice (the return URL hit twice) is a no-op. If Janus crashes after PayPal authorized but before saving, the next attempt adopts the existing authorization. One mutation per threshold runs at a time.
- **Sandbox negative testing.** `JANUS_PAYPAL_NEGATIVE_TEST=capture=INSTRUMENT_DECLINED` sends PayPal's `PayPal-Mock-Response` header on captures, so you can watch the failure path against the real sandbox. The mock honours the same setting.

## CLI

```bash
python -m janus create --listing examples/listing.json        # or --title --price --condition --photo [--description]
python -m janus pay <id>                                       # order + buyer link + PayPal approval URL
python -m janus authorize <id> --wait 600                      # after the buyer approves
python -m janus evidence <id> --photo examples/photos/received_match.jpg --verify
python -m janus verify <id>                                    # re-run on submitted evidence
python -m janus release <id>    |  refund <id>                 # human decisions while funds are held
python -m janus status <id>     |  settle <id>  |  sweep       # live PayPal status, retry, expiry check
python -m janus show <id> [--json]  |  list
```

## HTTP API

All endpoints are listed at the top of `janus/server.py`. Errors are JSON: 400 bad input, 403 not the seller, 409 step not allowed in the current state, 502 PayPal or AI failure.

```bash
curl -s localhost:8000/api/thresholds -d '{"title":"Red mug","price":"24","condition":"Pay only if it matches the photo","photo_b64":"<base64>"}'
curl -s -X POST localhost:8000/api/t/<id>/payment                 # -> approve_url (buyer approves on PayPal)
curl -s localhost:8000/api/t/<id>/evidence -d '{"photo_b64":"<base64>","note":"arrived"}'   # -> captured | voided | held
curl -s localhost:8000/api/t/<id>
```

## State machine

```
created -> payment_pending -> authorized -> evidence_submitted -> capturing -> captured
                                  |                |         \-> voiding   -> voided
                                  |                \-> held -> (new photo | buyer release | seller refund)
                                  \-> expired  (from any state that holds funds)
```

Every change goes through `janus/state.py`, and out-of-order calls raise `TransitionError`.

## Layout

```
janus/policy.py   verdict -> capture / void / hold (the rule that moves money)
janus/ai.py       condition compiler + verifier (Claude vision, offline mock)
janus/service.py  one method per step; settlement, expiry, reconciliation
janus/paypal.py   Orders v2 sandbox client + offline mock with PayPal's error names
janus/state.py    state machine
janus/server.py   phone-friendly pages + JSON API (stdlib only)
janus/cli.py      CLI + `demo`
janus/store.py    JSON file store (data/thresholds/<id>/), audit log in data/janus.log.jsonl
examples/         listing.json, demo photos and the script that draws them
```

## Limits

- Sandbox only, one use case (item vs listing). No accounts. Anyone with the buyer link can act as the buyer.
- The buyer supplies the evidence, so a buyer could photograph a different item to trigger a void. Janus reduces this (the photo must plausibly be the delivered item, and unclear goes to HOLD), but it is not fraud-proof.
- Janus does not reauthorize after the honor period, and does not partially capture.

## License

MIT, see `LICENSE`.
