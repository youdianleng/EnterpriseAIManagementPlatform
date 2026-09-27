/**
 * The weekly timesheet client.
 *
 * Three things this module owns so no screen has to:
 *
 * 1. **The wire shapes.** The grid is seven days of entries plus totals plus what the
 *    schedule expected; a screen that re-declared that shape would drift from the API
 *    the first time a field moved.
 * 2. **The week arithmetic.** A week is identified by its Monday, and the grid navigates
 *    by whole weeks. `mondayOf`/`shiftWeek` are here rather than in the component
 *    because a wrong Monday is a 422 from the server, which is a poor way to learn that
 *    a date helper is off by one.
 * 3. **Dates as plain `YYYY-MM-DD` strings.** The API takes and returns calendar dates,
 *    never instants, and `new Date("2026-03-09")` is UTC midnight — which in a browser
 *    west of Greenwich is the *8th*. Every conversion here goes through the components
 *    of a local date so the day cannot move.
 */

import { request } from "@/lib/api/client";

export type TimesheetStatus = "draft" | "pending" | "approved" | "rejected";

export type StepDecision = "approved" | "rejected" | "returned" | "pending" | "skipped";

export type TimesheetEntry = {
  id: string;
  entry_date: string;
  project_id: string;
  task_id: string;
  minutes: number;
  /** The server's answer, resolved from the task's configuration. Never sent up. */
  is_billable: boolean;
  note: string | null;
  project_code: string | null;
  task_code: string | null;
  task_name_es: string | null;
  task_name_en: string | null;
};

export type TimesheetDay = {
  entry_date: string;
  /** 0 is Monday, matching the API and `Date.prototype.getDay() + 6) % 7`. */
  weekday: number;
  entries: TimesheetEntry[];
  total_minutes: number;
  /** Null when no schedule reaches this person: not the same as expecting zero. */
  expected_minutes: number | null;
  expectation_source: string | null;
  is_holiday: boolean;
};

export type OverBudgetDay = {
  entry_date: string;
  total_minutes: number;
  expected_minutes: number;
  over_minutes: number;
};

export type TimesheetWeek = {
  employee_id: string;
  week_start: string;
  week_end: string;
  status: TimesheetStatus;
  is_editable: boolean;
  has_timesheet: boolean;
  submitted_at: string | null;
  approval_request_id: string | null;
  entries_total_minutes: number;
  expected_total_minutes: number | null;
  over_budget: boolean;
  over_budget_days: OverBudgetDay[];
  days: TimesheetDay[];
};

export type ApprovalDecision = {
  level: number;
  round: number;
  approver_employee_id: string;
  decision: StepDecision;
  comment: string | null;
  decided_at: string;
};

export type ApprovalState = {
  request_id: string;
  status: string;
  round: number;
  submitted_at: string | null;
  decided_at: string | null;
  pending_level: number | null;
  decisions: ApprovalDecision[];
};

export type TimesheetStatusRead = {
  week_start: string;
  status: TimesheetStatus;
  is_editable: boolean;
  submitted_at: string | null;
  approval_request_id: string | null;
  approval: ApprovalState | null;
};

export type ProjectOption = {
  id: string;
  code: string;
  name_es: string;
  name_en: string;
  status: string;
};

export type TaskOption = {
  id: string;
  project_id: string;
  code: string;
  name_es: string;
  name_en: string;
  is_active: boolean;
};

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

/**
 * The Monday of the week a date falls in.
 *
 * `getDay()` numbers Sunday 0, and the product numbers Monday 0, so the conversion is
 * `(getDay() + 6) % 7` — the one place that offset lives.
 */
export function mondayOf(value: Date): Date {
  const daysSinceMonday = (value.getDay() + 6) % 7;
  return new Date(value.getFullYear(), value.getMonth(), value.getDate() - daysSinceMonday);
}

export function shiftWeek(weekStart: string, weeks: number): string {
  const start = parseDay(weekStart);
  return formatDay(new Date(start.getFullYear(), start.getMonth(), start.getDate() + weeks * 7));
}

export function currentWeek(now: Date = new Date()): string {
  return formatDay(mondayOf(now));
}

export function readWeek(week: string): Promise<TimesheetWeek> {
  return request<TimesheetWeek>(`/api/v1/timesheets/week?${weekQuery(week)}`);
}

export function readStatus(week: string): Promise<TimesheetStatusRead> {
  return request<TimesheetStatusRead>(`/api/v1/timesheets/week/status?${weekQuery(week)}`);
}

export function submitWeek(week: string): Promise<TimesheetWeek> {
  return request<TimesheetWeek>(`/api/v1/timesheets/submit?${weekQuery(week)}`, {
    method: "POST",
  });
}

export function copyPreviousWeek(week: string): Promise<TimesheetWeek> {
  return request<TimesheetWeek>(`/api/v1/timesheets/copy-previous?${weekQuery(week)}`, {
    method: "POST",
  });
}

export function addEntry(
  week: string,
  body: {
    entry_date: string;
    project_id: string;
    task_id: string;
    minutes: number;
    note?: string | null;
  },
): Promise<TimesheetWeek> {
  return request<TimesheetWeek>(`/api/v1/timesheets/entries?${weekQuery(week)}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateEntry(
  week: string,
  entryId: string,
  body: { minutes?: number; note?: string | null },
): Promise<TimesheetWeek> {
  return request<TimesheetWeek>(`/api/v1/timesheets/entries/${entryId}?${weekQuery(week)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function removeEntry(week: string, entryId: string): Promise<TimesheetWeek> {
  return request<TimesheetWeek>(`/api/v1/timesheets/entries/${entryId}?${weekQuery(week)}`, {
    method: "DELETE",
  });
}

/**
 * The projects this caller may book against, read from the endpoint that applies the
 * kernel's own answer rather than the catalogue's.
 *
 * `/projects/selectable` is the list form of the same decision the write path makes, so
 * the picker cannot offer a project the entry would then be refused for.
 */
export function selectableProjects(): Promise<{ items: ProjectOption[]; total: number }> {
  return request<{ items: ProjectOption[]; total: number }>(
    "/api/v1/projects/selectable?limit=200",
  );
}

export function projectTasks(projectId: string): Promise<{ tasks: TaskOption[] }> {
  return request<{ tasks: TaskOption[] }>(`/api/v1/projects/${projectId}`);
}

function weekQuery(week: string): string {
  return new URLSearchParams({ week }).toString();
}
