"use client";

import { useState } from "react";

import type { Draft, PrefillField } from "@/lib/api/answers";
import { formatDate } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { SelectField, TextAreaField, TextField } from "@/lib/ui/field";
import { cn } from "@/lib/ui/cn";

/**
 * The draft the assistant prepared, as a complete editable form (ticket 40).
 *
 * DESIGN §6.3's first requirement is 「`PrefillForm` 必须是**完整、可编辑的表单**，展示所有将
 * 写入的字段值」, and this component is that requirement in the interface. Four decisions:
 *
 * **Every field the submission will write is drawn, and every one is editable.** The list
 * comes from the server (`prefill_form.fields`), one control per `kind`, and nothing is
 * read-only except the facts beside it: the fields *are* the submission's own field names,
 * which is what `api/tests/test_agent_draft_tools.py` asserts against the endpoints'
 * request models. A person can change every value before anybody confirms anything.
 *
 * **The values are local state from the moment the card is drawn.** Editing has to survive a
 * re-render and must not touch the server: nothing here is saved, because saving is not a
 * thing this ticket has — the draft is what the assistant proposed, and the *confirmed*
 * values are ticket 41's submission. So the card holds its own copy, which is also what
 * makes the fields demonstrably editable rather than a picture of a form.
 *
 * **The expiry is stated, and an expired draft says what to do instead.** §6.3 gives a draft
 * 24 hours and requires 「过期后标记为失效并要求重新生成」: the header carries the instant the
 * draft lapses, and an `expired` one is drawn with its values *visible* (the record of what
 * was proposed) and its confirmation replaced by the sentence that asks for a new one.
 *
 * **Confirmation is deliberately not here.** §6.3's second requirement — an explicit click
 * that re-validates and submits as the employee — is ticket 41's, and a button that looked
 * like it did that while doing nothing would be worse than one that says it is not ready.
 * The button below is disabled and the note beside it says so.
 */
