import type { MissingEmployee } from "@/lib/api/payslips";
import { ApiError, API_SERVER_BASE_URL } from "@/lib/api/client";
import { cookies } from "next/headers";

import { DEFAULT_LOCALE, isLocale, type Locale } from "@/lib/i18n/config";
import { getDictionary } from "@/lib/i18n";

import { PayslipScreen } from "./payslip-screen";

/**
 * The payslip reads, on the server, with the session forwarded by hand.
 *
 * **`fetch` rather than `lib/api/client.request`, and that is a decision rather than a
 * workaround.** `request` is the SPA's client: it sets `Content-Type: application/json`
 * whenever there is a body, carries `credentials: "include"`, and is designed for calls a
 * *browser* makes against the API's own origin. This page is a Server Component — the same
 * shape `readServerDocuments` and `readServerNotifications` have, and the same three lines
 * of header plumbing — and what it needs is one thing: the caller's cookie, forwarded
 * explicitly, because the browser's cookie is httpOnly and the server reaches the API on a
 * different host inside the compose network.
 */
async function readServer<T>(path: string): Promise<T> {
  const jar = await cookies();
  const session = jar.get("eam_session");

  const response = await fetch(`${API_SERVER_BASE_URL}${path}`, {
    headers: {
      Accept: "application/json",
      ...(session ? { Cookie: `eam_session=${session.value}` } : {}),
    },
    cache: "no-store",
  });

  if (!response.ok) {
    throw new ApiError(`Request failed with status ${response.status}`, response.status);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/**
 * The month's payslip upload, and the two lists it answers with.
 *
 * Read on the server so the missing list is on the first paint, like the attendance and leave
 * screens: a page that rendered empty and then filled in would make "nobody is missing a
 * payslip" and "we have not looked yet" the same screen for a moment — and on this screen
 * that difference is the whole message.
 *
 * `null` from a read is a state the screen renders rather than an exception it throws: a
 * reader who is not finance gets a 403, which the screen shows as the refusal sentence
 * (design system §6.3's permission state) rather than as a broken uploader. The page itself
 * is not gated, because the API is what decides — and the navigation entry is what keeps the
 * screen from being advertised to somebody it would refuse.
 */
export default async function PayslipsPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  const locale: Locale = isLocale(raw) ? raw : DEFAULT_LOCALE;
  const dict = getDictionary(locale);

  // The previous calendar month, from the local date's components: a payslip is filed for a
  // month that has ended, and a UTC conversion would name the wrong one east of Greenwich.
  const period = previousMonth();

  type Listing = { items: MissingEmployee[] };
  const listing = await attempt(() =>
    readServer<Listing>(`/api/v1/payslips/missing?period=${encodeURIComponent(period)}`),
  );
  const items: MissingEmployee[] | null = listing === null ? null : listing.items;
  const failed = items === null;

  // The selector is a convenience for a file whose name carries no number. A failed read
  // leaves it empty rather than turning the page into an error state.
  const employees =
    (await attempt(() =>
      readServer<Array<{ employee_id: string; employee_no: string | null; employee_name: string }>>(
        `/api/v1/payslips/employees?period=${encodeURIComponent(period)}`,
      ),
    )) ?? [];

  return (
    <div className="flex flex-col gap-8">
      <div className="max-w-3xl">
        <h1 className="text-2xl font-semibold tracking-tight">{dict.payslips.title}</h1>
        <p className="mt-2 text-fg-muted">{dict.payslips.intro}</p>
      </div>

      <PayslipScreen
        dict={dict}
        locale={locale}
        initialPeriod={period}
        initialMissing={items}
        initialMissingFailed={failed}
        employees={employees}
      />
    </div>
  );
}

/** The previous calendar month, `YYYY-MM`. See the call site for why it is computed here. */
function previousMonth(today: Date = new Date()): string {
  const year = today.getFullYear();
  const month = today.getMonth(); // 0-based, so `month` alone is already the previous one.
  const previousYear = month === 0 ? year - 1 : year;
  const previousMonthNumber = month === 0 ? 12 : month;
  return `${previousYear}-${`${previousMonthNumber}`.padStart(2, "0")}`;
}

/**
 * One read, retried once on a *network* failure.
 *
 * Only on a network failure: an `ApiError` carrying a status is the API's answer — a 403 or a
 * 503 is a decision, and asking again would be asking the same question twice for the same
 * answer. `null` means "the read did not happen", which is what the screen's error state is
 * for; an empty list is a different fact, and the two must not be conflated.
 */
async function attempt<T>(read: () => Promise<T>): Promise<T | null> {
  for (let tries = 0; tries < 2; tries += 1) {
    try {
      return await read();
    } catch (error) {
      if (error instanceof ApiError && error.status !== undefined) return null;
    }
  }
  return null;
}
