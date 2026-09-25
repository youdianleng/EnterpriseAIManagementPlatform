"use client";

import { useEffect } from "react";

import type { Dictionary } from "@/lib/i18n/dictionaries";
import { useBackendStore } from "@/lib/stores/backend-store";

import { StatusPill } from "./status-pill";

/**
 * Loads application facts from the API and renders them.
 *
 * A client component because it owns loading/error state; the rest of the page
 * stays server-rendered. The aria-live region exists so screen readers are told
 * when the probe finishes, not just sighted users.
 */
export function BackendStatusPanel({ dict }: { dict: Dictionary["backend"] }) {
  const status = useBackendStore((state) => state.status);
  const info = useBackendStore((state) => state.info);
  const errorMessage = useBackendStore((state) => state.errorMessage);
  const load = useBackendStore((state) => state.load);

  useEffect(() => {
    void load();
  }, [load]);

  const pill =
    status === "ready"
      ? { tone: "connected" as const, label: dict.connected }
      : status === "error"
        ? { tone: "failed" as const, label: dict.failed }
        : { tone: "loading" as const, label: dict.loading };

  const rows = info
    ? [
        { key: "name", label: dict.fields.name, value: info.name },
        { key: "version", label: dict.fields.version, value: info.version },
        { key: "environment", label: dict.fields.environment, value: info.environment },
        { key: "apiPrefix", label: dict.fields.apiPrefix, value: info.api_prefix },
      ]
    : [];

  return (
    <section
      aria-labelledby="backend-heading"
      className="rounded-lg border border-border bg-surface p-6 shadow-sm"
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h2 id="backend-heading" className="text-lg font-semibold">
          {dict.heading}
        </h2>
        <StatusPill tone={pill.tone} label={pill.label} />
      </div>

      <div aria-live="polite" aria-atomic="true">
        {status === "error" && (
          <div className="rounded-sm bg-danger-bg p-4 text-danger">
            <p className="font-medium">{dict.failed}</p>
            <p className="mt-1 text-sm">{dict.hint}</p>
            {errorMessage && <p className="mt-2 text-sm opacity-80">{errorMessage}</p>}
          </div>
        )}

        {status === "ready" && (
          <>
            <p className="mb-4 text-fg-muted">{dict.description}</p>
            <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
              {rows.map((row) => (
                <div key={row.key} className="min-w-0">
                  <dt className="text-sm text-fg-subtle">{row.label}</dt>
                  <dd className="tabular mt-1 truncate font-medium">{row.value}</dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </div>
    </section>
  );
}
