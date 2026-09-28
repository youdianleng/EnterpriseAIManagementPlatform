"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError } from "@/lib/api/client";
import {
  addEntry,
  copyPreviousWeek,
  openSupplement,
  parseDay,
  projectTasks,
  removeEntry,
  selectableProjects,
  submitWeek,
  updateEntry,
  type Correction,
  type OverBudgetDay,
  type ProjectOption,
  type TaskOption,
  type TaskNet,
  type TimesheetDay,
  type TimesheetEntry,
  type TimesheetStatusRead,
  type TimesheetWeek,
} from "@/lib/api/timesheets";
import { formatDate, formatDuration, formatNumber } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";

/**
 * The grid.
 *
 * Four things the design system asks of this screen (`frontend-design-system.md` §6.1
 * and §7), and where each one lives:
 *
 * 1. **Keyboard entry, never mouse-only.** The grid is a real `<table>` whose cells are
 *    real `<button>`s in DOM order, so Tab and Shift+Tab walk the week and Enter opens
 *    the cell's editor; the editor's own Enter saves and Escape closes and returns the
 *    focus to the cell it came from. A `<td>` with a click handler is not focusable, and
 *    a grid that cannot be tabbed into is not keyboard-navigable however many handlers
 *    it carries. "Filling a week with a mouse is torture" is the design's sentence.
 * 2. **Live totals, per day and for the week.** They come from the *server's* response
 *    to every write rather than from a client-side sum, so the number on screen and the
 *    number in the row are the same number by construction.
 * 3. **The warning warns and never truncates.** A day over its expectation is shown with
 *    an icon, a word and a colour — never the colour alone (§5) — the minutes stay
 *    exactly as they were entered, and the submit button is not disabled by it.
 * 4. **Spanish is the longer language,** so nothing here has a fixed width: the layout is
 *    driven by padding and the table scrolls inside its own container at tablet sizes
 *    rather than dragging the page sideways.
 *
 * **The lock and the correction are drawn, not implied** (ticket 29, design system
 * §6.1): a locked week says so, the correction's adjustment lines are marked as
 * adjustments rather than looking like hours somebody typed, and the offer to correct
 * the week appears only while the server says the week is inside its window. Editing is
 * decided per *row* — `entry.timesheet_id === editable_timesheet_id` — which is what
 * lets a locked original and an open correction share one table.
 *
 * At 375px this component is not rendered at all — `timesheet-screen.tsx` renders the
 * "please use a desktop" panel instead, because seven columns on a phone is an interface
 * that is bad at both sizes (§7).
 */

/** Which cell's editor is open, and what it is for. */
type Editing =
  | { mode: "add"; day: string }
  | { mode: "edit"; day: string; entry: TimesheetEntry };

