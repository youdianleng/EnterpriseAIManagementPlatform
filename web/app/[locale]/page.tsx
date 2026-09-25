import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { BackendStatusPanel } from "./backend-status-panel";
import { LocaleSwitcher } from "./locale-switcher";

export default async function HomePage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-2xl">
          {/* The shell layout owns the page h1. */}
          <p className="text-xl font-semibold tracking-tight">{dict.app.name}</p>
          <p className="mt-2 text-fg-muted">{dict.app.tagline}</p>
        </div>
        <LocaleSwitcher current={locale} pathWithoutLocale="" label={dict.language.label} />
      </div>

      <BackendStatusPanel dict={dict.backend} />

      <footer className="mt-auto border-t border-border pt-6 text-sm text-fg-subtle">
        {dict.footer.milestone}
      </footer>
    </div>
  );
}
