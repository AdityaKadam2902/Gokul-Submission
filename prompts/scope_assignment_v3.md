---
template_id: scope_assignment_v3
version: 3
status: draft (M0 stub — deterministic heading-chain scoping is authoritative; this model pass is fallback-only)
stage: context-aware scope proposal for ambiguous facts
model_target: llama3.2:3b via local Ollama HTTP adapter (fallback path only)
---

# Purpose

Propose a scope key for a candidate fact only when the deterministic heading-chain pass could not resolve scope unambiguously (DECISION_LOG D11). The binding precedence is:

1. **Deterministic (authoritative):** enclosing heading chain / nearest table caption or row header from `parsing.text_extract` supplies `scope_key`.
2. **Deterministic ambiguity:** multiple candidate scopes in one heading chain ⇒ `needs_review` (no model call).
3. **Model proposal (fallback only):** heading context absent entirely; result is always recorded as `model_proposed` and flagged for reviewer confirmation.

# Expected inputs

- `fact`: field_key, raw_value_as_published, deterministic scope attempts with their outcomes.
- `page_text_excerpt`: bounded text around the fact occurrence, including any caption/table-label context.
- `allowed_scopes`: the closed list registered for this source (e.g. `clinic`, `pathology`, `reception`).

# Expected output (fallback JSON)

```json
{
  "scope_proposal": "<one of allowed_scopes, as stated on the page>",
  "evidence_quote": "<page text that states the scope>",
  "assignment_basis": "heading | caption | row_header | none",
  "uncertainty_note": null
}
```

# Hard rules

1. Never invent a scope; if the page does not state one, return `scope_proposal: null` and let the fact go to review.
2. Never merge scopes: "clinic" and "pathology collection" are never one field (ARCHITECTURE §4/§9.3 principle retained in `docs/DECISION_LOG.md` D11).
3. A scope proposal is never permitted to *resolve* a conflict — scope resolution happens only in the deterministic stage or via explicit human review.

# Evaluation hooks

Metric-relevant: scope-assignment correctness versus hand-labelled scopes on F3 (multi-scope hours table) and F6 (hidden scope traps); never mixed-scope comparisons passing silently.
