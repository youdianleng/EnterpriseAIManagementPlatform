"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import {
  draftCorrection,
  exportRecordUrl,
  readCorrections,
  readPunches,
  readRange,
  submitCorrection,
  type AttendanceDay,
  type AttendanceRange,
  type Correction,
  type CorrectionState,
  type DayDetail,
} from "@/lib/api/attendance";
import { catalogueErrorText } from "@/lib/api/error-text";
import { formatDate, formatDuration, formatMonth, formatNumber, formatTime } from "@/lib/format";
import { madridInstant, shiftMonth } from "@/lib/format/day";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import {
  anomalyList,
  dayStatusLabel,
  dayStatusTone,
  statusNeedsAttention,
} from "@/lib/ui/attendance-words";
import { Button } from "@/lib/ui/button";
import { Card } from "@/lib/ui/card";
import { SelectField, TextAreaField, TextField } from "@/lib/ui/field";
import { StatusBadge, type StatusTone } from "@/lib/ui/status-badge";

import { AttendanceDayPanel } from "./attendance-day";

/**
 * The month's record and the correction flow (ticket 24).
 *
 * One protagonist: the correction request. Everything else on the page is the record the
 * request is about — the month's days, the selected day, the chains showing what earlier
 * corrections did — and every one of those is a quieter control than the form's single
 * primary button (§4.2).
 *
 * Six decisions worth naming:
 *
 * * **The month is one request.** `/attendance/range` answers every day in the window, gaps
 *   included, which is what lets a month with three days off render as a month with three
 *   `absent` days rather than as a list of the days somebody happened to punch.
 * * **Anomalies cost a request per flagged day, and only for flagged days.** The API serves
 *   anomalies one day at a time, each with its own detection and resolution times. Reading
 *   them for all thirty days would be thirty requests; reading them for the two states that
 *   mean *something was flagged* (`missing_out`, `incomplete`) is bounded by the number of
 *   problems in the month — which is the number the reader came to find. A day's full list,
 *   resolved ones included, is on the day panel.
 * * **The month and the day are the URL.** They can be linked, reloaded and reached with
 *   Back, and the server's first paint is what the reader asked for.
 * * **Picking a day takes the reader to it.** The table is the navigator and the panel is
 *   below it, so a selection scrolls the panel into view rather than leaving the reader at
 *   the table — with `prefers-reduced-motion` respected (§2.6).
 * * **Filing is two calls, because the API makes it two acts.** A correction is drafted and
 *   then submitted to the engine. The screen does both from one button but reports the state
 *   it reached: a draft that could not be filed is a document the reader can still see and
 *   act on, not a lost request.
 * * **The instant is built in Madrid's zone.** `madridInstant` resolves the offset for the
 *   wall-clock time the person typed; the API refuses a naive instant, and using the
 *   browser's own offset for a date across a DST switch would move the punch by an hour.
 */
