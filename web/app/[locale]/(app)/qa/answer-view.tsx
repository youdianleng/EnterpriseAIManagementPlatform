"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Citation, SourceNotice, StoredMessage } from "@/lib/api/answers";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { remarkCitationMarkers } from "@/lib/qa/citation-markers";
import { formatNumber } from "@/lib/format";
import type { Turn, TurnPhase } from "@/lib/stores/qa-store";

/**
 * One answer: the scope banner, the prose, the citation badges, and the two states that
 * are not prose at all — D20's refusal and a model that could not be reached.
 *
 * **The Markdown is rendered by `react-markdown` with no raw-HTML plugin.** The text of an
 * answer is *document content plus model output*, so `dangerouslySetInnerHTML` on it would
 * be an XSS hole with the corpus as the attack surface: a passage reading
 * `<img src=x onerror=alert(1)>` would execute for every reader of the answer that quoted
 * it. `react-markdown` renders a tree and never an HTML string, and `rehype-raw` is
 * deliberately absent — a `.answer-prose` table cell containing that passage shows the
 * passage. `scripts/visual-check.mjs` proves it end to end by uploading exactly that text
 * and asserting it arrives as text.
 *
 * **The refusal is a normal state, not an error (design system §4.4).** Neutral tone, no
 * red, the retrieval scope named, and an actionable next step; D20's behaviour is the
 * system working, and painting it as a failure sends people to IT. It also renders the
 * **catalogue key** (`errors.knowledge_base_no_basis`) rather than the refusal frame's own
 * `content`: that constant is deliberately bilingual — one string carrying Spanish and
 * English — because it is written for a client with no dictionary, and printing both
 * languages to a reader who reads one is not a bilingual interface.
 *
 * **The scope banner is the server's object, not a sentence the client composed.** Ticket
 * 36's `source_notice.text` carries the sentence in every language the interface ships, so
 * the reader's language comes out of the payload; `message_key` is the fallback for a
 * language the payload does not carry.
 */
export type AnswerView = {
  key: string;
  question: string;
  text: string;
  citations: Citation[];
  sourceNotice: SourceNotice | null;
  phase: TurnPhase;
  messageKey: string | null;
};

/** A live turn, as the view renders it. */
export function fromTurn(turn: Turn): AnswerView {
  return {
    key: turn.key,
    question: turn.question,
    text: turn.text,
    citations: turn.citations,
    sourceNotice: turn.sourceNotice,
    phase: turn.phase,
    messageKey: turn.messageKey,
  };
}

/**
 * A stored message, as the view renders it.
 *
 * The stored row carries `is_refusal` and `error_key` rather than a catalogue key, so the
 * two are translated here — one place, so a conversation read back tomorrow renders the
 * same two states the stream did.
 */
export function fromStored(message: StoredMessage): AnswerView {
  const phase: TurnPhase = message.is_refusal
    ? "refused"
    : message.error_key
      ? "failed"
      : "complete";
  return {
    key: message.id,
    question: message.question,
    text: message.content,
    citations: message.citations,
    sourceNotice: message.source_notice,
    phase,
    messageKey:
      phase === "refused"
        ? "errors.knowledge_base_no_basis"
        : phase === "failed"
          ? "errors.answer_model_unavailable"
          : null,
  };
}

export function AnswerView({
  answer,
  dict,
  locale,
  openCitation,
  onOpenCitation,
  onRetry,
}: {
  answer: AnswerView;
  dict: Dictionary;
  locale: Locale;
  openCitation: { turnKey: string; index: number } | null;
  onOpenCitation: (turnKey: string, index: number) => void;
  onRetry: (turnKey: string) => void;
}) {
  const t = dict.qa;
  return (
    <article
      className="rounded-lg border border-border bg-surface p-4 shadow-sm"
      data-testid="qa-answer"
      data-answer-phase={answer.phase}
    >
      <h3 className="text-base font-semibold" data-testid="qa-question">
        {answer.question}
      </h3>

      {/* §5.2's scope marker, at the top of the answer, before any text — it arrives on
          the `citations` frame precisely so it can be rendered here while the first
          tokens are still being generated. A refusal never carries one. */}
      {answer.sourceNotice && (
        <p
          className="mt-3 flex flex-wrap items-center gap-2 rounded bg-warning-bg px-3 py-2 text-sm text-warning"
          data-testid="qa-scope-banner"
        >
          <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 shrink-0 fill-current">
            <path d="M8 1.5 1.2 13.5h13.6L8 1.5Zm0 4a.9.9 0 0 1 .9.9v3a.9.9 0 0 1-1.8 0v-3a.9.9 0 0 1 .9-.9Zm0 5.1a1 1 0 1 1 0 2 1 1 0 0 1 0-2Z" />
          </svg>
          <span>
            {answer.sourceNotice.text[locale] ??
              answer.sourceNotice.text.es ??
              t.scope.fallback}
          </span>
        </p>
      )}

      {answer.phase === "refused" ? (
        <Refusal dict={dict} />
      ) : answer.phase === "failed" ? (
        <Failure
          dict={dict}
          messageKey={answer.messageKey}
          onRetry={() => onRetry(answer.key)}
        />
      ) : (
        <>
          {answer.text.length === 0 && answer.phase === "streaming" && (
            <p className="mt-3 text-fg-muted" role="status" data-testid="qa-searching">
              {t.answer.searching}
            </p>
          )}
          {answer.text.length > 0 && (
            <div className="answer-prose mt-3 break-words" data-testid="qa-answer-text">
              <Markdown
                remarkPlugins={[
                  remarkGfm,
                  [remarkCitationMarkers, { max: answer.citations.length }],
                ]}
                components={{
                  // Every link is rendered by this one function, so the citation marker
                  // and a link the answer legitimately contains cannot be confused: the
                  // marker's href is the `#cite-N` fragment the remark plugin writes, and
                  // everything else is an ordinary link opened as one.
                  a: ({ href, children }) => {
                    const marker = /^#cite-(\d+)$/.exec(href ?? "");
                    if (marker) {
                      const index = Number(marker[1]);
                      const citation = answer.citations[index - 1];
                      if (!citation) return <span>{children}</span>;
                      const open =
                        openCitation?.turnKey === answer.key &&
                        openCitation.index === index;
                      return (
                        <button
                          type="button"
                          data-testid="qa-citation-badge"
                          data-citation={index}
                          aria-expanded={open}
                          aria-label={t.citation.open.replace(
                            "{n}",
                            formatNumber(index, locale),
                          )}
                          onClick={() => onOpenCitation(answer.key, index)}
                          className="tabular mx-0.5 inline-flex min-h-6 min-w-6 items-center justify-center rounded bg-primary-subtle px-1 align-middle font-semibold text-primary no-underline hover:brightness-95"
                        >
                          {formatNumber(index, locale)}
                        </button>
                      );
                    }
                    return (
                      <a href={href} target="_blank" rel="noreferrer noopener">
                        {children}
                      </a>
                    );
                  },
                  // A wide table gets its own scrollbar rather than the page's: §3.1
                  // forbids a layout that clips, and §7 forbids the page scrolling
                  // sideways at 320px.
                  table: ({ children }) => (
                    <div className="max-w-full overflow-x-auto">
                      <table>{children}</table>
                    </div>
                  ),
                }}
              >
                {answer.text}
              </Markdown>
            </div>
          )}
          {answer.phase === "streaming" && answer.text.length > 0 && (
            <p className="mt-2 text-sm text-fg-subtle" role="status">
              {t.answer.streaming}
            </p>
          )}
          {answer.phase === "complete" && answer.citations.length > 0 && (
            <Citations answer={answer} dict={dict} locale={locale} />
          )}
        </>
      )}
    </article>
  );
}

