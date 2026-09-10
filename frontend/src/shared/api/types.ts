/**
 * API types: a hand-maintained mirror of `contracts/openapi.json` (the contract itself, checked
 * for drift by `python scripts/dev.py contracts-check`). Generating them with
 * `openapi-typescript` is the intended end state (CONTRIBUTING.md §2); until that is wired in,
 * this file is the only place that describes the wire format, so components never reach into raw
 * shapes themselves. A change here and not in `contracts/` — or the reverse — is a defect.
 */

export type RunStatus =
  | 'queued'
  | 'fetching'
  | 'analyzing'
  | 'generating'
  | 'validating'
  | 'archiving'
  | 'indexing_kb'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type PhaseStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export type ArtifactKind =
  | 'progress'
  | 'call_flow'
  | 'code_slice'
  | 'business_rules'
  | 'fdd'
  | 'test_case'
  | 'data_mapping'

export type ArtifactState = 'pending' | 'running' | 'ready' | 'failed'

export interface Phase {
  name: string
  label: string
  status: PhaseStatus
  progress: number
  detail: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
}

export interface Artifact {
  kind: ArtifactKind
  label: string
  state: ArtifactState
  filename: string | null
  size_bytes: number
  version: number
  summary: string
  error: string | null
  updated_at: string
}

export interface Run {
  id: string
  repo_key: string
  repo_url: string
  ref: string | null
  commit_sha: string | null
  /** The operator's request, verbatim. The pipeline's primary input. */
  message: string
  /** Derived by the analysis skill, not supplied — see RunComposer. */
  entrypoint: string
  /** A short label derived from the message, used in headings. */
  interface: string
  skills: string[]
  locale: string
  status: RunStatus
  phases: Phase[]
  artifacts: Artifact[]
  actor: string
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  usage: Record<string, number>
}

export interface RepoInfo {
  repo_key: string
  name: string
  url: string
  default_branch: string
  description: string
  has_credential: boolean
  cached: boolean
  last_fetched_at: string | null
}

export interface RunCreateRequest {
  repo_key: string
  ref?: string | null
  /** What to analyse, in the operator's words. The only scope input. */
  message: string
  skills?: Array<'fdd' | 'test_case' | 'data_mapping'>
  locale?: 'en' | 'zh'
}

export interface RemoteFetchResult {
  repo_key: string
  url: string
  default_branch: string
  default_branch_sha: string
  branches: string[]
  branch_count: number
  fetched_at: string
  duration_ms: number
}

export interface WorkbookSheet {
  name: string
  columns: string[]
  rows: Array<Array<string | number | boolean | null>>
  truncated?: boolean
}

export type ArtifactPreview =
  | { kind: ArtifactKind; type: 'markdown'; content: string }
  | { kind: ArtifactKind; type: 'workbook'; filename: string; sheets: WorkbookSheet[] }
  | { kind: ArtifactKind; type: 'json'; content: unknown }
  | { kind: ArtifactKind; type: 'text'; content: string }

export interface KbCitation {
  run_id: string
  repo_key: string
  commit_sha: string | null
  kind: ArtifactKind
  title: string
  locator: string
  snippet: string
}

export interface KbQueryResponse {
  answer: string
  citations: KbCitation[]
  provider: string
}

/** One line of the flattened log view (`GET /runs/{id}/logs`). */
export interface RunLogEntry {
  seq: number
  ts: string
  level: string
  message: string
}

export interface Health {
  status: string
  data_root: string
  repos: number
  model: string
  llm_base_url: string
  disk_usage_mb: number
  kb: { chunks: number; runs_indexed: number; by_kind: Record<string, number>; retriever: string }
}

/** A single envelope from the run event stream (docs/DESIGN.md §4.4). */
export interface RunEvent {
  seq: number
  ts: string
  event:
    | 'run.status'
    | 'phase'
    | 'tool'
    | 'log'
    | 'artifact'
    | 'error'
    | 'done'
  data: Record<string, unknown>
}

export const TERMINAL_STATUSES: RunStatus[] = ['completed', 'failed', 'cancelled']

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL_STATUSES.includes(status)
}
