import { afterEach, expect, it, vi } from "vitest";
import { confirmCareerPlan, updateCareerTask, fetchCareerConversation, fetchCareerState, fetchCareerWorkspaces, saveCareerJob, importCareerResume, fetchCareerResumeImport, fetchCareerResumeOriginal, retryCareerResumeImport } from "@/lib/career-api";

afterEach(() => vi.unstubAllGlobals());

const importedFile = {
  id: `import_${"a".repeat(32)}`, filename: "resume.pdf", file_type: "pdf", size: 4, name: "resume",
  status: "ready", error_code: null, resume_version_id: "imported-version",
  created_at: "2026-11-02T00:00:00Z", updated_at: "2026-11-02T00:00:00Z",
};
const importedVersion = {
  id: "imported-version", name: "resume", version: 2, text: "PDF text", notes: "", profile: {},
  source: "pdf", import_id: importedFile.id, status: "draft", created_at: importedFile.created_at,
};

it("stages binary resume content through the shared upload transport and sends only its reference to the career mutation", async () => {
  const client = { stageAttachments: vi.fn().mockResolvedValue([{ reference: "upload-reference" }]),
    requestMutation: vi.fn().mockResolvedValue({ import: importedFile, resume: importedVersion }) };
  const file = new File(["test"], "resume.pdf");
  const requestId = "11111111-1111-4111-8111-111111111111";
  expect((await importCareerResume(client, "workspace", file, requestId)).resume?.status).toBe("draft");
  expect(client.stageAttachments).toHaveBeenCalledWith([{ data_url: "data:application/pdf;base64,dGVzdA==", name: "resume.pdf" }]);
  expect(client.requestMutation).toHaveBeenCalledWith("career.resume.import", {
    workspace_ref: "workspace", request_id: requestId, attachment_ref: "upload-reference", filename: "resume.pdf", size: 4, name: "resume",
  }, 120_000);
  client.requestMutation.mockResolvedValue({ import: importedFile, resume: { ...importedVersion, import_id: "another-import" } });
  await expect(importCareerResume(client, "workspace", file, requestId)).rejects.toThrow("career_invalid_response");
});

it("validates the document evidence identity and line references before showing extracted text", async () => {
  const data = { ...importedFile, text: "PDF text", lines: [{ number: 1, locator: "page=1,line=1", text: "PDF text" }], sections: [{ key: "education", line_numbers: [1] }] };
  const read = (value: unknown) => vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(value)));
  read(data);
  expect((await fetchCareerResumeImport("token", "workspace", importedFile.id)).lines[0].locator).toBe("page=1,line=1");
  read({ ...data, id: `import_${"b".repeat(32)}` });
  await expect(fetchCareerResumeImport("token", "workspace", importedFile.id)).rejects.toThrow("career_invalid_response");
  read({ ...data, sections: [{ key: "education", line_numbers: [2] }] });
  await expect(fetchCareerResumeImport("token", "workspace", importedFile.id)).rejects.toThrow("career_invalid_response");
});

it("downloads originals with existing authentication and retries stored imports without accepting paths", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response("original bytes", { headers: { "content-type": "application/octet-stream" } }));
  vi.stubGlobal("fetch", fetch);
  const blob = await fetchCareerResumeOriginal("engineering-token", "workspace", importedFile.id);
  expect(await blob.text()).toBe("original bytes");
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/career/resume-file?workspace_ref=workspace"), expect.objectContaining({
    headers: { Authorization: "Bearer engineering-token" }, cache: "no-store", redirect: "error", credentials: "same-origin",
  }));
  const transport = { requestMutation: vi.fn().mockResolvedValue({ import: importedFile, resume: importedVersion }) };
  expect((await retryCareerResumeImport(transport, "workspace", importedFile.id)).resume?.id).toBe(importedVersion.id);
  expect(transport.requestMutation).toHaveBeenCalledWith("career.resume.import.retry", { workspace_ref: "workspace", id: importedFile.id }, 120_000);
});

