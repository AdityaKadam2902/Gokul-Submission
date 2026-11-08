# PracticeSignal — AI Development Handoff (Fresh Session Continuation)

> **Document purpose:** Self-contained persistent engineering handoff allowing any coding agent or engineer to resume work immediately without prior session history.
> **Current Milestone:** **M3.7 Complete** (Practice Lifecycle Intelligence: Closure, Relocation & Corporate Acquisition Detection)
> **Approved Architecture Baseline:** `docs/M3_ARCHITECTURE_PROPOSAL.md`, `docs/M3_DATA_MODEL.md`, `docs/M3_IMPLEMENTATION_PLAN.md`, `docs/M3_EVALUATION_PLAN.md`, `docs/M3_RISK_REGISTER.md` (Revision 3 Approved)
> **Baseline Checkpoint Commit:** `6741e90` (`M2: complete audit remediation and strengthen evidence verification`)
> **Date:** 7 November 2026
> **Operating Environment:** Windows 11 Enterprise (Build 26300), Python 3.12.6 (.venv), Node v24.20.0, npm 11.19.0, Ollama 0.40.14 (`llama3.2:3b`), CPU-only host.

---

## 1. Project Context & Principles

PracticeSignal is an evidence-first healthcare practice & workforce intelligence platform.
- **Core Principle:** The LLM proposes candidate information. Python deterministically verifies supporting quotes and character offsets against raw page snapshots. The database preserves immutable provenance. Locally identified human reviewers maintain decision authority.
- **Zero-Cost Policy:** ₹0 budget. Zero cloud AI APIs, zero external network fetching in tests/evaluation, zero paid services. Local Ollama (`llama3.2:3b`, pinned) performs inference.
- **Milestone Scope Gate:**
  - M0 (Foundation), M1 (Vertical Slice), and M2 (AI Extraction & Evidence Verification) are **complete and closed**.
  - **M3.0 (Data Contracts & Synthetic Evaluation Foundation) is COMPLETE.**
  - **M3.1 (Organisation Registry, Observation Heartbeat & Conservative Entity Resolution) is COMPLETE.**
  - **M3.2 (Cross-Source Discrepancy Detection & Conflict Identification) is COMPLETE.**
  - **M3.3 (Temporal Change Detection & Observation Delta Engine) is COMPLETE.**
  - **M3.4 (Evidence-Aware Information Freshness & Monitoring Policy Engine) is COMPLETE.**
  - **M3.5 (Healthcare Practice & Workforce Vacancy Lifecycle Intelligence) is COMPLETE.**
  - M3.6 (Human Review Workflow & Audit Log) and M3.7 (Practice Closure & Relocation Detection) are **pending human authorisation**.
  - M4 (Advanced Analytics & Recruiter Workflows) is **strictly deferred**.

---

## 2. Completed Work in M3.1

### Phase A: Additive Database Schema & Migration M3_001
- Added 4 additive SQLAlchemy ORM models in `backend/app/db/orm.py`:
  - `Organisation`: Canonical healthcare practice entity with unique slug constraint.
  - `OrganisationSourceLink`: Additive N:M mapping between organisations and registered source URLs with path scoping (`official_practice_site`, `corporate_parent`, `branch_page`, `third_party_directory`).
  - `SourceObservation`: Observation attempt heartbeat capturing unchanged short-circuits (`success_unchanged`), new snapshots, and failure states.
  - `OrganisationMatchCandidate`: Reviewable pairwise matching suggestions between sources and organisations.
- Added migration `M3_001` in `backend/app/db/migrations.py` with timestamped `.bak` database backups, foreign key validation, idempotent execution, and automated rollback capability.
- Integrated `M3_001` into `init_db` in `backend/app/db/base.py` for reproducible create-on-boot and safe migration application.

### Phase B: Observation Heartbeat Engine
- Implemented `backend/app/intelligence/observation_tracker.py`:
  - Records every observation attempt with strictly monotonic sequence numbers allocated per source (`MAX(sequence) + 1`).
  - Unchanged crawls reference existing immutable snapshots without creating duplicates.
  - Failed crawls capture error details without throwing unhandled exceptions.
  - Maintains strict independence between `parsing_completeness` and `extraction_completeness`.
- Integrated observation recording into `run_ingestion` in `backend/app/ingest/pipeline.py`.

