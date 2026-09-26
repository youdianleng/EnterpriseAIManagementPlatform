"use client";

import { useState, type FormEvent } from "react";

import { describeAuthFailure, type AuthFailure } from "@/lib/api/auth-error-text";
import { login } from "@/lib/api/auth";
import type { Locale } from "@/lib/i18n/config";
import type { Dictionary } from "@/lib/i18n/dictionaries";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { TextField } from "@/lib/ui/field";

/**
 * The sign-in form.
 *
 * Keyboard-first: `autoFocus` lands on the username, the browser's own
 * autofill is left enabled (`username` / `current-password` are the tokens
 * password managers look for), and Enter submits without reaching for the mouse.
 *
 * The outcome of a successful sign-in depends on the account, not on the form:
 * an account that still owes a password change is sent to that screen, because
 * the API will refuse every other page until it is done.
 */
export function LoginForm({ dict, locale }: { dict: Dictionary; locale: Locale }) {
  const t = dict.auth.login;
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [failure, setFailure] = useState<AuthFailure | null>(null);
  const [missing, setMissing] = useState<{ username?: string; password?: string }>({});
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    // One round trip is saved by catching the empty case here; the API's own
    // limits are not restated, they are simply not reached.
    const empty = {
      username: username.trim() ? undefined : t.usernameRequired,
      password: password ? undefined : t.passwordRequired,
    };
    setMissing(empty);
    if (empty.username || empty.password) return;

    setPending(true);
    setFailure(null);
    try {
      const session = await login(username.trim(), password);
      // A full assignment rather than a push: the destination is rendered by
      // Server Components from the cookie that was just set.
      window.location.assign(
        session.must_change_password ? `/${locale}/change-password` : `/${locale}`,
      );
    } catch (error) {
      setFailure(describeAuthFailure(error, dict, locale));
      setPassword("");
      setPending(false);
    }
  }

  return (
    <section
      aria-labelledby="login-heading"
      className="mx-auto w-full max-w-md rounded-lg border border-border bg-surface p-6 shadow-sm"
    >
      <h1 id="login-heading" className="text-xl font-semibold">
        {t.title}
      </h1>
      <p className="mt-2 text-fg-muted">{t.intro}</p>

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
          label={t.usernameLabel}
          placeholder={t.usernamePlaceholder}
          value={username}
          onChange={setUsername}
          autoComplete="username"
          error={missing.username}
          required
        />
        <TextField
          label={t.passwordLabel}
          placeholder={t.passwordPlaceholder}
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
          error={missing.password}
          required
        />

        <Button type="submit" disabled={pending} aria-busy={pending || undefined}>
          {pending ? t.submitting : t.submit}
        </Button>
      </form>
    </section>
  );
}
