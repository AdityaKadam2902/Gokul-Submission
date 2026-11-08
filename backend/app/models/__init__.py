"""Pydantic domain models — the single source of API truth (D19).

Pydantic v2 request/response schemas for M1. Frontend types are generated
from FastAPI's OpenAPI schema (which is built from these models), never
hand-written.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

PAGE_TYPES = ("home", "services", "contact", "team", "vacancies", "about", "other")
POLICY_STATUSES = ("manual_fixture_pending_policy_check", "blocked")
# ``approved_live`` is deliberately NOT settable through the API: it requires
# the documented manual robots/terms procedure in docs/SOURCE_REGISTER.md §3.
FUTURE_POLICY_STATUS = "approved_live"


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern="^(fixture|url)$")
    page_type: str = Field(pattern="^(home|services|contact|team|vacancies|about|other)$")
    fixture_id: str | None = None
    url: HttpUrl | None = None
    label: str | None = Field(default=None, max_length=255)
    # Approval metadata is otherwise "manual fixture pending policy check".
    policy_status: str = Field(default="manual_fixture_pending_policy_check")
    intended_fields: list[str] = Field(default_factory=list, max_length=32)
    approval_note: str | None = Field(default=None, max_length=2000)
    reviewed_by: str | None = Field(default=None, max_length=255)

    @field_validator("intended_fields", mode="before")
    @classmethod
    def _strip_fields(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return value


class SourcePatch(BaseModel):
    """Operator-editable fields only; policy/robots state is not API-editable."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=255)
    page_type: str | None = Field(default=None, pattern="^(home|services|contact|team|vacancies|about|other)$")
    intended_fields: list[str] | None = Field(default=None, max_length=32)
    approval_note: str | None = Field(default=None, max_length=2000)


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    label: str | None = None
    url: str | None = None
    domain: str | None = None
    fixture_id: str | None = None
    page_type: str
    policy_status: str
    robots_status: str
    intended_fields: list[str]
    approval_note: str | None = None
    reviewed_by: str | None = None
    created_at: str
    updated_at: str
    last_snapshot_id: int | None = None
    last_snapshot_at: str | None = None
    last_snapshot_hash: str | None = None


class SnapshotHead(BaseModel):
    """One snapshot row as listed inside a source detail response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    fetched_at: str
    title: str | None
    content_hash: str
    parser_version: str
    fixture: bool


class SourceDetailRead(SourceRead):
    """Source detail: adds the snapshot history for the source screen."""

    snapshots: list[SnapshotHead] = Field(default_factory=list)


class HeadingSpan(BaseModel):
    """One heading in the parsed text plus its exact offsets in ``text``."""

    model_config = ConfigDict(from_attributes=True)

    level: int
    text: str
    start_offset: int
    end_offset: int


class SnapshotRead(BaseModel):
    """Full snapshot detail — parsed text with heading structure + offsets."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    fetched_at: str
    title: str | None
    text: str
    content_hash: str
    parser_version: str
    fixture: bool
    headings: list[HeadingSpan]
    source_label: str | None = None
    source_fixture: bool = False


class IngestionResult(BaseModel):
    """Result of POST /api/ingestions/{source_id}."""

    snapshot_id: int | None = None
    unchanged: bool
    previous_snapshot_id: int | None = None
    content_hash: str | None = None
    message: str


class HealthStatus(BaseModel):
    status: str
    app_ok: bool
    database_ok: bool
    database_path: str
    ollama: str
    ollama_ok: bool = False
    robots_tool: str
    version: str


# ---------------------------------------------------------------------------
# M2 Models: Facts, Evidence and Prompt Runs (ARCHITECTURE §6.2, §6.3)
# ---------------------------------------------------------------------------

class EvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    snapshot_id: int
    start_offset: int
    end_offset: int
    quote: str
    captured_at: str
    match_algorithm_version: str


class FactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    snapshot_id: int
    prompt_run_id: int | None = None
    organisation_id: int | None = None
    field_key: str
    scope_key: str | None = None
    raw_value: str
    normalized_value: str | None = None
    timezone: str | None = None
    evidence_id: int | None = None
    evidence_quote: str | None = None
    evidence_start_offset: int | None = None
    evidence_end_offset: int | None = None
    model_tag: str | None = None
    prompt_version: str | None = None
    via: str
    status: str  # candidate | needs_review | rejected | reviewed | superseded
    needs_review_reason: str | None = None
    created_at: str


class PromptRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    snapshot_id: int
    model_tag: str
    prompt_version: str
    request_payload_hash: str
    raw_response_ref: str
    parse_ok: bool
    validation_errors: list[str]
    latency_ms: float
    prompt_eval_count: int
    eval_count: int
    tokens_per_second: float
    evidence_checks: dict[str, Any]
    created_at: str


class ExtractionResultRead(BaseModel):
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
    message: str


# ---------------------------------------------------------------------------
# M3 Models: Organisations, Links, Observations & Match Candidates (ARCHITECTURE §6.1, §6.2)
# ---------------------------------------------------------------------------

class OrganisationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    primary_domain: str | None = Field(default=None, max_length=255)
    corporate_parent_id: int | None = None
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=512)
    suburb: str | None = Field(default=None, max_length=128)
    postcode: str = Field(min_length=3, max_length=16)
    state: str = Field(min_length=2, max_length=16)
    status: str = Field(default="active", pattern="^(active|inactive|pending_review|merged)$")


class OrganisationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    primary_domain: str | None = None
    corporate_parent_id: int | None = None
    phone: str | None = None
    address: str | None = None
    suburb: str | None = None
    postcode: str
    state: str
    status: str
    created_at: str
    updated_at: str


class OrganisationLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: int = Field(gt=0)
    relationship_type: str = Field(pattern="^(official_practice_site|corporate_parent|branch_page|third_party_directory)$")
    scope_path: str | None = Field(default=None, max_length=512)
    verification_status: str = Field(default="suggested", pattern="^(suggested|confirmed|rejected)$")
    verified_by: str | None = Field(default=None, max_length=255)


class OrganisationLinkUpdate(BaseModel):
    """Operator-driven link verification status transition (suggested -> confirmed | rejected)."""
    model_config = ConfigDict(extra="forbid")

    verification_status: str = Field(pattern="^(suggested|confirmed|rejected)$")
    relationship_type: str | None = Field(default=None, pattern="^(official_practice_site|corporate_parent|branch_page|third_party_directory)$")
    scope_path: str | None = Field(default=None, max_length=512)
    verified_by: str | None = Field(default=None, max_length=255)


class OrganisationLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organisation_id: int
    source_id: int
    relationship_type: str
    scope_path: str | None = None
    verification_status: str
    verified_by: str | None = None
    verified_at: str | None = None
    created_at: str


class OrganisationDetailRead(OrganisationRead):
    source_links: list[OrganisationLinkRead] = Field(default_factory=list)
    active_facts_count: int = 0


class OrganisationMatchCandidateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    suggested_organisation_id: int
    match_tier: str
    raw_metric_score: float | None = None
    evidence_signals: dict[str, Any]
    rationale: str
    status: str
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    created_at: str


class SourceObservationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    observed_at: str
    outcome: str
    snapshot_id: int | None = None
    content_hash: str | None = None
    content_changed: bool
    parsing_completeness: str
    extraction_completeness: str
    headings_count: int
    character_count: int
    observation_sequence: int
    error_details: str | None = None
    created_at: str


# ---------------------------------------------------------------------------
# M3.2 Models: Discrepancy Detection (ARCHITECTURE §6.1, DECISION_LOG D41)
# ---------------------------------------------------------------------------

class DiscrepancyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organisation_id: int
    field_key: str
    scope_key: str
    fact_a_id: int
    fact_b_id: int
    conflict_type: str
    status: str
    rationale: str | None = None
    resolution_notes: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    detected_at: str
    created_at: str


class DiscrepancyDetailRead(DiscrepancyRead):
    fact_a: FactRead
    fact_b: FactRead
    source_a: SourceRead
    source_b: SourceRead


