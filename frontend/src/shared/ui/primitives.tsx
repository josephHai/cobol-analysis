/** Small presentational primitives shared across features. */

import type { ReactNode } from 'react'

import type { ArtifactState, RunStatus } from '../api/types'

type Tone = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

/** Status is never conveyed by colour alone — the label always carries the meaning. */
export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`badge ${tone}`}>
      <i className={`dot${tone === 'running' ? ' pulse' : ''}`} aria-hidden="true" />
      {children}
    </span>
  )
}

const RUN_TONE: Record<RunStatus, Tone> = {
  queued: 'pending',
  fetching: 'running',
  analyzing: 'running',
  generating: 'running',
  validating: 'running',
  archiving: 'running',
  indexing_kb: 'running',
  completed: 'done',
  failed: 'failed',
  cancelled: 'skipped',
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  return <Badge tone={RUN_TONE[status]}>{status.replace(/_/g, ' ')}</Badge>
}

const ARTIFACT_TONE: Record<ArtifactState, Tone> = {
  pending: 'pending',
  running: 'running',
  ready: 'done',
  failed: 'failed',
}

export function ArtifactBadge({ state }: { state: ArtifactState }) {
  return <Badge tone={ARTIFACT_TONE[state]}>{state}</Badge>
}

export function Card({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="spread" style={{ marginBottom: 'var(--s4)' }}>
          {title ? <h2 style={{ margin: 0 }}>{title}</h2> : <span />}
          {actions}
        </div>
      )}
      {children}
    </section>
  )
}

export function Alert({ tone = 'info', children }: { tone?: 'info' | 'warn' | 'error'; children: ReactNode }) {
  const role = tone === 'error' ? 'alert' : 'status'
  return (
    <div className={`alert ${tone}`} role={role}>
      {children}
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function Spinner({ label }: { label: string }) {
  return (
    <div className="row muted">
      <span className="dot pulse" style={{ background: 'var(--primary)' }} />
      {label}
    </div>
  )
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString()
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}