### Phase C: Conservative 3-Tier Entity Resolver
- Implemented `backend/app/intelligence/entity_resolver.py`:
  - Evaluates pairwise matching signals using a deterministic rule-gated 3-tier matching engine.
  - **Tier 1 (`strong_match_suggestion`):** Local telephone match (E.164) + exact postcode; or exact non-aggregator official domain + postcode + locality.
  - **Tier 2 (`possible_match`):** RapidFuzz token-sort similarity score >= 88.0 + exact postcode; or approved corporate aggregator + branch slug + street address match.
  - **Tier 3 (`insufficient_evidence`):** Strictly isolates lookalike branches in differing postcodes (F9), distinct suites sharing buildings, shared 1300/1800 corporate numbers without branch street proof, and generic clinic names without official domain/phone proof.
  - **Zero Auto-Merge:** Always outputs reviewable match candidates with status `suggested` or `needs_review`. Automatic merging is prohibited.

### Phase D: Fact Ownership Mapper
- Implemented `backend/app/intelligence/fact_ownership.py`:
  - Maps `facts.organisation_id` using confirmed source links and document heading context.
  - Single-practice sources with confirmed `official_practice_site` or `branch_page` links assign facts directly.
  - Multi-branch pages resolve evidence start offsets against enclosing heading spans. Facts under branch-specific headings are assigned to the corresponding branch organisation.
  - Corporate-wide sections ("Careers", "About Us", "Billing Policy") or ambiguous headings leave `organisation_id = None` and record `needs_review_reason = "heading_scope_corporate_unassigned"`.
  - Parent corporate domain alone NEVER assigns ownership.
  - Existing valid `organisation_id` assignments are preserved and not overwritten without explicit instruction.
  - Provides batch helper `assign_snapshot_facts_ownership(session, snapshot_id)`.

### Phase E: Organisations REST API & Type Generation
- Added API routes in `backend/app/api/routers/organisations.py` (and re-exported in `backend/app/api/organisations.py`):
  - `GET /api/organisations`: List organisations with filters (`slug`, `postcode`, `state`, `skip`, `limit`).
  - `POST /api/organisations`: Create practice organisation with slug uniqueness enforcement (409 conflict).
  - `GET /api/organisations/{id}`: Detailed profile with linked sources and `active_facts_count`.
  - `GET /api/organisations/{id}/sources`: List organisation-source links.
  - `POST /api/organisations/{id}/sources`: Add source link with duplicate prevention (409 conflict).
  - `GET /api/match-candidates`: List match suggestions with filters (`status`, `tier`, `source_id`, `organisation_id`).
  - `POST /api/match-candidates/scan/{source_id}`: Trigger entity matching scan for a source.
  - `GET /api/sources/{id}/observations`: Chronological observation heartbeat history.
- Registered router in `backend/app/api/main.py`.
- Added response models in `backend/app/models/__init__.py` and serializers in `backend/app/api/serializers.py`.
- Regenerated frontend TypeScript types via `openapi-typescript` to `frontend/src/api/schema.d.ts`.

### Phase F: Verification Test Suite
- Implemented `backend/tests/test_m3_1_backend.py` (14 passing tests):
  - Database migration M3_001 on fresh and existing databases, idempotency, data preservation, and rollback.
  - Observation heartbeat lifecycle: new snapshots, unchanged short-circuits (0 duplicate snapshots), failed crawls, and monotonic sequences.
  - Entity resolver: strong match, possible match, lookalike branch isolation, suite distinction, corporate phone isolation, and candidate deduplication.
  - Fact ownership: single-practice mapping, multi-branch heading resolution, corporate unassigned fallback, and non-overwrite.
  - API validation: CRUD, source linking, candidate querying, observation sequence queries, error codes (404, 409, 422), and strict absence of auto-merging routes.

---

## 3. Current Test Status

- **Total Backend Tests:** **135 / 135 passed** (`pytest backend/tests -v` in ~5.7s)
  - 18 M0 conformance tests
  - 29 M1 vertical-slice tests
  - 20 M2 AI extraction & evidence verification tests
  - 28 M3.1 organisation registry, observation heartbeat concurrency, entity resolver & API tests
  - 15 M3.0 domain contracts and schema validation tests
  - 25 M3.0 synthetic corpus and ground-truth validation tests
- **Frontend Typecheck (`npm run typecheck`):** **0 errors**
- **Frontend Production Build (`npm run build`):** **0 errors** (43 modules transformed)
- **Git Whitespace Integrity (`git diff --check`):** **Clean (0 errors)**

