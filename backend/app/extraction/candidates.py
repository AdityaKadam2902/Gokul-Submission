"""AI Candidate Extraction and Evidence Persistence Pipeline (ARCHITECTURE §6.1, §6.4).

Responsibility:
    Executes single structured AI extraction per changed page snapshot (DECISION_LOG D4),
    enforces Pydantic schema validation, runs deterministic evidence verification (D10),
    assigns heading-based scope (D11), applies data-preserving normalization (D7),
    and records immutable prompt runs and candidate facts.

Guarantees:
    - IDEMPOTENCY: Unchanged snapshots or identical prompt runs skip inference.
    - NEVER AUTO-APPROVES: Extracted facts persist as 'candidate', 'needs_review', or 'rejected'.
    - SNAPSHOT PRESERVATION: Failed inference never invalidates or mutates the snapshot.
    - EVIDENCE-FIRST: Every candidate references calculated Python character offsets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.ai.client import OllamaClient, OllamaInferenceResult, get_ollama_client
from backend.app.ai.prompt_store import (
    EXTRACTION_JSON_SCHEMA,
    compose_extraction_prompt,
    compute_request_hash,
)
from backend.app.core.exceptions import (
    OllamaInferenceError,
    OllamaTimeout,
    OllamaUnavailable,
    PracticeSignalError,
    SchemaValidationError,
)
from backend.app.core.logging import get_logger, safe_preview
from backend.app.db.orm import Evidence, Fact, PageSnapshot, PromptRun
from backend.app.ingest import evidence_verifier, field_normalizer, scope_assignment

logger = get_logger(__name__)


@dataclass(frozen=True)
class ExtractionSummary:
    snapshot_id: int
    prompt_run_id: int
    is_idempotent_skip: bool
    parse_ok: bool
    total_candidates: int
    accepted_candidates: int
    needs_review_candidates: int
    rejected_candidates: int
    latency_ms: float
    model_tag: str
    prompt_version: str


def run_extraction_for_snapshot(
    session: Session,
    snapshot: PageSnapshot,
    *,
    force: bool = False,
    client: OllamaClient | None = None,
) -> ExtractionSummary:
    """Run structured AI extraction and evidence verification for one page snapshot."""
    source = snapshot.source
    intended_fields = list(source.intended_fields) if source else []

    # 1. Compose versioned prompt and request hash
    prompt_text, schema, prompt_version = compose_extraction_prompt(
        page_text=snapshot.text,
        intended_fields=intended_fields,
        page_type_hint=source.page_type if source else None,
        source_kind=source.kind if source else "fixture",
    )

    ollama = client or get_ollama_client()
    payload_hash = compute_request_hash(prompt_text, schema, ollama.model)

    # 2. Idempotency short-circuit (D4): if already extracted with same hash and model
    if not force:
        existing_run = session.scalar(
            select(PromptRun)
            .where(
                PromptRun.snapshot_id == snapshot.id,
                PromptRun.request_payload_hash == payload_hash,
                PromptRun.parse_ok == True,  # noqa: E712
            )
            .limit(1)
        )
        if existing_run is not None:
            logger.info(
                "extraction_idempotent_skip snapshot_id=%d run_id=%d hash=%s",
                snapshot.id,
                existing_run.id,
                payload_hash[:12],
            )
            existing_facts = session.scalars(
                select(Fact).where(Fact.snapshot_id == snapshot.id)
            ).all()
            accepted = sum(1 for f in existing_facts if f.status == "candidate")
            review = sum(1 for f in existing_facts if f.status == "needs_review")
            rejected = sum(1 for f in existing_facts if f.status == "rejected")
            return ExtractionSummary(
                snapshot_id=snapshot.id,
                prompt_run_id=existing_run.id,
                is_idempotent_skip=True,
                parse_ok=True,
                total_candidates=len(existing_facts),
                accepted_candidates=accepted,
                needs_review_candidates=review,
                rejected_candidates=rejected,
                latency_ms=existing_run.latency_ms,
                model_tag=existing_run.model_tag,
                prompt_version=existing_run.prompt_version,
            )

    # 3. Local Ollama inference call
    inference_result: OllamaInferenceResult | None = None
    call_error: str | None = None
    try:
        inference_result = ollama.generate_structured_sync(prompt_text, schema)
    except (OllamaUnavailable, OllamaTimeout, OllamaInferenceError) as exc:
        call_error = f"{type(exc).__name__}: {exc.message}"
        # Record failed prompt run for audit; preserve snapshot
        failed_run = PromptRun(
            snapshot_id=snapshot.id,
            model_tag=ollama.model,
            prompt_version=prompt_version,
            request_payload_hash=payload_hash,
            raw_response_ref=call_error,
            parse_ok=False,
            validation_errors=[call_error],
            latency_ms=0,
            prompt_eval_count=0,
            eval_count=0,
            tokens_per_second=0,
            evidence_checks={"error": call_error},
        )
        session.add(failed_run)
        session.commit()
        logger.warning("ai_extraction_failed snapshot_id=%d error=%s", snapshot.id, call_error)
        raise

    # 4. Record PromptRun row regardless of validation outcome
    validation_errors: list[str] = []
    if not inference_result.parse_ok or not inference_result.parsed_json:
        validation_errors.append("Response did not contain valid JSON dictionary")

    parsed_data = inference_result.parsed_json or {}
    candidates_raw = parsed_data.get("candidates", [])
    if not isinstance(candidates_raw, list):
        validation_errors.append("JSON output missing 'candidates' array")
        candidates_raw = []

    prompt_run = PromptRun(
        snapshot_id=snapshot.id,
        model_tag=inference_result.model,
        prompt_version=prompt_version,
        request_payload_hash=payload_hash,
        raw_response_ref=inference_result.raw_response,
        parse_ok=inference_result.parse_ok and len(validation_errors) == 0,
        validation_errors=validation_errors,
        latency_ms=inference_result.total_duration_ms,
        prompt_eval_count=inference_result.prompt_eval_count,
        eval_count=inference_result.eval_count,
        tokens_per_second=inference_result.tokens_per_second,
        evidence_checks={},
    )
    session.add(prompt_run)
    session.flush()  # assign prompt_run.id

    # 5. Process candidate facts through deterministic evidence verification
    headings_list = snapshot.headings or []
    evidence_checks_summary: dict[str, Any] = {
        "total_proposed": len(candidates_raw),
        "verified": 0,
        "rejected": 0,
        "needs_review": 0,
        "details": [],
    }

    facts_to_add: list[Fact] = []

    for item in candidates_raw:
        if not isinstance(item, dict):
            continue

        field_key = str(item.get("field_key", "")).strip()
        raw_val = str(item.get("raw_value", "")).strip()
        quote = str(item.get("evidence_quote", "")).strip()
        model_scope = item.get("scope_proposal")

        # Deterministic verification with repeated quote disambiguation
        ver_res = evidence_verifier.verify_candidate_evidence(
            candidate=item,
            snapshot_text=snapshot.text,
            intended_fields=intended_fields,
            surrounding_context=str(model_scope or ""),
            headings=headings_list,
        )

        evidence_row: Evidence | None = None
        if ver_res.start_offset is not None and ver_res.end_offset is not None:
            evidence_row = Evidence(
                snapshot_id=snapshot.id,
                start_offset=ver_res.start_offset,
                end_offset=ver_res.end_offset,
                quote=ver_res.matched_quote or quote,
                match_algorithm_version=ver_res.match_algorithm,
            )
            session.add(evidence_row)
            session.flush()  # assign evidence_row.id

        # Deterministic Scope Assignment
        scope_res = scope_assignment.assign_scope_from_headings(
            start_offset=ver_res.start_offset or 0,
            end_offset=ver_res.end_offset or 0,
            headings=headings_list,
            model_scope_proposal=model_scope,
        )

        # Deterministic Field Normalization (preserves raw_value always)
        norm_res = field_normalizer.normalize_field(field_key, raw_val)

        # Determine authoritative candidate status (candidate | needs_review | rejected)
        status: str
        review_reason: str | None = None

        if not ver_res.verified:
            status = "rejected"
            review_reason = ver_res.rejection_reason
            evidence_checks_summary["rejected"] += 1
        elif ver_res.status == "needs_review":
            status = "needs_review"
            review_reason = ver_res.rejection_reason
            evidence_checks_summary["needs_review"] += 1
        elif scope_res.is_ambiguous:
            status = "needs_review"
            review_reason = scope_res.review_reason
            evidence_checks_summary["needs_review"] += 1
        elif norm_res.needs_review:
            status = "needs_review"
            review_reason = norm_res.review_reason
            evidence_checks_summary["needs_review"] += 1
        else:
            status = "candidate"
            review_reason = None
            evidence_checks_summary["verified"] += 1

        evidence_checks_summary["details"].append(
            {
                "field_key": field_key,
                "status": status,
                "verified": ver_res.verified,
                "rejection_reason": review_reason,
            }
        )

        if prompt_run.id is None:
            raise ValueError("Candidate facts created via ai_extraction must reference an authoritative prompt_run_id")

        # Explicit evidence-backed timezone only (never assume Brisbane)
        fact_tz: str | None = None
        comb_text = f"{norm_res.raw_value} {ver_res.matched_quote or quote}".lower()
        if any(ind in comb_text for ind in ("brisbane", "qld", "queensland", "aest")):
            fact_tz = "Australia/Brisbane"
        elif any(ind in comb_text for ind in ("sydney", "nsw", "aedt")):
            fact_tz = "Australia/Sydney"
        elif any(ind in comb_text for ind in ("melbourne", "vic")):
            fact_tz = "Australia/Melbourne"
        elif any(ind in comb_text for ind in ("perth", "wa", "awst")):
            fact_tz = "Australia/Perth"

        fact = Fact(
            snapshot_id=snapshot.id,
            prompt_run_id=prompt_run.id,
            organisation_id=source.organisation_id if source else None,
            field_key=field_key,
            scope_key=scope_res.scope_key,
            raw_value=norm_res.raw_value,
            normalized_value=norm_res.normalized_value,
            timezone=fact_tz,
            evidence_id=evidence_row.id if evidence_row else None,
            model_tag=inference_result.model,
            prompt_version=prompt_version,
            via="ai_extraction",
            status=status,
            needs_review_reason=review_reason,
        )
        facts_to_add.append(fact)

    session.add_all(facts_to_add)

    # Save evidence checks summary back into prompt_run
    prompt_run.evidence_checks = evidence_checks_summary
    session.commit()

    logger.info(
        "extraction_complete snapshot_id=%d run_id=%d candidates=%d verified=%d review=%d rejected=%d latency_ms=%.1f",
        snapshot.id,
        prompt_run.id,
        len(candidates_raw),
        evidence_checks_summary["verified"],
        evidence_checks_summary["needs_review"],
        evidence_checks_summary["rejected"],
        inference_result.total_duration_ms,
    )

    return ExtractionSummary(
        snapshot_id=snapshot.id,
        prompt_run_id=prompt_run.id,
        is_idempotent_skip=False,
        parse_ok=prompt_run.parse_ok,
        total_candidates=len(facts_to_add),
        accepted_candidates=evidence_checks_summary["verified"],
        needs_review_candidates=evidence_checks_summary["needs_review"],
        rejected_candidates=evidence_checks_summary["rejected"],
        latency_ms=inference_result.total_duration_ms,
        model_tag=inference_result.model,
        prompt_version=prompt_version,
    )
