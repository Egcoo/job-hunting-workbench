import { ApiError, API_READ_TIMEOUT_MS, mutation, request, type WebUIMutationTransport } from "@/lib/api";
import type {
  CareerState, CareerThread, CareerWorkspace, JobAction, JobVersion, JobWrite,
  PersonalConditions, ResumeAction, ResumeVersion, ResumeWrite,
  CareerConversationData,
  CareerPlan, CareerPlanSource, CareerTask, CareerTaskInput,
  ResumeImportSummary, ResumeImportDetail, ResumeImportResult,
  JobImageSummary, JobImageDetail, JobImageResult, JobImageReading,
} from "@/lib/career-types";
import type { NanobotClient } from "@/lib/nanobot-client";
import { fetchWithTimeout } from "@/lib/http";

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function invalid(): never { throw new ApiError(502, "career_invalid_response"); }
function version(value: unknown): value is Record<string, unknown> {
  return record(value) && typeof value.id === "string" && typeof value.name === "string"
    && typeof value.text === "string" && Number.isInteger(value.version)
    && typeof value.created_at === "string" && Number.isFinite(Date.parse(value.created_at))
    && (value.status === "draft" || value.status === "confirmed");
}
function resume(value: unknown): value is ResumeVersion {
  return version(value) && typeof value.notes === "string" && record(value.profile)
    && (value.source === undefined || ["text", "pdf", "docx"].includes(String(value.source)))
    && (value.import_id === undefined || value.import_id === null || typeof value.import_id === "string");
}
function job(value: unknown): value is JobVersion {
  return version(value) && typeof value.job_id === "string"
    && (value.company === null || typeof value.company === "string") && record(value.requirements)
    && (value.source === undefined || value.source === "text" || value.source === "image")
    && (value.import_id === undefined || value.import_id === null || typeof value.import_id === "string");
}
function thread(value: unknown): value is CareerThread {
  return record(value) && ["id", "workspace_id", "session_key", "resume_version_id", "job_version_id"]
    .every((field) => typeof value[field] === "string") && record(value.conditions)
    && ["days_per_week", "available_months"].every((field) => record(value.conditions)
      && (value.conditions[field] === null || Number.isInteger(value.conditions[field])))
    && Array.isArray(value.conditions.locations) && value.conditions.locations.every((item) => typeof item === "string");
}

function planSource(value: unknown): value is CareerPlanSource {
  return record(value) && (value.turn_id === null || typeof value.turn_id === "string")
    && ["queued", "running", "succeeded", "failed", "cancelled"].includes(String(value.analysis_status))
    && typeof value.resume_name === "string" && Number.isInteger(value.resume_version)
    && typeof value.job_name === "string" && Number.isInteger(value.job_version)
    && typeof value.created_at === "string" && Number.isFinite(Date.parse(value.created_at))
    && typeof value.reply_excerpt === "string";
}
function taskInput(value: unknown): value is CareerTaskInput {
  return record(value) && ["title", "action", "completion_criteria", "basis"].every((key) => typeof value[key] === "string");
}
function task(value: unknown): value is CareerTask {
  return record(value) && ["id", "plan_id", "title", "action", "completion_criteria", "result"].every((key) => typeof value[key] === "string")
    && (value.basis === undefined || typeof value.basis === "string") && (value.status === "pending" || value.status === "completed");
}

function resumeImport(value: unknown): value is ResumeImportSummary {
  return record(value) && ["id", "filename", "name"].every((key) => typeof value[key] === "string")
    && /^import_[a-f0-9]{32}$/.test(String(value.id)) && ["pdf", "docx"].includes(String(value.file_type))
    && Number.isSafeInteger(value.size) && Number(value.size) > 0 && Number(value.size) <= 6 * 1024 * 1024
    && ["processing", "ready", "failed"].includes(String(value.status))
    && (value.error_code === null || ["invalid_document", "encrypted", "empty_document", "text_limit", "safety_limit", "file_unavailable"].includes(String(value.error_code)))
    && (value.resume_version_id === null || typeof value.resume_version_id === "string")
    && (value.status === "ready" ? typeof value.resume_version_id === "string" && value.error_code === null
      : value.resume_version_id === null && (value.status === "failed" ? value.error_code !== null : value.error_code === null))
    && ["created_at", "updated_at"].every((key) => typeof value[key] === "string" && Number.isFinite(Date.parse(String(value[key]))));
}
function importResult(value: unknown): ResumeImportResult {
  if (!record(value) || !resumeImport(value.import)) return invalid();
  if (value.import.status === "ready") {
    if (!resume(value.resume) || value.resume.id !== value.import.resume_version_id || value.resume.import_id !== value.import.id || value.resume.source !== value.import.file_type) return invalid();
  } else if (value.resume !== null || value.import.resume_version_id !== null) return invalid();
  return value as unknown as ResumeImportResult;
}

