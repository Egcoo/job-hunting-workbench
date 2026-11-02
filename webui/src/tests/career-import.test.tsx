import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ResumeLibrary } from "@/components/career/ResumeLibrary";
import type { ResumeImportSummary, ResumeVersion } from "@/lib/career-types";
import * as api from "@/lib/career-api";

const context = vi.hoisted(() => ({ getToken: () => "engineering-token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => context }));
vi.mock("@/lib/career-api", () => ({ fetchCareerResumeImport: vi.fn(), fetchCareerResumeOriginal: vi.fn() }));
const importId = `import_${"a".repeat(32)}`;
const record: ResumeImportSummary = {
  id: importId, filename: "resume.pdf", file_type: "pdf", size: 128, name: "Resume",
  status: "ready", error_code: null, resume_version_id: "file-resume",
  created_at: "2026-11-02T00:00:00Z", updated_at: "2026-11-02T00:00:00Z",
};
const imported: ResumeVersion = {
  id: "file-resume", version: 2, name: "Resume", notes: "", source: "pdf", import_id: importId,
  text: "Original CV_FILE_6", profile: { education: "Original education", projects: "Original project" },
  status: "draft", parent_version_id: null, created_at: record.created_at,
};
const original: ResumeVersion = { ...imported, id: "old", version: 1, import_id: null, source: "text", name: "Old resume", status: "confirmed" };
const callbacks = { upload: vi.fn(), retry: vi.fn(), save: vi.fn(), confirm: vi.fn() };

function Harness({ supported = true, failed = false }: { supported?: boolean; failed?: boolean }) {
  const [records, setRecords] = useState<ResumeVersion[]>([original]);
  const [imports, setImports] = useState<ResumeImportSummary[]>(failed ? [{ ...record, status: "failed", resume_version_id: null, error_code: "encrypted" }] : []);
  return <ResumeLibrary records={records} imports={imports} workspaceRef="workspace-a" activeVersionId="old" busy={false} importSupported={supported}
    onImport={async (file, requestId) => {
      callbacks.upload(file, requestId); setRecords([original, imported]); setImports([record]);
      return { import: record, resume: imported };
    }} onRetryImport={async (id) => {
      callbacks.retry(id); setRecords([original, imported]); setImports([record]);
      return { import: record, resume: imported };
    }} onSave={async (action, input) => {
      callbacks.save(action, input);
      const updated = { ...imported, ...input, id: imported.id }; setRecords([original, updated]); return updated;
    }} onConfirm={async (id) => {
      callbacks.confirm(id); const confirmed = { ...imported, status: "confirmed" as const }; setRecords([original, confirmed]); return confirmed;
    }} onRename={vi.fn()} />;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchCareerResumeImport).mockResolvedValue({ ...record, text: imported.text,
    lines: [{ number: 1, locator: "page=1,line=1", text: imported.text }], sections: [] });
});
afterEach(cleanup);

it("uploads into a reviewable draft, preserves the selected preparation and requires saving corrections before confirmation", async () => {
  render(<Harness />);
  const file = new File(["engineering-pdf"], "resume.pdf", { type: "application/pdf" });
  fireEvent.change(screen.getByLabelText("Choose resume file"), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: "Import file" }));
  const form = screen.getByRole("form", { name: "Resume editor" });
  await waitFor(() => expect(within(form).getByLabelText("Resume text")).toHaveValue(imported.text));
  expect(callbacks.upload).toHaveBeenCalledWith(file, expect.any(String));
  expect(callbacks.confirm).not.toHaveBeenCalled();
  const versions = screen.getByRole("list", { name: "Resume versions" });
  expect(within(versions).getByRole("button", { name: /Old resume/ })).toHaveTextContent("In use");
  expect(within(versions).getByRole("button", { name: /v2.*Resume/ })).toHaveTextContent("PDF file");
  fireEvent.change(within(form).getByLabelText("Skills", { exact: true }), { target: { value: "User-added Excel" } });
  fireEvent.change(within(form).getByLabelText("Resume text"), { target: { value: "User-corrected CV_FILE_6" } });
  expect(within(form).getByRole("button", { name: "Confirm use" })).toBeDisabled();
  fireEvent.click(within(form).getByRole("button", { name: "Save draft" }));
  await waitFor(() => expect(within(form).getByRole("button", { name: "Confirm use" })).toBeEnabled());
  expect(callbacks.save).toHaveBeenCalledWith("career.resume.edit", expect.objectContaining({ text: "User-corrected CV_FILE_6", profile: expect.objectContaining({ skills: "User-added Excel" }) }));
  fireEvent.click(screen.getByText("Extracted text and locations", { exact: true }));
  await screen.findByText("Page 1, line 1", { exact: true });
  expect(screen.getByText("Original CV_FILE_6", { exact: true })).toBeVisible();
  fireEvent.click(within(form).getByRole("button", { name: "Confirm use" }));
  expect(callbacks.confirm).toHaveBeenCalledWith(imported.id);
});

it("shows a persisted import failure and retries its stored original into one draft", async () => {
  render(<Harness failed />);
  expect(screen.getByText("The PDF is encrypted. Export an unencrypted copy and upload it.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Retry extraction" }));
  await waitFor(() => expect(screen.getByLabelText("Resume text")).toHaveValue(imported.text));
  expect(callbacks.retry).toHaveBeenCalledWith(importId);
  expect(callbacks.confirm).not.toHaveBeenCalled();
});

it("blocks import while unsaved resume changes exist and rejects an unsupported file before uploading", async () => {
  render(<Harness />);
  fireEvent.change(screen.getByLabelText("Choose resume file"), { target: { files: [new File(["text"], "resume.txt")] } });
  expect(screen.getByRole("alert")).toHaveTextContent("Choose a nonempty PDF or DOCX");
  expect(callbacks.upload).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("Resume text"), { target: { value: "Unsaved resume text" } });
  expect(screen.getByLabelText("Choose resume file")).toBeDisabled();
  expect(screen.getByText("Save the current resume changes before importing a file.")).toBeVisible();
  expect(screen.getByLabelText("Resume text")).toHaveValue("Unsaved resume text");
});

it("keeps an older host usable without exposing file import controls", () => {
  render(<Harness supported={false} />);
  expect(screen.getByRole("form", { name: "Resume editor" })).toBeVisible();
  expect(screen.queryByLabelText("Choose resume file")).not.toBeInTheDocument();
  expect(callbacks.upload).not.toHaveBeenCalled();
});