---

## 4. Git State & Working Tree

- **Current Branch:** `main`
- **Baseline Checkpoint Commit:** `6741e90`
- **Status:** M3.1 complete; working tree has modified models, serializers, migration script, and new intelligence modules and tests.
- **Git Commit:** None created (adhering strictly to execution boundary; awaiting human commit authorisation).

### Files Created in M3.1:
- `backend/app/intelligence/observation_tracker.py`
- `backend/app/intelligence/entity_resolver.py`
- `backend/app/intelligence/fact_ownership.py`
- `backend/app/api/routers/organisations.py`
- `backend/app/api/organisations.py`
- `backend/tests/test_m3_1_backend.py`

### Files Modified in M3.1:
- `backend/app/db/orm.py` (added 4 M3 ORM models and Source relationships)
- `backend/app/db/migrations.py` (added M3_001 schema inspection, migration, and rollback)
- `backend/app/db/base.py` (integrated M3_001 in `init_db`)
- `backend/app/ingest/pipeline.py` (integrated observation heartbeat recording)
- `backend/app/models/__init__.py` (added M3 API request and response schemas)
- `backend/app/api/serializers.py` (added M3 ORM serializers)
- `backend/app/api/main.py` (registered organisations router)
- `frontend/src/api/schema.d.ts` (regenerated TypeScript types from OpenAPI)
- `docs/AI_DEVELOPMENT_HANDOFF.md` (updated handoff state)

---

## 5. Strict Boundaries Observed

- Strictly zero implementation of M3.2 (discrepancy detection), M3.3 (change events), M3.4 (freshness policies), M3.5 (vacancy lifecycle), M3.6 (human review workflow), or M3.7 (full M3 closure).
- Zero automatic organisation merges; all match proposals require human reviewer approval.
- Zero live web scraping or external network calls; 100% synthetic local fixtures.
- Zero database changes outside the four approved M3.1 tables.
- Zero Git commits or pushes executed.

---

## 6. Milestone M3.2 Completed State (Cross-Source Discrepancy Detection & Conflict Identification)

- **Date:** 2026-11-06 (Remediated: 2026-11-07)
- **Status:** Completed, Remediated & Verified
- **Baseline Git Commit:** `6741e9045c3f507316c7b63bd3993dcc28b6d467`
- **Total Backend Tests Passing:** 166 passing (18 M0 + 29 M1 + 20 M2 + 28 M3.1 + 31 M3.2 + 15 M3 contracts + 25 M3 corpus)
- **Frontend Verification:** TypeScript check (`npm run typecheck`) and production build (`npm run build`) passing with 0 errors.

### 6.1 Changes Delivered in M3.2 Remediation
1. **Historical Record Preservation & Migration M3_002 (`backend/app/db/migrations.py`, `backend/app/db/orm.py`):**
   - Discrepancies table foreign keys strictly defined as `ON DELETE RESTRICT` (replacing legacy CASCADE) for `organisation_id`, `fact_a_id`, and `fact_b_id`.
   - Attempting to delete an organisation or either referenced fact is restricted (raises `IntegrityError`), preserving historical evidence and fact relationships.
   - Migration `migrate_m3_002_discrepancies` safely migrates legacy schemas to `RESTRICT` using automated `.bak` backups and SQLite foreign-key validation.
   - Preserves canonical ordering check constraint `fact_a_id < fact_b_id` and unique constraint `(fact_a_id, fact_b_id)`.
2. **Temporal Divergence Correctness (`backend/app/intelligence/discrepancy_detector.py`):**
   - Temporal divergence strictly requires explicit, non-overlapping fact-specific validity periods (e.g. `effective from January 2024` vs `effective from March 2025`).
   - Generic page update timestamps (e.g. `Page updated March 2025`), crawl timestamp differences, and overlapping/compatible validity periods are strictly rejected from temporal divergence classification.
3. **Scope and Ambiguity Policy Consistency:**
   - Preserved `NULL scope != NULL scope`: unscoped differences are suppressed and NEVER produce a `value_mismatch`.
   - Incompatible known scopes (e.g. `clinic` vs `pathology`) suppress comparison (0 discrepancy).
   - Ambiguous/unparseable telephone numbers (missing area codes), freeform/unstructured opening hours, and descriptive addresses route to `ConflictType.AMBIGUITY`.
