"use client";

import { usePathname } from "next/navigation";

import { getDictionary } from "@/lib/i18n";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";

/**
 * What the document list looks like while the server reads it.
 *
 * A skeleton rather than a spinner (design system §4.3): the shape of the answer is
 * already known — a heading and a few rows — so the page does not jump when the real
 * list arrives. No animation, so there is nothing `prefers-reduced-motion` would have
 * to switch off.
 *
 * A **client** component, and the locale comes from the path: `loading.tsx` is
 * rendered without the page's props, so the route's `[locale]` segment is not handed
 * to it.
 */
export default function LoadingDocuments() {
  const pathname = usePathname();
  const segment = pathname.split("/")[1];
  const locale: Locale = isLocale(segment) ? segment : DEFAULT_LOCALE;
  const t = getDictionary(locale).documents;

  return (
    <div className="flex flex-col gap-8" aria-busy="true">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
      </div>
      <p role="status" className="text-fg-muted">
        {t.loading}
      </p>
      <ul className="flex flex-col gap-3" aria-hidden="true">
        {[0, 1, 2].map((row) => (
          <li key={row} className="h-20 rounded-lg border border-border bg-neutral-bg" />
        ))}
      </ul>
    </div>
  );
}
