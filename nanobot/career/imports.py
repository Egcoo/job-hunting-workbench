"""Local PDF/DOCX ingestion with durable originals and reviewable, cited drafts."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from nanobot.career.models import (
    CareerConflictError,
    ResumeImport,
    ResumeImportError,
    ResumeSectionKey,
    ResumeSourceLine,
    ResumeSourceSection,
    ResumeVersion,
)
from nanobot.career.store import CareerStore
from nanobot.utils.document import (
    DocumentExtractionError,
    DocxSafetyError,
    PdfSafetyError,
    open_document_line_source,
)

MAX_RESUME_BYTES = 6 * 1024 * 1024
_MIMES = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_HEADINGS: dict[str, ResumeSectionKey] = {}
_SECTION_NAMES: dict[ResumeSectionKey, tuple[str, ...]] = {
    "education": ("教育背景", "教育经历", "education", "educationbackground"),
    "projects": ("项目经历", "项目经验", "项目实践", "projects", "projectexperience"),
    "skills": ("技能", "专业技能", "个人技能", "技能特长", "skills", "technicalskills"),
    "summary": ("个人评价", "自我评价", "个人简介", "summary", "profile"),
    "experience": ("工作经历", "实习经历", "工作经验", "experience", "workexperience", "internships"),
}
for _key, _names in _SECTION_NAMES.items():
    for _name in _names:
        _HEADINGS[_name] = _key


def validate_resume_upload(filename: str, mime: str, size: int) -> Literal["pdf", "docx"]:
    if (not filename or len(filename) > 255 or not filename.isprintable()
            or any(character in filename for character in '/\\:') or Path(filename).name != filename):
        raise ValueError("career_invalid_filename")
    suffix = Path(filename).suffix.lower().removeprefix(".")
    if suffix not in _MIMES or mime != _MIMES[suffix]:
        raise ValueError("career_unsupported_resume_format")
    if not 0 < size <= MAX_RESUME_BYTES:
        raise ValueError("career_resume_file_limit")
    return "pdf" if suffix == "pdf" else "docx"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _changed(record: ResumeImport, **updates: object) -> ResumeImport:
    values = record.model_dump()
    values.update(updates, updated_at=_now())
    return ResumeImport.model_validate(values)


def _sections(lines: tuple[ResumeSourceLine, ...]) -> tuple[ResumeSourceSection, ...]:
    groups: dict[str, list[int]] = {}
    current: str | None = None
    for line in lines:
        heading = re.sub(r"[\s:：·•▸]+", "", line.text).lower()
        known = _HEADINGS.get(heading)
        if known is not None:
            current = known
            groups.setdefault(current, [])
        elif current is not None:
            groups[current].append(line.number)
    return tuple(ResumeSourceSection.model_validate({"key": key, "line_numbers": numbers})
                 for key, numbers in groups.items() if numbers)


class ResumeImports:
    def __init__(self, store: CareerStore) -> None:
        self.store = store

    def get(self, import_id: str) -> ResumeImport:
        with self.store.transaction() as db:
            return self.store.resume_import(db, import_id)

    def for_request(self, request_id: str) -> ResumeImport | None:
        with self.store.transaction() as db:
            return self.store.import_for_request(db, request_id)

    def list(self) -> list[ResumeImport]:
        with self.store.transaction() as db:
            return self.store.resume_imports(db)

    def file_path(self, record: ResumeImport) -> Path:
        # Both components are server-created, validated IDs; no client path is accepted.
        directory = self.store.path.parent / "resume-files"
        path = directory / f"{record.id}.{record.file_type}"
        if directory.resolve() != directory or path.resolve() != path:
            raise ValueError("career_resume_path_redirected")
        return path

    def original(self, import_id: str) -> tuple[ResumeImport, bytes]:
        record = self.get(import_id)
        with self.file_path(record).open("rb") as handle:
            content = handle.read(MAX_RESUME_BYTES + 1)
        if len(content) != record.size or hashlib.sha256(content).hexdigest() != record.sha256:
            raise OSError("stored resume file failed its integrity check")
        return record, content

    def accept(
        self, source: Path, *, request_id: str, filename: str, mime: str, size: int, name: str,
    ) -> ResumeImport:
        """Accept only a server-owned, committed attachment, never a client filename as path."""
        kind = validate_resume_upload(filename, mime, size)
        if source.is_symlink():
            raise ValueError("career_resume_path_redirected")
        with source.open("rb") as handle:
            content = handle.read(MAX_RESUME_BYTES + 1)
        if len(content) != size:
            raise ValueError("career_resume_upload_changed")
        digest = hashlib.sha256(content).hexdigest()
        now = _now()
        with self.store.transaction(write=True) as db:
            existing = self.store.import_for_request(db, request_id)
            if existing is not None:
                if existing.sha256 != digest or existing.filename != filename or existing.name != name:
                    raise CareerConflictError("career_import_request_reused")
                return existing
            record = ResumeImport(
                id=f"import_{uuid4().hex}", request_id=request_id, filename=filename,
                file_type=kind, size=size, sha256=digest, name=name, created_at=now, updated_at=now,
            )
            self.store.add_resume_import(db, record)
        destination = self.file_path(record)
        temporary = destination.with_suffix(".part")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except OSError:
            return self._fail(record.id, "file_unavailable")
        finally:
            temporary.unlink(missing_ok=True)
        return self.process(record.id)

    def _fail(self, import_id: str, code: ResumeImportError) -> ResumeImport:
        with self.store.transaction(write=True) as db:
            current = self.store.resume_import(db, import_id)
            if current.status == "ready":
                return current
            record = _changed(current, status="failed", error_code=code)
            self.store.update_resume_import(db, record)
            return record

    def process(self, import_id: str) -> ResumeImport:
        with self.store.transaction(write=True) as db:
            current = self.store.resume_import(db, import_id)
            if current.status == "ready":
                return current
            self.store.update_resume_import(db, _changed(current, status="processing", error_code=None))
        try:
            record, _ = self.original(import_id)
            source = open_document_line_source(self.file_path(record))
            if source is None:
                return self._fail(import_id, "invalid_document")
            if source.continuation:
                return self._fail(import_id, "safety_limit")
            lines: list[ResumeSourceLine] = []
            length = 0
            for line in source.lines:
                if not line.searchable or not line.text.strip():
                    continue
                if len(lines) >= 10_000:
                    return self._fail(import_id, "safety_limit")
                length += len(line.text) + (1 if lines else 0)
                if length > 200_000:
                    return self._fail(import_id, "text_limit")
                lines.append(ResumeSourceLine(number=len(lines) + 1, locator=line.locator, text=line.text))
            text = "\n".join(line.text for line in lines)
            if not text.strip() or not any(character.isalnum() for character in text):
                return self._fail(import_id, "empty_document")
            sections = _sections(tuple(lines))
        except (DocxSafetyError, PdfSafetyError):
            return self._fail(import_id, "safety_limit")
        except DocumentExtractionError as error:
            from pypdf.errors import FileNotDecryptedError

            return self._fail(import_id, "encrypted" if isinstance(error.__cause__, FileNotDecryptedError) else "invalid_document")
        except OSError:
            return self._fail(import_id, "file_unavailable")
        except Exception:
            # Third-party parsers report format failures using several exception families.
            return self._fail(import_id, "invalid_document")
        with self.store.transaction(write=True) as db:
            current = self.store.resume_import(db, import_id)
            if current.status == "ready":
                return current
            now = _now()
            profile: dict[str, JsonValue] = {
                section.key: "\n".join(lines[number - 1].text for number in section.line_numbers)
                for section in sections
            }
            resume = ResumeVersion(
                id=f"resume_{uuid4().hex}", version=self.store.next_resume_version(db),
                name=current.name, source=current.file_type, import_id=current.id,
                text=text, profile=profile, created_at=now, updated_at=now,
            )
            record = _changed(current, status="ready", text=text, lines=tuple(lines), sections=sections,
                              error_code=None, resume_version_id=resume.id)
            self.store.add_resume(db, resume)
            self.store.update_resume_import(db, record)
            return record
