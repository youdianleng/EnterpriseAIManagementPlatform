/**
 * The answers client: the streamed question, and the conversation's own four verbs.
 *
 * **The stream is a `fetch`, not an `EventSource`, and that is forced.** `EventSource`
 * can only issue a `GET` with no body, and ticket 34's contract is `POST /answers` with
 * `{question, conversation_id?}` — a `GET` would put the question in a URL, in the access
 * log and in every cache between here and the API. So the frames are read off
 * `response.body` and parsed here.
 *
 * **The frame names are the contract, and this module is the only place that knows them.**
 * `start` → `citations` → `delta`×n → `done`, or `refusal` → `done`, or a terminal `error`.
 * Every screen reads `AnswerTurn` and never a raw frame, so a change on the wire is a
 * change in one file.
 *
 * **Nothing here holds state.** The stream is started by the caller and its frames are
 * handed to a callback, which is what lets the Q&A store own the partial answer rather
 * than a component: see `lib/stores/qa-store.ts` for why that is the difference between
 * an answer that survives a language switch and one that does not.
 */

import { API_BASE_URL, ApiError, request, type ApiErrorBody } from "@/lib/api/client";

/** One citation, exactly as the `citations` and `done` frames carry it. */
export type Citation = {
  document_id: string;
  chunk_id: string;
  title: string;
  filename: string;
  /** `false` means somebody's personal upload — §5.2/Q29's marker, per citation. */
  is_company_kb: boolean;
  /** `null` for a format with no pages; a client omits it rather than inventing "p. 1". */
  page: number | null;
  page_to: number | null;
  heading_path: string | null;
  /** `"parent"` or `"child"`: which text `quote` came from. */
  context_scope: string;
  quote: string;
  content: string;
  rerank_score: number;
};

/**
 * §5.2/Q29's 「以下内容来自个人文档」 marker, in every language the interface ships.
 *
 * `text` carries the sentence in each of them, so the banner renders the reader's
 * language without a dictionary lookup; `message_key` is how a client that *has* a
 * dictionary renders its own copy.
 */
export type SourceNotice = {
  personal_documents: boolean;
  message_key: string;
  text: Record<string, string>;
};

/** One stored answer, as `GET /answers/conversations/{id}` returns it. */
export type StoredMessage = {
  id: string;
  question: string;
  content: string;
  citations: Citation[];
  model_used: string | null;
  provider_used: string | null;
  token_in: number;
  token_out: number;
  latency_ms: number;
  is_refusal: boolean;
  error_key: string | null;
  status: string;
  retrieval_filter: string | null;
  source_notice: SourceNotice | null;
  created_at: string;
};

export type Conversation = {
  id: string;
  title: string;
  created_at: string;
  last_message_at: string;
  expires_at: string;
};

export type ConversationDetail = Conversation & {
  messages: StoredMessage[];
  /** Ticket 40's draft: the newest one the assistant proposed in this conversation. */
  draft: Draft | null;
};

/**
 * One field of a draft form (ticket 40), as the API describes it.
 *
 * **The labels travel with the field, in both languages.** The wording lives in the API's
 * own catalogue (`app/core/messages.py`), and the alternative — sending `label_key` and
 * keeping a second copy in this dictionary — is the copy that goes stale. `kind` is which
 * control to draw and `options` the choices of a select, each named in both languages.
 */
export type PrefillField = {
  name: string;
  label_key: string;
  kind: "date" | "time" | "text" | "textarea" | "number" | "select";
  label_es: string;
  label_en: string;
  value: string | number | null;
  required: boolean;
  options: Array<{ value: string; label_es: string; label_en: string }>;
  hint_es: string | null;
  hint_en: string | null;
};

/**
 * A complete, editable form: what the assistant filled in, and where a confirmed one goes.
 *
 * `facts` is what the *validation* answered — the working days a leave costs, the billable
 * answer a time entry resolved to — and it is read-only by construction: nothing in it is a
 * field, because nothing in it is written by the submission.
 */
export type PrefillForm = {
  tool: string;
  entity: "leave_request" | "attendance_correction" | "timesheet_entry";
  title_key: string;
  title_es: string;
  title_en: string;
  submit_path: string;
  fields: PrefillField[];
  facts: Record<string, unknown>;
};

/**
 * The conversation's newest draft, with the status the database's clock implies.
 *
 * `proposed` is the only value that offers confirmation; `expired` means its 24 hours ran
 * out and the employee has to ask for a new one. The form still travels with an expired
 * draft: it is the record of what was proposed.
 */
export type Draft = {
  id: string;
  tool_name: string;
  status: "proposed" | "confirmed" | "rejected" | "expired";
  created_at: string;
  expires_at: string;
  prefill_form: PrefillForm | null;
};

export type ConversationPage = { items: Conversation[]; total: number };

