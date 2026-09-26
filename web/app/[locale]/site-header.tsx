import Link from "next/link";

import { loginPath } from "@/lib/api/session-server";
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
}: {
  locale: Locale;
  dict: Dictionary;
  identity?: HeaderIdentity;
  pathWithoutLocale: string;
}) {
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
        )}

        <LocaleSwitcher
          current={locale}
          pathWithoutLocale={pathWithoutLocale}
          label={dict.language.label}
        />

        {identity && (
          <div className="flex items-center gap-3 border-l border-border pl-4">
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
