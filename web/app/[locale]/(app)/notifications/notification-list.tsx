"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError } from "@/lib/api/client";
import {
  markAllRead as markAllReadRequest,
  markRead as markReadRequest,
  notificationTitle,
  type AppNotification,
} from "@/lib/api/notifications";
import { formatDateTime, formatNumber } from "@/lib/format";
import type { Dictionary } from "@/lib/i18n";
import type { Locale } from "@/lib/i18n/config";
import { Alert } from "@/lib/ui/alert";
import { Button } from "@/lib/ui/button";

/**
 * The list, and the two buttons that change it.
 *
 * The server component has already read the list; this half exists because
 * marking read is an action. Every item is a `li` of a `ul`, every action is a
 * real `button`, and read state is a word as well as a style — "unread" is not
 * something a colour can say on its own (design system §5).
 *
 * The metadata line is built through `lib/format` with the interface locale:
 * `level` and `round` come out of the payload as numbers, and a raw
 * `toLocaleString()` would follow the browser and print 1.5 where the rest of the
 * page prints 1,5.
 */
export function NotificationList({
  dict,
  locale,
  initialItems,
  total,
  failed,
}: {
  dict: Dictionary;
  locale: Locale;
  initialItems: AppNotification[];
  total: number;
  failed: boolean;
}) {
  const t = dict.notifications;
  const router = useRouter();
  const [items, setItems] = useState(initialItems);
  //: The id being marked, or "all", or null. One at a time: a second click while
  //: the first is in flight would be a second request for the same change.
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The server is the source of truth. A refresh — ours after a write, or the
  // browser's after a navigation — replaces what is on screen.
  useEffect(() => setItems(initialItems), [initialItems]);

  const unread = items.filter((item) => item.read_at === null).length;

  async function readOne(id: string) {
    setPending(id);
    setError(null);
    try {
      const marked = await markReadRequest(id);
      setItems((current) => current.map((item) => (item.id === id ? marked : item)));
      // The badge lives in the shell, which the server rendered.
      router.refresh();
    } catch (cause) {
      setError(errorText(dict, cause));
    } finally {
      setPending(null);
    }
  }

  async function readAll() {
    setPending("all");
    setError(null);
    try {
      await markAllReadRequest();
      const at = new Date().toISOString();
      setItems((current) =>
        current.map((item) => (item.read_at === null ? { ...item, read_at: at } : item)),
      );
      router.refresh();
    } catch (cause) {
      setError(errorText(dict, cause));
    } finally {
      setPending(null);
    }
  }

  if (failed) {
    return (
      <Alert tone="danger" role="alert" title={t.error}>
        <p className="mt-1">{t.emptyHint}</p>
        <Button
          variant="secondary"
          size="sm"
          className="mt-3"
          onClick={() => router.refresh()}
        >
          {t.retry}
        </Button>
      </Alert>
    );
  }

  return (
    <section aria-labelledby="notifications-heading" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="notifications-heading" className="text-lg font-semibold">
          {total === 0
            ? t.empty
            : t.unreadSummary
                .replace("{unread}", formatNumber(unread, locale))
                .replace("{total}", formatNumber(total, locale))}
        </h2>
        <Button
          variant="secondary"
          size="sm"
          onClick={readAll}
          disabled={unread === 0 || pending !== null}
        >
          {pending === "all" ? t.markingAllRead : t.markAllRead}
        </Button>
      </div>

      {error && (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      )}

      {items.length === 0 ? (
        <Alert tone="neutral">{t.emptyHint}</Alert>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) => {
            const isUnread = item.read_at === null;
            return (
              <li
                key={item.id}
                className="rounded-lg border border-border bg-surface p-4 shadow-sm"
              >
                <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                  <div className="min-w-0">
                    <p className="font-medium">{notificationTitle(dict, item.title_key)}</p>
                    <p className="mt-1 text-sm text-fg-muted">{metaLine(item, dict, locale)}</p>
                    {/* The state in words: colour alone is not a status. */}
                    <p className="mt-1 text-sm text-fg-subtle">
                      {isUnread ? t.unreadLabel : t.readLabel}
                    </p>
                  </div>
                  {isUnread && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => readOne(item.id)}
                      disabled={pending !== null}
                    >
                      {pending === item.id ? t.markingRead : t.markRead}
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** Received, level, round — the payload's numbers through the locale's format. */
function metaLine(
  item: AppNotification,
  dict: Dictionary,
  locale: Locale,
): string {
  const t = dict.notifications;
  const parts = [`${t.received} ${formatDateTime(item.created_at, locale)}`];
  const { level, round } = item.payload;
  if (typeof level === "number") parts.push(`${t.level} ${formatNumber(level, locale)}`);
  if (typeof round === "number") parts.push(`${t.round} ${formatNumber(round, locale)}`);
  return parts.join(" · ");
}

/**
 * The reader's language for a failure, from the catalogue key when the frontend
 * knows it and from the API's own sentence when it does not.
 */
function errorText(dict: Dictionary, cause: unknown): string {
  if (cause instanceof ApiError && cause.messageKey) {
    const known = cause.messageKey as keyof Dictionary["errors"];
    if (known in dict.errors) return dict.errors[known];
  }
  return dict.notifications.error;
}
