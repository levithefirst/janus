# Eval results: AI mock

13 synthetic cases (drawings, not photographs). Small: this shows which defences fire on these
cases; it is not a measurement of real-world error rates.

- **False accepts: 1 of 9** bad cases came out clean (capturable with no override)
- **False rejects: 1 of 4** good cases were flagged or held
- Spurious reuse flags: 2 photos flagged as reused that were not

| Category | n | False accept | False reject |
|---|---|---|---|
| good | 4 | 0 | 1 |
| reused | 3 | 0 | 0 |
| wrong_room | 3 | 0 | 0 |
| edited | 3 | 1 | 0 |

| Case | Expected | Got | State | Recommendation | Confidence | Scene | Edit | Blocking flags | Spurious reuse |
|---|---|---|---|---|---|---|---|---|---|
| good-hole | clean | clean | packet_ready | recommend_accept | 0.90 | same | none | - | - |
| good-shelf | clean | clean | packet_ready | recommend_accept | 0.90 | same | none | - | - |
| good-outlet | clean | flag (FALSE REJECT) | manual_review | review | 0.80 | same | none | - | - |
| good-no-exif | clean | clean | packet_ready | recommend_accept | 0.90 | same | none | - | - |
| reused-cross-job | flag | flag | manual_review | recommend_accept | 0.90 | same | none | reused_photo | 1 |
| reused-before | flag | flag | manual_review | review | 0.80 | same | none | after_matches_before | - |
| reused-cropped | flag | flag | manual_review | recommend_accept | 0.90 | same | none | reused_photo | - |
| wrong-room-1 | flag | flag | manual_review | review | 0.30 | unclear | none | reused_photo | 1 |
| wrong-room-2 | flag | flag | manual_review | review | 0.30 | different | none | wrong_room | - |
| wrong-room-3 | flag | flag | manual_review | review | 0.30 | unclear | none | - | - |
| edited-photoshop | flag | flag | manual_review | recommend_accept | 0.90 | same | likely | edited_software, likely_edited | - |
| edited-snapseed | flag | flag | manual_review | recommend_accept | 0.90 | same | likely | edited_software, likely_edited | - |
| edited-pasted | flag | clean (FALSE ACCEPT) | packet_ready | recommend_accept | 0.90 | same | none | - | - |
