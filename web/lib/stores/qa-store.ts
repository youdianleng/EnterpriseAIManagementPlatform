"use client";

import { create } from "zustand";

import {
  type Citation,
  type Conversation,
  type DoneFrame,
  type ErrorFrame,
  type RefusalFrame,
  type SourceNotice,
  type StartFrame,
  type StoredMessage,
  deleteConversation,
  readConversation,
  renameConversation,
  streamAnswer,
} from "@/lib/api/answers";
import { ApiError } from "@/lib/api/client";

/**
 * The Q&A screen's state, and **why the streaming half is not in the component**.
 *
 * The checklist line is 「流式过程中切换语言不影响正在生成的回答」 — switching the interface
 * language while an answer is being generated must not disturb it. In this product the
 * language lives in the URL (`/es/qa` → `/en/qa`), so switching it is a navigation, and a
 * navigation remounts the page's components: a partial answer held in `useState` would be
 * thrown away, and a `fetch` started from an effect would be cancelled with it.
 *
 * So four things live in a module-scope store rather than in the screen:
 *
 * 1. **the in-flight turn**, updated by a callback the *store* owns, so the request is not
 *    tied to any component's lifetime — unmounting the screen does not abort it;
 * 2. **which conversation is open, and the transcripts already read**, so the new page
 *    after a switch shows the same conversation with the answer still in it;
 * 3. **the composer's text**, so a half-typed question is not lost to §3.2's
 *    「不丢失已填写的表单内容」;
 * 4. **which citation's panel is open**, for the same reason as (2).
 *
 * A turn is keyed by its **message id** as soon as the `start` frame supplies one — the id
 * is what makes a partial answer identifiable across a remount, and it is the id the
 * transcript uses once the same message is read back. Before `start` arrives the turn is
 * keyed by a local id and re-keying is one assignment.
 *
 * **The conversation list is deliberately *not* here.** It is the server component's, so
 * it is on the first paint, and a language switch re-reads it — which is also how a
 * conversation titled by the question that created it appears in the sidebar without the
 * client keeping a second, drifting copy.
 *
 * **Nothing here is written during a render**, which is what keeps the server render
 * deterministic: the module is shared by every request on the server, so a store written
 * while rendering would leak one reader's conversation into the next one's HTML.
 */

/** Where a turn is: still arriving, finished, refused (D20) or failed (`ERR_ANS_001`). */
export type TurnPhase = "streaming" | "complete" | "refused" | "failed";

export type Turn = {
  /** `messageId` once the server named one, otherwise a client id. */
  key: string;
  messageId: string | null;
  conversationId: string | null;
  question: string;
  /** The answer so far. Empty for a refusal: that is rendered from the catalogue. */
  text: string;
  citations: Citation[];
  sourceNotice: SourceNotice | null;
  phase: TurnPhase;
  /** The catalogue key for a refusal or a failure, rendered in the reader's language. */
  messageKey: string | null;
  /** The language the *server* answered in, from the `start` frame. Display only. */
  answerLanguage: string | null;
};

export type OpenCitation = { turnKey: string; index: number };

type QaState = {
  /** `null` means "a new conversation", which is what the composer starts on. */
  selectedId: string | null;
  transcripts: Record<string, StoredMessage[]>;
  loadingTranscript: boolean;
  transcriptFailed: boolean;
  /** Every turn this session has asked, by key. Filtered by conversation when rendered. */
  turns: Record<string, Turn>;
  composer: string;
  openCitation: OpenCitation | null;
  /** Why the last question could not be *started* — a refusal, or the API being down. */
  askError: string | null;

  select: (id: string | null) => Promise<void>;
  setComposer: (text: string) => void;
  openPanel: (turnKey: string, index: number) => void;
  closePanel: () => void;
  ask: (question?: string) => Promise<void>;
  retry: (turnKey: string) => Promise<void>;
  rename: (id: string, title: string) => Promise<Conversation>;
  remove: (id: string) => Promise<void>;
};

