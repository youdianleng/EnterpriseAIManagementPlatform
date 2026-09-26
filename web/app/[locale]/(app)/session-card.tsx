import type { Dictionary } from "@/lib/i18n/dictionaries";

/**
 * Who is signed in, on the page itself and not only in the top bar.
 *
 * The header version is deliberately small; this is the confirmation a person
 * looks for after signing in — particularly on a shared machine, where the
 * question "whose session is this" has real consequences.
 */
export function SessionCard({
  dict,
  name,
  username,
}: {
  dict: Dictionary["auth"]["shell"];
  name: string;
  username: string;
}) {
  return (
    <section
      aria-labelledby="session-heading"
      className="rounded-lg border border-border bg-surface p-6 shadow-sm"
    >
      <h2 id="session-heading" className="text-lg font-semibold">
        {dict.signedInAs}
      </h2>
      <dl className="mt-4 grid gap-x-6 gap-y-3 sm:grid-cols-2">
        <div className="min-w-0">
          <dt className="text-sm text-fg-subtle">{dict.nameLabel}</dt>
          <dd className="mt-1 font-medium break-words">{name}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-sm text-fg-subtle">{dict.usernameLabel}</dt>
          <dd className="mt-1 font-medium break-words">{username}</dd>
        </div>
      </dl>
    </section>
  );
}
