"""Vercel entry point: the whole Janus app as one Python serverless function.

vercel.json rewrites /app/*, /paypal/* to this function and passes the original path as `__path`, so the
router sees the same URL it would see behind `python -m janus serve`. Persistent state lives in Postgres
(DATABASE_URL_UNPOOLED / DATABASE_URL); the function's own disk is only /tmp, used as a read cache.
"""
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("JANUS_DATA_DIR", "/tmp/janus")
os.environ.setdefault("JANUS_MOUNT", "/app")

from janus.server import make_handler  # noqa: E402
from janus.service import Janus  # noqa: E402

_Base = make_handler(Janus.from_env())


def restore_path(raw: str) -> str:
    """'/api/index?__path=/app/t/x&t=tok' -> '/app/t/x?t=tok'. Anything else is returned unchanged."""
    url = urlparse(raw)
    params = parse_qsl(url.query, keep_blank_values=True)
    original = next((v for k, v in params if k == "__path"), None)
    if original is None:
        return raw
    if not original.startswith("/"):
        original = "/" + original
    rest = urlencode([(k, v) for k, v in params if k != "__path"])
    return original + (f"?{rest}" if rest else "")


class handler(_Base):  # Vercel's Python runtime looks for a class named `handler`
    def _dispatch(self, method: str):
        self.path = restore_path(self.path)
        return super()._dispatch(method)
