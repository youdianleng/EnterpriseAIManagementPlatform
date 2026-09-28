import {
  readServerBalances,
  readServerDay,
  readServerLeaveCalendar,
  readServerLeaveRequests,
  readServerLeaveTypes,
} from "@/lib/api/session-server";
import { endOfMonth, startOfMonth, todayIso, yearOf } from "@/lib/format/day";
import { getDictionary } from "@/lib/i18n";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";

import { LeaveScreen } from "./leave-screen";

/** `YYYY-MM`, the shape the calendar month is addressed by in the URL. */
const MONTH = /^\d{4}-\d{2}$/;

/**
 * Leave: the balances, the request form and the calendar (ticket 25).
 *
 * **The year comes from the API's own answer to "what is today"** rather than from the
 * browser or the container clock, for the same reason the attendance month does: the
 * business day is Madrid's, and a balance opened on the wrong side of New Year's Eve is a
 * balance the reader cannot act on.
 *
 * Five reads, and each is a question the screen cannot answer for itself: the catalogue
 * (which decides whether the form asks for a supporting document), the year's balances with
 * the ledger behind them, the caller's requests, and the month's approved absences. The
 * calendar month is a query parameter, so a month can be linked and reached with Back.
 */
export default async function LeavePage({
  params,
  searchParams,
}: {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ month?: string }>;
}) {
  const { locale: raw } = await params;
  const { month: requestedMonth } = await searchParams;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const today = await readServerDay();
  const todayDate = today?.business_date ?? todayIso();
  const year = yearOf(todayDate);
  const monthStart =
    requestedMonth && MONTH.test(requestedMonth) ? `${requestedMonth}-01` : startOfMonth(todayDate);
  const monthEnd = endOfMonth(monthStart);

  const [types, balances, requests, calendar] = await Promise.all([
    readServerLeaveTypes(),
    readServerBalances(year),
    readServerLeaveRequests(),
    readServerLeaveCalendar(monthStart, monthEnd),
  ]);

  return (
    <LeaveScreen
      dict={dict}
      locale={locale}
      year={year}
      monthStart={monthStart}
      monthEnd={monthEnd}
      todayDate={todayDate}
      initialTypes={types}
      initialBalances={balances}
      initialRequests={requests?.items ?? null}
      initialRequestsTotal={requests?.total ?? null}
      initialCalendar={calendar}
    />
  );
}
