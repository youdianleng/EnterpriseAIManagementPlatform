import { useId } from "react";

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
  type?: "text" | "email" | "password" | "number" | "date" | "search";
  placeholder?: string;
  autoComplete?: string;
  inputMode?: "text" | "numeric" | "decimal" | "tel";
  name?: string;
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
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        autoComplete={autoComplete}
        inputMode={inputMode}
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
