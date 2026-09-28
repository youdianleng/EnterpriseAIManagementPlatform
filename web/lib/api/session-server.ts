/**
 * Session access for Server Components.
 *
 * The cookie is httpOnly, so only the server can read it. Server Components
 * forward it explicitly to the API, which is what lets the shell render the
 * signed-in identity on the first paint instead of after a client round trip.
 */

import { cookies } from "next/headers";

import { ApiError, API_SERVER_BASE_URL } from "@/lib/api/client";
import type {
  AttendanceDay,
  AttendanceRange,
  CorrectionPage,
  DayDetail,
} from "@/lib/api/attendance";
import type { AuthSession, OwnProfile, PasswordPolicy } from "@/lib/api/auth";
import type { DocumentPage } from "@/lib/api/documents";
import type {
  BalancePage,
  LeaveCalendarRead,
  LeaveRequestPage,
  LeaveType,
} from "@/lib/api/leave";
import type { ExpectedHours, MySchedule } from "@/lib/api/schedule";
import type { NotificationPage, UnreadCount } from "@/lib/api/notifications";
import type { TimesheetStatusRead, TimesheetWeek } from "@/lib/api/timesheets";

/** The cookie the API sets at login; httpOnly, so it never reaches browser script. */
export const SESSION_COOKIE = "eam_session";

export function loginPath(locale: string): string {
  return `/${locale}/login`;
}

export function changePasswordPath(locale: string): string {
  return `/${locale}/change-password`;
}

