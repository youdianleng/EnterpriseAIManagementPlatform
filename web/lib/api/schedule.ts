/**
 * The schedule client: which pattern governs one of the caller's days, and what a
 * month was worth (ticket 22).
 *
 * The clock screen needs the first of these for one reason only: "no expected hours"
 * and "you have not clocked in" are different facts, and the second one is not a
 * problem. `expected_minutes` on the day read is the number; `source` is the answer to
 * the question people actually ask — *why* is Friday six hours — and it comes from the
 * fallback chain rather than from the numbers.
 *
 * `expected-hours` is the month's figure, with `is_snapshot` saying whether it is a
 * stored, frozen number or a live answer computed from today's rules. A screen that
 * showed the figure without that flag would be claiming a stability it may not have.
 */

import { request } from "@/lib/api/client";

/**
 * Where a day's schedule came from, in the fallback chain's order.
 *
 * `none` is the important one: nobody has configured anything, so nothing is expected.
 * It is *not* zero minutes — zero is a decision, and this is the absence of one.
 */
export type ScheduleSource = "override" | "department" | "default" | "none";

export type ScheduleDay = {
  weekday: number;
  expected_minutes: number;
  start_time: string | null;
  end_time: string | null;
  break_minutes: number;
};

export type WorkSchedule = {
  id: string;
  code: string;
  name_es: string;
  name_en: string;
  department_id: string | null;
  weekly_hours: string;
  is_default: boolean;
  is_active: boolean;
  days: ScheduleDay[];
};

export type Holiday = {
  id: string;
  date: string;
  name_es: string;
  name_en: string;
  scope: string;
  region_code: string | null;
  year: number;
};

/** One of the caller's days: the pattern, what it expects, and why it is that one. */
export type MySchedule = {
  business_date: string;
  weekday: number;
  expected_minutes: number;
  source: ScheduleSource;
  region_code: string | null;
  schedule: WorkSchedule | null;
  holiday: Holiday | null;
};

/** A month's figure, with the days and holidays it was computed from. */
export type ExpectedHours = {
  employee_id: string;
  year: number;
  month: number;
  expected_minutes: number;
  /** False means the number came from today's rules and nobody has frozen it. */
  is_snapshot: boolean;
  snapshot_id: string | null;
  revision: number | null;
  region_codes: string[];
  days: Array<Record<string, unknown>>;
  holidays: Array<Record<string, unknown>>;
};

/** Which pattern governs the caller's day, and why it is that one. */
export function readMySchedule(onDate?: string): Promise<MySchedule> {
  const query = onDate ? `?on_date=${encodeURIComponent(onDate)}` : "";
  return request<MySchedule>(`/api/v1/schedules/mine${query}`);
}

/** The month's expected hours, snapshotted or live. Omitted, the API answers for Madrid's month. */
export function readExpectedHours(year?: number, month?: number): Promise<ExpectedHours> {
  const query = new URLSearchParams();
  if (year !== undefined) query.set("year", `${year}`);
  if (month !== undefined) query.set("month", `${month}`);
  const text = query.toString();
  return request<ExpectedHours>(`/api/v1/schedules/expected-hours${text ? `?${text}` : ""}`);
}

/** A holiday's name in the reader's language, from the row the API returned. */
export function holidayName(holiday: Holiday, locale: string): string {
  return locale === "es" ? holiday.name_es : holiday.name_en;
}

/** A schedule's name in the reader's language, from the row the API returned. */
export function scheduleName(schedule: WorkSchedule, locale: string): string {
  return locale === "es" ? schedule.name_es : schedule.name_en;
}
