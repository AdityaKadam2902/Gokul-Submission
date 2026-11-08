# PracticeSignal — Milestone 3 Evaluation Plan: Synthetic Multi-Source Corpus, Test Scenarios & Verification Metrics
## Revision 3 (Final Principal Systems Refinement)

> **Document Type:** Principal Quality Assurance & Systems Evaluation Specification
> **Milestone:** M3 (Business Intelligence, Entity Resolution & Change Detection)
> **Status:** Proposed Evaluation Plan (Revision 3 — Architecture Specification Only; No test executions applied)
> **Prepared:** 6 November 2026
> **Baseline Checkpoint Commit:** `6741e90`
> **Governing Architecture Docs:** `docs/M3_ARCHITECTURE_PROPOSAL.md`, `docs/M3_DATA_MODEL.md`, `docs/M3_IMPLEMENTATION_PLAN.md`

---

## 1. Evaluation Integrity & Ground-Truth Methodology

Milestone 3 evaluates multi-source business intelligence, conservative entity resolution, cross-source discrepancy detection, temporal change tracking, observation heartbeats, and recruitment vacancy intelligence.

### Core Testing Safeguards:
1. **Preserve Legacy Corpus (F1–F6):** Fixtures F1 through F6 remain frozen as M0–M2 regression benchmarks.
2. **Deterministic Evaluation Harness:** All observation recording, entity matching, fact ownership mapping, discrepancy logic, snapshot diffing, and vacancy state machines are strictly deterministic Python algorithms. They are evaluated against hand-annotated JSON ground-truth expectations.
3. **Clear Boundary on LLM Assistance:** If local LLM inference (`llama3.2:3b` on CPU) is invoked to extract candidate facts from new fixtures, its results are stored separately and evaluated as model extractions; all downstream intelligence rules run deterministically.
4. **Honest Reporting Rules:**
   - Raw counts accompany every percentage ($n / N$).
   - Precision and recall are reported only on hand-annotated finite sets.
   - Failures and edge-case rejections are primary deliverables.
   - Zero invented numbers.

---

## 2. Expanded Synthetic Multi-Source Evaluation Corpus (F7–F14)

All fixtures reside in `backend/tests/fixtures/m3_corpus/` and represent 100% fictional Australian healthcare practices using `.example.org` domains.

### Group Alpha: Organisation Matching & Branch Isolation (F7–F9)

#### Ground-Truth Practice Identities:
- **ORG-Alpha-1:** `St Jude Family Medical Centre` (Slug: `st-jude-family-medical-3000`), Domain: `stjude-medical.example.org`, Local Phone: `+61394198888`, Address: `120 Collins St, Melbourne VIC 3000`.
- **ORG-Alpha-2:** `Metropolitan Medical Centre — Box Hill` (Slug: `metropolitan-medical-box-hill-3128`), Domain: `metrohealth.example.org/clinics/box-hill`, Local Phone: `+61398901111`, Address: `450 Station St, Box Hill VIC 3128`.
- **ORG-Alpha-3:** `Metropolitan Medical Centre — Frankston` (Slug: `metropolitan-medical-frankston-3199`), Domain: `metrohealth.example.org/clinics/frankston`, Local Phone: `+61397832222`, Address: `12 Nepean Hwy, Frankston VIC 3199`.
- **ORG-Alpha-4:** `Metropolitan Specialist Suites — Box Hill` (Slug: `metropolitan-specialist-box-hill-3128`), Domain: `metrohealth.example.org/clinics/box-hill-specialists`, Address: `Suite 4, 450 Station St, Box Hill VIC 3128`.

#### Test Scenarios & Expectations:

