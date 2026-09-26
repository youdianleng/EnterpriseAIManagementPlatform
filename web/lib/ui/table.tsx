import type { ReactNode } from "react";

import { cn } from "@/lib/ui/cn";

export type Column<T> = {
  key: string;
  header: string;
  /** Numbers must be right-aligned and tabular so columns compare by place value. */
  numeric?: boolean;
  render: (row: T) => ReactNode;
};

/**
 * Minimal data table.
 *
 * Horizontal scrolling is contained rather than letting the page scroll, so a
 * wide timesheet grid does not drag the whole layout sideways.
 */
export function DataTable<T>({
  columns,
  rows,
  getRowKey,
  emptyMessage,
  caption,
}: {
  columns: Array<Column<T>>;
  rows: T[];
  getRowKey: (row: T) => string;
  emptyMessage: string;
  caption?: string;
}) {
  if (rows.length === 0) {
    return (
      <p className="rounded border border-dashed border-border px-4 py-8 text-center text-fg-muted">
        {emptyMessage}
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-border text-left">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={cn(
                  "px-3 py-2 font-medium text-fg-muted",
                  column.numeric && "text-right",
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={getRowKey(row)} className="border-b border-border last:border-0">
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={cn("px-3 py-2", column.numeric && "tabular text-right")}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
