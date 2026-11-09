"""The Vercel entry point: path restoration and a request through the real `handler` class."""
import importlib
import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from conftest import ROOT


def test_restore_path():
    sys.path.insert(0, str(ROOT))
    from api.index import restore_path
    assert restore_path("/api/index?__path=/app/t/th_1&t=tok") == "/app/t/th_1?t=tok"
    assert restore_path("/api/index?__path=/app") == "/app"
    assert restore_path("/api/index?__path=paypal/webhook") == "/paypal/webhook"
    assert restore_path("/app/t/x?t=1") == "/app/t/x?t=1"
    assert restore_path("/api/index?__path=/app/t/x&t=a%2Bb") == "/app/t/x?t=a%2Bb"


def test_handler_serves_the_app_through_the_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JANUS_SECRET", "s" * 40)
    monkeypatch.setenv("JANUS_BASE_URL", "https://usejanus.vercel.app/app")
    monkeypatch.delenv("JANUS_MOUNT", raising=False)
    for k in ("PAYPAL_CLIENT_ID", "ANTHROPIC_API_KEY", "DATABASE_URL", "DATABASE_URL_UNPOOLED", "JANUS_PAYPAL",
              "JANUS_AI", "JANUS_STORE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(ROOT))
    mod = importlib.reload(importlib.import_module("api.index"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        html = urllib.request.urlopen(base + "/api/index?__path=/app/").read().decode()
        assert "Write the checklist" in html
        req = urllib.request.Request(base + "/api/index?__path=/app/api/jobs", method="POST",
                                     data=json.dumps({"title": "Fix tap", "price": "30",
                                                      "description": "Replace the washer. Stop the drip."}).encode())
        d = json.loads(urllib.request.urlopen(req).read())
        assert d["provider_link"].startswith("https://usejanus.vercel.app/app/t/" + d["id"] + "?k=")
    finally:
        httpd.shutdown()
