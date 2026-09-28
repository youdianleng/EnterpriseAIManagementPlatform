"use client";

import { usePathname } from "next/navigation";

import { getDictionary } from "@/lib/i18n";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";

/**
 * What the Q&A screen looks like while the server reads the conversation list.
 *
 * A skeleton rather than a spinner (design system §4.3): the shape of the answer is
 * already known — a heading, a sidebar and a thread — so the page does not jump when the
 * real list arrives. No animation, so there is nothing `prefers-reduced-motion` would
 * have to switch off.
 *
 * A **client** component, and the locale comes from the path: `loading.tsx` is rendered
 * without the page's props, so the route's `[locale]` segment is not handed to it. The
 * `h1` is here rather than omitted because the heading is the one part of the page that
 * does not depend on the read — and a loading state that dropped it would fail §8.2's
 * "exactly one h1" for as long as the read took.
 */
export default function LoadingQa() {
  const pathname = usePathname();
  const segment = pathname.split("/")[1];
  const locale: Locale = isLocale(segment) ? segment : DEFAULT_LOCALE;
  const t = getDictionary(locale).qa;

  return (
    <div className="flex min-w-0 flex-col gap-6" aria-busy="true">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
      </div>
      <p role="status" className="text-fg-muted">
        {t.loading}
      </p>
      <div className="grid min-w-0 gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]" aria-hidden="true">
        <div className="h-48 rounded-lg border border-border bg-neutral-bg" />
        <div className="h-48 rounded-lg border border-border bg-neutral-bg" />
      </div>
    </div>
  );
}
