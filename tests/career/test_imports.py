from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from docx import Document
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from nanobot.career.imports import MAX_RESUME_BYTES, ResumeImports, validate_resume_upload
from nanobot.career.models import CareerConflictError
from nanobot.career.service import CareerService
from nanobot.career.store import CareerStore


def pdf_bytes(*, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    contents = DecodedStreamObject()
    contents.set_data(b'BT /F1 12 Tf 40 780 Td (EDUCATION) Tj 0 -20 Td (Engineering CV_PDF_6) Tj 0 -20 Td (PROJECTS) Tj 0 -20 Td (Built a local prototype.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(contents)
    if encrypted:
        writer.encrypt('engineering-test-password')
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def scanned_pdf_bytes() -> bytes:
    image = Image.new('RGB', (420, 100), 'white')
    ImageDraw.Draw(image).text((20, 30), 'Engineering scanned resume: image-only text.', fill='black')
    output = BytesIO()
    image.save(output, format='PDF')
    return output.getvalue()


def docx_bytes(text: str = '工程测试项目 CV_DOCX_6') -> bytes:
    document = Document()
    document.add_heading('教育背景', level=1)
    document.add_paragraph('工程测试大学；未提供到岗条件。')
    document.add_heading('项目经历', level=1)
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = text
    table.cell(0, 1).text = '完成一份原型；原文没有用户访谈经历。'
    document.add_heading('专业技能', level=1)
    document.add_paragraph('Python、Excel')
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def accept(imports: ResumeImports, tmp_path: Path, content: bytes, *, kind: str = 'docx', request_id: str | None = None):
    path = tmp_path / f'upload-{uuid4().hex}.{kind}'
    path.write_bytes(content)
    return imports.accept(path, request_id=request_id or str(uuid4()), filename=f'同名简历.{kind}',
                          mime='application/pdf' if kind == 'pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                          size=len(content), name='同名简历')


@pytest.fixture
def imports(tmp_path: Path) -> ResumeImports:
    return ResumeImports(CareerStore(tmp_path / 'workspace', data_dir=tmp_path / 'data'))


@pytest.mark.parametrize('kind', ['pdf', 'docx'])
def test_real_document_creates_only_a_cited_draft_and_retains_original(imports, tmp_path, kind):
    content = pdf_bytes() if kind == 'pdf' else docx_bytes()
    record = accept(imports, tmp_path, content, kind=kind)
    assert record.status == 'ready' and record.error_code is None
    service = CareerService(imports.store)
    resume = service.get_resume(record.resume_version_id)
    assert resume.status == 'draft' and resume.confirmed_at is None
    assert resume.source == kind and resume.import_id == record.id
    assert ('CV_PDF_6' if kind == 'pdf' else 'CV_DOCX_6') in resume.text
    assert resume.text == record.text
    assert record.lines and all(line.locator for line in record.lines)
    assert {'education', 'projects'} <= resume.profile.keys()
    assert imports.original(record.id)[1] == content
    assert record.sha256 == hashlib.sha256(content).hexdigest()
    job = service.create_job(name='测试岗位', text='原型设计')
    service.confirm_job(job.id)
    with pytest.raises(CareerConflictError, match='confirmed'):
        service.create_thread(session_key='test:unconfirmed-file', resume_version_id=resume.id, job_version_id=job.id)


def test_review_new_version_and_restart_keep_original_evidence_without_changing_old_facts(imports, tmp_path):
    record = accept(imports, tmp_path, docx_bytes())
    service = CareerService(imports.store)
    original_text = record.text
    corrected = service.edit_resume_draft(record.resume_version_id, text=original_text + '\n用户补充：仅能每周三天。', profile={'skills': '用户核对：Python'})
    confirmed = service.confirm_resume(corrected.id)
    with pytest.raises(CareerConflictError, match='immutable'):
        service.edit_resume_draft(confirmed.id, text='不能覆盖确认内容')
    revised = service.revise_resume(confirmed.id, text='另存修订草稿')
    assert revised.import_id == record.id and revised.source == 'docx'
    restored = ResumeImports(CareerStore(tmp_path / 'workspace', data_dir=tmp_path / 'data'))
    assert restored.get(record.id).text == original_text
    assert CareerService(restored.store).get_resume(confirmed.id) == confirmed
    assert restored.original(record.id)[1] == imports.original(record.id)[1]


def test_same_request_is_idempotent_but_same_filename_can_be_another_version(imports, tmp_path):
    request_id = str(uuid4())
    content = docx_bytes()
    first = accept(imports, tmp_path, content, request_id=request_id)
    assert accept(imports, tmp_path, content, request_id=request_id) == first
    assert imports.process(first.id) == first
    second = accept(imports, tmp_path, docx_bytes('新的工程项目 CV_NEW_6'))
    resumes = CareerService(imports.store).list_resumes()
    assert [resume.version for resume in resumes] == [1, 2]
    assert second.resume_version_id != first.resume_version_id
    with pytest.raises(CareerConflictError, match='reused'):
        accept(imports, tmp_path, docx_bytes('另一份内容'), request_id=request_id)


@pytest.mark.parametrize(('content', 'kind', 'code'), [
    (b'broken-document', 'docx', 'invalid_document'),
    (b'broken-pdf', 'pdf', 'invalid_document'),
    (scanned_pdf_bytes(), 'pdf', 'empty_document'),
    (pdf_bytes(encrypted=True), 'pdf', 'encrypted'),
    (docx_bytes('超长' * 110_000), 'docx', 'text_limit'),
], ids=['corrupt_docx', 'corrupt_pdf', 'scan_pdf', 'encrypted_pdf', 'large_text'])
def test_failed_parse_is_persisted_and_retry_never_creates_placeholder_resume(imports, tmp_path, content, kind, code):
    record = accept(imports, tmp_path, content, kind=kind)
    assert record.status == 'failed' and record.error_code == code
    assert record.resume_version_id is None
    assert imports.process(record.id).error_code == code
    assert CareerService(imports.store).list_resumes() == []
    assert imports.original(record.id)[1] == content


@pytest.mark.parametrize(('filename', 'mime', 'size'), [
    ('../resume.pdf', 'application/pdf', 10),
    ('resume.pdf', 'text/html', 10),
    ('resume.doc', 'application/msword', 10),
    ('resume.pdf', 'application/pdf', 0),
    ('resume.pdf', 'application/pdf', MAX_RESUME_BYTES + 1),
])
def test_upload_boundary_rejects_unsupported_metadata(filename, mime, size):
    with pytest.raises(ValueError):
        validate_resume_upload(filename, mime, size)


def test_line_limit_keeps_large_document_out_of_the_review_ui(imports, tmp_path):
    document = Document()
    for _ in range(10_001):
        document.add_paragraph('工程测试行')
    output = BytesIO()
    document.save(output)
    record = accept(imports, tmp_path, output.getvalue())
    assert record.status == 'failed' and record.error_code == 'safety_limit'
    assert record.resume_version_id is None
    assert CareerService(imports.store).list_resumes() == []
