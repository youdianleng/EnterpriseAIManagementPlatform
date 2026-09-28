/**
 * The attendance client: the punch stream, the day derived from it, and the
 * correction flow (tickets 21 and 24).
 *
 * Three things this module owns so no screen has to:
 *
 * 1. **The wire shapes.** `day`, `punches`, the correction document and the chain of
 *    corrections that restated one punch. A screen that re-declared them would drift
 *    from the API the first time a field moved.
 * 2. **The two derived facts the clock screen shows.** Which punch is still open (so
 *    the timer has something to count from) and what the day's punches *effectively*
 *    read once their corrections are applied. Both are read off `punches[]` and
 *    `effective_at` — the values the API says the day's own arithmetic uses — rather
 *    than recomputed here, so the screen and the record cannot disagree.
 * 3. **Instants in the product's zone.** The API takes a correction's instant *with an
 *    offset* and refuses a naive one: a naive instant cannot be attributed to a
 *    business day, and guessing is how an application records the wrong day twice a
 *    year. `madridInstant` resolves the offset for the Madrid wall-clock time the
 *    person typed — see the note on it.
 */

import { API_BASE_URL, request } from "@/lib/api/client";

/** The stream's vocabulary. `correction` is a restatement, never a button. */
export type EventType = "clock_in" | "clock_out" | "correction";

/** `web` for a punch somebody made; `correction` for one an approval appended. */
export type EventSource = "web" | "correction";

/**
 * The closed set of day states (`app/domain/attendance/models.py`).
 *
 * `working` is an open shift; `ok` is a day that was worked and closed; `missing_out`
 * is an open shift that ran past the maximum and was flagged; `incomplete` is a punch
 * that cannot be paired; `absent` is a working day with nothing; `holiday` and
 * `non_working` are days nobody was expected.
 */
export type DayStatus =
  | "working"
  | "ok"
  | "missing_out"
  | "incomplete"
  | "absent"
  | "holiday"
  | "non_working";

/**
 * The six states of a correction document, derived by the API from the row and the
 * engine. `approved` on its own means "the engine said yes and the punch has not moved
 * yet" — a real gap that clears on the next attempt to apply it.
 */
export type CorrectionState =
  | "draft"
  | "in_approval"
  | "approved"
  | "applied"
  | "rejected"
  | "withdrawn";

/** The row a punch was appended as, or the one an identical request already wrote. */
export type AttendanceEvent = {
  id: string;
  event_type: EventType;
  occurred_at: string;
  business_date: string;
  source: EventSource;
};

/** One day as derived. `recomputed_at` is null for a day derived to answer a read. */
export type AttendanceDay = {
  business_date: string;
  status: DayStatus;
  first_in: string | null;
  last_out: string | null;
  /** Minutes in *closed* intervals: the open shift is not counted until it closes. */
  worked_minutes: number;
  /** Null when no schedule reaches this person — not the same fact as zero. */
  expected_minutes: number | null;
  overtime_minutes: number | null;
  recomputed_at: string | null;
};

/** Every day in a range, gaps included and marked `absent`. */
export type AttendanceRange = {
  from_date: string;
  to_date: string;
  days: AttendanceDay[];
};

/** One row of the stream, as the chain read shows it. */
export type PunchEvent = {
  id: string;
  event_type: EventType;
  occurred_at: string;
  business_date: string;
  source: EventSource;
  reason: string | null;
  correction_of_event_id: string | null;
  created_by_employee_id: string | null;
  created_at: string;
};

/**
 * One punch, the corrections that restated it, and what the day reads.
 *
 * `corrections` is in write order — the original first — and `effective_at` is the
 * value the day's own arithmetic uses, so a chain reads as the evolution of one punch
 * rather than as a set of competing values.
 */
export type PunchChain = {
  punch: PunchEvent;
  corrections: PunchEvent[];
  effective_at: string;
  is_corrected: boolean;
  /** True when an approval *made up* a shift nobody clocked, rather than restating one. */
  is_made_up: boolean;
};

