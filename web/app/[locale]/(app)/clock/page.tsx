import {
  readServerDay,
  readServerMySchedule,
  readServerPunches,
} from "@/lib/api/session-server";
import { getDictionary } from "@/lib/i18n";
import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";

import { ClockScreen } from "./clock-screen";

/**
 * Today's clock (ticket 21).
 *
 * Read on the server, so the state and the timer are on the first paint rather than
 * arriving a round trip after it: somebody opening this page has one question — *has my
 * day started, and since when* — and a screen that shows "not clocked in" for a moment
 * before correcting itself answers it wrongly for that moment.
 *
 * **No business date is sent.** The API answers for *today in Madrid*, which is the
 * module's answer rather than the browser's; a laptop in another zone must not get to
 * decide which business day a punch belongs to (the same rule the timesheet grid
 * follows for a week's Monday).
 *
 * Three reads, because the day alone cannot answer the screen: the punch the open shift
 * began with comes from the record, and "no working time is expected today" is a
 * holiday or an unconfigured schedule — a fact about the pattern, not about the punches.
 */
export default async function ClockPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  const [day, detail, schedule] = await Promise.all([
    readServerDay(),
    readServerPunches(),
    readServerMySchedule(),
  ]);

  return (
    <ClockScreen
      dict={dict}
      locale={locale}
      initialDay={day}
      initialDetail={detail}
      initialSchedule={schedule}
    />
  );
}