4. **Evidence Integrity Verification:**
   - Strict validation: quotes must match snapshot text, snapshot IDs must match, and character offsets must be within bounds.
   - Fabricated, mismatched, invalid, or rejected evidence suppresses value mismatch detection.
5. **Conservative Normalisation:**
   - Australian telephone numbers normalized to canonical E.164. Format variations that normalize identically produce 0 discrepancy (F10). Conflicting valid numbers produce `value_mismatch` (F11).
   - Address street abbreviations (`st` vs `street`, `pde` vs `parade`) normalize safely without semantic guessing.
6. **Future Review Fields & REST API (`backend/app/api/routers/discrepancies.py`):**
   - Review fields (`resolution_notes`, `resolved_by`, `resolved_at`) default to `null`; `status` defaults to `'detected'`.
   - Strictly zero endpoints or services for auto-resolving, approving facts, choosing winning sources, or modifying fact values. Operator review workflow remains reserved for M3.6.
7. **Comprehensive Test Suite (`backend/tests/test_m3_2_backend.py`):**
   - 31 comprehensive tests covering migrations, historical record preservation, temporal classification, evidence integrity, conservative normalisation, ground truth (F10-F12), and REST API endpoints.

---

## 7. Milestone M3.3 Completed State (Temporal Change Detection & Observation Delta Engine)

- **Date:** 2026-11-07
- **Status:** Completed, Verified & Signed Off
- **Baseline Git Commit:** `6741e9045c3f507316c7b63bd3993dcc28b6d467`
- **Total Backend Tests Passing:** 209 passing (18 M0 + 29 M1 + 20 M2 + 28 M3.1 + 40 M3.2 + 34 M3.3 + 15 M3 contracts + 25 M3 corpus)

### 7.1 Key Deliverables in M3.3
1. **Migration M3_003 (`backend/app/db/migrations.py`, `backend/app/db/orm.py`):**
   - Added `change_events` table with immutable snapshot provenance and strict foreign-key integrity.
   - All foreign keys (`organisation_id`, `source_id`, `from_observation_id`, `to_observation_id`, `from_snapshot_id`, `to_snapshot_id`, `from_fact_id`, `to_fact_id`) configured with `ON DELETE RESTRICT` or `NO ACTION`.
   - Idempotent migration with automated `.bak` backups.
2. **Deterministic Delta Engine (`backend/app/intelligence/change_detector.py`):**
   - Heartbeat comparison detecting additions (`fact_added`), modifications (`fact_modified`), and absences/omissions (`fact_omitted` / `fact_disappeared`).
   - Absence counting: increments on complete, eligible snapshot extractions; resets when fact reappears; freezes during failed/incomplete crawls or identical unchanged heartbeats.
   - Reappearance handling: when a previously omitted fact reappears, creates `fact_reappeared` event, resets consecutive absences to 0, and preserves full historical provenance without overwriting historical facts.
   - Ground truth verification against F13 (`F13_v1_baseline`, `F13_v2_modified`, `F13_v3_omitted`, `F13_unchanged`, `F13_err_failure`, `F13_omit_extraction`).
3. **REST API & Router (`backend/app/api/routers/changes.py`):**
   - `GET /api/changes/organisation/{id}`, `GET /api/changes/source/{id}`, `GET /api/changes/{id}`.
4. **Test Suite (`backend/tests/test_m3_3_backend.py`):**
   - 34 tests covering additions, value changes, absence counting, failure freezing, unchanged heartbeats, reappearance, and database foreign-key integrity.

---

## 8. Milestone M3.4 Completed State (Evidence-Aware Information Freshness & Monitoring Policy Engine)

- **Date:** 2026-11-07
- **Status:** Completed, Verified & Signed Off
- **Baseline Git Commit:** `6741e9045c3f507316c7b63bd3993dcc28b6d467`
- **Total Backend Tests Passing:** **232 passing** (18 M0 + 29 M1 + 20 M2 + 28 M3.1 + 40 M3.2 + 34 M3.3 + 23 M3.4 + 15 M3 contracts + 25 M3 corpus)
- **Frontend Verification:** TypeScript check (`npm run typecheck`) and production build (`npm run build`) passing with 0 errors.

