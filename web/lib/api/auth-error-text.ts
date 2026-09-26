import { authErrorKey, lockoutSeconds, policyViolations } from "@/lib/api/auth";
import { formatDuration } from "@/lib/format";
import type { Locale } from "@/lib/i18n/config";
import type { Dictionary } from "@/lib/i18n/dictionaries";
import { violationText } from "@/lib/i18n/violations";

/**
 * Turns a failure from the auth client into what a form needs: one heading
 * sentence plus the specific rules that were broken.
 *
 * Both auth screens resolve errors through here. Getting it subtly different on
 * each is how one of them ends up showing a raw API sentence in a Spanish UI.
 */
export type AuthFailure = {
  /** Catalogue sentence for the key, falling back to the API's own message. */
  message: string;
  /** `policy violations: too_short, missing_digit` → one message per rule. */
  violations: string[];
};

export function describeAuthFailure(
  error: unknown,
  dict: Dictionary,
  locale: Locale,
): AuthFailure {
  // A lockout is only useful with the time left, and the API carries that inside
  // the human-readable detail rather than in a structured field.
  const seconds = lockoutSeconds(error);
  if (seconds !== null) {
    return {
      message: dict.auth.login.lockedOut.replace(
        "{time}",
        formatDuration(Math.ceil(seconds / 60), locale),
      ),
      violations: [],
    };
  }

  const key = authErrorKey(error);
  return {
    // Catalogue keys are namespaced (`errors.account_locked`); the dictionary
    // nests that namespace, because the file is already the `errors` node.
    message: key ? dict.errors[errorName(key)] : fallbackMessage(error),
    violations: policyViolations(error).map((rule) => violationText(rule, dict)),
  };
}

function errorName(key: string): keyof Dictionary["errors"] {
  return key.replace(/^errors\./, "") as keyof Dictionary["errors"];
}

/** The API's `message` is already Spanish; it is the last resort, not the first. */
function fallbackMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "";
}
