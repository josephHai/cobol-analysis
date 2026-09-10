/**
 * The single HTTP entry point for the app.
 *
 * Components never call `fetch` directly (CODESTYLE.md §2.3): every request goes through
 * here so the error shape, timeout and trace id are handled in one place.
 *
 * **No credential handling.** The console does not authenticate the caller and holds no
 * token — identity is established by whatever proxies to this app (the Vite dev proxy today,
 * the intranet SSO gateway in production) and the server reads it from a header it trusts.
 * A login step for an internal tool that already sits behind SSO is redundant, and any key
 * kept here would end up in the bundle or in `sessionStorage` on a shared workstation.
 *
 * When the SSO integration lands, this file needs only a token header on `request()` and on
 * the event stream; the server side is already reserved for it (`AUTH_MODE=e2e_token`).
 */

import type {
  ArtifactPreview,
  Health,
  KbQueryResponse,
  RemoteFetchResult,
  RepoInfo,
  Run,
  RunCreateRequest,
  RunLogEntry,
} from './types'

const TIMEOUT_MS = 30_000
/** A workbook can be large; downloading gets its own, longer budget. */
const DOWNLOAD_TIMEOUT_MS = 120_000

export class ApiError extends Error {
  readonly status: number
  readonly traceId: string | null

  constructor(message: string, status: number, traceId: string | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.traceId = traceId
  }
}

/**
 * What a component shows for a failure.
 *
 * Every screen wants the same two fields — the message and the trace id — so the narrowing from
 * `unknown` happens here, once, instead of at each call site.
 */
export function toDisplayError(err: unknown): { message: string; trace: string | null } {
  if (err instanceof ApiError) return { message: err.message, trace: err.traceId }
  if (err instanceof Error) return { message: err.message, trace: null }
  return { message: 'Something went wrong. Quote the trace id when reporting it.', trace: null }
}

/** Fetch with a timeout, so no request can hang the console forever. */
async function fetchWithTimeout(
  path: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(`/api/v1${path}`, { ...init, signal: controller.signal })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      throw new ApiError('The request timed out. The server may be busy — try again.', 0, null)
    }
    throw new ApiError('Cannot reach the API service. Check that it is running.', 0, null)
  } finally {
    window.clearTimeout(timer)
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchWithTimeout(
    path,
    {
      ...init,
      headers: {
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...(init.headers ?? {}),
      },
    },
    TIMEOUT_MS,
  )

  const traceId = response.headers.get('X-Trace-Id')
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}.`
    try {
      const body = (await response.json()) as { detail?: string | Array<{ msg: string }> }
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail)) detail = body.detail.map((d) => d.msg).join('; ')
    } catch {
      // Non-JSON error body: keep the generic message rather than crashing the UI.
    }
    throw new ApiError(detail, response.status, traceId)
  }
  return (await response.json()) as T
}

export const api = {
  health: () => request<Health>('/health'),

  listRepos: () => request<RepoInfo[]>('/repos'),

  /** Pull the latest refs from the repository's remote. Always contacts the remote. */
  fetchRemote: (repoKey: string) =>
    request<RemoteFetchResult>(`/repos/${encodeURIComponent(repoKey)}/fetch`, { method: 'POST' }),

  resolveRef: (repoKey: string, ref?: string) =>
    request<{ commit_sha: string; branches: string[] }>(
      `/repos/${encodeURIComponent(repoKey)}/resolve${ref ? `?ref=${encodeURIComponent(ref)}` : ''}`,
      { method: 'POST' },
    ),

  listRuns: (limit = 50) => request<Run[]>(`/runs?limit=${limit}`),

  getRun: (id: string) => request<Run>(`/runs/${id}`),

  createRun: (body: RunCreateRequest) =>
    request<Run>('/runs', { method: 'POST', body: JSON.stringify(body) }),

  cancelRun: (id: string) => request<{ accepted: boolean; detail: string }>(`/runs/${id}/cancel`, {
    method: 'POST',
  }),

  getLogs: (id: string, limit = 400) =>
    request<RunLogEntry[]>(`/runs/${id}/logs?limit=${limit}`),

  previewArtifact: (id: string, kind: string) =>
    request<ArtifactPreview>(`/runs/${id}/artifacts/${kind}`),

  downloadUrl: (id: string, kind: string) => `/api/v1/runs/${id}/artifacts/${kind}/download`,

  kbQuery: (question: string) =>
    request<KbQueryResponse>('/kb/query', {
      method: 'POST',
      body: JSON.stringify({ question, top_k: 6 }),
    }),
}

/**
 * Download a file.
 *
 * Fetched as a blob rather than via a plain `<a href>` so a failure surfaces as a normal
 * `ApiError` with the server's message and trace id, instead of navigating the browser to a
 * JSON error document.
 */
export async function downloadArtifact(runId: string, kind: string, filename: string): Promise<void> {
  const response = await fetchWithTimeout(api.downloadUrl(runId, kind), {}, DOWNLOAD_TIMEOUT_MS)
  if (!response.ok) {
    throw new ApiError(
      `Download failed with status ${response.status}. The artifact may not be ready.`,
      response.status,
      response.headers.get('X-Trace-Id'),
    )
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
