"""Pydantic v2 domain models for Milestone 3 intelligence capabilities (ARCHITECTURE §6.1, D39–D47).

Single source of truth for:
- Healthcare practice organisations & multi-source link mappings (D39, D40)
- Source observation heartbeats capturing unchanged crawls and failure states (D40)
- Conservative 3-tier entity resolution matching without probability claims (D39)
- Evidence-backed cross-source discrepancy detection with scope isolation (D41)
- Temporal snapshot change events distinguishing omissions from removals (D42)
- Category-specific information freshness with 4 distinct timestamps (D46)
- Healthcare recruitment vacancy intelligence with conservative lifecycles (D43)
- Application-level append-only human review actions by locally identified reviewers (D44)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


# =============================================================================
# ENUMS — DOMAIN VOCABULARIES & CONSERVATIVE LIFECYCLES
# =============================================================================

class OrganisationStatus(StrEnum):
    """Lifecycle status of a healthcare practice organisation."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING_REVIEW = "pending_review"
    MERGED = "merged"


class RelationshipType(StrEnum):
    """Categorical role of a source URL in relation to a practice organisation (D40)."""
    OFFICIAL_PRACTICE_SITE = "official_practice_site"
    CORPORATE_PARENT = "corporate_parent"
    BRANCH_PAGE = "branch_page"
    THIRD_PARTY_DIRECTORY = "third_party_directory"


class VerificationStatus(StrEnum):
    """Verification state of a source-to-organisation link."""
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ObservationOutcome(StrEnum):
    """Outcome of an ingestion attempt (captures unchanged short-circuits and errors) (D40)."""
    SUCCESS_NEW_SNAPSHOT = "success_new_snapshot"
    SUCCESS_UNCHANGED = "success_unchanged"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class ParsingCompleteness(StrEnum):
    """Structural parsing completeness of page snapshot text."""
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_RUN = "not_run"


class ExtractionCompleteness(StrEnum):
    """AI extraction outcome completeness across intended source fields."""
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_RUN = "not_run"


class MatchTier(StrEnum):
    """Qualitative entity matching confidence tier (D39).

    Explicitly separates raw metric scores from probability claims.
    """
    STRONG_MATCH_SUGGESTION = "strong_match_suggestion"
    POSSIBLE_MATCH = "possible_match"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ReviewStatus(StrEnum):
    """Status of an entity resolution match candidate suggestion and general review queue."""
    SUGGESTED = "suggested"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    ACCEPTED = "accepted"
    DEFERRED = "deferred"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ConflictType(StrEnum):
    """Nature of a factual discrepancy between two independent sources (D41)."""
    VALUE_MISMATCH = "value_mismatch"
    TEMPORAL_DIVERGENCE = "temporal_divergence"
    AMBIGUITY = "ambiguity"


class DiscrepancyStatus(StrEnum):
    """Resolution lifecycle state of a detected factual discrepancy."""
    DETECTED = "detected"
    RESOLVED_FACT_A = "resolved_fact_a"
    RESOLVED_FACT_B = "resolved_fact_b"
    DISMISSED_BENIGN = "dismissed_benign"
    NEEDS_INVESTIGATION = "needs_investigation"


class ChangeEventCategory(StrEnum):
    """Temporal lifecycle event between successive page snapshots (D42)."""
    FACT_ADDED = "fact_added"
    FACT_MODIFIED = "fact_modified"
    UNCONFIRMED_PRIOR = "unconfirmed_prior"
    FACT_UNOBSERVED = "fact_unobserved"
    FACT_REAPPEARED = "fact_reappeared"


class FreshnessCategory(StrEnum):
    """Temporal freshness classification of an observed fact or source (D46)."""
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"
    UNOBSERVABLE = "unobservable"
    UNCONFIRMED_PRIOR = "unconfirmed_prior"
    NO_LONGER_OBSERVED = "no_longer_observed"


class FreshnessInformationCategory(StrEnum):
    """Healthcare practice information categories for monitoring policies."""
    IDENTITY = "identity"
    OPERATIONAL = "operational"
    SERVICES = "services"
    HOURS = "hours"
    GENERAL = "general"