it("confirms an explicitly selected plan through the mutation transport and rejects a task reply for another preparation", async () => {
  const source = { turn_id: "turn", analysis_status: "succeeded", resume_name: "Resume", resume_version: 1,
    job_name: "Job", job_version: 1, created_at: "2026-11-01T08:00:00Z", reply_excerpt: "Advice" };
  const requestMutation = vi.fn().mockResolvedValue({ id: "plan", analysis_id: "analysis", career_thread_id: "thread", status: "confirmed", source });
  const tasks = [{ title: "Task", action: "Write one page", completion_criteria: "Page ready", basis: "Job asks for project evidence" }];
  await confirmCareerPlan({ requestMutation }, "workspace", "thread", "plan", tasks);
  expect(requestMutation).toHaveBeenCalledWith("career.plan.confirm", { workspace_ref: "workspace", thread_id: "thread", id: "plan", tasks }, expect.any(Number));
  requestMutation.mockResolvedValue({ id: "task", plan_id: "plan", career_thread_id: "other", title: "Task", action: "Action", completion_criteria: "Criteria", status: "completed", result: "Done" });
  await expect(updateCareerTask({ requestMutation }, "workspace", "thread", "task", "completed", "Done")).rejects.toThrow("career_invalid_response");
});

it("rejects a conversation response for another preparation before displaying its sources", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ thread_id: "other", preview: {}, analyses: [] }), { headers: { "content-type": "application/json" } })));
  await expect(fetchCareerConversation("engineering-token", "workspace", "current")).rejects.toThrow("career_invalid_response");
});

it("accepts existing conversation previews and validates the optional condition comparison before rendering", async () => {
  const payload = {
    thread_id: "current", truncated: false, analyses: [],
    preview: {
      resume: { id: "resume", name: "Resume", version: 1, text: "Resume text", profile: {} },
      job: { id: "job", name: "Job", version: 1, text: "Job text", company: "Demo", requirements: {} },
      conditions: { days_per_week: 5, available_months: 5, locations: [] },
      availability_conflicts: [], truncated: false,
    },
  };
  const read = (value: unknown) => vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } })));
  read(payload);
  expect((await fetchCareerConversation("engineering-token", "workspace", "current")).preview.conditions.days_per_week).toBe(5);
  read({ ...payload, preview: { ...payload.preview, resume_condition_differences: [{ field: "days_per_week", selected: 5, resume: "four", evidence: "Availability: 4 days per week." }] } });
  await expect(fetchCareerConversation("engineering-token", "workspace", "current")).rejects.toThrow("career_invalid_response");
  read({ ...payload, preview: { ...payload.preview, conditions: { ...payload.preview.conditions, available_from: {} } } });
  await expect(fetchCareerConversation("engineering-token", "workspace", "current")).rejects.toThrow("career_invalid_response");
});

it("reads with the existing bearer authentication and abort signal", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ workspaces: [{ ref: "project", name: "Project", is_default: true }] }), { headers: { "content-type": "application/json" } }));
  vi.stubGlobal("fetch", fetch);
  const controller = new AbortController();
  expect(await fetchCareerWorkspaces("engineering-token", controller.signal)).toEqual([{ ref: "project", name: "Project", is_default: true }]);
  expect(fetch).toHaveBeenCalledWith("/api/career/workspaces", expect.objectContaining({ headers: { Authorization: "Bearer engineering-token" }, credentials: "same-origin" }));
});

it("saves through the shared mutation transport and preserves null company", async () => {
  const requestMutation = vi.fn().mockResolvedValue({ id: "job", job_id: "root", name: "Intern", version: 1, text: "Description", status: "draft", created_at: "2026-10-30T08:00:00+00:00", company: null, requirements: {} });
  await saveCareerJob({ requestMutation }, "workspace", "career.job.create", { name: "Intern", company: null, text: "Description", requirements: {} });
  expect(requestMutation).toHaveBeenCalledWith("career.job.create", { workspace_ref: "workspace", name: "Intern", company: null, text: "Description", requirements: {} }, expect.any(Number));
});

it.each([
  undefined,
  "not-a-date",
])("rejects unusable creation timestamps from a host before displaying version metadata", async (created_at) => {
  const requestMutation = vi.fn().mockResolvedValue({ id: "job", job_id: "root", name: "Intern", version: 1, text: "Description", status: "draft", company: null, requirements: {}, created_at });
  await expect(saveCareerJob({ requestMutation }, "workspace", "career.job.create", { name: "Intern", company: null, text: "Description", requirements: {} })).rejects.toThrow("career_invalid_response");
});

it.each([
  { workspace_ref: "wrong" },
  { workspace_ref: "workspace", workspace_id: "id", resumes: [], jobs: [], threads: [{ conditions: { days_per_week: {} } }], plans: [], tasks: [], chat_available: {} },
  { workspace_ref: "workspace", workspace_id: "id", resumes: [], jobs: [], threads: [], plans: [], tasks: [], chat_available: {}, deleted_thread_ids: [17] },
])("rejects malformed or wrong-workspace read responses before rendering", async (payload) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } })));
  await expect(fetchCareerState("engineering-token", "workspace")).rejects.toThrow("career_invalid_response");
});
