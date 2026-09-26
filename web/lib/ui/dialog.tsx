"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * Native `<dialog>` modal.
 *
 * The browser already provides focus trapping, Escape-to-close and the top
 * layer, so this wraps that instead of reimplementing it. A hand-rolled modal
 * is where keyboard traps and invisible-content bugs come from.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  closeLabel,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  closeLabel: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      // Fires for Escape and for close(); one path keeps state in sync.
      onClose={onClose}
      aria-labelledby="dialog-title"
      className="w-[min(32rem,calc(100vw-2rem))] rounded-lg border border-border bg-surface p-6 text-fg backdrop:bg-black/40"
    >
      <div className="mb-4 flex items-start justify-between gap-4">
        <h2 id="dialog-title" className="text-lg font-semibold">
          {title}
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={closeLabel}
          className="rounded px-2 py-1 text-fg-muted hover:bg-neutral-bg hover:text-fg"
        >
          <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 fill-none stroke-current">
            <path d="M4 4l8 8M12 4l-8 8" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <div className="text-fg-muted">{children}</div>
      {footer && <div className="mt-6 flex flex-wrap justify-end gap-2">{footer}</div>}
    </dialog>
  );
}