class DiscrepancyScanResultRead(BaseModel):
    organisation_id: int
    total_facts_scanned: int
    comparisons_evaluated: int
    discrepancies_detected: int
    discrepancies: list[DiscrepancyRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# M3.3 Models: Temporal Change Detection (ARCHITECTURE §6.1, DECISION_LOG D42)
# ---------------------------------------------------------------------------

class ChangeEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    organisation_id: int | None = None
    previous_observation_id: int | None = None
    current_observation_id: int
    previous_snapshot_id: int | None = None
    current_snapshot_id: int
    previous_fact_id: int | None = None
    current_fact_id: int | None = None
    event_type: str
    entity_type: str = "fact"
    field_key: str
    scope_key: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    rationale: str
    diff_metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    detected_at: str
    created_at: str


class ChangeEventDetailRead(ChangeEventRead):
    source: SourceRead
    organisation: OrganisationRead | None = None
    previous_fact: FactRead | None = None
    current_fact: FactRead | None = None
    previous_snapshot: SnapshotHead | None = None
    current_snapshot: SnapshotHead | None = None


class ChangeScanResultRead(BaseModel):
    source_id: int
    observations_evaluated: int
    changes_detected: int
    changes: list[ChangeEventRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# M3.4 Models: Information Freshness & Policy Engine (ARCHITECTURE §6.1, D46)
# ---------------------------------------------------------------------------

class FreshnessPolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    policy_id: str
    version: str
    description: str
    source_observation_threshold_days: int = 30
    source_content_threshold_days: int = 90
    category_thresholds_days: dict[str, int]
    aging_threshold_ratio: float
    source_observation_overrides: dict[int, int] = Field(default_factory=dict)
    source_overrides: dict[int, dict[str, int]] = Field(default_factory=dict)
    threshold_governance_documentation: dict[str, str] = Field(default_factory=dict)


class SourceObservationFreshnessRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: int
    last_attempted_at: str | None = None
    last_successful_observation_at: str | None = None
    last_new_snapshot_at: str | None = None
    last_unchanged_observation_at: str | None = None
    last_complete_parsing_at: str | None = None
    last_complete_extraction_at: str | None = None
    observation_age_days: float | None = None
    classification: str
    policy_id: str
    threshold_days: int
    threshold_governance: str = "source_observation"
    latest_attempt_status: str | None = None
    latest_attempt_failed: bool = False
    as_of_reference_time: str
    rationale: str


class SourceContentFreshnessRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: int
    current_version_first_observed_at: str | None = None
    previous_version_observed_at: str | None = None
    previous_version_content_hash: str | None = None
    content_observation_age_days: float | None = None
    actual_publisher_modified_at: str | None = None
    last_content_change_at: str | None = None
    content_age_days: float | None = None
    latest_content_hash: str | None = None
    snapshot_count: int
    unchanged_reobservations_count: int
    policy_id: str = "ps_freshness_v1"
    threshold_days: int = 90
    threshold_governance: str = "source_content"
    as_of_reference_time: str
    rationale: str


class EvidenceVerificationFreshnessRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evidence_id: int | None = None
    snapshot_id: int | None = None
    captured_at: str | None = None
    checked_at: str | None = None
    evaluated_at: str | None = None
    is_verified: bool
    quote_length: int
    policy_id: str = "ps_freshness_v1"
    threshold_governance: str = "immutable_snapshot_integrity"
    as_of_reference_time: str
    rationale: str


class FactFreshnessRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fact_id: int
    source_id: int
    organisation_id: int | None = None
    field_key: str
    information_category: str
    raw_value: str | None = None
    observation_freshness: SourceObservationFreshnessRead
    content_freshness: SourceContentFreshnessRead
    evidence_freshness: EvidenceVerificationFreshnessRead
    has_explicit_temporal_evidence: bool
    explicit_fact_date: str | None = None
    fact_date_type: str | None = None
    temporal_evidence_quote: str | None = None
    date_precision: str | None = None
    date_range_start: str | None = None
    date_range_end: str | None = None
    min_fact_age_days: float | None = None
    max_fact_age_days: float | None = None
    fact_age_days: float | None = None
    fact_freshness_classification: str
    overall_classification: str
    policy_id: str
    policy_threshold_days: int
    threshold_governance: str = "fact_category"
    as_of_reference_time: str
    rationale: str


class OrganisationFreshnessSummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organisation_id: int
    organisation_name: str
    confirmed_source_count: int
    total_facts_count: int
    fresh_facts_count: int
    aging_facts_count: int
    stale_facts_count: int
    unknown_facts_count: int
    unobservable_sources_count: int
    sources: list[SourceObservationFreshnessRead] = Field(default_factory=list)
    as_of_reference_time: str
    policy_id: str
    summary_rationale: str


class FreshnessEvaluateRequest(BaseModel):
    source_id: int | None = None
    organisation_id: int | None = None
    fact_id: int | None = None
    policy_id: str | None = None
    source_observation_threshold_override: int | None = None
    category_thresholds_override: dict[str, int] | None = None
    reference_time: str | None = None


# =============================================================================
# M3.5 OPPORTUNITY MODELS (D43)
# =============================================================================

class OpportunityRead(BaseModel):
    """Healthcare workforce recruitment vacancy entity (D43)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    organisation_id: int
    source_id: int
    first_snapshot_id: int
    last_snapshot_id: int
    role_category: str
    role_title: str
    verbatim_wording: str
    contact_channel: str | None = None
    explicit_ad_id: str | None = None
    status: str
    closure_evidence_quote: str | None = None
    consecutive_absences: int = 0
    first_seen_at: str
    last_seen_at: str
    closed_at: str | None = None
    created_at: str
    updated_at: str


class OpportunityDetailRead(OpportunityRead):
    """Detailed vacancy read model with contextual relations."""
    organisation_name: str | None = None
    source_url: str | None = None


class OpportunityListResponse(BaseModel):
    """Paginated list of healthcare workforce opportunities."""
    items: list[OpportunityRead]
    total: int
    limit: int
    offset: int


class OpportunityScanResultRead(BaseModel):
    """Result of deterministic opportunity scan for a source."""
    source_id: int
    opportunities_found: int
    opportunities_created: int
    opportunities_updated: int
    status_counts: dict[str, int] = Field(default_factory=dict)
    opportunities: list[OpportunityRead] = Field(default_factory=list)
# =============================================================================
# M3.6 HUMAN REVIEW & AUDIT WORKFLOW MODELS (D44)
# =============================================================================

class ReviewQueueItemRead(BaseModel):
    queue_id: str
    entity_type: str
    entity_id: int
    title: str
    summary: str
    flagged_reason: str
    current_status: str
    review_status: str
    priority: str
    organisation_id: int | None = None
    organisation_name: str | None = None
    source_id: int | None = None
    source_url: str | None = None
    evidence_quote: str | None = None
    created_at: str


class ReviewQueueResponse(BaseModel):
    items: list[ReviewQueueItemRead]
    total: int
    limit: int
    offset: int


class ReviewMetricsRead(BaseModel):
    total_pending: int
    total_under_review: int
    total_completed: int
    total_deferred: int
    by_entity_type: dict[str, int] = Field(default_factory=dict)


class ReviewActionRead(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    action: str
    previous_status: str
    new_status: str
    reason_code: str
    reviewer: str
    notes: str | None = None
    evidence_id: int | None = None
    decision_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ReviewActionCreate(BaseModel):
    action: str
    reason_code: str
    reviewer: str
    notes: str | None = None
    evidence_id: int | None = None
    decision_metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewItemDetailRead(BaseModel):
    entity_type: str
    entity_id: int
    title: str
    summary: str
    flagged_reason: str
    current_status: str
    review_status: str
    priority: str
    organisation_id: int | None = None
    organisation_name: str | None = None
    source_id: int | None = None
    source_url: str | None = None
    snapshot_id: int | None = None
    snapshot_title: str | None = None
    snapshot_fetched_at: str | None = None
    evidence_id: int | None = None
    evidence_quote: str | None = None
    evidence_start_offset: int | None = None
    evidence_end_offset: int | None = None
    entity_data: dict[str, Any] = Field(default_factory=dict)
    discrepancy_context: dict[str, Any] | None = None
    opportunity_context: dict[str, Any] | None = None
    change_context: dict[str, Any] | None = None
    lifecycle_context: dict[str, Any] | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    review_history: list[ReviewActionRead] = Field(default_factory=list)


class ReviewHistoryResponse(BaseModel):
    items: list[ReviewActionRead]
    total: int
    limit: int
    offset: int
