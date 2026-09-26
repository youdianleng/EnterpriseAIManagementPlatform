import type { Dictionary } from "@/lib/i18n/dictionaries";

/**
 * Wording for the password rules and the violations of them.
 *
 * They live together because they must stay the same shape: a rule is stated in
 * the policy list and then echoed as a violation when it is broken. Rule names
 * come from the API (`core/security.py`), so a name this build does not know
 * still produces a sentence instead of a blank list item.
 */
const VIOLATION_KEYS: Record<string, keyof Dictionary["auth"]["violations"]> = {
  too_short: "too_short",
  missing_lower: "missing_lower",
  missing_upper: "missing_upper",
  missing_digit: "missing_digit",
  missing_special: "missing_special",
};

export function violationText(rule: string, dict: Dictionary): string {
  const key = VIOLATION_KEYS[rule];
  return key ? dict.auth.violations[key] : dict.auth.violations.unknown;
}

/** `lower` → the label the policy list shows; unknown classes stay as sent. */
export function classText(rule: string, dict: Dictionary): string {
  const classes = dict.auth.policy.classes;
  return rule in classes
    ? classes[rule as keyof Dictionary["auth"]["policy"]["classes"]]
    : rule;
}
