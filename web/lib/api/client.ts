/**
 * Typed client for the backend API.
 *
 * The browser reaches the API directly, so the base URL must be host-facing
 * (see NEXT_PUBLIC_API_URL). Server-side rendering uses the same URL, which
 * works because the host is also reachable from inside the web container.
 */

/**
 * Where the browser reaches the API.
 *
 * Host-facing on purpose: frontend and API are separate origins and the browser
 * calls the API directly, so this must be a URL the *browser* can resolve. See
 * the compose service for why it is not the compose network name.
 */
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/**
 * Where the server reaches the API.
 *
 * Under `docker compose` the web container does not have the API on its own
 * `localhost` — that is the container itself, which is why a Server Component
 * forwarding the session cookie needs the compose service name. Set
 * `API_INTERNAL_URL` for that. When it is unset (next dev on the host, where
 * `localhost` is the API) the public URL is used unchanged.
 */
export const API_SERVER_BASE_URL =
  process.env.API_INTERNAL_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** The error envelope every failing endpoint returns (see api/app/core/exception_handlers.py). */
export type ApiErrorBody = {
  code: string;
  message_key: string;
  /** Already rendered in Spanish by the API; the fallback when a key is unknown. */
  message: string;
  detail: string | null;
  request_id: string | null;
  timestamp: string;
  fields?: Array<{ field: string; message: string; type: string }>;
};

/**
 * A failed request, carrying the catalogue key as well as the sentence.
 *
 * Callers that only need to say "it failed" keep reading `message` and `status`;
 * callers that render bilingual text read `messageKey` and `code`. Both live on
 * one error type so no caller has to guess which one it received.
 */
export class ApiError extends Error {
  readonly status?: number;
  readonly code?: string;
  readonly messageKey?: string;
  readonly detail?: string | null;
  readonly requestId?: string | null;

  constructor(message: string, status?: number, error?: ApiErrorBody) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = error?.code;
    this.messageKey = error?.message_key;
    this.detail = error?.detail;
    this.requestId = error?.request_id;
  }
}

function isErrorEnvelope(value: unknown): value is { error: ApiErrorBody } {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const body = (value as { error: unknown }).error;
  return typeof body === "object" && body !== null && "code" in body;
}

async function readError(response: Response): Promise<ApiError> {
  // A failing request can still fail to be JSON: a proxy error page, a truncated
  // body. The status is the part that is always there, so it is the fallback.
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (isErrorEnvelope(body)) {
    return new ApiError(body.error.message, response.status, body.error);
  }
  return new ApiError(`Request failed with status ${response.status}`, response.status);
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      // The session travels in an httpOnly cookie, so every call has to opt in
      // to sending it. Defaulting to "same-origin" would silently drop it.
      credentials: "include",
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch (cause) {
    // Network-level failure: the API is down or unreachable.
    throw new ApiError(cause instanceof Error ? cause.message : "Network request failed");
  }

  if (!response.ok) {
    throw await readError(response);
  }

  // 204 and other empty bodies are legitimate responses (logout, end-all).
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export type AppInfo = {
  name: string;
  version: string;
  environment: string;
  api_prefix: string;
};

export function fetchAppInfo(): Promise<AppInfo> {
  // Consistent with every other call: the browser reaches a different origin,
  // and a request that forgets `credentials` silently loses the session.
  return request<AppInfo>("/api/v1/info");
}