/** A client-side id for a turn whose message the server has not named yet. */
function pendingKey(): string {
  return `pending:${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

export const useQaStore = create<QaState>((set, get) => ({
  selectedId: null,
  transcripts: {},
  loadingTranscript: false,
  transcriptFailed: false,
  turns: {},
  composer: "",
  openCitation: null,
  askError: null,

  select: async (id) => {
    // The panel closes on a conversation change: the citation it points at belongs to a
    // turn in the other conversation, and a panel that survived the switch would show a
    // passage from an answer that is no longer on screen.
    set({ selectedId: id, openCitation: null, transcriptFailed: false });
    if (id === null || get().transcripts[id]) return;
    set({ loadingTranscript: true });
    try {
      const detail = await readConversation(id);
      set((state) => ({
        transcripts: { ...state.transcripts, [id]: detail.messages },
        // A turn whose message the transcript now carries stops being a live turn: the
        // stored row has the accounting and the citations, and rendering both would show
        // the same answer twice.
        turns: dropLoaded(state.turns, detail.messages),
      }));
    } catch {
      set({ transcriptFailed: true });
    } finally {
      set({ loadingTranscript: false });
    }
  },

  setComposer: (composer) => set({ composer }),

  openPanel: (turnKey, index) => set({ openCitation: { turnKey, index } }),
  closePanel: () => set({ openCitation: null }),

  ask: async (question) => {
    const state = get();
    const asked = (question ?? state.composer).trim();
    // One stream at a time, which is what the composer's disabled state mirrors: a second
    // request while one is open would be two answers to one screen and two rows whose
    // order the transcript could not explain.
    if (!asked || isStreaming(state.turns)) return;

    const key = pendingKey();
    const askedIn = state.selectedId;
    set((current) => ({
      // A retry passes the question explicitly and must not clear what is being typed.
      composer: question === undefined ? "" : current.composer,
      askError: null,
      turns: {
        ...current.turns,
        [key]: {
          key,
          messageId: null,
          conversationId: askedIn,
          question: asked,
          text: "",
          citations: [],
          sourceNotice: null,
          phase: "streaming",
          messageKey: null,
          answerLanguage: null,
        },
      },
    }));

    // The turn's key moves from its local id to the server's message id on `start`, so the
    // frame handler tracks it rather than assuming `key` still names the turn.
    let current = key;
    try {
      await streamAnswer(asked, askedIn, (frame) => {
        set((state) => {
          const applied = applyFrame(state.turns, current, frame.kind, frame.payload);
          current = applied.key;
          // The `start` frame is also where a *first* question's conversation becomes
          // known, and the screen must select it immediately: the answer is rendered from
          // the turns of the selected conversation, so a turn that moved to its real
          // conversation while nothing was selected would vanish from the thread for the
          // rest of the stream — the answer would appear only once it had finished, which
          // is precisely the buffering this ticket exists to remove.
          const turn = applied.turns[current];
          const landed = turn?.conversationId;
          return {
            turns: applied.turns,
            selectedId: landed && !state.selectedId ? landed : state.selectedId,
          };
        });
      });
    } catch (cause) {
      set((state) => ({
        askError: cause instanceof ApiError ? cause.message : String(cause),
        turns: { ...state.turns, [current]: { ...state.turns[current], phase: "failed" } },
      }));
      return;
    }

    const finished = get().turns[current];
    if (!finished) return;
    // The turn belongs to its real conversation: a first question creates one, and the
    // `start` frame is where its id arrives.
    const landed = finished.conversationId;
    if (!landed) return;
    if (!get().selectedId) set({ selectedId: landed });
    await readInto(set, landed);
  },

  retry: async (turnKey) => {
    const turn = get().turns[turnKey];
    if (!turn) return;
    set((state) => {
      const turns = { ...state.turns };
      delete turns[turnKey];
      return { turns };
    });
    await get().ask(turn.question);
  },

  rename: async (id, title) => {
    // The API's answer, returned rather than stored: the list of conversations belongs to
    // the screen, which re-reads it from the server after this. Keeping a second copy here
    // would be a sidebar that could disagree with `GET /answers/conversations`.
    return renameConversation(id, title);
  },

  remove: async (id) => {
    await deleteConversation(id);
    set((state) => {
      const transcripts = { ...state.transcripts };
      delete transcripts[id];
      return {
        transcripts,
        selectedId: state.selectedId === id ? null : state.selectedId,
        openCitation: null,
      };
    });
  },
}));

// --- frames, and the small rules around them ---------------------------------

/** A frame applied, and the turn's key afterwards — `start` re-keys it to the message id. */
export type Applied = { turns: Record<string, Turn>; key: string };

/**
 * One frame applied to the turn it belongs to.
 *
 * The `delta` case appends and nothing else: the driver's framing guarantees one increment
 * per frame, and a client that re-parsed the whole answer out of `done` would defeat the
 * streaming the ticket is about.
 */
export function applyFrame(
  turns: Record<string, Turn>,
  key: string,
  kind: string,
  payload: Record<string, unknown>,
): Applied {
  const turn = turns[key];
  if (!turn) return { turns, key };

  if (kind === "start") {
    const start = payload as unknown as StartFrame;
    const next = { ...turns };
    delete next[key];
    next[start.message_id] = {
      ...turn,
      key: start.message_id,
      messageId: start.message_id,
      conversationId: start.conversation_id,
      answerLanguage: start.language,
    };
    return { turns: next, key: start.message_id };
  }
  if (kind === "citations") {
    const frame = payload as unknown as {
      citations: Citation[];
      source_notice: SourceNotice | null;
    };
    return {
      key,
      turns: {
        ...turns,
        [key]: {
          ...turn,
          citations: frame.citations ?? [],
          sourceNotice: frame.source_notice ?? null,
        },
      },
    };
  }
  if (kind === "delta") {
    return {
      key,
      turns: { ...turns, [key]: { ...turn, text: turn.text + String(payload.text ?? "") } },
    };
  }
  if (kind === "refusal") {
    // **The catalogue key, not the frame's `content`.** `refusal_text()` is deliberately
    // bilingual — one string carrying Spanish and English — because D20's statement is for
    // a client with no dictionary. A reader of the Spanish interface should be told once,
    // in Spanish (design system §4.4), so the sentence is rendered from
    // `errors.knowledge_base_no_basis` and the bilingual constant is not stored here.
    const frame = payload as unknown as RefusalFrame;
    return {
      key,
      turns: {
        ...turns,
        [key]: {
          ...turn,
          phase: "refused",
          messageKey: frame.message_key,
          citations: [],
          // Explicitly null on a refusal (ticket 36): nothing was quoted, so there is no
          // scope to label, and the banner must not appear.
          sourceNotice: null,
        },
      },
    };
  }
  if (kind === "error") {
    const frame = payload as unknown as ErrorFrame;
    return {
      key,
      turns: { ...turns, [key]: { ...turn, phase: "failed", messageKey: frame.message_key } },
    };
  }
  if (kind === "done") {
    const frame = payload as unknown as DoneFrame;
    return {
      key,
      turns: {
        ...turns,
        [key]: {
          ...turn,
          phase: frame.is_refusal ? "refused" : "complete",
          citations: frame.citations ?? turn.citations,
          sourceNotice: frame.source_notice ?? null,
        },
      },
    };
  }
  // An event this build has not learned is ignored rather than fatal: the turn survives it.
  return { turns, key };
}

/** Live turns minus the ones the stored transcript now carries. */
function dropLoaded(
  turns: Record<string, Turn>,
  messages: StoredMessage[],
): Record<string, Turn> {
  const stored = new Set(messages.map((message) => message.id));
  const next: Record<string, Turn> = {};
  for (const [key, turn] of Object.entries(turns)) {
    if (turn.messageId && stored.has(turn.messageId)) continue;
    next[key] = turn;
  }
  return next;
}

/** Read one conversation back, keep it, and retire the live turn it now duplicates. */
async function readInto(
  set: (partial: (state: QaState) => Partial<QaState>) => void,
  id: string,
): Promise<void> {
  try {
    const detail = await readConversation(id);
    set((state) => ({
      transcripts: { ...state.transcripts, [id]: detail.messages },
      turns: dropLoaded(state.turns, detail.messages),
    }));
  } catch {
    // The turn itself is on screen and complete; the transcript is the record of it. A
    // failed read leaves the answer visible rather than replacing it with an error.
  }
}

/** The turns belonging to one conversation, oldest first. */
export function turnsOf(turns: Record<string, Turn>, conversationId: string | null): Turn[] {
  return Object.values(turns).filter((turn) => turn.conversationId === conversationId);
}

/** Whether a stream is in flight, which is what disables the composer. */
export function isStreaming(turns: Record<string, Turn>): boolean {
  return Object.values(turns).some((turn) => turn.phase === "streaming");
}