### 8.1 Four Distinct Freshness Dimensions
1. **Observation Freshness (`evaluate_source_observation_freshness`):**
   - Evaluates when PracticeSignal last observed a registered source from `source_observations`.
   - Tracks `last_attempted_at`, `last_successful_observation_at`, `last_new_snapshot_at`, `last_unchanged_observation_at`, `last_complete_parsing_at`, and `last_complete_extraction_at`.
   - Failed or incomplete crawls remain visible (`latest_attempt_failed: true`, `error_details`) but never overwrite or reset the currency of the last valid successful observation.
   - Sources with zero successful crawls are accurately classified as `unobservable`.
2. **Content Freshness (`evaluate_source_content_freshness`):**
   - Evaluates when the source's observed content actually changed, anchored to `PageSnapshot.fetched_at` and `content_hash`.
   - Repeated unchanged observation heartbeats (`success_unchanged`) confirm continued observability without resetting content age.
3. **Fact Freshness (`evaluate_fact_freshness`):**
   - Evaluates when factual information was explicitly published, updated, or made effective based **strictly on fact-specific temporal evidence** in the quote (e.g. `effective from 1 January 2026`, `hours updated April 2025`, `notice published 10 January 2020`).
   - Rejects generic page update/maintenance metadata (e.g. `Page updated March 2026`).
   - Never infers fact freshness from observation recency (`observed_at`, `fetched_at`) or database timestamps.
   - Undated facts are classified as `UNKNOWN`, never falsely marked `STALE`.
4. **Evidence Verification Freshness (`evaluate_evidence_verification_freshness`):**
   - Evaluates when supporting evidence was captured and verifies verbatim substring integrity against the snapshot DOM text.
   - A valid quotation remains verifiable even when the underlying fact is decades old.

### 8.2 Monitoring Policy Engine & Zero Migrations
- **Dynamic Derivation:** 100% derived on demand from existing tables (`source_observations`, `page_snapshots`, `facts`, `evidence`, `change_events`). **Zero database schema migrations required.**
- **Policy Configuration (`FreshnessPolicy`):**
  - Default policy `ps_freshness_v1`:
    - `identity`: 90 days
    - `operational`: 30 days
    - `services`: 60 days
    - `hours`: 30 days
    - `general`: 60 days
    - `aging_threshold_ratio`: 0.80 (80% of threshold)
  - Source-specific threshold overrides supported.
  - Deterministic evaluation via injected reference clock (`reference_time`).
  - Zero LLMs used for freshness evaluation.

### 8.3 REST API Endpoints (`backend/app/api/routers/freshness.py`)
- `GET /api/freshness/policy`: Returns active default policy and thresholds.
- `GET /api/freshness/source/{source_id}`: Source observation and content freshness.
- `GET /api/freshness/fact/{fact_id}`: Comprehensive 4-dimensional fact freshness detail.
- `GET /api/freshness/organisation/{organisation_id}`: Organisation summary across all confirmed sources and active facts.
- `POST /api/freshness/evaluate`: Dynamic evaluation supporting custom reference time and category threshold overrides.
- Exported updated OpenAPI 3.1 specification to `frontend/openapi.json` (32 total endpoints).
- Regenerated TypeScript schema definitions via `npx openapi-typescript` to `frontend/src/api/schema.d.ts`.

### 8.4 Verification Test Suite (`backend/tests/test_m3_4_backend.py`)
- 33 comprehensive tests covering:
  - Fresh, aging, and stale observation classifications.
  - Heartbeat re-observations preserving content observation timestamp.
  - Preservation of last valid observation across failed/incomplete attempts.
  - Completeness isolation (partial parsing / failed extraction).
  - Explicit fact dates, undated facts (`unknown`), fresh crawl with old fact (`stale`), generic metadata rejection.
  - Date precision ranges (DAY, MONTH, YEAR, UNKNOWN): incomplete precision spanning policy boundaries resolves to `unknown`; complete precision within boundary retains valid classification; missing year never inferred.
  - Content observation timestamp sequence: unchanged heartbeats do not advance content observation timestamp; crawler observation timing decoupled from publisher modification date.
  - Evidence verification vs fact currency: `captured_at` vs `checked_at` / `evaluated_at` separation.
  - Policy threshold selection: explicit independent source-level observation threshold (`source_observation`), content threshold (`source_content`), category-specific fact threshold (`fact_category`), and immutable snapshot integrity (`immutable_snapshot_integrity`).
  - Unobservable sources and organisation summary.
  - Zero change-event mutation guarantee.
  - All REST API endpoints (`/policy`, `/source/{id}`, `/source/{id}/content`, `/fact/{id}`, `/organisation/{id}`, `/evaluate`) and error conditions.

---

