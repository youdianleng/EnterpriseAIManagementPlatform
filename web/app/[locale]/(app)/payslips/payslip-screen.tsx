"use client";

import { useEffect, useId, useMemo, useState } from "react";

import { ApiError } from "@/lib/api/client";
import {
  missingExportUrl,
  previousMonth,
  readEmployees,
  readMissing,
  uploadBatch,
  type AttributedPayslip,
  type BatchAnswer,
  type EmployeeOption,
  type MissingEmployee,
  type UnmatchedFile,
  type UnmatchedReason,
} from "@/lib/api/payslips";
import { formatDate, formatNumber } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { Card } from "@/lib/ui/card";
import { Dialog } from "@/lib/ui/dialog";
import { SelectField, TextField } from "@/lib/ui/field";
import { StatusBadge } from "@/lib/ui/status-badge";

/** The batch ceiling, mirroring the API's `MAX_BATCH_FILES`. */
const MAX_FILES = 400;

/** The months the picker offers, back from the current one. Two years of payslips. */
const MONTH_CHOICES = 24;

type Choice = {
  /** The `File` itself, kept so the same object is uploaded and re-uploaded. */
  file: File;
  /** The employee the uploader named for this file, or "" for "use the filename". */
  employeeId: string;
};

/**
 * The payslip upload, and the two lists it answers with (ticket 44, design system §6.3).
 *
 * Four rules from the design system, each of which is a thing this screen has to *do*
 * rather than merely know:
 *
 * 1. **The two lists are presented together** (§6.3's first line). They arrive in one
 *    response and are drawn as two sections of one screen, never one after the other: a
 *    screen that showed "9 uploaded" and then asked you to click for the missing list
 *    would let the half that looks like success stand in for the half that matters.
 * 2. **The missing list is visually prominent**, because it is what the screen is *for*
 *    (§6.3: 视觉上足够醒目，因为这是本界面的核心价值). It is the first list under the
 *    uploader, it is the one with a coloured surface and a heavy heading, and it carries
 *    the export link — while the attributed list is a plain table.
 * 3. **An unattributed file is listed with its reason, never dropped** (§6.3's second
 *    line). Every reason the server can send has a sentence here; the vocabulary is closed
 *    and translated in full, and an unknown token degrades to a general sentence rather
 *    than a blank cell.
 * 4. **The overwrite is a two-request confirmation in words** (§6.3's third line:
 *    「将覆盖 X 名员工的 Y 月工资单」). The first request is `confirm: false` — the server
 *    matches the files and writes nothing — and its answer supplies the count and the month
 *    the dialog states. Cancelling it leaves no trace at all, which is what makes the
 *    confirmation meaningful rather than decorative.
 *
 * The permission refusal is the fourth state: a reader who reaches this page without
 * `payslip.manage` sees the sentence rather than a broken uploader, and it is the API's
 * own 403 that produces it — the screen never guesses at who may upload.
 */
export function PayslipScreen({
  dict,
  locale,
  initialPeriod,
  initialMissing,
  initialMissingFailed,
  employees,
}: {
  dict: Dictionary;
  locale: Locale;
  initialPeriod: string;
  initialMissing: MissingEmployee[] | null;
  initialMissingFailed: boolean;
  employees: EmployeeOption[];
}) {
  const t = dict.payslips;
  const [period, setPeriod] = useState(initialPeriod);
  const [missing, setMissing] = useState(initialMissing);
  const [missingExpected, setMissingExpected] = useState<number | null>(null);
  const [refused, setRefused] = useState(false);
  const [locked, setLocked] = useState(initialMissingFailed);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [answer, setAnswer] = useState<BatchAnswer | null>(null);
  const [people, setPeople] = useState(employees);

  // The server rendered the month, so it is the source of truth for it. A refresh after an
  // upload brings a new list, and the local state follows rather than fighting it.
  useEffect(() => {
    if (initialMissing !== null) {
      setMissing(initialMissing);
      setLocked(false);
      setRefused(false);
    } else if (initialMissingFailed) {
      setLocked(true);
    }
  }, [initialMissing, initialMissingFailed]);

  async function loadMissing(next: string) {
    try {
      const listing = await readMissing(next);
      setMissing(listing.items);
      setMissingExpected(listing.expected);
      setLocked(false);
      setRefused(false);
      setError(null);
      // The selector follows the month: the people who could be named for a file are the
      // people on the books in *that* month, and a list that did not move would offer
      // somebody who had not been hired yet.
      setPeople(await readEmployees(next));
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 403) {
        setRefused(true);
        return;
      }
      setLocked(true);
    }
  }

  function onMonthChange(next: string) {
    setPeriod(next);
    setAnswer(null);
    setNotice(null);
    setError(null);
    void loadMissing(next);
  }

  function onUploaded(result: BatchAnswer) {
    setAnswer(result);
    setNotice(summaryOf(result, dict, locale));
    void loadMissing(result.period);
  }

  if (refused) {
    return (
      <Alert tone="warning" title={t.permission.title} data-testid="payslip-refused">
        <p className="mt-1">{t.permission.body}</p>
      </Alert>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      <UploadCard
        dict={dict}
        locale={locale}
        period={period}
        employees={people}
        onMonthChange={onMonthChange}
        onUploaded={onUploaded}
        onError={setError}
      />

      {error && (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      )}
      {notice && (
        <Alert tone="success" role="status">
          {notice}
        </Alert>
      )}

      <MissingSection
        dict={dict}
        locale={locale}
        period={period}
        items={missing}
        expected={missingExpected}
        failed={locked}
        onRetry={() => void loadMissing(period)}
      />

      <AttributedSection dict={dict} locale={locale} answer={answer} />

      <UnmatchedSection dict={dict} locale={locale} answer={answer} />
    </div>
  );
}

