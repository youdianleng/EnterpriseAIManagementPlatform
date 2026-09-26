import type { Metadata } from "next";

import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";

import "../globals.css";

export const metadata: Metadata = {
  title: "Enterprise AI Management Platform",
  description: "Internal platform for organisation, people, attendance and knowledge",
};

export const dynamic = "force-dynamic";

// The URL carries the locale for every page; no dynamic data yet.
export function generateStaticParams() {
  return [{ locale: "es" }, { locale: "en" }];
}

/**
 * Document shell only.
 *
 * Which chrome surrounds a page depends on whether anyone is signed in, and that
 * is a different question per route group, so the header lives in the group
 * layouts rather than here. This one owns `<html lang>`, the page container and
 * nothing that has to be signed in to render.
 */
export default async function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;

  return (
    <html lang={locale}>
      <body className="min-h-dvh antialiased">
        <div className="mx-auto flex min-h-dvh w-full max-w-5xl flex-col px-4 py-8 sm:px-6 lg:px-8">
          <div className="flex flex-1 flex-col">{children}</div>
        </div>
      </body>
    </html>
  );
}
