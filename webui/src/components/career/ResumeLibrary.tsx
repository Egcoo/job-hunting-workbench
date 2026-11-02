import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { fmtDateTime } from "@/lib/format-time";
import type { ResumeAction, ResumeVersion, ResumeWrite, ResumeImportSummary, ResumeImportResult, ResumeSectionKey } from "@/lib/career-types";
import { ResumeImportPanel, ResumeImportSource } from "./ResumeImport";

interface Props {
  records: ResumeVersion[];
  activeVersionId?: string;
  busy: boolean;
  onSave: (action: ResumeAction, input: ResumeWrite) => Promise<ResumeVersion | undefined>;
  onConfirm: (id: string) => Promise<ResumeVersion | undefined>;
  onRename: (id: string, name: string, notes: string) => Promise<ResumeVersion | undefined>;
  workspaceRef: string;
  imports: ResumeImportSummary[];
  importSupported: boolean;
  onImport: (file: File, requestId: string) => Promise<ResumeImportResult | undefined>;
  onRetryImport: (id: string) => Promise<ResumeImportResult | undefined>;
}

export function ResumeLibrary({ records, activeVersionId, busy, onSave, onConfirm, onRename, workspaceRef, imports, importSupported, onImport, onRetryImport }: Props) {
  const { t } = useTranslation();
  const [id, setId] = useState("");
  const [revising, setRevising] = useState(false);
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [text, setText] = useState("");
  const [profile, setProfile] = useState<Record<string, unknown>>({});
  const current = records.find((item) => item.id === id);
  const origin = imports.find((item) => item.id === current?.import_id);
  const locked = current?.status === "confirmed" && !revising;
  const dirty = Boolean(current && (current.name !== name || current.notes !== notes || current.text !== text || JSON.stringify(current.profile) !== JSON.stringify(profile)));

  function open(record?: ResumeVersion) {
    setId(record?.id ?? ""); setRevising(false);
    setName(record?.name ?? ""); setNotes(record?.notes ?? ""); setText(record?.text ?? "");
    setProfile(record?.profile ?? {});
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (locked && current) {
      const record = await onRename(current.id, name, notes);
      if (record) open(record);
      return;
    }
    const action = !current ? "career.resume.create" : revising ? "career.resume.revise" : "career.resume.edit";
    const record = await onSave(action, {
      ...(current ? { id: current.id } : {}), name, notes, text,
      profile: current?.import_id ? profile : current?.text === text ? current.profile : {},
    });
    if (record) open(record);
  }

  return <div className="min-w-0 space-y-5">
    {importSupported ? <ResumeImportPanel imports={imports} busy={busy} blocked={dirty || (!current && !!(name.trim() || text.trim() || notes.trim()))}
      onImport={async (file, requestId) => { const result = await onImport(file, requestId); if (result?.resume) open(result.resume); return result; }}
      onRetry={async (importId) => { const result = await onRetryImport(importId); if (result?.resume) open(result.resume); return result; }}
      onOpen={(versionId) => { const record = records.find((item) => item.id === versionId); if (record) open(record); }} /> : null}
    <div className="grid min-w-0 gap-5 lg:grid-cols-[13rem_minmax(0,1fr)]">
    <aside className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{t("career.resumeVersions")}</span><span>{t("career.resumeCount", { count: records.length })}</span></div>
      <Button variant="outline" onClick={() => open()} disabled={busy} className="w-full">{t("career.addResume")}</Button>
      {records.length === 0 ? <p className="text-sm text-muted-foreground">{t("career.noResumes")}</p> :
        <ul aria-label={t("career.resumeVersions")} className="space-y-2">{records.map((record) => <li key={record.id}>
          <button type="button" onClick={() => open(record)} disabled={busy} aria-pressed={id === record.id}
            className={`w-full rounded-xl border p-3 text-left disabled:opacity-50 ${id === record.id ? "border-primary bg-accent" : "border-border hover:bg-accent/50"}`}>
            <span className="block break-words font-medium">v{record.version} · {record.name}</span>
            <span className="mt-1 block text-xs text-muted-foreground">{t(`career.${record.status}`)} · {t(record.source === "pdf" || record.source === "docx" ? "career.fileSource" : "career.textSource", { format: record.source?.toUpperCase() })}</span>
            <span className="mt-1 block break-words text-xs text-muted-foreground">{t("career.addedAt")} <time dateTime={record.created_at}>{fmtDateTime(record.created_at)}</time></span>
            {record.id === activeVersionId ? <span className="mt-2 inline-block rounded-full bg-orange-100 px-2 py-0.5 text-xs text-orange-900 dark:bg-orange-200/10 dark:text-orange-200">{t("career.currentUse")}</span> : null}
            {record.notes ? <span className="mt-2 block break-words text-xs leading-relaxed text-muted-foreground">{record.notes}</span> : null}
          </button>
        </li>)}</ul>}
    </aside>
    <form onSubmit={(event) => { void save(event); }} className="min-w-0 space-y-4 rounded-2xl border bg-muted/20 p-4 sm:p-5" aria-label={t("career.resumeEditor")}>
      <h3 className="text-lg font-semibold">{current ? `${t("career.resume")} · v${current.version}${revising ? ` · ${t("career.newVersion")}` : ""}` : t("career.addResume")}</h3>
      <p className="text-xs leading-relaxed text-muted-foreground">{t("career.viewOnlyHint")}</p>
      <label className="block space-y-1.5 text-sm"><span>{t("career.name")}</span>
        <Input value={name} onChange={(event) => setName(event.target.value)} required maxLength={500} disabled={busy} />
      </label>
      {origin && importSupported ? <>
        <ResumeImportSource key={`${workspaceRef}:${origin.id}`} workspaceRef={workspaceRef} record={origin} />
        <fieldset className="min-w-0 space-y-3 rounded-lg border p-3">
          <legend className="px-1 text-sm font-medium">{t("career.reviewedFields")}</legend>
          <p className="text-xs leading-relaxed text-muted-foreground">{t("career.reviewedFieldsHint")}</p>
          {(["education", "projects", "skills", "experience", "summary"] as ResumeSectionKey[]).filter((key) => ["education", "projects", "skills"].includes(key) || profile[key]).map((key) => <label key={key} className="block space-y-1.5 text-sm">
            <span>{t(`career.resumeFields.${key}`)}</span><Textarea aria-label={t(`career.resumeFields.${key}`)} value={typeof profile[key] === "string" ? profile[key] : ""}
              maxLength={200000} readOnly={locked} disabled={busy} className="min-h-20" onChange={(event) => setProfile((value) => ({ ...value, [key]: event.target.value }))} />
          </label>)}
        </fieldset>
      </> : null}
      <label className="block space-y-1.5 text-sm"><span>{t("career.notes")}</span>
        <Input value={notes} onChange={(event) => setNotes(event.target.value)} maxLength={5000} disabled={busy} />
      </label>
      <label className="block space-y-1.5 text-sm"><span>{t("career.resumeText")}</span>
        <Textarea aria-label={t("career.resumeText")} value={text} onChange={(event) => setText(event.target.value)} required maxLength={200000}
          readOnly={locked} disabled={busy} className="min-h-64 resize-y" />
      </label>
      <p className="text-xs leading-relaxed text-muted-foreground">{t(locked ? "career.confirmedHint" : importSupported ? "career.resumeDraftHint" : "career.textHint")}</p>
      {current?.status === "draft" && dirty ? <p className="text-sm text-muted-foreground">{t("career.saveBeforeConfirm")}</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={busy || !name.trim() || !text.trim()}>{t(locked ? "career.saveLabels" : "career.saveDraft")}</Button>
        {locked ? <Button type="button" variant="outline" disabled={busy} onClick={() => setRevising(true)}>{t("career.saveNewVersion")}</Button> : null}
        {current?.status === "draft" && !revising ? <Button type="button" variant="outline" disabled={busy || dirty}
          onClick={() => { void onConfirm(current.id); }}>{t("career.confirm")}</Button> : null}
      </div>
    </form>
    </div>
  </div>;
}
