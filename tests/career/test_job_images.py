from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from nanobot.career.job_images import JobImages
from nanobot.career.models import CareerConflictError, JobImageReading
from nanobot.career.service import CareerService
from nanobot.career.store import CareerStore


def reading_payload() -> dict:
    return {
        "readable": True, "text": "图像岗位 JOB_IMG_A\n工程公司\n上海\n4天/周 6个月\n职责：设计原型\n要求：用户调研",
        "fields": {
            "name": {"value": "图像岗位 JOB_IMG_A", "evidence": "图像岗位 JOB_IMG_A"},
            "company": {"value": "工程公司", "evidence": "工程公司"},
            "locations": [{"value": "上海", "evidence": "上海"}],
            "responsibilities": [{"value": "设计原型", "evidence": "职责：设计原型"}],
            "qualifications": [{"value": "用户调研", "evidence": "要求：用户调研"}],
            "days_per_week": {"value": 4, "evidence": "4天/周"},
            "available_months": {"value": 6, "evidence": "6个月"},
        }, "warnings": [],
    }


def image_bytes(kind="png") -> bytes:
    image = Image.new("RGB", (480, 180), (255, 230, 230))
    ImageDraw.Draw(image).text((20, 30), "ENGINEERING JOB_IMG_A | 4 days/week | 6 months", fill="black")
    output = BytesIO()
    image.save(output, format={"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[kind])
    return output.getvalue()


def accept(images: JobImages, tmp_path: Path, *, kind="png", content=None, request_id=None):
    content = image_bytes(kind) if content is None else content
    source = tmp_path / f"upload-{uuid4().hex}.{kind}"
    source.write_bytes(content)
    return images.accept(source, request_id=request_id or str(uuid4()), filename=f"同名岗位.{kind}",
                         mime={"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[kind], size=len(content))


@pytest.fixture
def images(tmp_path):
    return JobImages(CareerStore(tmp_path / "workspace", data_dir=tmp_path / "data"))


@pytest.mark.parametrize("kind", ["png", "jpg", "webp"])
def test_image_formats_preserve_original_and_only_user_save_creates_a_draft(images, tmp_path, kind):
    content = image_bytes(kind)
    record = accept(images, tmp_path, kind=kind, content=content)
    assert (record.width, record.height) == (480, 180)
    service = CareerService(images.store)
    reading = JobImageReading.model_validate(reading_payload())
    ready = images.finish(record.id, reading)
    assert ready.status == "ready" and ready.job_version_id is None and service.list_jobs() == []
    requirements = {**reading.fields.requirements(), "days_per_week": 3}
    job = images.save_review(record.id, name="用户核对岗位", company="工程公司", text=reading.text + "\n用户核对", requirements=requirements, manual=False)
    assert job.source == "image" and job.import_id == record.id and job.status == "draft"
    assert job.requirements["days_per_week"] == 3
    assert images.get(record.id).reading == reading
    assert images.original(record.id)[1] == content
    resume = service.create_resume(name="工程简历", text="原型项目")
    service.confirm_resume(resume.id)
    with pytest.raises(CareerConflictError, match="confirmed"):
        service.create_thread(session_key="career:image-draft", resume_version_id=resume.id, job_version_id=job.id)
    confirmed = service.confirm_job(job.id)
    revised = service.revise_job(job.id, text="修订岗位", requirements={"days_per_week": 5})
    assert revised.source == "image" and revised.import_id == record.id and revised.version == 2
    assert service.get_job(job.id) == confirmed
    restored = JobImages(CareerStore(tmp_path / "workspace", data_dir=tmp_path / "data"))
    assert restored.original(record.id)[1] == content and restored.get(record.id).job_version_id == job.id


def test_saved_manual_job_does_not_become_fake_recognition_or_get_overwritten_by_a_late_result(images, tmp_path):
    record = accept(images, tmp_path)
    images.fail(record.id, "vision_unavailable")
    job = images.save_review(record.id, name="人工岗位", company=None, text="用户对照原图填写", requirements={"days_per_week": 4}, manual=True)
    late = images.finish(record.id, JobImageReading.model_validate(reading_payload()))
    assert late.manual and late.status == "failed" and late.reading is None and late.error_code == "vision_unavailable"
    assert CareerService(images.store).get_job(job.id).text == "用户对照原图填写"
    assert images.save_review(record.id, name=job.name, company=None, text=job.text, requirements=job.requirements, manual=True) == job
    with pytest.raises(CareerConflictError, match="already_saved"):
        images.save_review(record.id, name="迟到修改", company=None, text="另一段文字", requirements={}, manual=True)


def test_same_upload_request_reuses_its_record_and_failed_images_never_create_jobs(images, tmp_path):
    request_id = str(uuid4())
    content = image_bytes()
    first = accept(images, tmp_path, content=content, request_id=request_id)
    assert accept(images, tmp_path, content=content, request_id=request_id) == first
    bad = accept(images, tmp_path, content=b"broken image")
    assert bad.status == "failed" and bad.error_code == "invalid_image"
    assert images.original(bad.id)[1] == b"broken image"
    assert CareerService(images.store).list_jobs() == []


def test_unknown_fields_stay_empty_and_numeric_evidence_cannot_be_silently_changed():
    unknown = JobImageReading(readable=True, text="只能看见部分岗位说明")
    assert unknown.fields.name is None and unknown.fields.days_per_week is None
    wrong = reading_payload()
    wrong["fields"]["days_per_week"]["value"] = 5
    with pytest.raises(ValidationError, match="explicit matching"):
        JobImageReading.model_validate(wrong)
    invented = reading_payload()
    invented["fields"]["company"]["value"] = "未出现的公司"
    with pytest.raises(ValidationError, match="without inventing"):
        JobImageReading.model_validate(invented)
    chinese = JobImageReading(readable=True, text="每周四天，连续三个月", fields={
        "days_per_week": {"value": 4, "evidence": "每周四天"},
        "available_months": {"value": 3, "evidence": "三个月"},
    })
    assert chinese.fields.available_months.value == 3


def test_image_limits_and_corrupt_bodies_keep_failure_evidence(images, tmp_path):
    target = Image.new("RGB", (10_001, 1), "white")
    buffer = BytesIO()
    target.save(buffer, format="PNG")
    record = accept(images, tmp_path, content=buffer.getvalue())
    assert record.status == "failed" and record.error_code == "image_limit"
    assert images.get(record.id).job_version_id is None
