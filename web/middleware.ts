import { NextResponse, type NextRequest } from "next/server";

import {
  DEFAULT_LOCALE,
  LOCALE_COOKIE,
  matchLocaleFromAcceptLanguage,
  isLocale,
} from "@/lib/i18n/config";

/**
 * Resolves the locale for locale-less paths and redirects to the prefixed route.
 *
 * Priority: a previously chosen locale (cookie) beats browser preferences, so a
 * manual switch is not overridden on the next visit.
 */
export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  const hasLocalePrefix = pathname === "/" || pathname.startsWith("/") === false;
  if (!hasLocalePrefix && /^\/(es|en)(\/|$)/.test(pathname)) {
    return NextResponse.next();
  }

  const cookieLocale = request.cookies.get(LOCALE_COOKIE)?.value;
  const locale = isLocale(cookieLocale)
    ? cookieLocale
    : (matchLocaleFromAcceptLanguage(request.headers.get("accept-language")) ??
      DEFAULT_LOCALE);

  const url = request.nextUrl.clone();
  url.pathname = `/${locale}${pathname === "/" ? "" : pathname}`;
  return NextResponse.redirect(url);
}

export const config = {
  // Skip Next internals, static assets and files with an extension.
  matcher: ["/((?!_next|favicon.ico|.*\\..*).*)"],
};
