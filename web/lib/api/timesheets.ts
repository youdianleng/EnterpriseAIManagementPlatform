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

/**
 * What an entry *is*. `reversal` is one half of a supplementary correction: the exact
 * negation of a locked entry, pointing at it through `reverses_entry_id`. The sign of
 * `minutes` follows this, which is why a total is a plain sum over both kinds.
 */
export type EntryType = "normal" | "reversal";

export type TimesheetEntry = {
  id: string;
  /** Which sheet of the week the row belongs to; the locked original or a correction. */
  timesheet_id: string;
  entry_date: string;
  project_id: string;
  task_id: string;
  minutes: number;
  entry_type: EntryType;
  reverses_entry_id: string | null;
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
  /** The net of the day: every row, adjustments included. */
  total_minutes: number;
  /** What the net was reached from: `gross_minutes - reversal_minutes = total_minutes`. */
  gross_minutes: number;
  reversal_minutes: number;
  /** Null when no schedule reaches this person: not the same as expecting zero. */
  expected_minutes: number | null;
  expectation_source: string | null;
  is_holiday: boolean;
};

/** One task's week, after its adjustments: the per-task half of the net view. */
export type TaskNet = {
  project_id: string;
  task_id: string;
  gross_minutes: number;
  reversal_minutes: number;
  net_minutes: number;
};

export type OverBudgetDay = {
  entry_date: string;
  total_minutes: number;
  expected_minutes: number;
  over_minutes: number;
};

/**
 * One sheet of a week: the original, or a correction filed against it.
 *
 * `corrects_timesheet_id` is the "which week does this supplement correct" direction;
 * the week's own `supplements` list is the other one.
 */
export type TimesheetSheet = {
  timesheet_id: string;
  status: TimesheetStatus;
  submitted_at: string | null;
  approval_request_id: string | null;
  corrects_timesheet_id: string | null;
  week_start: string;
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
  /** Approved and therefore locked for ever: only a supplement changes it now. */
  is_locked: boolean;
  /** Closed by the global week lock: outside the eight-week window, no writes at all. */
  week_closed: boolean;
  /** How many weeks of supplementary filing this week still has; zero means closed. */
  supplement_weeks_left: number;
  supplement_window_weeks: number;
  can_supplement: boolean;
  is_supplementary: boolean;
  corrects_timesheet_id: string | null;
  /** Which sheet the next write would land in: a correction while one is open. */
  editable_timesheet_id: string | null;
  sheets: TimesheetSheet[];
  supplements: TimesheetSheet[];
  gross_total_minutes: number;
  reversal_total_minutes: number;
  tasks: TaskNet[];
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

/** One sheet's own approval history, for a week that has been corrected. */
export type SheetStatus = {
  timesheet_id: string;
  status: TimesheetStatus;
  is_supplementary: boolean;
  corrects_timesheet_id: string | null;
  submitted_at: string | null;
  approval_request_id: string | null;
  approval: ApprovalState | null;
};

export type TimesheetStatusRead = {
  week_start: string;
  status: TimesheetStatus;
  is_editable: boolean;
  is_locked: boolean;
  submitted_at: string | null;
  approval_request_id: string | null;
  approval: ApprovalState | null;
  sheets: SheetStatus[];
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

/**
 * One correction of one locked entry: the new amount, or `null` for "this should not
 * have been recorded" — the reversal on its own, with nothing replacing it.
 */
export type Correction = {
  entry_id: string;
  minutes?: number | null;
  note?: string | null;
  project_id?: string;
  task_id?: string;
};

/**
 * Correct a locked week: a *new* sheet beside the original, with a reversal and a
 * replacement per corrected entry. The original is never edited, and the response is
 * the week's grid with both sheets' rows in it and the day totals already net.
 */
export function openSupplement(
  week: string,
  corrections: Correction[],
): Promise<TimesheetWeek> {
  return request<TimesheetWeek>(`/api/v1/timesheets/supplements?${weekQuery(week)}`, {
    method: "POST",
    body: JSON.stringify({ corrections }),
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
