import type { DocumentPage } from "@/lib/api/documents";
import { readServerDocuments } from "@/lib/api/session-server";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { DocumentScreen } from "./document-screen";

/**
 * The document list and the upload control.
 *
 * Read on the server so the list is on the first paint, like the notification centre:
 * a page that renders empty and then fills in makes "you have no documents" and "we
 * have not looked yet" the same screen for a moment, and the difference matters to
 * somebody who came here to find a policy.
 *
 * `null` from the reader is a state the screen has, not an exception it throws: the
 * layout has already decided there is a session, so a failure here is about the
 * request, and the screen says so with a retry.
 */
export default async function DocumentsPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const page: DocumentPage | null = await readServerDocuments();

  return (
    <div className="flex flex-col gap-8">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{dict.documents.title}</h1>
        <p className="mt-2 text-fg-muted">{dict.documents.intro}</p>
      </div>

      <DocumentScreen
        dict={dict}
        locale={locale}
        initialItems={page?.items ?? []}
        total={page?.total ?? 0}
        failed={page === null}
      />
    </div>
  );
}
