"""ClaudeAI against a fake Anthropic client: checks request parameters and refusal handling."""
from types import SimpleNamespace

import pytest

from eid.ai import AIError, ClaudeAI, MockAI


class FakeMessages:
    def __init__(self, parsed=None, stop_reason="end_turn", text="# Acceptance packet: x"):
        self.parsed, self.stop_reason, self.text, self.calls = parsed, stop_reason, text, []

    def _resp(self, **extra):
        return SimpleNamespace(model="claude-opus-5-5", stop_reason=self.stop_reason, stop_details=None,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=20), **extra)

    def parse(self, **kw):
        self.calls.append(kw)
        return self._resp(parsed_output=self.parsed)

    def create(self, **kw):
        self.calls.append(kw)
        return self._resp(content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=self.text)])


def fake_client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def test_job_card_request(tmp_path):
    from eid.logs import setup
    setup(tmp_path)
    card = MockAI().build_job_card("Paint the fence. Price $90.")
    msgs = FakeMessages(parsed=card)
    ai = ClaudeAI(client=fake_client(msgs))
    assert ai.build_job_card("Paint the fence. Price $90.") == card
    kw = msgs.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_format"].__name__ == "JobCard"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert "Paint the fence" in kw["messages"][0]["content"]


def test_refusal_raises(tmp_path):
    from eid.logs import setup
    setup(tmp_path)
    ai = ClaudeAI(client=fake_client(FakeMessages(stop_reason="refusal")))
    with pytest.raises(AIError, match="declined"):
        ai.build_job_card("x")


def test_packet_returns_text_blocks_only(tmp_path):
    from eid.logs import setup
    setup(tmp_path)
    mock = MockAI()
    card = mock.build_job_card("Paint the fence. Price $90.")
    report = mock.check_evidence(card, [], tmp_path)
    ai = ClaudeAI(client=fake_client(FakeMessages()))
    assert ai.build_packet("job_1", card, "90.00", "USD", [], report) == "# Acceptance packet: x"