## 9. Completed Work in M3.5

### 9.1 Database Migration M3_004 (`backend/app/db/migrations.py`)
- Created additive migration `migrate_m3_004_opportunities` and rollback `rollback_m3_004_opportunities`:
  - Adds `opportunities` table with foreign keys referencing `organisations.id`, `sources.id`, `page_snapshots.id` (first/last) using `ON DELETE RESTRICT` to preserve historical evidence and relationships.
  - Adds `consecutive_absences` integer counter defaulting to 0.
  - Adds `closure_evidence_quote` text column for verified closure quotes.
  - Automatically detects and upgrades legacy tables declared with CASCADE to RESTRICT while preserving existing data.
  - Creates indices: `ix_opportunities_organisation_id`, `ix_opportunities_role_category`, `ix_opportunities_status`, `ix_opportunities_explicit_ad_id`, `ix_opportunities_source_id`.
  - Integrated into `init_db` in `backend/app/db/base.py`.

### 9.2 Deterministic Opportunity Engine (`backend/app/intelligence/opportunity_detector.py`)
- **Deterministic Identity Resolution:**
  1. Priority 1: Published job reference ID (`explicit_ad_id`, e.g. `GP-2026-04`).
  2. Priority 2: Stable slug or URL.
  3. Priority 3: Composite identity `(organisation_id, role_category, branch_locality, source_id)` — strictly omitting role title and remuneration so changes update the existing vacancy instead of creating duplicates.
  4. Priority 4: Ambiguity preserved across sources (zero speculative auto-merging).
- **Conservative Lifecycle State Transitions (`OpportunityStatus`):**
  - `advertised`: Complete, evidence-backed active vacancy posting.
  - `changed`: Existing vacancy observed with modified title, remuneration, or wording (preserves identity without duplicates).
  - `unconfirmed_prior`: Absent on 1st eligible complete observation (absence count = 1).
  - `no_longer_observed`: Absent across 2 consecutive eligible complete observations (absence count = 2).
  - `explicitly_closed`: Source explicitly publishes verified verbatim closure evidence quotation.
  - **Strict Architectural Invariant:** Status `filled` or `hired` is strictly forbidden.
- **Ineligible Observation Freezing:**
  - Failed crawls, incomplete crawls, partial parsing, partial extractions, and unchanged heartbeats (`success_unchanged`) freeze consecutive absence counters and produce zero status changes.
- **Extraction Omission Protection:**
  - If a vacancy is missing from extraction candidates but its reference or title tokens remain visible in raw snapshot text/HTML, disappearance is suppressed and absence counters are frozen.
- **Reappearance Handling:**
  - Previously unconfirmed, unobserved, or closed vacancies re-observed in eligible crawls are restored to `advertised`, resetting absences to 0 without creating duplicate records.

### 9.3 REST API Endpoints (`backend/app/api/routers/opportunities.py`)
- `GET /api/opportunities`: Paginated listing with query filters (`role_category`, `organisation_id`, `source_id`, `status`, `limit`, `offset`).
- `GET /api/opportunities/{id}`: Detailed vacancy read model with expanded organisation and source details (or typed 404 `opportunity_not_found`).
- `POST /api/opportunities/scan/{source_id}`: Deterministic opportunity scan returning scan metrics and detected opportunities (or typed 404 `source_not_found`).
- Exported updated OpenAPI 3.1 specification to `frontend/openapi.json` (35 total endpoints).
- Regenerated TypeScript schema definitions via `npx openapi-typescript` to `frontend/src/api/schema.d.ts`.

### 9.4 Verification Test Suite (`backend/tests/test_m3_5_backend.py`)
- 29 comprehensive tests covering:
  - Migration fresh install, idempotency, legacy CASCADE upgrade to RESTRICT, and clean rollback.
  - Foreign key RESTRICT enforcement on referenced organisations, sources, and snapshots.
  - Full F14 ground truth lifecycle sequence (v1 -> v2 -> v3a -> v3b -> v3c -> v3d).
  - Absence freezing on failed crawls, incomplete observations, partial parsing, partial extraction, and unchanged heartbeats.
  - Extraction omission defense suppressing disappearance when tokens remain in DOM.
  - Title and remuneration changes preserving vacancy identity without duplicate creation.
  - Branch location isolation preserving distinct opportunities across clinics.
  - Strict architectural prohibitions against `filled` or `hired` statuses.
  - Mandatory verbatim quotation on `explicitly_closed`.
  - Evidence integrity and character offset validation (`verify_opportunity_evidence`, `verify_closure_evidence`, and rejection of invalid offsets / fabricated quotes).
  - Explicit closure notices character offsets and quote slice matching in PageSnapshot text.
  - Historical preservation across full lifecycle sequences (`advertised -> changed -> explicitly_closed -> advertised` and `advertised -> unconfirmed_prior -> no_longer_observed -> advertised`).
  - Ground truth audit for all six F14 synthetic fixtures in `manifest.json`.
  - All REST API endpoints and error conditions.