function jobImage(value: unknown): value is JobImageSummary {
  return record(value) && typeof value.id === "string" && /^jobimage_[a-f0-9]{32}$/.test(value.id)
    && typeof value.filename === "string" && ["png", "jpg", "webp"].includes(String(value.file_type))
    && Number.isSafeInteger(value.size) && Number(value.size) > 0 && Number(value.size) <= 6 * 1024 * 1024
    && ["processing", "ready", "failed"].includes(String(value.status))
    && ["width", "height"].every((key) => value[key] === null || (Number.isInteger(value[key]) && Number(value[key]) > 0 && Number(value[key]) <= 10_000))
    && ["model", "provider", "job_version_id"].every((key) => value[key] === null || typeof value[key] === "string")
    && typeof value.manual === "boolean"
    && (value.error_code === null || ["invalid_image", "image_limit", "file_unavailable", "vision_unavailable", "vision_unverified", "unreadable", "invalid_result", "recognition_failed", "timeout", "interrupted"].includes(String(value.error_code)))
    && (value.status === "failed" ? value.error_code !== null : value.error_code === null)
    && ["created_at", "updated_at"].every((key) => typeof value[key] === "string" && Number.isFinite(Date.parse(String(value[key]))));
}
function jobReading(value: unknown): value is JobImageReading {
  if (!record(value) || typeof value.readable !== "boolean" || typeof value.text !== "string" || !record(value.fields)
    || !Array.isArray(value.warnings) || !value.warnings.every((item) => typeof item === "string")) return false;
  const cited = (item: unknown) => record(item) && typeof item.value === "string" && typeof item.evidence === "string";
  const count = (item: unknown, maximum: number) => item === null || (record(item) && Number.isInteger(item.value) && Number(item.value) >= 1 && Number(item.value) <= maximum && typeof item.evidence === "string");
  const fields = value.fields;
  return [fields.name, fields.company].every((item) => item === null || cited(item))
    && [fields.locations, fields.responsibilities, fields.qualifications].every((items) => Array.isArray(items) && items.every(cited))
    && count(fields.days_per_week, 7) && count(fields.available_months, 60);
}
function jobImageDetail(value: unknown): value is JobImageDetail {
  return jobImage(value) && record(value) && (value.reading === null || jobReading(value.reading))
    && (value.status !== "ready" || (record(value.reading) && value.reading.readable === true));
}
function jobImageResult(value: unknown): JobImageResult {
  if (!record(value) || !jobImageDetail(value.import)) return invalid();
  if (value.import.job_version_id !== null) {
    if (!job(value.job) || value.job.id !== value.import.job_version_id || value.job.source !== "image" || value.job.import_id !== value.import.id) return invalid();
  } else if (value.job !== null) return invalid();
  return value as unknown as JobImageResult;
}

export async function fetchCareerWorkspaces(token: string, signal?: AbortSignal): Promise<CareerWorkspace[]> {
  const value = await request<unknown>("/api/career/workspaces", token, { signal }, API_READ_TIMEOUT_MS);
  if (!record(value) || !Array.isArray(value.workspaces)) return invalid();
  const workspaces: CareerWorkspace[] = [];
  for (const item of value.workspaces) {
    if (!record(item) || typeof item.ref !== "string" || typeof item.name !== "string" || typeof item.is_default !== "boolean") return invalid();
    workspaces.push({ ref: item.ref, name: item.name, is_default: item.is_default });
  }
  return workspaces;
}