| Fixture Pair | Scenario | Input Data Signals | Expected Output & Tier | Acceptance Gate |
|---|---|---|---|---|
| **F7_a + F7_b** | Exact Domain Match | F7_a (Official site: `stjude-medical.example.org`, Postcode `3000`) vs F7_b (Directory listing citing same domain and postcode `3000`) | `match_tier: strong_match_suggestion`, `status: suggested` | **Zero auto-merge:** Branch-level evidence verified; suggested only |
| **F8_a + F8_b** | Name Variation, Same Postcode | "St Jude Family Medical" vs "St Jude Family Medical Centre", both with address `120 Collins St, Melbourne 3000` | `match_tier: possible_match`, RapidFuzz score `91/100` (experimental), `status: needs_review` | High fuzzy ratio + identical postcode yields `needs_review` |
| **F9_a + F9_b** | Lookalike Branch Network | F9_a ("Metropolitan Medical", Postcode `3128`, Box Hill) vs F9_b ("Metropolitan Medical", Postcode `3199`, Frankston) | **ZERO match candidate emitted.** `match_tier: insufficient_evidence` | **100% Branch Isolation:** Branch clinics never merged |
| **F9_precinct** | Same Host & Postcode, Distinct Suites | "Metropolitan Medical Centre" (Suite 1, 450 Station St, 3128) vs "Metropolitan Specialist Suites" (Suite 4, 450 Station St, 3128) | **ZERO match candidate emitted.** `match_tier: insufficient_evidence` | Distinct suites/specialties preserved as separate organisations |
| **F9_shared_phone** | Shared Corporate 1300 Contact | Two clinics citing national triage number `1300 000 123` without matching street address | **ZERO match candidate emitted.** `match_tier: insufficient_evidence` | Shared corporate 1300/1800 contact rejected from matching practices |
| **F9_neg** | Generic Clinic Name | "Family Health Clinic" without matching domain, phone, or postcode | **ZERO match candidate emitted.** | Prevents clustering unrelated generic clinics |

---

### Group Beta: Cross-Source Discrepancy Detection (F10–F12)

#### Test Scenarios & Expectations:

| Fixture Pair | Tested Field | Fact A (Source 1) | Fact B (Source 2) | Expected Outcome | Failure Defense |
|---|---|---|---|---|---|
| **F10_a + F10_b** | Telephone Formatting | `(03) 9419 8888` (General Reception) | `0394198888` (Appointments) | **NO Discrepancy.** Both normalize to E.164 `+61394198888` | E.164 normalization prevents false formatting conflicts |
| **F11_a + F11_b** | Genuine Phone Conflict | `(03) 9419 8888` (General Scope) | `(03) 9419 9999` (General Scope) | **Discrepancy Created:** `conflict_type: value_mismatch`, `status: detected`, links `fact_a_id` and `fact_b_id` | Flags true conflict between two verified evidence quotes |
| **F12_a + F12_b** | Departmental Hours (Multi-Scope) | Mon–Fri 08:00–18:00 (`scope: clinic`) | Mon–Fri 07:30–12:00 (`scope: pathology`) | **NO Discrepancy.** Cross-scope comparison strictly suppressed | Mandatory `scope_key` equality rejects false departmental conflict |
| **F12_null** | Unscoped Facts | Mon–Fri 08:00–18:00 (`scope: null`) | Mon–Fri 08:00–17:00 (`scope: null`) | **NO Discrepancy.** Routes to `needs_review` (NULL $\neq$ NULL) | Prevents comparing ambiguous contexts as definite conflicts |
| **F12_neg** | Incompatible Field Types | Phone: `(03) 9419 8888` | Address: `120 Collins St` | **NO Discrepancy.** Early exit on field key inequality | Incompatible fields rejected early |

---

### Group Gamma: Temporal Observation History & Change Monitoring (F13)

#### Sequential Snapshot Evolution & Observation Heartbeat:

