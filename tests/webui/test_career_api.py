from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.outbound_events import (
    StreamDeltaEvent,
    StreamEndEvent,
    TurnEndEvent,
    TurnModelUpdatedEvent,
    outbound_message_for_event,
)
from nanobot.bus.queue import MessageBus
from nanobot.career.context import build_materials
from nanobot.career.models import TaskInput
from nanobot.career.store import CareerStore
from nanobot.channels.manager import ChannelManager
from nanobot.channels.websocket.attachment_store import AttachmentUploadError
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.runtime_context import RUNTIME_CONTEXT_INPUT_META
from nanobot.security.workspace_access import WORKSPACE_SCOPE_METADATA_KEY
from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import WebuiTurnCoordinator
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.webui.career_policy import career_discussion_policy
from nanobot.webui.career_tools import ProposePreparationPlanTool
from nanobot.webui.career_turns import CAREER_TURN_METADATA
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.job_image_recognition import JobImageRecognizer, image_challenge
from nanobot.webui.workspaces import write_webui_workspace_state
from scripts.career_browser_gateway import EngineeringProvider


@pytest.fixture
async def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    data.mkdir()
    config_path = data / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: data)
    workspace = tmp_path / "workspace"
    sessions = SessionManager(workspace, sessions_root=data / "sessions")
    services = build_gateway_services(
        config=WebSocketConfig(), bus=MessageBus(), session_manager=sessions,
        static_dist_path=None, workspace_path=workspace, default_restrict_to_workspace=True,
        config_path=config_path, runtime_model_name=lambda: None, runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    token = services.tokens.issue_api_token(60)
    connection = SimpleNamespace(
        remote_address=("127.0.0.1", 32100), request=Request("/", Headers()),
    )
    try:
        yield services, token, connection
    finally:
        await sessions.state.aclose()


def _body(response: Response) -> dict:
    return json.loads(bytes(response.body))


async def _get(gateway, path: str, *, authenticated: bool = True) -> Response:
    services, token, connection = gateway
    headers = Headers({"Authorization": f"Bearer {token}"}) if authenticated else Headers()
    return await services.http.dispatch(connection, Request(path, headers))


async def _mutate(gateway, action: str, **payload) -> Response:
    services, _, connection = gateway
    return await services.http.dispatch_webui_mutation(connection, action, payload)


async def _scope(gateway) -> str:
    response = await _get(gateway, "/api/career/workspaces")
    assert response.status_code == 200
    return _body(response)["workspaces"][0]["ref"]


class _UploadConnection:
    def __init__(self):
        self.remote_address = ("127.0.0.1", 32101)
        self.request = Request("/", Headers())


def _resume_document() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("教育背景")
    document.add_paragraph("工程测试大学 API_FILE_6")
    document.add_paragraph("项目经历")
    document.add_paragraph("测试原型项目，不包含真实个人资料。")
    target = BytesIO()
    document.save(target)
    return target.getvalue()


async def _staged_resume(services, connection, content: bytes, *, filename="resume.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document") -> str:
    if not services.uploads.owner(connection):
        services.uploads.issue(connection)

    async def chunks():
        yield content

    return await services.uploads.store.upload(chunks(), owner=services.uploads.owner(connection), mime=mime, size=len(content), filename=filename)


def _job_picture(kind="png") -> bytes:
    output = BytesIO()
    Image.new("RGB", (320, 180), (255, 230, 230)).save(output, format={"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[kind])
    return output.getvalue()


def _vision_reader(gateway, monkeypatch, provider=None):
    provider = provider or EngineeringProvider()
    runtime = LLMRuntime.capture(provider, "engineering-job-image", context_window_tokens=32_000)
    gateway[0].http.career.job_image_recognizer = JobImageRecognizer(lambda: runtime)
    monkeypatch.setattr("nanobot.webui.job_image_recognition._challenge", lambda: image_challenge("246810"))
    return provider


@pytest.mark.parametrize("kind", ["png", "jpg", "webp"])
async def test_job_screenshot_requires_owned_upload_and_explicit_save_and_uses_confirmed_corrections(gateway, monkeypatch, kind):
    _vision_reader(gateway, monkeypatch)
    services, token, _ = gateway
    connection = _UploadConnection()
    local = (services, token, connection)
    reference = await _scope(gateway)
    content = _job_picture(kind)
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[kind]
    attachment = await _staged_resume(services, connection, content, filename=f"job.{kind}", mime=mime)
    payload = dict(workspace_ref=reference, request_id=str(uuid4()), attachment_ref=attachment, filename=f"job.{kind}", size=len(content))
    reply = await _mutate(local, "career.job.image.import", **payload)
    assert reply.status_code == 200
    result = _body(reply)
    record = result["import"]
    assert record["status"] == "ready" and result["job"] is None
    assert record["reading"]["fields"]["days_per_week"]["value"] == 4
    assert _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))["jobs"] == []
    duplicate = await _staged_resume(services, connection, content, filename=f"job.{kind}", mime=mime)
    assert _body(await _mutate(local, "career.job.image.import", **{**payload, "attachment_ref": duplicate})) == result
    with pytest.raises(AttachmentUploadError):
        services.uploads.store.inspect([duplicate], owner=services.uploads.owner(connection))
    services.uploads.revoke(connection)
    assert _body(await _mutate(local, "career.job.image.import", **payload)) == result
    path = f"/api/career/job-image-file?workspace_ref={reference}&id={record['id']}"
    assert (await _get(gateway, path, authenticated=False)).status_code == 401
    original = await _get(gateway, path)
    assert original.status_code == 200 and bytes(original.body) == content
    saved = _body(await _mutate(gateway, "career.job.image.save", workspace_ref=reference, id=record["id"],
                                name="用户核对岗位", company=None, text=record["reading"]["text"], requirements={"days_per_week": 3}, manual=False))
    assert saved["status"] == "draft" and saved["source"] == "image" and saved["import_id"] == record["id"]
    resume = _body(await _mutate(gateway, "career.resume.create", workspace_ref=reference, name="工程简历", text="原型项目"))
    await _mutate(gateway, "career.resume.confirm", workspace_ref=reference, id=resume["id"])
    binding = dict(workspace_ref=reference, resume_version_id=resume["id"], job_version_id=saved["id"], conditions={"days_per_week": 2})
    assert (await _mutate(gateway, "career.thread.create", **binding)).status_code == 409
    await _mutate(gateway, "career.job.confirm", workspace_ref=reference, id=saved["id"])
    thread = _body(await _mutate(gateway, "career.thread.create", **binding))
    service, _ = services.http.career.bound_thread(reference, thread["id"])
    materials = build_materials(service.create_analysis(thread["id"]).snapshot)
    assert materials.payload["availability_conflicts"] == [{"field": "days_per_week", "available": 2, "required": 3}]
    detail = _body(await _get(gateway, f"/api/career/job-image?workspace_ref={reference}&id={record['id']}"))
    assert detail["reading"]["fields"]["days_per_week"]["value"] == 4


async def test_unavailable_vision_keeps_original_and_allows_explicit_manual_entry_without_faking_recognition(gateway):
    services, token, _ = gateway
    connection = _UploadConnection()
    local = (services, token, connection)
    reference = await _scope(gateway)
    content = _job_picture()
    attachment = await _staged_resume(services, connection, content, filename="job.png", mime="image/png")
    record = _body(await _mutate(local, "career.job.image.import", workspace_ref=reference,
                                request_id=str(uuid4()), attachment_ref=attachment, filename="job.png", size=len(content)))["import"]
    assert record["status"] == "failed" and record["error_code"] == "vision_unavailable" and record["reading"] is None
    fields = dict(workspace_ref=reference, id=record["id"], name="人工岗位", text="对照原图人工填写", requirements={"days_per_week": 4})
    assert (await _mutate(gateway, "career.job.image.save", **fields, manual=False)).status_code == 409
    saved = _body(await _mutate(gateway, "career.job.image.save", **fields, manual=True))
    retried = _body(await _mutate(gateway, "career.job.image.retry", workspace_ref=reference, id=record["id"]))
    assert retried["import"]["manual"] and retried["import"]["status"] == "failed" and retried["job"] == saved
    assert retried["import"]["reading"] is None


async def test_job_image_rejects_foreign_connections_paths_and_wrong_workspace_reads(gateway, tmp_path):
    services, token, _ = gateway
    owner, stranger = _UploadConnection(), _UploadConnection()
    reference = await _scope(gateway)
    content = _job_picture()
    attachment = await _staged_resume(services, owner, content, filename="job.png", mime="image/png")
    services.uploads.issue(stranger)
    payload = dict(workspace_ref=reference, request_id=str(uuid4()), attachment_ref=attachment, filename="job.png", size=len(content))
    assert (await _mutate((services, token, stranger), "career.job.image.import", **payload)).status_code == 400
    assert (await _mutate((services, token, owner), "career.job.image.import", **payload, path="C:/private/image.png")).status_code == 400
    result = _body(await _mutate((services, token, owner), "career.job.image.import", **payload))
    other = tmp_path / "other-image-workspace"
    other.mkdir()
    write_webui_workspace_state({"favorite_projects": [str(other)]})
    foreign = next(item["ref"] for item in _body(await _get(gateway, "/api/career/workspaces"))["workspaces"] if item["ref"] != reference)
    assert (await _get(gateway, f"/api/career/job-image-file?workspace_ref={foreign}&id={result['import']['id']}")).status_code == 404


async def test_slow_image_call_keeps_career_reads_available_and_cancellation_is_persisted(gateway, monkeypatch):
    class SlowVision(EngineeringProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat(self, messages, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().chat(messages, **kwargs)

    provider = SlowVision()
    _vision_reader(gateway, monkeypatch, provider)
    services, token, _ = gateway
    connection = _UploadConnection()
    reference = await _scope(gateway)
    content = _job_picture()
    attachment = await _staged_resume(services, connection, content, filename="slow.png", mime="image/png")
    running = asyncio.create_task(_mutate((services, token, connection), "career.job.image.import", workspace_ref=reference,
                                         request_id=str(uuid4()), attachment_ref=attachment, filename="slow.png", size=len(content)))
    await asyncio.wait_for(provider.started.wait(), 5)
    state = _body(await asyncio.wait_for(_get(gateway, f"/api/career?workspace_ref={reference}"), 3))
    assert state["job_images"][0]["status"] == "processing" and state["jobs"] == []
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert state["job_images"][0]["error_code"] == "interrupted" and state["jobs"] == []


async def test_import_uses_live_upload_owner_and_retains_original_after_staging_cleanup(gateway, tmp_path):
    services, token, _ = gateway
    connection = _UploadConnection()
    upload_gateway = (services, token, connection)
    reference = await _scope(gateway)
    content = _resume_document()
    attachment = await _staged_resume(services, connection, content)
    payload = dict(workspace_ref=reference, request_id=str(uuid4()), attachment_ref=attachment,
                   filename="resume.docx", size=len(content), name="工程文件简历")
    response = await _mutate(upload_gateway, "career.resume.import", **payload)
    assert response.status_code == 200
    result = _body(response)
    record, resume = result["import"], result["resume"]
    assert record["status"] == "ready" and resume["status"] == "draft"
    assert "API_FILE_6" in resume["text"] and resume["import_id"] == record["id"]
    services.uploads.revoke(connection)
    assert _body(await _mutate(upload_gateway, "career.resume.import", **payload)) == result
    source_path = f"/api/career/resume-import?workspace_ref={reference}&id={record['id']}"
    source = _body(await _get(gateway, source_path))
    assert source["text"] == resume["text"] and source["lines"][0]["locator"]
    download_path = f"/api/career/resume-file?workspace_ref={reference}&id={record['id']}"
    assert (await _get(gateway, download_path, authenticated=False)).status_code == 401
    downloaded = await _get(gateway, download_path)
    assert bytes(downloaded.body) == content
    assert downloaded.headers["X-Content-Type-Options"] == "nosniff"
    assert downloaded.headers["Cache-Control"] == "no-store"
    assert downloaded.headers["Content-Disposition"].startswith("attachment;")
    other = tmp_path / "other-file-workspace"
    other.mkdir()
    write_webui_workspace_state({"favorite_projects": [str(other)]})
    scopes = _body(await _get(gateway, "/api/career/workspaces"))["workspaces"]
    foreign = next(item["ref"] for item in scopes if item["ref"] != reference)
    assert (await _get(gateway, f"/api/career/resume-file?workspace_ref={foreign}&id={record['id']}")).status_code == 404
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert state["imports"][0]["id"] == record["id"] and len(state["resumes"]) == 1


async def test_import_rejects_foreign_upload_and_client_paths_without_consuming_the_valid_reference(gateway):
    services, token, _ = gateway
    owner, stranger = _UploadConnection(), _UploadConnection()
    reference = await _scope(gateway)
    content = _resume_document()
    attachment = await _staged_resume(services, owner, content)
    services.uploads.issue(stranger)
    payload = dict(workspace_ref=reference, request_id=str(uuid4()), attachment_ref=attachment,
                   filename="resume.docx", size=len(content), name="工程文件")
    assert (await _mutate((services, token, stranger), "career.resume.import", **payload)).status_code == 400
    assert (await _mutate((services, token, owner), "career.resume.import", **payload, path="C:/untrusted.docx")).status_code == 400
    assert (await _mutate((services, token, owner), "career.resume.import", **{**payload, "filename": "../escape.docx"})).status_code == 400
    response = await _mutate((services, token, owner), "career.resume.import", **payload)
    assert response.status_code == 200 and _body(response)["resume"]["status"] == "draft"


async def test_failed_import_is_visible_and_interrupted_transaction_can_resume_without_duplicate_versions(gateway, monkeypatch):
    services, token, _ = gateway
    connection = _UploadConnection()
    upload_gateway = (services, token, connection)
    reference = await _scope(gateway)
    corrupt = b"not-a-pdf"
    attachment = await _staged_resume(services, connection, corrupt, filename="broken.pdf", mime="application/pdf")
    broken = _body(await _mutate(upload_gateway, "career.resume.import", workspace_ref=reference,
                                request_id=str(uuid4()), attachment_ref=attachment, filename="broken.pdf", size=len(corrupt), name="损坏演示"))
    assert broken["import"]["status"] == "failed" and broken["resume"] is None
    retried = _body(await _mutate(gateway, "career.resume.import.retry", workspace_ref=reference, id=broken["import"]["id"]))
    assert retried["import"]["id"] == broken["import"]["id"] and retried["import"]["error_code"] == "invalid_document"
    assert retried["resume"] is None
    content = _resume_document()
    attachment = await _staged_resume(services, connection, content)
    update = CareerStore.update_resume_import

    def fail_ready(db, record):
        if record.status == "ready":
            raise OSError("engineering interrupted write")
        return update(db, record)

    monkeypatch.setattr(CareerStore, "update_resume_import", staticmethod(fail_ready))
    assert (await _mutate(upload_gateway, "career.resume.import", workspace_ref=reference,
                         request_id=str(uuid4()), attachment_ref=attachment, filename="resume.docx", size=len(content), name="可恢复导入")).status_code == 503
    monkeypatch.setattr(CareerStore, "update_resume_import", staticmethod(update))
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert state["resumes"] == []
    pending = next(item for item in state["imports"] if item["status"] == "processing")
    recovered = _body(await _mutate(gateway, "career.resume.import.retry", workspace_ref=reference, id=pending["id"]))
    assert recovered["resume"]["version"] == 1 and recovered["import"]["status"] == "ready"
    assert _body(await _mutate(gateway, "career.resume.import.retry", workspace_ref=reference, id=pending["id"])) == recovered


async def _confirmed_pair(gateway, reference: str) -> tuple[dict, dict]:
    resume = _body(await _mutate(
        gateway, "career.resume.create", workspace_ref=reference, name="工程简历", text="一次原型练习",
    ))
    job = _body(await _mutate(
        gateway, "career.job.create", workspace_ref=reference, name="工程岗位", text="参与原型设计",
    ))
    assert (await _mutate(gateway, "career.resume.confirm", workspace_ref=reference, id=resume["id"])).status_code == 200
    assert (await _mutate(gateway, "career.job.confirm", workspace_ref=reference, id=job["id"])).status_code == 200
    return resume, job


async def test_reads_require_current_auth_and_mutations_reject_http(gateway, tmp_path: Path):
    assert (await _get(gateway, "/api/career/workspaces", authenticated=False)).status_code == 401
    assert (await _get(gateway, "/api/career?workspace_ref=arbitrary", authenticated=False)).status_code == 401
    assert not (tmp_path / "data/career").exists()
    direct = await _get(gateway, "/api/career/mutate/career.resume.create")
    assert direct.status_code == 405
    assert not (tmp_path / "data/career").exists()
    reference = await _scope(gateway)
    response = await _get(gateway, f"/api/career?workspace_ref={reference}")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert _body(response)["resumes"] == []
    gateway[0].tokens.clear()
    assert (await _get(gateway, f"/api/career?workspace_ref={reference}")).status_code == 401


async def test_deleting_chat_retires_its_preparation_and_reentry_gets_a_fresh_chat(gateway):
    reference = await _scope(gateway)
    resume, job = await _confirmed_pair(gateway, reference)
    payload = dict(workspace_ref=reference, resume_version_id=resume["id"], job_version_id=job["id"])
    thread = _body(await _mutate(gateway, "career.thread.create", **payload))
    service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
    analysis = service.create_analysis(thread["id"])
    deleted = await _mutate(gateway, "session.delete", key=thread["session_key"])
    assert _body(deleted)["deleted"] is True
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert state["threads"] == [] and state["deleted_thread_ids"] == [thread["id"]]
    assert state["resumes"][0]["id"] == resume["id"] and state["jobs"][0]["id"] == job["id"]
    assert service.get_thread(thread["id"], include_deleted=True).deleted_at is not None
    assert service.get_analysis(analysis.id).status == "cancelled"
    assert (await _get(gateway, f"/api/career/conversation?workspace_ref={reference}&thread_id={thread['id']}")).status_code == 404
    fresh = _body(await _mutate(gateway, "career.thread.create", **payload))
    assert fresh["id"] != thread["id"] and fresh["session_key"] != thread["session_key"]
    assert _body(await _mutate(gateway, "career.thread.create", **payload)) == fresh


async def test_old_deleted_chat_is_reconciled_but_read_errors_do_not_retire_a_preparation(gateway, monkeypatch):
    reference = await _scope(gateway)
    resume, job = await _confirmed_pair(gateway, reference)
    thread = _body(await _mutate(gateway, "career.thread.create", workspace_ref=reference,
                                resume_version_id=resume["id"], job_version_id=job["id"]))
    service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
    reader = gateway[0].session_manager.read_session_metadata

    def unavailable(_key):
        raise OSError("temporary read failure")

    monkeypatch.setattr(gateway[0].session_manager, "read_session_metadata", unavailable)
    assert (await _get(gateway, f"/api/career?workspace_ref={reference}")).status_code == 503
    assert service.get_thread(thread["id"]).deleted_at is None
    monkeypatch.setattr(gateway[0].session_manager, "read_session_metadata", reader)
    await gateway[0].session_manager.state.delete(thread["session_key"])
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert state["threads"] == [] and state["deleted_thread_ids"] == [thread["id"]]
    assert service.get_thread(thread["id"], include_deleted=True).deleted_at is not None


async def test_failed_chat_deletion_keeps_the_preparation_active(gateway):
    reference = await _scope(gateway)
    resume, job = await _confirmed_pair(gateway, reference)
    thread = _body(await _mutate(gateway, "career.thread.create", workspace_ref=reference,
                                resume_version_id=resume["id"], job_version_id=job["id"]))

    async def failed_delete():
        raise OSError("temporary write failure")

    with pytest.raises(OSError):
        await gateway[0].http.career.delete_session(thread["session_key"], failed_delete)
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert state["threads"][0]["id"] == thread["id"] and state["deleted_thread_ids"] == []


async def test_state_read_waits_for_new_preparation_binding_before_reconciling_missing_sessions(gateway, monkeypatch):
    reference = await _scope(gateway)
    resume, job = await _confirmed_pair(gateway, reference)
    entered, release = asyncio.Event(), asyncio.Event()
    mutate = gateway[0].session_manager.state.mutate_metadata

    async def delayed_bind(*args, **kwargs):
        entered.set()
        await release.wait()
        return await mutate(*args, **kwargs)

    monkeypatch.setattr(gateway[0].session_manager.state, "mutate_metadata", delayed_bind)
    creation = asyncio.create_task(_mutate(gateway, "career.thread.create", workspace_ref=reference,
                                         resume_version_id=resume["id"], job_version_id=job["id"]))
    await asyncio.wait_for(entered.wait(), 5)
    read = asyncio.create_task(_get(gateway, f"/api/career?workspace_ref={reference}"))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(read), 0.05)
        release.set()
        thread = _body(await creation)
        state = _body(await read)
        assert state["threads"][0]["id"] == thread["id"] and state["deleted_thread_ids"] == []
    finally:
        release.set()
        await asyncio.gather(creation, read, return_exceptions=True)


class _CareerConnection:
    remote_address = ("127.0.0.1", 32001)
    request = Request("/", Headers())

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


async def _career_turn(gateway, *, unified=False, turn_id="career-turn", ordinary=False):
    services = replace(gateway[0], unified_session=unified)
    reference = await _scope(gateway)
    resume, job = await _confirmed_pair(gateway, reference)
    thread = _body(await _mutate(gateway, "career.thread.create", workspace_ref=reference,
                                resume_version_id=resume["id"], job_version_id=job["id"]))
    chat_id = "ordinary-question" if ordinary else thread["session_key"].split(":", 1)[1]
    channel = WebSocketChannel(WebSocketConfig(), services.http.bus, gateway=services)
    connection = _CareerConnection()
    channel._webui_connections.add(connection)
    await channel._dispatch_envelope(connection, "browser", {
        "type": "message", "chat_id": chat_id, "turn_id": turn_id, "webui": True,
        "content": "根据我的资料，应该先准备什么？",
    })
    message = await services.http.bus.consume_inbound()
    return channel, connection, message, reference, thread


def _plan_context(gateway, message, thread):
    version = gateway[0].session_manager.read_session_version(thread["session_key"])
    assert version is not None
    return RequestContext(channel="websocket", chat_id=message.chat_id, session_key=thread["session_key"],
                          session_generation=version[0], metadata=message.metadata,
                          workspace_scope=gateway[0].http.workspaces.default_scope())


def _suggested_tasks():
    return [TaskInput(title=f"任务 {index}", action="整理一页项目说明", completion_criteria="一页说明包含问题、方案和验证",
                      basis="简历中有原型练习，岗位要求原型设计。") for index in range(1, 4)]


async def test_plan_draft_edit_confirm_retry_and_task_progress_are_scoped_and_persistent(gateway):
    channel, connection, message, reference, thread = await _career_turn(gateway)
    try:
        tool = ProposePreparationPlanTool(gateway[0].http.career, gateway[0].http.bus)
        proposed = _suggested_tasks()
        with request_context(_plan_context(gateway, message, thread)):
            result = json.loads(await tool.execute(tasks=[item.model_dump() for item in proposed]))
            repeated = json.loads(await tool.execute(tasks=[item.model_dump() for item in proposed]))
        assert result["draft_id"] == repeated["draft_id"] and result["saved_as_tasks"] is False
        state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
        assert len(state["drafts"]) == 1 and state["tasks"] == []
        payload = dict(workspace_ref=reference, thread_id=thread["id"], id=result["draft_id"],
                       tasks=[{**proposed[0].model_dump(), "title": "用户编辑后的任务"}, proposed[2].model_dump()])
        assert (await _mutate(gateway, "career.plan.confirm", **payload)).status_code == 409
        service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
        analysis_id = message.metadata[CAREER_TURN_METADATA]["analysis_id"]
        service.update_analysis(analysis_id, status="succeeded", result="工程建议摘要")
        service.rename_resume(thread["resume_version_id"], name="后来的名称", notes="")
        confirmed = _body(await _mutate(gateway, "career.plan.confirm", **payload))
        assert confirmed["source"]["resume_name"] == "工程简历"
        assert confirmed["source"]["turn_id"] == "career-turn"
        tasks = service.list_tasks(confirmed["id"])
        assert [task.title for task in tasks] == ["用户编辑后的任务", "任务 3"]
        assert tasks[0].basis == proposed[0].basis
        update = dict(workspace_ref=reference, thread_id=thread["id"], id=tasks[0].id)
        assert (await _mutate(gateway, "career.task.update", **update, status="completed", result="完成结果")).status_code == 200
        other = _body(await _mutate(gateway, "career.thread.create", workspace_ref=reference,
                                   resume_version_id=thread["resume_version_id"], job_version_id=thread["job_version_id"], conditions={"days_per_week": 5}))
        assert (await _mutate(gateway, "career.task.update", workspace_ref=reference, thread_id=other["id"], id=tasks[0].id,
                              status="pending", result="不应写入的内容")).status_code == 409
        assert service.list_tasks(confirmed["id"])[0].result == "完成结果"
        await _mutate(gateway, "career.plan.confirm", **payload)
        assert [task.id for task in service.list_tasks(confirmed["id"])] == [task.id for task in tasks]
        assert service.list_tasks(confirmed["id"])[0].status == "completed"
        await _mutate(gateway, "career.task.update", **update, status="pending")
        assert service.list_tasks(confirmed["id"])[0].result == "完成结果"
        assert service.get_plan(confirmed["id"]).proposed_tasks == tuple(proposed)
        state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
        assert state["drafts"] == [] and len(state["tasks"]) == 2
    finally:
        await channel._cleanup_connection(connection)


async def test_plan_actions_reject_another_preparation_and_cancel_does_not_create_tasks(gateway):
    channel, connection, message, reference, thread = await _career_turn(gateway)
    try:
        proposal = await gateway[0].http.career.propose_plan_for_turn(_plan_context(gateway, message, thread), tuple(_suggested_tasks()))
        other = _body(await _mutate(gateway, "career.thread.create", workspace_ref=reference,
                                   resume_version_id=thread["resume_version_id"], job_version_id=thread["job_version_id"], conditions={"days_per_week": 5}))
        wrong = dict(workspace_ref=reference, thread_id=other["id"], id=proposal["id"])
        assert (await _mutate(gateway, "career.plan.cancel", **wrong)).status_code == 409
        assert (await _mutate(gateway, "career.plan.confirm", **wrong, tasks=[_suggested_tasks()[0].model_dump()])).status_code == 409
        payload = dict(workspace_ref=reference, thread_id=thread["id"], id=proposal["id"])
        assert _body(await _mutate(gateway, "career.plan.cancel", **payload))["status"] == "cancelled"
        assert _body(await _mutate(gateway, "career.plan.cancel", **payload))["status"] == "cancelled"
        service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
        assert service.list_tasks(str(proposal["id"])) == []
        assert (await _mutate(gateway, "career.plan.confirm", **payload, tasks=[_suggested_tasks()[0].model_dump()])).status_code == 409
    finally:
        await channel._cleanup_connection(connection)


async def test_model_tool_cannot_forge_scope_or_save_without_a_bound_current_generation(gateway):
    channel, connection, message, reference, thread = await _career_turn(gateway)
    try:
        tool = ProposePreparationPlanTool(gateway[0].http.career, gateway[0].http.bus)
        tasks = [item.model_dump() for item in _suggested_tasks()]
        context = _plan_context(gateway, message, thread)
        with request_context(replace(context, metadata={})):
            assert "No tasks were saved" in await tool.execute(tasks=tasks)
        with request_context(replace(context, session_generation="a-stale-generation")):
            assert "No tasks were saved" in await tool.execute(tasks=tasks)
        with request_context(context):
            assert "No tasks were saved" in await tool.execute(tasks=tasks, analysis_id="forged")
        state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
        assert state["drafts"] == [] and state["tasks"] == []
    finally:
        await channel._cleanup_connection(connection)


async def test_registered_plan_tool_runs_through_the_real_agent_and_only_creates_a_draft(gateway):
    channel, connection, message, reference, thread = await _career_turn(gateway)
    bus = gateway[0].http.bus

    class Provider(EngineeringProvider):
        async def chat(self, messages, **kwargs):
            if messages[-1]["role"] != "tool":
                return LLMResponse(content="", tool_calls=[ToolCallRequest("proposal", "propose_preparation_plan", {"tasks": [item.model_dump() for item in _suggested_tasks()]})], finish_reason="tool_calls")
            return LLMResponse(content="工程草稿已生成，请在界面核对并确认。", finish_reason="stop")

    tools = ToolRegistry()
    tools.register(ProposePreparationPlanTool(gateway[0].http.career, bus))
    loop = AgentLoop(bus, Provider(), gateway[0].session_manager.workspace, session_manager=gateway[0].session_manager,
                     tool_registry=tools, hook_factories=[career_discussion_policy], max_iterations=3)

    def schedule(awaitable):
        async def work():
            await awaitable
        loop.schedule_background(work())

    coordinator = WebuiTurnCoordinator(bus, gateway[0].session_manager, schedule)
    unsubscribe = coordinator.subscribe()

    async def deliver():
        while True:
            item = await bus.consume_outbound()
            try:
                await ChannelManager._send_once(channel, item)
            finally:
                bus.outbound.task_done()

    delivery = asyncio.create_task(deliver())
    try:
        await loop._enqueue_session_message(message)
        await asyncio.gather(*tuple(loop._active_tasks[loop._effective_session_key(message)]))
        await asyncio.wait_for(bus.outbound.join(), 10)
        state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
        assert len(state["drafts"]) == 1 and state["tasks"] == []
        assert state["drafts"][0]["source"]["analysis_status"] == "succeeded"
    finally:
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        unsubscribe()
        await loop.aclose()
        await channel._cleanup_connection(connection)


async def test_conversation_preview_is_authenticated_and_rejects_wrong_session_scope(gateway, tmp_path: Path):
    reference = await _scope(gateway)
    resume, job = await _confirmed_pair(gateway, reference)
    thread = _body(await _mutate(gateway, "career.thread.create", workspace_ref=reference,
                                resume_version_id=resume["id"], job_version_id=job["id"]))
    path = f"/api/career/conversation?workspace_ref={reference}&thread_id={thread['id']}"
    assert (await _get(gateway, path, authenticated=False)).status_code == 401
    response = await _get(gateway, path)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    preview = _body(response)
    assert preview["thread_id"] == thread["id"] and preview["analyses"] == []
    assert preview["preview"]["resume"]["text"] == resume["text"]
    await gateway[0].session_manager.state.update_metadata(thread["session_key"], {
        WORKSPACE_SCOPE_METADATA_KEY: {"project_path": str(tmp_path / "wrong"), "access_mode": "restricted"},
    })
    assert (await _get(gateway, path)).status_code == 409


@pytest.mark.parametrize("unified", [False, True])
async def test_career_message_has_bound_context_and_its_own_execution_session(gateway, unified):
    channel, connection, message, reference, thread = await _career_turn(gateway, unified=unified)
    try:
        assert message.session_key == thread["session_key"]
        assert message.session_key_override == thread["session_key"]
        blocks = message.metadata[RUNTIME_CONTEXT_INPUT_META]
        assert {block.source for block in blocks} >= {"career_guidance", "career_materials"}
        assert "一次原型练习" in "\n".join(block.content for block in blocks)
        source = message.metadata[CAREER_TURN_METADATA]
        service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
        analysis = service.get_analysis(source["analysis_id"])
        assert analysis.snapshot.turn_id == "career-turn"
        service.rename_resume(thread["resume_version_id"], name="修改后的名称", notes="")
        assert service.get_analysis(analysis.id).snapshot.resume.name == "工程简历"
    finally:
        await channel._cleanup_connection(connection)


async def test_ordinary_message_does_not_receive_career_material(gateway):
    channel, connection, message, _, _ = await _career_turn(gateway, ordinary=True)
    try:
        assert CAREER_TURN_METADATA not in message.metadata
        assert not any(block.source.startswith("career") for block in message.metadata.get(RUNTIME_CONTEXT_INPUT_META, []))
    finally:
        await channel._cleanup_connection(connection)


async def test_late_output_after_chat_deletion_cannot_restore_the_preparation_or_session(gateway):
    channel, connection, message, reference, thread = await _career_turn(gateway)
    service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
    analysis_id = message.metadata[CAREER_TURN_METADATA]["analysis_id"]

    async def deliver(event):
        await ChannelManager._send_once(channel, outbound_message_for_event(
            channel="websocket", chat_id=message.chat_id, event=event, metadata=message.metadata,
        ))

    try:
        await deliver(TurnModelUpdatedEvent(model="engineering"))
        assert _body(await _mutate(gateway, "session.delete", key=thread["session_key"]))["deleted"] is True
        await deliver(StreamDeltaEvent(content="迟到的片段", stream_id="late"))
        await deliver(StreamEndEvent(stream_id="late"))
        await deliver(TurnEndEvent(outcome="completed"))
        assert service.get_analysis(analysis_id).status == "cancelled"
        assert gateway[0].session_manager.read_session_metadata(thread["session_key"]) is None
        state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
        assert state["threads"] == [] and state["deleted_thread_ids"] == [thread["id"]]
    finally:
        await channel._cleanup_connection(connection)


async def test_stop_command_uses_the_career_session_even_when_ordinary_chat_is_unified(gateway):
    channel, connection, original, reference, thread = await _career_turn(gateway, unified=True)
    try:
        await channel._dispatch_envelope(connection, "browser", {
            "type": "message", "chat_id": original.chat_id, "turn_id": "stop-turn", "webui": True,
            "content": "/stop",
        })
        command = await gateway[0].http.bus.consume_inbound()
        assert command.session_key == thread["session_key"]
        assert CAREER_TURN_METADATA not in command.metadata
        service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
        assert len(service.list_analyses(thread["id"])) == 1
        assert service.list_analyses(thread["id"])[0].status == "cancelled"
    finally:
        await channel._cleanup_connection(connection)


async def test_immediate_stop_without_nonce_waits_for_the_career_question_admission(gateway, monkeypatch):
    channel, connection, message, reference, thread = await _career_turn(gateway)
    entered, release = asyncio.Event(), asyncio.Event()
    dispatch_order = []
    original = channel._commands._dispatch_message_once

    async def delayed_admission(connection, client_id, envelope):
        if envelope.get("turn_id") == "slow-admission":
            entered.set()
            await release.wait()
        dispatch_order.append(envelope["content"])
        await original(connection, client_id, envelope)

    monkeypatch.setattr(channel._commands, "_dispatch_message_once", delayed_admission)
    question = asyncio.create_task(channel._dispatch_envelope(connection, "browser", {
        "type": "message", "chat_id": message.chat_id, "turn_id": "slow-admission", "webui": True,
        "content": "第二个工程问题",
    }))
    await asyncio.wait_for(entered.wait(), 5)
    stop = asyncio.create_task(channel._dispatch_envelope(connection, "browser", {
        "type": "message", "chat_id": message.chat_id, "webui": True, "content": "/stop",
    }))
    try:
        # Give the stop coroutine a turn while the earlier question is held.
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(question, stop), 10)
        assert dispatch_order == ["第二个工程问题", "/stop"]
        assert (await gateway[0].http.bus.consume_inbound()).content == "第二个工程问题"
        assert (await gateway[0].http.bus.consume_inbound()).content == "/stop"
        service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
        assert {item.status for item in service.list_analyses(thread["id"])} == {"cancelled"}
    finally:
        release.set()
        await asyncio.gather(question, stop, return_exceptions=True)
        await channel._cleanup_connection(connection)


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_career_stream_records_final_result_or_failure_against_original_snapshot(gateway, outcome):
    channel, connection, message, reference, thread = await _career_turn(gateway)
    try:
        service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
        analysis_id = message.metadata[CAREER_TURN_METADATA]["analysis_id"]
        async def deliver(event):
            await ChannelManager._send_once(channel, outbound_message_for_event(
                channel="websocket", chat_id=message.chat_id, event=event, metadata=message.metadata,
            ))
        await deliver(StreamDeltaEvent(content="先整理", stream_id="one"))
        await deliver(StreamDeltaEvent(content="一个项目案例，并确认访谈经历。", stream_id="one"))
        await deliver(StreamEndEvent(stream_id="one"))
        await deliver(TurnEndEvent(outcome=outcome, failure_kind="model" if outcome == "failed" else None))
        stored = service.get_analysis(analysis_id)
        assert stored.status == {"completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}[outcome]
        assert stored.result == "先整理一个项目案例，并确认访谈经历。"
        assert stored.snapshot.turn_id == "career-turn"
        assert service.list_plans(thread["id"]) == []
        response = _body(await _get(gateway, f"/api/career/conversation?workspace_ref={reference}&thread_id={thread['id']}"))
        assert response["analyses"][0]["id"] == stored.id
        assert response["analyses"][0]["resume_name"] == "工程简历"
    finally:
        await channel._cleanup_connection(connection)


async def test_runtime_recovery_records_the_reply_using_the_failed_turn_original_snapshot(gateway):
    channel, connection, message, reference, thread = await _career_turn(gateway)
    try:
        service, _ = gateway[0].http.career.bound_thread(reference, thread["id"])
        analysis_id = message.metadata[CAREER_TURN_METADATA]["analysis_id"]
        async def deliver(event):
            await ChannelManager._send_once(channel, outbound_message_for_event(channel="websocket", chat_id=message.chat_id, event=event, metadata=message.metadata))
        await deliver(TurnEndEvent(outcome="failed", failure_kind="model"))
        original = service.get_analysis(analysis_id).snapshot
        service.rename_resume(thread["resume_version_id"], name="新名称", notes="")
        await deliver(TurnModelUpdatedEvent(model="engineering"))
        await deliver(StreamEndEvent(content="恢复后的工程回复"))
        await deliver(TurnEndEvent(outcome="completed"))
        recovered = service.get_analysis(analysis_id)
        assert recovered.status == "succeeded" and recovered.snapshot == original
        assert recovered.result == "恢复后的工程回复"
    finally:
        await channel._cleanup_connection(connection)


async def test_resume_ui_actions_save_draft_metadata_atomically_and_preserve_versions(gateway):
    reference = await _scope(gateway)
    first = _body(await _mutate(gateway, "career.resume.create", workspace_ref=reference, name="同名简历", text="旧正文"))
    second = _body(await _mutate(gateway, "career.resume.create", workspace_ref=reference, name="同名简历", text="另一个版本"))
    assert first["id"] != second["id"]
    edited = await _mutate(gateway, "career.resume.edit", workspace_ref=reference, id=first["id"], name="改名", notes="新备注", text="新正文")
    assert edited.status_code == 200
    assert (_body(edited)["name"], _body(edited)["notes"], _body(edited)["text"]) == ("改名", "新备注", "新正文")
    invalid = await _mutate(gateway, "career.resume.edit", workspace_ref=reference, id=first["id"], name="", text="不应保存")
    assert invalid.status_code == 400
    await _mutate(gateway, "career.resume.confirm", workspace_ref=reference, id=first["id"])
    denied = await _mutate(gateway, "career.resume.edit", workspace_ref=reference, id=first["id"], name="覆盖", text="不应覆盖")
    assert denied.status_code == 409
    revised = _body(await _mutate(gateway, "career.resume.revise", workspace_ref=reference, id=first["id"], name="另存", text="另存正文"))
    assert revised["parent_version_id"] == first["id"] and revised["status"] == "draft"
    renamed = await _mutate(gateway, "career.resume.rename", workspace_ref=reference, id=first["id"], name="归档", notes="已核对")
    assert renamed.status_code == 200 and _body(renamed)["text"] == "新正文"
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert len(state["resumes"]) == 3


async def test_job_revisions_can_clear_company_without_changing_old_record(gateway):
    reference = await _scope(gateway)
    first = _body(await _mutate(gateway, "career.job.create", workspace_ref=reference, name="实习", company="原公司", text="原要求"))
    edited = await _mutate(gateway, "career.job.edit", workspace_ref=reference, id=first["id"], name="实习", company="原公司", text="每周三天")
    assert edited.status_code == 200
    await _mutate(gateway, "career.job.confirm", workspace_ref=reference, id=first["id"])
    revised = _body(await _mutate(gateway, "career.job.revise", workspace_ref=reference, id=first["id"], name="新要求", company=None, text="每周四天"))
    assert revised["company"] is None
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    old = next(item for item in state["jobs"] if item["id"] == first["id"])
    assert old["company"] == "原公司" and old["text"] == "每周三天"


async def test_thread_creation_owns_real_session_and_repeated_entry_reuses_it(gateway):
    reference = await _scope(gateway)
    resume, job = await _confirmed_pair(gateway, reference)
    payload = dict(workspace_ref=reference, resume_version_id=resume["id"], job_version_id=job["id"], conditions={"days_per_week": 3})
    first = await _mutate(gateway, "career.thread.create", **payload)
    assert first.status_code == 200
    thread = _body(first)
    assert _body(await _mutate(gateway, "career.thread.create", **payload)) == thread
    metadata = await gateway[0].session_manager.state.read_metadata(thread["session_key"])
    assert metadata["metadata"]["career_thread_id"] == thread["id"]
    assert metadata["metadata"][WORKSPACE_SCOPE_METADATA_KEY]["project_path"] == str(gateway[0].session_manager.workspace)
    second = _body(await _mutate(gateway, "career.thread.create", **{**payload, "conditions": {"days_per_week": 4}}))
    assert second["id"] != thread["id"]
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert len(state["threads"]) == 2 and state["chat_available"][thread["id"]] is True
    await gateway[0].session_manager.state.update_metadata(thread["session_key"], {WORKSPACE_SCOPE_METADATA_KEY: {"project_path": str(gateway[0].session_manager.workspace), "access_mode": "full"}})
    assert _body(await _mutate(gateway, "career.thread.create", **payload)) == thread
    assert (await gateway[0].session_manager.state.read_metadata(thread["session_key"]))["metadata"][WORKSPACE_SCOPE_METADATA_KEY]["access_mode"] == "full"


async def test_client_cannot_supply_paths_session_keys_or_confirmation_state(gateway, tmp_path: Path):
    reference = await _scope(gateway)
    response = await _mutate(gateway, "career.resume.create", workspace_ref=reference, name="测试", text="正文", status="confirmed")
    assert response.status_code == 400
    response = await _mutate(gateway, "career.resume.create", workspace_ref=reference, name="测试", text="正文", workspace_path=str(tmp_path))
    assert response.status_code == 400
    resume, job = await _confirmed_pair(gateway, reference)
    response = await _mutate(gateway, "career.thread.create", workspace_ref=reference, resume_version_id=resume["id"], job_version_id=job["id"], session_key="websocket:unrelated")
    assert response.status_code == 400
    assert await gateway[0].session_manager.state.read_metadata("websocket:unrelated") is None
    assert (await _get(gateway, "/api/career?workspace_ref=../../other")).status_code == 404


async def test_server_known_workspaces_remain_isolated(gateway, tmp_path: Path):
    default = await _scope(gateway)
    project = tmp_path / "known-project"
    project.mkdir()
    write_webui_workspace_state({"favorite_projects": [str(project)]})
    catalog = _body(await _get(gateway, "/api/career/workspaces"))["workspaces"]
    other = next(item["ref"] for item in catalog if not item["is_default"])
    resume, job = await _confirmed_pair(gateway, default)
    state = _body(await _get(gateway, f"/api/career?workspace_ref={other}"))
    assert state["resumes"] == [] and state["threads"] == []
    denied = await _mutate(gateway, "career.thread.create", workspace_ref=other, resume_version_id=resume["id"], job_version_id=job["id"])
    assert denied.status_code == 404
    assert (await _mutate(gateway, "career.resume.confirm", workspace_ref=other, id=resume["id"])).status_code == 404


async def test_session_scope_mismatch_does_not_rebind_or_overwrite_history(gateway, tmp_path: Path):
    reference = await _scope(gateway)
    resume, job = await _confirmed_pair(gateway, reference)
    payload = dict(workspace_ref=reference, resume_version_id=resume["id"], job_version_id=job["id"])
    thread = _body(await _mutate(gateway, "career.thread.create", **payload))
    changed = {"project_path": str(tmp_path / "another-project"), "access_mode": "default"}
    await gateway[0].session_manager.state.update_metadata(thread["session_key"], {WORKSPACE_SCOPE_METADATA_KEY: changed})
    assert (await _mutate(gateway, "career.thread.create", **payload)).status_code == 409
    state = _body(await _get(gateway, f"/api/career?workspace_ref={reference}"))
    assert state["chat_available"][thread["id"]] is False
    metadata = await gateway[0].session_manager.state.read_metadata(thread["session_key"])
    assert metadata["metadata"][WORKSPACE_SCOPE_METADATA_KEY] == changed


async def test_paused_runtime_does_not_initialize_or_mutate_career_storage(gateway, tmp_path: Path):
    services, _, _ = gateway
    reference = await _scope(gateway)
    with services.http.bus.admission.pause_before_start():
        assert (await _get(gateway, f"/api/career?workspace_ref={reference}")).status_code == 503
        response = await _mutate(gateway, "career.resume.create", workspace_ref=reference, name="测试", text="正文")
        assert response.status_code == 503
    assert not (tmp_path / "data/career").exists()
