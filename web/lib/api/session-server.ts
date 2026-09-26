/**
 * Session access for Server Components.
 *
 * The cookie is httpOnly, so only the server can read it. Server Components
 * forward it explicitly to the API, which is what lets the shell render the
 * signed-in identity on the first paint instead of after a client round trip.
 */

import { cookies } from "next/headers";

import { ApiError, API_SERVER_BASE_URL } from "@/lib/api/client";
import type { AuthSession, OwnProfile, PasswordPolicy } from "@/lib/api/auth";
import type { NotificationPage, UnreadCount } from "@/lib/api/notifications";

/** The cookie the API sets at login; httpOnly, so it never reaches browser script. */
export const SESSION_COOKIE = "eam_session";

export function loginPath(locale: string): string {
  return `/${locale}/login`;
}

export function changePasswordPath(locale: string): string {
  return `/${locale}/change-password`;
}

async function serverRequest<T>(path: string): Promise<T> {
  const store = await cookies();
  const cookie = store.get(SESSION_COOKIE);

  const response = await fetch(`${API_SERVER_BASE_URL}${path}`, {
    headers: {
      Accept: "application/json",
      ...(cookie ? { Cookie: `${SESSION_COOKIE}=${cookie.value}` } : {}),
    },
    // Identity is per-request and must never be served from a cache.
    cache: "no-store",
  });

  if (!response.ok) {
    // The body only matters for the log; the status is what callers branch on.
    throw new ApiError(`Request failed with status ${response.status}`, response.status);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/**
 * The signed-in account, or null for a visitor without a session.
 *
 * `GET /auth/session` answers in the forced-change state too — it is the call
 * that says *why* everything else is refused — so the gate can be decided from
 * this one read.
 */
export async function readServerSession(): Promise<AuthSession | null> {
  try {
    return await serverRequest<AuthSession>("/api/v1/auth/session");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

/** The caller's own profile, for the display name. Null when it cannot be read. */
export async function readServerProfile(): Promise<OwnProfile | null> {
  try {
    return await serverRequest<OwnProfile>("/api/v1/employees/me");
  } catch {
    // A name is decoration; the session is the contract. An account whose
    // profile cannot be read still gets a working shell, identified by username.
    return null;
  }
}

export async function readServerPasswordPolicy(): Promise<PasswordPolicy | null> {
  try {
    return await serverRequest<PasswordPolicy>("/api/v1/auth/password-policy");
  } catch {
    return null;
  }
}

/**
 * The caller's own notifications, for the centre's first paint.
 *
 * Null when they cannot be read, which the page renders as its error state with a
 * retry — the same shape `readServerProfile` uses, and for the same reason: a
 * failed read should not be an exception the shell has to catch.
 */
export async function readServerNotifications(): Promise<NotificationPage | null> {
  try {
    return await serverRequest<NotificationPage>("/api/v1/notifications");
  } catch {
    return null;
  }
}

/**
 * The unread badge number, or null when it cannot be read.
 *
 * Null means "no badge", not "zero": the header must not claim there is nothing to
 * read when the truth is that nobody asked.
 */
export async function readServerUnreadCount(): Promise<number | null> {
  try {
    const payload = await serverRequest<UnreadCount>("/api/v1/notifications/unread-count");
    return payload.unread;
  } catch {
    return null;
  }
}
