import { DEFAULT_LOCALE, type Locale } from "@/lib/i18n/config";

/**
 * Locale-aware formatting.
 *
 * Formatting follows the *interface locale*, never the host default. That is
 * the whole reason this module exists: `toLocaleString(undefined)` follows the
 * browser, so one page can render 11,5 and another 11.5 for the same value.
 * For an HR system that displays hours, days and money, an inconsistent decimal
 * separator is a data-reading hazard, not a cosmetic detail.
 */

const NUMBER_FORMATTERS = new Map<string, Intl.NumberFormat>();
const DATE_FORMATTERS = new Map<string, Intl.DateTimeFormat>();
const PERCENT_FORMATTERS = new Map<string, Intl.NumberFormat>();

/** BCP-47 tag for the interface locale. */
export function localeTag(locale: Locale): string {
  return locale === "es" ? "es-ES" : "en-GB";
}

/**
 * The zone every displayed instant is converted into.
 *
 * Stated rather than inherited, for two reasons (`docs/DESIGN.md` D31, §9):
 *
 * 1. **The product's day is Madrid's day.** Timestamps are stored UTC and shown in
 *    the company's zone, which is also what makes a `date`-only value such as
 *    `"2026-03-12"` render as the 12th everywhere.
 * 2. **Two renderers format these strings.** A Server Component produces the first
 *    paint and the browser hydrates it; if the zone came from the runtime, a server
 *    in UTC and a browser in Madrid would disagree about the hour, and React
 *    reports that as a hydration mismatch rather than as a wrong clock.
 */
const DISPLAY_TIME_ZONE = "Europe/Madrid";

function numberFormatter(locale: Locale, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${localeTag(locale)}:${JSON.stringify(options)}`;
  let formatter = NUMBER_FORMATTERS.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(localeTag(locale), options);
    NUMBER_FORMATTERS.set(key, formatter);
  }
  return formatter;
}

function dateFormatter(locale: Locale, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${localeTag(locale)}:${JSON.stringify(options)}`;
  let formatter = DATE_FORMATTERS.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(localeTag(locale), options);
    DATE_FORMATTERS.set(key, formatter);
  }
  return formatter;
}

export function formatNumber(
  value: number,
  locale: Locale = DEFAULT_LOCALE,
  options: Intl.NumberFormatOptions = {},
): string {
  return numberFormatter(locale, options).format(value);
}

/** Fixed decimal places — the shape used for hours and money columns. */
export function formatDecimal(
  value: number,
  locale: Locale = DEFAULT_LOCALE,
  fractionDigits = 1,
): string {
  return formatNumber(value, locale, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

export function formatPercent(
  value: number,
  locale: Locale = DEFAULT_LOCALE,
  fractionDigits = 0,
): string {
  const key = `${localeTag(locale)}:${fractionDigits}`;
  let formatter = PERCENT_FORMATTERS.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(localeTag(locale), {
      style: "percent",
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    });
    PERCENT_FORMATTERS.set(key, formatter);
  }
  return formatter.format(value);
}

/** Spanish convention is dd/mm/yyyy; English here is day-month-year too. */
export function formatDate(value: Date | string, locale: Locale = DEFAULT_LOCALE): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return dateFormatter(locale, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone: DISPLAY_TIME_ZONE,
  }).format(date);
}

/** "12 de marzo de 2026" / "12 March 2026" — for prose, not tables. */
export function formatLongDate(value: Date | string, locale: Locale = DEFAULT_LOCALE): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return dateFormatter(locale, {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: DISPLAY_TIME_ZONE,
  }).format(date);
}

/**
 * Date and time, for a record whose *hour* matters.
 *
 * A notification centre answers "when was I told", and the day alone cannot
 * separate two notices that arrived the same afternoon.
 */
export function formatDateTime(value: Date | string, locale: Locale = DEFAULT_LOCALE): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return dateFormatter(locale, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: DISPLAY_TIME_ZONE,
  }).format(date);
}

/** Durations are shown as "7 h 30 min" / "7 h 30 min" — never as "7.5 h". */
export function formatDuration(
  minutes: number,
  locale: Locale = DEFAULT_LOCALE,
): string {
  const hours = Math.trunc(minutes / 60);
  const rest = Math.abs(minutes % 60);
  const hourUnit = locale === "es" ? "h" : "h";
  const minuteUnit = locale === "es" ? "min" : "min";
  if (hours === 0) return `${rest} ${minuteUnit}`;
  if (rest === 0) return `${hours} ${hourUnit}`;
  return `${hours} ${hourUnit} ${rest} ${minuteUnit}`;
}
