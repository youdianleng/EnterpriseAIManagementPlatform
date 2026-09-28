/**
 * The leave client: the catalogue, the caller's balances, their requests, the calendar.
 *
 * Four things this module owns so no screen has to:
 *
 * 1. **The wire shapes**, including the two the server computes and the browser must not:
 *    `business_days_count` (the working days a range is worth, weekends and holidays
 *    excluded by the schedule module) and `allocations` (the cross-year split, read back
 *    from the ledger). A screen that recomputed either would be a second answer to a
 *    question the API already answers.
 * 2. **The absence of a note.** There is deliberately no `reason`/`note` field on a leave
 *    request — `docs/DESIGN.md` §8 records the AEPD position that a sick leave is a special
 *    category of data, and a free-text field on a sick note is an invitation to write a
 *    diagnosis into the database. The request body is a `StrictModel`, so sending one is a
 *    422 rather than a silently dropped field. `LeaveRequestInput` therefore has no such
 *    field, and the form has no such control: the only text a request carries is the
 *    reference to a separately stored file, and only for a type that asks for one.
 * 3. **The attachment reference.** The reference *itself* is returned only to a caller
 *    holding `leave.attachment_read` (HR alone); everybody else sees `has_attachment` plus
 *    `attachment_readable_by`. `draftRequest` sends whatever reference it is given, and the
 *    list renders `has_attachment` — a screen that assumed it could read the key back would
 *    show HR's own view to everybody.
 * 4. **Dates as plain `YYYY-MM-DD` strings**, like every other date this frontend handles:
 *    the API takes and returns calendar dates, never instants.
 */

import { request } from "@/lib/api/client";

/** What a client shows. Derived by the API from the row and the engine, never stored. */
export type LeaveRequestState =
  | "draft"
  | "in_approval"
  | "approved"
  | "rejected"
  | "withdrawn";

/** Where a balance's days came from, or went. A closed set the database enforces too. */
export type LeaveEntryType =
  | "grant"
  | "carry_over"
  | "adjustment"
  | "reserve"
  | "release"
  | "consume"
  | "refund";

export type LeaveType = {
  id: string;
  code: string;
  name_es: string;
  name_en: string;
  is_paid: boolean;
  /** Some types refuse a request that has no file behind it. */
  requires_attachment: boolean;
  counts_against_annual: boolean;
  is_active: boolean;
};

/** One movement of a balance, with the four figures that followed it. */
export type BalanceEntry = {
  entry_type: LeaveEntryType;
  days: number;
  entitled_days: number;
  carried_over_days: number;
  used_days: number;
  pending_days: number;
  remaining_days: number;
  leave_request_id: string | null;
  note: string | null;
  created_at: string | null;
};

/** One year of one type: the allowance, and the history that produced the remainder. */
export type LeaveBalance = {
  /** Null for a year nobody has needed yet: the row is what the allowance *would* grant. */
  id: string | null;
  employee_id: string;
  year: number;
  leave_type: string;
  leave_type_name_es: string;
  leave_type_name_en: string;
  entitled_days: number;
  carried_over_days: number;
  used_days: number;
  pending_days: number;
  remaining_days: number;
  projected: boolean;
  history: BalanceEntry[];
};

export type BalancePage = {
  items: LeaveBalance[];
  total: number;
  /** The configured allowance (D7), so "30 of what" is answerable from one response. */
  annual_leave_days: number;
  year: number | null;
};

export type LeaveApprovalStep = {
  level: number;
  round: number;
  decision: string;
  approver_employee_id: string;
  comment: string | null;
  decided_at: string;
};

/** The engine's request, with every round's decisions: both stay readable. */
export type LeaveApproval = {
  request_id: string;
  status: string;
  round: number;
  submitted_at: string | null;
  decided_at: string | null;
  decisions: LeaveApprovalStep[];
};

/** Which year's balance a part of a request was charged to. */
export type LeaveAllocation = {
  year: number;
  days: number;
  balance_id: string;
};

/** The document, with the answers that are not on its own row. */
export type LeaveRequest = {
  id: string;
  employee_id: string;
  leave_type: string;
  start_date: string;
  end_date: string;
  /** The working days the API computed. Never recomputed in the browser. */
  business_days_count: number;
  state: LeaveRequestState;
  submitted_at: string | null;
  approved_at: string | null;
  withdrawn_at: string | null;
  settled_at: string | null;
  created_at: string | null;
  allocations: LeaveAllocation[];
  has_attachment: boolean;
  /** Which roles may read the file, stated by the API from the action catalogue. */
  attachment_readable_by: string[];
  /** Present only for a caller who may read the file itself. */
  attachment_reference: string | null;
};