export async function fetchCareerState(token: string, reference: string, signal?: AbortSignal): Promise<CareerState> {
  const value = await request<unknown>(`/api/career?workspace_ref=${encodeURIComponent(reference)}`, token, { signal }, API_READ_TIMEOUT_MS);
  if (!record(value) || value.workspace_ref !== reference || typeof value.workspace_id !== "string"
    || !Array.isArray(value.resumes) || !value.resumes.every(resume)
    || (value.imports !== undefined && (!Array.isArray(value.imports) || !value.imports.every(resumeImport)))
    || !Array.isArray(value.jobs) || !value.jobs.every(job)
    || (value.job_images !== undefined && (!Array.isArray(value.job_images) || !value.job_images.every(jobImage)))
    || (value.job_image_recognition_available !== undefined && typeof value.job_image_recognition_available !== "boolean")
    || !Array.isArray(value.threads) || !value.threads.every(thread)
    || (value.deleted_thread_ids !== undefined && (!Array.isArray(value.deleted_thread_ids) || !value.deleted_thread_ids.every((item) => typeof item === "string")))
    || !Array.isArray(value.plans) || !value.plans.every((item) => record(item)
      && ["id", "analysis_id", "career_thread_id"].every((key) => typeof item[key] === "string") && item.status === "confirmed"
      && (item.source === undefined || planSource(item.source)))
    || (value.drafts !== undefined && (!Array.isArray(value.drafts) || !value.drafts.every((item) => record(item)
      && ["id", "analysis_id", "career_thread_id"].every((key) => typeof item[key] === "string") && item.status === "draft"
      && Array.isArray(item.proposed_tasks) && item.proposed_tasks.length > 0 && item.proposed_tasks.length <= 50
      && item.proposed_tasks.every(taskInput) && planSource(item.source))))
    || !Array.isArray(value.tasks) || !value.tasks.every(task)
    || !record(value.chat_available) || !Object.values(value.chat_available).every((item) => typeof item === "boolean")) return invalid();
  // All UI-consumed fields are checked at this transport boundary.
  return value as unknown as CareerState;
}

async function stageCareerFile(client: Pick<NanobotClient, "stageAttachments">, file: File, mime: string): Promise<string> {
  const data = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => typeof reader.result === "string" ? resolve(reader.result) : reject(new Error("File read failed"));
    reader.onerror = () => reject(new Error("File read failed"));
    reader.readAsDataURL(new Blob([file], { type: mime }));
  });
  const references = await client.stageAttachments([{ data_url: data, name: file.name }]);
  const attachment = references[0];
  if (!attachment || !("reference" in attachment) || typeof attachment.reference !== "string") return invalid();
  return attachment.reference;
}

export async function importCareerResume(
  client: Pick<NanobotClient, "stageAttachments" | "requestMutation">,
  reference: string, file: File, requestId: string,
): Promise<ResumeImportResult> {
  const extension = file.name.split(".").at(-1)?.toLowerCase();
  const mime = extension === "pdf" ? "application/pdf" : extension === "docx" ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "";
  if (!mime || file.size === 0 || file.size > 6 * 1024 * 1024) throw new ApiError(400, "career_invalid_resume_file");
  const attachment = await stageCareerFile(client, file, mime);
  const result = await mutation<unknown>(client, "career.resume.import", {
    workspace_ref: reference, request_id: requestId, attachment_ref: attachment,
    filename: file.name, size: file.size, name: file.name.replace(/\.(pdf|docx)$/i, ""),
  }, 120_000);
  return importResult(result);
}

export async function retryCareerResumeImport(transport: WebUIMutationTransport, reference: string, id: string): Promise<ResumeImportResult> {
  return importResult(await mutation<unknown>(transport, "career.resume.import.retry", { workspace_ref: reference, id }, 120_000));
}

export async function fetchCareerResumeImport(token: string, reference: string, id: string, signal?: AbortSignal): Promise<ResumeImportDetail> {
  const value = await request<unknown>(`/api/career/resume-import?workspace_ref=${encodeURIComponent(reference)}&id=${encodeURIComponent(id)}`, token, { signal }, API_READ_TIMEOUT_MS);
  if (!resumeImport(value) || value.id !== id || !record(value) || typeof value.text !== "string"
    || !Array.isArray(value.lines) || !value.lines.every((line, index) => record(line) && line.number === index + 1 && typeof line.locator === "string" && typeof line.text === "string")
    || !Array.isArray(value.sections) || !value.sections.every((section) => record(section) && ["education", "projects", "skills", "summary", "experience"].includes(String(section.key))
      && Array.isArray(section.line_numbers) && section.line_numbers.every((number) => Number.isInteger(number) && number > 0 && number <= (value.lines as unknown[]).length))) return invalid();
  return value as unknown as ResumeImportDetail;
}

