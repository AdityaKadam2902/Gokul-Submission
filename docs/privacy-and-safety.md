# PracticeSignal — Privacy & Safety Posture

> **Document type:** Engineering safeguard statement. **Not legal advice.** Commercial collection of identifiable practitioner information would require an independent privacy/legal review per source (ARCHITECTURE §4).

## Data scope (V1, DECISION_LOG D6/D16)

Supported fields are exactly the V1 registered allowlist: practice name; public practice address; official website; publicly published practice phone/email; service names/categories; public doctor/nurse vacancy wording. Provenance (URL, timestamp, evidence quote, review status) is stored on every fact.

Never collected: patient info; sensitive attributes (health status, ethnicity, age, family/immigration status); personal/home contact details; bulk named-clinician profiles; social-media content; applicant data or ranking; testimonials.

## Collection posture

1. **No arbitrary fetching** — only registered sources on allowlisted hosts; a pasted URL is never enough (`docs/SOURCE_REGISTER.md` procedure).
2. **Unknown policy ⇒ fixture-only.** As of M0, **zero** sources are approved for live scraping; all demonstrations use the clearly-labelled synthetic fixtures (F1–F6). This remains true until the manual robots/terms check for register row S1 completes.
3. **Conservative transport** when live fetching is later authorised: HTTPS only; one request per host; 3-second default delay; explicit timeouts; size/page caps; honest User-Agent; robots honoured per path; stop-and-mark on 403/429/CAPTCHA/auth walls — no circumvention, proxying or identity rotation.

## Fetch-security posture (DECISION_LOG D13 — with honest limitations)

Controls: per-hop redirect validation inside a manual loop (auto-redirect disabled); blocking of private/loopback/link-local/CGNAT/metadata IP ranges at connect time; registered-host allowlist enforced before any socket opens in tests.

**Explicit, non-claimable limitations:**

- This design does **not** claim complete SSRF immunity. DNS-resolution-to-connect timing windows (rebinding-style) remain theoretically possible in the reference implementation; mitigations used are short-lived address pinning per fetch and the allowlist-until-verified policy. The remaining residual risk is documented as acceptable for a localhost-only assessment prototype — and would need re-review before any networked deployment.
- Robots/permission compliance is an engineering control, not a substitute for per-source legal permission to collect or reuse content.
- No automated email/SMS path exists anywhere in the codebase (ACMA-consistent; exported contact fields are labelled "manual review; no consent inferred").

## Retention & erasure (DECISION_LOG D8)

Deleting a source cascades to its snapshots, facts, evidence, prompt runs and ingestion events in one review action. No other destructive paths exist in application code. Raw page artifacts, if ever produced by an authorised live fetch, stay on the local disk and out of Git; the repository ships only synthetic fixtures.

## Prompt-injection (DECISION_LOG D10)

Defence is structural: schema-validated structured output; deterministic exact-quote verification with offsets; per-source field allowlists; candidate-not-fact pipeline; human review gate. Prompt wording (see `prompts/injection_guard_v1.md`) is a supplement. We do not claim 3B-scale models are reliably prompt-proof; we claim the application's gates prevent injected content from becoming accepted facts or actions — with F4/F6 corpus negative tests enforcing this in M2+.

## What this document does not do

It does not license reuse of any third-party content, name, logo or brand; it does not determine lawful collection for any specific site; and it does not constitute the privacy review a production deployment would need.
