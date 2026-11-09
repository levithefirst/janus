# Attacks

Every attack we know of against Janus, how bad it is, and where it stands. See
[THREAT_MODEL.md](THREAT_MODEL.md) for actors and trust boundaries.

**Severity:** Critical (wrong money movement), High (money at risk or another party's data),
Medium (abuse, leakage or availability), Low (hardening).
**Status:** Mitigated (control in code, with a test), Partial (control exists but has a known gap),
Open (not addressed yet), Accepted (a deliberate trade-off, explained).

## Summary

| Status | Count |
|---|---|
| Mitigated | 35 |
| Partial | 8 |
| Open | 3 |
| Accepted | 3 |
| **Total** | **49** |

## Money movement

| # | Attack | Sev | Status | Control | Proof |
|---|---|---|---|---|---|
| M1 | Capture without the customer's consent (provider, bug, or insider triggers it) | Critical | Mitigated | `rules.check_capture` requires a consent bound to a hash of the current terms, or an explicit customer release; checked right before PayPal is called. Consent is only ever recorded by a deliberate POST (see M15) | `test_rules.py::test_check_capture_without_any_consent`, `test_capture_blocked_when_consent_is_for_other_terms`, `test_no_payment_without_consent`, `test_server.py::test_post_pay_records_consent_bound_to_terms` |
| M2 | Auto-settle on a low-confidence verdict | Critical | Mitigated | Policy holds below the bar; the rules re-check the bar independently, even if the policy is broken | `test_rules.py::test_below_threshold_always_holds`, `test_low_confidence_holds_even_if_policy_is_broken`, `test_threshold_edge_is_inclusive` |
| M3 | Capture after a void or an expiry | Critical | Mitigated | Rule 3 checks the void/expiry flag, PayPal status and the expiration time; the state machine has no edge out of voided/expired | `test_rules.py::test_no_capture_after_void`, `test_no_capture_after_expiry`, `test_no_capture_past_expiration_time_even_if_paypal_status_lags` |
| M4 | Change a job after it reached a terminal state | High | Mitigated | `Store.save` refuses any write once the stored state is terminal; every operation rejects terminal jobs | `test_atomic.py::test_terminal_threshold_cannot_be_saved`, `test_rules.py::test_terminal_threshold_rejects_every_operation` |
| M5 | Race: customer releases while the provider refunds | Critical | Mitigated | Per-job `flock` plus thread lock; compare-and-set version on every save | `test_atomic.py::test_concurrent_release_and_refund_settle_exactly_once`, `test_stale_write_is_rejected` |
| M6 | Double capture through a retry, timeout or crash | Critical | Mitigated | `PayPal-Request-Id` = `janus-<job>-<action>[-n]`; the exact request is saved before the call and replayed on recovery | `test_rules.py::test_crash_after_paypal_captured_before_save_recovers_once`, `test_crash_before_paypal_call_recovers_once`, `test_paypal_sandbox.py::test_mock_replays_same_request_id` |
| M7 | Capture the same checklist item twice | Critical | Mitigated | Rule refuses items not in `pending`; item status is updated with the capture | `test_product.py::test_item_rule_blocks_low_confidence_or_double_capture` |
| M8 | Silent customer leads to a capture (timeouts, auto-approve) | High | Mitigated | Scheduler and webhooks are refused by rule 5; provider evidence auto-captures only if the customer agreed to it; near expiry the scheduler voids | `test_scheduler.py::test_silent_customer_is_never_captured`, `test_product.py::test_provider_evidence_never_auto_captures_without_customer`, `test_rules.py::test_only_ai_path_or_customer_can_capture` |
| M9 | Bait and switch: the provider changes scope after the customer agreed | High | Mitigated | Amendments need a new customer consent; a pending amendment blocks auto-capture; never above the authorized amount | `test_product.py::test_pending_amendment_blocks_auto_capture`, `test_amendment_cannot_exceed_authorized_amount` |
| M10 | Provider forces a capture with their own evidence | High | Mitigated | Provider evidence never auto-captures unless the customer's consent names it | `test_product.py::test_provider_evidence_never_auto_captures_without_customer` |
| M11 | Customer forces a void by photographing a different item | High | Accepted | A void returns the customer's own money, so the system is biased toward it by design. Reuse, listing-copy and capture-time checks raise the bar; the provider can dispute and re-upload. A product decision, not a bug | `test_evidence.py` (reuse, EXIF), `test_product.py::test_dispute_*` |
| M12 | Authorization expires before the condition is settled (money stuck or capture fails) | Medium | Partial | Scheduler: warn before day 3, reauthorize up to `MAX_REAUTHORIZATIONS` (1) times, void before day 29. A second reauthorization is refused without calling PayPal and raises a high severity `new_authorization_needed` alert. Gap: the "ask the customer for a new authorization" step sets a flag and an alert, but there is no re-payment flow yet | `test_scheduler.py::test_timeline_warn_reauthorize_ask_void`, `test_second_reauthorization_is_refused_and_flagged` |
| M13 | Capture rejected by PayPal (declined instrument) leaves the job in limbo | Medium | Mitigated | Non-retryable rejection goes to HOLD with the reason; retryable errors stay pending and are retried with the same key | `test_flow.py::test_declined_capture_goes_to_hold`, `test_retryable_capture_failure_then_retry` |
| M14 | PayPal behaves differently from the mock (void after partial capture; reauthorize limits; how long a request id is honoured) | Medium | Open | Marked `TODO(sandbox)` in the mock and in `constants.py`. The reauthorization count is the single constant `MAX_REAUTHORIZATIONS = 1` because PayPal's docs conflict (multiple vs only once). Needs a live sandbox run | |
| M15 | Consent recorded by a GET: a link previewer, prefetcher, crawler or antivirus link scanner opens the buyer link and consents for the buyer | High | Mitigated | `GET /t/<id>/pay` only redirects to the page that shows the terms and records nothing. The Authorize button is a form POST carrying the hash of the terms shown; the server records consent for exactly those terms, then redirects to PayPal and sets the HttpOnly return cookie. A POST with stale terms is refused (409) and audited as `consent_refused` | `test_server.py::test_get_pay_records_nothing`, `test_post_pay_records_consent_bound_to_terms`, `test_post_pay_with_stale_terms_is_rejected` |
| M16 | Capture request with an amount of zero or less (itemized job with nothing pending) | Medium | Mitigated | `release()` refuses (or audits a no-op when every item is already captured) before recording consent; `_start` refuses a non-positive amount; `_settle` holds one instead of sending it. PayPal is never called | `test_product.py::test_release_with_nothing_pending_is_refused_without_calling_paypal`, `test_release_when_every_item_is_captured_is_an_audited_noop`, `test_capture_amount_zero_or_negative_never_reaches_paypal` |

## Webhooks

| # | Attack | Sev | Status | Control | Proof |
|---|---|---|---|---|---|
| W1 | Forged webhook | High | Mitigated | Sandbox: PayPal's verify-webhook-signature API with `PAYPAL_WEBHOOK_ID`; mock: HMAC. Unverified requests get 401 and an audit entry. Note: with `PAYPAL_WEBHOOK_ID` unset in sandbox, every webhook is refused | `test_webhooks.py::test_bad_signature_rejected_and_audited`, `test_paypal_sandbox.py::test_webhook_verification_calls_paypal` |
| W2 | Replayed or duplicated webhook | Medium | Mitigated | Event id claimed with `O_EXCL`; the claim is released if processing fails, so PayPal's retry still works | `test_webhooks.py::test_duplicates_are_dropped`, `test_concurrent_duplicates_processed_once`, `test_failed_processing_releases_the_claim` |
| W3 | Out-of-order webhooks roll state back | Medium | Mitigated | Events never set state; they trigger a read from PayPal and a forward-only reconcile | `test_webhooks.py::test_out_of_order_events_converge`, `test_late_event_after_terminal_is_ignored` |
| W4 | A webhook that claims a capture causes one | High | Mitigated | Reconcile only records what PayPal reports, and webhooks cannot capture (rule 5) | `test_webhooks.py::test_event_cannot_capture_by_claiming_it` |

## Evidence and AI

| # | Attack | Sev | Status | Control | Proof |
|---|---|---|---|---|---|
| E1 | Reuse a photo from another job | High | Partial | 64-bit dHash index across jobs; near-duplicates force a HOLD. Gap: a determined attacker can perturb a photo past dHash; the index is per host | `test_evidence.py::test_photo_reused_from_another_job_holds` |
| E2 | Submit the listing photo (or a stock photo of it) as evidence | High | Mitigated | Evidence is compared with the job's listing photos | `test_evidence.py::test_listing_photo_submitted_as_evidence_holds` |
| E3 | Old photo taken before the purchase | Medium | Partial | EXIF capture time before the authorization forces a HOLD. Gap: EXIF is easy to forge or strip; missing EXIF only raises the bar | `test_evidence.py::test_photo_taken_before_authorization_holds`, `test_missing_exif_raises_the_bar` |
| E4 | The web uploader strips EXIF (canvas resize), so web uploads always lack it | Medium | Open | Web uploads pay the no-EXIF penalty (bar 0.90). Fix needs a UI change to upload the original file, out of scope for this branch | |
| E5 | Photo from somewhere else (GPS) | Low | Partial | GPS far from a declared delivery location, or inconsistent across photos, raises the bar. Gap: GPS is optional and forgeable | `test_evidence.py::test_gps_far_from_delivery_raises_bar` |
| E6 | Blurry, dark or tiny photo gets a confident verdict | Medium | Mitigated | Quality gate holds before the AI is asked (bug in the blur measure found by calibration and fixed) | `test_evidence.py::test_blurry_dark_photo_holds_without_asking_ai`, `test_low_resolution_photo_holds`, `test_sharpness_ignores_the_unfiltered_border` |
| E7 | Prompt injection in the note | High | Mitigated | Notes are sanitised (angle brackets neutralised), fenced in `<note>`, and instruction-like notes go to a human | `test_evidence.py::test_instruction_like_notes_hold_for_a_human`, `test_note_cannot_break_out_of_its_tag_in_the_prompt`, `test_sanitize_note` |
| E8 | Prompt injection through text inside the photo | High | Partial | Prompt says image text is never an instruction and to flag it; a fooled verdict must still agree with its own checks and clear the rules. Gap: no OCR-based detection | `test_evidence.py::test_image_text_rule_is_in_the_verifier_prompt`, `test_manipulated_model_output_still_holds` |
| E9 | Model hallucination or overconfidence | High | Partial | Bar, self-consistency, quality and integrity gates, rules. Gap: calibration set is 12 synthetic cases; Claude not calibrated yet. See CONFIDENCE.md | `test_calibration.py` |
| E10 | Benign notes falsely flagged as injection | Low | Mitigated | Patterns target instructions, not opinions | `test_evidence.py::test_ordinary_notes_are_not_flagged` |
| E11 | Decompression bomb or oversized upload | Medium | Mitigated | 20 MB body, 12 MB image, 40 MP cap read from the header before decoding | `test_security.py::test_decompression_bomb_is_refused` |

## Access and data

| # | Attack | Sev | Status | Control | Proof |
|---|---|---|---|---|---|
| S1 | Guess or enumerate job links | High | Mitigated | 256-bit HMAC capability tokens; 64-bit random ids that are not credentials; wrong token and missing job both 404 | `test_security.py::test_tokens_have_at_least_128_bits_and_differ_per_role`, `test_threshold_ids_are_not_guessable_and_not_credentials`, `test_reads_need_a_token` |
| S2 | Act as the other party (buyer refunds, seller releases or consents) | High | Mitigated | Explicit role table per API action; submitter role taken from the token | `test_security.py::test_every_api_action_checks_the_role`, `test_evidence_submitter_comes_from_the_token_not_the_body` |
| S3 | Cross-tenant access | High | Mitigated | Tokens are bound to the tenant; tenant keys only reach their tenant; admin token is read-only | `test_security.py::test_tenant_isolation` |
| S4 | Token leaks through PayPal, referrers or logs | Medium | Mitigated | Return URL carries no token (HttpOnly SameSite=Lax cookie instead); `no-referrer`; log redaction | `test_server.py::pay`, `test_security.py::test_paypal_return_without_cookie_reveals_nothing`, `test_no_secret_or_token_is_written_to_disk` |
| S5 | Token in the page URL (browser history, shoulder surfing) | Low | Accepted | Links are the product's sharing model. Rotating `JANUS_SECRET` revokes every link | |
| S6 | Hotlink or share evidence photos | Medium | Mitigated | Photo URLs are HMAC-signed per file and expire after 5 minutes | `test_security.py::test_photo_urls_are_signed_and_expire`, `test_unsigned_photo_url_is_refused` |
| S7 | Path traversal through file routes | High | Mitigated | Signed path check, then resolved path must stay inside the job's folder | `test_server.py::test_rejects_bad_input` |
| S8 | XSS through listing text, condition or notes | High | Mitigated | `html.escape` on all output; packet Markdown escaped | `test_security.py::test_submitter_text_is_escaped_in_pages`, `test_packet.py` |
| S9 | CSRF | Medium | Mitigated | State-changing API calls need the token in a custom header, which a cross-site form cannot send. The consent form POST (`/t/<id>/pay`) carries the buyer token in its URL, not in an ambient cookie, so a cross-site form cannot submit it. The only cookie is scoped to the idempotent PayPal return path | design, `test_server.py::test_post_pay_needs_terms_and_the_buyer_token` |
| S10 | Denial of service or AI-cost abuse | Medium | Partial | Per-IP token buckets (tighter on AI routes), 429 with Retry-After. Gap: in-memory per process, no global spend cap, public create on by default (`JANUS_PUBLIC_CREATE=0` turns it off) | `test_security.py::test_rate_limit_returns_429`, `test_public_create_can_be_turned_off` |
| S11 | PII in logs (notes, emails, phones, GPS) | Medium | Mitigated | Redaction of sensitive keys and patterns on every log and audit write; GPS rounded and never logged | `test_logs_audit.py::test_redacts_pii_and_secrets`, `test_evidence.py::test_gps_is_rounded_and_never_logged` |
| S12 | Secrets committed or written to disk | High | Mitigated | Secrets only from the environment; tokens derived, never stored; `.env` gitignored; weak secrets refused | `test_security.py::test_no_secret_or_token_is_written_to_disk`, `test_weak_secrets_and_keys_are_refused` |
| S13 | Ephemeral secret in mock mode silently breaks links on restart | Low | Accepted | Logged as a warning; sandbox mode refuses to start without `JANUS_SECRET`; `setup.sh` writes one | `test_security.py::test_weak_secrets_and_keys_are_refused` |

## Operations and integrity

| # | Attack | Sev | Status | Control | Proof |
|---|---|---|---|---|---|
| O1 | Edit, delete or reorder audit entries | Medium | Partial | SHA-256 hash chain; `janus audit` verifies it. Gap: someone with write access can rewrite the whole chain; no external anchor | `test_logs_audit.py::test_audit_chain_and_tamper_detection`, `test_audit_deletion_detected` |
| O2 | Concurrent writers fork the audit chain | Medium | Mitigated | Exclusive lock around read-last-and-append | `test_logs_audit.py::test_audit_concurrent_appends_keep_one_chain` |
| O3 | Crash mid-capture loses the capture record | Critical | Mitigated | See M6; recovery runs at server start and on every tick | `test_scheduler.py::test_capture_in_flight_is_not_touched_by_expiry_but_recovered` |
| O4 | Local-time or naive timestamps shift expiry | Medium | Mitigated | One UTC clock; naive timestamps parse as UTC; EXIF without zone gets a 14-hour window | `test_clock.py` |
| O5 | Locks on a network filesystem do not hold | Low | Open | `flock` and `O_EXCL` assume local POSIX disk. Documented assumption; a database is the real fix | |
