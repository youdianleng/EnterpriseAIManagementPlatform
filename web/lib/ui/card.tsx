import type { ReactNode } from "react";

import { cn } from "@/lib/ui/cn";

/** Page section: one heading, optional description, optional actions. */
export function Card({
  title,
  description,
  actions,
  children,
  className,
  headingId,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  headingId?: string;
}) {
  return (
    <section
      aria-labelledby={headingId}
      className={cn("rounded-lg border border-border bg-surface p-6 shadow-sm", className)}
    >
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id={headingId} className="text-lg font-semibold">
            {title}
          </h2>
          {description && <p className="mt-1 text-fg-muted">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  );
}
