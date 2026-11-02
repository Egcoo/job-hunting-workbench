import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useClient } from "@/providers/ClientProvider";
import { fetchCareerResumeImport, fetchCareerResumeOriginal } from "@/lib/career-api";
import { fmtDateTime } from "@/lib/format-time";
import type { ResumeImportDetail, ResumeImportResult, ResumeImportSummary } from "@/lib/career-types";

export function ResumeImportPanel({ imports, busy, blocked, onImport, onRetry, onOpen }: {
  imports: ResumeImportSummary[]; busy: boolean; blocked: boolean;
  onImport: (file: File, requestId: string) => Promise<ResumeImportResult | undefined>;
  onRetry: (id: string) => Promise<ResumeImportResult | undefined>;
  onOpen: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [inputVersion, setInputVersion] = useState(0);
  const [working, setWorking] = useState(false);
  const [invalid, setInvalid] = useState(false);
  return <section aria-label={t("career.resumeImport")} className="min-w-0 space-y-3 rounded-2xl border p-4 sm:p-5">
    <h3 className="font-medium">{t("career.resumeImport")}</h3>
    <p className="text-sm leading-relaxed text-muted-foreground">{t("career.resumeImportHint")}</p>
    {blocked ? <p role="status" className="text-sm text-muted-foreground">{t("career.saveBeforeImport")}</p> : null}
    <div className="flex flex-wrap items-end gap-3">
      <label className="min-w-0 flex-1 space-y-1.5 text-sm"><span>{t("career.selectResumeFile")}</span>
        <Input key={inputVersion} type="file" accept=".pdf,.docx" disabled={busy || blocked} onChange={(event) => {
          const selected = event.target.files?.[0] ?? null;
          const rejected = !!selected && (!/\.(pdf|docx)$/i.test(selected.name) || selected.size === 0 || selected.size > 6 * 1024 * 1024);
          setInvalid(rejected); setFile(rejected ? null : selected); setRequestId(crypto.randomUUID());
        }} />
      </label>
      <Button type="button" disabled={busy || blocked || !file} onClick={async () => {
        if (!file) return;
        setWorking(true);
        try {
          const result = await onImport(file, requestId);
          if (result) { setFile(null); setInputVersion((value) => value + 1); setRequestId(crypto.randomUUID()); }
        } finally { setWorking(false); }
      }}>{t(working ? "career.importWorking" : "career.importFile")}</Button>
    </div>
    {file ? <p className="break-words text-xs text-muted-foreground">{file.name}</p> : null}
    {invalid ? <p role="alert" className="text-sm text-destructive">{t("career.importFileInvalid")}</p> : null}
    {imports.length > 0 ? <details open={imports.some((item) => item.status !== "ready")} className="text-sm"><summary className="cursor-pointer">{t("career.importHistory", { count: imports.length })}</summary>
      <ul className="mt-3 space-y-3">{imports.map((item) => <li key={item.id} className="min-w-0 space-y-2 rounded-lg border p-3">
        <p className="break-words font-medium">{item.filename} · {t(`career.importStatus.${item.status}`)}</p>
        <p className="text-xs text-muted-foreground"><time dateTime={item.created_at}>{fmtDateTime(item.created_at)}</time></p>
        {item.error_code ? <p role="status" className="text-sm text-destructive">{t(`career.importErrors.${item.error_code}`)}</p> : null}
        {item.status === "processing" ? <p className="text-xs text-muted-foreground">{t("career.importRecoveryHint")}</p> : null}
        {item.resume_version_id ? <Button type="button" size="sm" variant="outline" disabled={busy || blocked} onClick={() => onOpen(item.resume_version_id!)}>{t("career.openImportedResume")}</Button> :
          <Button type="button" size="sm" variant="outline" disabled={busy || blocked} onClick={async () => { await onRetry(item.id); }}>{t("career.retryImport")}</Button>}
      </li>)}</ul>
    </details> : null}
  </section>;
}

export function ResumeImportSource({ workspaceRef, record }: { workspaceRef: string; record: ResumeImportSummary }) {
  const { t } = useTranslation();
  const { getToken } = useClient();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<ResumeImportDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const download = useRef<AbortController | null>(null);
  useEffect(() => () => download.current?.abort(), []);
  function location(line: ResumeImportDetail["lines"][number]): string {
    const page = /^page=(\d+),line=(\d+)$/.exec(line.locator);
    const paragraph = /^paragraph=(\d+)$/.exec(line.locator);
    if (page) return t("career.pdfSourceLocation", { page: page[1], line: page[2] });
    if (paragraph) return t("career.docxSourceLocation", { paragraph: paragraph[1] });
    return t("career.sourceLine", { number: line.number });
  }
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setFailed(false); setDetail(null);
    void fetchCareerResumeImport(getToken(), workspaceRef, record.id, controller.signal).then((value) => {
      if (!controller.signal.aborted) setDetail(value);
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [open, getToken, workspaceRef, record.id]);
  return <section className="min-w-0 space-y-3 rounded-lg border p-3" aria-label={t("career.importSource")}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="break-words text-sm">{t("career.originalFile")}: {record.filename}</p>
      <Button type="button" size="sm" variant="outline" disabled={downloading} onClick={async () => {
        setDownloading(true); setFailed(false);
        const controller = new AbortController(); download.current = controller;
        try {
          const blob = await fetchCareerResumeOriginal(getToken(), workspaceRef, record.id, controller.signal);
          if (controller.signal.aborted) return;
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a"); link.href = url; link.download = record.filename;
          link.click(); URL.revokeObjectURL(url);
        } catch { if (!controller.signal.aborted) setFailed(true); } finally { if (!controller.signal.aborted) setDownloading(false); }
      }}>{t("career.downloadOriginal")}</Button>
    </div>
    <p className="text-xs leading-relaxed text-muted-foreground">{t("career.importReviewHint")}</p>
    {failed ? <p role="alert" className="text-sm text-destructive">{t("career.importSourceError")}</p> : null}
    <details onToggle={(event) => setOpen(event.currentTarget.open)}><summary className="cursor-pointer text-sm">{t("career.extractedOriginal")}</summary>
      {open && !detail && !failed ? <p role="status" className="mt-2 text-sm">{t("career.loading")}</p> : null}
      {detail ? <ol className="mt-3 max-h-80 space-y-2 overflow-auto text-xs">{detail.lines.map((line) => <li key={line.number} className="min-w-0">
        <span className="text-muted-foreground">{location(line)}</span>
        <p className="whitespace-pre-wrap break-words leading-relaxed">{line.text}</p>
      </li>)}</ol> : null}
    </details>
  </section>;
}
