type Tone = "loading" | "connected" | "failed";

const TONE_CLASSES: Record<Tone, string> = {
  loading: "bg-neutral-bg text-neutral",
  connected: "bg-success-bg text-success",
  failed: "bg-danger-bg text-danger",
};

/**
 * Status pill.
 *
 * Icon + text + colour together: state is never conveyed by colour alone.
 */
export function StatusPill({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-sm px-2 py-1 text-sm font-medium ${TONE_CLASSES[tone]}`}
    >
      {tone === "connected" && (
        <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 fill-none stroke-current">
          <path
            d="M3 8.5 6.5 12 13 4.5"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      )}
      {tone === "failed" && (
        <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 fill-none stroke-current">
          <path d="M8 4v5" strokeWidth="2" strokeLinecap="round" />
          <circle cx="8" cy="12" r="1" className="fill-current stroke-none" />
        </svg>
      )}
      {tone === "loading" && (
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          className="size-4 animate-spin fill-none stroke-current"
        >
          <circle cx="8" cy="8" r="6" strokeWidth="2" className="opacity-25" />
          <path d="M14 8a6 6 0 0 0-6-6" strokeWidth="2" strokeLinecap="round" />
        </svg>
      )}
      {label}
    </span>
  );
}
