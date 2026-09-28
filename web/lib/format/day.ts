/**
 * Calendar days as `YYYY-MM-DD` strings.
 *
 * The attendance, schedule and leave endpoints take and return *dates* — never
 * instants — so every date the frontend handles is a plain calendar day. The trap
 * this module exists to close is that `new Date("2026-03-09")` is UTC midnight,
 * which in a browser west of Greenwich is the *8th*, and `toISOString()` on a local
 * date is the same mistake pointing the other way. Every conversion therefore goes
 * through the components of a local date, so the day cannot move.
 *
 * These helpers are deliberately locale-free: they produce the string the API
 * speaks. Turning it into something a person reads is `lib/format`'s job and always
 * takes the interface language (`formatDate`, `formatLongDate`, `formatMonth`).
 */

import { DISPLAY_TIME_ZONE } from "@/lib/format";

/** A local `Date` for a `YYYY-MM-DD` calendar date, with no timezone in the way. */
export function parseDay(value: string): Date {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
}

/** A `YYYY-MM-DD` string for a local date. The inverse of `parseDay`. */
export function formatDay(value: Date): string {
  const month = `${value.getMonth() + 1}`.padStart(2, "0");
  const day = `${value.getDate()}`.padStart(2, "0");
  return `${value.getFullYear()}-${month}-${day}`;
}

/** Today, as the *browser* sees it. The server's own answer is Madrid's day. */
export function todayIso(now: Date = new Date()): string {
  return formatDay(now);
}

/** A day shifted by whole days. Month and year rolls are the `Date` object's job. */
export function addDays(value: string, days: number): string {
  const date = parseDay(value);
  return formatDay(new Date(date.getFullYear(), date.getMonth(), date.getDate() + days));
}

/** The first day of the month `value` falls in. */
export function startOfMonth(value: string): string {
  const date = parseDay(value);
  return formatDay(new Date(date.getFullYear(), date.getMonth(), 1));
}

/**
 * The last day of the month `value` falls in.
 *
 * Day 0 of the *next* month is the last day of this one, which is how the length of
 * February is answered without a table of month lengths.
 */
export function endOfMonth(value: string): string {
  const date = parseDay(value);
  return formatDay(new Date(date.getFullYear(), date.getMonth() + 1, 0));
}

/** The same day of the month, `months` months away — clamped by `Date` if it is short. */
export function shiftMonth(value: string, months: number): string {
  const date = parseDay(value);
  return formatDay(new Date(date.getFullYear(), date.getMonth() + months, 1));
}

/** The year `value` falls in. */
export function yearOf(value: string): number {
  return parseDay(value).getFullYear();
}

/**
 * Every calendar day from `from` to `to`, inclusive.
 *
 * A month view needs the days nobody worked as much as the ones somebody did, and
 * building them here keeps "which day is this row" out of the component.
 */
export function daysBetween(from: string, to: string): string[] {
  const days: string[] = [];
  const last = parseDay(to).getTime();
  let cursor = parseDay(from).getTime();
  let guard = 0;
  while (cursor <= last && guard < 4000) {
    days.push(formatDay(new Date(cursor)));
    const next = new Date(cursor);
    next.setDate(next.getDate() + 1);
    cursor = next.getTime();
    guard += 1;
  }
  return days;
}

/**
 * A Madrid wall-clock reading, as an instant with an explicit offset.
 *
 * The correction form asks a person for a date and a time — "the clock-in should have
 * been 09:05 on the 12th" — and the attendance API requires an offset, because a
 * business day is Madrid's calendar day and a naive instant cannot be attributed to
 * one. The offset therefore has to be resolved for *that* wall-clock time, not for now:
 * Madrid is two hours apart in March and in July, and reusing today's offset for a date
 * across a transition would move a punch by an hour.
 *
 * The resolution is the two-pass standard trick: guess that the wall-clock reading is
 * UTC, ask what offset Madrid was on at that guess, subtract it, and ask again. It
 * converges for every real zone. The one reading it cannot be exact about is the hour a
 * DST transition skips or repeats, where the wall clock is genuinely ambiguous — Spain
 * switches at 02:00/03:00 on a Sunday, which is not a time anybody records a punch for,
 * and both readings name the same business day either way.
 */
export function madridInstant(day: string, time: string): string {
  const [year, month, date] = day.split("-").map(Number);
  const [hour, minute] = time.split(":").map(Number);
  const naive = Date.UTC(year, month - 1, date, hour, minute);
  let instant = naive;
  for (let pass = 0; pass < 2; pass += 1) {
    instant = naive - zoneOffsetMinutes(new Date(instant)) * 60_000;
  }
  return `${day}T${pad(hour)}:${pad(minute)}:00${zoneOffset(new Date(instant))}`;
}

/** `+02:00` for the product zone's offset in force at `instant`. */
function zoneOffset(instant: Date): string {
  const minutes = zoneOffsetMinutes(instant);
  const sign = minutes < 0 ? "-" : "+";
  const absolute = Math.abs(minutes);
  return `${sign}${pad(Math.trunc(absolute / 60))}:${pad(absolute % 60)}`;
}

/** The zone's UTC offset at an instant, read from the runtime's own time-zone database. */
function zoneOffsetMinutes(instant: Date): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: DISPLAY_TIME_ZONE,
    timeZoneName: "longOffset",
  }).formatToParts(instant);
  const name = parts.find((part) => part.type === "timeZoneName")?.value ?? "GMT+00:00";
  const match = /GMT([+-])(\d{2}):(\d{2})/.exec(name);
  if (!match) return 0;
  const sign = match[1] === "-" ? -1 : 1;
  return sign * (Number(match[2]) * 60 + Number(match[3]));
}

function pad(value: number): string {
  return `${value}`.padStart(2, "0");
}