| Step / Snapshot | Page Content Summary | Ingestion Action | Expected Observation & Change Events | State Integrity Check |
|---|---|---|---|---|
| **F13_v1** | Baseline practice details: Phone `(03) 9123 4567`, Hours `08:00–17:00`, Service `General Practice` | Initial crawl | 1. `source_observations`: `outcome: success_new_snapshot`, `sequence: 1`.<br>2. `page_snapshots`: new row created.<br>3. Zero change events emitted. | Anchor snapshot created; facts persisted as baseline |
| **F13_unchanged** | Crawl of identical content to F13_v1 | Re-crawl (Content hash identical) | 1. `source_observations`: `outcome: success_unchanged`, `sequence: 2`.<br>2. **ZERO new page_snapshots rows created**.<br>3. Zero change events emitted. | Proves heartbeat recorded without mutating snapshot store |
| **F13_err** | Simulated HTTP / Crawler Failure | Crawl error (e.g. 500 error / parse failure) | 1. `source_observations`: `outcome: failed`, `sequence: 3`.<br>2. Zero snapshots created.<br>3. **ZERO change events emitted**. | Crawl error never triggers false "service removed" alert |
| **F13_v2** | Telephone changed to `(03) 9123 9999`; Added Service `Physiotherapy`; Hours unchanged | Re-crawl (Content hash changed) | 1. `source_observations`: `outcome: success_new_snapshot`, `sequence: 4`.<br>2. `change_events`: `fact_modified` (phone) with JSON diff; `fact_added` (physiotherapy). | Hours fact hash unchanged, emits no event |
| **F13_omit** | Extraction Omission Check | Snapshot text contains "Physiotherapy", but simulated extraction omits it | DOM verifier detects value still present in text; **ZERO change events emitted**. | Extraction omission suppressed from triggering false removal |
| **F13_v3a** | `Physiotherapy` genuinely omitted from page | 1st complete crawl with omission | 1. `source_observations`: `outcome: success_new_snapshot`, `sequence: 5`.<br>2. Fact status transitions to `unconfirmed_prior`.<br>3. Zero removal change events. | Single absence marked unconfirmed; no alert emitted |
| **F13_v3b** | `Physiotherapy` absent 2nd consecutive crawl; Phone reverted | 2nd consecutive complete crawl with omission | 1. `source_observations`: `outcome: success_unchanged`, `sequence: 6`.<br>2. `change_events`: `fact_unobserved` (physiotherapy); `fact_modified`/`reappeared` (phone). | 2 consecutive crawls triggers `fact_unobserved`; legacy fact preserved |

---

### Group Delta: Healthcare Vacancy Intelligence & Lifecycles (F14)

#### Vacancy Continuity, Title Modification & Closure Scenarios:

| Step / Snapshot | Advertised Role & Wording | System Action | Status Transition | Conservative Safeguard |
|---|---|---|---|---|
| **F14_v1** | "Seeking VR General Practitioner, 70% billings, immediate start. Ref: GP-2026-04" | Create `OpportunityORM`: identified by `explicit_ad_id: GP-2026-04`, role: `gp` | `status: advertised` | Identified primarily by published Job Ref ID; captures verbatim text |
| **F14_v2** | "Seeking VR General Practitioner — Urgent Start, 72% billings. Ref: GP-2026-04" | Match existing opportunity via `explicit_ad_id`; update `last_seen_at` and diff | `status: changed` | Title/wording modification updates existing record; **zero duplicate vacancy** |
| **F14_v3_absent1** | Careers page lists zero open roles (Successful complete crawl) | Re-crawl detects ad absence on 1st complete crawl | `status: unconfirmed_prior` (internal) | 1st absence is marked unconfirmed; remains visible as active unconfirmed |
| **F14_err** | Simulated network failure during crawl | Crawler fails to fetch careers page | Status remains `unconfirmed_prior`; sequence frozen | Failed observation does not increment consecutive missing counter |
| **F14_v3_absent2** | Careers page lists zero open roles (2nd consecutive complete crawl) | 2nd consecutive complete successful crawl confirms omission | `status: no_longer_observed` | **Strict Rule:** Never marks `filled` or `hired`; sets `no_longer_observed` |
| **F14_v4_reappear** | "Seeking VR General Practitioner, 70% billings. Ref: GP-2026-04" reappears | Re-crawl detects identical ad ID | `status: advertised` | Re-observed opportunity restored to `advertised`; diff logged in `change_events` |
| **F14_v5_closed** | Verbatim banner: "Applications for VR GP role (Ref: GP-2026-04) are now closed" | Verifies verbatim quote against snapshot text | `status: explicitly_closed` | Transitions to `explicitly_closed` strictly with verified citation quote |

