"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError } from "@/lib/api/client";
import {
  documentContentUrl,
  readDocuments,
  reprocessDocument,
  uploadDocument,
  type AppDocument,
  type DocumentStatus,
} from "@/lib/api/documents";
import { formatDate, formatNumber } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { Card } from "@/lib/ui/card";
import { SelectField, TextField } from "@/lib/ui/field";

/**
 * The document list and its upload control.
 *
 * Three rules from the ticket, each of them a thing the screen has to *show* rather
 * than merely know:
 *
 * 1. **Status and progress.** An upload returns immediately with the document in
 *    `processing`, so the screen has to say so and then show it becoming `ready`.
 *    How far along the pipeline is comes from the server (`stage`/`total_stages`) and
 *    is rendered as text inside a real `<progress>`; a bar the client computed from
 *    the status string would be a second implementation of the pipeline's shape.
 *    While anything is `processing` the list re-reads itself, which is what makes the
 *    bar move without the reader refreshing — and it stops when nothing is left to
 *    wait for, so an idle page makes no requests.
 *
 * 2. **The failure reason.** A document that failed shows *why*, in the pipeline's own
 *    words: `no text extracted; upload a text version` for a scan. That sentence is
 *    shown as it is rather than translated, because it is a fact about the file.
 *
 * 3. **Everything is a word.** Status, progress and the failure reason are all text
 *    (design system §5): a colour alone never carries a state.
 *
 * The file input is labelled, the form refuses an empty submit locally, and a refusal
 * from the API is rendered through the catalogue key when the frontend knows it — a
 * bilingual prompt rather than the API's Spanish sentence.
 */
