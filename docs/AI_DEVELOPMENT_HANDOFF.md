# PracticeSignal — AI Development Handoff (Fresh Session Continuation)

> **Document purpose:** Self-contained persistent engineering handoff allowing any coding agent or engineer to resume work immediately without prior session history.
> **Current Milestone:** **M2 Audit Remediation Complete** (AI Extraction, Evidence Verification & Audit Remediation)
> **Baseline Checkpoint Commit:** `d544e10`
> **Date:** 5 November 2026
> **Operating Environment:** Windows 11 Enterprise (Build 26300), Python 3.12.6 (.venv), Node v24.20.0, npm 11.19.0, Ollama 0.40.14 (`llama3.2:3b`), CPU-only host.

---

## 1. Project Context & Principles

PracticeSignal is an evidence-first healthcare practice & workforce intelligence prototype.
- **Core Principle:** The LLM proposes information. Python verifies it. The database preserves its provenance. Humans remain responsible for approval.
- **Zero-Cost Policy:** ₹0 budget. Zero cloud AI APIs, zero external network fetching in tests/evaluation, zero paid services. Local Ollama (`llama3.2:3b`, pinned) performs all inference.
- **Milestone Scope Gate:** M0 (Foundation), M1 (Vertical Slice), and M2 (AI Extraction & Evidence Verification) are **complete**. M3 (Conflicts, Scope Intelligence, Dedupe) and M4 (Review Dashboard & Exports) are **strictly deferred**.

---

## 2. Completed Work in M2 (Including Audit Remediation)

### Phase A: Local Model Feasibility & Benchmark
- Verified Ollama 0.40.14 running at `http://127.0.0.1:11434` with model `llama3.2:3b` (ID: `a80c4f17acd5`, 2.0 GB).
- Verified structured output via JSON Schema (`format: <json_schema>`, `temperature: 0.0`).
- Implemented real benchmark runner in `scripts/benchmark_ai.py` (stdlib urllib-only to preserve M0 conformance tests).
- Benchmarked on F2 (Services) and F5 (Vacancies): steady **11.2 – 12.6 tokens/sec** on CPU, 100% valid JSON schema rate, verbatim evidence quotes.
- Recorded benchmark details in `docs/RESULTS.md` and iteration log in `docs/prompt-engineering-log.md`.

### Phase B: Ollama Adapter (`backend/app/ai/client.py`)
- Single owner of local Ollama runtime communication.
- Model tag pinned to `llama3.2:3b`.
- Concurrency limit strictly enforced (maximum 1 inference call at a time using async/thread lock) to protect CPU resources.
- Error handling maps HTTP/transport issues to typed exceptions: `OllamaUnavailable` (503), `OllamaTimeout` (504), `OllamaModelNotFound` (503), `OllamaInferenceError` (502), `OllamaInputTooLarge` (413).
- Health checks probe Ollama service and verify model tag availability.

### Phase C: Extraction Schema & Prompt Store (`backend/app/ai/prompt_store.py`)
- Reads versioned templates from `prompts/*.md` (preserves historical stubs).
- Embeds injection-guard framing (`prompts/injection_guard_v1.md`).
- Binds source-specific `intended_fields` allowlist to structurally constrain output fields.
- Generates strict Pydantic JSON Schema for Ollama's `format` parameter.
- Computes SHA-256 payload hash for idempotency and audit tracking.

### Phase D: Deterministic Evidence Verification (`backend/app/ingest/`)
- `evidence_verifier.py`:
  - Verifies field is in source allowlist (D6).
  - Exact and whitespace-tolerant quote matching against snapshot text.
  - Python calculates authoritative character offsets `[start_offset..end_offset)`.
  - **Repeated Quotation Disambiguation (Requirement 6, D37):** `resolve_quote_offsets` maps repeated quotes to their enclosing headings. Ambiguous occurrences across distinct scopes route to `needs_review` rather than silently picking the first match.
  - **Value-to-evidence validation (Requirement 4):** verifies raw value is genuinely grounded in quote.
  - **Business-contact rule (D6/D10):** rejects phone/email originating from testimonial/review context (F4).
  - **External booking rejection (F3):** prevents external booking links from becoming official websites.
  - **Prompt injection guard (D10, D38, F6):** rejects injected instructions structurally; checks quote and section context window.
- `field_normalizer.py`:
  - Preserves original raw value always (Requirement 5).
  - Normalizes Australian phones to E.164, emails to lowercase, URLs to canonical form.
  - Unparseable or complex hours set `normalized_value = None` and flag `needs_review`.
- `scope_assignment.py`:
  - Assigns operational scope (`clinic`, `pathology`, `reception`, etc.) deterministically from snapshot headings (D11).
  - Ambiguous headings route to `needs_review`.
  - Model proposal is an advisory fallback only when no heading exists.

