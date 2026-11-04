# PracticeSignal — Evaluation Methodology

> **Document type:** Evaluation corpus rules, metric definitions, reporting standards.
> **Status:** M0 — methodology frozen in structure; **no results exist yet.** `docs/RESULTS.md` is created only in M2/M5 when real benchmark runs produce numbers (never prefilled).

## Corpus policy (DECISION_LOG D20)

Evaluation runs **entirely on the synthetic fixture corpus** (`backend/tests/fixtures/`), so results are reproducible without live fetching:

- F1 home — sparse page; tests "do not invent" behaviour (null discipline).
- F2 services — heading-chain category extraction; published eligibility wording preserved verbatim.
- F3 contact — **multi-scope hours table** (clinic / pathology / reception); scope-confusion checks; external booking link not promoted to official website.
- F4 team — role-category-only (no named individuals); testimonial block with a phone number that must be **rejected** as practice contact data (business-contact rule).
- F5 vacancies — GP ("Immediate start" wording, no date), RN (published AHPRA wording kept as-is), reception round explicitly closed.
- F6 policies — malformed HTML, script/style text stripping, hidden prompt-injection node, outdated "last updated" line (never interpreted as staleness of vacancies).

Additional negative cases live as edge-case metadata on each fixture and are asserted in M2+ tests.

## Metric definitions

| Metric | Definition |
|---|---|
| Extraction precision (per field) | accepted-candidate correctness ÷ extracted candidates, hand-labelled on F1–F6 |
| Extraction recall (per field) | expected facts captured ÷ hand-labelled expected facts |
| Evidence-quote validity | accepted facts whose evidence quote verifies against the stored snapshot (offsets valid) — target is structurally 1.0; deviations are bugs |
| Unsupported-field rejection rate | candidates rejected/flagged despite plausible wording — raw counts + examples |
| Role-category accuracy | GP/RN/EN classification vs hand-labelled F5 ground truth |
| False-conflict rate | conflicts raised that a human disagrees with; ambiguous-scope cases must route to review instead |
| Duplicate-suggestion quality | suggestions human reviewers endorse; no auto-merges ever (D12) |
| JSON-validity rate | model responses that validate against the strict schema on first parse |
| Latency / tokens/sec / resource | measured by `scripts/benchmark_ai.py` on the local CPU (D5) |

## Reporting rules (honesty rules, ARCHITECTURE §5/DECISION_LOG D20)

1. Raw counts are always shown beside percentages.
2. No score on a tiny n is presented as a production-grade guarantee; the n is always stated.
3. Failures are part of the deliverable: every metric report includes documented failure cases and example extractions.
4. Nothing in this file or in `docs/RESULTS.md` may be created from projection — only from executed runs.
