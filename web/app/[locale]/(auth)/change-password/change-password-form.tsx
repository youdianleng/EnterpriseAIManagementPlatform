"use client";

import { useState, type FormEvent } from "react";

import { changePassword, type PasswordPolicy } from "@/lib/api/auth";
import { describeAuthFailure, type AuthFailure } from "@/lib/api/auth-error-text";
import type { Locale } from "@/lib/i18n/config";
import type { Dictionary } from "@/lib/i18n/dictionaries";
import { classText } from "@/lib/i18n/violations";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { TextField } from "@/lib/ui/field";

import { SignOutButton } from "../../sign-out-button";

/**
 * The one form an account in the forced-change state can use.
 *
 * There is deliberately no way past it: the API refuses every other endpoint
 * while the flag is set, so this screen offers the change, and the escape hatch
 * of signing out — which is not a bypass, it just ends the session.
 *
 * When the API rejects a password it names every broken rule. All of them are
 * shown at once, as separate messages, so the person fixes one form instead of
 * discovering the rules by resubmitting.
 */
export function ChangePasswordForm({
  dict,
  locale,
  policy,
}: {
  dict: Dictionary;
  locale: Locale;
  policy: PasswordPolicy | null;
}) {
  const t = dict.auth.changePassword;
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [failure, setFailure] = useState<AuthFailure | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{
    current?: string;
    next?: string;
    confirmation?: string;
  }>({});
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const errors = {
      current: current ? undefined : t.currentRequired,
      next: next ? undefined : t.newRequired,
      confirmation: !confirmation
        ? t.confirmRequired
        : confirmation === next
          ? undefined
          : t.mismatch,
    };
    setFieldErrors(errors);
    if (errors.current || errors.next || errors.confirmation) return;

    setPending(true);
    // The previous refusal is cleared before the retry, not after it: leaving it
    // on screen while the corrected password is being checked reads as if the
    // fix did not take.
    setFailure(null);
    try {
      await changePassword(current, next);
      // The API replaced the cookie: the old session died with the old epoch, so
      // this device already holds a working one. A full navigation re-reads it.
      window.location.assign(`/${locale}`);
    } catch (error) {
      setFailure(describeAuthFailure(error, dict, locale));
      setPending(false);
    }
  }

  return (
    <section
      aria-labelledby="change-password-heading"
      className="mx-auto w-full max-w-md rounded-lg border border-border bg-surface p-6 shadow-sm"
    >
      <h1 id="change-password-heading" className="text-xl font-semibold">
        {t.title}
      </h1>
      <p className="mt-2 text-fg-muted">{t.intro}</p>

      <div className="mt-6 rounded bg-info-bg p-4 text-info">
        <h2 className="font-medium">{dict.auth.policy.title}</h2>
        <p className="mt-1 text-sm">{dict.auth.policy.intro}</p>
        <ul className="mt-2 list-disc pl-5 text-sm">
          {policy ? (
            <>
              <li>{dict.auth.policy.minimum.replace("{count}", String(policy.minimum_length))}</li>
              <li>
                {dict.auth.policy.contain}{" "}
                {policy.required_classes.map((rule) => classText(rule, dict)).join(", ")}.
              </li>
            </>
          ) : (
            // Without the policy the form still works — the API enforces the
            // rule either way — but the page must not invent a rule to display.
            <li>{dict.errors.service_unavailable}</li>
          )}
        </ul>
      </div>

      <form className="mt-6 flex flex-col gap-4" onSubmit={submit} noValidate>
        {failure && (
          <Alert tone="danger" role="alert" title={failure.message}>
            {failure.violations.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-sm">
                {failure.violations.map((violation) => (
                  <li key={violation}>{violation}</li>
                ))}
              </ul>
            )}
          </Alert>
        )}

        <TextField
          label={t.currentLabel}
          type="password"
          value={current}
          onChange={setCurrent}
          autoComplete="current-password"
          error={fieldErrors.current}
          required
        />
        <TextField
          label={t.newLabel}
          type="password"
          value={next}
          onChange={setNext}
          autoComplete="new-password"
          error={fieldErrors.next}
          required
        />
        <TextField
          label={t.confirmLabel}
          type="password"
          value={confirmation}
          onChange={setConfirmation}
          autoComplete="new-password"
          error={fieldErrors.confirmation}
          required
        />

        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={pending} aria-busy={pending || undefined}>
            {pending ? t.submitting : t.submit}
          </Button>
          <SignOutButton
            label={t.signOut}
            pendingLabel={dict.auth.shell.signingOut}
            redirectTo={`/${locale}/login`}
            className="rounded px-3 py-2 text-sm text-fg-muted transition-colors duration-150 hover:bg-neutral-bg hover:text-fg disabled:opacity-50"
          />
        </div>
        <p className="text-sm text-fg-subtle">{t.signOutHint}</p>
      </form>
    </section>
  );
}
