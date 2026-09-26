import type { Metadata } from "next";
import Link from "next/link";

import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import "../globals.css";

export const metadata: Metadata = {
  title: "Enterprise AI Management Platform",
  description: "Internal platform for organisation, people, attendance and knowledge",
};

// The URL carries the locale for every page; no dynamic data yet.
export function generateStaticParams() {
  return [{ locale: "es" }, { locale: "en" }];
}

export default async function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  return (
    <html lang={locale}>
      <body className="min-h-dvh antialiased">
        <div className="mx-auto flex min-h-dvh w-full max-w-5xl flex-col px-4 py-8 sm:px-6 lg:px-8">
          {/*
            The shell owns no heading: each page supplies its own h1, which keeps
            exactly one level-1 heading per document.
          */}
          <header className="mb-8 flex flex-wrap items-center justify-between gap-4 border-b border-border pb-4">
            <Link
              href={`/${locale}`}
              className="text-sm font-semibold tracking-[0.08em] text-primary uppercase"
            >
              {dict.app.name}
            </Link>
            <div className="flex items-center gap-4">
              <nav aria-label={dict.app.name}>
                <ul className="flex items-center gap-3 text-sm">
                  <li>
                    <Link href={`/${locale}`} className="text-fg-muted hover:text-fg">
                      {dict.nav.home}
                    </Link>
                  </li>
                  <li>
                    <Link
                      href={`/${locale}/style-guide`}
                      className="text-fg-muted hover:text-fg"
                    >
                      {dict.nav.styleGuide}
                    </Link>
                  </li>
                </ul>
              </nav>
            </div>
          </header>
          <main className="flex flex-1 flex-col">{children}</main>
        </div>
      </body>
    </html>
  );
}