export function DocumentScreen({
  dict,
  locale,
  initialItems,
  total,
  failed,
}: {
  dict: Dictionary;
  locale: Locale;
  initialItems: AppDocument[];
  total: number;
  failed: boolean;
}) {
  const t = dict.documents;
  const router = useRouter();
  const [items, setItems] = useState(initialItems);
  const [count, setCount] = useState(total);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  // The server is the source of truth: our refresh, or the browser's after a
  // navigation, replaces what is on screen.
  useEffect(() => {
    setItems(initialItems);
    setCount(total);
  }, [initialItems, total]);

  const pending = items.filter((item) => item.status === "processing").length;

  // Only while something is being parsed. A page with nothing pending makes no
  // requests at all, which is what keeps an idle tab from being a poll.
  useEffect(() => {
    if (pending === 0) return;
    const timer = setTimeout(async () => {
      try {
        const page = await readDocuments();
        setItems(page.items);
        setCount(page.total);
      } catch {
        // A failed refresh is not worth a message: the next tick tries again, and the
        // list on screen is still the last one the server gave us.
      }
    }, 1500);
    return () => clearTimeout(timer);
  }, [pending, items]);

  async function reprocess(id: string) {
    setBusyId(id);
    setError(null);
    setNotice(null);
    try {
      const updated = await reprocessDocument(id);
      setItems((current) => current.map((item) => (item.id === id ? updated : item)));
      router.refresh();
    } catch (cause) {
      setError(errorText(dict, cause));
    } finally {
      setBusyId(null);
    }
  }

  function accepted(document: AppDocument) {
    setNotice(t.upload.queued);
    setItems((current) => [document, ...current]);
    setCount((current) => current + 1);
    router.refresh();
  }

  return (
    <div className="flex flex-col gap-8">
      <UploadCard dict={dict} locale={locale} onAccepted={accepted} onError={setError} />

      {error && (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      )}
      {notice && (
        <Alert tone="success" role="status">
          {notice}
        </Alert>
      )}

      <section aria-labelledby="documents-heading" className="flex flex-col gap-4">
        <h2 id="documents-heading" className="text-lg font-semibold">
          {t.listHeading
            .replace("{shown}", formatNumber(items.length, locale))
            .replace("{total}", formatNumber(count, locale))}
        </h2>

        {failed ? (
          <Alert tone="danger" role="alert" title={t.error}>
            <p className="mt-1">{t.emptyHint}</p>
            <Button
              variant="secondary"
              size="sm"
              className="mt-3"
              onClick={() => router.refresh()}
            >
              {t.retry}
            </Button>
          </Alert>
        ) : items.length === 0 ? (
          <Alert tone="neutral">{t.emptyHint}</Alert>
        ) : (
          <ul className="flex flex-col gap-3" data-testid="document-list">
            {items.map((document) => (
              <li
                key={document.id}
                className="rounded-lg border border-border bg-surface p-4 shadow-sm"
                data-document-status={document.status}
              >
                <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                  <div className="min-w-0">
                    <p className="font-medium">{document.title}</p>
                    <p className="mt-1 text-sm text-fg-muted">
                      {metaLine(document, dict, locale)}
                    </p>
                    {/* The state in words, and the progress as text inside a real
                        progress element: a bar on its own says nothing to a reader
                        who cannot see it. */}
                    <p className="mt-1 flex flex-wrap items-center gap-2 text-sm">
                      <span
                        className="tabular rounded bg-neutral-bg px-2 py-0.5 font-medium text-neutral"
                        data-testid="document-status"
                      >
                        {t.status[document.status as DocumentStatus]}
                      </span>
                      <span className="text-fg-subtle">
                        {t.progress
                          .replace("{stage}", formatNumber(document.stage, locale))
                          .replace("{total}", formatNumber(document.total_stages, locale))}
                      </span>
                    </p>
                    <progress
                      className="mt-2 h-2 w-full max-w-sm"
                      aria-label={t.progressLabel}
                      value={document.stage}
                      max={document.total_stages}
                    />
                    {document.status === "ready" && (
                      <p className="mt-1 text-sm text-fg-subtle">
                        {t.parsed
                          .replace(
                            "{pages}",
                            document.page_count === null
                              ? "—"
                              : formatNumber(document.page_count, locale),
                          )
                          .replace("{chunks}", formatNumber(document.chunk_count, locale))
                          .replace(
                            "{characters}",
                            formatNumber(document.extracted_chars, locale),
                          )}
                      </p>
                    )}
                    {document.status === "failed" && document.failure_reason && (
                      <p className="mt-1 text-sm text-danger" data-testid="document-failure">
                        {t.failureReason}: {document.failure_reason}
                      </p>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <a
                      className="rounded border border-border px-3 py-1.5 text-sm text-fg hover:bg-neutral-bg"
                      href={documentContentUrl(document.id)}
                    >
                      {t.download}
                    </a>
                    {(document.status === "ready" || document.status === "failed") && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => reprocess(document.id)}
                        disabled={busyId !== null}
                      >
                        {busyId === document.id ? t.reprocessing : t.reprocess}
                      </Button>
                    )}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/** Uploaded, clearance, department — the row's facts, through `lib/format`. */
function metaLine(document: AppDocument, dict: Dictionary, locale: Locale): string {
  const t = dict.documents;
  const parts = [t.uploadedAt.replace("{date}", formatDate(document.created_at, locale))];
  parts.push(`${t.clearance} ${document.clearance_level}`);
  parts.push(
    `${t.department} ${document.department_id ? shortId(document.department_id) : t.noDepartment}`,
  );
  if (document.is_company_kb) parts.push(t.companyDocument);
  parts.push(`${formatNumber(Math.round(document.file_size / 1024), locale)} kB`);
  return parts.join(" · ");
}

/** The first block of a uuid: the full value is on the row, not in a line of prose. */
function shortId(value: string): string {
  return value.slice(0, 8);
}

/**
 * The reader's language for a failure, from the catalogue key when the frontend knows
 * it and from the API's own sentence when it does not.
 */
function errorText(dict: Dictionary, cause: unknown): string {
  if (cause instanceof ApiError && cause.messageKey) {
    const known = cause.messageKey.replace(/^errors\./, "") as keyof Dictionary["errors"];
    if (known in dict.errors) return dict.errors[known];
  }
  return dict.documents.error;
}

/** The upload form: a labelled file input, a title, and two optional fields. */
function UploadCard({
  dict,
  locale,
  onAccepted,
  onError,
}: {
  dict: Dictionary;
  locale: Locale;
  onAccepted: (document: AppDocument) => void;
  onError: (message: string | null) => void;
}) {
  const t = dict.documents.upload;
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [clearance, setClearance] = useState("low");
  const [category, setCategory] = useState("");
  const [tags, setTags] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    onError(null);
    if (!file || title.trim().length === 0) {
      setLocalError(t.required);
      return;
    }
    setLocalError(null);
    setSending(true);
    try {
      const document = await uploadDocument(file, {
        title: title.trim(),
        clearanceLevel: clearance,
        category: category.trim() || undefined,
        tags: tags.trim() || undefined,
        language: locale,
      });
      onAccepted(document);
      setFile(null);
      setTitle("");
      setCategory("");
      setTags("");
    } catch (cause) {
      onError(errorText(dict, cause));
    } finally {
      setSending(false);
    }
  }

  return (
    <Card title={t.heading} description={t.description} headingId="upload-heading">
      <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
        <div className="flex flex-col gap-1">
          <label htmlFor="document-file" className="text-sm font-medium">
            {t.fileLabel}
          </label>
          <input
            id="document-file"
            name="file"
            type="file"
            accept=".pdf,.docx,.xlsx,.txt,.md,.markdown"
            aria-describedby="document-file-hint"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            className="w-full rounded border border-border bg-surface px-3 py-2 text-fg file:mr-3 file:rounded file:border-0 file:bg-neutral-bg file:px-3 file:py-1.5"
          />
          <p id="document-file-hint" className="text-sm text-fg-subtle">
            {t.fileHint.replace("{formats}", t.formats)}
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            label={t.titleLabel}
            value={title}
            onChange={setTitle}
            placeholder={t.titlePlaceholder}
            required
            error={localError ?? undefined}
          />
          <SelectField
            label={t.clearanceLabel}
            value={clearance}
            onChange={setClearance}
            options={[
              { value: "low", label: "low" },
              { value: "medium", label: "medium" },
              { value: "high", label: "high" },
            ]}
          />
          <TextField label={t.categoryLabel} value={category} onChange={setCategory} />
          <TextField
            label={t.tagsLabel}
            value={tags}
            onChange={setTags}
            hint={t.tagsHint}
          />
        </div>

        <div>
          <Button type="submit" disabled={sending}>
            {sending ? t.submitting : t.submit}
          </Button>
        </div>
      </form>
    </Card>
  );
}
