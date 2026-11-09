"""Every money and time limit in one place. All times are UTC."""
from datetime import timedelta

# PayPal authorization lifetime (Orders v2, intent=AUTHORIZE).
# Source: https://developer.paypal.com/docs/checkout/standard/customize/authorization/
#   "A hold on the money that is valid for 29 days ... capture the payment within the 3-day honor period."
HONOR_DAYS = 3            # funds are guaranteed for this long after authorization (and after each reauthorization)
AUTH_EXPIRY_DAYS = 29     # after this an authorization can no longer be captured

HONOR_PERIOD = timedelta(days=HONOR_DAYS)
AUTH_VALIDITY = timedelta(days=AUTH_EXPIRY_DAYS)

# Scheduler margins.
WARN_BEFORE_HONOR = timedelta(hours=12)        # warn this long before the honor period ends
VOID_BEFORE_EXPIRY = timedelta(hours=24)       # void this long before day 29, so we never hit expiry mid-capture

# Reauthorization.
# Source: Payments v2 "Reauthorize authorized payment"
#   https://developer.paypal.com/docs/api/payments/v2/#authorizations_reauthorize
#   (text also in PayPal's official OpenAPI spec:
#   https://github.com/paypal/paypal-rest-api-specifications/blob/main/openapi/payments_payment_v2.json)
#   "You can reauthorize an authorized payment from 4 to 29 days after the 3-day honor period. The allowed
#   amount depends on context and geography, for example in US it is up to 115% of the original authorized
#   amount, not to exceed an increase of $75 USD." A reauthorization gets a new id and a new 3-day honor
#   period, but not a new 29 days: after 29 (the spec says 30) days from the ORIGINAL authorization you must
#   create a new authorization instead.
# TODO(sandbox): PayPal's docs conflict on how many reauthorizations are allowed. The checkout guide and one
#   description in the spec say "multiple re-authorizations" within the 29 days; another description in the
#   same spec says "only once from days four to 29". Sources:
#     https://developer.paypal.com/docs/api/payments/v2/#authorizations_reauthorize
#     https://github.com/paypal/paypal-rest-api-specifications/blob/main/openapi/payments_payment_v2.json
#   Until a live Sandbox run settles it, we allow exactly one. This is the only place the number lives.
MAX_REAUTHORIZATIONS = 1
REAUTHORIZE_MAX_PERCENT = 115            # US example in the docs; other regions may differ
REAUTHORIZE_MAX_INCREASE_USD = "75.00"   # US example in the docs; cap on the increase, not the total
REAUTHORIZE_EARLIEST = HONOR_PERIOD      # docs: reauthorize only after the 3-day honor period expires

# Void. Source: Payments v2 "Void authorized payment" (same links as above):
#   "You cannot void an authorized payment that has been fully captured."
# The docs do not say in words whether the remainder can be voided after a PARTIAL capture (final_capture
# false). TODO(sandbox): confirm. Janus treats it as allowed but re-reads PayPal state before and after.

# After capture (we do not act on these yet; recorded so the limits are in one place).
# Refund: "You can send a full or partial refund within 180 days of the payment date."
#   https://www.paypal.com/us/cshelp/article/how-do-i-issue-a-refund-help101
REFUND_WINDOW_DAYS = 180
# Buyer disputes (US): Item Not Received within 180 days of payment; Significantly Not as Described within
#   30 days of delivery or 180 days of payment, whichever is sooner.
#   https://www.paypal.com/us/cshelp/article/dispute-filing-timeframes-help1173
DISPUTE_WINDOW_DAYS = 180
DISPUTE_SNAD_AFTER_DELIVERY_DAYS = 30
# Card chargeback windows are set by card networks, not PayPal docs. TODO: not stated in PayPal developer docs.

# PayPal-Request-Id retention. Source: https://developer.paypal.com/reference/guidelines/idempotency/
#   "You can retry idempotent calls ... for as long as the server stores the ID" and "for information about
#   how long the server stores the ID, see the reference for your API". The Payments v2 reference does not
#   state a period. TODO(sandbox): unknown. Retries must happen promptly; reconcile also re-reads PayPal state.
REQUEST_ID_RETENTION = None

# Webhook event names (Payments v2). Source: https://developer.paypal.com/api/rest/webhooks/event-names/
WEBHOOK_EVENTS = (
    "PAYMENT.AUTHORIZATION.CREATED",
    "PAYMENT.AUTHORIZATION.VOIDED",   # manual void, or the authorization reached the end of its validity
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.PENDING",
    "PAYMENT.CAPTURE.DECLINED",
    "PAYMENT.CAPTURE.REFUNDED",
    "PAYMENT.CAPTURE.REVERSED",
    "CHECKOUT.ORDER.APPROVED",
    "CHECKOUT.PAYMENT-APPROVAL.REVERSED",
    "CUSTOMER.DISPUTE.CREATED",
    "CUSTOMER.DISPUTE.UPDATED",
    "CUSTOMER.DISPUTE.RESOLVED",
)

# Confidence below this never moves money.
DEFAULT_MIN_CONFIDENCE = 0.85

# Security.
TOKEN_BYTES = 32                  # capability tokens: 256-bit HMAC, never fewer than 128 bits
PHOTO_URL_TTL = timedelta(minutes=5)
