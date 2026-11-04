"""Validated career records; confirmation is user acknowledgement, not verification."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
BodyText = Annotated[str, Field(min_length=1, max_length=200_000)]


class CareerNotFoundError(LookupError):
    """The requested record does not exist in the selected workspace."""


class CareerConflictError(ValueError):
    """An operation conflicts with a confirmed record or its lifecycle."""


class _CareerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PersonalConditions(_CareerModel):
    days_per_week: Annotated[int, Field(strict=True, ge=1, le=7)] | None = None
    available_months: Annotated[int, Field(strict=True, ge=1)] | None = None
    available_from: date | None = None
    locations: tuple[ShortText, ...] = ()


class ResumeVersion(_CareerModel):
    id: str
    version: Annotated[int, Field(ge=1)]
    parent_version_id: str | None = None
    name: ShortText
    notes: Annotated[str, Field(max_length=5_000)] = ""
    source: Literal["text", "pdf", "docx"] = "text"
    import_id: str | None = None
    text: BodyText
    profile: dict[str, JsonValue] = Field(default_factory=dict)
    status: Literal["draft", "confirmed"] = "draft"
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resume text must not be blank")
        return value


class ResumeSourceLine(_CareerModel):
    number: Annotated[int, Field(ge=1)]
    locator: ShortText
    text: BodyText


ResumeSectionKey = Literal["education", "projects", "skills", "summary", "experience"]
ResumeImportError = Literal["invalid_document", "encrypted", "empty_document", "text_limit", "safety_limit", "file_unavailable"]


class ResumeSourceSection(_CareerModel):
    key: ResumeSectionKey
    line_numbers: tuple[Annotated[int, Field(ge=1)], ...]


class ResumeImport(_CareerModel):
    id: Annotated[str, StringConstraints(pattern=r"^import_[a-f0-9]{32}$")]
    request_id: ShortText
    filename: ShortText
    file_type: Literal["pdf", "docx"]
    size: Annotated[int, Field(ge=1, le=6 * 1024 * 1024)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    name: ShortText
    status: Literal["processing", "ready", "failed"] = "processing"
    text: Annotated[str, Field(max_length=200_000)] = ""
    lines: tuple[ResumeSourceLine, ...] = ()
    sections: tuple[ResumeSourceSection, ...] = ()
    error_code: ResumeImportError | None = None
    resume_version_id: str | None = None
    created_at: datetime
    updated_at: datetime


class JobVersion(_CareerModel):
    id: str
    job_id: str
    version: Annotated[int, Field(ge=1)]
    parent_version_id: str | None = None
    name: ShortText
    company: ShortText | None = None
    source: Literal["text", "image"] = "text"
    import_id: str | None = None
    text: BodyText
    requirements: dict[str, JsonValue] = Field(default_factory=dict)
    status: Literal["draft", "confirmed"] = "draft"
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("job text must not be blank")
        return value


def _source_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _written_count(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {character: index + 1 for index, character in enumerate("一二三四五六七八九")}
    if "十" not in value:
        return digits[value]
    before, after = value.split("十", 1)
    return (digits[before] if before else 1) * 10 + (digits[after] if after else 0)


class CitedJobText(_CareerModel):
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5_000)]
    evidence: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)]


class CitedJobCount(_CareerModel):
    value: Annotated[int, Field(strict=True, ge=1, le=60)]
    evidence: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)]


class JobImageFields(_CareerModel):
    name: CitedJobText | None = None
    company: CitedJobText | None = None
    locations: Annotated[tuple[CitedJobText, ...], Field(max_length=10)] = ()
    responsibilities: Annotated[tuple[CitedJobText, ...], Field(max_length=30)] = ()
    qualifications: Annotated[tuple[CitedJobText, ...], Field(max_length=30)] = ()
    days_per_week: CitedJobCount | None = None
    available_months: CitedJobCount | None = None

    @model_validator(mode="after")
    def short_fields(self) -> JobImageFields:
        if any(item is not None and len(item.value) > 500 for item in (self.name, self.company, *self.locations)):
            raise ValueError("job labels must not exceed 500 characters")
        if self.days_per_week is not None and self.days_per_week.value > 7:
            raise ValueError("days per week must be between 1 and 7")
        return self

    def requirements(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "locations": [item.value for item in self.locations],
            "responsibilities": "\n".join(item.value for item in self.responsibilities),
            "qualifications": "\n".join(item.value for item in self.qualifications),
        }
        for key, count in (("days_per_week", self.days_per_week), ("available_months", self.available_months)):
            if count is not None:
                result[key] = count.value
        return result


class JobImageReading(_CareerModel):
    readable: Annotated[bool, Field(strict=True)]
    text: Annotated[str, Field(max_length=30_000)]
    fields: JobImageFields = Field(default_factory=JobImageFields)
    warnings: Annotated[tuple[ShortText, ...], Field(max_length=10)] = ()

    @model_validator(mode="after")
    def evidence_matches_text(self) -> JobImageReading:
        if self.readable and not self.text.strip():
            raise ValueError("readable images require extracted text")
        body = _source_text(self.text)
        fields = self.fields
        for item in (fields.name, fields.company, *fields.locations, *fields.responsibilities, *fields.qualifications):
            if item is not None and (_source_text(item.evidence) not in body or _source_text(item.value) not in _source_text(item.evidence)):
                raise ValueError("each field must quote the extracted text without inventing content")
        for item, pattern in (
            (fields.days_per_week, r"(?<!\d)([1-7一二三四五六七])天/周|每周(?:至少|不少于)?([1-7一二三四五六七])天|(?<!\d)([1-7])days?(?:/|per|a)week"),
            (fields.available_months, r"(?<!\d)(\d{1,2}|[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九])(?:个?月|months?)"),
        ):
            if item is None:
                continue
            if _source_text(item.evidence) not in body:
                raise ValueError("numeric evidence must occur in extracted text")
            matches = re.finditer(pattern, _source_text(item.evidence))
            numbers: list[int] = []
            for match in matches:
                value = next((part for part in match.groups() if part), "")
                numbers.append(_written_count(value))
            if item.value not in numbers:
                raise ValueError("numeric values require explicit matching day/month evidence")
        return self


JobImageError = Literal["invalid_image", "image_limit", "file_unavailable", "vision_unavailable", "vision_unverified", "unreadable", "invalid_result", "recognition_failed", "timeout", "interrupted"]


class ImageJobRequirements(_CareerModel):
    locations: Annotated[tuple[ShortText, ...], Field(max_length=10)] = ()
    responsibilities: Annotated[str, Field(max_length=30_000)] = ""
    qualifications: Annotated[str, Field(max_length=30_000)] = ""
    days_per_week: Annotated[int, Field(strict=True, ge=1, le=7)] | None = None
    available_months: Annotated[int, Field(strict=True, ge=1, le=60)] | None = None

    def values(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"locations": list(self.locations), "responsibilities": self.responsibilities, "qualifications": self.qualifications}
        if self.days_per_week is not None:
            result["days_per_week"] = self.days_per_week
        if self.available_months is not None:
            result["available_months"] = self.available_months
        return result


class JobImageImport(_CareerModel):
    id: Annotated[str, StringConstraints(pattern=r"^jobimage_[a-f0-9]{32}$")]
    request_id: ShortText
    filename: ShortText
    file_type: Literal["png", "jpg", "webp"]
    size: Annotated[int, Field(ge=1, le=6 * 1024 * 1024)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    status: Literal["processing", "ready", "failed"] = "processing"
    width: Annotated[int, Field(ge=1, le=10_000)] | None = None
    height: Annotated[int, Field(ge=1, le=10_000)] | None = None
    reading: JobImageReading | None = None
    error_code: JobImageError | None = None
    model: ShortText | None = None
    provider: ShortText | None = None
    job_version_id: str | None = None
    manual: bool = False
    created_at: datetime
    updated_at: datetime


class CareerThread(_CareerModel):
    id: str
    workspace_id: str
    session_key: ShortText
    resume_version_id: str
    job_version_id: str
    conditions: PersonalConditions
    created_at: datetime
    deleted_at: datetime | None = None


class AnalysisSnapshot(_CareerModel):
    resume: ResumeVersion
    job: JobVersion
    conditions: PersonalConditions
    turn_id: ShortText | None = None


AnalysisStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class AnalysisRun(_CareerModel):
    id: str
    career_thread_id: str
    snapshot: AnalysisSnapshot
    status: AnalysisStatus = "queued"
    source_reply_id: ShortText | None = None
    result: Annotated[str, Field(max_length=200_000)] | None = None
    usage: dict[str, Annotated[int, Field(strict=True, ge=0)]] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class TaskInput(_CareerModel):
    title: ShortText
    action: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5_000)]
    completion_criteria: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5_000)
    ]
    basis: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2_000)] = ""


TaskList = Annotated[tuple[TaskInput, ...], Field(min_length=1, max_length=50)]


class PreparationPlan(_CareerModel):
    id: str
    analysis_id: str
    proposed_tasks: TaskList
    status: Literal["draft", "confirmed", "cancelled"] = "draft"
    created_at: datetime
    confirmed_at: datetime | None = None


TaskStatus = Literal["pending", "completed"]


class PreparationTask(_CareerModel):
    id: str
    plan_id: str
    position: Annotated[int, Field(ge=0)]
    title: ShortText
    action: str
    completion_criteria: str
    basis: str = ""
    status: TaskStatus = "pending"
    result: Annotated[str, Field(max_length=20_000)] = ""
    created_at: datetime
    updated_at: datetime
