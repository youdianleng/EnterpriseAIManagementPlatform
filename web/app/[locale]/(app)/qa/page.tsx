import type { ConversationPage } from "@/lib/api/answers";
import { readServerConversations } from "@/lib/api/session-server";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { QaScreen } from "./qa-screen";

/**
 * The Q&A screen (ticket 37).
 *
 * The conversation list is read on the server so the sidebar is on the first paint, like
 * the document list and for the same reason: a page that renders empty and then fills in
 * makes "you have no conversations" and "we have not looked yet" the same screen for a
 * moment. `null` from the reader is a *state* the screen has — the layout already decided
 * there is a session, so a failure here is about this request — and it is rendered as a
 * sentence with a retry rather than thrown.
 *
 * The 90-day retention is stated on the page rather than left to be discovered (§5.1's
 * 「界面上明确告知该期限」): once in the sidebar, as the rule, and beside an open
 * conversation as the **date the row carries** — the deadline travelled with the API's
 * `expires_at`, so the screen cannot disagree with the sweep that will enforce it.
 */
export default async function QaPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const page: ConversationPage | null = await readServerConversations();

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{dict.qa.title}</h1>
        <p className="mt-2 text-fg-muted">{dict.qa.intro}</p>
      </div>

      <QaScreen
        dict={dict}
        locale={locale}
        initialConversations={page?.items ?? []}
        total={page?.total ?? 0}
        failed={page === null}
      />
    </div>
  );
}
