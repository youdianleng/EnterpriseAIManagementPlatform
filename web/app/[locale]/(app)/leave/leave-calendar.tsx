"use client";

import type { LeaveCalendarRead, LeaveType } from "@/lib/api/leave";
import { calendarTypeName } from "@/lib/api/leave";
import { formatDate, formatWeekdayShort } from "@/lib/format";
import { addDays, parseDay } from "@/lib/format/day";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";

/**
 * The absence calendar (ticket 25): which days of the month are already approved leave.
 *
 * Two decisions worth naming:
 *
 * * **A mark in the grid, and the words underneath it.** Seven columns at 320px leave about
 *   forty pixels a cell, and "Nacimiento y cuidado de menor" does not fit in forty pixels in
 *   either language. So the grid carries an icon and a tint — and, for screen readers, the
 *   leave type as text — while the list under it names every absence in full. §5's rule is
 *   that a state is never carried by colour alone; it is satisfied by the page, and the grid
 *   is not asked to do something it cannot.
 * * **Every calendar day of an approved leave is marked, weekends included.** That is the
 *   API's answer (`LeaveCalendar` covers the range's calendar days), and it is the honest
 *   one: a leave from Friday to Monday is four days away, not two.
 *
 * A Monday-first grid, like the timesheet (`lib/format/day` numbers weekdays the same way).
 */
export function LeaveCalendar({
  dict,
  locale,
  calendar,
  types,
  monthStart,
  monthEnd,
}: {
  dict: Dictionary;
  locale: Locale;
  calendar: LeaveCalendarRead | null;
  types: LeaveType[];
  monthStart: string;
  monthEnd: string;
}) {
  const t = dict.leave.calendar;
  const byDate = new Map((calendar?.days ?? []).map((day) => [day.business_date, day]));
  const grid = gridDays(monthStart, monthEnd);

  // The month's absences grouped by type, which is the list that carries the words.
  const totals = new Map<string, number>();
  for (const day of calendar?.days ?? []) {
    totals.set(day.leave_type, (totals.get(day.leave_type) ?? 0) + 1);
  }
  const summary = [...totals.entries()]
    .map(([code, count]) => `${calendarTypeName(code, types, locale)}: ${count}`)
    .join(" · ");

  return (
    <div className="flex flex-col gap-4">
      <p className="text-fg-muted">{t.description}</p>

      <table className="w-full max-w-lg border-collapse text-sm" data-testid="leave-calendar">
        <caption className="sr-only">{t.caption}</caption>
        <thead>
          <tr>
            {/* A fixed reference Monday: the header names the weekday, not a date. */}
            {[0, 1, 2, 3, 4, 5, 6].map((offset) => (
              <th
                key={offset}
                scope="col"
                className="px-1 py-1 text-center text-xs font-medium text-fg-subtle"
              >
                {formatWeekdayShort(addDays("2024-01-01", offset), locale)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {chunk(grid, 7).map((week) => (
            <tr key={week[0]}>
              {week.map((day) => {
                const away = byDate.get(day);
                const inMonth = day >= monthStart && day <= monthEnd;
                return (
                  <td
                    key={day}
                    data-calendar-day={day}
                    data-on-leave={away ? "true" : undefined}
                    className={`border border-border px-1 py-1 text-center align-top ${
                      inMonth ? "" : "bg-neutral-bg text-fg-subtle"
                    } ${away ? "bg-info-bg" : ""}`}
                  >
                    <span className={`tabular ${away ? "font-semibold text-info" : ""}`}>
                      {parseDay(day).getDate()}
                    </span>
                    {away && (
                      <>
                        <LeaveGlyph />
                        <span className="sr-only">
                          {t.onLeave}: {calendarTypeName(away.leave_type, types, locale)}
                        </span>
                      </>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>

      <p className="text-sm text-fg-muted" data-testid="leave-calendar-summary">
        {summary === "" ? t.summaryEmpty : t.summary.replace("{parts}", summary)}
      </p>
    </div>
  );
}

/** `YYYY-MM-DD` for every cell of the month's grid, Monday first and padded to whole weeks. */
function gridDays(monthStart: string, monthEnd: string): string[] {
  const first = parseDay(monthStart);
  const offset = (first.getDay() + 6) % 7;
  const daysInMonth = parseDay(monthEnd).getDate();
  const cells = Math.ceil((offset + daysInMonth) / 7) * 7;
  const start = addDays(monthStart, -offset);
  return Array.from({ length: cells }, (_, index) => addDays(start, index));
}

function chunk<T>(values: T[], size: number): T[][] {
  const rows: T[][] = [];
  for (let index = 0; index < values.length; index += size) {
    rows.push(values.slice(index, index + size));
  }
  return rows;
}

/** A small mark for a day that is an approved absence: the icon half of icon + word. */
function LeaveGlyph() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      className="mx-auto mt-0.5 size-3 fill-none stroke-current text-info"
    >
      <path d="M3 6.5h10v6H3z" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M8 6.5V3.5M5.5 4.5l2.5-2 2.5 2" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
