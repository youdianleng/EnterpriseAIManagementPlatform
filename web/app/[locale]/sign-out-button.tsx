"use client";

import { useState } from "react";

import { logout } from "@/lib/api/auth";

/**
 * Sign-out control.
 *
 * A full navigation after the call, not a client-side route change: the
 * signed-in identity is rendered by Server Components, so the server has to
 * re-read the session. `router.push` could reuse a cached render of a page that
 * belongs to an account which has already signed out.
 */
export function SignOutButton({
  label,
  pendingLabel,
  redirectTo,
  className,
}: {
  label: string;
  pendingLabel: string;
  /** Absolute path of the sign-in screen for this locale. */
  redirectTo: string;
  className?: string;
}) {
  const [pending, setPending] = useState(false);

  async function signOut() {
    setPending(true);
    try {
      await logout();
    } catch {
      // The device must not be left on a page it can no longer use; the cookie
      // is cleared server-side on a best-effort basis either way.
    } finally {
      window.location.assign(redirectTo);
    }
  }

  return (
    <button
      type="button"
      disabled={pending}
      aria-busy={pending || undefined}
      onClick={() => void signOut()}
      className={className}
    >
      {pending ? pendingLabel : label}
    </button>
  );
}
