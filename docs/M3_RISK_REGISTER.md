# PracticeSignal — Milestone 3 Risk Register: Architectural, Data-Integrity, Model & Operational Safeguards
## Revision 3 (Final Principal Systems Refinement)

> **Document Type:** Principal Risk Assessment & Engineering Mitigation Matrix
> **Milestone:** M3 (Business Intelligence, Entity Resolution & Change Detection)
> **Status:** Proposed Risk Register (Revision 3 — Architecture Specification Only; No code changes applied)
> **Prepared:** 6 November 2026
> **Baseline Checkpoint Commit:** `6741e90`
> **Governing Architecture Docs:** `docs/M3_ARCHITECTURE_PROPOSAL.md`, `docs/M3_DATA_MODEL.md`, `docs/M3_IMPLEMENTATION_PLAN.md`, `docs/M3_EVALUATION_PLAN.md`

---

## 1. Risk Governance & Assessment Methodology

Milestone 3 shifts PracticeSignal from isolated per-page extraction to active multi-source business intelligence. Connecting disparate data sources, tracking temporal observation heartbeats, detecting factual discrepancies, resolving fact ownership on multi-practice pages, and monitoring recruitment vacancies introduces distinct failure modes that could mislead healthcare recruiters, compromise practice identity, or violate collection ethics.

Every identified risk is assessed across:
- **Inherent Severity & Likelihood:** Risk exposure before architectural controls.
- **Architectural Mitigation:** Concrete code, database constraint, or algorithmic defense.
- **Residual Risk:** Remaining operational risk after controls.
- **Decision References:** Architectural governance link (D1–D47).

---

## 2. Risk Matrix Summary (Revision 3 Refined)

| Risk ID | Category | Summary Description | Inherent Severity | Residual Risk | Status |
|---|---|---|---|---|---|
| **RSK-OBS-01** | Architecture | Unchanged crawl short-circuiting causing fresh content to appear stale | High | Negligible | Mitigated by `source_observations` recording observation heartbeats (D40) |
| **RSK-FP-01** | False-Positive | Misleading match score semantics or merging lookalike branch networks | Critical | Low | Mitigated by 3-tier matching, postcode isolation & zero auto-merge (D12, D39) |
| **RSK-OWN-01** | Data Integrity | Fact ownership pollution on corporate multi-branch pages | High | Low | Mitigated by heading-context scoping & leaving ambiguous facts NULL (D47) |
| **RSK-FP-02** | False-Positive | Flagging departmental hours (clinic vs pathology) as discrepancies | High | Low | Mitigated by mandatory `scope_key` equality & NULL scope gating (D27, D41) |
| **RSK-FP-03** | False-Positive | LLM extraction omission falsely triggering "service removed" change event | High | Low | Mitigated by DOM/text verification before emitting unobserved events (D42) |
| **RSK-VAC-01** | False-Positive | Vacancy title/wording modification spawning duplicate opportunity records | Medium | Low | Mitigated by priority-based identity (Job ID / slug) & `changed` status (D43) |
| **RSK-VAC-02** | Data Integrity | Ingestion failure or single crawl omission falsely closing vacancies | High | Negligible | Mitigated by 2-crawl rule, freeze on crawl errors & prohibition of "filled" (D43) |
| **RSK-SEC-01** | Security / Audit | Overclaiming cryptographic tamper-resistance for local SQLite review log | Medium | Negligible | Mitigated by honest terminology: locally identified reviewer & append-only log (D44) |
| **RSK-TIME-01** | Temporal Logic | Wall-clock flakiness or rigid 30-day timer corrupting freshness | Medium | Low | Mitigated by category-specific policies (14d/30d/90d) & injected clock (D46) |
| **RSK-ARCH-01** | Architecture | SQLite table lock contention during concurrent AI extraction & reviews | Medium | Low | Mitigated by WAL mode, serial inference & short transactions (D5, D40) |
| **RSK-ARCH-02** | Architecture | Foreign key cascades erasing historical intelligence on source deletion | Critical | Negligible | Mitigated by `ON DELETE RESTRICT` / `SET NULL` on intelligence tables (D40) |
| **RSK-PRIV-01** | Privacy & Safety | Accidental extraction of individual clinician personal contact info | High | Low | Mitigated by practice contact allowlist & no-clinician bio filter (D6, D16) |
| **RSK-PRIV-02** | Privacy & Safety | Spam Act / ACMA violation via automated marketing messaging | Critical | Zero | Mitigated by structural absence of email/SMS transmission libraries (D13) |
| **RSK-MOD-01** | Model Reliability | 3B local LLM hallucinating structured opportunity attributes | Medium | Low | Mitigated by Pydantic schemas & deterministic quote offset verifier (D2, D23) |
| **RSK-MIG-01** | Migration | Schema migration corrupting existing M0–M2 tables | Critical | Negligible | Mitigated by safe additive migrations & automated timestamped backups (D28) |

