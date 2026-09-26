import type { CSSProperties } from "react";

import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";
import { formatDecimal } from "@/lib/format";

import { StyleGuideDemo } from "./style-guide-demo";

/** Token swatches, read from the CSS variables so they cannot drift. */
const NEUTRAL_TOKENS = [
  "--eam-bg",
  "--eam-surface",
  "--eam-border",
  "--eam-fg",
  "--eam-fg-muted",
  "--eam-fg-subtle",
];

/** Foreground paired with its own background: they are only correct together. */
const SEMANTIC_PAIRS = [
  { label: "success", fg: "--eam-success", bg: "--eam-success-bg" },
  { label: "warning", fg: "--eam-warning", bg: "--eam-warning-bg" },
  { label: "danger", fg: "--eam-danger", bg: "--eam-danger-bg" },
  { label: "info", fg: "--eam-info", bg: "--eam-info-bg" },
  { label: "neutral", fg: "--eam-neutral", bg: "--eam-neutral-bg" },
];

const SPACING_TOKENS = [
  "--eam-space-1",
  "--eam-space-2",
  "--eam-space-3",
  "--eam-space-4",
  "--eam-space-6",
  "--eam-space-8",
  "--eam-space-12",
  "--eam-space-16",
];

/** Values chosen so the decimal separator is visible at a glance. */
const TABULAR_SAMPLES = [1.5, 11.5, 111.5, 1111.5];

function Swatch({ token }: { token: string }) {
  return (
    <div className="flex items-center gap-3">
      <span
        aria-hidden="true"
        className="size-10 shrink-0 rounded border border-border"
        style={{ background: `var(${token})` }}
      />
      <code className="text-sm text-fg-muted">{token}</code>
    </div>
  );
}

/**
 * A foreground/background pair shown as one unit.
 *
 * Listing the two colours separately makes it impossible to see whether they
 * actually work together, which is the only question that matters for a
 * semantic colour: the pair is readable or it is not.
 */
function PairSwatch({ fg, bg, sample }: { fg: string; bg: string; sample: string }) {
  return (
    <div className="flex items-center gap-3">
      <span
        className="inline-flex size-10 shrink-0 items-center justify-center rounded border border-border text-sm font-semibold"
        style={{ background: `var(${bg})`, color: `var(${fg})` }}
      >
        {sample}
      </span>
      <code className="text-sm text-fg-muted">
        {fg}
        <br />
        {bg}
      </code>
    </div>
  );
}

export default async function StyleGuidePage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);
  const t = dict.styleGuide;

  return (
    <div className="flex flex-col gap-10">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
        <p className="mt-2 max-w-3xl text-fg-muted">{t.intro}</p>
      </div>

      <section aria-labelledby="sg-colours">
        <h2 id="sg-colours" className="text-lg font-semibold">
          {t.colours.title}
        </h2>
        <p className="mt-1 mb-4 text-fg-muted">{t.colours.description}</p>

        <div className="grid gap-6 md:grid-cols-2">
          <div>
            <h3 className="mb-3 text-sm font-medium text-fg-subtle">{t.colours.neutrals}</h3>
            <div className="flex flex-col gap-3">
              {NEUTRAL_TOKENS.map((token) => (
                <Swatch key={token} token={token} />
              ))}
            </div>
          </div>
          <div className="flex flex-col gap-6">
            <div>
              <h3 className="mb-3 text-sm font-medium text-fg-subtle">{t.colours.primary}</h3>
              <Swatch token="--eam-primary" />
            </div>
            <div>
              <h3 className="mb-3 text-sm font-medium text-fg-subtle">{t.colours.semantic}</h3>
              <div className="flex flex-col gap-3">
                {SEMANTIC_PAIRS.map((pair) => (
                  <PairSwatch key={pair.label} fg={pair.fg} bg={pair.bg} sample="Aa" />
                ))}
              </div>
            </div>
          </div>
        </div>
        <p className="mt-4 text-sm text-fg-subtle">{t.colours.contrastNote}</p>
      </section>

      <section aria-labelledby="sg-typography">
        <h2 id="sg-typography" className="text-lg font-semibold">
          {t.typography.title}
        </h2>
        <p className="mt-1 mb-4 text-fg-muted">{t.typography.description}</p>
        <div className="flex flex-col gap-4">
          <p className="text-xl font-semibold">{t.typography.pageHeading}</p>
          <p className="text-lg font-semibold">{t.typography.sectionHeading}</p>
          <p>{t.typography.body}</p>
          <p className="text-sm text-fg-muted">{t.typography.secondary}</p>
          <div className="rounded border border-border bg-surface p-4">
            <p className="mb-2 text-sm font-medium">{t.typography.tabularTitle}</p>
            <div className="tabular flex flex-wrap gap-6">
              {TABULAR_SAMPLES.map((sample) => (
                <span key={sample}>{formatDecimal(sample, locale)}</span>
              ))}
            </div>
            <p className="mt-2 text-sm text-fg-subtle">{t.typography.tabularNote}</p>
          </div>
        </div>
      </section>

      <section aria-labelledby="sg-spacing">
        <h2 id="sg-spacing" className="text-lg font-semibold">
          {t.spacing.title}
        </h2>
        <p className="mt-1 mb-4 text-fg-muted">{t.spacing.description}</p>
        <div className="flex flex-col gap-2">
          {SPACING_TOKENS.map((token) => (
            <div key={token} className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className="h-3 rounded-sm bg-primary"
                style={{ width: `var(${token})` } as CSSProperties}
              />
              <code className="text-sm text-fg-muted">{token}</code>
            </div>
          ))}
        </div>
      </section>

      <StyleGuideDemo dict={dict} locale={locale} />
    </div>
  );
}
