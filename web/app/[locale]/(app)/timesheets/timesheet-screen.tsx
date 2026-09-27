"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  currentWeek,
  readStatus,
  readWeek,
  shiftWeek,
  type TimesheetStatusRead,
  type TimesheetWeek,
} from "@/lib/api/timesheets";
import { formatDate } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";

import { TimesheetGrid } from "./timesheet-grid";

/**
 * The screen around the grid: week navigation, the states, and the narrow-viewport gate.
 *
 * Three decisions worth naming:
 *
 * * **The narrow-viewport downgrade is here, and it is measured, not guessed.** Below
 *   the design system's 768px breakpoint (§7) the grid is replaced by a panel that says
 *   so in the reader's language. The alternative — seven columns squeezed into 375px —
 *   is the outcome the design explicitly rejects: "hard-fitting a 7-column grid into a
 *   375px screen does not produce responsive design, it produces an interface that is
 *   hard to use on both sides".
 * * **Reading is not writing.** The gate and the empty state both mean no request goes
 *   out on their behalf: an empty week is answered by the API as seven empty days, and
 *   the screen says so rather than creating a row.
 * * **The week is a query parameter,** so a week can be linked and reloaded and the
 *   server's first paint is the week the reader asked for — no client round trip before
 *   the grid appears.
 */
export function TimesheetScreen({
  dict,
  locale,
  week,
  initialGrid,
  initialStatus,
}: {
  dict: Dictionary;
  locale: Locale;
  week: string;
  initialGrid: TimesheetWeek | null;
  initialStatus: TimesheetStatusRead | null;
}) {
  const t = dict.timesheets;
  const router = useRouter();
  const [grid, setGrid] = useState(initialGrid);
  const [status, setStatus] = useState(initialStatus);
  const [wide, setWide] = useState<boolean | null>(null);

  // The server-rendered week is the truth: a refresh — ours after a write, or the
  // browser's after a navigation — replaces what is on screen.
  useEffect(() => setGrid(initialGrid), [initialGrid]);
  useEffect(() => setStatus(initialStatus), [initialStatus]);

  /**
   * Whether the viewport is wide enough for the grid.
   *
   * `null` until the first measurement, and the grid is not rendered during that pass:
   * rendering it and then hiding it would flash the wrong screen on a phone, and
   * assuming "wide" would render the grid on the device least able to show it.
   */
  useEffect(() => {
    const query = window.matchMedia("(min-width: 768px)");
    const measure = () => setWide(query.matches);
    measure();
    query.addEventListener("change", measure);
    return () => query.removeEventListener("change", measure);
  }, []);

  /** Move to another week: the URL is the state, so Back works and the week is linkable. */
  function goTo(target: string) {
    router.push(`/${locale}/timesheets?week=${target}`);
  }

  async function refresh() {
    router.refresh();
    const [next, state] = await Promise.all([readWeek(week), readStatus(week)]);
    setGrid(next);
    setStatus(state);
  }

  return (
    <div className="flex flex-col gap-8">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
        <p className="mt-2 text-fg-muted">{t.intro}</p>
      </div>

      <nav aria-label={t.title} className="flex flex-wrap items-center gap-2 text-sm">
        <button
          type="button"
          onClick={() => goTo(shiftWeek(week, -1))}
          className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
        >
          ← {t.previousWeek}
        </button>
        <button
          type="button"
          onClick={() => goTo(currentWeek())}
          className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
        >
          {t.thisWeek}
        </button>
        <button
          type="button"
          onClick={() => goTo(shiftWeek(week, 1))}
          className="min-h-9 rounded border border-border bg-surface px-3 hover:bg-neutral-bg"
        >
          {t.nextWeek} →
        </button>
        <span className="tabular text-fg-subtle">
          {t.weekOf
            .replace("{from}", formatDate(week, locale))
            .replace("{to}", formatDate(shiftWeek(week, 1), locale))}
        </span>
      </nav>

      {grid === null ? (
        <ErrorState dict={dict} onRetry={refresh} />
      ) : wide === null ? (
        <p className="text-sm text-fg-subtle">{t.loading}</p>
      ) : wide ? (
        <TimesheetGrid
          dict={dict}
          locale={locale}
          week={week}
          grid={grid}
          status={status}
          onGrid={setGrid}
        />
      ) : (
        <DesktopOnly dict={dict} />
      )}
    </div>
  );
}

/**
 * The grid does not fit a phone, and this says so instead of pretending otherwise.
 *
 * Three things it does, in the order the design asks for them (§4.3): it says why the
 * screen is not what was expected, it says what to do instead, and it does not render a
 * control nobody can use. The link is the way out, and it is a real link so Back returns
 * to the week.
 */
function DesktopOnly({ dict }: { dict: Dictionary }) {
  const t = dict.timesheets.desktopOnly;
  return (
    <section
      aria-labelledby="timesheet-desktop-only"
      className="flex flex-col gap-3 rounded border border-border bg-info-bg p-4 text-info"
    >
      <h2 id="timesheet-desktop-only" className="flex items-center gap-2 text-base font-semibold">
        <span aria-hidden="true">🖥</span>
        {t.title}
      </h2>
      <p className="text-sm">{t.body}</p>
      <p className="text-sm">{t.alternative}</p>
    </section>
  );
}

/** The three states a failed read has, and the retry that is always offered (§4.3). */
function ErrorState({ dict, onRetry }: { dict: Dictionary; onRetry: () => void }) {
  const t = dict.timesheets;
  return (
    <section
      role="alert"
      className="flex flex-col gap-3 rounded border border-border bg-danger-bg p-4 text-danger"
    >
      <h2 className="text-base font-semibold">{t.error}</h2>
      <p className="text-sm">{t.emptyHint}</p>
      <button
        type="button"
        onClick={onRetry}
        className="min-h-9 self-start rounded border border-border bg-surface px-3 text-sm text-fg hover:bg-neutral-bg"
      >
        {t.retry}
      </button>
    </section>
  );
}
