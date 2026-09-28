import {
  readServerCorrections,
  readServerDay,
  readServerExpectedHours,
  readServerPunches,
  readServerRange,
} from "@/lib/api/session-server";
import { endOfMonth, startOfMonth, todayIso, yearOf } from "@/lib/format/day";
import { getDictionary } from "@/lib/i18n";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";

import { AttendanceScreen } from "./attendance-screen";

/** `YYYY-MM`, the shape the month is addressed by in the URL. */
const MONTH = /^\d{4}-\d{2}$/;
/** `YYYY-MM-DD`, the shape a day is addressed by in the URL. */
const DAY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * The caller's own attendance record (ticket 24).
 *
 * The month and the selected day live in the query string, the way the timesheet grid
 * keeps its week there: a month and a day can be linked, reloaded and reached with Back,
 * and the server's first paint is what the reader asked for rather than a client round
 * trip before anything appears.
 *
 * **The default month is Madrid's month**, taken from the API's own answer to "what is
 * today" rather than from the browser or the container clock. A record that opened on the
 * wrong month for a few hours twice a year — and the employee's own record, at that — is
 * exactly the confusion the attendance module's date rule exists to prevent.
 *
 * Four reads, all of them things the screen cannot work out for itself: the month's days
 * (one request, gaps included), the selected day's punches with their chains and
 * anomalies, the correction documents with their states, and the month's expected hours
 * (a stored snapshot or a live figure — the response says which).
 */
export default async function AttendancePage({
  params,
  searchParams,
}: {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ month?: string; day?: string }>;
}) {
  const { locale: raw } = await params;
  const { month: requestedMonth, day: requestedDay } = await searchParams;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const today = await readServerDay();
  const todayDate = today?.business_date ?? todayIso();
  const monthStart = startOfMonth(
    requestedMonth && MONTH.test(requestedMonth) ? `${requestedMonth}-01` : todayDate,
  );
  const monthEnd = endOfMonth(monthStart);

  // The day has to belong to the month on screen: arriving on `?month=2026-03` from a link
  // that also carried August's day would otherwise show a day the table below cannot reach.
  const wantedDay = requestedDay && DAY.test(requestedDay) ? requestedDay : todayDate;
  const selectedDay =
    wantedDay >= monthStart && wantedDay <= monthEnd ? wantedDay : monthStart;

  const [range, detail, corrections, expected] = await Promise.all([
    readServerRange(monthStart, monthEnd),
    readServerPunches(selectedDay),
    readServerCorrections(),
    readServerExpectedHours(yearOf(monthStart), Number(monthStart.slice(5, 7))),
  ]);

  return (
    <AttendanceScreen
      dict={dict}
      locale={locale}
      monthStart={monthStart}
      monthEnd={monthEnd}
      todayDate={todayDate}
      selectedDay={selectedDay}
      initialRange={range}
      initialDetail={detail}
      initialCorrections={corrections?.items ?? null}
      initialCorrectionsTotal={corrections?.total ?? null}
      expectedMinutes={expected?.expected_minutes ?? null}
      expectedIsSnapshot={expected?.is_snapshot ?? null}
    />
  );
}
