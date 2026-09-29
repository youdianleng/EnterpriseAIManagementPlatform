"use client";

import { useState } from "react";

import type { Draft, PrefillField } from "@/lib/api/answers";
import { ApiError } from "@/lib/api/client";
import { formatDate } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { Dialog } from "@/lib/ui/dialog";
import { SelectField, TextAreaField, TextField } from "@/lib/ui/field";
import { cn } from "@/lib/ui/cn";

/**
 * The draft the assistant prepared, as a complete editable form the employee can act on.
 *
 * DESIGN §6.3's first requirement is 「`PrefillForm` 必须是**完整、可编辑的表单**」, and this
 * component is that requirement in the interface. Five decisions:
 *
 * **Every field the submission will write is drawn, and every one is editable.** The list
 * comes from the server (`prefill_form.fields`), one control per `kind`, and nothing is
 * read-only except the facts beside it: the fields *are* the submission's own field names,
 * which is what `api/tests/test_agent_draft_tools.py` asserts against the endpoints'
 * request models. A person can change every value before anybody confirms anything.
 *
 * **The values are local state, and the *confirmed* ones are what is sent.** Editing has to
 * survive a re-render, and the confirmation posts the values on screen rather than the ones
 * the assistant proposed — §6.3's first and second requirements meeting: a form nobody can
 * edit and a form whose edits are discarded are the same defect from two sides. Only the
 * keys the form has are sent, so the server's own coercion (`domain/agent/confirmation.py`)
 * is what decides what a value means.
 *
 * **The confirmation is a dialog, and the dialog says what will be created.** §6.3's second
 * requirement is 「必须是**显式按钮点击**」 and the design system's rule for a committing
 * action is a reversible-looking confirmation that states the consequence — so the button
 * opens a dialog naming the document, and the actual POST happens when *that* dialog's
 * button is pressed. Two clicks rather than one, and the second one is the ticket's whole
 * point: 「聊天里回一句"好的"不算确认」.
 *
 * **A rejection is a dialog too, and its reason is optional.** Discarding one's own draft
 * cannot collide with anything, so §6.4's "a rejection needs a reason" — which is about an
 * *approver* rejecting an employee's request — does not apply; nobody is waiting for an
 * explanation, and a required sentence before a person may clear their own screen is a form
 * for its own sake.
 *
 * **A refusal is rendered from the catalogue, not invented here.** The API answers a failed
 * confirmation with a `message_key`, and every key it can answer with exists in both
 * languages (`api/app/core/messages.py`): a lapsed draft is regenerated, a document whose
 * rules moved is regenerated, a session that ended is signed in again. This component
 * renders the reader's own sentence from `dict.errors[key]` and never composes one.
 */
