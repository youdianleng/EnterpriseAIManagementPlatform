"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import type { Conversation } from "@/lib/api/answers";
import { formatDate, formatNumber } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";
import { Dialog } from "@/lib/ui/dialog";
import { TextField } from "@/lib/ui/field";
import { isStreaming, turnsOf, useQaStore } from "@/lib/stores/qa-store";
import { cn } from "@/lib/ui/cn";

import { AnswerView, fromStored, fromTurn, type AnswerView as Answer } from "./answer-view";
import { CitationPanel } from "./citation-panel";
import { DraftForm } from "./draft-form";

/**
 * The Q&A screen: the conversations on the left, the answer on the right, the question box
 * underneath (the ticket's 「左侧会话列表、右侧对话流、底部输入框」).
 *
 * The screen is a *view* of `lib/stores/qa-store.ts`, and the store is where the reasoning
 * for what lives outside it is written down: the in-flight turn, the open conversation,
 * the composer and the open citation panel all survive a locale switch, because switching
 * the language is a navigation in this product and 「流式过程中切换语言不影响正在生成的回答」
 * cannot be satisfied by state that a remount throws away.
 *
 * The conversation list comes from the server (`page.tsx`) and is held here, exactly as the
 * document list is: it is on the first paint, and `router.refresh()` re-reads it after a
 * write, so the sidebar never holds a second, drifting copy of the API's answer.
 *
 * Four states are designed rather than assumed (§4.3): the empty list says what to do
 * first, the stream shows a searching line before its first token, a refusal is its own
 * block (§4.4), and a failed read is a sentence with a retry.
 */
