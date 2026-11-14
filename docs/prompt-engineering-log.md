# PracticeSignal — Prompt Engineering Log

> **Document type:** Iteration history of prompt templates (DECISION_LOG D17).  
> **Master Documentation:** For full prompt engineering methodology, iterative refinement comparisons, and development prompts, see [`PROMPT_DOCUMENTATION.md`](../PROMPT_DOCUMENTATION.md).

## Template inventory at M0

| Template | File | Version | Status | Change reason |
|---|---|---|---|---|
| Classification + extraction (combined) | `prompts/classification_extraction_v1.md` | 1 | draft stub | Initial; final wording settled in M2 evaluation |
| Evidence verification | `prompts/evidence_verification_v2.md` | 2 | draft stub | Documented as deterministic-first (model advisory only) |
| Scope assignment | `prompts/scope_assignment_v3.md` | 3 | draft stub | Version reflects the D11 decision (deterministic-first scoping) already at stub stage |
| Vacancy extraction | `prompts/vacancy_extraction_v1.md` | 1 | draft stub | Initial |
| Injection guard | `prompts/injection_guard_v1.md` | 1 | draft stub | Initial; structural defence remains code-owned (D10) |

## Iteration log (real entries only)

### Iteration 1 — 5 November 2026: Combined Structured Extraction V1 (`classification_extraction_v1`)
- **Template & Version:** `classification_extraction_v1.md` (Version 1) + `injection_guard_v1.md` (Version 1)
- **Target Runtime:** Local Ollama 0.40.14 with `llama3.2:3b` on CPU-only host
- **Benchmark Case:** Fixtures F2 (Services) and F5 (Vacancies)
- **Observed Behavior & Measurements:**
  - Single composed structured-output call via Ollama's `format: <json_schema>` parameter.
  - F2 wall latency: **26.72 s** (384 prompt tokens evaluated in 4,192 ms; 192 output tokens generated in 22,528 ms at 12.64 tok/s).
  - F5 wall latency: **35.67 s** (501 prompt tokens evaluated in 4,520 ms; 324 output tokens generated in 31,150 ms at 11.19 tok/s).
  - Schema validity: **100%** (2/2 responses strictly valid JSON without conversational framing).
  - Extraction accuracy: Extracted core vacancy titles, role categories, and application channels on F5; extracted service names and categories on F2.
  - Quotation verification: All valid candidates had exact verbatim quotations in the snapshot text; rejected 2 invalid candidates on F5 cleanly.
- **Decision Taken:** Approved single-call extraction prompt architecture. Confirmed that single-call per changed snapshot prevents CPU timeouts while guaranteeing deterministic evidence verification downstream. All template stubs preserved.

## Process rules

1. Template versions never decrease and never skip silently; each bump has a log entry here.
2. Every change requires the D5 benchmark delta (latency p50/p95, tokens/sec, JSON-validity) — not intuition.
3. The six-stage sequential flow was replaced by ONE combined call per changed page in DECISION_LOG D4; older per-stage designs exist only as this repo's history.
4. Small-model honesty: all reported metrics include raw counts; a 100% figure on tiny n is presented with its n, never as a production guarantee.
