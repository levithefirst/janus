# Calibration report (claude)

**12 cases. This is a small, synthetic set**: it catches regressions and gross
miscalibration; it does not certify accuracy. Mock-AI numbers say nothing about Claude.

- Accuracy: 100%
- Unsafe settlements at bar 0.85, bare model: 0 (capture when not met, or void when not clearly failed)
- Unsafe settlements after the quality gate (the real pipeline): **0**
- Clear cases the quality gate held: 0
- Needless holds on clear cases: 5
- Brier score: 0.2394 (0 is perfect)  |  ECE (3 bins): 0.3875

| Confidence | n | Accuracy | Mean confidence |
|---|---|---|---|
| 0.00-0.50 | 2 | 100% | 0.00 |
| 0.50-0.85 | 6 | 100% | 0.63 |
| 0.85-1.00 | 4 | 100% | 0.89 |

| Case | Label | Verdict | Confidence | Action | Gate | |
|---|---|---|---|---|---|---|
| match | met | met | 0.60 | hold | pass | hold |
| match-rot8 | met | met | 0.60 | hold | pass | hold |
| match-crop80 | met | met | 0.65 | hold | pass | hold |
| match-dim | met | met | 0.60 | hold | pass | hold |
| match-mirror | met | met | 0.65 | hold | pass | hold |
| mismatch | not_met | not_met | 0.85 | void | pass | ok |
| mismatch-rot8 | not_met | not_met | 0.85 | void | pass | ok |
| wrong-colour | not_met | not_met | 0.95 | void | pass | ok |
| wrong-colour-dim | not_met | not_met | 0.90 | void | pass | ok |
| greyscale | uncertain | uncertain | 0.70 | hold | pass | ok |
| blurred | uncertain | uncertain | 0.00 | hold | blocked | ok |
| dark-blurry | uncertain | uncertain | 0.00 | hold | blocked | ok |