### Phase E: Database & Idempotency (`backend/app/db/`)
- Incremental SQLite migration (D9, D35, D36) adding:
  - `evidence`: immutable quote excerpts with exact offsets.
  - `facts`: candidate facts with scope, raw value, normalized value, nullable timezone, prompt_run_id lineage, evidence link, and unapproved status (`candidate`, `needs_review`, `rejected`).
  - `prompt_runs`: immutable audit of every inference call (latency, tokens, validation errors, evidence checks summary).
- **Timezone Data Integrity (Requirement 3, D35):** `facts.timezone` is `Mapped[str | None]` with no automatic Brisbane assumption.
- **Direct PromptRun Lineage (Requirement 4, D36):** `facts.prompt_run_id` references `prompt_runs.id` on delete `SET NULL` with index.
- **Safe SQLite Migration (`backend/app/db/migrations.py`):** 12-step table rebuild with automatic `.bak` pre-migration backup and verified rollback.
- Idempotency: equivalent extractions (matching snapshot ID and payload hash) skip model inference (D4).

### Phase F: FastAPI Endpoints & Serializers
- `POST /api/snapshots/{snapshot_id}/extract`: triggers extraction (with optional `force=true` query param).
- `GET /api/snapshots/{snapshot_id}/facts`: lists candidate facts with evidence quote, offsets, and `prompt_run_id`.
- `GET /api/snapshots/{snapshot_id}/prompt-runs`: lists prompt runs for snapshot.
- `GET /api/facts`: global facts listing with status/field/snapshot filters.
- `GET /api/facts/{fact_id}`: single fact detail.
- `GET /api/prompt-runs`: audit log listing.
- `GET /api/health`: probes local Ollama status and reports live connectivity.

### Phase G: Frontend Evidence & Candidate Display (`frontend/`)
- Regenerated TypeScript contracts from live OpenAPI schema in `frontend/src/api/schema.d.ts` (D19).
- Enhanced `SnapshotPage.tsx`:
  - "Extract with Local AI" / "Force Re-run" controls.
  - Candidate Facts table with prominent unapproved candidate badge.
  - "View in Text" button highlights exact evidence offsets in parsed snapshot text.
  - Local AI Prompt Runs table showing latencies, tokens, tokens/sec, and schema status.
- Added `FactsPage.tsx` (`#/facts`): global candidate facts explorer with status filter.
- Added `PromptRunsPage.tsx` (`#/prompt-runs`): global AI inference audit log.
- Strict typecheck (`tsc --noEmit`) and Vite production build: 0 errors.

---

## 3. Current Test Status

- **Total Backend Tests:** **67 / 67 passed** (`pytest backend/tests -v` in ~11.2s)
  - 18 M0 conformance tests
  - 29 M1 vertical-slice tests
  - 20 M2 AI extraction, evidence verification, migration, repeated quotation, prompt run lineage, and visible injection tests
- **Frontend Typecheck (`npx tsc --noEmit`):** **0 errors**
- **Frontend Production Build (`npm run build`):** **0 errors** (43 modules transformed)

---

## 4. Git State & Working Tree

- **Current Branch:** `main`
- **Baseline Checkpoint Commit:** `d544e10`
- **Status:** M2 remediation patch applied and verified green across backend and frontend.

### Files Modified & Created for M2 Remediation:
- Created: `backend/app/db/migrations.py` (safe SQLite migration & rollback)
- Modified: `backend/app/db/orm.py` (nullable timezone, prompt_run_id column & relationship)
- Modified: `backend/app/db/base.py` (idempotent migration trigger in init_db)
- Modified: `backend/app/models/__init__.py` (FactRead prompt_run_id and nullable timezone)
- Modified: `backend/app/api/serializers.py` (prompt_run_id in to_fact_read)
- Modified: `backend/app/ingest/evidence_verifier.py` (repeated quote disambiguation, visible injection context)
- Modified: `backend/app/extraction/candidates.py` (prompt_run_id assignment, explicit evidence timezone)
- Modified: `backend/tests/test_m2_ai_and_evidence.py` (4 new comprehensive tests for visible injection, repeated quotes, lineage, migration)
- Modified: `frontend/src/api/schema.d.ts` (updated TypeScript contracts for FactRead)
- Modified: `docs/RESULTS.md` (corrected Ollama native benchmark calculations and evaluation breakdown)
- Modified: `docs/DECISION_LOG.md` (recorded D35–D38 remediation decisions)
- Modified: `docs/AI_DEVELOPMENT_HANDOFF.md` (updated progress and test counts)

---

## 5. Strict Boundaries

- **M2 Remediation Only:** Milestone 3 (cross-source conflict detection, entity resolution, opportunity lifecycle management) has **NOT** been started.
- Await human review and formal milestone authorization before proceeding to M3.
