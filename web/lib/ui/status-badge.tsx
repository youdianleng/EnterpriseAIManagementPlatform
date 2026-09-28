import type { ReactNode } from "react";

import { cn } from "@/lib/ui/cn";

/**
 * Tone of a state, from the design system's fixed semantic set (§2.4).
 *
 * `neutral` is deliberately not "grey because we did not decide": it is the tone for
 * a state that carries no judgement — a withdrawal, a day with nothing expected.
 */
export type StatusTone = "success" | "warning" | "danger" | "info" | "neutral";

const TONE_CLASSES: Record<StatusTone, string> = {
  success: "bg-success-bg text-success",
  warning: "bg-warning-bg text-warning",
  danger: "bg-danger-bg text-danger",
  info: "bg-info-bg text-info",
  neutral: "bg-neutral-bg text-neutral",
};

/** Icon per tone. Two states never share one, so the shape is information too. */
const TONE_ICONS: Record<StatusTone, ReactNode> = {
  success: (
    <path d="M3 8.5 6.5 12 13 4.5" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  ),
  // A clock face: a state that is still going on.
  info: (
    <>
      <circle cx="8" cy="8" r="5.5" strokeWidth="1.6" />
      <path d="M8 5v3.2l2 1.4" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
  warning: (
    <>
      <path d="M8 3.5 14 13.5H2z" strokeWidth="1.6" strokeLinejoin="round" />
      <path d="M8 7v3" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="8" cy="11.8" r="0.8" className="fill-current stroke-none" />
    </>
  ),
  danger: (
    <>
      <circle cx="8" cy="8" r="5.5" strokeWidth="1.6" />
      <path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
  // A dash: a state that records the absence of something.
  neutral: <path d="M4 8h8" strokeWidth="1.8" strokeLinecap="round" />,
};

/**
 * State as a badge: icon + word + colour, never colour alone.
 *
 * One component for every state in the product, because §5's rule is about a habit
 * rather than about a screen: a helper that draws a coloured square is how a status
 * ends up conveyed by hue on one page and by words on the next. The word is required
 * — there is no icon-only form of this component — so the rule cannot be forgotten
 * at a call site.
 *
 * The icon is `aria-hidden`: it repeats the word beside it, and a screen reader
 * announcing "clock icon working" is noise. Nothing here is sized in pixels, so a
 * Spanish label that runs 20% longer widens the badge instead of clipping it (§3.1).
 */
export function StatusBadge({
  tone,
  label,
  className,
}: {
  tone: StatusTone;
  label: string;
  className?: string;
}) {
  return (
    <span
      className={cn(
        // `whitespace-nowrap`: a badge is one phrase, and "Jornada cerrada" broken over two
        // lines reads as two states stacked. Where the space genuinely is not there, the
        // layout gives the badge room (or drops a column) rather than the badge losing its
        // shape — §3.1 forbids the other answer, which is truncating it.
        "inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-sm font-medium whitespace-nowrap",
        TONE_CLASSES[tone],
        className,
      )}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        className="size-3.5 shrink-0 fill-none stroke-current"
      >
        {TONE_ICONS[tone]}
      </svg>
      {label}
    </span>
  );
}
