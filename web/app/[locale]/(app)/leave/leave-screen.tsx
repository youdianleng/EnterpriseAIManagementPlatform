"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { catalogueErrorText } from "@/lib/api/error-text";
import {
  balanceTypeName,
  calendarTypeName,
  draftRequest,
  leaveTypeName,
  readBalances,
  readCalendar,
  readRequest,
  readRequests,
  submitRequest,
  withdrawRequest,
  type BalancePage,
  type LeaveApproval,
  type LeaveBalance,
  type LeaveCalendarRead,
  type LeaveRequest,
  type LeaveRequestDetail,
  type LeaveRequestState,
  type LeaveType,
} from "@/lib/api/leave";
import { formatDate, formatMonth, formatNumber } from "@/lib/format";
import { endOfMonth, shiftMonth } from "@/lib/format/day";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { Card } from "@/lib/ui/card";
import { SelectField, TextField } from "@/lib/ui/field";
import { StatusBadge, type StatusTone } from "@/lib/ui/status-badge";

import { LeaveCalendar } from "./leave-calendar";

/**
 * Leave: the balances, the request form and the calendar (ticket 25).
 *
 * One protagonist: filing a request. The balances are the argument for it and the list is
 * its history, so both are quieter than the form's single primary button (§4.2).
 *
 * Six decisions worth naming:
 *
 * * **The working-day count is the API's, and the screen shows it before committing.** The
 *   form drafts first (nothing is reserved), reads `business_days_count` and `allocations`
 *   off the answer, and only then offers to file it. Recomputing "weekends and holidays
 *   excluded" in the browser would be a second implementation of the schedule module — and
 *   the whole point of that module is that it is the company's calendar rather than the
 *   reader's guess.
 * * **There is no note field, and the screen says why.** A leave request carries a type, two
 *   dates and — for a type that asks for one — a reference to a separately stored file. The
 *   API refuses a free-text field on purpose (a sick leave is a special category of data),
 *   so a textarea here would be a control whose write is rejected. The reason is on screen
 *   rather than left to be discovered.
 * * **The balance refusal is a state, not a surprise.** A draft whose range the year cannot
 *   afford is refused by the API before two people are asked to decide it, and the sentence
 *   points at the figures in the balances panel above.
 * * **Withdrawing is offered only where the API allows it.** A leave that has already begun
 *   cannot be withdrawn — the API names HR's correction flow instead — and a draft has no
 *   withdrawal at all, so a draft gets "send it" rather than a button that would be refused.
 * * **The decisions are read per document, after the first paint.** The list endpoint carries
 *   the state and not the engine's decisions, and there is no bulk read for them; a page of a
 *   person's own requests is small, so the details are fetched in parallel after paint and
 *   capped, and a document that cannot be read still renders with its state.
 * * **The calendar is a query parameter**, so a month can be linked and reached with Back.
 */
