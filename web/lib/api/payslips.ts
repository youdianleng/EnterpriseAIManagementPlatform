/**
 * The payslip client (ticket 44).
 *
 * Three things this module owns so no screen has to:
 *
 * 1. **The wire shapes**, including the two lists the upload answers with. `attributed`
 *    and `unmatched` are separate keys and `partitioned` is served beside them, so a screen
 *    cannot accidentally render one and believe it has the other — the API's own check
 *    travels with the answer.
 * 2. **The reason vocabulary**, as a closed union. `UnmatchedReason` on the server is an
 *    enum with no `unknown`, and a file that was not attributed *always* carries one of
 *    these; a screen that switched on a free-form string would render a blank line the day
 *    the server learned a seventh.
 * 3. **The multipart upload.** Files are `File`s from an `<input type="file">` and the rest
 *    are form fields, so the browser sets the boundary and the `Content-Type` itself —
 *    `request()` cannot be used for this one because it sets `Content-Type: application/json`
 *    whenever there is a body, and a hand-set multipart header produces a body the server
 *    cannot parse.
 */

import { API_BASE_URL, ApiError, request, type ApiErrorBody } from "@/lib/api/client";

/** Why a file was not attributed. Mirrors the server's `UnmatchedReason`, exactly. */
export type UnmatchedReason =
  /** No piece of the filename is a staff number, and none looks like one either. */
  | "no_employee_number"
  /** A number the filename carries is not on file — check it against the payroll record. */
  | "unknown_employee_number"
  /** The filename names two different employees, so the file belongs to one of two people. */
  | "ambiguous_employee_number"
  /** The bytes are not a PDF, whatever the name says. */
  | "not_a_pdf"
  /** Over the module's ceiling. Refused rather than truncated. */
  | "oversized_file"
  /** Zero bytes. */
  | "empty_file"
  /** Two files for one employee in one upload; the second would replace the first. */
  | "duplicate_for_employee"
  /** The same bytes twice in one upload. */
  | "duplicate_file";

export type UnmatchedFile = {
  filename: string;
  reason: UnmatchedReason;
  /** The number the file named, when it named one — the fact the reason cannot carry. */
  employee_no: string | null;
  /** The size of an oversized file, or the name it collided with. */
  detail: string | null;
};

export type AttributedPayslip = {
  id: string;
  period: string;
  employee_id: string;
  employee_name: string;
  /** A withheld field, present because this surface is finance's — it is what a filename is matched on. */
  employee_no: string | null;
  filename: string;
  /** A string: the byte count, so JSON's one number type cannot round it. */
  file_size: string;
  content_sha256: string;
  status: string;
  /** True when this upload overwrote the payslip that was already there. */
  replaced: boolean;
  /** What the row held before — the checksum that makes a replacement checkable. */
  previous_sha256: string | null;
  previous_file_size: string | null;
  created_at: string | null;
};

export type MissingEmployee = {
  employee_id: string;
  employee_no: string | null;
  employee_name: string;
  department_name: string | null;
  /** The window of the salary record that was in force — why this person is expected. */
  salary_effective_from: string;
  salary_effective_to: string | null;
};

export type BatchAnswer = {
  batch_id: string;
  period: string;
  total_count: number;
  attributed_count: number;
  replaced_count: number;
  unmatched_count: number;
  missing_count: number;
  attributed: AttributedPayslip[];
  unmatched: UnmatchedFile[];
  missing: MissingEmployee[];
  /** The server's own check that every uploaded file is in exactly one of the two lists. */
  partitioned: boolean;
  created_at: string | null;
  /**
   * False when this answer is the **dry run**: the files were matched and nothing was
   * written. The screen uses it to draw the two lists before the overwrite is agreed to.
   */
  confirmed: boolean;
  /**
   * How many of the attributed files would replace a payslip that is already there — the
   * count §6.3's confirmation names, read from the server's own rows rather than counted by
   * the client.
   */
  reserved_count: number;
};

export type MissingList = {
  period: string;
  /** How many people the derivation looked at — not the same number as `missing_count`. */
  expected: number;
  missing_count: number;
  items: MissingEmployee[];
};

