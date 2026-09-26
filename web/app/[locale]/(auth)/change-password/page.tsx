import { redirect } from "next/navigation";

import { readServerPasswordPolicy, readServerSession } from "@/lib/api/session-server";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { SiteHeader } from "../../site-header";
import { ChangePasswordForm } from "./change-password-form";

/**
 * Forced password change.
 *
 * Reachable in exactly two states: an account flagged `must_change_password`
 * (which the shell sends here and the API refuses to let anywhere else), and the
 * same account coming back to finish the job. Anyone else is redirected, so this
 * screen cannot be used as a password-change shortcut.
 *
 * The rule shown on the page is fetched from `/auth/password-policy`; the UI
 * never restates it, because a server-side policy change would otherwise be
 * enforced against a form that describes the old rule.
 */
export default async function ChangePasswordPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const session = await readServerSession().catch(() => null);
  if (!session) redirect(`/${locale}/login`);
  // Nothing to do here for an account that is not being forced.
  if (!session.must_change_password) redirect(`/${locale}`);

  const policy = await readServerPasswordPolicy();

  return (
    <div className="flex flex-1 flex-col">
      <SiteHeader locale={locale} dict={dict} pathWithoutLocale="/change-password" />
      <main className="flex flex-1 flex-col justify-center">
        <ChangePasswordForm dict={dict} locale={locale} policy={policy} />
      </main>
    </div>
  );
}
