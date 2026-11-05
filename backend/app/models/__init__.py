"""Pydantic domain models — the single source of API truth (D19).

Pydantic v2 request/response schemas for M1. Frontend types are generated
from FastAPI's OpenAPI schema (which is built from these models), never
hand-written.
"""

from __future__ import annotations

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
    ollama: str  # configured for M2, not used in M1
    robots_tool: str
    version: str
