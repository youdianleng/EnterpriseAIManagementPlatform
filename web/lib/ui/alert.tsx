import type { ReactNode } from "react";

import { cn } from "@/lib/ui/cn";

export type AlertTone = "info" | "success" | "warning" | "danger" | "neutral";

/**
 * Inline message block.
 *
 * `role` is a parameter rather than a constant: a static notice should not be
 * announced, while a validation failure must interrupt. Getting this wrong in
 * either direction is a real accessibility defect, so it stays explicit.
 */
const TONE_CLASSES: Record<AlertTone, string> = {
  info: "bg-info-bg text-info",
  success: "bg-success-bg text-success",
  warning: "bg-warning-bg text-warning",
  danger: "bg-danger-bg text-danger",
  neutral: "bg-neutral-bg text-neutral",
};

export function Alert({
  tone = "info",
  title,
  children,
  role,
  className,
}: {
  tone?: AlertTone;
  title?: string;
  children?: ReactNode;
  role?: "alert" | "status";
  className?: string;
}) {
  if (!role) {
    return (
      <div className={cn("rounded p-4", TONE_CLASSES[tone], className)}>
        {title && <p className="font-medium">{title}</p>}
        {children}
      </div>
    );
  }

  return (
    <div role={role} aria-live={role === "status" ? "polite" : undefined} className={cn("rounded p-4", TONE_CLASSES[tone], className)}>
      {title && <p className="font-medium">{title}</p>}
      {children}
    </div>
  );
}