export function LeaveScreen({
  dict,
  locale,
  year,
  monthStart,
  monthEnd,
  todayDate,
  initialTypes,
  initialBalances,
  initialRequests,
  initialRequestsTotal,
  initialCalendar,
}: {
  dict: Dictionary;
  locale: Locale;
  year: number;
  monthStart: string;
  monthEnd: string;
  todayDate: string;
  initialTypes: LeaveType[] | null;
  initialBalances: BalancePage | null;
  initialRequests: LeaveRequest[] | null;
  initialRequestsTotal: number | null;
  initialCalendar: LeaveCalendarRead | null;
}) {
  const t = dict.leave;
  const router = useRouter();

  const [types, setTypes] = useState(initialTypes);
  const [balances, setBalances] = useState(initialBalances);
  const [requests, setRequests] = useState(initialRequests);
  const [requestsTotal, setRequestsTotal] = useState(initialRequestsTotal);
  const [calendar, setCalendar] = useState(initialCalendar);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setTypes(initialTypes), [initialTypes]);
  useEffect(() => setBalances(initialBalances), [initialBalances]);
  useEffect(() => setRequests(initialRequests), [initialRequests]);
  useEffect(() => setRequestsTotal(initialRequestsTotal), [initialRequestsTotal]);
  useEffect(() => setCalendar(initialCalendar), [initialCalendar]);

  /**
   * The engine's decisions for the requests that have any, keyed by request id.
   *
   * Fetched after the first paint because the list endpoint does not carry them and there is
   * no bulk read: one request per document, capped at the page size the list asks for, and a
   * document whose detail cannot be read still renders with the state the list gave.
   */
  const [approvals, setApprovals] = useState<Record<string, LeaveApproval | null>>({});
  const filedKey = useMemo(
    () =>
      (requests ?? [])
        .filter((request) => request.state !== "draft" && request.state !== "withdrawn")
        .slice(0, 20)
        .map((request) => request.id)
        .join(","),
    [requests],
  );

  useEffect(() => {
    const wanted = filedKey === "" ? [] : filedKey.split(",");
    if (wanted.length === 0) {
      setApprovals({});
      return;
    }
    let cancelled = false;
    void (async () => {
      const found = await Promise.all(
        wanted.map(async (id) => {
          try {
            const detail = await readRequest(id);
            return [id, detail.approval] as const;
          } catch {
            return [id, null] as const;
          }
        }),
      );
      if (!cancelled) setApprovals(Object.fromEntries(found));
    })();
    return () => {
      cancelled = true;
    };
  }, [filedKey]);

  const refresh = useCallback(async () => {
    try {
      const [nextBalances, nextRequests] = await Promise.all([
        readBalances(year),
        readRequests(),
      ]);
      setBalances(nextBalances);
      setRequests(nextRequests.items);
      setRequestsTotal(nextRequests.total);
    } catch {
      // The server render the caller triggers next replaces both; what is on screen is still
      // the last answer the API gave.
    }
  }, [year]);

  /** The list's own actions: file a draft, or withdraw what can still be withdrawn. */
  async function act(id: string, action: "submit" | "withdraw") {
    setBusyId(id);
    setError(null);
    setNotice(null);
    try {
      if (action === "submit") {
        await submitRequest(id);
        setNotice(t.request.filed);
      } else {
        await withdrawRequest(id);
        setNotice(t.list.withdrawn);
      }
      await refresh();
      router.refresh();
    } catch (cause) {
      setError(catalogueErrorText(cause, dict, t.error));
    } finally {
      setBusyId(null);
    }
  }

  /** Move the calendar to another month. The URL is the state. */
  function goToMonth(target: string) {
    const first = `${target.slice(0, 7)}-01`;
    setCalendar(null);
    router.push(`/${locale}/leave?month=${target.slice(0, 7)}`, { scroll: false });
    void (async () => {
      try {
        setCalendar(await readCalendar(first, endOfMonth(first)));
      } catch {
        // The server render replaces it; a failed read leaves the loading line in place.
      }
    })();
  }

  const failed = types === null || balances === null || requests === null;

  if (failed) {
    return (
      <div className="flex flex-col gap-8">
        <Header dict={dict} locale={locale} year={year} allowance={null} />
        <Alert tone="danger" role="alert" title={t.error} className="max-w-2xl">
          <p className="mt-1">{t.errorHint}</p>
          <Button variant="secondary" size="sm" className="mt-3" onClick={() => router.refresh()}>
            {t.retry}
          </Button>
        </Alert>
      </div>
    );
  }

  const activeTypes = types.filter((type) => type.is_active);

  return (
    <div className="flex flex-col gap-8">
      <Header
        dict={dict}
        locale={locale}
        year={year}
        allowance={balances.annual_leave_days}
      />

      {notice && (
        <Alert tone="success" role="status">
          {notice}
        </Alert>
      )}
      {error && (
        // Its own heading: the balances and the list loaded — it is the *request* that did not
        // go through, and saying the page failed to load would be a false statement about it.
        <Alert tone="danger" role="alert" title={t.request.refused}>
          <p className="mt-1">{error}</p>
        </Alert>
      )}

      <Balances dict={dict} locale={locale} balances={balances.items} types={types} />

      <RequestForm
        dict={dict}
        locale={locale}
        types={activeTypes}
        todayDate={todayDate}
        onFiled={async () => {
          setNotice(t.request.filed);
          await refresh();
          router.refresh();
        }}
        onError={setError}
      />

      <RequestsList
        dict={dict}
        locale={locale}
        requests={requests}
        types={types}
        total={requestsTotal}
        approvals={approvals}
        todayDate={todayDate}
        busyId={busyId}
        onAct={act}
      />

      <section aria-labelledby="leave-calendar-heading" className="flex flex-col gap-4">
        <h2 id="leave-calendar-heading" className="text-lg font-semibold">
          {t.calendar.heading}
        </h2>
        <nav aria-label={t.calendar.monthNavLabel} className="flex flex-wrap items-center gap-2 text-sm">
          <button
            type="button"
            onClick={() => goToMonth(shiftMonth(monthStart, -1))}
            className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
          >
            ← {t.calendar.previousMonth}
          </button>
          <button
            type="button"
            onClick={() => goToMonth(monthStart)}
            className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
          >
            {t.calendar.thisMonth}
          </button>
          <button
            type="button"
            onClick={() => goToMonth(shiftMonth(monthStart, 1))}
            className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
          >
            {t.calendar.nextMonth} →
          </button>
          <span className="tabular text-fg-subtle" data-testid="leave-calendar-month">
            {t.calendar.monthOf.replace("{month}", formatMonth(monthStart, locale))}
          </span>
        </nav>
        {calendar === null ? (
          <p role="status" className="text-fg-muted">
            {t.calendar.loading}
          </p>
        ) : (
          <LeaveCalendar
            dict={dict}
            locale={locale}
            calendar={calendar}
            types={types}
            monthStart={monthStart}
            monthEnd={monthEnd}
          />
        )}
      </section>
    </div>
  );
}

