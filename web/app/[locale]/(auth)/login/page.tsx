import { redirect } from "next/navigation";

import { changePasswordPath, readServerSession } from "@/lib/api/session-server";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { SiteHeader } from "../../site-header";
import { LoginForm } from "./login-form";

/**
 * Sign-in screen.
 *
 * A visitor who already has a usable session never sees the form: the session is
 * read on the server, so the redirect happens before any of this is sent. That
 * also settles the forced-change case — an account that still owes a password
 * change is sent straight there rather than being allowed to sign in again.
 */
export default async function LoginPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const session = await readServerSession().catch(() => null);
  if (session) {
    redirect(session.must_change_password ? changePasswordPath(locale) : `/${locale}`);
  }

  return (
    <div className="flex flex-1 flex-col">
      <SiteHeader locale={locale} dict={dict} pathWithoutLocale="/login" />
      <main className="flex flex-1 flex-col justify-center">
        <LoginForm dict={dict} locale={locale} />
      </main>
    </div>
  );
}
