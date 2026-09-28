import Link from "next/link";

import { loginPath } from "@/lib/api/session-server";
import { formatNumber } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n/dictionaries";
import type { Locale } from "@/lib/i18n/config";

import { LocaleSwitcher } from "./locale-switcher";
import { SignOutButton } from "./sign-out-button";

export type HeaderIdentity = {
  /** Display name, or the username when the profile could not be read. */
  name: string;
  username: string;
};

/**
 * Shared top bar.
 *
 * The language switcher is here in both states — a signed-out visitor still has
 * to be able to read the sign-in screen in their language. The navigation and
 * the account block only exist once there is an identity, so the sign-in screen
 * never advertises pages it would refuse to open.
 */
export function SiteHeader({
  locale,
  dict,
  identity,
  pathWithoutLocale,
  unreadCount,
}: {
  locale: Locale;
  dict: Dictionary;
  identity?: HeaderIdentity;
  pathWithoutLocale: string;
  /**
   * Unread notifications, or null when the number could not be read.
   *
   * Null and zero are different states and are drawn differently: no badge means
   * "nobody asked", a hidden badge at zero means "there is nothing waiting".
   */
  unreadCount?: number | null;
}) {
  // A badge at zero is noise: the count is drawn only when there is something to
  // read, and the accessible name carries the number for anyone who cannot see it.
  const badgeCount =
    unreadCount === null || unreadCount === undefined || unreadCount <= 0 ? null : unreadCount;
  const badgeLabel =
    badgeCount === null
      ? null
      : dict.notifications.unreadBadge.replace("{count}", formatNumber(badgeCount, locale));

  return (
    <header className="mb-8 flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-b border-border pb-4">
      <Link
        href={`/${locale}`}
        className="text-sm font-semibold tracking-[0.08em] text-primary uppercase"
      >
        {dict.app.name}
      </Link>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {identity && (
          <nav aria-label={dict.nav.main}>
            {/* `flex-wrap` and `min-w-0`, because the navigation grew to five links
                with the document list: at 320px a single-line `ul` is wider than the
                viewport, and a flex item's automatic minimum size makes the *page*
                scroll rather than the list wrap. Design system §7 requires every
                screen to work at 320px, so the list wraps instead. */}
            <ul className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              <li>
                <Link href={`/${locale}`} className="text-fg-muted hover:text-fg">
                  {dict.nav.home}
                </Link>
              </li>
              <li>
                {/* Your own hours: every account holds `employee`, so there is no role
                    that would be refused this page (§4.1). */}
                <Link href={`/${locale}/timesheets`} className="text-fg-muted hover:text-fg">
                  {dict.nav.timesheets}
                </Link>
              </li>
              <li>
                {/* The document list. Every role may open it; which documents it shows
                    is §4.2's answer, applied by the API rather than advertised here. */}
                <Link href={`/${locale}/documents`} className="text-fg-muted hover:text-fg">
                  {dict.nav.documents}
                </Link>
              </li>
              <li>
                <Link
                  href={`/${locale}/notifications`}
                  className="inline-flex items-center gap-1.5 text-fg-muted hover:text-fg"
                >
                  {dict.nav.notifications}
                  {badgeCount !== null && badgeLabel !== null && (
                    // The number is visible and the accessible name says what it
                    // counts: "3" on its own is a number nobody can place.
                    <span
                      role="status"
                      aria-label={badgeLabel}
                      className="tabular inline-flex min-w-5 items-center justify-center rounded-full bg-primary px-1.5 text-xs font-semibold text-primary-fg"
                    >
                      {formatNumber(badgeCount, locale)}
                    </span>
                  )}
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
        )}

        <LocaleSwitcher
          current={locale}
          pathWithoutLocale={pathWithoutLocale}
          label={dict.language.label}
        />

        {identity && (
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 border-l border-border pl-4">
            <p className="text-sm">
              <span className="text-fg-subtle">{dict.auth.shell.signedInAs}</span>{" "}
              <span className="font-medium">{identity.name}</span>{" "}
              <span className="text-fg-subtle">({identity.username})</span>
            </p>
            <SignOutButton
              label={dict.auth.shell.signOut}
              pendingLabel={dict.auth.shell.signingOut}
              redirectTo={loginPath(locale)}
              className="rounded px-2 py-1 text-sm text-fg-muted transition-colors duration-150 hover:bg-neutral-bg hover:text-fg disabled:opacity-50"
            />
          </div>
        )}
      </div>
    </header>
  );
}
