"use client";

import type { Locale } from "@/lib/i18n/config";
import { LOCALES, LOCALE_COOKIE, LOCALE_LABELS } from "@/lib/i18n/config";

type Props = {
  current: Locale;
  /** Path without the locale prefix, e.g. "" or "/employees". */
  pathWithoutLocale: string;
  label: string;
};

/**
 * Language switcher.
 *
 * Renders links rather than buttons so the choice works without JavaScript and
 * the target locale is a real, shareable URL. The cookie makes the choice stick
 * for later locale-less visits.
 */
export function LocaleSwitcher({ current, pathWithoutLocale, label }: Props) {
  return (
    <nav aria-label={label} className="flex items-center gap-1">
      {LOCALES.map((locale) => {
        const isCurrent = locale === current;
        return (
          <a
            key={locale}
            href={`/${locale}${pathWithoutLocale}`}
            hrefLang={locale}
            aria-current={isCurrent ? "true" : undefined}
            onClick={() => {
              document.cookie = `${LOCALE_COOKIE}=${locale}; path=/; max-age=31536000; samesite=lax`;
            }}
            className={`rounded-sm px-2 py-1 text-sm transition-colors duration-150 ${
              isCurrent
                ? "bg-primary-subtle font-semibold text-primary"
                : "text-fg-muted hover:bg-neutral-bg hover:text-fg"
            }`}
          >
            {LOCALE_LABELS[locale]}
          </a>
        );
      })}
    </nav>
  );
}
