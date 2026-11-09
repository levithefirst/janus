# Hard questions

The hardest questions a judge or product manager will ask, with honest answers. Where we have no data,
we say so. Sources are linked. See [ATTACKS.md](ATTACKS.md) for the full attack list and status.

## 1. Who holds the money? Do you need a money-transmitter or escrow license?

Janus never holds funds. The provider is the PayPal merchant. The customer's money sits in a PayPal
authorization on the customer's own payment method. Janus only tells PayPal to capture or void that
authorization, using the provider's API access. Funds go from customer to provider through PayPal and never
through a Janus account. That is the design that keeps us out of money transmission and escrow. It is our
reading, not legal advice: a lawyer must confirm it before launch, and it changes if Janus ever pays providers
itself (then KYC and licensing apply).
Sources: [PayPal authorize and capture](https://developer.paypal.com/docs/checkout/standard/customize/authorization/),
[FinCEN definitions, 31 CFR 1010.100](https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1010/subpart-A/section-1010.100).

## 2. Does Janus solve a problem existing escrow does not? Where is the loss data?

Loss data: Unknown, not yet measured. We have no survey or published figure for unpaid or disputed local
service jobs that we trust enough to quote. The next step is interviews and a small pilot that records how
often jobs end in a payment fight. What we can say: escrow services require both sides to sign up and wait
for a manual release; Janus is a link the provider sends, the customer pays with PayPal, and release is tied
to a written condition and evidence.

## 3. Thumbtack, Angi, Upwork, Escrow.com and PayPal already exist. Why you?

PayPal already offers authorize and capture; Janus is built on it, not against it. The gap we target is the
provider who does not want to join a marketplace and just wants a pay-on-proof link. A side-by-side
comparison of incumbent pay-on-completion features: Unknown, not yet measured (not yet researched with
sources). We will not claim a feature gap we have not checked.

## 4. Why would a provider accept a hold instead of cash now?

The hold proves the money is there before work starts, so the provider is not chasing payment afterwards.
The cost is waiting until the customer accepts. Whether providers accept that trade: Unknown, not yet
measured. It needs provider interviews.

## 5. The authorization is only good for 29 days, and the guarantee only for 3. What happens on long jobs?

PayPal holds the funds for 29 days and honors capture for 3 days. After day 3 you can reauthorize, which
gives a new 3-day honor period but never extends past the original 29 days. In the US a reauthorization can
be up to 115% of the original, with an increase of at most $75. Janus warns before day 3, reauthorizes once,
then asks the customer for a new authorization, and voids before day 29 so money is never stuck. Gap: the
"authorize again" step raises an alert but there is no re-payment flow yet. Jobs longer than 29 days need a
new authorization; Janus does not hide that.
Source: [PayPal authorize and capture](https://developer.paypal.com/docs/checkout/standard/customize/authorization/),
[Payments v2 reauthorize](https://developer.paypal.com/docs/api/payments/v2/#authorizations_reauthorize).

## 6. What if the AI is wrong?

The AI never moves money on its own judgment alone. Hard rules run immediately before every capture: a
recorded customer consent bound to the current terms, a confidence at or above the bar (default 0.85), no
capture after a void or expiry, and no capture while a dispute or amendment is open. Anything unclear is held
for more evidence or a human. The scheduler and webhooks can never capture. The customer can always release by
hand. Gap: confidence is calibrated on a 12-case fixture set, which is small; real error rates are Unknown,
not yet measured. See [CONFIDENCE.md](CONFIDENCE.md).

## 7. Can someone fool it with old, stock or edited photos, or prompt injection?

Partly mitigated. Janus checks perceptual hashes for reused images across jobs and against the listing,
checks EXIF time and GPS when present (missing EXIF lowers trust, never passes), runs a blur and resolution
gate, and treats notes and image text as untrusted data in the prompt, with injection tests. Gap: web uploads
often strip EXIF, so the time and place checks are weak for browser uploads. A determined forger with a fresh,
staged photo is not stopped by software alone.

Job mode adds deterministic checks on the provider's before and after photos (reuse across jobs, after
matching before, editor names in EXIF, inconsistent capture times) and asks the model whether before and after
show the same place and whether a photo looks edited. Any flag sends the job to manual review, and the customer
must override it, with a reason that is logged, to pay. On a 13-case synthetic eval with the mock AI: 1 false
accept out of 9 bad cases (a pixel edit with no editor tag) and 1 false reject out of 4 good cases. Those are
drawings, not real photos; real error rates: Unknown, not yet measured. See `eval/results-mock.md`.

## 8. What about refunds and disputes after the money is captured?

PayPal's own rules still apply after capture. A merchant can refund within 180 days of payment. A US buyer can
open an Item Not Received dispute within 180 days, or a Significantly Not as Described dispute within 30 days
of delivery or 180 days of payment, whichever is sooner. Inside Janus, a customer can open a dispute while
funds are held, which stops automatic settlement. Gap: post-capture refunds and PayPal dispute webhooks are not
wired into a product flow yet.
Sources: [PayPal refunds](https://www.paypal.com/us/cshelp/article/how-do-i-issue-a-refund-help101),
[PayPal dispute timeframes](https://www.paypal.com/us/cshelp/article/dispute-filing-timeframes-help1173).

## 9. How does Janus handle partial work?

In a job, the customer can pay part: they enter an amount, Janus captures it with final_capture false, then
voids the rest of the hold, and a crash between the two finishes with the void. In threshold mode, each
checklist item can carry an amount. Janus captures only the items verified as met, leaves the rest held,
and never captures the same item twice. Gap: an itemized job cannot be amended yet. Sandbox check (2026-11-09):
PayPal does allow voiding the remainder after a partial capture. On authorization 8TJ27215PX638500B ($10.00),
a $4.00 capture with final_capture false left status PARTIALLY_CAPTURED (capture 0CX971222E3797614,
COMPLETED); a following void returned 200 and the authorization became VOIDED.

## 10. What does a job cost, and who pays?

Cost per job = PayPal fee + AI cost + hosting.
PayPal fee (US, PayPal Checkout) = 3.49% of the amount + a fixed fee (0.49 USD for USD), paid by the provider
as the merchant. Source: [PayPal US merchant fees](https://www.paypal.com/us/business/paypal-business-fees).
AI cost = (photos per job) x (vision-model cost per photo). Per-photo cost: Unknown, not yet measured on real
jobs. Revenue model and price: not decided yet.

## 11. Why now?

Vision models can now check a photo against a written checklist and explain each item, which makes
pay-on-proof practical without a human reviewer on every job. That this is enough to change behavior:
Unknown, not yet measured.
