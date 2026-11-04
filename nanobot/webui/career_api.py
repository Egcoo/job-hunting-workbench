"""Authenticated career transport and gateway-owned screenshot recognition coordination."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from nanobot.agent.tools.context import RequestContext
from nanobot.career.context import redact
from nanobot.career.imports import ResumeImports, validate_resume_upload
from nanobot.career.job_images import JobImages, validate_job_image_upload
from nanobot.career.models import (
    CareerConflictError,
    CareerNotFoundError,
    CareerThread,
    JobImageImport,
    PersonalConditions,
    PreparationPlan,
    ResumeImport,
    ShortText,
    TaskList,
    TaskStatus,
)
from nanobot.career.service import CareerService
from nanobot.career.store import CareerStore
from nanobot.channels.websocket.attachment_store import AttachmentStore
from nanobot.security.workspace_access import (
    WORKSPACE_SCOPE_METADATA_KEY,
    WorkspaceScope,
    build_workspace_scope,
)
from nanobot.session.manager import SessionManager
from nanobot.utils.cancellation import shield_and_drain
from nanobot.webui.job_image_recognition import JobImageRecognizer, JobVisionError
from nanobot.webui.metadata import CAREER_TURN_METADATA, WEBUI_TURN_METADATA_KEY
from nanobot.webui.workspaces import WebUIWorkspaceController, read_webui_workspace_state

CAREER_MUTATIONS = frozenset({
    "career.resume.create", "career.resume.edit", "career.resume.revise",
    "career.resume.rename", "career.resume.confirm",
    "career.resume.import", "career.resume.import.retry",
    "career.job.create", "career.job.edit", "career.job.revise", "career.job.confirm",
    "career.job.image.import", "career.job.image.retry", "career.job.image.save",
    "career.thread.create",
    "career.plan.confirm", "career.plan.cancel", "career.task.update",
})


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_ref: ShortText


class _VersionRequest(_Request):
    id: ShortText


class _ResumeWrite(_Request):
    id: ShortText | None = None
    name: ShortText
    notes: Annotated[str, Field(max_length=5_000)] = ""
    text: Annotated[str, Field(min_length=1, max_length=200_000)]
    profile: dict[str, JsonValue] = Field(default_factory=dict)


class _ResumeRename(_VersionRequest):
    name: ShortText
    notes: Annotated[str, Field(max_length=5_000)] = ""


class _ResumeImport(_Request):
    request_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
    attachment_ref: ShortText
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    size: Annotated[int, Field(strict=True, ge=1, le=6 * 1024 * 1024)]
    name: ShortText


class _JobWrite(_Request):
    id: ShortText | None = None
    name: ShortText
    company: ShortText | None = None
    text: Annotated[str, Field(min_length=1, max_length=200_000)]
    requirements: dict[str, JsonValue] = Field(default_factory=dict)


class _JobImageUpload(_Request):
    request_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
    attachment_ref: ShortText
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    size: Annotated[int, Field(strict=True, ge=1, le=6 * 1024 * 1024)]


class _JobImageSave(_Request):
    id: ShortText
    manual: Annotated[bool, Field(strict=True)] = False
    name: ShortText
    company: ShortText | None = None
    text: Annotated[str, Field(min_length=1, max_length=200_000)]
    requirements: dict[str, JsonValue] = Field(default_factory=dict)


class _ThreadCreate(_Request):
    resume_version_id: ShortText
    job_version_id: ShortText
    conditions: PersonalConditions = Field(default_factory=PersonalConditions)


class _PlanBinding(_Request):
    thread_id: ShortText
    analysis_id: ShortText


class _PlanAction(_VersionRequest):
    thread_id: ShortText


class _PlanConfirm(_PlanAction):
    tasks: TaskList


class _TaskUpdate(_PlanAction):
    status: TaskStatus
    result: Annotated[str, Field(max_length=20_000)] | None = None


def _scope_ref(scope: WorkspaceScope) -> str:
    canonical = os.path.normcase(str(scope.project_path.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _matches_session(metadata: dict[str, object], thread_id: str, scope: WorkspaceScope) -> bool:
    raw_scope = metadata.get(WORKSPACE_SCOPE_METADATA_KEY)
    if not isinstance(raw_scope, dict):
        return False
    fields = cast(dict[object, object], raw_scope)
    return metadata.get("career_thread_id") == thread_id and fields.get("project_path") == str(scope.project_path)


class CareerAPI:
    """Resolve opaque references from server-owned project selections on every request.

    Construction is lazy: protected gateway startup must not create a career database.
    Callers authenticate and enter runtime admission before invoking this adapter.
    """

    def __init__(
        self, *, sessions: SessionManager | None, workspaces: WebUIWorkspaceController,
        data_dir: Path,
    ) -> None:
        self.sessions = sessions
        self.workspaces = workspaces
        self.data_dir = data_dir
        self._lifecycle_lock = asyncio.Lock()
        self._image_lock = asyncio.Lock()
        self.job_image_recognizer: JobImageRecognizer | None = None

    def _scopes(self) -> dict[str, WorkspaceScope]:
        default = self.workspaces.default_scope()
        scopes = {_scope_ref(default): default}
        state = read_webui_workspace_state()
        for path in (*state["favorite_projects"], *state["recent_projects"]):
            project = Path(path)
            if project.is_dir():
                scope = build_workspace_scope(project, default.access_mode, source_channel="websocket")
                scopes.setdefault(_scope_ref(scope), scope)
        if self.sessions is not None:
            for row in self.sessions.list_session_metadata():
                if not str(row["key"]).startswith("websocket:"):
                    continue
                metadata = row["metadata"]
                scope = self.workspaces.scope_for_indexed_metadata(
                    metadata.get(WORKSPACE_SCOPE_METADATA_KEY),
                    scope_present=WORKSPACE_SCOPE_METADATA_KEY in metadata, default_scope=default,
                )
                scopes.setdefault(_scope_ref(scope), scope)
        return scopes

    def _resolve(self, reference: str) -> tuple[CareerService, WorkspaceScope]:
        if self.sessions is None:
            raise CareerConflictError("career_unavailable")

        scope = self._scopes().get(reference)
        if scope is None:
            raise CareerNotFoundError("career_workspace_not_found")
        return CareerService(CareerStore(scope.project_path, data_dir=self.data_dir)), scope

    def catalog(self) -> dict[str, object]:
        default_ref = _scope_ref(self.workspaces.default_scope())
        return {"workspaces": [
            {"ref": ref, "name": scope.project_name, "is_default": ref == default_ref}
            for ref, scope in self._scopes().items()
        ]}

    def session_binding(
        self, chat_id: str, scope: WorkspaceScope,
    ) -> tuple[CareerService, CareerThread, str] | None:
        if self.sessions is None:
            return None
        session_key = f"websocket:{chat_id}"
        stored = self.sessions.read_session_metadata(session_key)
        metadata: dict[str, object] = stored["metadata"] if stored is not None else {}
        if "career_thread_id" not in metadata:
            if chat_id.startswith("career-"):
                raise CareerConflictError("career_session_conflict")
            return None
        thread_id = metadata["career_thread_id"]
        if not isinstance(thread_id, str) or not thread_id:
            raise CareerConflictError("career_session_conflict")
        reference = _scope_ref(scope)
        service, _ = self._resolve(reference)
        thread = service.get_thread(thread_id)
        if thread.session_key != session_key or not _matches_session(metadata, thread.id, scope):
            raise CareerConflictError("career_session_conflict")
        return service, thread, reference

    def bound_thread(self, reference: str, thread_id: str) -> tuple[CareerService, CareerThread]:
        service, scope = self._resolve(reference)
        thread = service.get_thread(thread_id)
        chat_id = thread.session_key.removeprefix("websocket:")
        binding = self.session_binding(chat_id, scope)
        if binding is None or binding[1].id != thread.id:
            raise CareerConflictError("career_session_conflict")
        return service, thread

    def _retire_missing_threads(self, service: CareerService) -> None:
        assert self.sessions is not None
        # Read every session before writing. A storage error or a mismatched
        # binding is not evidence of deletion. Serialize this with chat creation.
        missing = [thread.id for thread in service.list_threads()
                   if thread.session_key.startswith("websocket:career-")
                   and self.sessions.read_session_metadata(thread.session_key) is None]
        if missing:
            service.retire_threads(missing)

    async def state(self, reference: str) -> dict[str, object]:
        async with self._lifecycle_lock:
            return await asyncio.to_thread(self._state, reference)

    def _state(self, reference: str) -> dict[str, object]:
        service, scope = self._resolve(reference)
        self._retire_missing_threads(service)
        threads = service.list_threads()
        all_plans = [plan for thread in threads for plan in service.list_plans(thread.id)]
        plans = [plan for plan in all_plans if plan.status == "confirmed"]
        tasks = [task for plan in plans for task in service.list_tasks(plan.id)]
        availability: dict[str, bool] = {}
        for thread in threads:
            payload = self.sessions.read_session_metadata(thread.session_key) if self.sessions else None
            metadata: dict[str, object] = payload["metadata"] if payload is not None else {}
            availability[thread.id] = _matches_session(metadata, thread.id, scope)
        return {
            "workspace_ref": reference, "workspace_id": service.store.workspace_id,
            "resumes": [item.model_dump(mode="json") for item in service.list_resumes()],
            "imports": [self._import_payload(item) for item in ResumeImports(service.store).list()],
            "jobs": [item.model_dump(mode="json") for item in service.list_jobs()],
            "job_images": [self._job_image_payload(item) for item in JobImages(service.store).list()],
            "job_image_recognition_available": self.job_image_recognizer is not None,
            "threads": [item.model_dump(mode="json") for item in threads],
            "deleted_thread_ids": [item.id for item in service.list_threads(include_deleted=True) if item.deleted_at is not None],
            "plans": [self._plan_payload(service, item) for item in plans],
            "drafts": [self._plan_payload(service, item) for item in all_plans if item.status == "draft"],
            "tasks": [item.model_dump(mode="json") for item in tasks],
            "chat_available": availability,
        }

    @staticmethod
    def _import_payload(record: ResumeImport) -> dict[str, object]:
        return record.model_dump(mode="json", exclude={"text", "lines", "sections", "request_id", "sha256"})

    def resume_import(self, reference: str, import_id: str) -> dict[str, object]:
        service, _ = self._resolve(reference)
        record = ResumeImports(service.store).get(import_id)
        return {**self._import_payload(record), "text": record.text,
                "lines": [line.model_dump() for line in record.lines],
                "sections": [section.model_dump() for section in record.sections]}

    def resume_file(self, reference: str, import_id: str) -> tuple[ResumeImport, bytes]:
        service, _ = self._resolve(reference)
        return ResumeImports(service.store).original(import_id)

    def _import_result(self, service: CareerService, record: ResumeImport) -> dict[str, object]:
        resume = service.get_resume(record.resume_version_id) if record.resume_version_id is not None else None
        return {"import": self._import_payload(record), "resume": resume.model_dump(mode="json") if resume else None}

    async def _import_resume(
        self, action: str, payload: dict[str, object], uploads: AttachmentStore | None, owner: str,
    ) -> dict[str, object]:
        async with self._lifecycle_lock:
            if action == "career.resume.import.retry":
                retry = _VersionRequest.model_validate(payload)
                service, _ = await asyncio.to_thread(self._resolve, retry.workspace_ref)
                record = await asyncio.to_thread(ResumeImports(service.store).process, retry.id)
                return await asyncio.to_thread(self._import_result, service, record)
            request = _ResumeImport.model_validate(payload)
            service, _ = await asyncio.to_thread(self._resolve, request.workspace_ref)
            imports = ResumeImports(service.store)
            existing = await asyncio.to_thread(imports.for_request, request.request_id)
            if existing is not None:
                if (existing.filename, existing.name, existing.size) != (request.filename, request.name, request.size):
                    raise CareerConflictError("career_import_request_reused")
                return await asyncio.to_thread(self._import_result, service, existing)
            if uploads is None or not owner:
                raise ValueError("career_import_requires_upload_connection")
            entry = uploads.inspect([request.attachment_ref], owner=owner)[0]
            validate_resume_upload(request.filename, entry.mime, entry.size)
            if entry.size != request.size or entry.path.resolve().parent != uploads.media_dir.resolve() or entry.path.is_symlink():
                raise ValueError("career_invalid_upload")
            # Transfer ownership before any await: staging TTL/disconnect cleanup
            # must not remove a file while the business worker is adopting it.
            source = Path(uploads.commit([request.attachment_ref], owner=owner)[0])
            try:
                record = await asyncio.to_thread(
                    imports.accept, source, request_id=request.request_id,
                    filename=request.filename, mime=entry.mime, size=entry.size, name=request.name,
                )
            finally:
                await asyncio.to_thread(source.unlink, missing_ok=True)
            return await asyncio.to_thread(self._import_result, service, record)

    @staticmethod
    def _job_image_payload(record: JobImageImport, *, detail: bool = False) -> dict[str, object]:
        return record.model_dump(mode="json", exclude={"request_id", "sha256"} if detail else {"request_id", "sha256", "reading"})

    def job_image(self, reference: str, import_id: str) -> dict[str, object]:
        service, _ = self._resolve(reference)
        return self._job_image_payload(JobImages(service.store).get(import_id), detail=True)

    def job_image_file(self, reference: str, import_id: str) -> tuple[JobImageImport, bytes]:
        service, _ = self._resolve(reference)
        return JobImages(service.store).original(import_id)

    def _job_image_result(self, service: CareerService, record: JobImageImport) -> dict[str, object]:
        job = service.get_job(record.job_version_id) if record.job_version_id is not None else None
        return {"import": self._job_image_payload(record, detail=True), "job": job.model_dump(mode="json") if job else None}

    async def _recognize_job_image(self, service: CareerService, record: JobImageImport) -> JobImageImport:
        images = JobImages(service.store)
        if record.status == "ready" or record.job_version_id is not None:
            return record
        record = await asyncio.to_thread(images.validate, record.id)
        if record.error_code in {"invalid_image", "image_limit", "file_unavailable"}:
            return record
        reader = self.job_image_recognizer
        if reader is None:
            return await asyncio.to_thread(images.fail, record.id, "vision_unavailable")
        try:
            runtime = reader.resolve_runtime()
            await asyncio.to_thread(images.begin, record.id, model=runtime.model, provider=runtime.provider.provider_name)
            _, content = await asyncio.to_thread(images.original, record.id)
            reading = await asyncio.wait_for(reader.recognize(runtime, content), timeout=75)
        except asyncio.CancelledError:
            await shield_and_drain(asyncio.to_thread(images.fail, record.id, "interrupted"))
            raise
        except TimeoutError:
            return await asyncio.to_thread(images.fail, record.id, "timeout")
        except JobVisionError as error:
            return await asyncio.to_thread(images.fail, record.id, error.code)
        except OSError:
            return await asyncio.to_thread(images.fail, record.id, "file_unavailable")
        except Exception:
            return await asyncio.to_thread(images.fail, record.id, "recognition_failed")
        return await shield_and_drain(asyncio.to_thread(images.finish, record.id, reading))

    async def _import_job_image(self, action: str, payload: dict[str, object], uploads: AttachmentStore | None, owner: str) -> dict[str, object]:
        # Image calls can take time. Serialize them separately so ordinary career
        # reads, conversations, deletion and task updates do not wait on a model.
        async with self._image_lock:
            if action == "career.job.image.save":
                review = _JobImageSave.model_validate(payload)
                service, _ = await asyncio.to_thread(self._resolve, review.workspace_ref)
                images = JobImages(service.store)
                job = await shield_and_drain(asyncio.to_thread(images.save_review, review.id, name=review.name, company=review.company,
                                                               text=review.text, requirements=review.requirements, manual=review.manual))
                return job.model_dump(mode="json")
            if action == "career.job.image.retry":
                retry = _VersionRequest.model_validate(payload)
                service, _ = await asyncio.to_thread(self._resolve, retry.workspace_ref)
                record = await asyncio.to_thread(JobImages(service.store).get, retry.id)
            else:
                request = _JobImageUpload.model_validate(payload)
                service, _ = await asyncio.to_thread(self._resolve, request.workspace_ref)
                images = JobImages(service.store)
                existing = await asyncio.to_thread(images.for_request, request.request_id)
                if existing is not None:
                    if (existing.filename, existing.size) != (request.filename, request.size):
                        raise CareerConflictError("career_image_request_reused")
                    if uploads is not None:
                        uploads.discard([request.attachment_ref], owner=owner)
                    return await asyncio.to_thread(self._job_image_result, service, existing)
                if uploads is None or not owner:
                    raise ValueError("career_image_requires_upload_connection")
                entry = uploads.inspect([request.attachment_ref], owner=owner)[0]
                validate_job_image_upload(request.filename, entry.mime, entry.size)
                if entry.size != request.size or entry.path.resolve().parent != uploads.media_dir.resolve() or entry.path.is_symlink():
                    raise ValueError("career_invalid_upload")
                source = Path(uploads.commit([request.attachment_ref], owner=owner)[0])
                try:
                    record = await shield_and_drain(asyncio.to_thread(images.accept, source, request_id=request.request_id,
                                                                      filename=request.filename, mime=entry.mime, size=entry.size))
                finally:
                    await asyncio.to_thread(source.unlink, missing_ok=True)
            record = await self._recognize_job_image(service, record)
            return await asyncio.to_thread(self._job_image_result, service, record)

    @staticmethod
    def _plan_payload(service: CareerService, plan: PreparationPlan) -> dict[str, object]:
        analysis = service.get_analysis(plan.analysis_id)
        snapshot = analysis.snapshot
        return {
            **plan.model_dump(mode="json"), "career_thread_id": analysis.career_thread_id,
            "source": {
                "turn_id": snapshot.turn_id, "analysis_status": analysis.status,
                "resume_name": redact(snapshot.resume.name), "resume_version": snapshot.resume.version,
                "job_name": redact(snapshot.job.name), "job_version": snapshot.job.version,
                "created_at": analysis.created_at.isoformat(), "reply_excerpt": redact(analysis.result or "")[:2_000],
            },
        }

    async def propose_plan_for_turn(self, context: RequestContext, tasks: TaskList) -> dict[str, object]:
        if (context.channel != "websocket" or context.metadata.get("webui") is not True
                or context.workspace_scope is None or not context.session_generation
                or context.session_key != f"websocket:{context.chat_id}"):
            raise CareerConflictError("career_plan_requires_bound_turn")
        binding = _PlanBinding.model_validate(context.metadata.get(CAREER_TURN_METADATA))
        if _scope_ref(context.workspace_scope) != binding.workspace_ref:
            raise CareerConflictError("career_plan_scope_mismatch")

        def propose() -> dict[str, object]:
            service, thread = self.bound_thread(binding.workspace_ref, binding.thread_id)
            assert self.sessions is not None
            version = self.sessions.read_session_version(thread.session_key)
            analysis = service.get_analysis(binding.analysis_id)
            turn_id = context.metadata.get(WEBUI_TURN_METADATA_KEY)
            if (thread.session_key != context.session_key or version is None
                    or version[0] != context.session_generation
                    or analysis.career_thread_id != thread.id or not isinstance(turn_id, str)
                    or analysis.snapshot.turn_id != turn_id):
                raise CareerConflictError("career_plan_scope_mismatch")
            # Tool execution can precede delivery of the first streaming event.
            if analysis.status == "queued":
                service.update_analysis(analysis.id, status="running")
            return self._plan_payload(service, service.propose_plan(analysis.id, tasks))

        async with self._lifecycle_lock:
            return await asyncio.to_thread(propose)

    def _mutate_plan(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        request = (_PlanConfirm if action == "career.plan.confirm" else _TaskUpdate if action == "career.task.update" else _PlanAction).model_validate(payload)
        service, thread = self.bound_thread(request.workspace_ref, request.thread_id)
        if isinstance(request, _TaskUpdate):
            # Resolve ownership through stored relationships, never client paths.
            with service.store.transaction() as db:
                current = service.store.task(db, request.id)
                plan = service.store.plan(db, current.plan_id)
                analysis = service.store.analysis(db, plan.analysis_id)
            if analysis.career_thread_id != thread.id:
                raise CareerConflictError("career_task_scope_mismatch")
            return {**service.update_task(request.id, status=request.status, result=request.result).model_dump(mode="json"), "session_key": thread.session_key, "career_thread_id": thread.id}
        plan = service.get_plan(request.id)
        if service.get_analysis(plan.analysis_id).career_thread_id != thread.id:
            raise CareerConflictError("career_plan_scope_mismatch")
        result = service.confirm_plan(plan.id, tasks=request.tasks) if isinstance(request, _PlanConfirm) else service.cancel_plan(plan.id)
        return {**self._plan_payload(service, result), "session_key": thread.session_key}

    def _deletion_binding(self, session_key: str) -> tuple[CareerService, CareerThread] | None:
        if self.sessions is None or not session_key.startswith("websocket:career-"):
            return None
        stored = self.sessions.read_session_metadata(session_key)
        if stored is None:
            return None
        metadata = stored["metadata"]
        thread_id = metadata.get("career_thread_id")
        if not isinstance(thread_id, str):
            return None
        scope = self.workspaces.scope_for_session_key(session_key)
        service, _ = self._resolve(_scope_ref(scope))
        try:
            thread = service.get_thread(thread_id)
        except CareerNotFoundError:
            return None
        if thread.session_key == session_key and _matches_session(metadata, thread.id, scope):
            return service, thread
        return None

    async def delete_session(self, session_key: str, delete: Callable[[], Awaitable[bool]]) -> bool:
        async with self._lifecycle_lock:
            binding = await asyncio.to_thread(self._deletion_binding, session_key)
            deleted = await delete()
            if deleted and binding is not None:
                await asyncio.to_thread(binding[0].retire_threads, [binding[1].id])
            return deleted

    def _mutate_record(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        if action in {"career.resume.confirm", "career.job.confirm"}:
            request = _VersionRequest.model_validate(payload)
            service, _ = self._resolve(request.workspace_ref)
            record = (service.confirm_resume(request.id) if action == "career.resume.confirm"
                      else service.confirm_job(request.id))
            return record.model_dump(mode="json")
        if action == "career.resume.rename":
            rename = _ResumeRename.model_validate(payload)
            service, _ = self._resolve(rename.workspace_ref)
            return service.rename_resume(rename.id, name=rename.name, notes=rename.notes).model_dump(mode="json")
        if action.startswith("career.resume."):
            resume = _ResumeWrite.model_validate(payload)
            service, _ = self._resolve(resume.workspace_ref)
            if action == "career.resume.create":
                if resume.id is not None:
                    raise ValueError("career_invalid_request")
                return service.create_resume(
                    name=resume.name, text=resume.text, notes=resume.notes, profile=resume.profile,
                ).model_dump(mode="json")
            if resume.id is None:
                raise ValueError("career_invalid_request")
            if action == "career.resume.revise":
                return service.revise_resume(
                    resume.id, name=resume.name, notes=resume.notes, text=resume.text, profile=resume.profile,
                ).model_dump(mode="json")
            return service.edit_resume_draft(
                resume.id, name=resume.name, notes=resume.notes, text=resume.text, profile=resume.profile,
            ).model_dump(mode="json")
        job = _JobWrite.model_validate(payload)
        service, _ = self._resolve(job.workspace_ref)
        if action == "career.job.create":
            if job.id is not None:
                raise ValueError("career_invalid_request")
            return service.create_job(
                name=job.name, company=job.company, text=job.text, requirements=job.requirements,
            ).model_dump(mode="json")
        if job.id is None:
            raise ValueError("career_invalid_request")
        if action == "career.job.revise":
            return service.revise_job(
                job.id, name=job.name, company=job.company, text=job.text, requirements=job.requirements,
            ).model_dump(mode="json")
        return service.edit_job_draft(
            job.id, name=job.name, company=job.company, text=job.text, requirements=job.requirements,
        ).model_dump(mode="json")

    async def mutate(
        self, action: str, payload: dict[str, object], *,
        uploads: AttachmentStore | None = None, upload_owner: str = "",
    ) -> dict[str, object]:
        if action not in CAREER_MUTATIONS:
            raise CareerNotFoundError("career_action_not_found")
        if action in {"career.job.image.import", "career.job.image.retry", "career.job.image.save"}:
            return await self._import_job_image(action, payload, uploads, upload_owner)
        if action in {"career.resume.import", "career.resume.import.retry"}:
            return await self._import_resume(action, payload, uploads, upload_owner)
        if action in {"career.plan.confirm", "career.plan.cancel", "career.task.update"}:
            async with self._lifecycle_lock:
                return await asyncio.to_thread(self._mutate_plan, action, payload)
        if action != "career.thread.create":
            return await asyncio.to_thread(self._mutate_record, action, payload)
        async with self._lifecycle_lock:
            return await self._create_thread(payload)

    async def _create_thread(self, payload: dict[str, object]) -> dict[str, object]:
        request = _ThreadCreate.model_validate(payload)
        service, scope = await asyncio.to_thread(self._resolve, request.workspace_ref)
        await asyncio.to_thread(self._retire_missing_threads, service)
        binding = json.dumps({
            "workspace_id": service.store.workspace_id,
            "resume": request.resume_version_id, "job": request.job_version_id,
            "conditions": request.conditions.model_dump(mode="json"),
        }, sort_keys=True, ensure_ascii=False)
        chat_id = "career-" + hashlib.sha256(binding.encode("utf-8")).hexdigest()[:32]
        thread = await asyncio.to_thread(
            service.create_thread, session_key=f"websocket:{chat_id}",
            resume_version_id=request.resume_version_id, job_version_id=request.job_version_id,
            conditions=request.conditions,
        )
        if self.sessions is None:
            raise CareerConflictError("career_unavailable")

        title = await asyncio.to_thread(lambda: (
            f"{service.get_resume(thread.resume_version_id).name} → "
            f"{service.get_job(thread.job_version_id).name}"
        )[:160])

        def bind(metadata: dict[str, object]) -> None:
            if metadata:
                if not _matches_session(metadata, thread.id, scope):
                    raise CareerConflictError("career_session_conflict")
                return  # Preserve the existing chat's access policy and other metadata.
            metadata.update({
                "webui": True, "career_thread_id": thread.id,
                "title": title,
                WORKSPACE_SCOPE_METADATA_KEY: scope.metadata(),
            })

        await self.sessions.state.mutate_metadata(thread.session_key, bind, create_if_missing=True)
        return thread.model_dump(mode="json")
