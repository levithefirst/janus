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
   the delivery point, or inconsistent across photos: a hard fail (hold, the model is not called). The bar never goes above 0.99, and
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
- **Live Claude on 12 synthetic cases** (`calibration/report-claude.md`, model `claude-opus-5-5`,
  run 2026-11-09, bar 0.85):
  - Accuracy: 12 of 12 (100%).
  - Unsafe money movements: **0** for the bare model and **0** after the quality gate.
  - Brier score: 0.0352. ECE (3 bins): 0.165.
  - Needless holds on clear cases: 4. Four of the five correct `met` cases came back at 0.80 to 0.82,
    below the bar, so they were held instead of captured. Claude was correct but under-confident on
    matches here, which is the safe direction.
  - The quality gate blocked both degraded photos (`blurred`, `dark-blurry`) even though Claude gave them
    0.80 and 0.85 as `uncertain`.

  | Confidence | n | Accuracy | Mean confidence |
  |---|---|---|---|
  | 0.00-0.50 | 0 | n/a | n/a |
  | 0.50-0.85 | 6 | 100% | 0.77 |
  | 0.85-1.00 | 6 | 100% | 0.90 |

  With 12 synthetic cases these numbers catch regressions only. They are not an accuracy claim.

- **Live Claude, verify-v2 (per-item answers, short output), 12 synthetic cases** (branch `cost-cuts`,
  run 2026-11-09, `claude-opus-5-5`, photos at most 1024 px, bar 0.85):
  - Accuracy 12 of 12. Unsafe money movements: 0 (bare model and after the gate).
  - Brier 0.2394, ECE 0.3875. Needless holds: 5 of 5 matches held (confidence 0.60 to 0.65), worse
    than the v1 prompt (4 of 5). Code takes the lowest required-item confidence, and the model is unsure
    about "no chips or cracks" from a single front photo.
  - The two gate-blocked cases no longer call the model.
  - At 768 px the same set scored 7 of 12 (all five matches became uncertain), so 1024 px stays.

### What a real calibration needs

- At least a few hundred real phone photos of real items, labelled by two people independently, with
  disagreements resolved or dropped.
- Hard cases on purpose: near-duplicates, the same model in another colour, a scratch the condition
  rules out, bad light, partial views.
- Measure per category, refit the bar (or a mapping from raw confidence to a calibrated probability)
  on one split and report on another.
