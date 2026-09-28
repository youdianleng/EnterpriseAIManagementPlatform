"use client";

import {
  effectivePunches,
  type DayDetail,
  type PunchEvent,
} from "@/lib/api/attendance";
import { formatDuration, formatTime } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import {
  anomalyLabel,
  dayStatusHint,
  dayStatusLabel,
  dayStatusTone,
} from "@/lib/ui/attendance-words";
import { StatusBadge } from "@/lib/ui/status-badge";

/**
 * One day's record (ticket 24): what was punched, how each punch evolved, what was flagged.
 *
 * The chain is the point of this panel, and it is the ticket's own line — *several
 * corrections of the same day form a chain, and the interface shows the whole evolution*.
 * Each punch is drawn as what it was written as, then every correction in write order,
 * each naming the value it supersedes; the day's own figure (`effective_at`) closes the
 * group, so a reader sees both the history and the value in force without arithmetic.
 *
 * Two rules the drawing follows:
 *
 * * **A correction never replaces the row it restates.** The original stays on screen with
 *   its own time — that is the whole reason the stream is append-only, and a panel that
 *   hid it would undo the guarantee the API went to some trouble to keep.
 * * **Every state is a word.** `is_corrected`, `is_made_up`, resolved and unresolved are
 *   all badges carrying text, not colours (§5), because a corrected day is exactly the
 *   kind of row somebody scans quickly and misreads.
 */
export function AttendanceDayPanel({
  dict,
  locale,
  detail,
}: {
  dict: Dictionary;
  locale: Locale;
  detail: DayDetail;
}) {
  const t = dict.attendance;
  const punches = effectivePunches(detail);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <StatusBadge
          tone={dayStatusTone(detail.day.status)}
          label={dayStatusLabel(dict, detail.day.status)}
        />
        <p className="mt-2 max-w-xl text-fg-muted">{dayStatusHint(dict, detail.day.status)}</p>
      </div>

      <dl className="grid gap-x-8 gap-y-2 text-sm sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)]">
        <dt className="text-fg-subtle">{t.day.worked}</dt>
        <dd className="tabular" data-testid="attendance-day-worked">
          {formatDuration(detail.day.worked_minutes, locale)}
        </dd>

        <dt className="text-fg-subtle">{t.day.expected}</dt>
        <dd className="tabular">
          {detail.day.expected_minutes === null
            ? t.day.expectedUnknown
            : formatDuration(detail.day.expected_minutes, locale)}
        </dd>

        <dt className="text-fg-subtle">{t.day.firstIn}</dt>
        <dd className="tabular">
          {detail.day.first_in === null ? t.month.dash : formatTime(detail.day.first_in, locale)}
        </dd>

        <dt className="text-fg-subtle">{t.day.lastOut}</dt>
        <dd className="tabular">
          {detail.day.last_out === null ? t.month.dash : formatTime(detail.day.last_out, locale)}
        </dd>

        {detail.day.overtime_minutes !== null && (
          <>
            <dt className="text-fg-subtle">{t.day.overtime}</dt>
            <dd className="tabular">{formatDuration(detail.day.overtime_minutes, locale)}</dd>
          </>
        )}
      </dl>

      <div>
        <h3 className="text-base font-semibold">{t.day.chainHeading}</h3>
        {punches.length === 0 ? (
          <p className="mt-2 text-fg-muted" data-testid="attendance-day-empty">
            {t.day.noPunches}
          </p>
        ) : (
          <ul className="mt-3 flex flex-col gap-4" data-testid="attendance-day-punches">
            {punches.map((punch) => (
              <li key={punch.id} className="rounded border border-border p-3">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="font-medium">
                    {punch.kind === "clock_in"
                      ? t.correction.kind.clock_in
                      : t.correction.kind.clock_out}
                  </span>
                  <span
                    className="tabular rounded bg-neutral-bg px-2 py-0.5 text-sm font-medium text-neutral"
                    data-testid="attendance-punch-effective"
                  >
                    {t.day.effective}: {formatTime(punch.at, locale)}
                  </span>
                  {punch.is_corrected && <StatusBadge tone="info" label={t.day.corrected} />}
                  {punch.is_made_up && <StatusBadge tone="info" label={t.day.madeUp} />}
                </div>

                {/* The evolution, in write order: the row as it was written, then each
                    correction that restated it. `supersedes` is the previous link — the
                    original for the first correction, the first correction for the second —
                    which is what makes this a chain rather than a set of competing values. */}
                <ol className="mt-2 flex flex-col gap-1 text-sm" data-testid="attendance-chain">
                  {chainLinks(punch.chain.punch, punch.chain.corrections).map((link) => (
                    <li
                      key={link.event.id}
                      className="flex flex-wrap items-baseline gap-x-2 gap-y-1 border-l border-border pl-3"
                    >
                      <span className="tabular font-medium">
                        {formatTime(link.event.occurred_at, locale)}
                      </span>
                      <span className="text-fg-muted">
                        {link.supersedes === null ? t.day.original : t.day.correction}
                      </span>
                      {link.supersedes !== null && (
                        <span className="tabular text-fg-subtle">
                          {t.day.supersedes} {formatTime(link.supersedes.occurred_at, locale)}
                        </span>
                      )}
                      {link.event.reason !== null && (
                        <span className="text-fg-muted">
                          {t.day.reason}: “{link.event.reason}”
                        </span>
                      )}
                    </li>
                  ))}
                </ol>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <h3 className="text-base font-semibold">{t.day.anomaliesHeading}</h3>
        {detail.anomalies.length === 0 ? (
          <p className="mt-2 text-fg-muted">{t.day.anomaliesNone}</p>
        ) : (
          <ul className="mt-3 flex flex-col gap-2" data-testid="attendance-day-anomalies">
            {detail.anomalies.map((anomaly, index) => (
              <li
                key={`${anomaly.type}-${index}`}
                className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm"
              >
                <StatusBadge
                  tone={anomaly.resolved_by_event_id === null ? "warning" : "neutral"}
                  label={anomalyLabel(dict, anomaly.type)}
                />
                <StatusBadge
                  tone={anomaly.resolved_by_event_id === null ? "warning" : "success"}
                  label={anomaly.resolved_by_event_id === null ? t.day.unresolved : t.day.resolved}
                />
                <span className="tabular text-fg-subtle">
                  {t.day.detectedAt} {formatTime(anomaly.detected_at, locale)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/**
 * The chain as an ordered list of links, each naming what it supersedes.
 *
 * The first link is the event as it was written and supersedes nothing; every later link
 * is a correction that restates the link before it. Exported because it is the rule the
 * ticket asks the interface to show, and a rule is easier to trust when it is one
 * function rather than an inline expression inside a `map`.
 */
export function chainLinks(
  punch: PunchEvent,
  corrections: PunchEvent[],
): Array<{ event: PunchEvent; supersedes: PunchEvent | null }> {
  return [
    { event: punch, supersedes: null },
    ...corrections.map((event, index) => ({
      event,
      supersedes: index === 0 ? punch : corrections[index - 1],
    })),
  ];
}
