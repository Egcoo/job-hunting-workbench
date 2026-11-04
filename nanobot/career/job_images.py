"""Durable job screenshots and reviewed job versions, independent of model execution."""

from __future__ import annotations

import hashlib
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from pydantic import JsonValue

from nanobot.career.models import (
    CareerConflictError,
    ImageJobRequirements,
    JobImageError,
    JobImageImport,
    JobImageReading,
    JobVersion,
)
from nanobot.career.store import CareerStore

MAX_JOB_IMAGE_BYTES = 6 * 1024 * 1024
JOB_IMAGE_MIMES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def validate_job_image_upload(filename: str, mime: str, size: int) -> Literal["png", "jpg", "webp"]:
    if (not filename or len(filename) > 255 or not filename.isprintable()
            or any(character in filename for character in '/\\:') or Path(filename).name != filename):
        raise ValueError("career_invalid_filename")
    suffix = Path(filename).suffix.lower().removeprefix(".")
    if suffix == "jpeg":
        suffix = "jpg"
    if suffix not in JOB_IMAGE_MIMES or mime != JOB_IMAGE_MIMES[suffix] or not 0 < size <= MAX_JOB_IMAGE_BYTES:
        raise ValueError("career_invalid_job_image")
    return "png" if suffix == "png" else "webp" if suffix == "webp" else "jpg"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _changed(record: JobImageImport, **updates: object) -> JobImageImport:
    values = record.model_dump()
    values.update(updates, updated_at=_now())
    return JobImageImport.model_validate(values)


class JobImages:
    def __init__(self, store: CareerStore) -> None:
        self.store = store

    def get(self, import_id: str) -> JobImageImport:
        with self.store.transaction() as db:
            return self.store.job_image(db, import_id)

    def for_request(self, request_id: str) -> JobImageImport | None:
        with self.store.transaction() as db:
            return self.store.job_image_for_request(db, request_id)

    def list(self) -> list[JobImageImport]:
        with self.store.transaction() as db:
            return self.store.job_images(db)

    def file_path(self, record: JobImageImport) -> Path:
        directory = self.store.path.parent / "job-images"
        path = directory / f"{record.id}.{record.file_type}"
        if directory.resolve() != directory or path.resolve() != path:
            raise ValueError("career_job_image_redirected")
        return path

    def original(self, import_id: str) -> tuple[JobImageImport, bytes]:
        record = self.get(import_id)
        with self.file_path(record).open("rb") as handle:
            content = handle.read(MAX_JOB_IMAGE_BYTES + 1)
        if len(content) != record.size or hashlib.sha256(content).hexdigest() != record.sha256:
            raise OSError("stored job image failed its integrity check")
        return record, content

    def accept(self, source: Path, *, request_id: str, filename: str, mime: str, size: int) -> JobImageImport:
        kind = validate_job_image_upload(filename, mime, size)
        if source.is_symlink():
            raise ValueError("career_job_image_redirected")
        with source.open("rb") as handle:
            content = handle.read(MAX_JOB_IMAGE_BYTES + 1)
        if len(content) != size:
            raise ValueError("career_upload_changed")
        digest = hashlib.sha256(content).hexdigest()
        now = _now()
        with self.store.transaction(write=True) as db:
            existing = self.store.job_image_for_request(db, request_id)
            if existing is not None:
                if existing.sha256 != digest or existing.filename != filename:
                    raise CareerConflictError("career_image_request_reused")
                return existing
            record = JobImageImport(id=f"jobimage_{uuid4().hex}", request_id=request_id, filename=filename,
                                    file_type=kind, size=size, sha256=digest, created_at=now, updated_at=now)
            self.store.add_job_image(db, record)
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
            return self.fail(record.id, "file_unavailable")
        finally:
            temporary.unlink(missing_ok=True)
        return self.validate(record.id)

    def validate(self, import_id: str) -> JobImageImport:
        try:
            record, content = self.original(import_id)
        except OSError:
            return self.fail(import_id, "file_unavailable")
        try:
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width > 10_000 or height > 10_000 or width * height > 20_000_000:
                    return self.fail(import_id, "image_limit")
                expected = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[record.file_type]
                if image.format != expected or getattr(image, "is_animated", False):
                    return self.fail(import_id, "invalid_image")
                image.verify()
        except Image.DecompressionBombError:
            return self.fail(import_id, "image_limit")
        except (UnidentifiedImageError, SyntaxError, ValueError, OSError):
            return self.fail(import_id, "invalid_image")
        with self.store.transaction(write=True) as db:
            current = self.store.job_image(db, import_id)
            record = _changed(current, width=width, height=height)
            self.store.update_job_image(db, record)
            return record

    def begin(self, import_id: str, *, model: str, provider: str) -> JobImageImport:
        with self.store.transaction(write=True) as db:
            current = self.store.job_image(db, import_id)
            if current.status == "ready" or current.job_version_id is not None:
                return current
            record = _changed(current, status="processing", reading=None, error_code=None, model=model, provider=provider)
            self.store.update_job_image(db, record)
            return record

    def finish(self, import_id: str, reading: JobImageReading) -> JobImageImport:
        with self.store.transaction(write=True) as db:
            current = self.store.job_image(db, import_id)
            if current.status == "ready" or current.job_version_id is not None:
                return current
            record = _changed(current, reading=reading, status="ready" if reading.readable else "failed",
                              error_code=None if reading.readable else "unreadable")
            self.store.update_job_image(db, record)
            return record

    def fail(self, import_id: str, code: JobImageError) -> JobImageImport:
        with self.store.transaction(write=True) as db:
            current = self.store.job_image(db, import_id)
            if current.status == "ready" or current.job_version_id is not None:
                return current
            record = _changed(current, status="failed", error_code=code)
            self.store.update_job_image(db, record)
            return record

    def save_review(self, import_id: str, *, name: str, company: str | None, text: str,
                    requirements: dict[str, JsonValue], manual: bool) -> JobVersion:
        requirements = ImageJobRequirements.model_validate(requirements).values()
        with self.store.transaction(write=True) as db:
            current = self.store.job_image(db, import_id)
            if current.job_version_id is not None:
                saved = self.store.job(db, current.job_version_id)
                if (saved.name, saved.company, saved.text, saved.requirements) != (name, company, text, requirements):
                    raise CareerConflictError("career_image_already_saved")
                return saved
            if not manual and (current.status != "ready" or current.reading is None or not current.reading.readable):
                raise CareerConflictError("career_image_not_ready")
            now = _now()
            job = JobVersion(id=f"job_version_{uuid4().hex}", job_id=f"job_{uuid4().hex}", version=1,
                             name=name, company=company, text=text, requirements=requirements,
                             source="image", import_id=current.id, created_at=now, updated_at=now)
            self.store.add_job(db, job)
            self.store.update_job_image(db, _changed(current, job_version_id=job.id, manual=manual))
            return job