/** The page's one `h1`, with the year and the allowance the figures come from. */
function Header({
  dict,
  locale,
  year,
  allowance,
}: {
  dict: Dictionary;
  locale: Locale;
  year: number;
  allowance: number | null;
}) {
  const t = dict.leave;
  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
      <p className="mt-2 text-fg-muted">{t.intro}</p>
      <p className="tabular mt-2 text-sm text-fg-subtle">
        {t.yearOf.replace("{year}", formatNumber(year, locale, { useGrouping: false }))}
        {allowance !== null &&
          ` · ${t.allowance.replace("{days}", formatNumber(allowance, locale))}`}
      </p>
    </div>
  );
}

/** One figure, with its label. Numbers right-aligned and tabular so they compare (§2.3). */
function Figure({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div>
      <dt className="text-fg-subtle">{label}</dt>
      <dd className={`tabular ${strong ? "text-lg font-semibold" : ""}`}>{value}</dd>
    </div>
  );
}

/**
 * The balances: the allowance per type, and the ledger behind each one.
 *
 * The history is a `<details>` rather than a dialog or a toggle: it is a document-shaped
 * list that a reader may or may not want, the browser already gives it keyboard operation
 * and a disclosure state assistive technology announces, and nothing has to be fetched to
 * show it — the movements travel with the balance.
 */