class TemporalPrecision(StrEnum):
    """Precision of explicitly stated temporal evidence (day, month, year, or unknown/missing)."""
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


class OpportunityStatus(StrEnum):
    """Conservative lifecycle states for healthcare vacancies (D43).

    NOTE: Status 'filled' or 'hired' is STRICTLY FORBIDDEN by architecture.
    """
    ADVERTISED = "advertised"
    CHANGED = "changed"
    POSSIBLY_STALE = "possibly_stale"
    UNCONFIRMED_PRIOR = "unconfirmed_prior"
    NO_LONGER_OBSERVED = "no_longer_observed"
    EXPLICITLY_CLOSED = "explicitly_closed"


class RoleCategory(StrEnum):
    """Standardized healthcare workforce role categories."""
    GP = "gp"
    NURSE = "nurse"
    ALLIED_HEALTH = "allied_health"
    ADMINISTRATIVE = "administrative"
    SPECIALIST = "specialist"
    OTHER = "other"


class ReviewActionType(StrEnum):
    """Action taken by a locally identified reviewer in the audit log (D44)."""
    APPROVED = "approved"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RESOLVED_A = "resolved_a"
    RESOLVED_B = "resolved_b"
    RESOLVED = "resolved"
    DISMISSED_BENIGN = "dismissed_benign"
    DISMISSED = "dismissed"
    ASSIGNED_ORG = "assigned_org"
    REOPENED = "reopened"
    DEFERRED = "deferred"
    ACKNOWLEDGED = "acknowledged"
    UNDER_REVIEW = "under_review"


class LifecycleEventType(StrEnum):
    """Categorical type of healthcare practice lifecycle event."""
    PERMANENT_CLOSURE = "permanent_closure"
    TEMPORARY_CLOSURE = "temporary_closure"
    RELOCATION = "relocation"
    ACQUISITION = "acquisition"


class LifecycleStatus(StrEnum):
    """Evidence confidence and verification state for practice lifecycle signals."""
    CONFIRMED_EXPLICIT = "confirmed_explicit"
    POTENTIAL_NEEDS_REVIEW = "potential_needs_review"
    NO_LONGER_OBSERVED = "no_longer_observed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_SOURCES = "conflicting_sources"


# =============================================================================
# DOMAIN CONTRACTS — PYDANTIC SCHEMAS
# =============================================================================

class Organisation(BaseModel):
    """Canonical healthcare practice organisation entity."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    primary_domain: str | None = Field(default=None, max_length=255)
    corporate_parent_id: int | None = None
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=512)
    suburb: str | None = Field(default=None, max_length=128)
    postcode: str = Field(min_length=3, max_length=16)
    state: str = Field(min_length=2, max_length=16)
    status: OrganisationStatus = Field(default=OrganisationStatus.ACTIVE)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class OrganisationSourceLink(BaseModel):
    """Additive N:M mapping between organisations and registered source URLs (D40)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    organisation_id: int = Field(gt=0)
    source_id: int = Field(gt=0)
    relationship_type: RelationshipType
    scope_path: str | None = Field(default=None, max_length=512)
    verification_status: VerificationStatus = Field(default=VerificationStatus.SUGGESTED)
    verified_by: str | None = Field(default=None, max_length=255)
    verified_at: datetime | None = None
    created_at: datetime | None = None


