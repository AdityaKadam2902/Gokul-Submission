---
template_id: vacancy_extraction_v1
version: 1
status: draft (M0 stub — candidate fields only; live-availability is never inferred)
stage: public vacancy candidate extraction (doctors / nurses)
model_target: llama3.2:3b via local Ollama HTTP adapter
---

# Purpose

Extract *published description* of public doctor/nurse vacancies from the vacancies archetype page (and any other registered source page that states one in scope) as candidate records. This is wording about a listing — never a claim that the role is currently open.

# Expected inputs

- `page_text`: bounded cleaned vacancy page text (scripts/styles/hidden stripped).
- `intended_fields` restriction from the source registry (vacancy fields only).
- `page_metadata`: publication date line if present (often absent; absence is not a signal).

# Expected output (strict JSON)

```json
{
  "vacancies": [
    {
      "title_as_published": "General Practitioner — Immediate start",
      "role_category": "GP | RN | EN | other_indicated | unknown",
      "employment_type_stated": "casual | part_time | full_time | locum | not_stated",
      "public_contact_channel_as_published": "<the application route as the page presents it>",
      "qualifications_wording_as_published": "<AHPRA/registration sentence exactly as written>",
      "publication_date_stated": null,
      "evidence_quote": "<contiguous page text naming the vacancy>",
      "status_wording_captured": ["immediate_start_wording_present", "no_date_stated"]
    }
  ],
  "closed_or_non_vacancy_items": [
    {
      "title_as_published": "Reception — applications closed",
      "why_not_candidate": "<page wording explicitly closes applications>",
      "evidence_quote": "<page text>"
    }
  ]
}
```

# Hard rules

1. Model output states only what the page says. Nothing in the pipeline or UI may claim the vacancy is currently open — the UI displays wording plus observation date and the lifecycle state that only a reviewer or the change detector can move (ARCHITECTURE §9; `docs/IMPLEMENTATION_PLAN.md` AC-14).
2. The model MUST NOT fill `publication_date_stated` from anywhere on the page beyond an explicit, in-scope posting date; absence stays null.
3. Role-category classification must not paraphrase qualifications wording (AHPRA/registration sentences copied verbatim).
4. The vacancy contact channel is recorded as published for applications purposes; no contact is ever used for outreach (there is no outbound path anywhere in the codebase — ARCHITECTURE §4.2; ACMA-consistent posture in `DECISION_LOG` D16).

# Evaluation hooks

Metric-relevant: role-category accuracy against hand-labelled F5 expectations (GP/RN/other), unsupported-field (no-date-status) honesty, and the deliberate absence of any live-availability field in the output schema — the pipeline records wording and an observation date, never a claim that a role is open now.
