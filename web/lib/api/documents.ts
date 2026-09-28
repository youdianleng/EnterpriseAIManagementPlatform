/**
 * The documents client.
 *
 * Two things this module owns so no screen has to:
 *
 * 1. **The wire shapes**, including the two fields the *server* computes about
 *    progress — `stage` and `total_stages`. A screen that derived "1 of 2" from the
 *    status string would be a second implementation of the pipeline's shape, and the
 *    two would disagree the first time a status was added.
 * 2. **The multipart upload.** The file is a `File` from an `<input type="file">` and
 *    everything else is a form field, so the browser sets the boundary and the
 *    `Content-Type` — a client that set that header itself would produce a body the
 *    server cannot parse. `request()` cannot be used here for exactly that reason: it
 *    sets `Content-Type: application/json` whenever there is a body.
 */

import { API_BASE_URL, ApiError, request, type ApiErrorBody } from "@/lib/api/client";

export type DocumentStatus = "processing" | "ready" | "failed" | "archived";

export type AppDocument = {
  id: string;
  title: string;
  owner_employee_id: string | null;
  department_id: string | null;
  clearance_level: string;
  visibility: string;
  is_company_kb: boolean;
  category: string | null;
  tags: string[];
  language: string;
  status: DocumentStatus;
  filename: string;
  media_type: string;
  file_size: number;
  content_sha256: string;
  extracted_chars: number;
  page_count: number | null;
  chunk_count: number;
  /** The pipeline's own sentence — `no text extracted; upload a text version` for a scan. */
  failure_reason: string | null;
  uploaded_by_employee_id: string | null;
  parsed_at: string | null;
  created_at: string;
  updated_at: string;
  stage: number;
  total_stages: number;
  chunking_version: string;
};

export type DocumentPage = {
  items: AppDocument[];
  total: number;
  limit: number;
  offset: number;
};

export type UploadFields = {
  title: string;
  departmentId?: string;
  clearanceLevel?: string;
  category?: string;
  tags?: string;
  language?: string;
};

export function readDocuments(limit = 50): Promise<DocumentPage> {
  return request<DocumentPage>(`/api/v1/documents?limit=${limit}`);
}

export function readDocument(id: string): Promise<AppDocument> {
  return request<AppDocument>(`/api/v1/documents/${id}`);
}

export function reprocessDocument(id: string): Promise<AppDocument> {
  return request<AppDocument>(`/api/v1/documents/${id}/reprocess`, { method: "POST" });
}

/**
 * The original file, as a URL the browser can follow.
 *
 * A link rather than a fetch: the session travels in an httpOnly cookie the browser
 * attaches to a navigation, and the response is an attachment. Downloading it through
 * script would mean holding a 50 MB body in memory to hand the user a blob the
 * browser can already stream.
 */
export function documentContentUrl(id: string): string {
  return `${API_BASE_URL}/api/v1/documents/${id}/content`;
}

/**
 * The original, opened at the page a citation came from.
 *
 * **No second endpoint.** §5.2's rule is 「前端可点击回链到原文（PDF 定位到页）」, and the
 * fragment `#page=N` is the documented way a PDF viewer is told which page to start on —
 * so the citation links to the route that already exists and adds the anchor. `null` for
 * `page` (a text file, a spreadsheet, Markdown) means no anchor rather than `#page=1`: an
 * invented page number is worse than none, which is the same rule the API follows when it
 * answers `null`.
 *
 * The route currently answers `Content-Disposition: attachment` — ticket 31's deliberate
 * choice, so a stored file the server has not inspected is never rendered by the
 * browser's own viewer. Whether a browser honours the fragment therefore depends on how
 * it handles the download, and the ticket file records that honestly rather than claiming
 * a jump this client cannot guarantee.
 */
export function documentPageUrl(id: string, page: number | null): string {
  const base = documentContentUrl(id);
  return page === null ? base : `${base}#page=${page}`;
}

/** Upload one file with its metadata. A 409 means the caller already has these bytes. */
export async function uploadDocument(file: File, fields: UploadFields): Promise<AppDocument> {
  const body = new FormData();
  body.append("file", file);
  body.append("title", fields.title);
  if (fields.departmentId) body.append("department_id", fields.departmentId);
  if (fields.clearanceLevel) body.append("clearance_level", fields.clearanceLevel);
  if (fields.category) body.append("category", fields.category);
  if (fields.tags) body.append("tags", fields.tags);
  if (fields.language) body.append("language", fields.language);

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/v1/documents`, {
      method: "POST",
      body,
      // No `Content-Type`: the browser writes the multipart boundary itself.
      credentials: "include",
      headers: { Accept: "application/json" },
    });
  } catch (cause) {
    throw new ApiError(cause instanceof Error ? cause.message : "Network request failed");
  }

  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const envelope = (payload as { error?: ApiErrorBody } | null)?.error;
    if (envelope) throw new ApiError(envelope.message, response.status, envelope);
    throw new ApiError(`Request failed with status ${response.status}`, response.status);
  }
  return (await response.json()) as AppDocument;
}