export type BatchHistory = {
  id: string;
  period: string;
  total_count: number;
  success_count: number;
  missing_employee_ids: string[];
  unmatched: UnmatchedFile[];
  created_at: string | null;
};

export type BatchPage = {
  items: BatchHistory[];
  total: number;
  limit: number;
  offset: number;
};

/** One file and the employee the uploader picked for it, when they picked one. */
export type SelectedFile = {
  file: File;
  employeeId?: string;
};

/**
 * File a month's payslips.
 *
 * The selections travel as repeated `employee_id` parts **in the same order as the files**,
 * which is the pairing rule the endpoint states. They are sent only when at least one file
 * has one, so the ordinary case — every filename carries its own number — sends none.
 */
export async function uploadBatch(
  period: string,
  entries: SelectedFile[],
  confirm = true,
): Promise<BatchAnswer> {
  const body = new FormData();
  body.append("period", period);
  body.append("confirm", confirm ? "true" : "false");
  for (const entry of entries) body.append("files", entry.file);
  const selections = entries.map((entry) => entry.employeeId ?? "");
  if (selections.some((value) => value !== "")) {
    for (const value of selections) body.append("employee_id", value);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/v1/payslips/batches`, {
      method: "POST",
      body,
      // No `Content-Type`: the browser writes the multipart boundary itself.
      credentials: "include",
      headers: { Accept: "application/json" },
    });
  } catch (cause) {
    throw new ApiError(cause instanceof Error ? cause.message : "Network request failed");
  }

  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const envelope = (payload as { error?: ApiErrorBody } | null)?.error;
    if (envelope) throw new ApiError(envelope.message, response.status, envelope);
    throw new ApiError(`Request failed with status ${response.status}`, response.status);
  }
  return (await response.json()) as BatchAnswer;
}

export function readMissing(period: string): Promise<MissingList> {
  return request<MissingList>(
    `/api/v1/payslips/missing?period=${encodeURIComponent(period)}`,
  );
}

/**
 * The people who can be named for a file in this month.
 *
 * The screen's per-file selector is the checklist's 「界面选择」, and it has to name people
 * the way the *files* name them — by staff number — so it is this module's own small read
 * rather than the employee directory, which is projected by §4.1 and does not carry the
 * number at all.
 */
export function readEmployees(period: string): Promise<EmployeeOption[]> {
  return request<EmployeeOption[]>(
    `/api/v1/payslips/employees?period=${encodeURIComponent(period)}`,
  );
}

export type EmployeeOption = {
  employee_id: string;
  employee_no: string | null;
  employee_name: string;
};

export function readBatches(limit = 20): Promise<BatchPage> {
  return request<BatchPage>(`/api/v1/payslips/batches?limit=${limit}`);
}

/**
 * The missing list as the file finance works from.
 *
 * A URL rather than a fetch, for the reason `documentContentUrl` gives: the session travels
 * in an httpOnly cookie the browser attaches to a navigation, and the response is an
 * attachment — downloading it through script would hold the file in memory to hand the user
 * a blob the browser can already stream.
 */
export function missingExportUrl(period: string): string {
  return `${API_BASE_URL}/api/v1/payslips/missing/export?period=${encodeURIComponent(period)}`;
}

/** The month `YYYY-MM` the screen opens on: the previous calendar month, locally. */
export function previousMonth(today: Date = new Date()): string {
  // Built from the date's components, never from `toISOString()`: that converts to UTC, and
  // a browser in Madrid on the 1st of a month would then name the month before it.
  const year = today.getFullYear();
  const month = today.getMonth(); // 0-based, so `month` alone is already the previous one.
  const previousYear = month === 0 ? year - 1 : year;
  const previousMonthNumber = month === 0 ? 12 : month;
  return `${previousYear}-${`${previousMonthNumber}`.padStart(2, "0")}`;
}

/** `2026-03` as a `Date` local to the browser, for the month input's value. */
export function monthValue(period: string): string {
  return period;
}