class SourceObservation(BaseModel):
    """Observation attempt heartbeat recording successful crawls, unchanged states, and failures (D40)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    source_id: int = Field(gt=0)
    observed_at: datetime
    outcome: ObservationOutcome
    snapshot_id: int | None = None
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    content_changed: bool = False
    parsing_completeness: ParsingCompleteness = Field(default=ParsingCompleteness.COMPLETE)
    extraction_completeness: ExtractionCompleteness = Field(default=ExtractionCompleteness.COMPLETE)
    headings_count: int = Field(default=0, ge=0)
    character_count: int = Field(default=0, ge=0)
    observation_sequence: int = Field(gt=0)
    error_details: str | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_outcome_consistency(self) -> SourceObservation:
        """Enforce internal consistency between outcome, snapshot_id, and content_changed."""
        if self.outcome == ObservationOutcome.SUCCESS_NEW_SNAPSHOT and not self.content_changed:
            raise ValueError("SUCCESS_NEW_SNAPSHOT requires content_changed=True")
        if self.outcome == ObservationOutcome.SUCCESS_UNCHANGED and self.content_changed:
            raise ValueError("SUCCESS_UNCHANGED requires content_changed=False")
        if self.outcome == ObservationOutcome.FAILED and self.content_changed:
            raise ValueError("FAILED outcome cannot report content_changed=True")
        return self


class MatchSignals(BaseModel):
    """Detailed factual evidence signals supporting an entity match candidate (D39)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    domain_match: bool = False
    postcode_match: bool = False
    local_phone_match: bool = False
    corporate_phone_detected: bool = False
    branch_slug_match: bool = False
    street_address_match: bool = False
    name_similarity_metric: float | None = Field(default=None, ge=0.0, le=100.0)


class OrganisationMatchCandidate(BaseModel):
    """Conservative entity resolution match suggestion between a source and organisation (D39)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    source_id: int = Field(gt=0)
    suggested_organisation_id: int = Field(gt=0)
    match_tier: MatchTier
    raw_metric_score: float | None = Field(default=None, ge=0.0, le=100.0)
    evidence_signals: MatchSignals
    rationale: str = Field(min_length=1, max_length=2000)
    status: ReviewStatus = Field(default=ReviewStatus.SUGGESTED)
    reviewed_by: str | None = Field(default=None, max_length=255)
    reviewed_at: datetime | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_tier_signals(self) -> OrganisationMatchCandidate:
        """Validate that strong match suggestions possess branch-level evidence signals."""
        if self.match_tier == MatchTier.STRONG_MATCH_SUGGESTION:
            has_branch_proof = (
                (self.evidence_signals.local_phone_match and self.evidence_signals.postcode_match) or
                (self.evidence_signals.domain_match and self.evidence_signals.postcode_match and not self.evidence_signals.corporate_phone_detected) or
                (self.evidence_signals.branch_slug_match and self.evidence_signals.postcode_match)
            )
            if not has_branch_proof:
                raise ValueError("STRONG_MATCH_SUGGESTION requires branch-level identity proof (not parent domain alone)")
        return self


class Discrepancy(BaseModel):
    """Evidence-backed factual conflict between independent sources linked to the same organisation (D41)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    organisation_id: int = Field(gt=0)
    field_key: str = Field(min_length=1, max_length=64)
    scope_key: str = Field(min_length=1, max_length=64)
    fact_a_id: int = Field(gt=0)
    fact_b_id: int = Field(gt=0)
    conflict_type: ConflictType = Field(default=ConflictType.VALUE_MISMATCH)
    status: DiscrepancyStatus = Field(default=DiscrepancyStatus.DETECTED)
    rationale: str | None = Field(default=None, max_length=2000)
    resolution_notes: str | None = Field(default=None, max_length=2000)
    resolved_by: str | None = Field(default=None, max_length=255)
    resolved_at: datetime | None = None
    detected_at: datetime | None = None
    created_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def canonicalize_fact_ids(cls, data: Any) -> Any:
        if isinstance(data, dict):
            a = data.get("fact_a_id")
            b = data.get("fact_b_id")
            if isinstance(a, int) and isinstance(b, int) and a > b:
                data["fact_a_id"], data["fact_b_id"] = b, a
        return data

    @model_validator(mode="after")
    def validate_distinct_facts(self) -> Discrepancy:
        if self.fact_a_id == self.fact_b_id:
            raise ValueError("fact_a_id and fact_b_id must be distinct")
        return self


