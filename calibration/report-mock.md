# Calibration report (mock)

**12 cases. This is a small, synthetic set**: it catches regressions and gross
miscalibration; it does not certify accuracy. Mock-AI numbers say nothing about Claude.

- Accuracy: 92%
- Unsafe settlements at bar 0.85, bare model: 1 (capture when not met, or void when not clearly failed)
- Unsafe settlements after the quality gate (the real pipeline): **0**
- Clear cases the quality gate held: 0
- Needless holds on clear cases: 0
- Brier score: 0.1464 (0 is perfect)  |  ECE (3 bins): 0.1642

| Confidence | n | Accuracy | Mean confidence |
|---|---|---|---|
| 0.00-0.50 | 2 | 100% | 0.35 |
| 0.85-1.00 | 10 | 90% | 0.97 |

| Case | Label | Verdict | Confidence | Action | Gate | |
|---|---|---|---|---|---|---|
| match | met | met | 0.97 | capture | pass | ok |
| match-rot8 | met | met | 0.97 | capture | pass | ok |
| match-crop80 | met | met | 0.97 | capture | pass | ok |
| match-dim | met | met | 0.96 | capture | pass | ok |
| match-mirror | met | met | 0.97 | capture | pass | ok |
| mismatch | not_met | not_met | 0.97 | void | pass | ok |
| mismatch-rot8 | not_met | not_met | 0.97 | void | pass | ok |
| wrong-colour | not_met | not_met | 0.97 | void | pass | ok |
| wrong-colour-dim | not_met | not_met | 0.97 | void | pass | ok |
| greyscale | uncertain | uncertain | 0.35 | hold | pass | ok |
| blurred | uncertain | met | 0.95 | capture | blocked | UNSAFE |
| dark-blurry | uncertain | uncertain | 0.35 | hold | blocked | ok |