export async function fetchCareerResumeOriginal(token: string, reference: string, id: string, signal?: AbortSignal): Promise<Blob> {
  return fetchCareerOriginal("resume-file", token, reference, id, signal);
}

async function fetchCareerOriginal(path: "resume-file" | "job-image-file", token: string, reference: string, id: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetchWithTimeout(`/api/career/${path}?workspace_ref=${encodeURIComponent(reference)}&id=${encodeURIComponent(id)}`, {
    headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin", redirect: "error", cache: "no-store", signal,
  }, API_READ_TIMEOUT_MS);
  if (!response.ok) throw new ApiError(response.status, "career_original_unavailable");
  if (!response.headers.get("content-type")?.startsWith("application/octet-stream")) return invalid();
  return response.blob();
}

export async function importCareerJobImage(client: Pick<NanobotClient, "stageAttachments" | "requestMutation">, reference: string, file: File, requestId: string): Promise<JobImageResult> {
  const extension = file.name.split(".").at(-1)?.toLowerCase();
  const mime = extension === "png" ? "image/png" : extension === "jpg" || extension === "jpeg" ? "image/jpeg" : extension === "webp" ? "image/webp" : "";
  if (!mime || file.size === 0 || file.size > 6 * 1024 * 1024) throw new ApiError(400, "career_invalid_job_image");
  const attachment = await stageCareerFile(client, file, mime);
  return jobImageResult(await mutation<unknown>(client, "career.job.image.import", {
    workspace_ref: reference, request_id: requestId, attachment_ref: attachment, filename: file.name, size: file.size,
  }, 120_000));
}
export async function retryCareerJobImage(transport: WebUIMutationTransport, reference: string, id: string): Promise<JobImageResult> {
  return jobImageResult(await mutation<unknown>(transport, "career.job.image.retry", { workspace_ref: reference, id }, 120_000));
}
export async function fetchCareerJobImage(token: string, reference: string, id: string, signal?: AbortSignal): Promise<JobImageDetail> {
  const value = await request<unknown>(`/api/career/job-image?workspace_ref=${encodeURIComponent(reference)}&id=${encodeURIComponent(id)}`, token, { signal }, API_READ_TIMEOUT_MS);
  return jobImageDetail(value) && value.id === id ? value : invalid();
}
export async function fetchCareerJobImageOriginal(token: string, reference: string, id: string, signal?: AbortSignal): Promise<Blob> {
  return fetchCareerOriginal("job-image-file", token, reference, id, signal);
}
export async function saveCareerImageJob(transport: WebUIMutationTransport, reference: string, id: string, manual: boolean, input: JobWrite): Promise<JobVersion> {
  const value = await mutation<unknown>(transport, "career.job.image.save", { workspace_ref: reference, ...input, id, manual });
  return job(value) && value.source === "image" && value.import_id === id ? value : invalid();
}

export async function saveCareerResume(transport: WebUIMutationTransport, reference: string, action: ResumeAction, payload: ResumeWrite): Promise<ResumeVersion> {
  const result = await mutation<unknown>(transport, action, { workspace_ref: reference, ...payload });
  return resume(result) ? result : invalid();
}
export async function confirmCareerResume(transport: WebUIMutationTransport, reference: string, id: string): Promise<ResumeVersion> {
  const result = await mutation<unknown>(transport, "career.resume.confirm", { workspace_ref: reference, id });
  return resume(result) ? result : invalid();
}
export async function renameCareerResume(transport: WebUIMutationTransport, reference: string, id: string, name: string, notes: string): Promise<ResumeVersion> {
  const result = await mutation<unknown>(transport, "career.resume.rename", { workspace_ref: reference, id, name, notes });
  return resume(result) ? result : invalid();
}
export async function saveCareerJob(transport: WebUIMutationTransport, reference: string, action: JobAction, payload: JobWrite): Promise<JobVersion> {
  const result = await mutation<unknown>(transport, action, { workspace_ref: reference, ...payload });
  return job(result) ? result : invalid();
}
export async function confirmCareerJob(transport: WebUIMutationTransport, reference: string, id: string): Promise<JobVersion> {
  const result = await mutation<unknown>(transport, "career.job.confirm", { workspace_ref: reference, id });
  return job(result) ? result : invalid();
}
export async function createCareerThread(transport: WebUIMutationTransport, reference: string, resumeId: string, jobId: string, conditions: PersonalConditions): Promise<CareerThread> {
  const result = await mutation<unknown>(transport, "career.thread.create", { workspace_ref: reference, resume_version_id: resumeId, job_version_id: jobId, conditions });
  return thread(result) ? result : invalid();
}

