"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  effectivePunches,
  elapsedSeconds,
  openShiftStart,
  punch,
  readDay,
  readPunches,
  type AttendanceDay,
  type DayDetail,
  type EffectivePunch,
} from "@/lib/api/attendance";
import { ApiError } from "@/lib/api/client";
import { catalogueErrorText } from "@/lib/api/error-text";
import { holidayName, scheduleName, type MySchedule } from "@/lib/api/schedule";
import { formatDate, formatDuration, formatElapsed, formatTime } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import {
  anomalyLabel,
  dayStatusHint,
  dayStatusLabel,
  dayStatusTone,
} from "@/lib/ui/attendance-words";
import { Button } from "@/lib/ui/button";
import { Dialog } from "@/lib/ui/dialog";
import { StatusBadge } from "@/lib/ui/status-badge";
import { DataTable, type Column } from "@/lib/ui/table";

/**
 * Today's clock (ticket 21).
 *
 * One protagonist: the state of today's shift, with one button under it. Everything else
 * on the page — the punches, the pattern behind the day, what was flagged — is evidence
 * for that state, and each of it is quieter than the state itself (§4.2).
 *
 * Five decisions worth naming:
 *
 * * **The state is a word, an icon and a colour, in that order** (§5). The three states
 *   the ticket names — working / clocked out / not clocked in yet — come from the API's
 *   own derived day status rather than from inspecting the punches here, and the days
 *   nobody was expected (a holiday, a weekday the pattern does not work) are states
 *   rather than gaps. None of them is an error.
 * * **The secondary states are designed, not reported.** "Already clocked in" (a stale
 *   tab pressing the button twice) and "no schedule reaches you" arrive as their own
 *   neutral or informational blocks. A red box for either would tell somebody their day
 *   is broken when it is not.
 * * **A day flagged `missing_out` offers no clock-out button.** A shift that ran past the
 *   maximum cannot be closed by a punch — the API refuses it with its own code — so the
 *   screen says so and points at the attendance record instead of drawing a button whose
 *   write would fail. The same rule the timesheet grid applies to a closed week.
 * * **Closing the day is confirmed; opening it is not.** Clocking out is the one act
 *   here that cannot be undone in place: no endpoint changes a punch, so a mis-click
 *   costs a correction approved by two people. The dialog states the time it is about to
 *   record. Clocking in is confirmed by its own effect — the state and the timer change
 *   under the reader's eyes — so it does not get a dialog.
 * * **The timer is the only thing on the page that moves.** It is its own component with
 *   its own interval, and it renders nothing until it has a browser clock, so the
 *   server-rendered markup and the hydrated markup are identical and React never reports
 *   a mismatch about an hour.
 */
