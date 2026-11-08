from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator
from .config import Strict

Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
]
Version = Annotated[str, Field(min_length=1, max_length=200)]


class Lease(Strict):
    run_id: UUID
    lease_token: UUID


class Claim(Strict):
    household_id: str = "home"
    local_date: date
    config_hash: Version
    dry_run: bool = True
    notify: bool = False


class Heartbeat(Strict):
    lease_token: UUID


class Event(Strict):
    id: Identifier
    start_time: AwareDatetime
    end_time: AwareDatetime | None = None
    camera: Identifier
    zones: list[str] = Field(default_factory=list, max_length=100)
    has_snapshot: bool = False
    has_clip: bool = False
    raw: dict

    @model_validator(mode="after")
    def bounds(self):
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("Event ends before start")
        return self


class DateRange(Strict):
    local_start: date
    local_end: date

    @model_validator(mode="after")
    def interval(self):
        if not 1 <= (self.local_end - self.local_start).days <= 31:
            raise ValueError("Expected a half-open calendar interval of 1–31 days")
        return self


class Upsert(Lease, DateRange):
    source_id: Identifier
    events: list[Event] = Field(max_length=500)
    collection_status: Literal["complete", "partial", "failed"] = "partial"
    coverage: dict = Field(default_factory=lambda: {"status": "unknown"})


class Candidate(Strict):
    station_id: Identifier
    member_event_ids: list[Identifier] = Field(min_length=1, max_length=1000)
    pet_id: Literal["coconut"] | None = "coconut"
    identity_method: Literal["single_pet_assumption", "unknown"] = "single_pet_assumption"


class Batch(Lease, DateRange):
    grouping_version: Version
    candidates: list[Candidate] = Field(max_length=1000)


class Cache(Lease):
    event_ids: list[Identifier] = Field(min_length=1, max_length=10)
    variants: list[Literal["snapshot", "bbox", "clip"]] = Field(
        default=["snapshot"], min_length=1, max_length=3
    )


class Observation(Strict):
    behavior: Literal["eating", "drinking", "urination", "defecation", "other", "unknown"]
    assessment: Literal["supported", "not_observed", "uncertain"]
    evidence_ids: list[UUID] = Field(max_length=20)
    reason: str = Field(max_length=500)


class ClassificationResult(Strict):
    schema_version: Literal[1]
    visit_id: UUID
    pet_id: Literal["coconut"] | None
    identity_method: Literal["single_pet_assumption", "unknown"]
    observations: list[Observation] = Field(max_length=20)
    evidence_quality: Literal["good", "limited", "missing"]


class Classification(Lease):
    result: ClassificationResult
    model: dict[str, str]
    prompt_version: Version
    input_fingerprint: Version


class Prepare(Lease, DateRange):
    kind: Literal["daily", "weekly"]
    timezone: str = "America/Toronto"


class Finalize(Lease):
    expected_revision: int = Field(ge=1)
    # Unverified free-form model claims are not published in v1.
    narrative: str | None = Field(default=None, max_length=2000)
    use_template: bool = True


class DeliveryClaim(Lease):
    local_send_date: date
    channel: Literal["home_assistant"] = "home_assistant"
    report_id: UUID


class DeliveryResult(Lease):
    result: Literal["accepted", "definitely_failed", "unknown"]
    # Machine codes only: never persist an upstream exception or credential.
    error_code: Literal["pre_send_failure", "timeout", "transport_failure", "upstream_rejected"] | None = None


class RunDiagnostic(Strict):
    request_stage: Literal[
        "readiness",
        "claim",
        "processing",
        "stored_boundary",
        "boundary_context",
        "login",
        "relogin",
        "oldest",
        "source_page",
        "refresh_open",
        "member_detail",
        "heartbeat",
        "group",
        "upsert",
        "reconcile",
        "cache",
        "visit_detail",
        "image",
        "classification",
        "prepare",
        "finalize",
        "delivery_claim",
        "delivery_send",
        "delivery_result",
        "cleanup",
        "finish",
        "failed_finish",
    ]
    http_status: int = Field(ge=0, le=599)
    code: Literal["http_failure", "invalid_response", "transport_failure", "planning_failure"]


class Finish(Lease):
    diagnostic: RunDiagnostic | None = None
    status: Literal["complete", "partial", "failed"]
    stage: Literal["collect", "group", "classify", "report", "delivery", "finished"]
    error_codes: list[
        Literal[
            "collection_incomplete",
            "media_unavailable",
            "model_failed",
            "delivery_unknown",
            "dependency_unavailable",
        ]
    ] = Field(default_factory=list)


class Cleanup(Strict):
    dry_run: bool = True