/** Something the nightly pass found wrong with a day. Resolved ones are included. */
export type AttendanceAnomaly = {
  type: string;
  detected_at: string;
  notified_at: string | null;
  resolved_by_event_id: string | null;
};

/** A day's punches, the day they derive, and what was flagged about it. */
export type DayDetail = {
  employee_id: string;
  business_date: string;
  day: AttendanceDay;
  punches: PunchChain[];
  anomalies: AttendanceAnomaly[];
};

export type ApprovalDecision = {
  level: number;
  round: number;
  decision: string;
  approver_employee_id: string;
  comment: string | null;
  decided_at: string;
};

/** The engine's request, with every round's decisions. Both rounds stay readable. */
export type AttendanceApproval = {
  request_id: string;
  status: string;
  round: number;
  submitted_at: string | null;
  decided_at: string | null;
  decisions: ApprovalDecision[];
};

/** The correction document. `state` is the one field a screen branches on. */
export type Correction = {
  id: string;
  employee_id: string;
  business_date: string;
  kind: EventType;
  corrected_at: string;
  reason: string;
  state: CorrectionState;
  requested_by_employee_id: string;
  applied_event_id: string | null;
  applied_at: string | null;
  submitted_at: string | null;
  created_at: string | null;
};

export type CorrectionDetail = Correction & { approval: AttendanceApproval | null };

export type CorrectionPage = {
  items: Correction[];
  total: number;
  limit: number;
  offset: number;
};

/** One punch to file a correction for: the day, which punch, what it should say, why. */
export type CorrectionInput = {
  business_date: string;
  kind: "clock_in" | "clock_out";
  /** ISO 8601 **with an offset**; a naive instant is refused. See `madridInstant`. */
  corrected_at: string;
  reason: string;
};

/**
 * Punch your own clock.
 *
 * `at` is deliberately not sent by the screens: absent means now, on the server's
 * clock, in the server's zone. A browser that supplied its own `new Date()` would be
 * writing the punch from a clock nobody verified.
 */
export function punch(kind: "clock_in" | "clock_out"): Promise<AttendanceEvent> {
  return request<AttendanceEvent>("/api/v1/attendance/clock", {
    method: "POST",
    body: JSON.stringify({ kind }),
  });
}

/** One of the caller's days. Omitted, the API answers for *today in Madrid*. */
export function readDay(businessDate?: string): Promise<AttendanceDay> {
  return request<AttendanceDay>(`/api/v1/attendance/day${dayQuery({ business_date: businessDate })}`);
}

/** A range of the caller's days, complete: a day nobody worked is an `absent` row. */
export function readRange(fromDate: string, toDate: string): Promise<AttendanceRange> {
  return request<AttendanceRange>(
    `/api/v1/attendance/range${dayQuery({ from_date: fromDate, to_date: toDate })}`,
  );
}

/** One day's punches, their chains and the day derived. Any historical day is answered. */
export function readPunches(businessDate?: string): Promise<DayDetail> {
  return request<DayDetail>(
    `/api/v1/attendance/punches${dayQuery({ business_date: businessDate })}`,
  );
}

export function readCorrections(options: {
  state?: CorrectionState;
  limit?: number;
} = {}): Promise<CorrectionPage> {
  const query = new URLSearchParams();
  if (options.state) query.set("state", options.state);
  query.set("limit", `${options.limit ?? 50}`);
  return request<CorrectionPage>(`/api/v1/attendance/corrections?${query.toString()}`);
}

export function readCorrection(id: string): Promise<CorrectionDetail> {
  return request<CorrectionDetail>(`/api/v1/attendance/corrections/${id}`);
}

