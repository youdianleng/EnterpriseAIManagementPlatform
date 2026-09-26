/**
 * The notification centre's client.
 *
 * Two things this module owns so no screen has to:
 *
 * 1. **The title keys.** The API stores a bilingual key, never a sentence. This is
 *    the one place that knows which dictionary entry a key names, and a key the
 *    frontend has not learned yet degrades to a generic title instead of a blank
 *    row — the same shape `authErrorKey` uses for error codes.
 * 2. **The payload's fields.** They are structured (`level`, `round`, `outcome`),
 *    so a screen formats them through `lib/format` with the interface locale rather
 *    than printing whatever the JSON happened to hold.
 */

import { request } from "@/lib/api/client";
import type { Dictionary } from "@/lib/i18n";

export type NotificationPayload = {
  approval_request_id?: string;
  round?: number;
  level?: number;
  requester_employee_id?: string;
  /** A token, not a sentence: `approved`, `rejected`, `returned`, `withdrawn`. */
  outcome?: string;
};

export type AppNotification = {
  id: string;
  /** The event, e.g. `approval.approved`. */
  type: string;
  /** The dictionary key the title is rendered from. */
  title_key: string;
  payload: NotificationPayload;
  entity_type: string;
  entity_id: string;
  /** Null while it is still unread; a timestamp once it has been seen. */
  read_at: string | null;
  created_at: string;
  expires_at: string | null;
};

export type NotificationPage = {
  items: AppNotification[];
  total: number;
  limit: number;
  offset: number;
};

export type UnreadCount = { unread: number };

export type ReadAllResult = { marked: number };

/**
 * Which dictionary entry each stored key names.
 *
 * Kept as a table rather than a template string built from the API's key: a key is
 * a contract, and a mapping that invented dictionary paths would render a blank
 * title the day the backend renamed one.
 */
const TITLE_KEYS: Record<string, keyof Dictionary["notifications"]["titles"]> = {
  "notifications.approval.awaiting_decision": "awaitingDecision",
  "notifications.approval.approved": "approved",
  "notifications.approval.rejected": "rejected",
  "notifications.approval.returned": "returned",
  "notifications.approval.withdrawn": "withdrawn",
};

/** The title of a notification, in the reader's language. */
export function notificationTitle(
  dict: Dictionary,
  titleKey: string,
): string {
  const known = TITLE_KEYS[titleKey];
  return dict.notifications.titles[known ?? "unknown"];
}

export function listNotifications(options?: {
  unreadOnly?: boolean;
  limit?: number;
  offset?: number;
}): Promise<NotificationPage> {
  const params = new URLSearchParams();
  if (options?.unreadOnly) params.set("unread_only", "true");
  if (options?.limit !== undefined) params.set("limit", String(options.limit));
  if (options?.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.toString();
  return request<NotificationPage>(`/api/v1/notifications${query ? `?${query}` : ""}`);
}

/** The badge number. */
export function unreadCount(): Promise<UnreadCount> {
  return request<UnreadCount>("/api/v1/notifications/unread-count");
}

export function markRead(notificationId: string): Promise<AppNotification> {
  return request<AppNotification>(`/api/v1/notifications/${notificationId}/read`, {
    method: "POST",
  });
}

export function markAllRead(): Promise<ReadAllResult> {
  return request<ReadAllResult>("/api/v1/notifications/read-all", { method: "POST" });
}
