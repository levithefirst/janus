# Eid

Eid is an AI acceptance-and-release agent for local service jobs.

## Core flow

1. The provider creates a job from plain text.
2. AI turns it into a structured job card with a checklist.
3. The customer receives a link and authorizes payment via PayPal Orders v2 (`AUTHORIZE`).
4. The provider uploads evidence.
5. AI checks evidence completeness against the checklist and produces an acceptance packet.
6. The customer approves → `CAPTURE`, or requests a fix / dispute → `VOID` if needed.