class DiffMetadata(BaseModel):
    """Granular diff metadata detailing old vs new values in a change event (D42)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    wording_diff: str | None = None
    old_normalized: str | None = None
    new_normalized: str | None = None
    omission_confirmed_in_dom: bool = False
    observation_count_absent: int = Field(default=0, ge=0)


class ChangeEvent(BaseModel):
    """Immutable, append-only record of a detected fact or opportunity modification (D42)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    source_id: int = Field(gt=0)
    organisation_id: int | None = None
    previous_observation_id: int | None = None
    current_observation_id: int | None = None
    previous_snapshot_id: int | None = None
    current_snapshot_id: int = Field(gt=0)
    previous_fact_id: int | None = None
    current_fact_id: int | None = None
    event_type: ChangeEventCategory
    entity_type: str = Field(default="fact", min_length=1, max_length=32)
    entity_id: int | None = None
    field_key: str = Field(min_length=1, max_length=64)
    scope_key: str | None = Field(default=None, max_length=64)
    old_value: str | None = None
    new_value: str | None = None
    rationale: str | None = None
    diff_metadata: DiffMetadata = Field(default_factory=DiffMetadata)
    idempotency_key: str | None = None
    detected_at: datetime | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_distinct_snapshots(self) -> ChangeEvent:
        if self.previous_snapshot_id is not None and self.previous_snapshot_id == self.current_snapshot_id:
            raise ValueError("previous_snapshot_id and current_snapshot_id must be distinct")
        return self


class FreshnessAssessment(BaseModel):
    """Category-specific temporal freshness evaluation for a fact or vacancy (D46)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    entity_id: int = Field(gt=0)
    field_key: str = Field(min_length=1, max_length=64)
    freshness_category: FreshnessCategory
    observed_at: datetime
    published_at: datetime | None = None
    updated_at: datetime | None = None
    verified_at: datetime | None = None
    observation_age_days: float = Field(ge=0.0)
    policy_threshold_days: int = Field(gt=0)
    as_of_reference_time: datetime


class FreshnessPolicy(BaseModel):
    """Configurable monitoring policy defining governance thresholds (D46)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    policy_id: str = Field(default="ps_freshness_v1", min_length=1, max_length=64)
    version: str = Field(default="1.0.0", min_length=1, max_length=32)
    description: str = Field(default="PracticeSignal deterministic monitoring policy", max_length=255)
    source_observation_threshold_days: int = Field(default=30, gt=0)
    source_content_threshold_days: int = Field(default=90, gt=0)
    category_thresholds_days: dict[str, int] = Field(
        default_factory=lambda: {
            "identity": 90,
            "operational": 30,
            "services": 60,
            "hours": 30,
            "general": 60,
        }
    )
    aging_threshold_ratio: float = Field(default=0.8, gt=0.0, lt=1.0)
    source_observation_overrides: dict[int, int] = Field(default_factory=dict)
    source_overrides: dict[int, dict[str, int]] = Field(default_factory=dict)

    def get_source_observation_threshold(self, source_id: int | None = None) -> int:
        """Resolve threshold days specifically governing source observation freshness."""
        if source_id is not None and source_id in self.source_observation_overrides:
            return self.source_observation_overrides[source_id]
        return self.source_observation_threshold_days

    def get_source_content_threshold(self, source_id: int | None = None) -> int:
        """Resolve threshold days specifically governing source content freshness."""
        return self.source_content_threshold_days

    def get_fact_threshold(self, category: str, source_id: int | None = None) -> int:
        """Resolve category-specific fact threshold with source override fallback."""
        if source_id is not None and source_id in self.source_overrides:
            override = self.source_overrides[source_id].get(category)
            if override is not None:
                return override
        return self.category_thresholds_days.get(category, 60)

    def get_threshold(self, category: str, source_id: int | None = None) -> int:
        """Backward-compatible alias for fact threshold resolution."""
        return self.get_fact_threshold(category, source_id)

    def classify_age(self, age_days: float, threshold_days: int) -> FreshnessCategory:
        """Classify numeric age against threshold with aging window."""
        if age_days < 0:
            age_days = 0.0
        if age_days < threshold_days * self.aging_threshold_ratio:
            return FreshnessCategory.FRESH
        if age_days <= threshold_days:
            return FreshnessCategory.AGING
        return FreshnessCategory.STALE


