/**
 * Locale configuration.
 *
 * Spanish is the primary product language and the fallback; English is the
 * secondary. The preference cookie outlives a locale-less visit so a manual
 * switch sticks.
 */

export const LOCALES = ["es", "en"] as const;

export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "es";

export const LOCALE_COOKIE = "eam_locale";

/** How each language names itself — never translated. */
export const LOCALE_LABELS: Record<Locale, string> = {
  es: "Español",
  en: "English",
};

export function isLocale(value: string | undefined | null): value is Locale {
  return value !== undefined && value !== null && (LOCALES as readonly string[]).includes(value);
}

/**
 * First supported locale in the browser's Accept-Language list.
 *
 * Only the primary subtag is compared, so "es-ES" and "es-419" both match "es".
 */
export function matchLocaleFromAcceptLanguage(header: string | null): Locale | null {
  if (!header) return null;
  const ranked = header
    .split(",")
    .map((part) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.find((p) => p.trim().startsWith("q="));
      const quality = q ? Number.parseFloat(q.split("=")[1]) : 1;
      return { tag: tag.trim().toLowerCase(), quality: Number.isNaN(quality) ? 0 : quality };
    })
    .filter((entry) => entry.tag.length > 0)
    .sort((a, b) => b.quality - a.quality);

  for (const entry of ranked) {
    const primary = entry.tag.split("-")[0];
    if (isLocale(primary)) return primary;
  }
  return null;
}