/** One person the selector can name: the id, the staff number and the name. */
export type { EmployeeOption };

/** The upload: a month, the files, and a per-file employee when a name carries none. */
function UploadCard({
  dict,
  locale,
  period,
  employees,
  onMonthChange,
  onUploaded,
  onError,
}: {
  dict: Dictionary;
  locale: Locale;
  period: string;
  employees: EmployeeOption[];
  onMonthChange: (period: string) => void;
  onUploaded: (answer: BatchAnswer) => void;
  onError: (message: string | null) => void;
}) {
  const t = dict.payslips;
  const [choices, setChoices] = useState<Choice[]>([]);
  const [localError, setLocalError] = useState<string | null>(null);
  const [preview, setPreview] = useState<BatchAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const filesId = useId();

  const months = useMemo(() => monthChoices(period), [period]);
  const options = useMemo(
    () =>
      employees.map((employee) => ({
        value: employee.employee_id,
        label: employee.employee_no
          ? `${employee.employee_no} — ${employee.employee_name}`
          : employee.employee_name,
      })),
    [employees],
  );

  function choose(list: FileList | null) {
    const picked = Array.from(list ?? []).slice(0, MAX_FILES);
    setChoices(picked.map((file) => ({ file, employeeId: "" })));
    setLocalError(null);
    setPreview(null);
  }

  function setEmployee(index: number, employeeId: string) {
    setChoices((current) =>
      current.map((entry, position) =>
        position === index ? { ...entry, employeeId } : entry,
      ),
    );
  }

  function payload() {
    return choices.map((entry) => ({
      file: entry.file,
      employeeId: entry.employeeId || undefined,
    }));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    onError(null);
    setPreview(null);
    if (choices.length === 0) {
      setLocalError(t.upload.nothingSelected);
      return;
    }
    setLocalError(null);
    setBusy(true);
    try {
      // **The dry run.** Nothing is written: the server matches the files and answers with
      // the two lists plus how many payslips the upload would replace, which is exactly what
      // the confirmation has to name. A batch that replaces nothing commits straight away —
      // there is nothing dangerous to agree to, and an extra dialog on the ordinary case
      // would train the reader to click through the one that matters.
      const reviewed = await uploadBatch(period, payload(), false);
      if (reviewed.reserved_count > 0) {
        setPreview(reviewed);
        return;
      }
      await commit();
    } catch (cause) {
      onError(errorText(dict, cause, t.upload.refused));
    } finally {
      setBusy(false);
    }
  }

  /** The same request again, this time writing: `confirm` is true on the wire. */
  async function commit() {
    try {
      const result = await uploadBatch(period, payload(), true);
      setPreview(null);
      setChoices([]);
      onUploaded(result);
    } catch (cause) {
      setPreview(null);
      onError(errorText(dict, cause, t.upload.refused));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={t.upload.heading} description={t.upload.description} headingId="payslip-upload">
      <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
        <div className="grid gap-4 sm:grid-cols-2">
          <SelectField
            label={t.upload.monthLabel}
            value={period}
            onChange={onMonthChange}
            hint={t.upload.monthHint}
            options={months}
          />
          <div className="flex flex-col gap-1">
            <label htmlFor={filesId} className="text-sm font-medium">
              {t.upload.filesLabel}
            </label>
            <input
              id={filesId}
              name="files"
              type="file"
              multiple
              accept=".pdf,application/pdf"
              aria-describedby={`${filesId}-hint`}
              onChange={(event) => choose(event.target.files)}
              className="min-h-11 w-full rounded border border-border bg-surface px-3 py-2 text-fg file:mr-3 file:rounded file:border-0 file:bg-neutral-bg file:px-3 file:py-1.5"
            />
            <p id={`${filesId}-hint`} className="text-sm text-fg-subtle">
              {t.upload.filesHint}
            </p>
          </div>
        </div>

        {choices.length > 0 && (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-fg-muted" data-testid="payslip-selected-count">
              {t.upload.selectedCount.replace(
                "{count}",
                formatNumber(choices.length, locale),
              )}
            </p>
            <ul className="flex flex-col gap-2">
              {choices.map((entry, index) => (
                <li key={`${entry.file.name}-${index}`} className="min-w-0">
                  {/* The selector is *per file* and its label names the file, because the
                      pairing is positional: this is the control the checklist means by
                      「界面选择」, and a bare select would not say which file it settles. */}
                  <SelectField
                    label={t.upload.employeeLabel.replace("{filename}", entry.file.name)}
                    value={entry.employeeId}
                    onChange={(value) => setEmployee(index, value)}
                    placeholder={t.upload.employeePlaceholder}
                    hint={index === 0 ? t.upload.employeeHint : undefined}
                    options={options}
                  />
                </li>
              ))}
            </ul>
          </div>
        )}

        {localError && (
          <Alert tone="danger" role="alert">
            {localError}
          </Alert>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={busy}>
            {busy ? t.upload.submitting : t.upload.submit}
          </Button>
          {choices.length > 0 && (
            <Button
              variant="ghost"
              type="button"
              onClick={() => {
                setChoices([]);
                setPreview(null);
              }}
            >
              {t.upload.clear}
            </Button>
          )}
        </div>
      </form>

      {/* §6.3's third rule: the overwrite is confirmed in words, naming how many employees
          and which month, and cancelling it leaves nothing behind — because the request
          that produced this dialog wrote nothing. */}
      <Dialog
        open={preview !== null}
        onClose={() => setPreview(null)}
        title={t.overwrite.title}
        closeLabel={t.overwrite.cancel}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPreview(null)}>
              {t.overwrite.cancel}
            </Button>
            <Button disabled={busy} onClick={() => void commit()}>
              {busy ? t.overwrite.confirming : t.overwrite.confirm}
            </Button>
          </>
        }
      >
        <p data-testid="payslip-overwrite-body">
          {t.overwrite.body
            .replace(
              "{count}",
              formatNumber(preview?.reserved_count ?? 0, locale),
            )
            .replace("{period}", preview?.period ?? period)}
        </p>
        <p className="mt-2 text-sm">{t.overwrite.hint}</p>
      </Dialog>
    </Card>
  );
}

/** The missing list. Prominent on purpose: it is the screen's whole value (§6.3). */
function MissingSection({
  dict,
  locale,
  period,
  items,
  expected,
  failed,
  onRetry,
}: {
  dict: Dictionary;
  locale: Locale;
  period: string;
  items: MissingEmployee[] | null;
  expected: number | null;
  failed: boolean;
  onRetry: () => void;
}) {
  const t = dict.payslips.missing;

  return (
    <section
      aria-labelledby="payslip-missing-heading"
      data-testid="payslip-missing"
      // The one section with a tinted surface and a heavier heading: §6.3 asks for the
      // missing list to be 视觉上足够醒目, and the tint is *beside* the words rather than
      // instead of them.
      className="rounded-lg border-2 border-warning bg-warning-bg p-6"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <div className="min-w-0">
          <h2 id="payslip-missing-heading" className="text-lg font-semibold text-warning">
            {t.heading}
          </h2>
          <p className="mt-1 text-fg-muted">{t.description}</p>
        </div>
        <a
          href={missingExportUrl(period)}
          className="inline-flex min-h-11 items-center rounded border border-border bg-surface px-3 text-sm text-fg hover:bg-neutral-bg"
          data-testid="payslip-missing-export"
        >
          {t.export}
        </a>
      </div>
      <p className="mt-1 text-sm text-fg-subtle">{t.exportHint}</p>

      {failed ? (
        <Alert tone="danger" role="alert" title={dict.payslips.error} className="mt-4">
          <p className="mt-1">{dict.payslips.errorHint}</p>
          <Button variant="secondary" size="sm" className="mt-3" onClick={onRetry}>
            {dict.payslips.retry}
          </Button>
        </Alert>
      ) : items === null ? (
        <p className="mt-4 text-fg-muted">{dict.payslips.loading}</p>
      ) : items.length === 0 ? (
        <div className="mt-4">
          <StatusBadge
            tone="success"
            label={
              expected === 0
                ? t.noneExpected.replace("{period}", period)
                : t.noneInPeriod.replace("{period}", period)
            }
          />
          {expected !== null && (
            <p className="mt-2 text-sm text-fg-subtle">
              {t.expectedNote.replace("{expected}", formatNumber(expected, locale))}
            </p>
          )}
        </div>
      ) : (
        <>
          <p className="mt-3 font-medium text-warning" data-testid="payslip-missing-count">
            {t.count
              .replace("{count}", formatNumber(items.length, locale))
              .replace("{period}", period)}
          </p>
          {expected !== null && (
            <p className="mt-1 text-sm text-fg-subtle">
              {t.expectedNote.replace("{expected}", formatNumber(expected, locale))}
            </p>
          )}
          <div className="mt-3 overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm">
              <caption className="sr-only">{t.heading}</caption>
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.employee}
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.number}
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.department}
                  </th>
                  <th scope="col" className="py-2 font-medium">
                    {t.since}
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.employee_id}
                    className="border-b border-border/60"
                    data-testid="payslip-missing-row"
                  >
                    <td className="py-2 pr-4">{item.employee_name}</td>
                    <td className="tabular py-2 pr-4">{item.employee_no ?? "—"}</td>
                    <td className="py-2 pr-4">{item.department_name ?? "—"}</td>
                    <td className="tabular py-2">
                      {formatDate(item.salary_effective_from, locale)}
                      {" — "}
                      {item.salary_effective_to
                        ? formatDate(item.salary_effective_to, locale)
                        : t.open}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

/** What was attributed. A plain list: the eye should land on the missing one first. */
function AttributedSection({
  dict,
  locale,
  answer,
}: {
  dict: Dictionary;
  locale: Locale;
  answer: BatchAnswer | null;
}) {
  const t = dict.payslips.attributed;
  const rows: AttributedPayslip[] = answer?.attributed ?? [];
  const total = answer?.total_count ?? 0;

  return (
    <section aria-labelledby="payslip-attributed-heading" data-testid="payslip-attributed">
      <div className="max-w-3xl">
        <h2 id="payslip-attributed-heading" className="text-lg font-semibold">
          {t.heading}
        </h2>
        <p className="mt-1 text-fg-muted">{t.description}</p>
      </div>

      {answer === null ? (
        <Alert tone="neutral" className="mt-4">
          <p>{t.empty}</p>
          <p className="mt-1 text-sm">{t.emptyHint}</p>
        </Alert>
      ) : (
        <>
          <p className="mt-3 text-sm text-fg-muted">
            {t.count
              .replace("{count}", formatNumber(rows.length, locale))
              .replace("{total}", formatNumber(total, locale))}
          </p>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm">
              <caption className="sr-only">{t.heading}</caption>
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.employee}
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.number}
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.file}
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.size}
                  </th>
                  <th scope="col" className="py-2 font-medium">
                    {t.state}
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-border/60 align-top"
                    data-testid="payslip-attributed-row"
                  >
                    <td className="py-2 pr-4">{row.employee_name}</td>
                    <td className="tabular py-2 pr-4">{row.employee_no ?? "—"}</td>
                    <td className="py-2 pr-4 break-all">{row.filename}</td>
                    <td className="tabular py-2 pr-4">
                      {formatNumber(
                        Math.max(1, Math.round(Number(row.file_size) / 1024)),
                        locale,
                      )}{" "}
                      kB
                    </td>
                    <td className="py-2">
                      {/* §5: the state is a word beside a colour, never the colour alone. */}
                      <StatusBadge
                        tone={row.replaced ? "warning" : "success"}
                        label={row.replaced ? t.replaced : t.newFile}
                      />
                      {row.replaced && row.previous_sha256 && (
                        <p className="mt-1 text-xs text-fg-subtle">
                          {t.previous.replace("{sha}", row.previous_sha256.slice(0, 12))}
                        </p>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

/** Files that matched nobody, with the reason. The half 「不静默丢弃」 is about. */
function UnmatchedSection({
  dict,
  locale,
  answer,
}: {
  dict: Dictionary;
  locale: Locale;
  answer: BatchAnswer | null;
}) {
  const t = dict.payslips.unmatched;
  const rows: UnmatchedFile[] = answer?.unmatched ?? [];

  return (
    <section aria-labelledby="payslip-unmatched-heading" data-testid="payslip-unmatched">
      <div className="max-w-3xl">
        <h2 id="payslip-unmatched-heading" className="text-lg font-semibold">
          {t.heading}
        </h2>
        <p className="mt-1 text-fg-muted">{t.description}</p>
      </div>

      {answer === null ? (
        <Alert tone="neutral" className="mt-4">
          {t.none}
        </Alert>
      ) : rows.length === 0 ? (
        <div className="mt-4">
          <StatusBadge tone="success" label={t.none} />
        </div>
      ) : (
        <>
          <p className="mt-3 text-sm text-fg-muted">
            {t.count.replace("{count}", formatNumber(rows.length, locale))}
          </p>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm">
              <caption className="sr-only">{t.heading}</caption>
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.file}
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.number}
                  </th>
                  <th scope="col" className="py-2 pr-4 font-medium">
                    {t.reason}
                  </th>
                  <th scope="col" className="py-2 font-medium">
                    {t.detail}
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <tr
                    key={`${row.filename}-${index}`}
                    className="border-b border-border/60 align-top"
                    data-testid="payslip-unmatched-row"
                    data-reason={row.reason}
                  >
                    <td className="py-2 pr-4 break-all">{row.filename}</td>
                    <td className="tabular py-2 pr-4">{row.employee_no ?? "—"}</td>
                    <td className="py-2 pr-4" data-testid="payslip-unmatched-reason">
                      {reasonText(dict, row.reason)}
                    </td>
                    <td className="py-2 text-fg-subtle">{row.detail ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

/**
 * The reason a file was not attributed, in the reader's language.
 *
 * The server sends a token from a **closed** vocabulary and never a sentence, so the
 * sentence is this dictionary's — and `unknown` is the degradation for a token a newer
 * server than this build might send. A blank cell where a reason belongs would be exactly
 * the silence the ticket forbids.
 */
function reasonText(dict: Dictionary, reason: UnmatchedReason): string {
  const t = dict.payslips.reasons;
  const known = reason as keyof typeof t;
  return known in t ? t[known] : t.unknown;
}

/** One sentence saying what the upload did, built from the server's own counts. */
function summaryOf(answer: BatchAnswer, dict: Dictionary, locale: Locale): string {
  const t = dict.payslips.outcome;
  const parts = [
    t.filed
      .replace("{attributed}", formatNumber(answer.attributed_count, locale))
      .replace("{total}", formatNumber(answer.total_count, locale))
      .replace("{period}", answer.period),
  ];
  if (answer.replaced_count > 0) {
    parts.push(t.replaced.replace("{count}", formatNumber(answer.replaced_count, locale)));
  }
  if (answer.unmatched_count > 0) {
    parts.push(t.refused.replace("{count}", formatNumber(answer.unmatched_count, locale)));
  } else {
    parts.push(t.noticeKey);
  }
  if (answer.missing_count > 0) {
    parts.push(t.missing.replace("{count}", formatNumber(answer.missing_count, locale)));
  }
  return parts.join(" ");
}

/**
 * The months the picker offers, newest first, always including the one on screen.
 *
 * Derived from the browser's own date *components* rather than from `toISOString()`, for
 * the reason `lib/api/payslips.previousMonth` gives: a UTC conversion names the wrong month
 * for anybody east of Greenwich on the first of it.
 */
function monthChoices(current: string): Array<{ value: string; label: string }> {
  const [year, month] = current.split("-").map((part) => Number(part));
  const options: Array<{ value: string; label: string }> = [];
  for (let back = 0; back < MONTH_CHOICES; back += 1) {
    const zeroBased = month - 1 - back;
    const optionYear = year + Math.floor(zeroBased / 12);
    const optionMonth = ((zeroBased % 12) + 12) % 12;
    const value = `${optionYear}-${`${optionMonth + 1}`.padStart(2, "0")}`;
    options.push({ value, label: value });
  }
  return options;
}

/**
 * The reader's language for a failure, from the catalogue key when the frontend knows it.
 *
 * `payslip.*` keys are the module's own; anything else falls back to the caller's sentence,
 * which is the same shape every other screen uses.
 */
function errorText(dict: Dictionary, cause: unknown, fallback: string): string {
  if (cause instanceof ApiError && cause.messageKey) {
    const known = cause.messageKey.replace(/^errors\./, "") as keyof Dictionary["errors"];
    if (known in dict.errors) return dict.errors[known];
  }
  return fallback;
}

export { previousMonth };
