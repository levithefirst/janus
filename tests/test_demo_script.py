"""The two-run demo script stays runnable offline and ends where the README says it does."""
import sys

from conftest import ROOT


def test_demo_runs_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JANUS_DATA_DIR", str(tmp_path / "data"))
    for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "ANTHROPIC_API_KEY", "DATABASE_URL", "DATABASE_URL_UNPOOLED",
              "JANUS_PAYPAL", "JANUS_AI", "JANUS_STORE"):
        monkeypatch.delenv(k, raising=False)
    sys.path.insert(0, str(ROOT / "scripts"))
    import demo_jobs
    assert demo_jobs.main(["--wait", "0"]) == 0
    out = capsys.readouterr().out
    run1, run2 = out.split("Run 2:")
    assert "outcome: partially_captured (partial); PayPal authorization now VOIDED" in run1 and "80.00 partial" in run1
    assert "refused" in run2 and "reused_photo" in run2 and "outcome: voided (void)" in run2
    assert out.count("chain intact") == 2
