# Calibration report (claude)

**12 cases. This is a small, synthetic set**: it catches regressions and gross
miscalibration; it does not certify accuracy. Mock-AI numbers say nothing about Claude.

- Accuracy: 100%
- Unsafe settlements at bar 0.85, bare model: 0 (capture when not met, or void when not clearly failed)
- Unsafe settlements after the quality gate (the real pipeline): **0**
- Clear cases the quality gate held: 0
- Needless holds on clear cases: 4
- Brier score: 0.0352 (0 is perfect)  |  ECE (3 bins): 0.165

| Confidence | n | Accuracy | Mean confidence |
|---|---|---|---|
| 0.50-0.85 | 6 | 100% | 0.77 |
| 0.85-1.00 | 6 | 100% | 0.90 |

| Case | Label | Verdict | Confidence | Action | Gate | |
|---|---|---|---|---|---|---|
| match | met | met | 0.80 | hold | pass | hold |
| match-rot8 | met | met | 0.80 | hold | pass | hold |
| match-crop80 | met | met | 0.82 | hold | pass | hold |
| match-dim | met | met | 0.80 | hold | pass | hold |
| match-mirror | met | met | 0.85 | capture | pass | ok |
| mismatch | not_met | not_met | 0.95 | void | pass | ok |
| mismatch-rot8 | not_met | not_met | 0.95 | void | pass | ok |
| wrong-colour | not_met | not_met | 0.90 | void | pass | ok |
| wrong-colour-dim | not_met | not_met | 0.90 | void | pass | ok |
| greyscale | uncertain | uncertain | 0.60 | hold | pass | ok |
| blurred | uncertain | uncertain | 0.80 | hold | blocked | ok |
| dark-blurry | uncertain | uncertain | 0.85 | hold | blocked | ok |
