import type { Metadata } from "next";

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
          <header className="mb-8 border-b border-border pb-6">
            <p className="text-xs font-semibold tracking-[0.08em] text-primary uppercase">
              {dict.app.name}
            </p>
            {/*
              The shell owns the single h1 and the page has no visible page title
              in this milestone, so this is the visible-in-AT heading. When real
              screens land, move this level-1 heading into them.
            */}
            <h1 className="sr-only">{dict.app.name}</h1>
          </header>
          <main className="flex flex-1 flex-col">{children}</main>
        </div>
      </body>
    </html>
  );
}
