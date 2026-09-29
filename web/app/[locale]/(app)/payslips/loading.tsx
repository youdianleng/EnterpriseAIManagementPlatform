"use client";

import { usePathname } from "next/navigation";

import { getDictionary } from "@/lib/i18n";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";

/**
 * What the payslip screen looks like while the server reads the month.
 *
 * A skeleton rather than a spinner (design system §4.3): the shape of the page is already
 * known — a heading, the uploader, and the two lists — so it does not jump when the real
 * content arrives. No animation, so there is nothing `prefers-reduced-motion` has to
 * switch off.
 *
 * A **client** component, and the locale comes from the path: `loading.tsx` is rendered
 * without the page's props, so the route's `[locale]` segment is not handed to it. The `h1`
 * is here rather than omitted because the heading is the one part of the page that does not
 * depend on the read — and a loading state without it would fail §8.2's "exactly one h1"
 * for as long as the read took.
 *
 * The two skeleton blocks are the two lists, and the *missing* one is drawn first and
 * larger, so the page's shape while loading is the page's shape when it arrives.
 */
export default function LoadingPayslips() {
  const pathname = usePathname();
  const segment = pathname.split("/")[1];
  const locale: Locale = isLocale(segment) ? segment : DEFAULT_LOCALE;
  const t = getDictionary(locale).payslips;

  return (
    <div className="flex flex-col gap-8" aria-busy="true">
      <div className="max-w-3xl">
        <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
        <p className="mt-2 text-fg-muted">{t.intro}</p>
      </div>
      <p role="status" className="text-fg-muted">
        {t.loading}
      </p>
      <div className="h-40 rounded-lg border-2 border-warning bg-warning-bg" aria-hidden="true" />
      <div className="h-24 rounded-lg border border-border bg-neutral-bg" aria-hidden="true" />
    </div>
  );
}
