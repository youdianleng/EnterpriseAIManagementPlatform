import {
  currentWeek,
  type TimesheetStatusRead,
  type TimesheetWeek,
} from "@/lib/api/timesheets";
import { readServerWeek, readServerWeekStatus } from "@/lib/api/session-server";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { TimesheetScreen } from "./timesheet-screen";

/**
 * Weekly timesheet entry.
 *
 * The week is read on the server so the grid is on the first paint, the same shape the
 * notification centre uses: a grid that renders empty and then fills in makes "you have
 * no hours" and "we have not looked yet" the same screen for a moment, and the
 * difference matters to somebody who came here to file a week.
 *
 * The week comes from the query string and defaults to the current week. A wrong Monday
 * is a 422 from the API, so the default is computed here rather than in the browser.
 */
export default async function TimesheetsPage({
  params,
  searchParams,
}: {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ week?: string }>;
}) {
  const { locale: raw } = await params;
  const { week: requested } = await searchParams;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const week = requested ?? currentWeek();
  const [grid, status] = await Promise.all([readServerWeek(week), readServerWeekStatus(week)]);

  return (
    <TimesheetScreen
      dict={dict}
      locale={locale}
      week={week}
      initialGrid={grid}
      initialStatus={status}
    />
  );
}
