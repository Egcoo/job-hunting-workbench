import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { JobLibrary } from "@/components/career/JobLibrary";
import { JobImageSource } from "@/components/career/JobImageImport";
import type { JobImageDetail, JobImageSummary, JobVersion } from "@/lib/career-types";
import * as api from "@/lib/career-api";
import en from "@/i18n/locales/en/common.json";

const context = vi.hoisted(() => ({ getToken: () => "engineering-token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => context }));
vi.mock("@/lib/career-api", () => ({ fetchCareerJobImage: vi.fn(), fetchCareerJobImageOriginal: vi.fn() }));
const c = en.career;
const importId = `jobimage_${"a".repeat(32)}`;
const detail: JobImageDetail = {
  id: importId, filename: "job.png", file_type: "png", size: 128, status: "ready", width: 320, height: 180,
  error_code: null, model: "engineering-vision", provider: "engineering", job_version_id: null, manual: false,
  created_at: "2026-11-02T08:00:00Z", updated_at: "2026-11-02T08:00:00Z",
  reading: { readable: true, text: "IMAGE_JOB_A\n4天/周 6个月", warnings: ["Title is cut off; please review."],
    fields: { name: { value: "IMAGE_JOB_A", evidence: "IMAGE_JOB_A" }, company: null, locations: [], responsibilities: [], qualifications: [],
      days_per_week: { value: 4, evidence: "4天/周" }, available_months: { value: 6, evidence: "6个月" } } },
};
const old: JobVersion = { id: "old-job", job_id: "old", version: 1, name: "Old job", company: null, text: "Old job text", source: "text", status: "confirmed", requirements: {}, parent_version_id: null, created_at: detail.created_at };
const callbacks = { import: vi.fn(), retry: vi.fn(), save: vi.fn(), confirm: vi.fn() };

function Harness({ supported = true, failed = false }: { supported?: boolean; failed?: boolean }) {
  const [records, setRecords] = useState<JobVersion[]>([old]);
  const [images, setImages] = useState<JobImageSummary[]>(failed ? [{ ...detail, status: "failed", error_code: "vision_unavailable" }] : []);
  return <JobLibrary records={records} images={images} workspaceRef="workspace-a" activeVersionId={old.id} busy={false}
    imageSupported={supported} recognitionAvailable={!failed}
    onImportImage={async (file, requestId) => { callbacks.import(file, requestId); setImages([detail]); return { import: detail, job: null }; }}
    onRetryImage={async (id) => { callbacks.retry(id); setImages([detail]); return { import: detail, job: null }; }}
    onReadImage={async () => failed ? { ...detail, status: "failed", error_code: "vision_unavailable", reading: null } : detail}
    onSaveImage={async (id, manual, input) => {
      callbacks.save(id, manual, input);
      const saved: JobVersion = { ...old, ...input, id: "new-job", job_id: "new", status: "draft", source: "image", import_id: id, parent_version_id: null };
      setRecords([old, saved]); setImages([{ ...detail, job_version_id: saved.id, manual }]); return saved;
    }} onSave={vi.fn()} onConfirm={async (id) => { callbacks.confirm(id); const saved = { ...records.find((job) => job.id === id)!, status: "confirmed" as const }; setRecords([old, saved]); return saved; }} />;
}

beforeEach(() => {
  vi.clearAllMocks();
  const ExistingURL = URL;
  vi.stubGlobal("URL", class extends ExistingURL {
    static createObjectURL = vi.fn(() => "blob:engineering-job-image");
    static revokeObjectURL = vi.fn();
  });
  vi.mocked(api.fetchCareerJobImage).mockResolvedValue(detail);
  vi.mocked(api.fetchCareerJobImageOriginal).mockResolvedValue(new Blob(["engineering image"]));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("recognizes into an editable review, preserves the image text and only saves and confirms on explicit user actions", async () => {
  render(<Harness />);
  const file = new File(["image bytes"], "job.png", { type: "image/png" });
  fireEvent.change(screen.getByLabelText(c.selectJobImage), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: c.recognizeJobImage }));
  const form = screen.getByRole("form", { name: c.jobEditor });
  await waitFor(() => expect(within(form).getByLabelText(c.jobName)).toHaveValue("IMAGE_JOB_A"));
  expect(callbacks.save).not.toHaveBeenCalled(); expect(callbacks.confirm).not.toHaveBeenCalled();
  expect(within(form).getByLabelText(c.jobDays)).toHaveValue(4);
  expect(await screen.findByText("Title is cut off; please review.")).toBeVisible();
  fireEvent.click(within(form).getByText(c.jobImageCompare, { exact: true }));
  expect(await within(form).findByRole("img", { name: "Original job image: job.png" })).toBeVisible();
  fireEvent.change(within(form).getByLabelText(c.jobDays), { target: { value: "3" } });
  fireEvent.change(within(form).getByLabelText(c.jobName), { target: { value: "User reviewed job" } });
  fireEvent.click(within(form).getByRole("button", { name: c.saveDraft }));
  await waitFor(() => expect(within(form).getByRole("button", { name: c.confirm })).toBeEnabled());
  expect(callbacks.save).toHaveBeenCalledWith(importId, false, expect.objectContaining({ name: "User reviewed job", requirements: expect.objectContaining({ days_per_week: 3 }) }));
  expect(within(form).getByRole("region", { name: c.jobImageSource })).toHaveTextContent(/IMAGE_JOB_A\s+4天\/周 6个月/);
  expect(within(screen.getByRole("list", { name: c.jobVersions })).getByRole("button", { name: /Old job/ })).toHaveTextContent(c.currentUse);
  fireEvent.click(within(form).getByRole("button", { name: c.confirm }));
  expect(callbacks.confirm).toHaveBeenCalledWith("new-job");
  expect(within(form).getByLabelText(c.jobText)).toHaveAttribute("readonly");
});

it("allows explicit manual entry after failed recognition without filling the job from its filename", async () => {
  render(<Harness failed />);
  fireEvent.click(screen.getByRole("button", { name: c.manualEntry }));
  const form = screen.getByRole("form", { name: c.jobEditor });
  await waitFor(() => expect(within(form).getByText(c.imageManualHint)).toBeVisible());
  expect(within(form).getByLabelText(c.jobName)).toHaveValue("");
  fireEvent.change(within(form).getByLabelText(c.jobName), { target: { value: "Manual job" } });
  fireEvent.change(within(form).getByLabelText(c.jobText), { target: { value: "Manually reviewed requirements" } });
  fireEvent.click(within(form).getByRole("button", { name: c.saveDraft }));
  await waitFor(() => expect(callbacks.save).toHaveBeenCalledWith(importId, true, expect.objectContaining({ name: "Manual job" })));
  expect(callbacks.confirm).not.toHaveBeenCalled();
});

it("keeps unsaved edits while preventing a new upload and rejects an unsupported file before recognition", () => {
  render(<Harness />);
  fireEvent.change(screen.getByLabelText(c.selectJobImage), { target: { files: [new File(["pdf"], "job.pdf")] } });
  expect(screen.getByRole("alert")).toHaveTextContent(c.jobImageFileInvalid);
  expect(callbacks.import).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText(c.jobName), { target: { value: "Unsaved job" } });
  expect(screen.getByLabelText(c.selectJobImage)).toBeDisabled();
  expect(screen.getByText(c.jobImageSaveBeforeNext)).toBeVisible();
});

it("retains text entry on an older host without image-import requests", () => {
  render(<Harness supported={false} />);
  expect(screen.getByRole("form", { name: c.jobEditor })).toBeVisible();
  expect(screen.queryByLabelText(c.selectJobImage)).not.toBeInTheDocument();
  expect(callbacks.import).not.toHaveBeenCalled();
});

it("aborts pending source reads when leaving the selected image", async () => {
  let resolveDetail!: (value: JobImageDetail) => void;
  let resolveImage!: (value: Blob) => void;
  vi.mocked(api.fetchCareerJobImage).mockReturnValue(new Promise((resolve) => { resolveDetail = resolve; }));
  vi.mocked(api.fetchCareerJobImageOriginal).mockReturnValue(new Promise((resolve) => { resolveImage = resolve; }));
  const view = render(<JobImageSource workspaceRef="workspace-a" record={detail} />);
  const signal = vi.mocked(api.fetchCareerJobImage).mock.calls[0][3];
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => { resolveDetail(detail); resolveImage(new Blob(["old source"])); });
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});