export function QaScreen({
  dict,
  locale,
  initialConversations,
  total,
  failed,
}: {
  dict: Dictionary;
  locale: Locale;
  initialConversations: Conversation[];
  total: number;
  failed: boolean;
}) {
  const t = dict.qa;
  const router = useRouter();
  const [items, setItems] = useState(initialConversations);
  const [count, setCount] = useState(total);
  const [askError, setAskError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<Conversation | null>(null);

  const selectedId = useQaStore((state) => state.selectedId);
  const transcripts = useQaStore((state) => state.transcripts);
  const drafts = useQaStore((state) => state.drafts);
  const turns = useQaStore((state) => state.turns);
  const composer = useQaStore((state) => state.composer);
  const openCitation = useQaStore((state) => state.openCitation);
  const loadingTranscript = useQaStore((state) => state.loadingTranscript);
  const transcriptFailed = useQaStore((state) => state.transcriptFailed);
  const select = useQaStore((state) => state.select);
  const setComposer = useQaStore((state) => state.setComposer);
  const openPanel = useQaStore((state) => state.openPanel);
  const closePanel = useQaStore((state) => state.closePanel);
  const ask = useQaStore((state) => state.ask);
  const retry = useQaStore((state) => state.retry);

  // The server is the source of truth: a refresh, or the browser's own navigation,
  // replaces what is on screen.
  useEffect(() => {
    setItems(initialConversations);
    setCount(total);
  }, [initialConversations, total]);

  const busy = isStreaming(turns);
  const stored = selectedId ? (transcripts[selectedId] ?? null) : null;
  /**
   * The draft waiting in the open conversation, if the assistant proposed one.
   *
   * Read from the same server answer the transcript comes from (`GET
   * /answers/conversations/{id}` carries both), so a refresh or a restart cannot lose it and
   * the screen never holds a second copy it would have to keep in step. Ticket 40's card is
   * drawn *above* the answers: what a draft asks for is the next thing the reader does.
   */
  const draft = selectedId ? (drafts[selectedId] ?? null) : null;

  /**
   * The transcript and the live turns, as one list.
   *
   * A live turn whose message the transcript already carries is dropped from the live side
   * (`dropLoaded`), so an answer is never drawn twice — and while the transcript has not
   * arrived yet the live turn *is* the answer, which is what makes the first question of a
   * new conversation appear without a round trip.
   */
  const answers: Answer[] = [
    ...(stored ?? []).map(fromStored),
    ...turnsOf(turns, selectedId).map(fromTurn),
  ];

  const citation = openCitation
    ? (findCitation(answers, openCitation.turnKey, openCitation.index) ?? null)
    : null;
  const openIndex = openCitation?.index ?? null;

  async function submit() {
    setAskError(null);
    await ask();
    // After the answer, and only then: the sidebar gains the conversation a first question
    // created, with the title the server derived from it.
    router.refresh();
  }

  return (
    <div
      className={cn(
        "grid min-w-0 gap-6",
        citation ? "lg:grid-cols-[16rem_minmax(0,1fr)_22rem]" : "lg:grid-cols-[16rem_minmax(0,1fr)]",
      )}
    >
      {/* --- the conversations ------------------------------------------------ */}
      <aside
        aria-labelledby="qa-conversations-heading"
        className="min-w-0 rounded-lg border border-border bg-surface p-4 shadow-sm"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="qa-conversations-heading" className="text-base font-semibold">
            {t.conversations.heading}
          </h2>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              select(null);
              setAskError(null);
            }}
            disabled={busy}
            data-testid="qa-new-conversation"
          >
            {t.conversations.newConversation}
          </Button>
        </div>
        <p className="mt-1 text-sm text-fg-subtle" data-testid="qa-retention">
          {t.conversations.retention.replace("{days}", formatNumber(90, locale))}
        </p>

        {failed ? (
          <Alert tone="danger" role="alert" className="mt-3" title={t.conversations.error}>
            <Button
              variant="secondary"
              size="sm"
              className="mt-2"
              onClick={() => router.refresh()}
            >
              {t.conversations.retry}
            </Button>
          </Alert>
        ) : items.length === 0 && answers.length === 0 ? (
          // **Only when nothing is on screen either.** The two are separate facts: the list
          // is the server's copy, and the thread can hold an answer the list has not caught
          // up with yet (a first question creates its conversation and `router.refresh()`
          // takes a round trip). "You have no conversations yet" printed beside a visible
          // answer is a screen contradicting itself, which is what the screenshot of this
          // screen showed — so the empty state waits for both halves to be empty.
          <p className="mt-3 text-sm text-fg-muted" data-testid="qa-conversations-empty">
            {t.conversations.empty} {t.conversations.emptyHint}
          </p>
        ) : items.length === 0 ? null : (
          <ul className="mt-3 flex flex-col gap-1" data-testid="qa-conversation-list">
            {items.map((conversation) => (
              <ConversationRow
                key={conversation.id}
                conversation={conversation}
                selected={conversation.id === selectedId}
                busy={busy}
                dict={dict}
                locale={locale}
                onOpen={() => select(conversation.id)}
                onRenamed={(updated) => {
                  setItems((current) =>
                    current.map((item) => (item.id === updated.id ? updated : item)),
                  );
                  router.refresh();
                }}
                onDelete={() => setConfirming(conversation)}
              />
            ))}
          </ul>
        )}
        {items.length > 0 && (
          <p className="mt-2 text-sm text-fg-subtle tabular" data-testid="qa-conversation-count">
            {t.conversations.count
              .replace("{shown}", formatNumber(items.length, locale))
              .replace("{total}", formatNumber(count, locale))}
          </p>
        )}
      </aside>

      {/* --- the conversation ------------------------------------------------- */}
      <section aria-labelledby="qa-thread-heading" className="flex min-w-0 flex-col gap-4">
        <h2 id="qa-thread-heading" className="text-base font-semibold">
          {threadHeading(items, selectedId, answers, t.thread.newConversation)}
        </h2>
        {selectedId && (
          <p className="text-sm text-fg-subtle" data-testid="qa-expiry">
            {t.thread.expiresAt.replace(
              "{date}",
              formatDate(
                items.find((item) => item.id === selectedId)?.expires_at ?? new Date().toISOString(),
                locale,
              ),
            )}
          </p>
        )}

        {askError && (
          <Alert tone="danger" role="alert">
            {askError}
          </Alert>
        )}
        {transcriptFailed && (
          <Alert tone="danger" role="alert" title={t.thread.error}>
            <Button
              variant="secondary"
              size="sm"
              className="mt-2"
              onClick={() => selectedId && select(selectedId)}
            >
              {t.thread.retry}
            </Button>
          </Alert>
        )}

        {loadingTranscript && answers.length === 0 && (
          <p className="text-fg-muted">{t.thread.loading}</p>
        )}

        {draft && draft.prefill_form && (
          <DraftForm draft={draft} dict={dict} locale={locale} />
        )}

        {/* **Not while a draft is on screen.** The thread's empty state tells a reader how to
            start; a conversation whose whole content is a form waiting to be confirmed has
            already started, and the two together read as a screen contradicting itself —
            which is what the first screenshot of this card showed. */}
        {answers.length === 0 && !loadingTranscript && !draft ? (
          <p className="rounded-lg border border-border bg-surface p-4 text-fg-muted" data-testid="qa-thread-empty">
            {t.thread.empty}
          </p>
        ) : (
          <ul className="flex min-w-0 flex-col gap-4" data-testid="qa-answers">
            {answers.map((answer) => (
              <li key={answer.key} className="min-w-0">
                <AnswerView
                  answer={answer}
                  dict={dict}
                  locale={locale}
                  openCitation={openCitation}
                  onOpenCitation={openPanel}
                  onRetry={retry}
                />
              </li>
            ))}
          </ul>
        )}

        {/* --- the question ---------------------------------------------------- */}
        <form
          className="rounded-lg border border-border bg-surface p-4 shadow-sm"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
          noValidate
        >
          <div className="flex flex-col gap-2">
            <label htmlFor="qa-question" className="text-sm font-medium">
              {t.composer.label}
            </label>
            <textarea
              id="qa-question"
              name="question"
              rows={3}
              maxLength={2000}
              value={composer}
              disabled={busy}
              aria-describedby="qa-question-hint"
              onChange={(event) => setComposer(event.target.value)}
              onKeyDown={(event) => {
                // Enter sends, Shift+Enter is a newline: a question is usually one line,
                // and the shortcut is announced in the hint so it is discoverable.
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submit();
                }
              }}
              className="w-full rounded border border-border bg-surface px-3 py-2 text-fg disabled:opacity-60"
            />
            <p id="qa-question-hint" className="text-sm text-fg-subtle">
              {t.composer.hint}
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" disabled={busy || composer.trim().length === 0}>
                {busy ? t.composer.asking : t.composer.submit}
              </Button>
              {busy && (
                <span className="text-sm text-fg-muted" role="status">
                  {t.composer.streaming}
                </span>
              )}
            </div>
          </div>
        </form>
      </section>

      {/* --- the passage ------------------------------------------------------ */}
      {citation && openIndex !== null && (
        <CitationPanel
          citation={citation}
          index={openIndex}
          dict={dict}
          locale={locale}
          onClose={closePanel}
        />
      )}

      <DeleteDialog
        conversation={confirming}
        dict={dict}
        onClose={() => setConfirming(null)}
        onDeleted={(id) => {
          setItems((current) => current.filter((item) => item.id !== id));
          setCount((current) => Math.max(0, current - 1));
          setConfirming(null);
          router.refresh();
        }}
      />
    </div>
  );
}

