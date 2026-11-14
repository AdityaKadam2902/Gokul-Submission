# PracticeSignal

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.12.6-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19.0-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.6-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![SQLite](https://img.shields.io/badge/Storage-SQLite_WAL_(Anti--Tamper_Triggers)-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Tests](https://img.shields.io/badge/Automated_Tests-299_Passing_(100%25)-brightgreen)](https://github.com/AdityaKadam2902/Gokul-Submission)
[![Cost](https://img.shields.io/badge/API_Cost-%E2%82%B90_Zero--Cost-success)](https://github.com/AdityaKadam2902/Gokul-Submission)
[![Architecture](https://img.shields.io/badge/Architecture-Deterministic_Hybrid_Intelligence-orange)](docs/TECHNICAL_CHALLENGES.md)

**A local-first, evidence-grounded healthcare practice and workforce intelligence platform.**  
*Resolving practice identities, tracking operational lifecycles, detecting cross-source factual conflicts, and monitoring workforce vacancies across uncoordinated public web sources with byte-level provenance and storage-enforced auditability.*

</div>

---

## Table of Contents

- [1. Executive Summary](#1-executive-summary)
- [2. System Architecture](#2-system-architecture)
- [3. Core Engineering Breakthroughs & Complex Challenges Solved](#3-core-engineering-breakthroughs--complex-challenges-solved)
  - [3.1 The Shared Infrastructure vs. Corporate Acquisition Dilemma](#31-the-shared-infrastructure-vs-corporate-acquisition-dilemma)
  - [3.2 Exact Character-Offset Grounding Across Normalised DOM Trees](#32-exact-character-offset-grounding-across-normalised-dom-trees)
  - [3.3 Temporal Absence Accounting vs. Premature Opportunity Closure](#33-temporal-absence-accounting-vs-premature-opportunity-closure)
  - [3.4 Practice Lifecycle Disambiguation (Permanent vs. Holiday Negative Controls)](#34-practice-lifecycle-disambiguation-permanent-vs-holiday-negative-controls)
  - [3.5 Storage-Engine Anti-Tamper Immutability (SQLite Triggers)](#35-storage-engine-anti-tamper-immutability-sqlite-triggers)
- [4. Intelligence Engines & Domain Capabilities](#4-intelligence-engines--domain-capabilities)
- [5. Quickstart & Reproducible Setup](#5-quickstart--reproducible-setup)
- [6. Application Workspaces & UI Tour](#6-application-workspaces--ui-tour)
- [7. API Reference & Contract Architecture](#7-api-reference--contract-architecture)
- [8. Automated Verification & Test Suite Matrix](#8-automated-verification--test-suite-matrix)
- [9. Data Governance, Safety & Zero-PII Compliance](#9-data-governance-safety--zero-pii-compliance)
- [10. Repository Directory Structure](#10-repository-directory-structure)

---

## 1. Executive Summary

Healthcare operational data—such as clinic locations, emergency contact numbers, operating schedules, workforce vacancies, and branch closures—is distributed across thousands of uncoordinated public clinic sites, corporate portals, and health directories. 

In conventional operations, tracking these changes relies on either manual human monitoring or black-box, probabilistic Large Language Models (LLMs). **In healthcare intelligence, unverified probabilistic AI fails catastrophically**:
- LLMs routinely **hallucinate** non-existent phone numbers, doctor accreditations, and addresses.
- Naïve fuzzy matchers **collapse distinct legal practices** into a single corrupted record simply because they share a medical precinct address or a `1300` directory number.
- Keyword scrapers produce **false closure emergencies** when clinics publish temporary notices like *"Closed for Good Friday"*.
- When an advertised GP position disappears from a careers page, scrapers falsely infer that the role was *"Hired"* or *"Filled"*, when in reality the listing temporarily expired or the website underwent a CMS migration.

### The PracticeSignal Paradigm
PracticeSignal solves this by enforcing a **strict separation of concerns between probabilistic candidate extraction and deterministic verification**:

1. **Untrusted LLM Boundary:** A locally running, zero-cost open model (`llama3.2:3b` via Ollama) proposes candidate fields and supporting text quotes. It has zero network access, zero shell access, and **zero authority to declare facts**.
2. **Deterministic Evidence Verification:** Every proposed quote is mathematically verified against the stored, immutable page snapshot at exact character coordinates (`start_offset`, `end_offset`). If a quote deviates by even a single character, it is quarantined.
3. **Formal State Automata:** Entity resolution, temporal change deltas, absence accounting, and lifecycle events are governed by **finite-state automata** with explicit negative controls.
4. **Append-Only Immutability:** Decisions are recorded through a dedicated Human Review Queue, backed by native SQLite triggers that make it physically impossible to alter or delete historical audit records.

---

## 2. System Architecture

PracticeSignal is engineered as a local-first system with a decoupled single-page application frontend, an asynchronous FastAPI backend service, seven deterministic intelligence engines, and an ACID-compliant SQLite storage engine operating in Write-Ahead Logging (WAL) mode.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              PRESENTATION LAYER (React 19 + TypeScript)                │
│   Sources Overview  │  Snapshot Inspector  │  Candidate Facts  │  Review Queue (Triage)│
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ Type-Safe OpenAPI 3.1 Fetch Client
┌───────────────────────────────────────────▼────────────────────────────────────────────┐
│                              API ROUTING & LIFESPAN (FastAPI + Python 3.12)           │
│   /api/sources   │  /api/snapshots   │  /api/organisations  │  /api/discrepancies      │
│   /api/changes   │  /api/freshness   │  /api/opportunities  │  /api/reviews            │
└─────────────────────┬─────────────────────────────────────────────────┬────────────────┘
                      │                                                 │
┌─────────────────────▼─────────────────────────┐     ┌─────────────────▼────────────────┐
│      INGESTION & SNAPSHOT PIPELINE            │     │  LOCAL INFERENCE BOUNDARY        │
│ • Deterministic HTML Text Stream Assembly     │     │ • Pinned Ollama llama3.2:3b      │
│ • SHA-256 Content Hashing & Short-Circuit     │◄────┤ • Zero-Cost / Offline Execution  │
│ • Heading Offset Extraction [(lvl, span)]     │     │ • Untrusted Candidate Proposals  │
└─────────────────────┬─────────────────────────┘     └─────────────────┬────────────────┘
                      │                                                 │
                      └────────────────────────┬────────────────────────┘
                                               │
┌──────────────────────────────────────────────▼─────────────────────────────────────────┐
│                    DETERMINISTIC VERIFICATION & INTELLIGENCE ENGINES                  │
│                                                                                        │
│  [M2: Evidence Verifier]          Exact byte-level character coordinate subslice check │
│  [M3.1: Entity Resolver]          Conservative 3-tier matcher with negative controls   │
│  [M3.2: Discrepancy Detector]     Pairwise canonical fact conflict identification      │
│  [M3.3: Change Delta Engine]      Temporal snapshot difference & heartbeat accounting  │
│  [M3.4: Freshness Engine]         4D dimensional freshness & observation staleness     │
│  [M3.5: Vacancy Tracker]          Monotonic absence automaton (never infer "hired")    │
│  [M3.6: Central Review Service]   Human triage workspace with allowed transition gates │
│  [M3.7: Practice Lifecycle]       Closure, branch relocation & acquisition detection   │
└──────────────────────────────────────────────┬─────────────────────────────────────────┘
                                               │
┌──────────────────────────────────────────────▼─────────────────────────────────────────┐
│                    STORAGE ENGINE (SQLite with WAL & Database Triggers)                │
│ • 14 Relational Tables (Additive Migrations M3_001–M3_006)                             │
│ • BEFORE UPDATE / BEFORE DELETE Native C-Triggers (Audit Append-Only Guarantee)       │
│ • Idempotency Constraints (Prevent Duplicate Crawler Ingestions)                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Core Engineering Breakthroughs & Complex Challenges Solved

The core technical value of PracticeSignal lies in the sophisticated software engineering algorithms designed to handle edge cases that defeat conventional AI pipelines. *(For full mathematical and architectural details, see [`docs/TECHNICAL_CHALLENGES.md`](docs/TECHNICAL_CHALLENGES.md)).*

### 3.1 The Shared Infrastructure vs. Corporate Acquisition Dilemma
* **The Problem:** In healthcare, independent general practitioners frequently co-locate inside a shared medical precinct, share a single reception desk, or advertise through a common `1300` aggregator number. Naïve similarity matching collapses independent medical practices into a single corrupted entity.
* **The Solution (`entity_resolver.py`):** Designed a 3-tier entity resolution automaton:
  - **Tier 1 (High Confidence):** Exact normalized primary domain match, OR exact phone match combined with string similarity $\ge 0.90$.
  - **Tier 2 (Probable Link):** High name similarity combined with matching locality/postcode. Flagged as a candidate, **never auto-merged**.
  - **Tier 3 (Inconclusive / Negative Controls):** Explicitly isolates shared 1300 numbers, suite multi-tenancy tokens, and distinct Australian Business Numbers (ABNs).
  - **Zero Auto-Merge Law (D39):** Under no circumstances does the system automatically overwrite organisation records. All links remain suggested until confirmed by an authorized human operator.

### 3.2 Exact Character-Offset Grounding Across Normalised DOM Trees
* **The Problem:** Converting HTML into parsed text disrupts byte indices due to whitespace collapse, non-breaking spaces (`&nbsp;`), and variable line endings. If an LLM proposes an extracted fact, conventional systems cannot prove where in the document it originated.
* **The Solution (`evidence_verifier.py`):** 
  - Constructed a single-pass DOM stream assembler that preserves verbatim source character indices.
  - Implemented multi-pass coordinate verification: Pass 1 executes an exact binary slice `snapshot.text[start:end] == quote`. Pass 2 performs bounded section searches to account for LLM boundary trims.
  - Implemented heading-based semantic disambiguation: when identical quotes appear across multiple branches, nearest preceding heading spans isolate the correct branch ownership.

### 3.3 Temporal Absence Accounting vs. Premature Opportunity Closure
* **The Problem:** When an advertised vacancy disappears from a clinic website, scrapers assume the position was filled. In reality, websites experience network timeouts, CMS redesigns, or temporary listing expirations.
* **The Solution (`opportunity_detector.py`):** Engineered a 5-state monotonic absence automaton:
  $$\text{advertised} \xrightarrow{\text{absence}=1} \text{unconfirmed\_prior} \xrightarrow{\text{absence}\ge 2} \text{no\_longer\_observed}$$
  - **Network Error Freeze:** If a crawler encounters an HTTP 500, timeout, or partial parse, absence counters are **frozen**. A technical failure is never mistaken for a real-world change.
  - **"Never Infer Hired" Invariant:** The database schema strictly prohibits status values of `filled` or `hired`.
  - **Verbatim Closure Evidence:** Transition to `explicitly_closed` strictly requires verbatim textual proof (e.g., *"Applications closed on 14 November"*).

### 3.4 Practice Lifecycle Disambiguation (Permanent vs. Holiday Negative Controls)
* **The Problem:** Clinic notices frequently contain words like *"Closed"*, triggering false emergency alerts when a clinic simply posts holiday hours or lunch breaks. Furthermore, closing a single branch must not mark the entire corporate group as closed.
* **The Solution (`practice_lifecycle_detector.py`):**
  - **Negative-Control Filter Matrix:** Regex filters trap temporary closures (e.g. `closed for the public holiday`, `closed for renovations`, `reopening tomorrow`) and permanently block them from the permanent closure pipeline.
  - **Branch-Level Scope Isolation:** The engine extracts geographical and branch tokens. If a notice states *"Our Box Hill branch is closing"*, the event is isolated to `is_branch_level = True`, leaving parent and sibling branches active.
  - **Date Precision Parsing:** Automatically parses ISO effective dates with precision classifications (`exact`, `month`, `quarter`).

### 3.5 Storage-Engine Anti-Tamper Immutability (SQLite Triggers)
* **The Problem:** Application-level audit logs can be bypassed by ORM bugs, direct SQL queries, or administrative scripts.
* **The Solution (`orm.py`):** Embedded native SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers directly onto the `review_actions` table:
  ```sql
  CREATE TRIGGER IF NOT EXISTS prevent_review_actions_update
  BEFORE UPDATE ON review_actions
  BEGIN
      SELECT RAISE(FAIL, 'UPDATE on review_actions is prohibited: audit records are append-only');
  END;
  ```
  Even if Python application code executes an update or delete query, the SQLite engine itself aborts the transaction immediately with a hardware-level integrity failure.

---

## 4. Intelligence Engines & Domain Capabilities

| Engine Module | Primary File | Core Capability & Algorithmic Design |
| :--- | :--- | :--- |
| **Observation Tracker** | `backend/app/intelligence/observation_tracker.py` | Assigns strictly monotonic observation sequence numbers per source. Employs SHA-256 hash checks to short-circuit unchanged crawls. |
| **Entity Resolver** | `backend/app/intelligence/entity_resolver.py` | 3-tier conservative identity matcher. Evaluates lexical similarity, domain normalization, and negative-control precinct filters. |
| **Discrepancy Detector** | `backend/app/intelligence/discrepancy_detector.py` | Canonical pairwise fact comparison (`fact_a_id < fact_b_id`). Detects contact divergence, address conflicts, and operating hour discrepancies. |
| **Temporal Change Engine** | `backend/app/intelligence/change_detector.py` | Snapshot delta engine. Employs cryptographic idempotency keys to record additions, modifications, and reappearances across crawls. |
| **Freshness Engine** | `backend/app/intelligence/freshness_engine.py` | Evaluates 4D information freshness: observation recency, content stability, verification age, and crawl cadence. |
| **Vacancy Tracker** | `backend/app/intelligence/opportunity_detector.py` | Healthcare recruitment vacancy lifecycle tracker. Monitors title/remuneration diffs and enforces consecutive absence accounting. |
| **Practice Lifecycle** | `backend/app/intelligence/practice_lifecycle_detector.py` | Detects permanent clinic closures, branch consolidations, and corporate acquisitions with negative-control filters. |
| **Review Service** | `backend/app/intelligence/review_service.py` | Unified human triage queue with strict state transition gates and append-only audit recording. |

---

## 5. Quickstart & Reproducible Setup

### System Prerequisites
- **Python:** $\ge 3.11$ (Verified on Python 3.12.6)
- **Node.js:** $\ge 18$ (Verified on Node v24.20.0, npm 11.19.0)
- **Local AI Runtime (Optional for extraction):** [Ollama](https://ollama.ai/) with `llama3.2:3b` pinned. *(All tests and pre-seeded demonstration workflows run completely offline without Ollama).*

### 1. Clone & Set Up Python Environment
```bash
# Clone the repository
git clone https://github.com/AdityaKadam2902/Gokul-Submission.git
cd Gokul-Submission

# Create and activate Python virtual environment
python -m venv .venv
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On macOS / Linux:
source .venv/bin/activate

# Install backend package with development dependencies
pip install -e ".[dev]"
```

### 2. Set Up Frontend Dependencies
```bash
# Install frontend packages (package.json at repository root)
npm install
```

### 3. Seed Realistic Demonstration Data (Instant 2-Second Setup)
To inspect the application with complete, connected healthcare data matching realistic operational scenarios (sources, snapshots, candidate facts, match candidates, phone discrepancies, workforce vacancies, practice closures, and audit history):
```bash
python scripts/seed_demo_data.py
```
*Output: `Demonstration database successfully seeded with all realistic data!`*

### 4. Run the Development Servers
Open two terminal windows:

* **Terminal 1 — FastAPI Backend:**
  ```bash
  python -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port 8000 --reload
  ```
  *Backend API running at `http://127.0.0.1:8000` (Interactive Swagger Docs at `/docs`)*

* **Terminal 2 — React Frontend:**
  ```bash
  npm run dev
  ```
  *Frontend UI running at `http://localhost:5173`*

---

## 6. Application Workspaces & UI Tour

Navigate to **`http://localhost:5173`** in your browser:

### Workspace 1: Sources & Observation History (`#/sources`)
- Displays all registered healthcare web sources (Official clinic sites, directory aggregators, careers portals, operational notice pages).
- Displays crawling policy status, robots.txt compliance, and immutable snapshot counts.
- Re-ingesting an unchanged source automatically demonstrates **short-circuit hash matching**, preventing duplicate snapshots.

### Workspace 2: Snapshot Detail & Grounded Evidence (`#/snapshot/:id`)
- Displays the raw parsed text on the left and extracted candidate facts on the right.
- Clicking any extracted fact highlights the exact text quotation in the document and displays the verified byte coordinates (`start_offset`, `end_offset`).

### Workspace 3: Unified Human Review Queue (`#/reviews`)
- **Triage Dashboard:** Shows pending items, active investigations, deferred decisions, and completed audit actions.
- **Category Filtering:** Filter across *Organisation Matches*, *Factual Discrepancies*, *Workforce Opportunities*, and *Practice Lifecycle Events*.
- **Interactive Decision Modal:**
  - Open a **Factual Discrepancy** to compare conflicting phone numbers between an official site (`03 9419 8888`) and an external directory (`03 9419 7777`) side-by-side with verbatim quotes.
  - Open a **Practice Lifecycle Event** to inspect an explicit branch closure notice (`Box Hill branch`) and verify that sibling branches remain active.
  - Enter reviewer identity, select decision action (`Approve` / `Reject` / `Defer`), enter operational rationale, check the confirmation box, and click **Record Decision**.
- **Audit History Tab:** Real-time append-only ledger displaying recorded human decisions, timestamps, reviewer identity, and complete decision rationale.

### Workspace 4: Candidate Facts (`#/facts`) & Prompt Audits (`#/prompt-runs`)
- Global inventory of all candidate facts, verification statuses, and extraction provenance.
- Complete audit logs for local inference: token counts, generation latency (ms), parsing success, and validation error reports.

### Workspace 5: System Health & Storage Status (`#/health`)
- Live diagnostic reporting for SQLite database connection, storage path, WAL status, and AI model configuration.

---

## 7. API Reference & Contract Architecture

All endpoints follow strict RESTful conventions, return structured JSON with correlation IDs, and generate type-safe TypeScript interfaces via OpenAPI 3.1.

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/api/health` | `GET` | Health status: database connectivity, WAL mode, and Ollama configuration |
| `/api/sources` | `GET` / `POST` | List registered sources or register a new policy-approved source |
| `/api/sources/{id}/ingest` | `POST` | Execute ingestion pipeline: fetch, hash, parse headings, and record observation |
| `/api/snapshots/{id}` | `GET` | Retrieve immutable snapshot text, heading spans, and character offsets |
| `/api/facts` | `GET` | Retrieve candidate facts with associated evidence IDs and verification state |
| `/api/organisations` | `GET` / `POST` | Retrieve canonical organisation registry and branch hierarchies |
| `/api/discrepancies` | `GET` | List detected cross-source factual conflicts with canonical fact pairs |
| `/api/opportunities` | `GET` | Query workforce vacancy opportunities by status (`advertised`, `possibly_stale`) |
| `/api/lifecycle` | `GET` | Retrieve practice lifecycle events (closures, relocations, acquisitions) |
| `/api/reviews/queue` | `GET` | Retrieve unified multi-domain human review queue with priority sorting |
| `/api/reviews/metrics` | `GET` | Summary statistics of pending, under review, deferred, and completed items |
| `/api/reviews/{type}/{id}/detail` | `GET` | Retrieve complete contextual evidence, conflicting facts, and allowed actions |
| `/api/reviews/{type}/{id}/action` | `POST` | Submit an auditable review decision (appends to immutable audit ledger) |
| `/api/reviews/actions` | `GET` | Retrieve chronological, append-only history of recorded human review actions |

---

## 8. Automated Verification & Test Suite Matrix

The platform is enforced by an automated test suite comprising **299 tests** across 11 test modules. All tests execute deterministically against in-memory SQLite engines and synthetic fixtures without external network dependencies.

```bash
# Execute the full automated test suite
pytest backend/tests -v
```

### Test Suite Inventory & Coverage Map

| Test Suite | Tests | Milestone Scope & Verification Coverage |
| :--- | :---: | :--- |
| `test_m0_docs_and_fixtures.py` | 18 | M0 architectural doc contracts, schema validation, synthetic fixture integrity |
| `test_m1_backend.py` | 27 | M1 source registration, ingestion pipelines, SHA-256 hashing, heading parsers |
| `test_m2_ai_and_evidence.py` | 18 | M2 candidate extraction, character-offset verification, prompt audit logging |
| `test_m3_contracts.py` | 55 | M3 schema contracts, foreign keys, DDL constraints, Pydantic type safety |
| `test_m3_corpus.py` | 12 | Synthetic multi-source evaluation corpus validation across groups Alpha–Epsilon |
| `test_m3_1_backend.py` | 22 | M3.1 organisation registry, monotonic observation sequences, 3-tier entity resolver |
| `test_m3_2_backend.py` | 25 | M3.2 cross-source discrepancy detector, canonical fact pair constraints |
| `test_m3_3_backend.py` | 32 | M3.3 temporal change delta engine, snapshot difference tracking, idempotency |
| `test_m3_4_backend.py` | 32 | M3.4 4D information freshness engine, staleness monitoring, observation cadence |
| `test_m3_5_backend.py` | 30 | M3.5 healthcare vacancy lifecycle engine, absence tracking, possibly_stale policy |
| `test_m3_6_backend.py` | 28 | M3.6 human review queue, allowed action transitions, SQLite trigger immutability |
| `test_m3_7_backend.py` | 28 | M3.7 practice lifecycle detector, permanent closure, branch relocations, acquisitions |
| **Total Passing Tests** | **299** | **100% Pass Rate (0 Failures, 0 Regressions)** |

```bash
# Frontend type check & production build verification
npm run typecheck    # 0 TypeScript errors (tsc --noEmit)
npm run build        # Vite production asset compilation
```

---

## 9. Data Governance, Safety & Zero-PII Compliance

PracticeSignal is designed around strict ethical engineering boundaries:
1. **Zero Patient Health Information (PII/PHI):** The system strictly processes public practice operational metadata (addresses, phone numbers, opening hours, corporate entities, workforce advertisements). It contains **zero patient health records, zero medical histories, and zero candidate CV files**.
2. **Zero-Cost Constraint:** Built with ₹0 budget. Does not require paid commercial APIs (OpenAI, Anthropic, Gemini), paid cloud databases, or third-party scraping services.
3. **Synthetic Demonstration Corpus:** All demonstration records use synthetic, invented data (`backend/tests/fixtures/`) and reserved `.example.com` domains to guarantee compliance and prevent data contamination.
4. **Prompt Injection Defense:** Local extraction prompts are hardened against indirect prompt injection using structural XML delimiters, schema white-listing, and deterministic offset verification.

---

## 10. Repository Directory Structure

```text
Gokul-Submission/
├── .env.example                       # Documented environment variables template
├── .gitignore                         # Complete exclusion list (venv, node_modules, sqlite, caches)
├── ARCHITECTURE.md                    # Core architectural specification & system boundaries
├── README.md                          # Master technical overview & engineering documentation
├── package.json                       # Frontend dependencies, scripts, and build tasks
├── pyproject.toml                     # Backend dependencies, metadata, and pytest configuration
├── tsconfig.json                      # Strict TypeScript compiler configuration
├── vite.config.ts                     # Vite bundler configuration & proxy settings
│
├── backend/
│   ├── app/
│   │   ├── ai/                        # Local Ollama client & prompt store
│   │   ├── api/                       # FastAPI factory, middleware, serializers, & routers
│   │   │   └── routers/               # Routers: sources, snapshots, facts, reviews, etc.
│   │   ├── core/                      # Configuration, structured logging, & typed exceptions
│   │   ├── db/                        # Base engine, ORM models, migrations, & SQLite triggers
│   │   ├── extraction/                # Candidate extraction orchestrator
│   │   ├── ingest/                    # Snapshot store, evidence verifier, & scope normalizer
│   │   ├── intelligence/              # The 7 deterministic intelligence engines:
│   │   │   ├── entity_resolver.py     # 3-tier conservative identity matching
│   │   │   ├── discrepancy_detector.py# Cross-source factual conflict detector
│   │   │   ├── change_detector.py     # Temporal observation delta engine
│   │   │   ├── freshness_engine.py    # 4D information freshness evaluator
│   │   │   ├── opportunity_detector.py# Workforce vacancy lifecycle tracker
│   │   │   ├── practice_lifecycle_detector.py # Practice closure/relocation engine
│   │   │   ├── review_service.py      # Unified human review queue & audit service
│   │   │   └── schemas.py             # Domain Pydantic schemas & lifecycle enums
│   │   ├── models/                    # Shared Pydantic data transfer models
│   │   ├── parsers/                   # Structured DOM text & heading extraction
│   │   └── sources/                   # Source registry, fixture loaders, & policy gates
│   │
│   ├── data/                          # Local SQLite runtime database (practicesignal.db)
│   └── tests/                         # 299 automated unit, integration, and contract tests
│       └── fixtures/                  # Synthetic evaluation corpus (F1–F15)
│
├── docs/
│   ├── DECISION_LOG.md                # Architectural decisions D1–D22 with formal rationale
│   ├── IMPLEMENTATION_PLAN.md         # Phased milestone delivery plan (M0–M3.7)
│   ├── M3_ARCHITECTURE_PROPOSAL.md    # Multi-source intelligence architecture specification
│   ├── M3_DATA_MODEL.md               # Complete database schema data dictionary
│   ├── M3_EVALUATION_PLAN.md          # Multi-source evaluation protocol
│   ├── M3_IMPLEMENTATION_PLAN.md      # Detailed engineering breakdown for M3 milestones
│   ├── M3_RISK_REGISTER.md            # Technical risk matrix & mitigation controls
│   ├── RESULTS.md                     # Measured empirical benchmark & latency results
│   ├── SOURCE_REGISTER.md             # Source approval policy & synthetic archetype registry
│   ├── TECHNICAL_CHALLENGES.md        # Engineering deep dive into the 5 hardest challenges solved
│   ├── evaluation-methodology.md      # Ground truth evaluation methodology
│   ├── privacy-and-safety.md          # Data governance, fetch security, & prompt defense
│   └── prompt-engineering-log.md      # Versioned prompt template audit history
│
├── frontend/
│   ├── index.html                     # SPA HTML5 entry point
│   ├── openapi.json                   # Exported OpenAPI 3.1 specification
│   └── src/
│       ├── api/                       # Auto-generated TypeScript client & API types
│       ├── components/                # Reusable UI components (Panels, Badges, Modals)
│       ├── pages/                     # Workspaces: Sources, Snapshot, Facts, ReviewQueue, Health
│       ├── routing.ts                 # Zero-dependency hash router
│       └── styles.css                 # Clean, responsive design stylesheet
│
├── prompts/                           # Versioned system prompt templates
└── scripts/
    ├── benchmark_ai.py                # Local inference performance benchmarking utility
    ├── reset_db.sh                    # Database reset utility script
    └── seed_demo_data.py              # 2-second realistic demonstration database seeder
```

---

## Licence & Affiliation Statement

PracticeSignal is an independent engineering project developed by **Aditya Kadam**. It is **not** affiliated with, endorsed by, or sponsored by any commercial healthcare organisation, EHR vendor, or clinical provider. All organisations, clinics, addresses, phone numbers, and job vacancies referenced in demonstration fixtures are **deliberately synthetic** and intended solely for software engineering verification.
