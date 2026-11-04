# PracticeSignal — Prompt Engineering Log

> **Document type:** Real iteration history of prompt templates (assessment deliverable #3, DECISION_LOG D17).
> **Status:** Structure ready at M0; **no completed iteration entries yet** — entries are added only when a template is actually revised during M2+ evaluation. Nothing in this file is a fabricated experiment.

## Template inventory at M0

| Template | File | Version | Status | Change reason |
|---|---|---|---|---|
| Classification + extraction (combined) | `prompts/classification_extraction_v1.md` | 1 | draft stub | Initial; final wording settled in M2 evaluation |
| Evidence verification | `prompts/evidence_verification_v2.md` | 2 | draft stub | Documented as deterministic-first (model advisory only) |
| Scope assignment | `prompts/scope_assignment_v3.md` | 3 | draft stub | Version reflects the D11 decision (deterministic-first scoping) already at stub stage |
| Vacancy extraction | `prompts/vacancy_extraction_v1.md` | 1 | draft stub | Initial |
| Injection guard | `prompts/injection_guard_v1.md` | 1 | draft stub | Initial; structural defence remains code-owned (D10) |

## Iteration log (real entries only)

*(empty — to be filled from M2 evaluation runs; each entry must state: date, template + new version, benchmark case, measured change, and the decision taken. Entries may not be backfilled with imagined experiments.)*

## Process rules

1. Template versions never decrease and never skip silently; each bump has a log entry here.
2. Every change requires the D5 benchmark delta (latency p50/p95, tokens/sec, JSON-validity) — not intuition.
3. The six-stage sequential flow was replaced by ONE combined call per changed page in DECISION_LOG D4; older per-stage designs exist only as this repo's history.
4. Small-model honesty: all reported metrics include raw counts; a 100% figure on tiny n is presented with its n, never as a production guarantee.