export function TimesheetGrid({
  dict,
  locale,
  week,
  grid,
  status,
  onGrid,
}: {
  dict: Dictionary;
  locale: Locale;
  week: string;
  grid: TimesheetWeek;
  status: TimesheetStatusRead | null;
  onGrid: (next: TimesheetWeek) => void;
}) {
  const t = dict.timesheets;
  const router = useRouter();
  const [editing, setEditing] = useState<Editing | null>(null);
  const [correcting, setCorrecting] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const cellRefs = useRef(new Map<string, HTMLElement>());

  // Which sheet may be written: the week's open one, which is the correction while a
  // correction is being filled in. A row of the locked original draws as read-only in
  // the same table, which is the whole reason the API answers per entry. The week is
  // writable when the server says so, which includes a week nobody has written yet —
  // there the first write is what creates the sheet, and `editableSheetId` is null.
  const editableSheetId = grid.editable_timesheet_id;
  const editable = grid.is_editable;
  const overByDate = useMemo(() => {
    const map = new Map<string, OverBudgetDay>();
    for (const day of grid.over_budget_days) map.set(day.entry_date, day);
    return map;
  }, [grid.over_budget_days]);

  /** One write, one error path: every action in this component goes through here. */
  async function run(action: () => Promise<TimesheetWeek>, done?: string) {
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      onGrid(await action());
      setEditing(null);
      if (done) setNotice(done);
      // The week list and the status panel are server-rendered, so a write that changes
      // them has to ask the server for them again.
      router.refresh();
    } catch (cause) {
      setError(errorText(dict, cause, grid.supplement_window_weeks));
    } finally {
      setPending(false);
    }
  }

  function remember(day: string, entryId: string | null, node: HTMLElement | null) {
    const key = cellKey(day, entryId);
    if (node) cellRefs.current.set(key, node);
    else cellRefs.current.delete(key);
  }

  function open(day: string, entry: TimesheetEntry | null) {
    setEditing(entry ? { mode: "edit", day, entry } : { mode: "add", day });
  }

  /** Escape, or a saved edit: close the editor and put the focus back where it was. */
  function close(day: string, entry: TimesheetEntry | null) {
    setEditing(null);
    requestAnimationFrame(() => cellRefs.current.get(cellKey(day, entry?.id ?? null))?.focus());
  }

  return (
    <section aria-labelledby="timesheet-grid-heading" className="flex flex-col gap-4">
      <h2 id="timesheet-grid-heading" className="sr-only">
        {t.caption}
      </h2>

      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <p className="text-sm font-medium">
          {t.weekOf
            .replace("{from}", formatDate(grid.week_start, locale))
            .replace("{to}", formatDate(grid.week_end, locale))}
        </p>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <StatusPill dict={dict} status={grid.status} />
          {grid.is_locked && <LockPill dict={dict} />}
          {grid.submitted_at && (
            <span className="text-fg-subtle">
              {t.submittedOn.replace("{date}", formatDate(grid.submitted_at, locale))}
            </span>
          )}
        </div>
      </div>

      <LockNotice dict={dict} grid={grid} />
      {grid.status === "rejected" && <p className="text-sm text-fg-muted">{t.rejectedHint}</p>}
      {!editable && !grid.is_locked && !grid.week_closed && (
        <p className="text-sm text-fg-muted">{t.lockedHint}</p>
      )}
      {notice && (
        <p role="status" className="rounded bg-success-bg p-3 text-sm text-success">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="rounded bg-danger-bg p-3 text-sm text-danger">
          {error}
        </p>
      )}
      {grid.over_budget && (
        <p className="rounded bg-warning-bg p-3 text-sm text-warning">
          {/* Icon + word + colour: a colour alone is not a warning (§5). */}
          <span aria-hidden="true">⚠ </span>
          {t.overBudget}
        </p>
      )}

      <div className="overflow-x-auto">
        {/*
          The grid's own hook, so a check can address it without depending on the
          language of its caption. The submission-history table below is inside the same
          section, so "the table in the grid's section" is two elements.
        */}
        <table data-testid="timesheet-grid" className="w-full border-collapse text-sm">
          <caption className="sr-only">{t.caption}</caption>
          <thead>
            <tr className="border-b border-border text-left">
              {grid.days.map((day) => (
                <th
                  key={day.entry_date}
                  id={`day-${day.entry_date}`}
                  scope="col"
                  className="px-2 py-2 align-top font-medium"
                >
                  <span className="block">{weekdayName(day, locale)}</span>
                  <span className="tabular block text-xs font-normal text-fg-subtle">
                    {formatDate(day.entry_date, locale)}
                  </span>
                  <span className="block text-xs font-normal text-fg-subtle">
                    {expectationLabel(day, dict, locale)}
                  </span>
                  {day.is_holiday && (
                    <span className="mt-1 inline-block rounded bg-neutral-bg px-1.5 text-xs font-normal text-neutral">
                      {t.holiday}
                    </span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.days.map((day) => (
              <DayGroup
                key={day.entry_date}
                dict={dict}
                locale={locale}
                day={day}
                week={week}
                editable={editable}
                editableSheetId={editableSheetId}
                pending={pending}
                editing={editing}
                over={overByDate.get(day.entry_date)}
                remember={remember}
                onOpen={open}
                onClose={close}
                onRun={run}
              />
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-border font-medium">
              <th scope="row" className="px-2 py-3 text-left">
                {t.weekTotal}
              </th>
              <td colSpan={6} className="tabular px-2 py-3">
                {formatDuration(grid.entries_total_minutes, locale)}
                {grid.reversal_total_minutes > 0 && (
                  <span className="ml-3 font-normal text-fg-subtle">
                    {t.adjustments.gross}: {formatDuration(grid.gross_total_minutes, locale)} −{" "}
                    {t.adjustments.reversed}:{" "}
                    {formatDuration(grid.reversal_total_minutes, locale)}
                  </span>
                )}
                {grid.expected_total_minutes !== null && (
                  <span className="ml-3 font-normal text-fg-subtle">
                    {t.weekExpected}: {formatDuration(grid.expected_total_minutes, locale)}
                  </span>
                )}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <WeekActions
        dict={dict}
        locale={locale}
        week={week}
        grid={grid}
        status={status}
        pending={pending}
        correcting={correcting}
        onCorrect={() => setCorrecting((open) => !open)}
        onRun={run}
      />

      {correcting && (
        <SupplementForm
          dict={dict}
          locale={locale}
          week={week}
          grid={grid}
          pending={pending}
          onCancel={() => setCorrecting(false)}
          onRun={run}
        />
      )}

      <TaskNetTable
        dict={dict}
        locale={locale}
        tasks={grid.tasks}
        entries={grid.days.flatMap((day) => day.entries)}
      />
    </section>
  );
}

/**
 * One day: its entries, then the day's total and its "add" control.
 *
 * A row per day rather than a cell per day, because a day holds as many entries as there
 * is work — the grid's columns are the seven days and a day's work reads down its own
 * row. The `headers` attribute on each cell is what makes a screen reader announce
 * "Wednesday, 11/03/2026, 8 h expected" rather than an unlabelled button.
 */
function DayGroup({
  dict,
  locale,
  day,
  week,
  editable,
  editableSheetId,
  pending,
  editing,
  over,
  remember,
  onOpen,
  onClose,
  onRun,
}: {
  dict: Dictionary;
  locale: Locale;
  day: TimesheetDay;
  week: string;
  editable: boolean;
  editableSheetId: string | null;
  pending: boolean;
  editing: Editing | null;
  over: OverBudgetDay | undefined;
  remember: (day: string, entryId: string | null, node: HTMLElement | null) => void;
  onOpen: (day: string, entry: TimesheetEntry | null) => void;
  onClose: (day: string, entry: TimesheetEntry | null) => void;
  onRun: (action: () => Promise<TimesheetWeek>, done?: string) => Promise<void>;
}) {
  const t = dict.timesheets;
  const adding = editing?.mode === "add" && editing.day === day.entry_date;

  return (
    <>
      {day.entries.map((entry) => {
        const isEditing = editing?.mode === "edit" && editing.entry.id === entry.id;
        // A row is writable when it belongs to the week's open sheet. A locked
        // original's rows are drawn read-only beside an open correction's, and an
        // adjustment line is never editable — it is one half of a pair.
        const rowEditable =
          editable && entry.entry_type !== "reversal" && entry.timesheet_id === editableSheetId;
        return (
          <tr
            key={entry.id}
            data-entry-type={entry.entry_type}
            className={
              entry.entry_type === "reversal"
                ? "border-b border-border bg-neutral-bg align-top"
                : "border-b border-border align-top"
            }
          >
            <td headers={`day-${day.entry_date}`} className="px-2 py-2">
              {isEditing ? (
                <EntryForm
                  dict={dict}
                  locale={locale}
                  day={day.entry_date}
                  week={week}
                  entry={entry}
                  pending={pending}
                  onCancel={() => onClose(day.entry_date, entry)}
                  onRun={onRun}
                />
              ) : (
                <div className="flex flex-wrap items-start gap-x-2 gap-y-1">
                  <button
                    type="button"
                    ref={(node) => remember(day.entry_date, entry.id, node)}
                    onClick={() => onOpen(day.entry_date, entry)}
                    disabled={!rowEditable || pending}
                    aria-label={`${taskLabel(entry, locale)}, ${formatDuration(entry.minutes, locale)}`}
                    className="tabular min-h-9 rounded px-2 text-left hover:bg-neutral-bg disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <span className="font-medium">{formatDuration(entry.minutes, locale)}</span>
                    <span className="ml-2">{taskLabel(entry, locale)}</span>
                    {/* An adjustment is a word as well as a colour and a minus sign: a
                        reader has to be able to tell a correction from hours typed in,
                        and colour alone could not say it (§5). */}
                    {entry.entry_type === "reversal" && (
                      <span className="ml-2 rounded bg-warning-bg px-1.5 text-xs text-warning">
                        <span aria-hidden="true">↩ </span>
                        {t.adjustments.reversalRow}
                      </span>
                    )}
                    {/* Billability is a fact about the row, so it is a word: colour alone
                        could not say it (§5). */}
                    {entry.is_billable && (
                      <span className="ml-2 rounded bg-success-bg px-1.5 text-xs text-success">
                        {t.billable}
                      </span>
                    )}
                  </button>
                  {entry.note && <span className="py-2 text-fg-subtle">{entry.note}</span>}
                  {rowEditable && (
                    <button
                      type="button"
                      onClick={() => onRun(() => removeEntry(week, entry.id))}
                      disabled={pending}
                      aria-label={`${t.removeEntry}: ${taskLabel(entry, locale)}`}
                      className="min-h-9 rounded px-2 text-sm text-fg-subtle hover:bg-neutral-bg hover:text-danger disabled:opacity-50"
                    >
                      ×
                    </button>
                  )}
                </div>
              )}
            </td>
            {/* The other six days are empty for this row: the grid's shape is the
                columns, and a day's work is its own row. */}
            {day.entries.length > 0 && <td colSpan={6} />}
          </tr>
        );
      })}

      <tr
        data-day-total={day.entry_date}
        className={
          over
            ? "border-b border-border bg-warning-bg align-top"
            : "border-b border-border align-top"
        }
      >
        <td headers={`day-${day.entry_date}`} className="px-2 py-2">
          {adding ? (
            <EntryForm
              dict={dict}
              locale={locale}
              day={day.entry_date}
              week={week}
              entry={null}
              pending={pending}
              onCancel={() => onClose(day.entry_date, null)}
              onRun={onRun}
            />
          ) : (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="tabular font-medium">
                {t.dayTotal}: {formatDuration(day.total_minutes, locale)}
              </span>
              {/* A corrected day says what it was reached from: "8 h" and
                  "registrado 13 h − ajustado 8 h" are different statements, and only
                  one of them shows the correction that produced the figure. */}
              {day.reversal_minutes > 0 && (
                <span className="tabular text-fg-subtle">
                  <span aria-hidden="true">↩ </span>
                  {t.adjustments.gross}: {formatDuration(day.gross_minutes, locale)} −{" "}
                  {t.adjustments.reversed}: {formatDuration(day.reversal_minutes, locale)}
                </span>
              )}
              {over && (
                <span className="text-warning">
                  <span aria-hidden="true">⚠ </span>
                  {t.overBudgetDay.replace(
                    "{duration}",
                    formatDuration(over.over_minutes, locale),
                  )}
                </span>
              )}
              {editable && (
                <button
                  type="button"
                  ref={(node) => remember(day.entry_date, null, node)}
                  onClick={() => onOpen(day.entry_date, null)}
                  disabled={pending}
                  aria-label={`${t.addEntry}: ${weekdayName(day, locale)} ${formatDate(day.entry_date, locale)}`}
                  className="min-h-9 rounded px-2 text-sm text-primary hover:bg-neutral-bg disabled:opacity-50"
                >
                  + {t.addEntry}
                </button>
              )}
            </div>
          )}
        </td>
        <td colSpan={6} />
      </tr>
    </>
  );
}

/**
 * The cell editor.
 *
 * A native `<form>`, so Enter submits and Escape closes without a keyboard handler of
 * its own, and every field carries a `<label>`. The project list comes from
 * `/projects/selectable`, which applies the kernel's own answer, so the picker cannot
 * offer a project the write would then refuse.
 */
function EntryForm({
  dict,
  locale,
  day,
  week,
  entry,
  pending,
  onCancel,
  onRun,
}: {
  dict: Dictionary;
  locale: Locale;
  day: string;
  week: string;
  entry: TimesheetEntry | null;
  pending: boolean;
  onCancel: () => void;
  onRun: (action: () => Promise<TimesheetWeek>, done?: string) => Promise<void>;
}) {
  const t = dict.timesheets.entryForm;
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [tasks, setTasks] = useState<TaskOption[]>([]);
  const [projectId, setProjectId] = useState(entry?.project_id ?? "");
  const [taskId, setTaskId] = useState(entry?.task_id ?? "");
  const [minutes, setMinutes] = useState(entry ? String(entry.minutes) : "");
  const [note, setNote] = useState(entry?.note ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const minutesRef = useRef<HTMLInputElement>(null);

  /**
   * Take the focus when the editor opens.
   *
   * The cell that was activated is a `<button>` inside a `<td>`, and opening replaces
   * that button with this form — a different row of the table. A focused element that is
   * removed sends the focus to `<body>`, and from there Escape reaches nothing and the
   * next Tab restarts at the top of the page, which is a keyboard trap in reverse. So
   * the editor claims the focus itself, on the field a person wants to type in first.
   */
  useEffect(() => {
    minutesRef.current?.focus();
    minutesRef.current?.select();
  }, []);

  useEffect(() => {
    let live = true;
    selectableProjects()
      .then((page) => {
        if (live) setProjects(page.items);
      })
      .catch(() => {
        if (live) setProblem(dict.timesheets.error);
      });
    return () => {
      live = false;
    };
  }, [dict.timesheets.error]);

  useEffect(() => {
    let live = true;
    if (!projectId) {
      setTasks([]);
      return () => {
        live = false;
      };
    }
    projectTasks(projectId)
      .then((detail) => {
        if (live) setTasks(detail.tasks.filter((task) => task.is_active));
      })
      .catch(() => {
        if (live) setTasks([]);
      });
    return () => {
      live = false;
    };
  }, [projectId]);

  function save() {
    const parsed = Number(minutes);
    if (!projectId) return setProblem(t.projectRequired);
    if (!taskId) return setProblem(t.taskRequired);
    if (!minutes.trim()) return setProblem(t.minutesRequired);
    if (!Number.isInteger(parsed) || parsed <= 0 || parsed > 1440) {
      return setProblem(t.minutesRange);
    }

    setProblem(null);
    const target = entry;
    void onRun(() =>
      target
        ? updateEntry(week, target.id, { minutes: parsed, note: note.trim() || null })
        : addEntry(week, {
            entry_date: day,
            project_id: projectId,
            task_id: taskId,
            minutes: parsed,
            note: note.trim() || null,
          }),
    );
  }

  return (
    <form
      ref={formRef}
      id={`entry-form-${day}`}
      aria-label={t.heading.replace("{date}", formatDate(day, locale))}
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          onCancel();
        }
      }}
      className="flex flex-col gap-2 rounded border border-border bg-surface p-3"
    >
      <p className="text-xs font-medium text-fg-subtle">
        {t.heading.replace("{date}", formatDate(day, locale))}
      </p>

      <label className="flex flex-col gap-1 text-xs">
        {t.project}
        <select
          value={projectId}
          onChange={(event) => {
            setProjectId(event.target.value);
            setTaskId("");
          }}
          className="min-h-9 rounded border border-border bg-surface px-2 text-sm"
        >
          <option value="">{t.projectPlaceholder}</option>
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.code} · {locale === "es" ? project.name_es : project.name_en}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-xs">
        {t.task}
        <select
          value={taskId}
          onChange={(event) => setTaskId(event.target.value)}
          disabled={!projectId}
          className="min-h-9 rounded border border-border bg-surface px-2 text-sm disabled:bg-neutral-bg"
        >
          <option value="">{t.taskPlaceholder}</option>
          {tasks.map((task) => (
            <option key={task.id} value={task.id}>
              {task.code} · {locale === "es" ? task.name_es : task.name_en}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-xs">
        {t.minutes}
        <input
          ref={minutesRef}
          type="number"
          inputMode="numeric"
          min={1}
          max={1440}
          value={minutes}
          onChange={(event) => setMinutes(event.target.value)}
          aria-describedby={`entry-minutes-hint-${day}`}
          className="tabular min-h-9 w-24 rounded border border-border bg-surface px-2 text-sm"
        />
        <span id={`entry-minutes-hint-${day}`} className="text-fg-subtle">
          {dict.timesheets.minutesHint}
        </span>
      </label>

      <label className="flex flex-col gap-1 text-xs">
        {t.note}
        <input
          type="text"
          value={note}
          maxLength={500}
          placeholder={t.notePlaceholder}
          onChange={(event) => setNote(event.target.value)}
          className="min-h-9 rounded border border-border bg-surface px-2 text-sm"
        />
      </label>

      {problem && (
        <p role="alert" className="text-xs text-danger">
          {problem}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={pending}
          className="min-h-9 rounded bg-primary px-3 text-sm font-medium text-primary-fg disabled:opacity-50"
        >
          {pending ? dict.timesheets.saving : t.add}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="min-h-9 rounded border border-border px-3 text-sm hover:bg-neutral-bg"
        >
          {t.cancel}
        </button>
      </div>
    </form>
  );
}

/**
 * The week's actions: file it, fill it from the week before, or correct a locked one.
 *
 * A correction being filled in makes the *week* editable again, so the submit button
 * files the correction rather than the original — which is the same two-level approval
 * over a second document, and the reason no separate "submit the correction" route
 * exists. The offer to correct is drawn only when the server says the week is inside
 * its window (`can_supplement`), so the screen and the refusal cannot disagree.
 */
function WeekActions({
  dict,
  locale,
  week,
  grid,
  status,
  pending,
  correcting,
  onCorrect,
  onRun,
}: {
  dict: Dictionary;
  locale: Locale;
  week: string;
  grid: TimesheetWeek;
  status: TimesheetStatusRead | null;
  pending: boolean;
  correcting: boolean;
  onCorrect: () => void;
  onRun: (action: () => Promise<TimesheetWeek>, done?: string) => Promise<void>;
}) {
  const t = dict.timesheets;
  const empty = grid.entries_total_minutes === 0;
  const inFlight = grid.supplements.filter(
    (sheet) => sheet.status === "draft" || sheet.status === "pending",
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => onRun(() => submitWeek(week))}
          disabled={!grid.is_editable || empty || pending}
          className="min-h-11 rounded bg-primary px-4 font-medium text-primary-fg disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t.submit}
        </button>
        <button
          type="button"
          onClick={() => onRun(() => copyPreviousWeek(week))}
          disabled={!grid.is_editable || !empty || pending}
          className="min-h-11 rounded border border-border bg-surface px-4 hover:bg-neutral-bg disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t.copyPrevious}
        </button>
        {grid.can_supplement && (
          <button
            type="button"
            onClick={onCorrect}
            aria-expanded={correcting}
            className="min-h-11 rounded border border-border bg-surface px-4 hover:bg-neutral-bg"
          >
            {t.supplement.open}
          </button>
        )}
      </div>

      {inFlight.map((sheet) => (
        <p key={sheet.timesheet_id} className="text-sm text-fg-muted">
          {t.supplement.inFlight.replace("{status}", t.status[sheet.status])}{" "}
          {t.supplement.inFlightHint}
        </p>
      ))}

      <SheetList dict={dict} locale={locale} grid={grid} />
      <SubmissionHistory dict={dict} locale={locale} status={status} />
    </div>
  );
}

/**
 * Which sheets this week has, and what each one corrects.
 *
 * The link the ticket asks to be readable both ways: the week lists the corrections
 * filed against it, and each correction names the original it adjusts — which is the
 * only way a reader can tell "the week" from "the document that changed it" once a
 * locked original and an open correction share one grid.
 */
function SheetList({
  dict,
  locale,
  grid,
}: {
  dict: Dictionary;
  locale: Locale;
  grid: TimesheetWeek;
}) {
  const t = dict.timesheets.supplement;
  if (grid.supplements.length === 0) return null;

  return (
    <section aria-labelledby="timesheet-sheets-heading" className="flex flex-col gap-2">
      <h3 id="timesheet-sheets-heading" className="text-base font-semibold">
        {t.sheetsHeading}
      </h3>
      <ul className="flex flex-col gap-1 text-sm">
        {grid.sheets.map((sheet) => (
          <li key={sheet.timesheet_id} className="flex flex-wrap items-center gap-x-2">
            <span className="font-medium">
              {sheet.corrects_timesheet_id ? t.sheetSupplement : t.sheetOriginal}
            </span>
            <span className="rounded bg-neutral-bg px-1.5 text-xs text-neutral">
              {dict.timesheets.status[sheet.status]}
            </span>
            {sheet.corrects_timesheet_id && (
              <span className="text-fg-subtle">
                {t.linkOriginal.replace("{date}", formatDate(sheet.week_start, locale))}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * The correction's form: one line per locked entry, with the corrected minutes.
 *
 * It posts *all* the changes in one request, because they are one document and one
 * approval round — a per-row write would leave a correction half-applied if the second
 * request failed. The check box is the "this should not exist" case: a reversal with no
 * replacement, which is what cancelling an entry outright means.
 */
function SupplementForm({
  dict,
  locale,
  week,
  grid,
  pending,
  onCancel,
  onRun,
}: {
  dict: Dictionary;
  locale: Locale;
  week: string;
  grid: TimesheetWeek;
  pending: boolean;
  onCancel: () => void;
  onRun: (action: () => Promise<TimesheetWeek>, done?: string) => Promise<void>;
}) {
  const t = dict.timesheets.supplement;
  const originals = grid.days
    .flatMap((day) => day.entries)
    .filter((entry) => entry.entry_type === "normal" && !entry.reverses_entry_id);
  const [draft, setDraft] = useState<Record<string, { minutes: string; remove: boolean }>>(
    () =>
      Object.fromEntries(
        originals.map((entry) => [entry.id, { minutes: String(entry.minutes), remove: false }]),
      ),
  );
  const [problem, setProblem] = useState<string | null>(null);

  function change(entryId: string, next: { minutes?: string; remove?: boolean }) {
    setDraft((current) => ({ ...current, [entryId]: { ...current[entryId], ...next } }));
  }

  function send() {
    const corrections: Correction[] = [];
    for (const entry of originals) {
      const row = draft[entry.id];
      if (!row) continue;
      if (row.remove) {
        // A reversal with no replacement: the entry should not have been recorded.
        corrections.push({ entry_id: entry.id, minutes: null });
        continue;
      }
      const minutes = Number(row.minutes);
      if (!Number.isInteger(minutes) || minutes <= 0 || minutes > 1440) {
        return setProblem(t.invalidMinutes);
      }
      // Only what actually changed: a correction restates one entry, and sending the
      // untouched rows would write reversals and replacements that cancel out — noise
      // in a document two people have to read.
      if (minutes !== entry.minutes) {
        corrections.push({ entry_id: entry.id, minutes });
      }
    }
    if (corrections.length === 0) return setProblem(t.nothingChanged);
    setProblem(null);
    void onRun(() => openSupplement(week, corrections), t.opened);
  }

  return (
    <form
      aria-labelledby="timesheet-supplement-heading"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
      className="flex flex-col gap-3 rounded border border-border bg-surface p-4"
    >
      <h3 id="timesheet-supplement-heading" className="text-base font-semibold">
        {t.heading}
      </h3>
      <p className="text-sm text-fg-muted">{t.intro}</p>

      <fieldset className="flex flex-col gap-3">
        <legend className="text-sm font-medium">{t.heading}</legend>
        {originals.map((entry) => (
          <div key={entry.id} className="flex flex-wrap items-end gap-x-4 gap-y-2">
            <span className="text-sm">
              <span className="font-medium">
                {t.entryLabel.replace("{date}", formatDate(entry.entry_date, locale))}
              </span>
              <span className="ml-2 text-fg-subtle">
                {taskLabel(entry, locale)} · {formatDuration(entry.minutes, locale)}
              </span>
            </span>
            <label className="flex items-center gap-1 text-sm">
              <input
                type="checkbox"
                checked={draft[entry.id]?.remove ?? false}
                onChange={(event) => change(entry.id, { remove: event.target.checked })}
                className="size-4"
              />
              {t.remove}
            </label>
            <label className="flex items-center gap-2 text-sm">
              {t.newMinutes}
              <input
                type="number"
                inputMode="numeric"
                min={1}
                max={1440}
                value={draft[entry.id]?.minutes ?? ""}
                disabled={draft[entry.id]?.remove ?? false}
                onChange={(event) => change(entry.id, { minutes: event.target.value })}
                className="tabular min-h-9 w-24 rounded border border-border bg-surface px-2 text-sm disabled:bg-neutral-bg"
              />
            </label>
          </div>
        ))}
      </fieldset>
      <p className="text-xs text-fg-subtle">{t.removeHint}</p>

      {problem && (
        <p role="alert" className="text-sm text-danger">
          {problem}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={pending}
          className="min-h-9 rounded bg-primary px-3 text-sm font-medium text-primary-fg disabled:opacity-50"
        >
          {pending ? t.submitting : t.submit}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="min-h-9 rounded border border-border px-3 text-sm hover:bg-neutral-bg"
        >
          {t.cancel}
        </button>
      </div>
    </form>
  );
}

/**
 * The net per task, which is the half of a corrected week a day total cannot show.
 *
 * A day can net to zero while two tasks moved in opposite directions; the table only
 * appears once something has been adjusted, because a list of gross figures that all
 * equal their net is a table of nothing. The names come from the week's own rows, so
 * the table says what a reader is looking at rather than printing an id.
 */
function TaskNetTable({
  dict,
  locale,
  tasks,
  entries,
}: {
  dict: Dictionary;
  locale: Locale;
  tasks: TaskNet[];
  entries: TimesheetEntry[];
}) {
  const t = dict.timesheets;
  const adjusted = tasks.some((task) => task.reversal_minutes > 0);
  if (!adjusted) return null;

  const named = new Map(entries.map((entry) => [entry.task_id, entry]));
  return (
    <section aria-labelledby="timesheet-tasks-heading" className="flex flex-col gap-2">
      <h3 id="timesheet-tasks-heading" className="text-base font-semibold">
        {t.tasks.heading}
      </h3>
      <div className="overflow-x-auto">
        <table data-testid="timesheet-task-net" className="w-full border-collapse text-sm">
          <caption className="sr-only">{t.tasks.heading}</caption>
          <thead>
            <tr className="border-b border-border text-left text-fg-muted">
              <th scope="col" className="px-2 py-2 font-medium">
                {t.adjustments.task}
              </th>
              <th scope="col" className="px-2 py-2 font-medium">
                {t.adjustments.gross}
              </th>
              <th scope="col" className="px-2 py-2 font-medium">
                {t.adjustments.reversed}
              </th>
              <th scope="col" className="px-2 py-2 font-medium">
                {t.adjustments.net}
              </th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => {
              const entry = named.get(task.task_id);
              return (
                <tr
                  key={`${task.project_id}-${task.task_id}`}
                  className="border-b border-border last:border-0"
                >
                  <td className="px-2 py-2">
                    {entry ? taskLabel(entry, locale) : task.task_id.slice(0, 8)}
                  </td>
                  <td className="tabular px-2 py-2">
                    {formatDuration(task.gross_minutes, locale)}
                  </td>
                  <td className="tabular px-2 py-2">
                    {formatDuration(task.reversal_minutes, locale)}
                  </td>
                  <td className="tabular px-2 py-2">
                    {formatDuration(task.net_minutes, locale)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/**
 * The lock, said in words beside the status (ticket 29).
 *
 * Three statements and only one of them is true of a given week: it is locked and can
 * still be corrected, it is locked and the window has closed, or it is locked with some
 * weeks left — the count is the server's, interpolated, so the sentence a person reads
 * and the number the refusal would name are the same number.
 */
function LockNotice({ dict, grid }: { dict: Dictionary; grid: TimesheetWeek }) {
  const t = dict.timesheets.lock;
  if (!grid.is_locked && !grid.week_closed) return null;

  return (
    <div
      data-testid="timesheet-lock-notice"
      className={
        grid.week_closed
          ? "flex flex-col gap-1 rounded bg-neutral-bg p-3 text-sm text-neutral"
          : "flex flex-col gap-1 rounded bg-info-bg p-3 text-sm text-info"
      }
    >
      <p>
        <span aria-hidden="true">{grid.week_closed ? "🔒 " : "✓ "}</span>
        <span className="font-medium">{grid.week_closed ? t.closed : t.locked}</span>{" "}
        {grid.week_closed
          ? t.closedHint.replace("{weeks}", String(grid.supplement_window_weeks))
          : t.lockedHint}
      </p>
      {grid.is_locked && !grid.week_closed && grid.supplement_weeks_left > 0 && (
        <p className="text-fg-muted">
          {t.supplementWindow.replace("{weeks}", String(grid.supplement_weeks_left))}
        </p>
      )}
    </div>
  );
}

/** The lock, as an icon and a word: a colour alone never carries a state (§5). */
function LockPill({ dict }: { dict: Dictionary }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded bg-info-bg px-2 py-1 text-sm text-info">
      <span aria-hidden="true">🔒</span>
      {dict.timesheets.lock.locked}
    </span>
  );
}

/**
 * The submission history: the engine's own record, round by round.
 *
 * A week that was returned, corrected and refiled shows both attempts, which is the
 * 历史提交记录 the ticket asks to keep. Nothing is recomputed here — this renders what
 * `ApprovalState` already holds.
 */
function SubmissionHistory({
  dict,
  locale,
  status,
}: {
  dict: Dictionary;
  locale: Locale;
  status: TimesheetStatusRead | null;
}) {
  const t = dict.timesheets.history;
  const decisions = status?.approval?.decisions ?? [];

  return (
    <section aria-labelledby="timesheet-history-heading" className="flex flex-col gap-2">
      <h3 id="timesheet-history-heading" className="text-base font-semibold">
        {t.heading}
      </h3>
      {decisions.length === 0 ? (
        <p className="text-sm text-fg-subtle">{t.empty}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-border text-left text-fg-muted">
                <th scope="col" className="px-2 py-2 font-medium">
                  {t.round}
                </th>
                <th scope="col" className="px-2 py-2 font-medium">
                  {t.level}
                </th>
                <th scope="col" className="px-2 py-2 font-medium">
                  {t.decision}
                </th>
                <th scope="col" className="px-2 py-2 font-medium">
                  {t.comment}
                </th>
                <th scope="col" className="px-2 py-2 font-medium">
                  {t.decidedAt}
                </th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((decision, index) => (
                <tr
                  key={`${decision.round}-${decision.level}-${index}`}
                  className="border-b border-border last:border-0"
                >
                  <td className="tabular px-2 py-2">{formatNumber(decision.round, locale)}</td>
                  <td className="tabular px-2 py-2">{formatNumber(decision.level, locale)}</td>
                  <td className="px-2 py-2">{t.decisions[decision.decision]}</td>
                  <td className="px-2 py-2">{decision.comment ?? "—"}</td>
                  <td className="tabular px-2 py-2">
                    {formatDate(decision.decided_at, locale)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** The status, as an icon, a word and a colour — never the colour alone (§5). */
export function StatusPill({
  dict,
  status,
}: {
  dict: Dictionary;
  status: TimesheetWeek["status"];
}) {
  const t = dict.timesheets;
  const look: Record<
    TimesheetWeek["status"],
    { className: string; icon: string; label: string }
  > = {
    draft: { className: "bg-neutral-bg text-neutral", icon: "✎", label: t.status.draft },
    pending: { className: "bg-info-bg text-info", icon: "⏳", label: t.status.pending },
    approved: { className: "bg-success-bg text-success", icon: "✓", label: t.status.approved },
    rejected: { className: "bg-warning-bg text-warning", icon: "↩", label: t.status.rejected },
  };
  const shown = look[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded px-2 py-1 text-sm ${shown.className}`}
    >
      <span aria-hidden="true">{shown.icon}</span>
      {shown.label}
    </span>
  );
}

/**
 * The day's name, from `Intl` rather than a hardcoded list.
 *
 * `parseDay` builds a *local* date, so the weekday cannot slip a day in a browser west of
 * Greenwich — the same reason this module never writes `new Date("2026-03-09")`.
 */
function weekdayName(day: TimesheetDay, locale: Locale): string {
  return new Intl.DateTimeFormat(locale === "es" ? "es-ES" : "en-GB", {
    weekday: "long",
  }).format(parseDay(day.entry_date));
}

function expectationLabel(day: TimesheetDay, dict: Dictionary, locale: Locale): string {
  if (day.expected_minutes === null) return dict.timesheets.expectedUnknown;
  return dict.timesheets.expected.replace(
    "{duration}",
    formatDuration(day.expected_minutes, locale),
  );
}

function taskLabel(entry: TimesheetEntry, locale: Locale): string {
  const name = locale === "es" ? entry.task_name_es : entry.task_name_en;
  return [entry.project_code, entry.task_code, name].filter(Boolean).join(" · ");
}

/**
 * The refusal in the reader's language, from the catalogue key when it is known.
 *
 * The one refusal that carries a number is the closed window, and the number is the
 * *server's*: the detail states how many weeks remain, and the sentence interpolates
 * it — the same shape the sign-in screen uses for a lockout's remaining time. A
 * hard-coded count in the copy would be a second answer to "how long is the window",
 * and the two would eventually disagree.
 */
function errorText(dict: Dictionary, cause: unknown, windowWeeks: number): string {
  if (cause instanceof ApiError && cause.messageKey) {
    const known = cause.messageKey.replace(/^errors\./, "") as keyof Dictionary["errors"];
    if (known in dict.errors) {
      const message = dict.errors[known];
      return known === "timesheet_week_closed"
        ? message.replace("{weeks}", String(windowWeeks))
        : message;
    }
  }
  return dict.timesheets.error;
}

/**
 * The cell key the grid focuses by, named so the keyboard test can address a cell.
 */
export function cellKey(day: string, entryId: string | null): string {
  return `${day}:${entryId ?? "new"}`;
}