export function DraftForm({
  draft,
  dict,
  locale,
  onDecide,
}: {
  draft: Draft;
  dict: Dictionary;
  locale: Locale;
  /**
   * Answer the draft. `fields` is the form as edited, and it is passed for a confirmation
   * only: a rejection discards the values, so sending them would be pretending they matter.
   */
  onDecide: (
    decision: "confirm" | "reject",
    fields?: Record<string, string | number | null>,
    reason?: string,
  ) => Promise<unknown>;
}) {
  const t = dict.qa.draft;
  const form = draft.prefill_form;
  const [values, setValues] = useState<Record<string, string>>(() =>
    initialValues(form?.fields ?? []),
  );
  const [confirming, setConfirming] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!form) return null;
  const answered = draft.status !== "proposed";
  const expired = draft.status === "expired";
  const decided = draft.status === "confirmed" || draft.status === "rejected";

  /** One failure, rendered by the catalogue: the API's `message_key` or nothing. */
  function failure(cause: unknown): string {
    const key = cause instanceof ApiError ? cause.messageKey : undefined;
    const catalogue = dict.errors as Record<string, string>;
    if (key && catalogue[key]) return catalogue[key];
    return cause instanceof ApiError ? cause.message : t.failed;
  }

  async function decide(decision: "confirm" | "reject") {
    setSending(true);
    setError(null);
    try {
      await onDecide(
        decision,
        decision === "confirm" ? Object.fromEntries(Object.entries(values)) : undefined,
        decision === "reject" ? reason.trim() || undefined : undefined,
      );
      setConfirming(false);
      setRejecting(false);
      setReason("");
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setSending(false);
    }
  }

  return (
    <section
      aria-labelledby="qa-draft-heading"
      className={cn(
        "min-w-0 rounded-lg border bg-surface p-4 shadow-sm",
        answered ? "border-border" : "border-primary",
      )}
      data-testid="qa-draft"
      data-draft-status={draft.status}
      data-draft-entity={form.entity}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 id="qa-draft-heading" className="text-base font-semibold" data-draft-title={cardTitle(draft, form, locale)}>
          {cardTitle(draft, form, locale)}
        </h3>
        {/* §5's 「状态绝不只靠颜色」: the badge is a *word* first — and it takes a semantic
            tone once it is a *status* rather than a prompt. The first version kept the flat
            border-and-grey treatment for "Confirmado", which read as a muted annotation
            beside a green panel: the wording is right, the visual weight is not, and a
            status an employee has to squint at is not a status. The wording comes from the
            catalogue keys the API sends, which already have the (draft) suffix only on the
            proposed state. */}
        <span
          className={cn(
            "rounded-full border px-2 py-0.5 text-xs font-medium",
            draft.status === "confirmed"
              ? "border-success bg-success-bg text-success"
              : answered
                ? "border-border bg-neutral-bg text-fg-muted"
                : "border-primary text-primary",
          )}
          data-testid="qa-draft-status"
        >
          {statusWord(draft.status, t)}
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

      {decided && (
        <div data-testid="qa-draft-decided">
          <Alert
            tone={draft.status === "confirmed" ? "success" : "neutral"}
            className="mt-3"
            title={draft.status === "confirmed" ? t.confirmedTitle : t.rejectedTitle}
          >
            <p>{draft.status === "confirmed" ? t.confirmedBody : t.rejectedBody}</p>
            {/* The document the row recorded, linked: the employee asked for something and
                it now exists, so the card offers the way to it rather than only saying so.
                The id is the `agent_actions` row's own `resulting_entity_id`. */}
            {draft.status === "confirmed" && draft.resulting_entity_id && (
              <a
                className="mt-2 inline-block font-medium underline"
                href={`/${locale}/${entityRoute(draft.resulting_entity_type)}`}
                data-testid="qa-draft-entity-link"
              >
                {t.openDocument}
              </a>
            )}
          </Alert>
        </div>
      )}

      {error && (
        <div data-testid="qa-draft-error">
          <Alert tone="danger" role="alert" className="mt-3" title={t.failedTitle}>
            {error}
          </Alert>
        </div>
      )}

      <form
        className="mt-3 flex flex-col gap-3"
        noValidate
        onSubmit={(event) => {
          // The submit control below opens a dialog rather than posting: see the docstring.
          // Preventing the default keeps a stray Enter from navigating the thread away.
          event.preventDefault();
        }}
      >
        {form.fields.map((field) => (
          <Field
            key={field.name}
            field={field}
            locale={locale}
            value={values[field.name] ?? ""}
            disabled={answered}
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

        {/* **The buttons are gone once the draft is answered, rather than disabled.** The
            screenshot at 320px is what settled this: `disabled:opacity-50` is legible on a
            desktop and reads as "you can press this" on a phone, and a form whose own
            "Documento enviado" panel sits above an enabled-looking "Confirmar y enviar" is a
            screen telling the employee two different things. There is nothing left to do
            with this card, so there is nothing left to press; the note says why. */}
        {answered ? (
          <p className="text-sm text-fg-subtle" data-testid="qa-draft-confirm-note">
            {expired ? t.expiredAction : t.answeredNote}
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              disabled={sending}
              onClick={() => {
                setError(null);
                setConfirming(true);
              }}
              data-testid="qa-draft-confirm"
            >
              {t.confirm}
            </Button>
            <Button
              type="button"
              variant="secondary"
              disabled={sending}
              onClick={() => {
                setError(null);
                setRejecting(true);
              }}
              data-testid="qa-draft-reject"
            >
              {t.reject}
            </Button>
            <p className="text-sm text-fg-subtle" data-testid="qa-draft-confirm-note">
              {t.confirmNote}
            </p>
          </div>
        )}
      </form>

      {/* The committing action's dialog: it names the document, and the POST happens when
          *this* button is pressed. The wording is the catalogue's, in the reader's language. */}
      <Dialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={t.confirmTitle}
        closeLabel={t.cancel}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirming(false)} disabled={sending}>
              {t.cancel}
            </Button>
            <Button onClick={() => void decide("confirm")} disabled={sending}>
              {sending ? t.confirming : t.confirmAction}
            </Button>
          </>
        }
      >
        <p data-testid="qa-draft-confirm-body">
          {t.confirmBody.replace("{title}", locale === "es" ? form.title_es : form.title_en)}
        </p>        <p className="mt-2 text-sm">{t.confirmIdentity}</p>
        <ul className="mt-2 list-disc pl-5 text-sm" data-testid="qa-draft-confirm-fields">
          {form.fields.map((field) => (
            <li key={field.name}>
              {locale === "es" ? field.label_es : field.label_en}:{" "}
              <span className="tabular">{displayValue(field, values[field.name] ?? "", t)}</span>
            </li>
          ))}
        </ul>
      </Dialog>

      {/* The rejection's dialog. No reason is required: see the docstring. */}
      <Dialog
        open={rejecting}
        onClose={() => setRejecting(false)}
        title={t.rejectTitle}
        closeLabel={t.cancel}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRejecting(false)} disabled={sending}>
              {t.cancel}
            </Button>
            <Button
              variant="danger"
              onClick={() => void decide("reject")}
              disabled={sending}
              data-testid="qa-draft-reject-action"
            >
              {sending ? t.rejecting : t.rejectAction}
            </Button>
          </>
        }
      >
        <p data-testid="qa-draft-reject-body">
          {t.rejectBody.replace("{title}", locale === "es" ? form.title_es : form.title_en)}
        </p>
        <TextAreaField
          label={t.rejectReasonLabel}
          hint={t.rejectReasonHint}
          required={false}
          name="draft-reject-reason"
          value={reason}
          onChange={setReason}
        />
      </Dialog>
    </section>
  );
}