export function ClockScreen({
  dict,
  locale,
  initialDay,
  initialDetail,
  initialSchedule,
}: {
  dict: Dictionary;
  locale: Locale;
  initialDay: AttendanceDay | null;
  initialDetail: DayDetail | null;
  initialSchedule: MySchedule | null;
}) {
  const t = dict.clock;
  const router = useRouter();

  const [day, setDay] = useState(initialDay);
  const [detail, setDetail] = useState(initialDetail);
  const [schedule, setSchedule] = useState(initialSchedule);
  const [busy, setBusy] = useState<"clock_in" | "clock_out" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [alreadyOpen, setAlreadyOpen] = useState(false);
  /**
   * The instant the confirmation is about, captured when the dialog opens.
   *
   * Not `new Date()` inside the render: the `<dialog>`'s children are rendered even while
   * it is closed, so a clock read during render would be evaluated on the server and
   * again in the browser and the two minutes would not always match — a hydration
   * mismatch reported over a number nobody asked to see yet.
   */
  const [confirmAt, setConfirmAt] = useState<string | null>(null);

  // The server's read is the truth: our refresh after a punch, or the browser's after a
  // navigation, replaces what is on screen.
  useEffect(() => setDay(initialDay), [initialDay]);
  useEffect(() => setDetail(initialDetail), [initialDetail]);
  useEffect(() => setSchedule(initialSchedule), [initialSchedule]);

  const refresh = useCallback(async () => {
    try {
      const [nextDay, nextDetail] = await Promise.all([readDay(), readPunches()]);
      setDay(nextDay);
      setDetail(nextDetail);
    } catch {
      // A failed re-read is not worth a message of its own: the server render that the
      // refresh below triggers will replace both, and the state on screen is still the
      // last one the API gave us.
    }
  }, []);

  async function submit(kind: "clock_in" | "clock_out") {
    setBusy(kind);
    setError(null);
    setNotice(null);
    setAlreadyOpen(false);
    try {
      const event = await punch(kind);
      setNotice(
        (kind === "clock_in" ? t.confirmedIn : t.confirmedOut).replace(
          "{time}",
          formatTime(event.occurred_at, locale),
        ),
      );
      await refresh();
      router.refresh();
    } catch (cause) {
      // A second clock-in while one is open is not a failure: it is a stale tab, and the
      // useful answer is the state the screen is about to show.
      if (cause instanceof ApiError && cause.messageKey === "errors.attendance_already_clocked_in") {
        setAlreadyOpen(true);
        await refresh();
        router.refresh();
      } else {
        setError(catalogueErrorText(cause, dict, t.error));
      }
    } finally {
      setBusy(null);
      setConfirmAt(null);
    }
  }

  if (day === null || detail === null) {
    return (
      <div className="flex flex-col gap-8">
        <Header dict={dict} locale={locale} businessDate={null} />
        <Alert tone="danger" role="alert" title={t.error} className="max-w-2xl">
          <p className="mt-1">{t.errorHint}</p>
          <Button variant="secondary" size="sm" className="mt-3" onClick={() => router.refresh()}>
            {t.retry}
          </Button>
        </Alert>
      </div>
    );
  }

  const restDay = day.status === "holiday" || day.status === "non_working";
  const noSchedule = day.expected_minutes === null;
  const working = day.status === "working";
  const openStart = openShiftStart(detail);
  // A shift too old for a punch to close: the API refuses the clock-out, so it is not
  // offered. The correction flow is the way out, and the state hint says so.
  const correctInstead = day.status === "missing_out";
  const primary: "clock_in" | "clock_out" | null = correctInstead
    ? null
    : working
      ? "clock_out"
      : "clock_in";
  const rows = effectivePunches(detail);

  return (
    <div className="flex flex-col gap-8">
      <Header dict={dict} locale={locale} businessDate={day.business_date} />

      {notice && (
        <Alert tone="success" role="status" className="max-w-2xl">
          {notice}
        </Alert>
      )}
      {error && (
        <Alert tone="danger" role="alert" title={t.error} className="max-w-2xl">
          <p className="mt-1">{error}</p>
        </Alert>
      )}
      {alreadyOpen && (
        <Alert tone="info" className="max-w-2xl" title={t.alreadyClockedIn.title}>
          <p className="mt-1">{t.alreadyClockedIn.body}</p>
        </Alert>
      )}

      {/* The protagonist: what today is, and the one thing to do about it. */}
      <section
        aria-labelledby="clock-state-heading"
        className="rounded-lg border border-border bg-surface p-6 shadow-sm"
        data-testid="clock-state"
        data-day-status={day.status}
      >
        <h2 id="clock-state-heading" className="sr-only">
          {t.title}
        </h2>
        <div className="flex flex-wrap items-start justify-between gap-x-8 gap-y-4">
          <div className="min-w-0">
            <StatusBadge tone={dayStatusTone(day.status)} label={dayStatusLabel(dict, day.status)} />
            <p className="mt-3 max-w-xl text-fg-muted">{dayStatusHint(dict, day.status)}</p>

            <p className="mt-4 flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-fg-subtle">{working ? t.elapsed : t.worked}</span>
              {working && openStart !== null ? (
                <RunningTimer
                  startedAt={openStart}
                  locale={locale}
                  className="tabular text-3xl font-semibold"
                />
              ) : (
                <span className="tabular text-3xl font-semibold" data-testid="clock-worked">
                  {formatDuration(day.worked_minutes, locale)}
                </span>
              )}
            </p>

            {/* While a shift is open the closed intervals are the quieter figure: the
                number above is the one that answers "how long have I been here". */}
            {working && (
              <p className="mt-1 text-sm text-fg-subtle">
                {t.worked}:{" "}
                <span className="tabular">{formatDuration(day.worked_minutes, locale)}</span>
              </p>
            )}

            <p className="mt-2 text-sm text-fg-muted" data-testid="clock-expected">
              {restDay
                ? t.restDay
                : noSchedule
                  ? t.expectedUnknown
                  : t.expected.replace(
                      "{duration}",
                      formatDuration(day.expected_minutes ?? 0, locale),
                    )}
            </p>
          </div>

          <div className="flex flex-col items-start gap-2">
            {primary === "clock_in" && (
              <Button
                onClick={() => submit("clock_in")}
                disabled={busy !== null}
                icon={<ClockIcon />}
              >
                {busy === "clock_in" ? t.action.clockingIn : t.action.clockIn}
              </Button>
            )}
            {primary === "clock_out" && (
              // Primary, like the clock-in button it replaces: which of the two is on
              // screen is the state, and the one that is there is the page's main act
              // (§4.2). The confirmation dialog is what makes closing the day safe, not a
              // quieter button.
              <Button
                onClick={() => setConfirmAt(new Date().toISOString())}
                disabled={busy !== null}
                icon={<ClockIcon />}
              >
                {busy === "clock_out" ? t.action.clockingOut : t.action.clockOut}
              </Button>
            )}
            {primary === null && (
              <a
                href={`/${locale}/attendance`}
                className="inline-flex min-h-11 items-center rounded border border-border bg-surface px-4 font-medium text-fg hover:bg-neutral-bg"
              >
                {dict.nav.attendance}
              </a>
            )}
          </div>
        </div>
      </section>

      {/* No pattern reaches this person: the absence of a rule, not a zero. Said once, in
          the informational tone, because nothing is wrong. */}
      {noSchedule && !restDay && (
        <Alert tone="info" className="max-w-2xl" title={t.noSchedule.title}>
          <p className="mt-1">{t.noSchedule.body}</p>
        </Alert>
      )}

      <section
        aria-labelledby="clock-day-heading"
        className="rounded-lg border border-border bg-surface p-6 shadow-sm"
      >
        <h2 id="clock-day-heading" className="text-lg font-semibold">
          {t.schedule.heading}
        </h2>
        <dl className="mt-3 grid gap-x-8 gap-y-3 sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)]">
          <dt className="text-fg-subtle">{t.schedule.patternLabel}</dt>
          <dd>
            {schedule?.schedule
              ? scheduleName(schedule.schedule, locale)
              : t.schedule.patternNone}
          </dd>

          <dt className="text-fg-subtle">{t.schedule.sourceLabel}</dt>
          <dd>{schedule === null ? t.schedule.source.none : t.schedule.source[schedule.source]}</dd>

          <dt className="text-fg-subtle">{t.schedule.holidayLabel}</dt>
          <dd>
            {schedule?.holiday ? holidayName(schedule.holiday, locale) : t.schedule.holidayNone}
          </dd>

          <dt className="text-fg-subtle">{t.anomalies.heading}</dt>
          <dd>
            {detail.anomalies.length === 0 ? (
              t.anomalies.none
            ) : (
              <ul className="flex flex-col gap-1" data-testid="clock-anomalies">
                {detail.anomalies.map((anomaly, index) => (
                  <li
                    key={`${anomaly.type}-${index}`}
                    className="flex flex-wrap items-center gap-2"
                  >
                    <StatusBadge
                      tone={anomaly.resolved_by_event_id === null ? "warning" : "neutral"}
                      label={anomalyLabel(dict, anomaly.type)}
                    />
                    {anomaly.resolved_by_event_id !== null && (
                      <StatusBadge tone="success" label={t.anomalies.resolved} />
                    )}
                    <span className="tabular text-sm text-fg-subtle">
                      {formatTime(anomaly.detected_at, locale)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </dl>
      </section>

      <section
        aria-labelledby="clock-events-heading"
        className="rounded-lg border border-border bg-surface p-6 shadow-sm"
      >
        <h2 id="clock-events-heading" className="text-lg font-semibold">
          {t.events.heading}
        </h2>
        {rows.length === 0 ? (
          <div className="mt-3">
            <Alert tone="neutral">
              <p className="font-medium">{t.events.empty}</p>
              <p className="mt-1 text-sm">{t.events.emptyHint}</p>
            </Alert>
          </div>
        ) : (
          <div className="mt-3" data-testid="clock-punches">
            <DataTable
              columns={punchColumns(t, locale)}
              rows={rows}
              getRowKey={(row) => row.id}
              emptyMessage={t.events.empty}
              caption={t.events.heading}
            />
          </div>
        )}
      </section>

      <Dialog
        open={confirmAt !== null}
        onClose={() => setConfirmAt(null)}
        title={t.confirm.title}
        closeLabel={t.confirm.close}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirmAt(null)}>
              {t.confirm.cancel}
            </Button>
            <Button onClick={() => submit("clock_out")} disabled={busy !== null}>
              {busy === "clock_out" ? t.action.clockingOut : t.confirm.confirm}
            </Button>
          </>
        }
      >
        {/* Rendered only once there is an instant to name: the dialog element and its
            children exist even while it is closed, so an unguarded `new Date()` here would
            be evaluated on the server and again in the browser. */}
        {confirmAt !== null && (
          <p>{t.confirm.body.replace("{time}", formatTime(confirmAt, locale))}</p>
        )}
      </Dialog>
    </div>
  );
}

/** The page's one `h1`, with the business day it is about. */
function Header({
  dict,
  locale,
  businessDate,
}: {
  dict: Dictionary;
  locale: Locale;
  businessDate: string | null;
}) {
  const t = dict.clock;
  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
      <p className="mt-2 text-fg-muted">{t.intro}</p>
      {businessDate !== null && (
        <p className="tabular mt-2 text-sm text-fg-subtle" data-testid="clock-business-date">
          {t.businessDate.replace("{date}", formatDate(businessDate, locale))}
        </p>
      )}
    </div>
  );
}

/**
 * The elapsed time of the shift that is still open.
 *
 * Its own component, and its own interval, for two reasons: the tick then re-renders one
 * number instead of the whole screen once a second, and the value is rendered only after
 * the component has a browser clock. The server cannot know "now" the way the reader's
 * own device does, so rendering it on the server and hydrating it in the browser is the
 * classic React mismatch about an hour — this renders nothing on both sides until the
 * effect has run.
 */
function RunningTimer({
  startedAt,
  locale,
  className,
}: {
  startedAt: string;
  locale: Locale;
  className?: string;
}) {
  const [now, setNow] = useState<number | null>(null);

  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  if (now === null) return null;

  return (
    // `role="timer"` with no live region: the number is there to be read when the reader
    // looks at it, and a screen reader announcing a value once a second is worse than
    // useless.
    <span role="timer" className={className} data-testid="clock-elapsed">
      {formatElapsed(elapsedSeconds(startedAt, now), locale)}
    </span>
  );
}

/**
 * The derived day status as a word.
 *
 * The mapping lives in `lib/ui/attendance-words`, shared with the attendance record: the
 * API names its states `ok` and `absent` while the interface calls them "shift closed" and
 * "not clocked in yet", and one screen doing that translation would eventually disagree
 * with the other. The anomaly names come from the same module, for the same reason.
 */

/** The day's punches: when each counts, what it is, and how it got there. */
function punchColumns(t: Dictionary["clock"], locale: Locale): Array<Column<EffectivePunch>> {
  return [
    {
      key: "time",
      header: t.events.time,
      numeric: true,
      render: (row) => (
        <div className="flex flex-col items-end">
          <span className="tabular font-medium" data-testid="punch-time">
            {formatTime(row.at, locale)}
          </span>
          {/* A corrected punch shows the value in force *and* what it replaced: the
              evolution is the fact, and a reader who only saw the new time could not tell
              that anything had been changed. */}
          {row.is_corrected && (
            <span className="tabular text-sm text-fg-subtle">
              {t.events.original}: {formatTime(row.chain.punch.occurred_at, locale)}
            </span>
          )}
        </div>
      ),
    },
    {
      key: "kind",
      header: t.events.kind,
      render: (row) => (
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">
            {row.kind === "clock_in" ? t.kind.clock_in : t.kind.clock_out}
          </span>
          {row.is_made_up && <StatusBadge tone="info" label={t.events.madeUp} />}
          {row.is_corrected && !row.is_made_up && (
            <StatusBadge tone="info" label={t.events.corrected} />
          )}
        </div>
      ),
    },
    {
      key: "source",
      header: t.events.source,
      render: (row) => (
        <span className="text-fg-muted">
          {row.source === "correction" ? t.source.correction : t.source.web}
        </span>
      ),
    },
  ];
}

function ClockIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 fill-none stroke-current">
      <circle cx="8" cy="8" r="6" strokeWidth="1.6" />
      <path d="M8 4.5V8l2.5 1.7" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}
