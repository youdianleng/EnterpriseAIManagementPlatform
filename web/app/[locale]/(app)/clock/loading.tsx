"use client";

import { usePathname } from "next/navigation";

import { getDictionary } from "@/lib/i18n";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";

/**
 * What the clock looks like while the server reads the day.
 *
 * A skeleton rather than a spinner (§4.3), and it is shaped like the answer: a heading,
 * the state block, then the punch rows. Nothing here animates, so there is nothing
 * `prefers-reduced-motion` would have to switch off.
 *
 * A **client** component, and the locale comes from the path: `loading.tsx` is rendered
 * without the page's props, so the route's `[locale]` segment is not handed to it.
 */
export default function LoadingClock() {
  const pathname = usePathname();
  const segment = pathname.split("/")[1];
  const locale: Locale = isLocale(segment) ? segment : DEFAULT_LOCALE;
  const t = getDictionary(locale).clock;

  return (
    <div className="flex flex-col gap-8" aria-busy="true">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
      </div>
      <p role="status" className="text-fg-muted">
        {t.loading}
      </p>
      <div className="h-48 rounded-lg border border-border bg-neutral-bg" aria-hidden="true" />
      <ul className="flex flex-col gap-2" aria-hidden="true">
        {[0, 1].map((row) => (
          <li key={row} className="h-10 rounded border border-border bg-neutral-bg" />
        ))}
      </ul>
    </div>
  );
}