---

## 3. Detailed Threat Analysis & Concrete Mitigations

### 1. Ingestion Short-Circuiting & Freshness Blindness (RSK-OBS-01)
- **Threat Scenario:** M1 short-circuits unchanged crawls using `content_hash` and does not insert a new `page_snapshots` row. If freshness logic checks `page_snapshots.fetched_at`, an active clinic website that has not changed content for 60 days will be flagged as stale and unmonitored.
- **Architectural Mitigations:**
  1. **Additive `source_observations` Table (D40):** Every ingestion run writes a row capturing `observed_at`, `outcome: success_unchanged`, and `observation_sequence`.
  2. Freshness evaluation queries `source_observations.observed_at`, guaranteeing that regular crawls of static websites are accurately recognized as freshly observed.
  3. Consecutive missing observation logic queries `source_observations` to verify that 2 successful, complete crawls have occurred before retiring unobserved facts.

### 2. Match Score Semantics & Branch Merging (RSK-FP-01)
- **Threat Scenario:** Presenting a raw string similarity metric (RapidFuzz 88/100) as an "88% probability of identity" misleads human reviewers. Merging two clinics sharing a brand name or parent domain across different suburbs corrupts doctor attribution and telephone numbers.
- **Architectural Mitigations:**
  1. **Discrete Qualitative Tiers (D39):** Raw metric scores are separated from probability claims. The system emits `strong_match_suggestion`, `possible_match`, and `insufficient_evidence`.
  2. **Branch-Level Evidence Requirement:** A `strong_match_suggestion` requires branch-level identity proof (local practice phone + postcode, or specific branch URL slug), never parent domain alone.
  3. **Strict Locality Enforcement:** Shared brand names in differing postcodes (F9) evaluate as `insufficient_evidence` (zero match candidate emitted).
  4. **Zero Auto-Merge Policy:** Matching logic produces suggestions only; merges require human confirmation.

### 3. Fact Ownership Pollution on Corporate Multi-Branch Pages (RSK-OWN-01)
- **Threat Scenario:** A corporate directory page lists clinics in Box Hill and Frankston. If facts are assigned organisation ownership solely via domain or source URL, Box Hill's telephone number and hours will be attributed to Frankston.
- **Architectural Mitigations:**
  1. **Deterministic Heading-Context Scoping (D47):** Facts extracted from multi-branch pages inspect the quote's nearest preceding heading in `page_snapshots.headings`. If the heading matches a specific branch locality, the fact is assigned to that branch.
  2. **Unresolved Ownership for Ambiguous Facts:** Corporate-wide or ambiguous facts leave `facts.organisation_id = NULL` and emit an ownership review item in `review_actions`.
  3. Reusing the existing nullable `facts.organisation_id` avoids risky schema modifications.

### 4. Vacancy Title Modifications & False Closures (RSK-VAC-01 & RSK-VAC-02)
- **Threat Scenario:** A clinic updates an advertised job title from "VR GP" to "VR GP — Urgent Start". The system spawns a duplicate vacancy, while inferring that the previous ad was filled.
- **Architectural Mitigations:**
  1. **Priority-Based Vacancy Identity (D43):** Opportunities are identified primarily by published Job Reference ID or stable source URL slug, followed by composite tuple (`org + role + location`).
  2. Title modifications transition the existing opportunity to `status = 'changed'` and log the diff in `change_events`. Zero duplicate vacancies are created.
  3. **Two-Crawl Persistence Rule:** Missing vacancies transition internally to `unconfirmed_prior` on the first crawl, and only transition to `no_longer_observed` after a second consecutive complete successful crawl confirms absence.
  4. **Freeze on Crawl Failures:** Network errors or parse failures freeze the observation sequence.
  5. **Prohibition on "Filled":** Missing vacancies are never marked `filled` or `hired`.

### 5. Honest Audit Security Terminology (RSK-SEC-01)
- **Threat Scenario:** Describing a local SQLite review log as "tamper-proof" or claiming "authenticated reviewer identity" without an enterprise authentication infrastructure overclaims security posture.
- **Architectural Mitigations:**
  1. **Locally Identified Reviewer (D44):** The prototype uses the explicit term *locally identified reviewer* (reviewers identify via non-empty string parameter, e.g. `"analyst_1"`).
  2. **Application-Level Append-Only History:** Documented honestly as application-level append-only history (zero update/delete endpoints in code). Transparently acknowledges that local SQLite files are non-cryptographic and do not prevent direct raw disk edits by a system administrator.

---

## 4. Monitoring & Verification Schedule

1. **Pre-Milestone Gate Assertions:** Automated tests in `backend/tests/` verify every safeguard before moving to the next sub-milestone.
2. **Deterministic Edge-Case Corpus:** Synthetic fixtures F7–F15 explicitly test every failure mode listed in this register.
3. **Audit Inspection:** Review records generated during testing are inspected for completeness and immutability before milestone sign-off.