export function draftCorrection(input: CorrectionInput): Promise<CorrectionDetail> {
  return request<CorrectionDetail>("/api/v1/attendance/corrections", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Change a document that has not been filed. A returned one is back with its requester. */
export function updateCorrection(
  id: string,
  body: { corrected_at?: string; reason?: string },
): Promise<CorrectionDetail> {
  return request<CorrectionDetail>(`/api/v1/attendance/corrections/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

/** Hand it to the engine, which resolves the route: the manager, then HR. */
export function submitCorrection(id: string): Promise<CorrectionDetail> {
  return request<CorrectionDetail>(`/api/v1/attendance/corrections/${id}/submit`, {
    method: "POST",
  });
}

/**
 * A link to the record as a CSV, for an accountant or a labour inspector.
 *
 * A link rather than a fetch: the session travels in an httpOnly cookie the browser
 * attaches to a navigation, and the response is an attachment — downloading it through
 * script would mean holding the whole file in memory to hand the user a blob the
 * browser can already stream.
 */
export function exportRecordUrl(fromDate: string, toDate: string): string {
  const query = new URLSearchParams({ from_date: fromDate, to_date: toDate });
  return `${API_BASE_URL}/api/v1/attendance/export?${query.toString()}`;
}

/**
 * The day's punches as the day counts them: one entry per logical punch, in the order
 * the derivation walked them, carrying the instant the day reads.
 *
 * This is what makes the clock legible rather than merely present. A corrected punch
 * has an original row *and* the corrections that restated it; the screen must show the
 * evolution and, for the timer, use the value in force. Both come from here, so the
 * two cannot drift.
 */
export type EffectivePunch = {
  id: string;
  kind: "clock_in" | "clock_out";
  /** The instant the day's arithmetic uses — the newest correction, or the original. */
  at: string;
  source: EventSource;
  is_corrected: boolean;
  is_made_up: boolean;
  chain: PunchChain;
};

export function effectivePunches(detail: DayDetail): EffectivePunch[] {
  return detail.punches
    .map((chain) => ({
      id: chain.punch.id,
      kind: chain.punch.event_type === "clock_out" ? ("clock_out" as const) : ("clock_in" as const),
      at: chain.effective_at,
      source: chain.punch.source,
      is_corrected: chain.is_corrected,
      is_made_up: chain.is_made_up,
      chain,
    }))
    .sort((left, right) => Date.parse(left.at) - Date.parse(right.at));
}

/**
 * When the shift that is still open began, or null when none is.
 *
 * The last punch of the day decides: if it is a `clock_in` nobody closed, that is the
 * open shift, and a second open shift cannot exist — the API refuses a second
 * `clock_in` while one is open. Null for a day that is closed, and null for a day whose
 * last row is a made-up `clock_in` from an approval, because that shift was never
 * really started and counting from it would invent working time.
 */
export function openShiftStart(detail: DayDetail): string | null {
  const punches = effectivePunches(detail);
  const last = punches[punches.length - 1];
  if (!last || last.kind !== "clock_in") return null;
  return last.at;
}

/**
 * Elapsed seconds since `startedAt`, measured against `now` (milliseconds).
 *
 * Clamped at zero: a server clock a second or two ahead of the browser would otherwise
 * render "−1 s", which reads as a broken timer rather than as clock skew.
 */
export function elapsedSeconds(startedAt: string, now: number): number {
  return Math.max(0, Math.floor((now - Date.parse(startedAt)) / 1000));
}

/**
 * The Madrid wall-clock time a person typed, as an instant with an offset.
 *
 * Re-exported from `lib/format/day`, where the offset resolution lives beside the rest
 * of the calendar-day arithmetic: the correction form is the only caller, and a second
 * implementation of "what offset was Madrid on that day" is exactly how a punch ends up
 * an hour out once a year.
 */
export { madridInstant } from "@/lib/format/day";

function dayQuery(values: Record<string, string | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value) query.set(key, value);
  }
  const text = query.toString();
  return text ? `?${text}` : "";
}