class SourceObservationFreshness(BaseModel):
    """Observation freshness evaluating how recently PracticeSignal observed a source."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    source_id: int = Field(gt=0)
    last_attempted_at: datetime | None = None
    last_successful_observation_at: datetime | None = None
    last_new_snapshot_at: datetime | None = None
    last_unchanged_observation_at: datetime | None = None
    last_complete_parsing_at: datetime | None = None
    last_complete_extraction_at: datetime | None = None
    observation_age_days: float | None = None
    classification: FreshnessCategory
    policy_id: str
    threshold_days: int
    threshold_governance: str = "source_observation"
    latest_attempt_status: str | None = None
    latest_attempt_failed: bool = False
    as_of_reference_time: datetime
    rationale: str


class SourceContentFreshness(BaseModel):
    """Content freshness evaluating when a source's observed content was first captured."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    source_id: int = Field(gt=0)
    current_version_first_observed_at: datetime | None = None
    previous_version_observed_at: datetime | None = None
    previous_version_content_hash: str | None = None
    content_observation_age_days: float | None = None
    actual_publisher_modified_at: datetime | None = None
    last_content_change_at: datetime | None = None
    content_age_days: float | None = None
    latest_content_hash: str | None = None
    snapshot_count: int = Field(ge=0)
    unchanged_reobservations_count: int = Field(ge=0)
    policy_id: str = "ps_freshness_v1"
    threshold_days: int = 90
    threshold_governance: str = "source_content"
    as_of_reference_time: datetime
    rationale: str


class EvidenceVerificationFreshness(BaseModel):
    """Freshness of evidence quotation integrity verification."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    evidence_id: int | None = None
    snapshot_id: int | None = None
    captured_at: datetime | None = None
    checked_at: datetime
    evaluated_at: datetime | None = None
    is_verified: bool = False
    quote_length: int = Field(default=0, ge=0)
    policy_id: str = "ps_freshness_v1"
    threshold_governance: str = "immutable_snapshot_integrity"
    as_of_reference_time: datetime
    rationale: str


class FactFreshnessDetail(BaseModel):
    """Complete 4-dimensional freshness evaluation for a specific stored practice fact."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    fact_id: int = Field(gt=0)
    source_id: int = Field(gt=0)
    organisation_id: int | None = None
    field_key: str
    information_category: FreshnessInformationCategory
    raw_value: str | None = None
    observation_freshness: SourceObservationFreshness
    content_freshness: SourceContentFreshness
    evidence_freshness: EvidenceVerificationFreshness
    has_explicit_temporal_evidence: bool = False
    explicit_fact_date: datetime | None = None
    fact_date_type: str | None = None
    temporal_evidence_quote: str | None = None
    date_precision: TemporalPrecision | None = None
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    min_fact_age_days: float | None = None
    max_fact_age_days: float | None = None
    fact_age_days: float | None = None
    fact_freshness_classification: FreshnessCategory
    overall_classification: FreshnessCategory
    policy_id: str
    policy_threshold_days: int
    threshold_governance: str = "fact_category"
    as_of_reference_time: datetime
    rationale: str


class OrganisationFreshnessSummary(BaseModel):
    """Freshness evaluation summary for an organisation across all linked sources and facts."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    organisation_id: int = Field(gt=0)
    organisation_name: str
    confirmed_source_count: int = Field(ge=0)
    total_facts_count: int = Field(ge=0)
    fresh_facts_count: int = Field(ge=0)
    aging_facts_count: int = Field(ge=0)
    stale_facts_count: int = Field(ge=0)
    unknown_facts_count: int = Field(ge=0)
    unobservable_sources_count: int = Field(ge=0)
    sources: list[SourceObservationFreshness] = Field(default_factory=list)
    as_of_reference_time: datetime
    policy_id: str
    summary_rationale: str


class Opportunity(BaseModel):
    """Healthcare workforce recruitment vacancy entity (D43)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    organisation_id: int = Field(gt=0)
    source_id: int = Field(gt=0)
    first_snapshot_id: int = Field(gt=0)
    last_snapshot_id: int = Field(gt=0)
    role_category: RoleCategory
    role_title: str = Field(min_length=1, max_length=255)
    verbatim_wording: str = Field(min_length=1)
    contact_channel: str | None = Field(default=None, max_length=255)
    explicit_ad_id: str | None = Field(default=None, max_length=128)
    status: OpportunityStatus = Field(default=OpportunityStatus.ADVERTISED)
    closure_evidence_quote: str | None = None
    consecutive_absences: int = Field(default=0, ge=0)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_closure_proof(self) -> Opportunity:
        """Enforce that status EXPLICITLY_CLOSED requires verbatim evidence quote."""
        if self.status == OpportunityStatus.EXPLICITLY_CLOSED and not self.closure_evidence_quote:
            raise ValueError("status EXPLICITLY_CLOSED strictly requires closure_evidence_quote")
        return self


