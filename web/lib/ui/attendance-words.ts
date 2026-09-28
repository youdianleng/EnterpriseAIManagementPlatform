import type { DayStatus } from "@/lib/api/attendance";
import type { Dictionary } from "@/lib/i18n/dictionaries";
import type { StatusTone } from "@/lib/ui/status-badge";

/**
 * The two closed sets of attendance states, in words — in one place.
 *
 * Two screens show a day's state and its anomalies (the clock and the attendance record),
 * and the API's names for them (`ok`, `absent`, `non_working`, `no_punches`) are not the
 * interface's ("shift closed", "not clocked in yet", "non-working day", "a working day
 * with no punches at all"). Mapping that here is what keeps the two screens saying the
 * same thing about the same day.
 *
 * An explicit `switch` rather than `dict[status]` is deliberate: it makes TypeScript
 * refuse a state the API has grown until it has been given words, where an index would
 * silently render `undefined` in the one case nobody looks at.
 *
 * The tone is part of the same mapping for the same reason: `missing_out` is the one day
 * state that is genuinely a fault, and two screens deciding that separately would
 * eventually decide it differently.
 */
export function dayStatusLabel(dict: Dictionary, status: DayStatus): string {
  const labels = dict.dayStatus.label;
  switch (status) {
    case "working":
      return labels.working;
    case "ok":
      return labels.finished;
    case "missing_out":
      return labels.missingOut;
    case "incomplete":
      return labels.incomplete;
    case "holiday":
      return labels.holiday;
    case "non_working":
      return labels.nonWorking;
    case "absent":
      return labels.notStarted;
  }
}

/** What the state means for the person reading it. */
export function dayStatusHint(dict: Dictionary, status: DayStatus): string {
  const hints = dict.dayStatus.hint;
  switch (status) {
    case "working":
      return hints.working;
    case "ok":
      return hints.finished;
    case "missing_out":
      return hints.missingOut;
    case "incomplete":
      return hints.incomplete;
    case "holiday":
      return hints.holiday;
    case "non_working":
      return hints.nonWorking;
    case "absent":
      return hints.notStarted;
  }
}

/** The semantic tone each state carries. `neutral` means "not a problem". */
export function dayStatusTone(status: DayStatus): StatusTone {
  switch (status) {
    case "working":
      return "info";
    case "ok":
      return "success";
    case "missing_out":
      return "danger";
    case "incomplete":
      return "warning";
    case "absent":
    case "holiday":
    case "non_working":
      return "neutral";
  }
}

/** An anomaly's name, with a sentence for a type this build has not learned yet. */
export function anomalyLabel(dict: Dictionary, type: string): string {
  const known = dict.anomalyType as Record<string, string | undefined>;
  return known[type] ?? dict.anomalyType.unknown;
}

/**
 * Several anomaly types as one line, for a month row that has room for a phrase.
 *
 * Duplicates are dropped — a day flagged twice for the same reason is one fact — and the
 * order is the API's own, so two runs of the same month read identically.
 */
export function anomalyList(dict: Dictionary, types: string[]): string {
  const seen = new Set<string>();
  const labels: string[] = [];
  for (const type of types) {
    if (seen.has(type)) continue;
    seen.add(type);
    labels.push(anomalyLabel(dict, type));
  }
  return labels.join(" · ");
}

/**
 * Whether a day's record is worth opening on the month view.
 *
 * The two states that mean *something was flagged* rather than *nothing happened*: a shift
 * left open, and punches that cannot be paired. `absent` is deliberately not on the list —
 * "nobody punched" is already legible from the status word, it is the ordinary case on a
 * month with a gap in it, and treating it as a flag would both mislead the reader and turn
 * one month view into thirty requests, since the API serves anomalies a day at a time.
 */
export function statusNeedsAttention(status: DayStatus): boolean {
  return status === "missing_out" || status === "incomplete";
}