---

## 10. Current Test & Build Status

- **Total Backend Tests:** **327 / 327 passed** (`pytest backend/tests` in 37.86s)
  - 18 M0 conformance tests
  - 29 M1 vertical-slice tests
  - 20 M2 AI extraction & evidence verification tests
  - 28 M3.1 organisation registry & entity resolver tests
  - 40 M3.2 cross-source discrepancy detector tests
  - 34 M3.3 temporal change detection & delta engine tests
  - 33 M3.4 evidence-aware freshness & monitoring policy tests
  - 29 M3.5 healthcare workforce vacancy lifecycle intelligence tests
  - 28 M3.6 human review & auditable decision workflow tests
  - 28 M3.7 practice lifecycle intelligence tests (closure, relocation, acquisition)
  - 15 M3.0 domain contracts and schema validation tests
  - 25 M3.0 synthetic corpus and ground-truth validation tests
- **Frontend Typecheck (`npm run typecheck`):** **0 errors**
- **Frontend Production Build (`npm run build`):** **0 errors** (44 modules transformed)
- **Git Commit Checkpoint:** Baseline HEAD `6741e9045c3f507316c7b63bd3993dcc28b6d467` on `main`. 0 commits made, 0 pushes made.

---

## 11. Milestone 3.6 Implementation Summary

**M3.6: Human Review, Evidence Verification & Auditable Decision Workflow** is fully implemented and verified:
1. **Database Migration M3_005:**
   - Additive migration adding `review_actions` table with `ON DELETE RESTRICT` foreign key to `evidence(id)`.
   - Comprehensive indexing on `(entity_type, entity_id)`, `action`, `reviewer`, `created_at`.
   - SQLite triggers `prevent_review_actions_update` and `prevent_review_actions_delete` enforcing append-only immutability.
2. **Central Review Service & Domain Aggregation:**
   - Unified queue triage covering all intelligence domains.
   - Grounded context retrieval returning verbatim quotation text and character offsets.
3. **Architectural Invariant Protections:**
   - Zero organisation merging, zero fact overwrite, append-only audit trail.

---

## 12. Milestone 3.7 Implementation Summary

**M3.7: Practice Lifecycle Intelligence (Closure, Relocation & Corporate Acquisition Detection)** is fully implemented and verified:
1. **Database Migration M3_006:**
   - Additive migration adding `practice_lifecycle_events` table with `ON DELETE RESTRICT` foreign keys to `organisations(id)`, `sources(id)`, `page_snapshots(id)`, `evidence(id)`.
   - Idempotency key uniqueness, comprehensive indexing, timestamped backup, and clean rollback.
2. **Deterministic Lifecycle Detection Engine (`backend/app/intelligence/practice_lifecycle_detector.py`):**
   - **Explicit Permanent Closure:** Fact-specific verbatim evidence, exact character offsets, date precision.
   - **Branch Closure Isolation:** Isolates branch scope; sibling branches and parent organisation remain active.
   - **Negative Controls:** Temporary holiday, emergency, lunch, and renovation closures strictly distinguished from permanent closure.
   - **Possible Closure via Absence:** Repeated absence creates `potential_needs_review` / `no_longer_observed`, never premature confirmed closure.
   - **Explicit Relocation & Address Diff:** Preserves old and new addresses, isolates branch additions on multi-branch pages.
   - **Corporate Acquisition Intelligence:** Strict negative controls (shared domain, phone, directors, branding rejected); preserves distinct entities with zero auto-merge.
3. **Review Queue & Frontend Integration:**
   - Lifecycle events integrated into M3.6 review service and `ReviewQueuePage.tsx` with grounded modal inspector and decision submission.
4. **REST API Endpoints (`backend/app/api/routers/lifecycle.py`):**
   - Endpoints for event listing, filters, detail inspection, organisation history, branch history, and scan orchestration.
