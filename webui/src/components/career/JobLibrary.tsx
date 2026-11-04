import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { fmtDateTime } from "@/lib/format-time";
import type { JobAction, JobVersion, JobWrite, JobImageDetail, JobImageResult, JobImageSummary } from "@/lib/career-types";
import { JobImageImportPanel, JobImageSource } from "./JobImageImport";

interface Props {
  records: JobVersion[];
  activeVersionId?: string;
  busy: boolean;
  onSave: (action: JobAction, input: JobWrite) => Promise<JobVersion | undefined>;
  onConfirm: (id: string) => Promise<JobVersion | undefined>;
  workspaceRef: string; images: JobImageSummary[]; imageSupported: boolean; recognitionAvailable: boolean;
  onImportImage: (file: File, requestId: string) => Promise<JobImageResult | undefined>;
  onRetryImage: (id: string) => Promise<JobImageResult | undefined>;
  onReadImage: (id: string) => Promise<JobImageDetail | undefined>;
  onSaveImage: (id: string, manual: boolean, input: JobWrite) => Promise<JobVersion | undefined>;
}

export function JobLibrary({ records, activeVersionId, busy, onSave, onConfirm, workspaceRef, images, imageSupported, recognitionAvailable, onImportImage, onRetryImage, onReadImage, onSaveImage }: Props) {
  const { t } = useTranslation();
  const [id, setId] = useState("");
  const [revising, setRevising] = useState(false);
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [text, setText] = useState("");
  const [pendingImage, setPendingImage] = useState<JobImageDetail | null>(null);
  const [manual, setManual] = useState(false);
  const [locations, setLocations] = useState("");
  const [days, setDays] = useState("");
  const [months, setMonths] = useState("");
  const [responsibilities, setResponsibilities] = useState("");
  const [qualifications, setQualifications] = useState("");
  const current = records.find((item) => item.id === id);
  const imageRecord = pendingImage ?? images.find((item) => item.id === current?.import_id);
  const imageJob = !!pendingImage || current?.source === "image";
  const locked = current?.status === "confirmed" && !revising;
  function requirements(): Record<string, unknown> {
    return { locations: locations.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean), responsibilities, qualifications,
      ...(days ? { days_per_week: Number(days) } : {}), ...(months ? { available_months: Number(months) } : {}) };
  }
  const dirty = Boolean(current && (current.name !== name || (current.company ?? "") !== company || current.text !== text
    || (imageJob && JSON.stringify(current.requirements) !== JSON.stringify(requirements()))));

  function openRequirements(value: Record<string, unknown>) {
    setLocations(Array.isArray(value.locations) ? value.locations.filter((item) => typeof item === "string").join("，") : "");
    setDays(typeof value.days_per_week === "number" ? String(value.days_per_week) : "");
    setMonths(typeof value.available_months === "number" ? String(value.available_months) : "");
    setResponsibilities(typeof value.responsibilities === "string" ? value.responsibilities : "");
    setQualifications(typeof value.qualifications === "string" ? value.qualifications : "");
  }

  function open(record?: JobVersion) {
    setId(record?.id ?? ""); setRevising(false);
    setName(record?.name ?? ""); setCompany(record?.company ?? ""); setText(record?.text ?? "");
    setPendingImage(null); setManual(false); openRequirements(record?.requirements ?? {});
  }
  function openImage(record: JobImageDetail, manualEntry: boolean) {
    setId(""); setRevising(false); setPendingImage(record); setManual(manualEntry || record.status !== "ready");
    const fields = record.reading?.fields;
    setName(fields?.name?.value ?? ""); setCompany(fields?.company?.value ?? ""); setText(record.reading?.text ?? "");
    openRequirements({ locations: fields?.locations.map((item) => item.value) ?? [],
      responsibilities: fields?.responsibilities.map((item) => item.value).join("\n") ?? "",
      qualifications: fields?.qualifications.map((item) => item.value).join("\n") ?? "",
      days_per_week: fields?.days_per_week?.value, available_months: fields?.available_months?.value });
  }
  function receiveImage(result: JobImageResult) {
    if (result.job) open(result.job); else openImage(result.import, false);
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    const action = !current ? "career.job.create" : revising ? "career.job.revise" : "career.job.edit";
    const input: JobWrite = {
      ...(current ? { id: current.id } : {}), name, company: company.trim() || null, text,
      requirements: imageJob ? requirements() : current?.text === text ? current.requirements : {},
    };
    const record = pendingImage ? await onSaveImage(pendingImage.id, manual, input) : await onSave(action, input);
    if (record) open(record);
  }

  return <div className="min-w-0 space-y-5">
    {imageSupported ? <JobImageImportPanel records={images} busy={busy} available={recognitionAvailable}
      blocked={dirty || (!current && !!(name.trim() || company.trim() || text.trim()))}
      onImport={onImportImage} onRetry={onRetryImage} onResult={receiveImage}
      onJob={(versionId) => { const record = records.find((item) => item.id === versionId); if (record) open(record); }}
      onOpen={async (importId, manualEntry) => { const record = await onReadImage(importId); if (record) openImage(record, manualEntry); }} /> : null}
    <div className="grid min-w-0 gap-5 lg:grid-cols-[13rem_minmax(0,1fr)]">
    <aside className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{t("career.jobVersions")}</span><span>{t("career.resumeCount", { count: records.length })}</span></div>
      <Button variant="outline" onClick={() => open()} disabled={busy} className="w-full">{t("career.addJob")}</Button>
      {records.length === 0 ? <p className="text-sm text-muted-foreground">{t("career.noJobs")}</p> :
        <ul aria-label={t("career.jobVersions")} className="space-y-2">{records.map((record) => <li key={record.id}>
          <button type="button" onClick={() => open(record)} disabled={busy} aria-pressed={id === record.id}
            className={`w-full rounded-xl border p-3 text-left disabled:opacity-50 ${id === record.id ? "border-primary bg-accent" : "border-border hover:bg-accent/50"}`}>
            <span className="block break-words font-medium">{record.name} · v{record.version}</span>
            {record.company ? <span className="block break-words text-xs text-muted-foreground">{record.company}</span> : null}
            <span className="text-xs text-muted-foreground">{t(`career.${record.status}`)} · {t(record.source === "image" ? "career.imageSourceTag" : "career.textSource")}</span>
            <span className="mt-1 block break-words text-xs text-muted-foreground">{t("career.addedAt")} <time dateTime={record.created_at}>{fmtDateTime(record.created_at)}</time></span>
            {record.id === activeVersionId ? <span className="mt-2 block w-fit rounded-full bg-orange-100 px-2 py-0.5 text-xs text-orange-900 dark:bg-orange-200/10 dark:text-orange-200">{t("career.currentUse")}</span> : null}
          </button>
        </li>)}</ul>}
    </aside>
    <form onSubmit={(event) => { void save(event); }} className="min-w-0 space-y-4 rounded-2xl border bg-muted/20 p-4 sm:p-5" aria-label={t("career.jobEditor")}>
      <h3 className="text-lg font-semibold">{current ? `${t("career.job")} · v${current.version}${revising ? ` · ${t("career.newVersion")}` : ""}` : t(pendingImage ? "career.imageReviewDraft" : "career.addJob")}</h3>
      <p className="text-xs leading-relaxed text-muted-foreground">{t("career.viewOnlyHint")}</p>
      {pendingImage && manual ? <p className="text-sm text-muted-foreground">{t("career.imageManualHint")}</p> : null}
      {imageRecord && imageSupported ? <JobImageSource key={`${workspaceRef}:${imageRecord.id}`} workspaceRef={workspaceRef} record={imageRecord} /> : null}
      <label className="block space-y-1.5 text-sm"><span>{t("career.jobName")}</span>
        <Input value={name} onChange={(event) => setName(event.target.value)} required maxLength={500} disabled={busy || locked} />
      </label>
      <label className="block space-y-1.5 text-sm"><span>{t("career.company")}</span>
        <Input value={company} onChange={(event) => setCompany(event.target.value)} maxLength={500} disabled={busy || locked} />
      </label>
      {imageJob && imageSupported ? <fieldset className="min-w-0 space-y-3 rounded-lg border p-3">
        <legend className="px-1 text-sm font-medium">{t("career.jobRequirements")}</legend>
        <p className="text-xs leading-relaxed text-muted-foreground">{t("career.jobRequirementHint")}</p>
        <label className="block space-y-1.5 text-sm"><span>{t("career.jobLocations")}</span><Input value={locations} maxLength={5000} disabled={busy} readOnly={locked} onChange={(event) => setLocations(event.target.value)} /></label>
        <div className="grid min-w-0 gap-3 sm:grid-cols-2">
          <label className="block space-y-1.5 text-sm"><span>{t("career.jobDays")}</span><Input type="number" min={1} max={7} step={1} value={days} disabled={busy} readOnly={locked} onChange={(event) => setDays(event.target.value)} /></label>
          <label className="block space-y-1.5 text-sm"><span>{t("career.jobMonths")}</span><Input type="number" min={1} max={60} step={1} value={months} disabled={busy} readOnly={locked} onChange={(event) => setMonths(event.target.value)} /></label>
        </div>
        <label className="block space-y-1.5 text-sm"><span>{t("career.jobResponsibilities")}</span><Textarea aria-label={t("career.jobResponsibilities")} value={responsibilities} maxLength={30000} disabled={busy} readOnly={locked} className="min-h-24" onChange={(event) => setResponsibilities(event.target.value)} /></label>
        <label className="block space-y-1.5 text-sm"><span>{t("career.jobQualifications")}</span><Textarea aria-label={t("career.jobQualifications")} value={qualifications} maxLength={30000} disabled={busy} readOnly={locked} className="min-h-24" onChange={(event) => setQualifications(event.target.value)} /></label>
      </fieldset> : null}
      <label className="block space-y-1.5 text-sm"><span>{t("career.jobText")}</span>
        <Textarea aria-label={t("career.jobText")} value={text} onChange={(event) => setText(event.target.value)} required maxLength={200000}
          readOnly={locked} disabled={busy} className="min-h-64 resize-y" />
      </label>
      <p className="text-xs leading-relaxed text-muted-foreground">{t(locked ? "career.confirmedHint" : imageSupported ? "career.imageDraftHint" : "career.textHint")}</p>
      {current?.status === "draft" && dirty ? <p className="text-sm text-muted-foreground">{t("career.saveBeforeConfirm")}</p> : null}
      <div className="flex flex-wrap gap-2">
        {!locked ? <Button type="submit" disabled={busy || !name.trim() || !text.trim()}>{t("career.saveDraft")}</Button> : null}
        {locked ? <Button type="button" variant="outline" disabled={busy} onClick={() => setRevising(true)}>{t("career.saveNewVersion")}</Button> : null}
        {current?.status === "draft" && !revising ? <Button type="button" variant="outline" disabled={busy || dirty}
          onClick={() => { void onConfirm(current.id); }}>{t("career.confirm")}</Button> : null}
      </div>
    </form>
    </div>
  </div>;
}
