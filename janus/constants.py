"""Every money and time limit in one place. All times are UTC."""
from datetime import timedelta

# PayPal authorization lifetime (Orders v2, intent=AUTHORIZE).
HONOR_DAYS = 3            # funds are guaranteed for this long after authorization
AUTH_EXPIRY_DAYS = 29     # after this an authorization can no longer be captured

HONOR_PERIOD = timedelta(days=HONOR_DAYS)
AUTH_VALIDITY = timedelta(days=AUTH_EXPIRY_DAYS)

# Scheduler margins.
WARN_BEFORE_HONOR = timedelta(hours=12)        # warn this long before the honor period ends
VOID_BEFORE_EXPIRY = timedelta(hours=24)       # void this long before day 29, so we never hit expiry mid-capture

# Reauthorization. PayPal allows reauthorizing an authorization after the honor period, with limits
# on how often and by how much. The exact numbers must be confirmed against the live Sandbox.
REAUTHORIZE_MAX_COUNT = None      # TODO: confirm PayPal's limit on reauthorizations per authorization
REAUTHORIZE_MAX_PERCENT = None    # TODO: confirm the maximum amount increase allowed on reauthorize
REAUTHORIZE_EARLIEST = HONOR_PERIOD  # TODO: confirm the earliest reauthorize time (assumed: after day 3)
REAUTHORIZE_ATTEMPTS_ALLOWED = 1  # our own conservative cap until the TODOs above are confirmed

# Confidence below this never moves money.
DEFAULT_MIN_CONFIDENCE = 0.85

# Security.
TOKEN_BYTES = 32                  # capability tokens: 256-bit HMAC, never fewer than 128 bits
PHOTO_URL_TTL = timedelta(minutes=5)
