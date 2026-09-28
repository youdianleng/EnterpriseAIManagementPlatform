"use client";

import type { Citation } from "@/lib/api/answers";
import { documentPageUrl } from "@/lib/api/documents";
import { formatNumber } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";

/**
 * The passage behind a citation badge, in the side panel the ticket asks for.
 *
 * Four decisions a reader should be able to see:
 *
 * * **The quote is the corpus's text, never a translation.** §5.2's 「引用原文不翻译」: an
 *   answer is generated in the asker's language, and a citation that translated the
 *   passage would be quoting a document that does not exist. So this block is rendered as
 *   the server sent it, and the surrounding labels are the only translated part.
 * * **"Open the original" goes to the document at the cited page**, through the download
 *   route that already exists and is guarded by the same §4.2 rule the retrieval applied —
 *   a citation that cannot be opened is not a citation. The anchor is `#page=N` and it is
 *   omitted for a format with no pages (see `documentPageUrl`).
 * * **A page range is named as one.** A chunk that spans pages carries `page_to`, and a
 *   panel that printed only `page` would tell the reader the passage is on a single page
 *   when it is not.
 * * **Whether the passage is a personal upload is stated in words**, because §5.2/Q29's
 *   marker is about *provenance* and a colour cannot carry it (§5).
 */
export function CitationPanel({
  citation,
  index,
  dict,
  locale,
  onClose,
}: {
  citation: Citation;
  index: number;
  dict: Dictionary;
  locale: Locale;
  onClose: () => void;
}) {
  const t = dict.qa.panel;
  const page =
    citation.page === null
      ? null
      : citation.page_to !== null && citation.page_to !== citation.page
        ? t.pageRange
            .replace("{from}", formatNumber(citation.page, locale))
            .replace("{to}", formatNumber(citation.page_to, locale))
        : t.page.replace("{page}", formatNumber(citation.page, locale));

  return (
    <aside
      aria-labelledby="qa-citation-panel-heading"
      data-testid="qa-citation-panel"
      data-citation={index}
      className="min-w-0 rounded-lg border border-border bg-surface p-4 shadow-sm lg:sticky lg:top-4 lg:self-start"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 id="qa-citation-panel-heading" className="text-base font-semibold">
          {t.title.replace("{n}", formatNumber(index, locale))}
        </h3>
        <button
          type="button"
          onClick={onClose}
          aria-label={t.close}
          className="rounded px-2 py-1 text-fg-muted hover:bg-neutral-bg hover:text-fg"
        >
          <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 fill-none stroke-current">
            <path d="M4 4l8 8M12 4l-8 8" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      <p className="mt-2 break-words text-sm font-medium">{citation.title}</p>
      <dl className="mt-2 flex flex-col gap-1 text-sm text-fg-muted">
        <div className="flex flex-wrap gap-x-2">
          <dt className="font-medium">{t.file}</dt>
          <dd className="min-w-0 break-all">{citation.filename}</dd>
        </div>
        {page && (
          <div className="flex flex-wrap gap-x-2">
            <dt className="font-medium">{t.pageLabel}</dt>
            <dd className="tabular">{page}</dd>
          </div>
        )}
        {citation.heading_path && (
          <div className="flex flex-wrap gap-x-2">
            <dt className="font-medium">{t.section}</dt>
            <dd className="min-w-0 break-words">{citation.heading_path}</dd>
          </div>
        )}
        <div className="flex flex-wrap gap-x-2">
          <dt className="font-medium">{t.provenance}</dt>
          <dd>{citation.is_company_kb ? t.companyKb : t.personalDocument}</dd>
        </div>
        <div className="flex flex-wrap gap-x-2">
          <dt className="font-medium">{t.scope}</dt>
          <dd>{citation.context_scope === "parent" ? t.scopeParent : t.scopeChild}</dd>
        </div>
      </dl>

      <p className="mt-3 text-sm font-medium">{t.quote}</p>
      <blockquote className="mt-1 max-h-96 overflow-y-auto rounded border-l-4 border-border bg-neutral-bg p-3 text-sm">
        {citation.quote}
      </blockquote>

      <a
        className="mt-3 inline-flex min-h-9 items-center rounded border border-border px-3 py-1.5 text-sm hover:bg-neutral-bg"
        href={documentPageUrl(citation.document_id, citation.page)}
        target="_blank"
        rel="noreferrer noopener"
        data-testid="qa-open-original"
      >
        {t.openOriginal}
      </a>
    </aside>
  );
}