/** The badge's word. A word and not only a colour, which is §5's rule. */
function statusWord(status: Draft["status"], t: Dictionary["qa"]["draft"]): string {
  if (status === "confirmed") return t.confirmedBadge;
  if (status === "rejected") return t.rejectedBadge;
  if (status === "expired") return t.expiredBadge;
  return t.proposedBadge;
}

/**
 * The card's heading: the form's own title while it is a form, the document's once it is one.
 *
 * The API's titles carry their state in the wording — `Solicitud de permiso (borrador)` /
 * `Leave request (draft)` — because the form *is* a draft when it is offered. After the click
 * it is not: it is a leave request, and a card headed 「Solicitud de permiso (borrador)」 beside
 * a green "Documento enviado" panel tells the employee the opposite of what happened. So the
 * suffix is dropped from the title whose state has ended, which is a change of *words* and
 * not of the API's titles: the form's own title is still what the proposed state shows, and
 * `data-draft-title` carries it in every state so nothing has to parse this to check it.
 */
function cardTitle(
  draft: Draft,
  form: NonNullable<Draft["prefill_form"]>,
  locale: Locale,
): string {
  const posted = locale === "es" ? form.title_es : form.title_en;
  if (draft.status === "proposed" || draft.status === "expired") return posted;
  return posted.replace(/\s*\((borrador|draft)\)\s*$/i, "");
}

/**
 * Where the created document is listed, from the entity the audit row names.
 *
 * The three surfaces that already show these documents (tickets 24, 25 and 28), so the link
 * goes to a screen a person recognises rather than to an API path. An unknown type returns
 * the QA screen's own page segment, which is the honest fallback: the card still says what
 * happened, and the link does not pretend to know a surface that does not exist.
 */
function entityRoute(entityType: string | null): string {
  if (entityType === "leave_request") return "leave";
  if (entityType === "attendance_correction") return "attendance";
  if (entityType === "timesheet") return "timesheets";
  return "qa";
}

/**
 * One field's value, as the confirmation dialog states it.
 *
 * A blank optional field is the em-dash the catalogue carries rather than an empty gap: the
 * dialog's job is to say what will be written, and "nothing" is a thing to say.
 */
function displayValue(
  field: PrefillField,
  value: string,
  t: Dictionary["qa"]["draft"],
): string {
  if (value.trim().length === 0) return t.emptyValue;
  if (field.kind === "select") {
    const option = field.options.find((item) => item.value === value);
    return option ? option.label_en : value;
  }
  return value;
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
