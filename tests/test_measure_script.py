"""The Sandbox measurement script, against the offline mock: it records observations and claims nothing."""
import json
import sys
from datetime import timedelta

from conftest import ROOT
from janus import clock
from janus.paypal import PayPalMock

sys.path.insert(0, str(ROOT / "scripts"))
import measure_sandbox as m  # noqa: E402


class AutoApprove(PayPalMock):
    """The mock approves on get_order; the script authorizes directly, so approve on create."""
    def create_order(self, *a, **k):
        o = super().create_order(*a, **k)
        self.get_order(a[0], o["order_id"])
        return o


def test_reauth_protocol_records_each_attempt(tmp_path, monkeypatch):
    from janus.logs import setup
    setup(tmp_path)
    t0 = clock.utcnow()
    with clock.frozen(t0) as travel:
        monkeypatch.setattr(m, "now", clock.utcnow)
        pp, rec = AutoApprove(tmp_path / "pp"), m.Recorder(tmp_path / "log.jsonl", tmp_path / "state.json")
        m.reauth_start(pp, rec)
        m.reauth_authorize(pp, rec)
        early = m.reauth_try(pp, rec)
        assert early[-1]["ok"] is False and early[-1]["issue"] == "REAUTHORIZATION_TOO_SOON"
        travel.advance(timedelta(days=4))
        later = m.reauth_try(pp, rec)
        assert later[0]["ok"] is True and later[0]["new_authorization_id"]
        assert later[-1]["ok"] is False   # the mock refuses again at once; a live run shows what PayPal does
    lines = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert [x["step"] for x in lines][:2] == ["start", "authorize"] and all("at" in x for x in lines)


def test_request_id_protocol_reports_replay(tmp_path):
    from janus.logs import setup
    setup(tmp_path)
    pp, rec = PayPalMock(tmp_path / "pp"), m.Recorder(tmp_path / "log.jsonl", tmp_path / "state.json")
    m.request_id_start(pp, rec)
    obs = m.request_id_check(pp, rec)
    assert obs["replayed"] is True and obs["returned_order_id"] == obs["original_order_id"]


def test_script_refuses_without_sandbox_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert m.main(["request-id-start"]) == 2
