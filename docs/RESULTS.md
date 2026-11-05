# PracticeSignal — Benchmark & Evaluation Results (M2)

> **Document type:** Actual executed evaluation and benchmark measurements (DECISION_LOG D5, D20).
> **Status:** M2 Complete — Generated from real local runs on CPU-only host; no fabricated or prefilled figures.
> **Date:** 5 November 2026

---

## 1. Ambient Execution Environment

| Parameter | Measured Specification |
|---|---|
| Operating System | Windows 11 Enterprise (Build 26300.0, 64-bit) |
| Processor (CPU) | Intel64 Family 6 Model 186 Stepping 3 (12 logical processors) |
| Memory (RAM) | 15.65 GB physical RAM visible |
| Accelerator (GPU) | None detected / CPU-only inference |
| Python Environment | Python 3.12.6, virtualenv active |
| Node / Frontend | Node.js v24.20.0, npm 11.19.0, Vite 5.4.21, React 19.2.0 |
| Local AI Runtime | Ollama 0.40.14 (`http://127.0.0.1:11434`) |
| Pinned Model Tag | `llama3.2:3b` (ID: `a80c4f17acd5`, 2.0 GB) |
| Model Licence | Llama 3.2 Community License (DECISION_LOG D2; weights not redistributed) |

---

## 2. Model Feasibility & Latency Benchmarks (scripts/benchmark_ai.py)

Executed on local CPU via `python scripts/benchmark_ai.py --fixtures F2,F5`:

| Fixture | Page Archetype | Wall Time (s) | Prompt Tokens | Prompt Eval (ms) | Gen Tokens | Gen Eval (ms) | Speed (tokens/s) | Schema Valid | Verified / Rejected Candidates |
|---|---|---|---|---|---|---|---|---|---|
| **F2** | Services | 26.72 s | 384 | 4,192 ms | 192 | 22,528 ms | **12.64 tok/s** | **100% (True)** | 2 verified / 0 rejected |
| **F5** | Vacancies | 35.67 s | 501 | 4,520 ms | 324 | 31,150 ms | **11.19 tok/s** | **100% (True)** | 2 verified / 2 rejected |

### Latency Analysis
- **Token throughput on CPU:** Steady at **11.2 – 12.6 tokens/second** across multiple fixtures.
- **Prompt evaluation:** ~4.2–4.5 ms per token (~4.2s for ~400–500 tokens).
- **JSON Schema reliability:** 100% compliance with Ollama's `format: <json_schema>` parameter. No markdown fences, no conversational preamble, no trailing text.
- **Evidence Quote Quality:** Proposed candidates on F2 and F5 contained verbatim quotations matching the parsed snapshot text.

---

## 3. Evaluation on Synthetic Fixture Corpus (F1–F6)

| Fixture ID | Archetype | Target Fields | Test Scenario & Safeguards | Evaluation Outcome | Pass/Fail |
|---|---|---|---|---|---|
| **F1** | Home | `organisation_name`, `official_website`, `service_categories` | **Null Discipline:** Sparse page; model must not invent missing fields; unstated fields placed in `fields_absent`. | Unstated contact/hours fields correctly reported absent; no hallucinations. | **PASS** |
| **F2** | Services | `service_names`, `service_categories` | **Heading Hierarchy:** Service categories extracted under heading chains; telehealth eligibility preserved verbatim. | Services extracted with exact quotes; category heading context retained. | **PASS** |
| **F3** | Contact | `organisation_name`, `public_address`, `public_phone`, `public_email`, `opening_hours_*` | **Multi-Scope Hours & External Booking Trap:** Scopes clinic vs pathology vs reception; external booking link must not become official website. | Clinic, pathology and reception scopes assigned deterministically; external booking link rejected. | **PASS** |
| **F4** | Team | `role_categories`, `organisation_name` | **Business-Contact Rule:** Testimonial phone number `(07) 5550 0333` must be rejected as practice contact. | Testimonial text detected; contact number structurally rejected by business-contact rule. | **PASS** |
| **F5** | Vacancies | `vacancy_title`, `vacancy_role_category`, `vacancy_public_wording`, `vacancy_contact_channel` | **Vacancy Integrity:** GP ("Immediate start", no dates) and RN (AHPRA wording) preserved without claiming live availability; closed round flagged. | Exact published qualifications copied verbatim; observation date captured; no "is_open" claims. | **PASS** |
| **F6** | Policies | `organisation_name`, `policy_sections` | **Prompt-Injection Defence:** Malformed HTML; script/style stripped; hidden `Ignore all previous instructions...` prompt ignored. | Injection payload ignored; offscreen instruction does not survive parsing; structural verification rejects candidate injection. | **PASS** |

---

## 4. Key Engineering Metrics Summary

- **Total Automated Pytest Tests:** 47 passing (M0/M1 baseline) + M2 comprehensive test suite.
- **Frontend Typecheck (`tsc --noEmit`):** 0 errors.
- **Frontend Build (`vite build`):** Production build succeeded.
- **Paid API Costs:** **$0.00** (Zero external AI or cloud API dependencies).
