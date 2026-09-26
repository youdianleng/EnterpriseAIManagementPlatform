/**
 * Authentication client.
 *
 * Three things this module deliberately owns so no screen has to:
 *
 * 1. **The catalogue keys.** The API answers with `message` (Spanish prose) and
 *    `message_key`. Rendering a key is what makes the screens bilingual, so the
 *    keys this flow can produce are enumerated here and the Spanish sentence is
 *    only a fallback for a key the frontend has not learned yet.
 * 2. **The lockout countdown.** 423 carries the remaining time inside a
 *    human-readable `detail`, not in a structured field. Parsing it in one place
 *    keeps that coupling visible instead of scattering a regex across screens.
 * 3. **The redirect flags.** `must_change_password` decides where a sign-in lands,
 *    so it is read here rather than at each call site.
 */

import { ApiError, request } from "@/lib/api/client";

export type AuthSession = {
  user_id: string;
  username: string;
  employee_id: string;
  /** The name to greet somebody by; the server resolves it with the session. */
  employee_full_name: string;
  /** While true the API refuses everything but the change-password flow. */
  must_change_password: boolean;
};

/** The account's own profile, used for the display name. */
export type OwnProfile = {
  id: string;
  first_name: string;
  last_name: string;
  preferred_name: string | null;
  email: string | null;
};

export type PasswordPolicy = {
  minimum_length: number;
  /** Rule names, e.g. `["lower", "upper", "digit", "special"]`. */
  required_classes: string[];
};

/** Catalogue keys this flow can surface. Mirrors `api/app/core/messages.py`. */
export const AUTH_ERROR_KEYS = [
  "errors.account_invalid_credentials",
  "errors.account_locked",
  "errors.account_disabled",
  "errors.account_password_policy",
  "errors.account_password_reused",
  "errors.session_invalid",
  "errors.password_change_required",
  "errors.validation_failed",
  "errors.internal_error",
  "errors.service_unavailable",
] as const;

export type AuthErrorKey = (typeof AUTH_ERROR_KEYS)[number];

const KNOWN_KEYS: ReadonlySet<string> = new Set(AUTH_ERROR_KEYS);

/**
 * Error code → catalogue key, for the responses that carry only a code.
 *
 * The API always sends `message_key`, so this is belt and braces: it keeps the
 * "render from the key" rule true even if a response arrives from the fallback
 * handlers with an unexpected shape.
 */
const CODE_KEYS: Record<string, AuthErrorKey> = {
  ERR_ACC_006: "errors.account_password_policy",
  ERR_ACC_007: "errors.account_invalid_credentials",
  ERR_ACC_008: "errors.account_disabled",
  ERR_ACC_009: "errors.account_password_reused",
  ERR_AUTH_003: "errors.account_locked",
  ERR_SES_001: "errors.session_invalid",
  ERR_SES_002: "errors.password_change_required",
  ERR_VALIDATION_001: "errors.validation_failed",
  ERR_INTERNAL_001: "errors.internal_error",
  ERR_INTERNAL_002: "errors.service_unavailable",
};

/**
 * The catalogue key for a failure, or null when the frontend does not know it.
 *
 * Null is not an error state: the caller falls back to the API's Spanish
 * sentence, which is why a new backend key degrades to a readable message
 * instead of a blank one.
 */
export function authErrorKey(error: unknown): AuthErrorKey | null {
  if (!(error instanceof ApiError)) return null;
  const key = error.messageKey ?? (error.code ? CODE_KEYS[error.code] : undefined);
  return key !== undefined && KNOWN_KEYS.has(key) ? (key as AuthErrorKey) : null;
}

/**
 * Rule names from `errors.account_password_policy`'s detail, e.g.
 * `"policy violations: too_short, missing_digit"` → `["too_short", "missing_digit"]`.
 *
 * The list is what lets the form show every broken rule at once rather than
 * making the person discover them one submission at a time.
 */
export function policyViolations(error: unknown): string[] {
  if (!(error instanceof ApiError) || !error.detail) return [];
  const marker = "policy violations:";
  const at = error.detail.indexOf(marker);
  if (at === -1) return [];
  return error.detail
    .slice(at + marker.length)
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
}

/** Seconds until a locked account may try again, or null when not a lockout. */
export function lockoutSeconds(error: unknown): number | null {
  if (!(error instanceof ApiError) || error.code !== "ERR_AUTH_003") return null;
  const found = /(\d+)/.exec(error.detail ?? "");
  return found ? Number.parseInt(found[1], 10) : null;
}

export function login(username: string, password: string): Promise<AuthSession> {
  return request<AuthSession>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

/** Who is signed in, or null when nobody is. */
export async function readSession(): Promise<AuthSession | null> {
  try {
    return await request<AuthSession>("/api/v1/auth/session");
  } catch (error) {
    // 401 is the expected answer for a signed-out visitor, not a failure.
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<AuthSession> {
  return request<AuthSession>("/api/v1/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

export function logout(): Promise<void> {
  return request<void>("/api/v1/auth/logout", { method: "POST" });
}

/** `preferred_name` is what the person is called; the full name is the fallback. */
export function displayName(profile: OwnProfile): string {
  const preferred = profile.preferred_name?.trim();
  if (preferred) return preferred;
  return `${profile.first_name} ${profile.last_name}`.trim();
}
