"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

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
 * **Client-side navigation, and the page you are on.** Both halves matter, and the second
 * one is a defect this ticket had to fix rather than a detail:
 *
 * * It used to be a plain `<a href>`, which is a *document* navigation: the browser tears
 *   the page down and loads the other locale from scratch. Design system §3.2 says the
 *   switch takes effect 「不整页刷新」 and does not lose what has been typed, and ticket 37's
 *   checklist line is 「流式过程中切换语言不影响正在生成的回答」 — an answer arriving over
 *   `fetch` cannot survive a document teardown, and neither can a half-typed question.
 *   `next/link` navigates in the client, so the module-scope store that owns the stream
 *   (`lib/stores/qa-store.ts`) is the same module before and after.
 * * The target path is derived from `usePathname()` rather than taken from the prop alone,
 *   because the signed-in shell passes `""` — so the entry links pointed at the locale's
 *   *home*: switching to English from `/es/documents` landed on `/en`. The prop is still
 *   the fallback, and the auth screens that pass an explicit path keep working unchanged.
 *
 * The choice is still a cookie as well as a URL, so a later locale-less visit remembers it.
 */
export function LocaleSwitcher({ current, pathWithoutLocale, label }: Props) {
  const pathname = usePathname();
  const target = pathWithoutLocaleOf(pathname) ?? pathWithoutLocale;

  return (
    <nav aria-label={label} className="flex items-center gap-1">
      {LOCALES.map((locale) => {
        const isCurrent = locale === current;
        return (
          <Link
            key={locale}
            href={`/${locale}${target}`}
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
          </Link>
        );
      })}
    </nav>
  );
}

/**
 * The current path with its leading locale segment removed, or `null` when there is none.
 *
 * `/es` → `""`, `/es/qa` → `/qa`, `/en/login` → `/login`. A path that does not begin with
 * a locale is not a route this application serves — the middleware redirects it — so it
 * falls back to the caller's own `pathWithoutLocale`.
 */
function pathWithoutLocaleOf(pathname: string | null): string | null {
  if (!pathname) return null;
  const segments = pathname.split("/");
  if (segments.length < 2 || !(LOCALES as readonly string[]).includes(segments[1])) return null;
  return `/${segments.slice(2).join("/")}`.replace(/\/$/, "");
}
