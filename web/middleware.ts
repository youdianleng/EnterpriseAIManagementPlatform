import { NextResponse, type NextRequest } from "next/server";

import {
  DEFAULT_LOCALE,
  LOCALE_COOKIE,
  matchLocaleFromAcceptLanguage,
  isLocale,
} from "@/lib/i18n/config";

/**
 * Routes that a visitor without a session may reach on purpose.
 *
 * The middleware cannot validate a session — that needs the cookie, which is
 * httpOnly, and a round trip to the API — so it does the cheap half: an
 * obviously signed-out visitor is sent to the sign-in screen without rendering a
 * protected page first. The page itself decides with the real session, and is
 * what actually keeps the forced-change gate closed.
 */
const PUBLIC_PATHS = ["/login", "/change-password"];

function isPublic(pathWithoutLocale: string): boolean {
  return PUBLIC_PATHS.some(
    (path) => pathWithoutLocale === path || pathWithoutLocale.startsWith(`${path}/`),
  );
}

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
    const withoutLocale = pathname.replace(/^\/(es|en)/, "");
    // `/change-password` is in PUBLIC_PATHS for the same reason the API exempts
    // it: a redirect here would loop back to `/login`, which sends a signed-in
    // account straight back to the screen it is trying to finish.
    if (isPublic(withoutLocale) || request.cookies.has("eam_session")) {
      return NextResponse.next();
    }
    // No session cookie at all: nothing to render, so skip the page render and
    // land on the sign-in screen. A cookie that is present but stale is caught
    // by the page, which is the only place that can tell the difference.
    const url = request.nextUrl.clone();
    url.pathname = `/${pathname.slice(1, 3)}/login`;
    url.search = "";
    return NextResponse.redirect(url);
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