async function serverRequest<T>(path: string): Promise<T> {
  const store = await cookies();
  const cookie = store.get(SESSION_COOKIE);

  const response = await fetch(`${API_SERVER_BASE_URL}${path}`, {
    headers: {
      Accept: "application/json",
      ...(cookie ? { Cookie: `${SESSION_COOKIE}=${cookie.value}` } : {}),
    },
    // Identity is per-request and must never be served from a cache.
    cache: "no-store",
  });

  if (!response.ok) {
    // The body only matters for the log; the status is what callers branch on.
    throw new ApiError(`Request failed with status ${response.status}`, response.status);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/**
 * The signed-in account, or null for a visitor without a session.
 *
 * `GET /auth/session` answers in the forced-change state too — it is the call
 * that says *why* everything else is refused — so the gate can be decided from
 * this one read.
 */
export async function readServerSession(): Promise<AuthSession | null> {
  try {
    return await serverRequest<AuthSession>("/api/v1/auth/session");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

/** The caller's own profile, for the display name. Null when it cannot be read. */
export async function readServerProfile(): Promise<OwnProfile | null> {
  try {
    return await serverRequest<OwnProfile>("/api/v1/employees/me");
  } catch {
    // A name is decoration; the session is the contract. An account whose
    // profile cannot be read still gets a working shell, identified by username.
    return null;
  }
}

export async function readServerPasswordPolicy(): Promise<PasswordPolicy | null> {
  try {
    return await serverRequest<PasswordPolicy>("/api/v1/auth/password-policy");
  } catch {
    return null;
  }
}

/**
 * The caller's own notifications, for the centre's first paint.
 *
 * Null when they cannot be read, which the page renders as its error state with a
 * retry — the same shape `readServerProfile` uses, and for the same reason: a
 * failed read should not be an exception the shell has to catch.
 */
export async function readServerNotifications(): Promise<NotificationPage | null> {
  try {
    return await serverRequest<NotificationPage>("/api/v1/notifications");
  } catch {
    return null;
  }
}

/**
 * The unread badge number, or null when it cannot be read.
 *
 * Null means "no badge", not "zero": the header must not claim there is nothing to
 * read when the truth is that nobody asked.
 */
export async function readServerUnreadCount(): Promise<number | null> {
  try {
    const payload = await serverRequest<UnreadCount>("/api/v1/notifications/unread-count");
    return payload.unread;
  } catch {
    return null;
  }
}

/**
 * One week of the caller's own timesheet, for the grid's first paint.
 *
 * Null when it cannot be read, which the screen renders as its error state with a
 * retry — the same shape `readServerNotifications` uses, and for the same reason: a
 * failed read is a state the screen has, not an exception the shell must catch.
 */
export async function readServerWeek(week: string): Promise<TimesheetWeek | null> {
  try {
    return await serverRequest<TimesheetWeek>(
      `/api/v1/timesheets/week?week=${encodeURIComponent(week)}`,
    );
  } catch {
    return null;
  }
}

/** The week's approval state, with every round's decisions. */
export async function readServerWeekStatus(
  week: string,
): Promise<TimesheetStatusRead | null> {
  try {
    return await serverRequest<TimesheetStatusRead>(
      `/api/v1/timesheets/week/status?week=${encodeURIComponent(week)}`,
    );
  } catch {
    return null;
  }
}

/**
 * The documents this caller may read, for the list's first paint.
 *
 * Null when they cannot be read, which the screen renders as its error state with a
 * retry — the same shape the notification centre uses. `total` travels with the page,
 * so a heading that says "3 of 12" is the server's count and not the page's length.
 */
export async function readServerDocuments(limit = 50): Promise<DocumentPage | null> {
  try {
    return await serverRequest<DocumentPage>(`/api/v1/documents?limit=${limit}`);
  } catch {
    return null;
  }
}

/**
 * The caller's own day, for the clock's first paint.
 *
 * `businessDate` omitted asks the API for *today in Madrid*, which is the module's
 * answer rather than the browser's: a laptop in another zone must not decide which
 * business day a punch belongs to. Null when it cannot be read, which the screen renders
 * as its error state with a retry — the same shape every other reader here uses.
 */
export async function readServerDay(businessDate?: string): Promise<AttendanceDay | null> {
  try {
    const query = businessDate ? `?business_date=${encodeURIComponent(businessDate)}` : "";
    return await serverRequest<AttendanceDay>(`/api/v1/attendance/day${query}`);
  } catch {
    return null;
  }
}

/**
 * The day's punches and what was flagged about it.
 *
 * Read on the server as well as the day, because the clock's timer needs the punch the
 * open shift began with and the day read does not carry it — and a timer that appeared
 * a second after the state it belongs to would be two answers to one question.
 */
export async function readServerPunches(businessDate?: string): Promise<DayDetail | null> {
  try {
    const query = businessDate ? `?business_date=${encodeURIComponent(businessDate)}` : "";
    return await serverRequest<DayDetail>(`/api/v1/attendance/punches${query}`);
  } catch {
    return null;
  }
}

/**
 * Which pattern governs the caller's day, and why it is that one.
 *
 * The clock needs this for one distinction: "no working time is expected today" is not a
 * problem to be reported, and the day read alone cannot tell a holiday from a schedule
 * nobody configured.
 */
export async function readServerMySchedule(onDate?: string): Promise<MySchedule | null> {
  try {
    const query = onDate ? `?on_date=${encodeURIComponent(onDate)}` : "";
    return await serverRequest<MySchedule>(`/api/v1/schedules/mine${query}`);
  } catch {
    return null;
  }
}

/**
 * A month of the caller's days, gaps included, for the attendance record's first paint.
 *
 * The range endpoint answers every day in the window — a day nobody worked comes back
 * marked `absent` rather than missing — which is what lets the month table be complete
 * without a request per day.
 */
export async function readServerRange(
  fromDate: string,
  toDate: string,
): Promise<AttendanceRange | null> {
  try {
    const query = new URLSearchParams({ from_date: fromDate, to_date: toDate });
    return await serverRequest<AttendanceRange>(`/api/v1/attendance/range?${query.toString()}`);
  } catch {
    return null;
  }
}

/**
 * The caller's correction documents, newest first.
 *
 * Read on the server with the month so the list's states are on the first paint: a
 * document waiting for a decision is the reason somebody opens this screen, and a list
 * that arrives after a round trip makes "nothing is in flight" a claim the page has not
 * earned yet.
 */
export async function readServerCorrections(limit = 50): Promise<CorrectionPage | null> {
  try {
    return await serverRequest<CorrectionPage>(`/api/v1/attendance/corrections?limit=${limit}`);
  } catch {
    return null;
  }
}

/** The month's expected hours, snapshotted or live, for the month summary. */
export async function readServerExpectedHours(
  year: number,
  month: number,
): Promise<ExpectedHours | null> {
  try {
    return await serverRequest<ExpectedHours>(
      `/api/v1/schedules/expected-hours?year=${year}&month=${month}`,
    );
  } catch {
    return null;
  }
}

/**
 * The leave catalogue, the balances, the requests and the calendar of one month.
 *
 * Four reads because the leave screen is four facts, and each is a different question: the
 * types the company offers (which decides what the form may ask for), the allowance and how
 * it was reached, the documents with their states, and which days are already away. All four
 * are the caller's own — `leave.read_own` is self-only at the kernel, so no role check is
 * needed to offer the screen.
 */
export async function readServerLeaveTypes(): Promise<LeaveType[] | null> {
  try {
    return await serverRequest<LeaveType[]>("/api/v1/leave/types");
  } catch {
    return null;
  }
}

export async function readServerBalances(year: number): Promise<BalancePage | null> {
  try {
    return await serverRequest<BalancePage>(`/api/v1/leave/balances?year=${year}`);
  } catch {
    return null;
  }
}

export async function readServerLeaveRequests(limit = 20): Promise<LeaveRequestPage | null> {
  try {
    return await serverRequest<LeaveRequestPage>(`/api/v1/leave/requests?limit=${limit}`);
  } catch {
    return null;
  }
}

export async function readServerLeaveCalendar(
  fromDate: string,
  toDate: string,
): Promise<LeaveCalendarRead | null> {
  try {
    const query = new URLSearchParams({ from_date: fromDate, to_date: toDate });
    return await serverRequest<LeaveCalendarRead>(`/api/v1/leave/calendar?${query.toString()}`);
  } catch {
    return null;
  }
}