export function AttendanceScreen({
  dict,
  locale,
  monthStart,
  monthEnd,
  todayDate,
  selectedDay,
  initialRange,
  initialDetail,
  initialCorrections,
  initialCorrectionsTotal,
  expectedMinutes,
  expectedIsSnapshot,
}: {
  dict: Dictionary;
  locale: Locale;
  monthStart: string;
  monthEnd: string;
  todayDate: string;
  selectedDay: string;
  initialRange: AttendanceRange | null;
  initialDetail: DayDetail | null;
  initialCorrections: Correction[] | null;
  initialCorrectionsTotal: number | null;
  expectedMinutes: number | null;
  expectedIsSnapshot: boolean | null;
}) {
  const t = dict.attendance;
  const router = useRouter();

  const [range, setRange] = useState(initialRange);
  const [detail, setDetail] = useState(initialDetail);
  const [corrections, setCorrections] = useState(initialCorrections);
  const [correctionsTotal, setCorrectionsTotal] = useState(initialCorrectionsTotal);
  const [day, setDay] = useState(selectedDay);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // The server's read is the truth: our refresh after a write, or the browser's after a
  // navigation, replaces what is on screen.
  useEffect(() => setRange(initialRange), [initialRange]);
  useEffect(() => setDetail(initialDetail), [initialDetail]);
  useEffect(() => setCorrections(initialCorrections), [initialCorrections]);
  useEffect(() => setCorrectionsTotal(initialCorrectionsTotal), [initialCorrectionsTotal]);
  useEffect(() => setDay(selectedDay), [selectedDay]);

  const days = useMemo(() => range?.days ?? [], [range]);

  /**
   * The anomaly types of the days that were flagged, keyed by business date.
   *
   * Keyed on the *set* of flagged days rather than on the days themselves, so a re-render
   * cannot start a second round of requests for the same month.
   */
  const [anomalies, setAnomalies] = useState<Record<string, string[]>>({});
  const flaggedKey = useMemo(
    () =>
      days
        .filter((entry) => statusNeedsAttention(entry.status))
        .map((entry) => entry.business_date)
        .join(","),
    [days],
  );

  useEffect(() => {
    const wanted = flaggedKey === "" ? [] : flaggedKey.split(",");
    if (wanted.length === 0) {
      setAnomalies({});
      return;
    }
    let cancelled = false;
    void (async () => {
      const found = await Promise.all(
        wanted.map(async (businessDate) => {
          try {
            const record = await readPunches(businessDate);
            return [businessDate, record.anomalies.map((anomaly) => anomaly.type)] as const;
          } catch {
            // A day whose detail cannot be read still renders: the status word on the row
            // is the API's own answer, and losing a marker is better than losing the month.
            return [businessDate, []] as const;
          }
        }),
      );
      if (!cancelled) setAnomalies(Object.fromEntries(found));
    })();
    return () => {
      cancelled = true;
    };
  }, [flaggedKey]);

  /** Move to another month. The URL is the state, so Back works and the month is linkable. */
  function goToMonth(target: string) {
    router.push(`/${locale}/attendance?month=${target.slice(0, 7)}&day=${target}`, {
      scroll: false,
    });
  }

  /** Open one day's record, and take the reader to it. */
  function selectDay(businessDate: string) {
    setDay(businessDate);
    router.push(`/${locale}/attendance?month=${monthStart.slice(0, 7)}&day=${businessDate}`, {
      scroll: false,
    });
    const panel = document.getElementById("attendance-day");
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    panel?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
  }

  const refresh = useCallback(
    async (businessDate: string) => {
      try {
        const [nextRange, nextDetail, nextCorrections] = await Promise.all([
          readRange(monthStart, monthEnd),
          readPunches(businessDate),
          readCorrections(),
        ]);
        setRange(nextRange);
        setDetail(nextDetail);
        setCorrections(nextCorrections.items);
        setCorrectionsTotal(nextCorrections.total);
      } catch {
        // The server render the caller triggers next replaces all three; the state on screen
        // is still the last one the API gave us.
      }
    },
    [monthStart, monthEnd],
  );

  async function fileCorrection(input: {
    business_date: string;
    kind: "clock_in" | "clock_out";
    time: string;
    reason: string;
  }): Promise<boolean> {
    const correctedAt = madridInstant(input.business_date, input.time);
    if (Date.parse(correctedAt) > Date.now()) {
      setFormError(t.correction.future);
      return false;
    }

    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const draft = await draftCorrection({
        business_date: input.business_date,
        kind: input.kind,
        corrected_at: correctedAt,
        reason: input.reason,
      });
      // The draft exists from here on: if the submission is refused, the document is still
      // on screen with its state, which is why the two acts are reported separately.
      await submitCorrection(draft.id);
      setNotice(t.correction.filed);
      setDay(input.business_date);
      await refresh(input.business_date);
      router.refresh();
      return true;
    } catch (cause) {
      setError(catalogueErrorText(cause, dict, t.error));
      await refresh(input.business_date);
      return false;
    } finally {
      setBusy(false);
    }
  }

  if (range === null) {
    return (
      <div className="flex flex-col gap-8">
        <Header dict={dict} locale={locale} monthStart={monthStart} />
        <Alert tone="danger" role="alert" title={t.error} className="max-w-2xl">
          <p className="mt-1">{t.errorHint}</p>
          <Button variant="secondary" size="sm" className="mt-3" onClick={() => router.refresh()}>
            {t.retry}
          </Button>
        </Alert>
      </div>
    );
  }

  const totals = monthTotals(days);
  const flaggedDays = days.filter((entry) => statusNeedsAttention(entry.status)).length;

  return (
    <div className="flex flex-col gap-8">
      <Header dict={dict} locale={locale} monthStart={monthStart} />

      <nav aria-label={t.monthNavLabel} className="flex flex-wrap items-center gap-2 text-sm">
        <button
          type="button"
          onClick={() => goToMonth(shiftMonth(monthStart, -1))}
          className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
        >
          ← {t.previousMonth}
        </button>
        <button
          type="button"
          onClick={() => goToMonth(monthStart)}
          className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
        >
          {t.thisMonth}
        </button>
        <button
          type="button"
          onClick={() => goToMonth(shiftMonth(monthStart, 1))}
          className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
        >
          {t.nextMonth} →
        </button>
        <span className="tabular text-fg-subtle" data-testid="attendance-month">
          {t.monthOf.replace("{month}", formatMonth(monthStart, locale))}
        </span>
        <a
          href={exportRecordUrl(monthStart, monthEnd)}
          className="ml-auto inline-flex min-h-9 items-center rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
          title={t.export.hint}
        >
          {t.export.label}
        </a>
      </nav>

      {notice && (
        <Alert tone="success" role="status">
          {notice}
        </Alert>
      )}
      {error && (
        // Its own heading: the record above loaded perfectly well — it is the *request* that
        // was refused, and telling the reader their record could not be loaded would be a
        // different (and false) statement about what happened.
        <Alert tone="danger" role="alert" title={t.correction.refused}>
          <p className="mt-1">{error}</p>
        </Alert>
      )}

      <Card title={t.summary.heading} headingId="attendance-summary-heading">
        <dl className="grid gap-4 sm:grid-cols-3">
          <div>
            <dt className="text-fg-subtle">{t.summary.worked}</dt>
            <dd className="tabular text-xl font-semibold" data-testid="attendance-month-worked">
              {formatDuration(totals.worked, locale)}
            </dd>
          </div>
          <div>
            <dt className="text-fg-subtle">{t.summary.expected}</dt>
            <dd className="tabular text-xl font-semibold">
              {expectedMinutes === null
                ? t.summary.expectedUnknown
                : formatDuration(expectedMinutes, locale)}
            </dd>
            {/* Which of the two the figure is matters: a snapshot is frozen with the rules
                that produced it, a live answer reflects a schedule edited since. */}
            {expectedIsSnapshot !== null && (
              <p className="mt-1 text-sm text-fg-subtle">
                {expectedIsSnapshot ? t.summary.expectedSnapshot : t.summary.expectedLive}
              </p>
            )}
          </div>
          <div>
            <dt className="text-fg-subtle">{t.summary.flaggedLabel}</dt>
            {/* A bare number in the value cell, so the sentence needs no plural form:
                "1 días" is the kind of thing a message with `{count}` inside it produces
                in Spanish and "1 days" in English, and neither is worth a plural rule. */}
            <dd className="tabular text-xl font-semibold" data-testid="attendance-month-flagged">
              {flaggedDays === 0 ? t.summary.flaggedNone : formatNumber(flaggedDays, locale)}
            </dd>
          </div>
        </dl>
      </Card>

      <section aria-labelledby="attendance-month-heading" className="flex flex-col gap-3">
        <h2 id="attendance-month-heading" className="text-lg font-semibold">
          {t.month.heading}
        </h2>
        {days.length === 0 ? (
          <Alert tone="neutral">
            <p className="font-medium">{t.month.empty}</p>
            <p className="mt-1 text-sm">{t.month.emptyHint}</p>
          </Alert>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-border bg-surface">
            <table className="w-full border-collapse text-sm" data-testid="attendance-month-table">
              <caption className="sr-only">{t.month.caption}</caption>
              <thead>
                <tr className="border-b border-border text-left">
                  <th scope="col" className="px-3 py-2 font-medium text-fg-muted">
                    {t.month.date}
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium text-fg-muted">
                    {t.month.status}
                  </th>
                  {/* Below 640px the table keeps the day and its state and drops the
                      minutes: a badge that reads "Jornada cerrada" needs the room, and the
                      day panel below has every figure for whichever day is opened. A
                      measured downgrade rather than a squeezed table (§3.1, §7). */}
                  <th
                    scope="col"
                    className="hidden px-3 py-2 text-right font-medium text-fg-muted sm:table-cell"
                  >
                    {t.month.worked}
                  </th>
                  {/* Below 768px the table keeps these three columns rather than scrolling:
                      §7 allows a horizontal scroll on a tablet, and squeezing six columns
                      onto a phone is the outcome the design system rejects. */}
                  <th
                    scope="col"
                    className="hidden px-3 py-2 text-right font-medium text-fg-muted md:table-cell"
                  >
                    {t.month.firstIn}
                  </th>
                  <th
                    scope="col"
                    className="hidden px-3 py-2 text-right font-medium text-fg-muted md:table-cell"
                  >
                    {t.month.lastOut}
                  </th>
                  <th
                    scope="col"
                    className="hidden px-3 py-2 text-right font-medium text-fg-muted md:table-cell"
                  >
                    {t.month.expected}
                  </th>
                </tr>
              </thead>
              <tbody>
                {days.map((entry) => (
                  <MonthRow
                    key={entry.business_date}
                    dict={dict}
                    locale={locale}
                    day={entry}
                    flags={anomalies[entry.business_date] ?? []}
                    selected={entry.business_date === day}
                    onSelect={() => selectDay(entry.business_date)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section
        id="attendance-day"
        aria-labelledby="attendance-day-heading"
        className="scroll-mt-4 rounded-lg border border-border bg-surface p-6 shadow-sm"
      >
        <h2 id="attendance-day-heading" className="text-lg font-semibold">
          {t.day.heading} · <span className="tabular">{formatDate(day, locale)}</span>
        </h2>
        {detail === null ? (
          <p className="mt-3 text-fg-muted">{t.day.selectPrompt}</p>
        ) : (
          <div className="mt-4 grid gap-8 lg:grid-cols-2">
            <AttendanceDayPanel dict={dict} locale={locale} detail={detail} />
            <CorrectionForm
              dict={dict}
              locale={locale}
              selectedDay={day}
              todayDate={todayDate}
              busy={busy}
              error={formError}
              onError={setFormError}
              onSubmit={fileCorrection}
            />
          </div>
        )}
      </section>

      <section aria-labelledby="attendance-requests-heading" className="flex flex-col gap-3">
        <h2 id="attendance-requests-heading" className="text-lg font-semibold">
          {t.correction.listHeading}
          {correctionsTotal !== null && (
            <span className="tabular ml-2 text-sm font-normal text-fg-subtle">
              {formatNumber(correctionsTotal, locale)}
            </span>
          )}
        </h2>
        {corrections === null || corrections.length === 0 ? (
          <Alert tone="neutral">
            <p className="font-medium">{t.correction.listEmpty}</p>
            <p className="mt-1 text-sm">{t.correction.listEmptyHint}</p>
          </Alert>
        ) : (
          <ul className="flex flex-col gap-3" data-testid="attendance-corrections">
            {corrections.map((document) => (
              <li
                key={document.id}
                className="rounded-lg border border-border bg-surface p-4"
                data-correction-state={document.state}
              >
                <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-2">
                      <span className="tabular font-medium">
                        {formatDate(document.business_date, locale)}
                      </span>
                      <span className="text-fg-muted">
                        {document.kind === "clock_in"
                          ? t.correction.kind.clock_in
                          : t.correction.kind.clock_out}
                      </span>
                      <span className="tabular rounded bg-neutral-bg px-2 py-0.5 text-sm font-medium text-neutral">
                        {formatTime(document.corrected_at, locale)}
                      </span>
                      <StatusBadge
                        tone={correctionTone(document.state)}
                        label={t.correction.state[document.state]}
                      />
                    </p>
                    <p className="mt-1 text-sm text-fg-muted">
                      {t.day.reason}: “{document.reason}”
                    </p>
                    <p className="mt-1 text-sm text-fg-subtle">
                      {t.correction.submittedAt}:{" "}
                      <span className="tabular">
                        {document.submitted_at
                          ? formatDate(document.submitted_at, locale)
                          : t.month.dash}
                      </span>
                      {" · "}
                      {t.correction.appliedAt}:{" "}
                      <span className="tabular">
                        {document.applied_at
                          ? formatDate(document.applied_at, locale)
                          : t.month.dash}
                      </span>
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => selectDay(document.business_date)}
                  >
                    {t.correction.viewDay}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/** The page's one `h1`, with the month it is about. */
function Header({
  dict,
  locale,
  monthStart,
}: {
  dict: Dictionary;
  locale: Locale;
  monthStart: string;
}) {
  const t = dict.attendance;
  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
      <p className="mt-2 text-fg-muted">{t.intro}</p>
      <p className="tabular mt-2 text-sm text-fg-subtle">
        {t.monthOf.replace("{month}", formatMonth(monthStart, locale))}
      </p>
    </div>
  );
}

/**
 * One day of the month.
 *
 * The date cell is the control, with an accessible name that says *which* day it opens. A
 * clickable row with no name is a control a keyboard user cannot describe, and the date is
 * the only useful label for it.
 */
function MonthRow({
  dict,
  locale,
  day,
  flags,
  selected,
  onSelect,
}: {
  dict: Dictionary;
  locale: Locale;
  day: AttendanceDay;
  flags: string[];
  selected: boolean;
  onSelect: () => void;
}) {
  const t = dict.attendance;
  return (
    <tr
      className={`border-b border-border last:border-0 ${selected ? "bg-primary-subtle" : ""}`}
      data-day-status={day.status}
    >
      <th scope="row" className="px-3 py-2 text-left font-normal">
        <button
          type="button"
          onClick={onSelect}
          aria-current={selected ? "true" : undefined}
          aria-label={t.month.open.replace("{date}", formatDate(day.business_date, locale))}
          className="tabular rounded px-2 py-1 text-left font-medium hover:bg-neutral-bg"
        >
          {formatDate(day.business_date, locale)}
        </button>
      </th>
      <td className="px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={dayStatusTone(day.status)} label={dayStatusLabel(dict, day.status)} />
          {flags.length > 0 && (
            <span className="text-sm text-fg-subtle" data-testid="attendance-day-flags">
              {anomalyList(dict, flags)}
            </span>
          )}
        </div>
      </td>
      <td className="tabular hidden px-3 py-2 text-right sm:table-cell">
        {formatDuration(day.worked_minutes, locale)}
      </td>
      <td className="tabular hidden px-3 py-2 text-right md:table-cell">
        {day.first_in === null ? t.month.dash : formatTime(day.first_in, locale)}
      </td>
      <td className="tabular hidden px-3 py-2 text-right md:table-cell">
        {day.last_out === null ? t.month.dash : formatTime(day.last_out, locale)}
      </td>
      <td className="tabular hidden px-3 py-2 text-right md:table-cell">
        {day.expected_minutes === null ? t.month.dash : formatDuration(day.expected_minutes, locale)}
      </td>
    </tr>
  );
}

/** The correction request itself: the page's one primary action. */
function CorrectionForm({
  dict,
  locale,
  selectedDay,
  todayDate,
  busy,
  error,
  onError,
  onSubmit,
}: {
  dict: Dictionary;
  locale: Locale;
  selectedDay: string;
  todayDate: string;
  busy: boolean;
  error: string | null;
  onError: (message: string | null) => void;
  onSubmit: (input: {
    business_date: string;
    kind: "clock_in" | "clock_out";
    time: string;
    reason: string;
  }) => Promise<boolean>;
}) {
  const t = dict.attendance.correction;
  const [businessDate, setBusinessDate] = useState(selectedDay);
  const [kind, setKind] = useState<"clock_in" | "clock_out">("clock_out");
  const [time, setTime] = useState("09:00");
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  // Picking another day in the table re-points the form at it: the form is about the day
  // the reader is looking at, and making them retype the date would be the screen asking
  // for a fact it already has.
  useEffect(() => setBusinessDate(selectedDay), [selectedDay]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    onError(null);
    if (businessDate.trim().length === 0) {
      setLocalError(t.dateRequired);
      return;
    }
    if (time.trim().length === 0) {
      setLocalError(t.timeRequired);
      return;
    }
    if (reason.trim().length === 0) {
      setLocalError(t.reasonRequired);
      return;
    }
    setLocalError(null);
    const filed = await onSubmit({ business_date: businessDate, kind, time, reason: reason.trim() });
    if (filed) {
      setReason("");
      setLocalError(null);
    }
  }

  return (
    <Card title={t.heading} description={t.description} headingId="attendance-correction-heading">
      <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            label={t.dateLabel}
            type="date"
            value={businessDate}
            onChange={setBusinessDate}
            // The bound the API enforces, stated on the control: a correction of a day that
            // has not happened is refused, and the browser can say so before the round trip.
            max={todayDate}
            hint={t.dateHint}
            required
          />
          <SelectField
            label={t.kindLabel}
            value={kind}
            onChange={(value) => setKind(value === "clock_in" ? "clock_in" : "clock_out")}
            options={[
              { value: "clock_out", label: t.kind.clock_out },
              { value: "clock_in", label: t.kind.clock_in },
            ]}
            required
          />
          <TextField
            label={t.timeLabel}
            type="time"
            value={time}
            onChange={setTime}
            hint={t.timeHint}
            required
          />
        </div>
        <TextAreaField
          label={t.reasonLabel}
          value={reason}
          onChange={setReason}
          placeholder={t.reasonPlaceholder}
          hint={t.reasonHint}
          error={localError ?? undefined}
          maxLength={1000}
          required
        />
        {error !== null && <Alert tone="danger" role="alert">{error}</Alert>}
        <p className="text-sm text-fg-subtle">{t.note}</p>
        <div>
          <Button type="submit" disabled={busy}>
            {busy ? t.submitting : t.submit}
          </Button>
        </div>
      </form>
    </Card>
  );
}

/** The month's own arithmetic: a sum over the days the API answered. */
function monthTotals(days: AttendanceDay[]): { worked: number; expected: number } {
  return days.reduce(
    (totals, day) => ({
      worked: totals.worked + day.worked_minutes,
      expected: totals.expected + (day.expected_minutes ?? 0),
    }),
    { worked: 0, expected: 0 },
  );
}

/** The tone each correction state carries. */
function correctionTone(state: CorrectionState): StatusTone {
  switch (state) {
    case "applied":
      return "success";
    case "rejected":
      return "danger";
    case "approved":
      return "warning";
    case "in_approval":
      return "info";
    case "draft":
    case "withdrawn":
      return "neutral";
  }
}
