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


def with_exif(name, taken_at=None, offset="+00:00", gps=None, scale=1.0) -> bytes:
    """A copy of an example photo carrying EXIF capture time (default: now) and optional GPS."""
    import io

    from PIL import Image

    from janus import clock
    img = Image.open(PHOTOS / name)
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    ex = Image.Exif()
    when = taken_at or clock.utcnow()
    sub = {36867: when.strftime("%Y:%m:%d %H:%M:%S")}
    if offset:
        sub[36881] = offset
    ex[0x8769] = sub
    if gps:
        def dms(v):
            v = abs(v)
            return (float(int(v)), float(int((v % 1) * 60)), round(((v * 60) % 1) * 60, 2))
        ex[0x8825] = {1: "N" if gps[0] >= 0 else "S", 2: dms(gps[0]), 3: "E" if gps[1] >= 0 else "W", 4: dms(gps[1])}
    out = io.BytesIO()
    img.save(out, "JPEG", exif=ex, quality=90)
    return out.getvalue()


def make_config(tmp_path: Path, **over) -> Config:
    base = dict(paypal_mode="mock", paypal_client_id="", paypal_client_secret="", ai_mode="mock",
                model="claude-opus-5-5", data_dir=tmp_path / "data", base_url="http://localhost:8000",
                janus_secret="test-secret-" + "x" * 32)
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
