import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from janus.config import Config  # noqa: E402
from janus.service import Janus  # noqa: E402

EXAMPLES = ROOT / "examples"
PHOTOS = EXAMPLES / "photos"
LISTING = dict(title="Red ceramic mug with white star", price="24",
               description="12 oz glossy red ceramic mug. White star printed on the front. No chips or cracks.",
               condition="Pay only if this item matches the listing photo and description.")


def make_config(tmp_path: Path, **over) -> Config:
    base = dict(paypal_mode="mock", paypal_client_id="", paypal_client_secret="", ai_mode="mock",
                model="claude-opus-5-5", data_dir=tmp_path / "data", base_url="http://localhost:8000")
    base.update(over)
    return Config(**base)


@pytest.fixture
def janus(tmp_path):
    return Janus(make_config(tmp_path))


def new_threshold(j):
    return j.create(photos=[PHOTOS / "listing_red_mug.jpg"], **LISTING)


def authorized(j):
    t = new_threshold(j)
    j.consent(t.id, channel="api")
    j.create_payment(t.id)
    return j.authorize(t.id)
