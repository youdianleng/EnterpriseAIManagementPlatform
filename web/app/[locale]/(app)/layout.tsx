import { redirect } from "next/navigation";

import { displayName } from "@/lib/api/auth";
import {
  changePasswordPath,
  loginPath,
  readServerProfile,
  readServerSession,
} from "@/lib/api/session-server";
import type { Locale } from "@/lib/i18n/config";
import { DEFAULT_LOCALE, isLocale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { SiteHeader } from "../site-header";
import { SessionUnavailable } from "./session-unavailable";

/**
 * The signed-in shell.
 *
 * Session enforcement lives here rather than in each page, so a page added later
 * cannot forget it — the same reasoning the API's forced-change guard uses. The
 * two rules are:
 *
 *   no session            → sign-in screen
 *   must_change_password  → change-password screen, and nowhere else
 *
 * Both are decided on the server from `/auth/session`, so no protected markup is
 * ever sent to a browser that is not entitled to it.
 */
export default async function SignedInLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  let session;
  try {
    session = await readServerSession();
  } catch {
    // The API did not answer, so "no session" cannot be distinguished from "no
    // API". Sending someone to the sign-in screen here would look like a logout,
    // which is a lie; say what actually happened instead.
    return <SessionUnavailable dict={dict.auth.shell} locale={locale} />;
  }

  if (!session) redirect(loginPath(locale));
  if (session.must_change_password) redirect(changePasswordPath(locale));

  const profile = await readServerProfile();

  return (
    <div className="flex flex-1 flex-col">
      <SiteHeader
        locale={locale}
        dict={dict}
        pathWithoutLocale=""
        identity={{
          name: profile ? displayName(profile) : session.username,
          username: session.username,
        }}
      />
      <main className="flex flex-1 flex-col">{children}</main>
    </div>
  );
}