/** One row: open it, rename it in place, or delete it. */
function ConversationRow({
  conversation,
  selected,
  busy,
  dict,
  locale,
  onOpen,
  onRenamed,
  onDelete,
}: {
  conversation: Conversation;
  selected: boolean;
  busy: boolean;
  dict: Dictionary;
  locale: Locale;
  onOpen: () => void;
  onRenamed: (updated: Conversation) => void;
  onDelete: () => void;
}) {
  const t = dict.qa.conversations;
  const rename = useQaStore((state) => state.rename);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(conversation.title);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  async function save() {
    if (title.trim().length === 0) {
      setError(t.titleRequired);
      return;
    }
    try {
      const updated = await rename(conversation.id, title);
      setEditing(false);
      setError(null);
      onRenamed(updated);
    } catch {
      setError(t.renameFailed);
    }
  }

  if (editing) {
    return (
      <li className="rounded border border-border p-2">
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <TextField
            label={t.renameLabel}
            value={title}
            onChange={setTitle}
            inputRef={inputRef}
            required
            error={error ?? undefined}
          />
          <div className="flex flex-wrap gap-2">
            <Button type="submit" size="sm" disabled={busy}>
              {t.save}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="secondary"
              onClick={() => {
                setEditing(false);
                setTitle(conversation.title);
                setError(null);
              }}
            >
              {t.cancel}
            </Button>
          </div>
        </form>
      </li>
    );
  }

  return (
    <li
      className={cn(
        "rounded border p-2",
        selected ? "border-primary bg-primary-subtle" : "border-transparent hover:bg-neutral-bg",
      )}
      data-testid="qa-conversation"
      data-conversation-id={conversation.id}
      data-selected={selected ? "true" : "false"}
    >
      <button
        type="button"
        onClick={onOpen}
        className="block w-full text-left text-sm font-medium"
        aria-current={selected ? "true" : undefined}
      >
        <span className="break-words">{conversation.title}</span>
        <span className="mt-1 block text-xs text-fg-subtle">
          {t.lastMessage.replace("{date}", formatDate(conversation.last_message_at, locale))}
        </span>
      </button>
      <div className="mt-1 flex flex-wrap gap-1">
        {/* §5's touch rule against §1's density: 44px where a finger is the pointer,
            36px (`size="sm"`) from the tablet breakpoint up, which is where the design
            system expects this product to be used. */}
        <Button
          size="sm"
          variant="ghost"
          className="min-h-11 md:min-h-9"
          onClick={() => {
            setTitle(conversation.title);
            setEditing(true);
          }}
          aria-label={t.renameLabelFor.replace("{title}", conversation.title)}
        >
          {t.rename}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="min-h-11 md:min-h-9"
          onClick={onDelete}
          aria-label={t.deleteLabelFor.replace("{title}", conversation.title)}
        >
          {t.delete}
        </Button>
      </div>
    </li>
  );
}