export type LeaveRequestDetail = LeaveRequest & { approval: LeaveApproval | null };

export type LeaveRequestPage = {
  items: LeaveRequest[];
  total: number;
  limit: number;
  offset: number;
};

/** One date covered by approved leave. Weekends included, so a Friday-to-Monday leave
 *  does not render as two separate leaves. */
export type LeaveCalendarDay = {
  employee_id: string;
  business_date: string;
  leave_type: string;
  request_id: string;
};

export type LeaveCalendarRead = {
  employee_id: string;
  from_date: string;
  to_date: string;
  days: LeaveCalendarDay[];
};

/** What may be filed. Type, two dates, and the file's reference when the type needs one. */
export type LeaveRequestInput = {
  leave_type: string;
  start_date: string;
  end_date: string;
  attachment_reference?: string | null;
};

/** The catalogue. A retired type is readable on request, and not offered by default. */
export function readLeaveTypes(): Promise<LeaveType[]> {
  return request<LeaveType[]>("/api/v1/leave/types");
}

/** The allowance, and how it was reached. `year` omitted means every year on record. */
export function readBalances(year?: number): Promise<BalancePage> {
  const query = year === undefined ? "" : `?year=${year}`;
  return request<BalancePage>(`/api/v1/leave/balances${query}`);
}

/** A page of the caller's requests, newest first, each with its state. */
export function readRequests(limit = 20): Promise<LeaveRequestPage> {
  return request<LeaveRequestPage>(`/api/v1/leave/requests?limit=${limit}`);
}

/** One request and every round of its approval. */
export function readRequest(id: string): Promise<LeaveRequestDetail> {
  return request<LeaveRequestDetail>(`/api/v1/leave/requests/${id}`);
}

/**
 * Write the request, having refused what could never be filed.
 *
 * The draft computes and stores the working days the range is worth and refuses a range
 * with none, an overlapping request, a retired type, a missing attachment, a reference
 * that is not a storage key, and a balance that does not cover it. Nothing is reserved
 * until it is filed — which is what makes the draft a safe place to *see* the computed
 * count before committing to it.
 */
export function draftRequest(input: LeaveRequestInput): Promise<LeaveRequestDetail> {
  return request<LeaveRequestDetail>("/api/v1/leave/requests", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** File it: the days are reserved, then the engine is handed the document. */
export function submitRequest(id: string): Promise<LeaveRequestDetail> {
  return request<LeaveRequestDetail>(`/api/v1/leave/requests/${id}/submit`, { method: "POST" });
}

/**
 * Stop the leave and give the days back — while it is still in the future.
 *
 * Withdrawing is the requester's own act, and a leave that has begun cannot be withdrawn:
 * the API refuses it with a sentence naming HR's correction flow, which the screen shows.
 */
export function withdrawRequest(id: string): Promise<LeaveRequestDetail> {
  return request<LeaveRequestDetail>(`/api/v1/leave/requests/${id}/withdraw`, { method: "POST" });
}

/** The days somebody is away on approved leave, for an inclusive range. */
export function readCalendar(fromDate: string, toDate: string): Promise<LeaveCalendarRead> {
  const query = new URLSearchParams({ from_date: fromDate, to_date: toDate });
  return request<LeaveCalendarRead>(`/api/v1/leave/calendar?${query.toString()}`);
}

/** A leave type's name in the reader's language, from the row the API returned. */
export function leaveTypeName(type: LeaveType, locale: string): string {
  return locale === "es" ? type.name_es : type.name_en;
}

/**
 * A balance's type name in the reader's language.
 *
 * The balance response carries both names rather than an id, so a balance can be rendered
 * without a second request for the catalogue — and a type that was retired after the
 * balance was opened still has its name.
 */
export function balanceTypeName(balance: LeaveBalance, locale: string): string {
  return locale === "es" ? balance.leave_type_name_es : balance.leave_type_name_en;
}

/** A calendar day's leave type name, from the catalogue the screen already holds. */
export function calendarTypeName(
  code: string,
  types: LeaveType[],
  locale: string,
): string {
  const found = types.find((type) => type.code === code);
  return found ? leaveTypeName(found, locale) : code;
}
