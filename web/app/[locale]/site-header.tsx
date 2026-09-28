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
 *
 * **Two rows, not one wrapping row.** The brand, the language and the identity are the
 * top bar; the navigation is its own line beneath them. A single `flex-wrap` row worked
 * while there were five entries and stopped working at eight: at 1280 the identity block
 * was pushed onto a line of its own with a stray left border while the row it left behind
 * was right-aligned beside the brand. A row that silently rearranges itself is the
 * "elements squeezed together" failure §8.2 names; two rows are stable at every width.
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

  /**
   * The navigation entries, in the order a signed-in reader meets them.
   *
   * Four decisions are folded into this list, each of them §4.1's rule that an interface
   * shows no entry its reader cannot open:
   *
   * * the clock, attendance and leave screens are self-service and self-only at the API,
   *   so every account may open all three and no role check is needed to offer them;
   * * the clock comes before the rest because it is the one people open with a minute to
   *   spare;
   * * the document list is offered to every role, and *which* documents it shows is the
   *   API's answer rather than something advertised here;
   * * the Q&A screen is offered under the same rule and for the same reason (ticket 37):
   *   every role may ask, and `GET /answers/conversations` answers with the caller's *own*
   *   conversations and nothing else. **There is deliberately no entry for anybody else's
   *   conversations** — §5.3 gives that read to compliance, and its screen is ticket 48's;
   * * `badge` marks the notification centre, which is the only entry that carries a count.
   */
  const links: Array<{ href: string; label: string; badge?: boolean }> = [
    { href: `/${locale}`, label: dict.nav.home },
    { href: `/${locale}/clock`, label: dict.nav.clock },
    { href: `/${locale}/attendance`, label: dict.nav.attendance },
    { href: `/${locale}/leave`, label: dict.nav.leave },
    { href: `/${locale}/timesheets`, label: dict.nav.timesheets },
    { href: `/${locale}/documents`, label: dict.nav.documents },
    { href: `/${locale}/qa`, label: dict.nav.qa },
    { href: `/${locale}/notifications`, label: dict.nav.notifications, badge: true },
    { href: `/${locale}/style-guide`, label: dict.nav.styleGuide },
  ];

  return (
    <header className="mb-8 flex flex-col gap-3 border-b border-border pb-4">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <Link
          href={`/${locale}`}
          className="text-sm font-semibold tracking-[0.08em] text-primary uppercase"
        >
          {dict.app.name}
        </Link>

        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
          <LocaleSwitcher
            current={locale}
            pathWithoutLocale={pathWithoutLocale}
            label={dict.language.label}
          />

          {identity && (
            <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
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
      </div>

      {identity && (
        <nav aria-label={dict.nav.main}>
          {/* `flex-wrap` and `min-w-0`, because the navigation has nine entries at 320px
              a single-line `ul` is wider than the viewport, and a flex item's automatic
              minimum size makes the *page* scroll rather than the list wrap. Design system
              §7 requires every screen to work at 320px, so the list wraps instead. */}
          <ul className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm">
            {links.map((link) => (
              <li key={link.href}>
                <Link
                  href={link.href}
                  className={
                    link.badge
                      ? "inline-flex items-center gap-1.5 text-fg-muted hover:text-fg"
                      : "text-fg-muted hover:text-fg"
                  }
                >
                  {link.label}
                  {link.badge && badgeCount !== null && badgeLabel !== null && (
                    // The number is visible and the accessible name says what it counts:
                    // "3" on its own is a number nobody can place.
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
            ))}
          </ul>
        </nav>
      )}
    </header>
  );
}
