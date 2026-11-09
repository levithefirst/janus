from conftest import authorized, with_exif
from janus.packet import build


def test_packet_shows_reasoning_per_item_and_escapes_notes(janus):
    t = authorized(janus)
    t = janus.submit_and_verify(t.id, with_exif("received_match.jpg"), note="**bold** [link](http://x) | pipe")
    md = build(t)
    for r in t.compiled.requirements:
        line = next(x for x in md.splitlines() if x.startswith(f"| {r.id} "))
        obs = next(c.observation for c in t.last_verdict.verdict.checks if c.requirement_id == r.id)
        assert "met" in line and obs.split("(")[0].strip()[:10] in line
    assert "Bar to move money: 0.85" in md and "**CAPTURE**" in md
    assert "\\*\\*bold\\*\\*" in md and "\\[link\\]" in md and "\\|" in md
    assert "auto-capture on evidence from buyer" in md and f"request janus-{t.id}-capture-1" in md
