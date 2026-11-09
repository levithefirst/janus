import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eid.config import Config  # noqa: E402
from eid.service import Eid  # noqa: E402

EXAMPLES = ROOT / "examples"


def make_config(tmp_path: Path, **over) -> Config:
    base = dict(paypal_mode="mock", paypal_client_id="", paypal_client_secret="", ai_mode="mock",
                model="claude-opus-5-5", data_dir=tmp_path / "data", base_url="http://localhost:8000")
    base.update(over)
    return Config(**base)


@pytest.fixture
def eid(tmp_path):
    return Eid(make_config(tmp_path))


@pytest.fixture
def job_text():
    return (EXAMPLES / "job.txt").read_text()
