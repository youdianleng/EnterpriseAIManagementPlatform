import { ApiError } from "@/lib/api/client";
import type { Dictionary } from "@/lib/i18n/dictionaries";

/**
 * The reader's language for a failure, and never the API's.
 *
 * Every refusal from this API carries a catalogue key (`errors.leave_type_inactive`)
 * and an already-Spanish sentence. The sentence is for a log; the key is for a screen,
 * because a reader of the English interface must be told in English that the leave type
 * has been retired. So the key is what this resolves, and the fallback is the caller's
 * own sentence for "this failed" rather than the server's Spanish one.
 *
 * A key this build does not know degrades to the fallback rather than to a blank line —
 * the same rule the notification centre's titles follow, and for the same reason: a key
 * added on the server must not empty a message on the client.
 */
export function catalogueErrorText(
  error: unknown,
  dict: Dictionary,
  fallback: string,
): string {
  if (error instanceof ApiError && error.messageKey) {
    const name = error.messageKey.replace(/^errors\./, "");
    if (name in dict.errors) return dict.errors[name as keyof Dictionary["errors"]];
  }
  return fallback;
}

/**
 * Whether a failure is the kernel refusing a record rather than the request being wrong.
 *
 * Used where a screen has a designed state for "you may not read this" — the balance
 * panel a manager cannot open, say — as opposed to "that input was wrong". The status is
 * the API's (403 for a refusal the kernel recorded); the key is checked too, because the
 * two agree and the key is the part that names *what* was refused.
 */
export function isRefusal(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.messageKey === "errors.forbidden");
}
