import { useId, type RefObject } from "react";

import { cn } from "@/lib/ui/cn";

type CommonProps = {
  label: string;
  /** Validation message; presence also marks the control invalid. */
  error?: string;
  hint?: string;
  required?: boolean;
};

const CONTROL_CLASSES =
  "w-full rounded border border-border bg-surface px-3 py-2 text-fg placeholder:text-fg-subtle " +
  "focus-visible:border-primary disabled:cursor-not-allowed disabled:bg-neutral-bg aria-[invalid=true]:border-danger";

export type TextFieldProps = CommonProps & {
  value: string;
  onChange: (value: string) => void;
  /**
   * `time` is on the list because a punch is corrected *to a time of day*: the date is
   * the day the document names and the field that is wrong is the hour, so a native time
   * control is the one a correction form actually needs.
   */
  type?: "text" | "email" | "password" | "number" | "date" | "time" | "search";
  placeholder?: string;
  autoComplete?: string;
  inputMode?: "text" | "numeric" | "decimal" | "tel";
  name?: string;
  /** Bounds the browser enforces for `type="date"` / `type="time"`, in the value's format. */
  min?: string;
  max?: string;
  /**
   * The input itself, for the one case a caller cannot express declaratively: taking the
   * focus. An inline editor that appears after a click must put the caret in the field, or
   * a keyboard user has to Tab back to where the click was — and the focus would be left on
   * a button that no longer exists.
   */
  inputRef?: RefObject<HTMLInputElement | null>;
};

/**
 * Labelled text input.
 *
 * The id/aria wiring is the component's job, not the caller's: a caller that
 * forgets `htmlFor` produces an unlabelled field that screen readers announce
 * as blank, and that is the most common form accessibility defect.
 */
export function TextField({
  label,
  error,
  hint,
  required,
  value,
  onChange,
  type = "text",
  placeholder,
  autoComplete,
  inputMode,
  name,
  min,
  max,
  inputRef,
}: TextFieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ");

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
        {required && (
          <span aria-hidden="true" className="ml-1 text-danger">
            *
          </span>
        )}
      </label>
      <input
        id={id}
        name={name}
        type={type}
        ref={inputRef}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        autoComplete={autoComplete}
        inputMode={inputMode}
        min={min}
        max={max}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className={cn(CONTROL_CLASSES, error && "border-danger")}
      />
      {hint && !error && (
        <p id={hintId} className="text-sm text-fg-subtle">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

export type SelectFieldProps = CommonProps & {
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  placeholder?: string;
  name?: string;
};

export function SelectField({
  label,
  error,
  hint,
  required,
  value,
  onChange,
  options,
  placeholder,
  name,
}: SelectFieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ");

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
        {required && (
          <span aria-hidden="true" className="ml-1 text-danger">
            *
          </span>
        )}
      </label>
      <select
        id={id}
        name={name}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className={cn(CONTROL_CLASSES, "pr-8", error && "border-danger")}
      >
        {placeholder && <option value="">{placeholder}</option>}
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {hint && !error && (
        <p id={hintId} className="text-sm text-fg-subtle">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

export type TextAreaFieldProps = CommonProps & {
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  placeholder?: string;
  name?: string;
  /** The ceiling the API enforces, so a refusal is predicted rather than discovered. */
  maxLength?: number;
};

/**
 * Labelled multi-line input.
 *
 * A correction is refused without a reason, and a rejection is refused without a comment,
 * so the free text this system needs is a *paragraph* rather than a line: a one-line
 * input would clip "salí a las 15:00 por una urgencia" into a box the reader cannot see
 * the end of. It carries the same label/error/hint wiring as the other controls, because
 * a textarea that forgets `htmlFor` is the same defect as an input that does.
 */
export function TextAreaField({
  label,
  error,
  hint,
  required,
  value,
  onChange,
  rows = 3,
  placeholder,
  name,
  maxLength,
}: TextAreaFieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ");

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
        {required && (
          <span aria-hidden="true" className="ml-1 text-danger">
            *
          </span>
        )}
      </label>
      <textarea
        id={id}
        name={name}
        rows={rows}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        required={required}
        maxLength={maxLength}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className={cn(CONTROL_CLASSES, "resize-y", error && "border-danger")}
      />
      {hint && !error && (
        <p id={hintId} className="text-sm text-fg-subtle">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
