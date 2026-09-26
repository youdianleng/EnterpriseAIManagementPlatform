import { readServerSession } from "@/lib/api/session-server";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { BackendStatusPanel } from "./backend-status-panel";
import { SessionCard } from "./session-card";

/**
 * Home page.
 *
 * The signed-in shell already refused anyone without a usable session and sent
 * anyone who owes a password change to that screen, so reaching this code means
 * there is an identity to render. The session carries the name, so showing it
 * costs no second request.
 */
export default async function HomePage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const session = await readServerSession();

  return (
    <div className="flex flex-col gap-8">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{dict.app.name}</h1>
        <p className="mt-2 text-fg-muted">{dict.app.tagline}</p>
      </div>

      {session && (
        <SessionCard
          dict={dict.auth.shell}
          name={session.employee_full_name || session.username}
          username={session.username}
        />
      )}

      <BackendStatusPanel dict={dict.backend} />

      <footer className="mt-auto border-t border-border pt-6 text-sm text-fg-subtle">
        {dict.footer.milestone}
      </footer>
    </div>
  );
}
