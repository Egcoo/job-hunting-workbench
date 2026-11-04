import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ImageLightbox } from "@/components/ImageLightbox";
import { useClient } from "@/providers/ClientProvider";
import { fetchCareerJobImage, fetchCareerJobImageOriginal } from "@/lib/career-api";
import { fmtDateTime } from "@/lib/format-time";
import type { JobImageDetail, JobImageResult, JobImageSummary } from "@/lib/career-types";

export function JobImageImportPanel({ records, busy, blocked, available, onImport, onRetry, onResult, onOpen, onJob }: {
  records: JobImageSummary[]; busy: boolean; blocked: boolean; available: boolean;
  onImport: (file: File, requestId: string) => Promise<JobImageResult | undefined>;
  onRetry: (id: string) => Promise<JobImageResult | undefined>;
  onResult: (result: JobImageResult) => void;
  onOpen: (id: string, manual: boolean) => Promise<void>; onJob: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [inputVersion, setInputVersion] = useState(0);
  const [working, setWorking] = useState(false);
  const [invalid, setInvalid] = useState(false);
  return <section aria-label={t("career.jobImageImport")} className="min-w-0 space-y-3 rounded-2xl border p-4 sm:p-5">
    <h3 className="font-medium">{t("career.jobImageImport")}</h3>
    <p className="text-sm leading-relaxed text-muted-foreground">{t("career.jobImageHint")}</p>
    {!available ? <p className="text-sm text-muted-foreground">{t("career.jobImageServiceUnavailable")}</p> : null}
    {blocked ? <p role="status" className="text-sm text-muted-foreground">{t("career.jobImageSaveBeforeNext")}</p> : null}
    <div className="flex flex-wrap items-end gap-3">
      <label className="min-w-0 flex-1 space-y-1.5 text-sm"><span>{t("career.selectJobImage")}</span>
        <Input key={inputVersion} type="file" accept=".png,.jpg,.jpeg,.webp" disabled={busy || blocked} onChange={(event) => {
          const selected = event.target.files?.[0] ?? null;
          const rejected = !!selected && (!/\.(png|jpe?g|webp)$/i.test(selected.name) || selected.size === 0 || selected.size > 6 * 1024 * 1024);
          setInvalid(rejected); setFile(rejected ? null : selected); setRequestId(crypto.randomUUID());
        }} />
      </label>
      <Button type="button" disabled={busy || blocked || !file} onClick={async () => {
        if (!file) return;
        setWorking(true);
        try {
          const result = await onImport(file, requestId);
          if (result) {
            setFile(null); setInputVersion((value) => value + 1); setRequestId(crypto.randomUUID());
            if (result.import.status === "ready" || result.job) onResult(result);
          }
        } finally { setWorking(false); }
      }}>{t(working ? "career.recognizeWorking" : "career.recognizeJobImage")}</Button>
    </div>
    {file ? <p className="break-words text-xs text-muted-foreground">{file.name}</p> : null}
    {invalid ? <p role="alert" className="text-sm text-destructive">{t("career.jobImageFileInvalid")}</p> : null}
    {records.length ? <details open={records.some((item) => !item.job_version_id)} className="text-sm">
      <summary className="cursor-pointer">{t("career.jobImageHistory", { count: records.length })}</summary>
      <ul className="mt-3 space-y-3">{records.map((item) => <li key={item.id} className="min-w-0 space-y-2 rounded-lg border p-3">
        <p className="break-words font-medium">{item.filename} · {t(`career.jobImageStatus.${item.status}`)}{item.manual ? ` · ${t("career.manualSaved")}` : ""}</p>
        <p className="text-xs text-muted-foreground"><time dateTime={item.created_at}>{fmtDateTime(item.created_at)}</time></p>
        {item.error_code ? <p role="status" className="text-sm text-destructive">{t(`career.jobImageErrors.${item.error_code}`)}</p> : null}
        <div className="flex flex-wrap gap-2">{item.job_version_id ? <Button type="button" size="sm" variant="outline" disabled={busy || blocked} onClick={() => onJob(item.job_version_id!)}>{t("career.viewImageJob")}</Button> : <>
          {item.status === "ready" ? <Button type="button" size="sm" variant="outline" disabled={busy || blocked} onClick={() => { void onOpen(item.id, false); }}>{t("career.viewImageResult")}</Button> : <Button type="button" size="sm" variant="outline" disabled={busy || blocked} onClick={async () => {
            const result = await onRetry(item.id); if (result?.import.status === "ready" || result?.job) onResult(result);
          }}>{t("career.retryRecognition")}</Button>}
          <Button type="button" size="sm" variant="ghost" disabled={busy || blocked} onClick={() => { void onOpen(item.id, true); }}>{t("career.manualEntry")}</Button>
        </>}</div>
      </li>)}</ul>
    </details> : null}
  </section>;
}

export function JobImageSource({ workspaceRef, record }: { workspaceRef: string; record: JobImageSummary }) {
  const { t } = useTranslation();
  const { getToken } = useClient();
  const [source, setSource] = useState<{ detail: JobImageDetail | null; url: string; failed: boolean }>({ detail: null, url: "", failed: false });
  const [epoch, setEpoch] = useState(0);
  const [zoom, setZoom] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let objectURL = "";
    setSource({ detail: null, url: "", failed: false }); setZoom(false);
    void Promise.allSettled([
      fetchCareerJobImage(getToken(), workspaceRef, record.id, controller.signal),
      fetchCareerJobImageOriginal(getToken(), workspaceRef, record.id, controller.signal),
    ]).then(([detail, image]) => {
      if (controller.signal.aborted) return;
      const mime = record.file_type === "jpg" ? "image/jpeg" : `image/${record.file_type}`;
      if (image.status === "fulfilled") objectURL = URL.createObjectURL(new Blob([image.value], { type: mime }));
      setSource({ detail: detail.status === "fulfilled" ? detail.value : null, url: objectURL,
        failed: detail.status === "rejected" || image.status === "rejected" });
    });
    return () => { controller.abort(); if (objectURL) URL.revokeObjectURL(objectURL); };
  }, [getToken, workspaceRef, record.id, record.file_type, epoch]);
  const reading = source.detail?.reading;
  const model = source.detail?.model ?? record.model;
  const validImage = (source.detail?.width ?? record.width) !== null && !!source.url;
  return <section aria-label={t("career.jobImageSource")} className="min-w-0 space-y-3 rounded-lg border p-3">
    <p className="break-words text-sm">{t("career.originalFile")}: {record.filename}</p>
    {model ? <p className="break-words text-xs text-muted-foreground">{t("career.sourceRequestedModel", { model })}</p> : null}
    {record.manual ? <p className="text-sm text-muted-foreground">{t("career.imageManualHint")}</p> : null}
    {reading?.warnings.length ? <div role="note" className="space-y-1 text-sm text-orange-800 dark:text-orange-200"><p>{t("career.sourceWarnings")}</p><ul className="list-disc space-y-1 pl-4">{reading.warnings.map((warning, index) => <li key={index} className="break-words">{warning}</li>)}</ul></div> : null}
    {source.failed ? <div className="space-y-2"><p role="alert" className="text-sm text-destructive">{t("career.sourceImageError")}</p><Button type="button" size="sm" variant="outline" onClick={() => setEpoch((value) => value + 1)}>{t("career.sourceImageReload")}</Button></div> : null}
    <details><summary className="cursor-pointer text-sm">{t("career.jobImageCompare")}</summary>
      <div className="mt-3 grid min-w-0 gap-4 lg:grid-cols-2">
        {validImage ? <button type="button" className="min-w-0 rounded-lg border p-1" aria-label={t("career.jobImageZoom")} onClick={() => setZoom(true)}>
          <img src={source.url} alt={t("career.jobImageOriginal", { filename: record.filename })} className="max-h-96 w-full object-contain" />
        </button> : <p className="text-sm text-muted-foreground">{source.failed || record.width === null ? t("career.noRecognitionText") : t("career.loading")}</p>}
        <div className="min-w-0 space-y-2"><p className="text-sm font-medium">{t("career.recognitionText")}</p>
          <p className="max-h-96 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed">{reading?.text || t("career.noRecognitionText")}</p>
        </div>
      </div>
      {source.url ? <Button type="button" variant="outline" size="sm" className="mt-3" onClick={() => {
        const link = document.createElement("a"); link.href = source.url; link.download = record.filename; link.click();
      }}>{t("career.downloadOriginal")}</Button> : null}
    </details>
    <ImageLightbox images={source.url ? [{ url: source.url, name: t("career.jobImageOriginal", { filename: record.filename }) }] : []}
      index={zoom ? 0 : null} onIndexChange={() => setZoom(true)} onOpenChange={setZoom} />
  </section>;
}