function Balances({
  dict,
  locale,
  balances,
  types,
}: {
  dict: Dictionary;
  locale: Locale;
  balances: LeaveBalance[];
  types: LeaveType[];
}) {
  const t = dict.leave;
  if (balances.length === 0) {
    return (
      <Card title={t.balances.heading} headingId="leave-balances-heading">
        <Alert tone="neutral">
          <p className="font-medium">{t.balances.empty}</p>
          <p className="mt-1 text-sm">{t.balances.emptyHint}</p>
        </Alert>
      </Card>
    );
  }

  return (
    <Card title={t.balances.heading} headingId="leave-balances-heading">
      <ul className="flex flex-col gap-6" data-testid="leave-balances">
        {balances.map((balance) => {
          const catalogue = types.find((type) => type.code === balance.leave_type);
          return (
            <li key={`${balance.year}-${balance.leave_type}`}>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h3 className="text-base font-semibold">{balanceTypeName(balance, locale)}</h3>
                {catalogue && !catalogue.counts_against_annual && (
                  <span className="text-sm text-fg-subtle">{t.balances.noAllowanceType}</span>
                )}
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                <Figure
                  label={t.balances.entitled}
                  value={formatNumber(balance.entitled_days, locale)}
                />
                <Figure
                  label={t.balances.carried}
                  value={formatNumber(balance.carried_over_days, locale)}
                />
                <Figure label={t.balances.used} value={formatNumber(balance.used_days, locale)} />
                <Figure
                  label={t.balances.pending}
                  value={formatNumber(balance.pending_days, locale)}
                />
                <Figure
                  label={t.balances.remaining}
                  value={formatNumber(balance.remaining_days, locale)}
                  strong
                />
              </dl>
              {balance.projected && (
                <p className="mt-2 text-sm text-fg-subtle">{t.balances.projected}</p>
              )}
              <details className="mt-2">
                <summary className="cursor-pointer text-sm text-fg-muted">
                  {t.balances.historyHeading}
                </summary>
                {balance.history.length === 0 ? (
                  <p className="mt-2 text-sm text-fg-subtle">{t.balances.historyEmpty}</p>
                ) : (
                  <ul className="mt-2 flex flex-col gap-1 text-sm">
                    {balance.history.map((entry, index) => (
                      <li
                        key={`${entry.entry_type}-${index}`}
                        className="flex flex-wrap items-baseline gap-x-2 gap-y-1"
                      >
                        <span className="text-fg">{t.entry[entry.entry_type]}</span>
                        <span className="tabular text-fg-muted">
                          {t.balances.historyRow
                            .replace("{days}", formatNumber(entry.days, locale))
                            .replace("{remaining}", formatNumber(entry.remaining_days, locale))}
                        </span>
                        {entry.created_at !== null && (
                          <span className="tabular text-fg-subtle">
                            {formatDate(entry.created_at, locale)}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </details>
            </li>
          );
        })}
      </ul>
      {/* What the panel does *not* show, said rather than left to be wondered about: the
          other leave types have no annual allowance, so the API has no figures for them. */}
      <p className="mt-4 text-sm text-fg-subtle">{t.balances.otherTypes}</p>
    </Card>
  );
}

/**
 * The form: draft, look at what the API says the range is worth, then file it.
 *
 * The two steps are the API's own shape — a draft reserves nothing and a submission
 * reserves the days — and they are what lets the reader see the computed working-day count
 * *before* it costs them anything. The draft is kept in state rather than thrown away, so
 * the second button files exactly the document the first one wrote.
 */
function RequestForm({
  dict,
  locale,
  types,
  todayDate,
  onFiled,
  onError,
}: {
  dict: Dictionary;
  locale: Locale;
  types: LeaveType[];
  todayDate: string;
  onFiled: () => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const t = dict.leave.request;
  const [code, setCode] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [attachment, setAttachment] = useState("");
  const [draft, setDraft] = useState<LeaveRequestDetail | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"preview" | "submit" | null>(null);

  const selected = types.find((type) => type.code === code);

  async function preview(event: React.FormEvent) {
    event.preventDefault();
    onError(null);
    setLocalError(null);
    if (code === "") return setLocalError(t.typeRequired);
    if (start === "") return setLocalError(t.startRequired);
    if (end === "") return setLocalError(t.endRequired);
    if (end < start) return setLocalError(t.endBeforeStart);
    if (selected?.requires_attachment && attachment.trim() === "") {
      return setLocalError(t.attachmentRequired);
    }

    setBusy("preview");
    try {
      const created = await draftRequest({
        leave_type: code,
        start_date: start,
        end_date: end,
        attachment_reference: attachment.trim() || null,
      });
      setDraft(created);
    } catch (cause) {
      setDraft(null);
      setLocalError(catalogueErrorText(cause, dict, t.preview));
    } finally {
      setBusy(null);
    }
  }

  async function submit() {
    if (draft === null) return;
    setBusy("submit");
    onError(null);
    try {
      await submitRequest(draft.id);
      setDraft(null);
      setStart("");
      setEnd("");
      setAttachment("");
      await onFiled();
    } catch (cause) {
      onError(catalogueErrorText(cause, dict, t.submit));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card title={t.heading} description={t.description} headingId="leave-request-heading">
      <form className="flex flex-col gap-4" onSubmit={preview} noValidate>
        <div className="grid gap-4 sm:grid-cols-2">
          <SelectField
            label={t.typeLabel}
            value={code}
            onChange={(value) => {
              setCode(value);
              setDraft(null);
            }}
            options={types.map((type) => ({ value: type.code, label: leaveTypeName(type, locale) }))}
            placeholder={t.typePlaceholder}
            required
          />
          <TextField
            label={t.startLabel}
            type="date"
            value={start}
            onChange={(value) => {
              setStart(value);
              setDraft(null);
            }}
            // Only the future can be requested: a leave that has begun is HR's correction
            // flow, which the API states with its own code.
            min={todayDate}
            required
          />
          <TextField
            label={t.endLabel}
            type="date"
            value={end}
            onChange={(value) => {
              setEnd(value);
              setDraft(null);
            }}
            min={start || todayDate}
            required
          />
          {selected?.requires_attachment && (
            <TextField
              label={t.attachmentLabel}
              value={attachment}
              onChange={(value) => {
                setAttachment(value);
                setDraft(null);
              }}
              hint={t.attachmentHint}
              required
            />
          )}
        </div>

        {selected && (
          <ul className="flex flex-wrap gap-2 text-sm">
            <li>
              <StatusBadge tone="neutral" label={selected.is_paid ? dict.leave.type.paid : dict.leave.type.unpaid} />
            </li>
            <li>
              <StatusBadge
                tone={selected.requires_attachment ? "warning" : "neutral"}
                label={
                  selected.requires_attachment
                    ? dict.leave.type.needsAttachment
                    : dict.leave.type.noAttachment
                }
              />
            </li>
            {selected.counts_against_annual && (
              <li>
                <StatusBadge tone="info" label={dict.leave.type.countsAgainstAnnual} />
              </li>
            )}
            <li className="text-fg-subtle">{t.overlapHint}</li>
          </ul>
        )}

        {localError !== null && (
          <Alert tone="danger" role="alert">
            {localError}
          </Alert>
        )}

        {/* The API's own answer about the range, before anything is reserved. */}
        {draft !== null && (
          <div data-testid="leave-draft">
            <Alert tone="info">
              <p className="font-medium">
                {t.computed.replace("{days}", formatNumber(draft.business_days_count, locale))}
              </p>
              {draft.allocations.length > 1 && (
                <p className="mt-1 text-sm">
                  {t.computedSplit.replace(
                    "{parts}",
                    draft.allocations
                      .map(
                        (part) =>
                          `${formatNumber(part.year, locale, { useGrouping: false })}: ${formatNumber(part.days, locale)}`,
                      )
                      .join(" · "),
                  )}
                </p>
              )}
              <p className="mt-1 text-sm">{t.drafted}</p>
            </Alert>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={busy !== null}>
            {busy === "preview" ? t.previewing : t.preview}
          </Button>
          {draft !== null && (
            <Button onClick={submit} disabled={busy !== null}>
              {busy === "submit" ? t.submitting : t.submit}
            </Button>
          )}
        </div>

        {/* The design's own rule, said out loud: there is no note field, and why. */}
        <p className="text-sm text-fg-subtle">{t.noNote}</p>
      </form>
    </Card>
  );
}

/** The caller's requests, with their states, the API's day count and the engine's decisions. */
function RequestsList({
  dict,
  locale,
  requests,
  types,
  total,
  approvals,
  todayDate,
  busyId,
  onAct,
}: {
  dict: Dictionary;
  locale: Locale;
  requests: LeaveRequest[];
  types: LeaveType[];
  total: number | null;
  approvals: Record<string, LeaveApproval | null>;
  todayDate: string;
  busyId: string | null;
  onAct: (id: string, action: "submit" | "withdraw") => Promise<void>;
}) {
  const t = dict.leave;
  return (
    <section aria-labelledby="leave-list-heading" className="flex flex-col gap-3">
      <h2 id="leave-list-heading" className="text-lg font-semibold">
        {t.list.heading}
        {total !== null && (
          <span className="tabular ml-2 text-sm font-normal text-fg-subtle">
            {formatNumber(total, locale)}
          </span>
        )}
      </h2>
      {requests.length === 0 ? (
        <Alert tone="neutral">
          <p className="font-medium">{t.list.empty}</p>
          <p className="mt-1 text-sm">{t.list.emptyHint}</p>
        </Alert>
      ) : (
        <ul className="flex flex-col gap-3" data-testid="leave-requests">
          {requests.map((request) => {
            const approval = approvals[request.id] ?? null;
            const withdrawable = request.start_date > todayDate;
            return (
              <li
                key={request.id}
                className="rounded-lg border border-border bg-surface p-4"
                data-leave-state={request.state}
              >
                <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">
                        {calendarTypeName(request.leave_type, types, locale)}
                      </span>
                      <StatusBadge
                        tone={requestTone(request.state)}
                        label={t.list.state[request.state]}
                      />
                      <span className="tabular text-fg-muted" data-testid="leave-days">
                        {t.list.days.replace(
                          "{days}",
                          formatNumber(request.business_days_count, locale),
                        )}
                      </span>
                    </p>
                    <p className="tabular mt-1 text-sm text-fg-muted">
                      {t.list.period}: {formatDate(request.start_date, locale)} —{" "}
                      {formatDate(request.end_date, locale)}
                      {request.created_at !== null && (
                        <>
                          {" · "}
                          {t.list.requestedOn}: {formatDate(request.created_at, locale)}
                        </>
                      )}
                    </p>
                    {request.allocations.length > 1 && (
                      <p className="tabular mt-1 text-sm text-fg-subtle">
                        {t.list.daysSplit.replace(
                          "{parts}",
                          request.allocations
                            .map(
                              (part) =>
                                `${formatNumber(part.year, locale, { useGrouping: false })}: ${formatNumber(part.days, locale)}`,
                            )
                            .join(" · "),
                        )}
                      </p>
                    )}
                    {request.has_attachment && (
                      <p className="mt-1 text-sm text-fg-subtle">
                        {t.list.attachment}
                        {request.attachment_reference === null && ` · ${t.list.attachmentHrOnly}`}
                      </p>
                    )}

                    <div className="mt-2">
                      <h3 className="text-sm font-medium text-fg-muted">
                        {t.list.decisionsHeading}
                      </h3>
                      {approval === null || approval.decisions.length === 0 ? (
                        <p className="text-sm text-fg-subtle">{t.list.decisionsEmpty}</p>
                      ) : (
                        <ul className="mt-1 flex flex-col gap-1 text-sm">
                          {approval.decisions.map((decision, index) => (
                            <li
                              key={`${decision.level}-${decision.round}-${index}`}
                              className="flex flex-wrap items-baseline gap-x-2 gap-y-1"
                            >
                              <span className="text-fg-subtle">
                                {t.list.decisionLine
                                  .replace("{level}", formatNumber(decision.level, locale))
                                  .replace("{round}", formatNumber(decision.round, locale))}
                              </span>
                              <StatusBadge
                                tone={decisionTone(decision.decision)}
                                label={decisionLabel(dict, decision.decision)}
                              />
                              <span className="tabular text-fg-subtle">
                                {formatDate(decision.decided_at, locale)}
                              </span>
                              {decision.comment !== null && (
                                <span className="text-fg-muted">
                                  {t.list.comment}: “{decision.comment}”
                                </span>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    {/* Secondary, both of them: the page's one primary action is the form
                        above, and a blue button on every draft row would make a list action
                        look like the screen's protagonist (§4.2). */}
                    {request.state === "draft" && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => onAct(request.id, "submit")}
                        disabled={busyId !== null}
                      >
                        {busyId === request.id ? t.request.submitting : t.request.submit}
                      </Button>
                    )}
                    {(request.state === "in_approval" || request.state === "approved") &&
                      withdrawable && (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => onAct(request.id, "withdraw")}
                          disabled={busyId !== null}
                        >
                          {busyId === request.id ? t.list.withdrawing : t.list.withdraw}
                        </Button>
                      )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** The tone each request state carries. */
function requestTone(state: LeaveRequestState): StatusTone {
  switch (state) {
    case "approved":
      return "success";
    case "rejected":
      return "danger";
    case "in_approval":
      return "info";
    case "draft":
    case "withdrawn":
      return "neutral";
  }
}

/** The tone of one engine decision. An unknown outcome reads as neutral rather than green. */
function decisionTone(decision: string): StatusTone {
  switch (decision) {
    case "approved":
      return "success";
    case "rejected":
      return "danger";
    case "returned":
      return "warning";
    case "skipped":
      return "neutral";
    default:
      return "info";
  }
}

/** A decision's name, degrading to the outcome itself for one this build has not learned. */
function decisionLabel(dict: Dictionary, decision: string): string {
  const known = dict.leave.list.decisions as Record<string, string | undefined>;
  return known[decision] ?? decision;
}