class ReviewAction(BaseModel):
    """Application-level append-only record of a human decision by a locally identified reviewer (D44)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    entity_type: str = Field(min_length=1, max_length=32)
    entity_id: int = Field(gt=0)
    action: ReviewActionType
    previous_status: str = Field(min_length=1, max_length=32)
    new_status: str = Field(min_length=1, max_length=32)
    reason_code: str = Field(min_length=1, max_length=64)
    reviewer: str = Field(min_length=1, max_length=255)  # Locally identified reviewer (e.g. "analyst_1")
    notes: str | None = Field(default=None, max_length=2000)
    evidence_id: int | None = None
    decision_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class PracticeLifecycleEvent(BaseModel):
    """Healthcare practice lifecycle event (closure, relocation, corporate acquisition)."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int | None = None
    organisation_id: int = Field(gt=0)
    branch_locality: str | None = Field(default=None, max_length=128)
    is_branch_level: bool = False
    event_type: LifecycleEventType
    status: LifecycleStatus
    source_id: int = Field(gt=0)
    snapshot_id: int = Field(gt=0)
    evidence_id: int | None = None
    evidence_quote: str | None = None
    effective_date: str | None = Field(default=None, max_length=64)
    date_precision: TemporalPrecision = Field(default=TemporalPrecision.UNKNOWN)
    old_value: str | None = None
    new_value: str | None = None
    acquiring_organisation_id: int | None = None
    acquiring_entity_name: str | None = Field(default=None, max_length=255)
    rationale: str = Field(min_length=1)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    review_status: ReviewStatus = Field(default=ReviewStatus.PENDING)
    reviewed_by: str | None = Field(default=None, max_length=255)
    reviewed_at: datetime | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)
    detected_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_explicit_evidence(self) -> PracticeLifecycleEvent:
        """Enforce that confirmed explicit closure/relocation/acquisition requires verbatim evidence quote."""
        if self.status == LifecycleStatus.CONFIRMED_EXPLICIT and not self.evidence_quote:
            raise ValueError("status CONFIRMED_EXPLICIT strictly requires evidence_quote")
        return self


class PracticeLifecycleEventRead(BaseModel):
    """Read model for API responses with enriched context."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    organisation_id: int
    organisation_name: str | None = None
    branch_locality: str | None = None
    is_branch_level: bool = False
    event_type: LifecycleEventType
    status: LifecycleStatus
    source_id: int
    source_url: str | None = None
    snapshot_id: int
    evidence_id: int | None = None
    evidence_quote: str | None = None
    effective_date: str | None = None
    date_precision: TemporalPrecision = TemporalPrecision.UNKNOWN
    old_value: str | None = None
    new_value: str | None = None
    acquiring_organisation_id: int | None = None
    acquiring_organisation_name: str | None = None
    acquiring_entity_name: str | None = None
    rationale: str
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    review_status: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    idempotency_key: str
    detected_at: datetime
    created_at: datetime
    updated_at: datetime


class LifecycleScanResult(BaseModel):
    """Summary metrics of a practice lifecycle detection scan."""
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    source_id: int | None = None
    organisation_id: int | None = None
    events_detected: int = Field(ge=0)
    by_type: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    new_events_count: int = Field(ge=0)
    existing_events_count: int = Field(ge=0)
