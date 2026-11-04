---
template_id: evidence_verification_v2
version: 2
status: draft (M0 stub — verification is deterministic code first, model assist is advisory only)
stage: evidence-quote verification / contradiction guard
model_target: optional local model assist (llama3.2:3b); the binding check is deterministic and code-owned
---

# Purpose

Keep model involvement away from the decision of *whether a proposed evidence quote actually exists in the stored snapshot*. The code path (backend module `ingest.evidence_verifier`) performs exact quote matching, whitespace-normalised tolerance and offset extraction wholly deterministically; this template exists for the optional model-clarification pass when a candidate is sent to review.

# Expected inputs

- `page_text`: the snapshot normalized text (authority).
- `proposed_field`: field_key + `raw_value_as_published` + `evidence_quote` from the extraction call.

# Expected output (advisory JSON)

```json
{
  "quote_verified_by_code": true,
  "quote_span": {"start": 0, "end": 0},
  "disagreement": null,
  "review_note": null
}
```

Model returns a `review_note` only when it believes the code-recorded span is misaligned, never as a substitute for the code decision.

# Hard rules

1. The deterministic verifier's verdict is binding. A model note cannot overrule missing/failed verification.
2. No model call runs when the code verifier succeeds — this stage is skipped in the happy path to keep CPU cost low.
3. No optional pass may introduce any value absent from `page_text` (respective `candidates` remain candidate).

# Deterministic checklist this template documents (code-owned)

- Whitespace-normalised substring match (collapsing repeated whitespace, normalising dashes/quotes).
- Offset capture against the snapshot text (start/end character positions stored with the fact).
- Failure outcome: fact is not persisted as supported; it either gets `needs_review` (if `raw_value_as_published` seems plausible) or is rejected as unsupported.