/**
 * D20's refusal, drawn as the system working (design system §4.4).
 *
 * Three rules from that section, each visible here: the tone is neutral and does not
 * over-apologise; the retrieval scope is stated ("the company knowledge base"); and the
 * next step is concrete. The `warning` pair rather than `danger` is the whole point of the
 * section — a refusal rendered red reads as a fault, and readers then ask IT about a
 * system that behaved exactly as designed. The icon and the heading carry the state as
 * well as the colour does, which §5 requires of every status.
 */
function Refusal({ dict }: { dict: Dictionary }) {
  const t = dict.qa.refusal;
  return (
    <div
      className="mt-3 rounded border-l-4 border-warning bg-warning-bg p-3"
      data-testid="qa-refusal"
    >
      <p className="flex flex-wrap items-center gap-2 font-semibold text-warning">
        <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 shrink-0 fill-current">
          <path d="M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13Zm0 2.7a.9.9 0 0 1 .9.9v3.4a.9.9 0 0 1-1.8 0V5.1a.9.9 0 0 1 .9-.9Zm0 6.1a1 1 0 1 1 0 2 1 1 0 0 1 0-2Z" />
        </svg>
        {t.title}
      </p>
      <p className="mt-2">{dict.errors.knowledge_base_no_basis}</p>
      <ul className="mt-2 list-disc pl-6">
        {t.steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ul>
    </div>
  );
}

/** A model that could not be reached: an error *is* an error, and it offers the retry. */
function Failure({
  dict,
  messageKey,
  onRetry,
}: {
  dict: Dictionary;
  messageKey: string | null;
  onRetry: () => void;
}) {
  const t = dict.qa.failure;
  const known = messageKey?.replace(/^errors\./, "") as keyof Dictionary["errors"] | undefined;
  const sentence =
    known && known in dict.errors ? dict.errors[known] : dict.errors.answer_model_unavailable;
  return (
    <div className="mt-3 rounded border border-danger bg-danger-bg p-3" role="alert" data-testid="qa-error">
      <p className="font-semibold text-danger">{t.title}</p>
      <p className="mt-2">{sentence}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 rounded border border-border bg-surface px-3 py-1.5 text-sm hover:bg-neutral-bg"
      >
        {t.retry}
      </button>
    </div>
  );
}

/** The sources, as a list beside the prose — every one of them openable. */
function Citations({
  answer,
  dict,
  locale,
}: {
  answer: AnswerView;
  dict: Dictionary;
  locale: Locale;
}) {
  const t = dict.qa.citation;
  return (
    <section className="mt-4" aria-label={t.listLabel}>
      <h4 className="text-sm font-semibold text-fg-muted">{t.listLabel}</h4>
      <ul className="mt-2 flex flex-col gap-1 text-sm" data-testid="qa-citation-list">
        {answer.citations.map((citation, index) => (
          // **No `flex-wrap` here, and that is the fix a screenshot produced.** With the
          // citation panel open the thread column is ~290px wide, and a wrapping row put
          // the source *number* on one line and its text on the next — a column of bare
          // digits with the sources printed underneath them. The number is `flex-none` and
          // the text takes the remaining width, so the two can never separate.
          <li key={`${citation.chunk_id}-${index}`} className="flex gap-x-2">
            <span className="tabular flex-none font-semibold text-primary">
              {formatNumber(index + 1, locale)}
            </span>
            <span className="min-w-0 break-words">
              {citation.title}
              {citation.page !== null && (
                <>
                  {" · "}
                  <span className="tabular">
                    {t.page.replace("{page}", formatNumber(citation.page, locale))}
                  </span>
                </>
              )}
              {!citation.is_company_kb && ` · ${t.personal}`}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