/**
 * Deleting is confirmed, and the confirmation says what "deleted" means today.
 *
 * §3.6/D18 make the removal immediate and flag-based: the conversation leaves the owner's
 * list at once and its row is removed later by the retention sweep. Saying "permanently
 * deleted" would be false, and saying nothing would leave a reader guessing whether the
 * question is still somewhere — so the sentence states both halves without pretending the
 * transcript is destroyed.
 */
function DeleteDialog({
  conversation,
  dict,
  onClose,
  onDeleted,
}: {
  conversation: Conversation | null;
  dict: Dictionary;
  onClose: () => void;
  onDeleted: (id: string) => void;
}) {
  const t = dict.qa.deleteDialog;
  const remove = useQaStore((state) => state.remove);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  async function confirm() {
    if (!conversation) return;
    setSending(true);
    setError(null);
    try {
      await remove(conversation.id);
      onDeleted(conversation.id);
    } catch {
      setError(t.failed);
    } finally {
      setSending(false);
    }
  }

  return (
    <Dialog
      open={conversation !== null}
      onClose={() => {
        setError(null);
        onClose();
      }}
      title={t.title}
      closeLabel={t.cancel}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={sending}>
            {t.cancel}
          </Button>
          <Button variant="danger" onClick={() => void confirm()} disabled={sending}>
            {sending ? t.deleting : t.confirm}
          </Button>
        </>
      }
    >
      <p>{t.body.replace("{title}", conversation?.title ?? "")}</p>
      <p className="mt-2">{t.hint}</p>
      {error && (
        <p className="mt-2 text-danger" role="alert">
          {error}
        </p>
      )}
    </Dialog>
  );
}

function selectedTitle(items: Conversation[], id: string | null): string | null {
  if (!id) return null;
  return items.find((item) => item.id === id)?.title ?? null;
}

/**
 * The thread's heading: the conversation's own title, and the question until it arrives.
 *
 * The server derives a conversation's title from the question that created it
 * (`answer.models.title_for`), and the sidebar learns it on the next `router.refresh()`.
 * Until that lands, the screen would say "New conversation" above a conversation that
 * plainly has one — so the fallback is the *question*, which is the same text the title is
 * made of, rather than a shorter copy this client clipped for itself. The list replaces it
 * as soon as it catches up.
 */
function threadHeading(
  items: Conversation[],
  selectedId: string | null,
  answers: Answer[],
  fallback: string,
): string {
  return selectedTitle(items, selectedId) ?? answers[0]?.question ?? fallback;
}

/** The citation a badge points at, found across the live turns and the stored messages. */
function findCitation(
  answers: Answer[],
  turnKey: string,
  index: number,
): Answer["citations"][number] | null {
  const answer = answers.find((item) => item.key === turnKey);
  if (!answer) return null;
  return answer.citations[index - 1] ?? null;
}
