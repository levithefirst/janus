# What the confidence score means

## The number

`Verdict.confidence` is the verifier's own estimate, from 0.0 to 1.0, of the probability that its
**overall verdict** (`met`, `not_met` or `uncertain`) is correct. `RequirementCheck.confidence`, when
present, is the same estimate for one checklist item.

It is **self-reported by the model**. It is not a frequency measured on Janus traffic. Large language
models are often overconfident, and nothing makes 0.9 mean "right 9 times out of 10" until it has
been measured. Treat it as a ranking signal that has been sanity-checked, not as a probability you
can bill against.

## How Janus uses it

Money moves only when every one of these holds (see `policy.py` and `rules.py`):

1. Confidence is at or above the bar: `JANUS_MIN_CONFIDENCE` (default 0.85) **plus** an evidence
   trust penalty. No EXIF: +0.05. EXIF time with no zone near the authorization: +0.02. GPS far from
   the delivery point, or inconsistent across photos: +0.10. The bar never goes above 0.99, and
   present, plausible EXIF never lowers it.
2. The verdict agrees with its own per-requirement checks (a `met` verdict with a failed or
   unchecked critical requirement is a HOLD).
3. The evidence passed the quality gate and the integrity checks (reuse, capture time, notes).
4. The hard rules pass (consent, no capture after void or expiry, and so on).

Confidence is therefore one gate among several, never the only one. Below the bar, the result is
always HOLD: no capture, no void.

For itemized thresholds each item is settled on its own confidence against the same bar.

## The calibration harness

`python -m janus calibrate` runs the verifier over `calibration/labels.json` and writes
`calibration/report-<mode>.md`. It reports:

- **Unsafe settlements:** a capture when the label is not `met`, or a void when it is not `not_met`.
  Reported for the bare model and after the quality gate (the real pipeline). This is the number
  the harness exists for. The command exits with status 2 if the pipeline number is above 0.
- Accuracy per confidence bucket (0 to 0.5, 0.5 to the bar, the bar to 1), the Brier score and the
  expected calibration error (ECE) over those 3 bins.
- Needless holds on clear cases: the cost of caution.

### It is small. Plainly.

The set has **12 cases**, built from **3 synthetic drawings** of a mug with simple transforms
(rotation, crop, lighting, mirror, wrong colour, greyscale, blur). That is enough to catch a
regression or gross miscalibration. It is nowhere near enough to state an accuracy or a calibration
curve with any confidence interval worth printing. With 12 cases one mistake moves accuracy by 8
points.

It has already earned its place once: its first run found that a heavily blurred photo passed the
quality gate (a bug in the blur measure, fixed in `45901ea`).

### Current results

- **Mock AI** (`calibration/report-mock.md`): 0 unsafe settlements after the gate. The mock compares
  colours, so this says nothing about Claude.
- **Claude:** not run yet. It needs `ANTHROPIC_API_KEY`. Run `JANUS_AI=claude python -m janus calibrate`
  and commit `calibration/report-claude.md`.

### What a real calibration needs

- At least a few hundred real phone photos of real items, labelled by two people independently, with
  disagreements resolved or dropped.
- Hard cases on purpose: near-duplicates, the same model in another colour, a scratch the condition
  rules out, bad light, partial views.
- Measure per category, refit the bar (or a mapping from raw confidence to a calibrated probability)
  on one split and report on another.