/** The frames, as ticket 34's ticket file writes them out. */
export type StartFrame = {
  message_id: string;
  conversation_id: string;
  question: string;
  model: string;
  provider: string;
  language: string;
};

export type CitationsFrame = {
  citations: Citation[];
  source_notice: SourceNotice | null;
};

export type DeltaFrame = { text: string };

export type RefusalFrame = {
  message_id: string;
  conversation_id: string;
  content: string;
  message_key: string;
  best_score: number;
  threshold: number;
  is_refusal: true;
  model_called: false;
  source_notice: null;
};

export type ErrorFrame = {
  message_id: string;
  conversation_id: string;
  code: string;
  message_key: string;
  retryable: boolean;
};

export type DoneFrame = {
  message_id: string;
  conversation_id: string;
  citations: Citation[];
  source_notice: SourceNotice | null;
  model: string | null;
  provider: string | null;
  token_in: number;
  token_out: number;
  latency_ms: number;
  is_refusal: boolean;
};

export function readConversations(limit = 50): Promise<ConversationPage> {
  return request<ConversationPage>(`/api/v1/answers/conversations?limit=${limit}`);
}

export function readConversation(id: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/api/v1/answers/conversations/${id}`);
}

export function renameConversation(id: string, title: string): Promise<Conversation> {
  return request<Conversation>(`/api/v1/answers/conversations/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export function deleteConversation(id: string): Promise<void> {
  return request<void>(`/api/v1/answers/conversations/${id}`, { method: "DELETE" });
}

/**
 * One `event:`/`data:` frame, as it arrived.
 *
 * `kind` is the raw event name and `payload` the parsed JSON, because the wire *is* the
 * contract and a frame this build does not know has to be ignorable rather than fatal —
 * a server that adds an event must not break a client that has not learned it.
 */
export type Frame = { kind: string; payload: Record<string, unknown> };

export type FrameHandler = (frame: Frame) => void;

/**
 * Ask a question and hand every frame to `onFrame`. Resolves when the stream ends.
 *
 * **The caller owns the abort.** A `signal` cancels the request, and the store uses that
 * to stop a turn the reader has abandoned; nothing here decides when a stream is over.
 *
 * The frames are split on a blank line, which is the SSE separator, and the `data:` line
 * is parsed as JSON. A frame whose payload is not JSON is skipped rather than thrown: a
 * malformed frame is a server bug that the *turn* should survive, because the reader can
 * still read the text that did arrive.
 */
export async function streamAnswer(
  question: string,
  conversationId: string | null,
  onFrame: FrameHandler,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/v1/answers`, {
      method: "POST",
      credentials: "include",
      headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
      body: JSON.stringify(
        conversationId ? { question, conversation_id: conversationId } : { question },
      ),
      signal,
    });
  } catch (cause) {
    if (signal?.aborted) return;
    throw new ApiError(cause instanceof Error ? cause.message : "Network request failed");
  }

  // A refusal to *start* is an ordinary JSON envelope: the permission guard answers 403
  // before any frame exists, so this is where a caller learns it may not ask at all.
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const envelope = (body as { error?: ApiErrorBody } | null)?.error;
    throw new ApiError(
      envelope?.message ?? `Request failed with status ${response.status}`,
      response.status,
      envelope,
    );
  }
  if (!response.body) {
    throw new ApiError("the answer stream carried no body");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split = buffer.indexOf("\n\n");
      while (split !== -1) {
        const frame = parseFrame(buffer.slice(0, split));
        if (frame) onFrame(frame);
        buffer = buffer.slice(split + 2);
        split = buffer.indexOf("\n\n");
      }
    }
    // The last frame of a well-formed stream is followed by a blank line, so anything left
    // here is a truncated body — drained rather than dropped, because a `done` that lost
    // only its trailing newline still carries the message id and the accounting.
    const tail = parseFrame(buffer);
    if (tail) onFrame(tail);
  } finally {
    reader.cancel().catch(() => {
      // The stream is already over; a failed cancel is not a fact anybody can act on.
    });
  }
}

/** One frame's `event:` and `data:` lines, or null when the block carries neither. */
function parseFrame(block: string): Frame | null {
  let kind = "";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    const trimmed = line.endsWith("\r") ? line.slice(0, -1) : line;
    if (trimmed.startsWith("event:")) kind = trimmed.slice("event:".length).trim();
    else if (trimmed.startsWith("data:")) data.push(trimmed.slice("data:".length).trim());
  }
  if (!kind || data.length === 0) return null;
  try {
    return { kind, payload: JSON.parse(data.join("\n")) as Record<string, unknown> };
  } catch {
    // A frame that is not JSON is a frame this client cannot render. Dropping it keeps
    // the frames around it — and the turn — usable.
    return null;
  }
}
