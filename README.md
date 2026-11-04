# PracticeSignal

**Healthcare Practice & Workforce Intelligence** — a local-first, evidence-first prototype that turns a small, policy-approved and policy-gated set of public healthcare organisation pages (or, until policies are verified, clearly-labelled synthetic fixtures) into structured, source-backed records with per-fact evidence, change/conflict intelligence, human review and provenance-complete exports.

> **Name status:** PracticeSignal is a **provisional** working name. No trademark, domain or collision search has been performed yet (tracked in `docs/IMPLEMENTATION_PLAN.md §9`).
> **Status:** Milestone 0 (project foundation + synthetic fixture corpus). Milestones M1–M6 are specified in `docs/IMPLEMENTATION_PLAN.md` and not yet implemented.
> **Zero-cost rule:** no paid APIs, paid models, subscriptions, paid hosting, credit cards or trials. Runtime AI is a locally installed open model; nothing is metered or key-gated.

## What is documented where

| Document | Contents |
|---|---|
| `ARCHITECTURE.md` | Implementation-ready specification: components, data model, API contracts, engineering standards |
| `docs/DECISION_LOG.md` | Architectural decisions D1–D22 with rationale and correction traceability |
| `docs/SOURCE_REGISTER.md` | Source approval policy, registered sources (all currently fixture-only), fixture mapping, future-source procedure |
| `docs/IMPLEMENTATION_PLAN.md` | Milestones M0–M6 with per-milestone acceptance criteria, AC matrix, test execution map, dependency ledger, unresolved issues |
| `docs/privacy-and-safety.md` | Data minimisation, fetch-security posture and its honest limitations |
| `docs/prompt-engineering-log.md` | Versioned prompt template history (real iterations recorded as they happen) |
| `docs/evaluation-methodology.md` | Evaluation corpus rules and metrics definitions |
| `docs/demo-script.md` | Demo-video plan for the assessment submission |

## Evidence-first principles (short version)

1. Every accepted fact must carry an **exact quote** verifiable against a stored page snapshot — offsets included, no exceptions.
2. The local AI model only *proposes* candidate fields; it never approves facts, never resolves conflicts, never decides live availability, and cannot reach the network or the shell.
3. Duplicate organisations are *suggested*, never silently merged. Ambiguous facts are surfaced for human review.
4. No patient data, no sensitive attributes, no personal contact harvesting, no automated outreach of any kind.
5. Nothing here claims affiliation with any company or product (see `docs/ARCHITECTURE.md §13` for the "must not claim" list).

## Toolchain (verified 4 Nov 2026)

- Python 3.12.6 (specfloor for 3.11+)
- Node.js v24.20.0, npm 11.19.0
- Ollama 0.40.14 with `llama3.2:3b` already installed locally (Llama 3.2 Community License; **weights are not redistributed by this repository** — pull via Ollama yourself)
- No GPU required; CPU-only inference is supported by design (per DECISION_LOG D4/D5).

## Setup (current milestone: M0)

M0 is documentation + fixtures + conformance tests only. **Do not install** anything yet if following the assessment's milestone discipline; dependency installation is authorised from M1 per `docs/IMPLEMENTATION_PLAN.md`.

```
# after git clone, when M1 is authorised:
python -m venv .venv
pip install -e ".[dev]"          # backend + dev tooling from pyproject.toml
cd frontend && npm install        # not yet required at M0
```

Run the M0 conformance tests (requires pytest availability):

```
pytest backend/tests/test_m0_docs_and_fixtures.py
```

Environment variables: see `.env.example`. No secrets are needed at any point.

## Honest limitations (current)

- All ingestion from source **S1** runs on synthetic fixtures; no live scraping has been authorised (see `docs/SOURCE_REGISTER.md`), so nothing here demonstrates a real crawl yet.
- Latency figures do **not exist yet**; the first benchmark runs in M2 (`scripts/benchmark_ai.py`) and will be recorded verbatim in `docs/RESULTS.md` — no performance numbers claimed in advance.
- The name "PracticeSignal" is provisional; a collision search is pending.
- The Graffiti Teams own no real-world contact details, and no invention is asserted as real. All organisations, addresses, phone numbers and people referenced in `backend/tests/fixtures/` are **deliberately fictional**.

## Licence & affiliation statement

This prototype is an independent assessment artefact and is **not** affiliated with, endorsed by, part of, or reviewed by Gokul Infocare Pty Ltd, the MIRA project, Healthdirect/healthdirect NHSD, Setmore, or Robina 7 Day Doctors. Structures modelled in fixtures are generic practice-website archetypes; no real page content is redistributed.</think>README complete. Next: the six synthetic fixtures + manifest. First fixture batch:<tool_call>write_todos<arg_key>todos</arg_key><arg_value>[{"task": "Pre-flight: inspect directory, git status, toolchain versions", "completed": true}, {"task": "Re-read the four spec docs and resolve documented contradictions", "completed": true}, {"task": "Create repo foundation (.gitignore, .env.example, pyproject.toml, package.json, README.md)", "completed": true}, {"task": "Author six synthetic fixtures F1–F6 + fixture manifest", "completed": false}, {"task": "Create five versioned prompt templates in prompts/", "completed": false}, {"task": "Add placeholder docs (prompt log, evaluation method, privacy, demo script)", "completed": false}, {"task": "Write backend/tests/test_m0_docs_and_fixtures.py", "completed": false}, {"task": "Run pytest (if installed), fix failures, report results verbatim", "completed": false}, {"task": "Final M0 report (files, structure, git status, honest test results)", "completed": false}]