export function DraftForm({
  draft,
  dict,
  locale,
}: {
  draft: Draft;
  dict: Dictionary;
  locale: Locale;
}) {
  const t = dict.qa.draft;
  const form = draft.prefill_form;
  const [values, setValues] = useState<Record<string, string>>(() =>
    initialValues(form?.fields ?? []),
  );

  if (!form) return null;
  const expired = draft.status === "expired";

  return (
    <section
      aria-labelledby="qa-draft-heading"
      className={cn(
        "min-w-0 rounded-lg border bg-surface p-4 shadow-sm",
        expired ? "border-border" : "border-primary",
      )}
      data-testid="qa-draft"
      data-draft-status={draft.status}
      data-draft-entity={form.entity}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 id="qa-draft-heading" className="text-base font-semibold">
          {locale === "es" ? form.title_es : form.title_en}
        </h3>
        <span
          className={cn(
            "rounded-full border px-2 py-0.5 text-xs font-medium",
            expired ? "border-border text-fg-muted" : "border-primary text-primary",
          )}
          data-testid="qa-draft-status"
        >
          {expired ? t.expiredBadge : t.proposedBadge}
        </span>
      </div>

      <p className="mt-1 text-sm text-fg-subtle" data-testid="qa-draft-expiry">
        {expired
          ? t.expiredAt.replace("{date}", formatDate(draft.expires_at, locale))
          : t.expiresAt.replace("{date}", formatDate(draft.expires_at, locale))}
      </p>

      {expired && (
        <div data-testid="qa-draft-expired">
          <Alert tone="warning" className="mt-3" title={t.expiredTitle}>
            {t.expiredBody}
          </Alert>
        </div>
      )}

      <form
        className="mt-3 flex flex-col gap-3"
        noValidate
        onSubmit={(event) => {
          // Nothing is submitted from this screen yet — see the docstring. Preventing the
          // default keeps a stray Enter from navigating the page away from the thread.
          event.preventDefault();
        }}
      >
        {form.fields.map((field) => (
          <Field
            key={field.name}
            field={field}
            locale={locale}
            value={values[field.name] ?? ""}
            disabled={expired}
            onChange={(next) => setValues((current) => ({ ...current, [field.name]: next }))}
          />
        ))}

        {Object.keys(form.facts).length > 0 && (
          <dl
            className="grid gap-1 rounded border border-border bg-neutral-bg p-3 text-sm"
            data-testid="qa-draft-facts"
          >
            {factRows(form, locale, t).map(([label, value]) => (
              <div key={label} className="flex flex-wrap justify-between gap-2">
                <dt className="text-fg-muted">{label}</dt>
                <dd className="tabular">{value}</dd>
              </div>
            ))}
          </dl>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled data-testid="qa-draft-confirm">
            {t.confirm}
          </Button>
          <p className="text-sm text-fg-subtle" data-testid="qa-draft-confirm-note">
            {expired ? t.expiredAction : t.confirmNote}
          </p>
        </div>
      </form>
    </section>
  );
}

/** One field, drawn as the control its `kind` asks for, in the reader's language. */
function Field({
  field,
  locale,
  value,
  disabled,
  onChange,
}: {
  field: PrefillField;
  locale: Locale;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const label = locale === "es" ? field.label_es : field.label_en;
  const hint = (locale === "es" ? field.hint_es : field.hint_en) ?? undefined;
  const name = `draft-${field.name}`;

  if (field.kind === "select") {
    return (
      <SelectField
        label={label}
        hint={hint}
        required={field.required}
        disabled={disabled}
        name={name}
        value={value}
        onChange={onChange}
        options={field.options.map((option) => ({
          value: option.value,
          label: locale === "es" ? option.label_es : option.label_en,
        }))}
      />
    );
  }
  if (field.kind === "textarea") {
    return (
      <TextAreaField
        label={label}
        hint={hint}
        required={field.required}
        disabled={disabled}
        name={name}
        value={value}
        onChange={onChange}
      />
    );
  }
  return (
    <TextField
      label={label}
      hint={hint}
      required={field.required}
      disabled={disabled}
      name={name}
      value={value}
      onChange={onChange}
      // One map rather than a chain of ternaries: the kinds are a closed set
      // (`app/domain/agent/models.py::FieldKind`) and each one is exactly one control.
      type={CONTROL_TYPE[field.kind]}
      inputMode={field.kind === "number" ? "numeric" : undefined}
    />
  );
}

/** The native input each kind is drawn as. `select` and `textarea` never reach it. */
const CONTROL_TYPE: Record<PrefillField["kind"], "text" | "number" | "date" | "time"> = {
  date: "date",
  time: "time",
  number: "number",
  text: "text",
  textarea: "text",
  select: "text",
};

/**
 * The initial value of every field, as the form's own string state.
 *
 * `null` becomes the empty string rather than the text "null": a field the assistant could
 * not fill (a sick note's attachment, a note nobody wrote) is an empty box a person fills in,
 * which is what an editable draft is for.
 */
function initialValues(fields: PrefillField[]): Record<string, string> {
  const values: Record<string, string> = {};
  for (const field of fields) {
    values[field.name] = field.value === null || field.value === undefined ? "" : String(field.value);
  }
  return values;
}

/**
 * The validated facts, as label/value rows in the reader's language.
 *
 * Only the facts a person reads: the working days a leave costs, the week a time entry
 * belongs to, the project and task codes, whether the entry is billable. Everything else the
 * server sends is deliberately not drawn — `facts` is extensible, and a screen that printed
 * every key would show an identifier the moment the API added one.
 */
function factRows(
  form: NonNullable<Draft["prefill_form"]>,
  locale: Locale,
  t: Dictionary["qa"]["draft"],
): Array<[string, string]> {
  const rows: Array<[string, string]> = [];
  const facts = form.facts as Record<string, unknown>;
  if (typeof facts.business_days_count === "number") {
    rows.push([t.factWorkingDays, String(facts.business_days_count)]);
  }
  if (typeof facts.week_start === "string") {
    rows.push([t.factWeek, formatDate(facts.week_start, locale)]);
  }
  if (typeof facts.project_code === "string") {
    rows.push([t.factProject, facts.project_code]);
  }
  if (typeof facts.task_code === "string") {
    rows.push([t.factTask, facts.task_code]);
  }
  if (typeof facts.is_billable === "boolean") {
    rows.push([t.factBillable, facts.is_billable ? t.yes : t.no]);
  }
  if (typeof facts.business_date === "string") {
    rows.push([t.factDay, formatDate(facts.business_date, locale)]);
  }
  if (facts.requires_attachment === true) {
    rows.push([t.factAttachment, t.yes]);
  }
  return rows;
}