---

### Group Epsilon: Multi-Practice Page Fact Ownership (F15)

#### Multi-Branch Corporate Page Scenarios:
- **Fixture F15:** Corporate page `metrohealth.example.org/locations` containing:
  - Heading Level 2: `Box Hill Family Practice` -> lists Phone `(03) 9890 1111`, Address `450 Station St`, Services: `General Practice`.
  - Heading Level 2: `Frankston Medical Centre` -> lists Phone `(03) 9783 2222`, Address `12 Nepean Hwy`, Services: `General Practice, Skin Clinic`.
  - Heading Level 2: `Corporate Office` -> lists Phone `1300 000 123`, Email `corporate@metrohealth.example.org`.

#### Ground-Truth Ownership Expectations:
1. Facts under `Box Hill Family Practice` deterministically resolve `facts.organisation_id = ORG-Alpha-2.id`.
2. Facts under `Frankston Medical Centre` deterministically resolve `facts.organisation_id = ORG-Alpha-3.id`.
3. Facts under `Corporate Office` leave `facts.organisation_id = NULL` and emit an ownership review item in `review_actions` (`reason_code: heading_scope_ambiguous`).
4. **Zero facts are assigned to an organisation solely based on the parent domain `metrohealth.example.org`.**

---

## 3. Evaluation Metrics & Success Thresholds

### 1. Observation History & Heartbeat
- **Heartbeat Recording Accuracy:** Target $100.0\%$ (Every crawl attempt accurately logged with outcome).
- **Snapshot Immutability Preservation:** Target $100.0\%$ (Zero new snapshot rows created on unchanged crawls).

### 2. Organisation Matching & Entity Resolution
- **Matching Precision ($TP / (TP + FP)$):** Target $\ge 95.0\%$.
- **Lookalike Branch Rejection Rate:** Target $100.0\%$ (Strict constraint: F9 Box Hill vs Frankston must never produce a match).
- **Auto-Merge Count:** **Zero** (Strict structural constraint).

### 3. Discrepancy Detection & Scope Gating
- **Discrepancy Precision ($TP / (TP + FP)$):** Target $\ge 95.0\%$.
- **Cross-Scope Suppression Rate:** Target $100.0\%$ (Clinic vs Pathology hours must yield 0 conflicts).
- **Formatting False-Conflict Rate:** Target $0.0\%$ (E.164 phone normalization eliminates formatting divergence).

### 4. Change Detection Correctness
- **Event Classification Accuracy:** Target $100.0\%$ on synthetic F13 corpus.
- **False Removal Suppression Rate:** Target $100.0\%$ (Zero removal events on crawl errors or verified DOM extractions).

### 5. Vacancy Lifecycle Integrity
- **False Filled Inferences:** **Zero** (Hard architectural constraint).
- **Closure Citation Validity:** Target $100.0\%$ (Every `explicitly_closed` transition must possess valid evidence offsets).

---

## 4. Execution & Reporting Runner

The automated test script `scripts/evaluate_m3.py` will execute the evaluation suite and generate `docs/M3_EVALUATION_REPORT.md`:

```powershell
# Synthetic Benchmark Evaluation Command
python scripts/evaluate_m3.py --corpus backend/tests/fixtures/m3_corpus/ --output docs/M3_EVALUATION_REPORT.md
```

All reported numbers will show raw counts alongside percentages (e.g. `14 / 14 (100.0%)`) and explicitly document known synthetic boundaries.