export async function confirmCareerPlan(transport: WebUIMutationTransport, reference: string, threadId: string, planId: string, tasks: CareerTaskInput[]): Promise<CareerPlan> {
  const value = await mutation<unknown>(transport, "career.plan.confirm", { workspace_ref: reference, thread_id: threadId, id: planId, tasks });
  if (!record(value) || value.id !== planId || value.career_thread_id !== threadId || value.status !== "confirmed"
    || typeof value.analysis_id !== "string" || !planSource(value.source)) return invalid();
  return value as unknown as CareerPlan;
}
export async function cancelCareerPlan(transport: WebUIMutationTransport, reference: string, threadId: string, planId: string): Promise<void> {
  const value = await mutation<unknown>(transport, "career.plan.cancel", { workspace_ref: reference, thread_id: threadId, id: planId });
  if (!record(value) || value.id !== planId || value.career_thread_id !== threadId || value.status !== "cancelled") return invalid();
}
export async function updateCareerTask(transport: WebUIMutationTransport, reference: string, threadId: string, taskId: string, status: CareerTask["status"], result: string): Promise<CareerTask> {
  const value = await mutation<unknown>(transport, "career.task.update", { workspace_ref: reference, thread_id: threadId, id: taskId, status, result });
  return record(value) && value.career_thread_id === threadId && task(value) && value.id === taskId ? value : invalid();
}

export async function fetchCareerConversation(token: string, reference: string, threadId: string, signal?: AbortSignal): Promise<CareerConversationData> {
  const value = await request<unknown>(`/api/career/conversation?workspace_ref=${encodeURIComponent(reference)}&thread_id=${encodeURIComponent(threadId)}`, token, { signal }, API_READ_TIMEOUT_MS);
  if (!record(value) || value.thread_id !== threadId || typeof value.truncated !== "boolean"
    || !record(value.preview) || !record(value.preview.resume) || !record(value.preview.job)
    || ![value.preview.resume, value.preview.job].every((item) => typeof item.id === "string" && typeof item.name === "string" && typeof item.text === "string" && Number.isInteger(item.version))
    || !record(value.preview.resume.profile) || !record(value.preview.job.requirements) || typeof value.preview.job.company !== "string"
    || !record(value.preview.conditions) || !Array.isArray(value.preview.conditions.locations)
    || !value.preview.conditions.locations.every((item) => typeof item === "string")
    || (value.preview.conditions.available_from !== undefined && value.preview.conditions.available_from !== null && typeof value.preview.conditions.available_from !== "string")
    || !["days_per_week", "available_months"].every((key) => record(value.preview) && record(value.preview.conditions) && (value.preview.conditions[key] === null || Number.isInteger(value.preview.conditions[key])))
    || typeof value.preview.truncated !== "boolean" || !Array.isArray(value.preview.availability_conflicts)
    || !value.preview.availability_conflicts.every((item) => record(item) && typeof item.field === "string" && Number.isInteger(item.available) && Number.isInteger(item.required))
    || (value.preview.resume_condition_differences !== undefined && (!Array.isArray(value.preview.resume_condition_differences)
      || !value.preview.resume_condition_differences.every((item) => record(item)
        && ["days_per_week", "available_months"].includes(String(item.field))
        && Number.isInteger(item.selected) && Number.isInteger(item.resume)
        && typeof item.evidence === "string" && item.evidence.length <= 200)))
    || !Array.isArray(value.analyses) || !value.analyses.every((item) => record(item)
      && item.career_thread_id === threadId && typeof item.id === "string" && (item.turn_id === null || typeof item.turn_id === "string")
      && typeof item.resume_name === "string" && typeof item.job_name === "string" && typeof item.created_at === "string"
      && Number.isInteger(item.resume_version) && Number.isInteger(item.job_version)
      && ["queued", "running", "succeeded", "failed", "cancelled"].includes(String(item.status)))) return invalid();
  return value as unknown as CareerConversationData;
}
