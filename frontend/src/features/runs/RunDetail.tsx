/** Run detail: live pipeline progress, event timeline, logs and artifact preview. */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { api, downloadArtifact, toDisplayError } from '../../shared/api/client'
import type { Artifact, ArtifactKind, Run, RunLogEntry } from '../../shared/api/types'
import { isTerminal } from '../../shared/api/types'
import { useRunEvents } from '../../shared/hooks/useRunEvents'
import {
  Alert,
  ArtifactBadge,
  Card,
  formatBytes,
  formatDuration,
  formatTime,
  Spinner,
} from '../../shared/ui/primitives'
import { ArtifactPanel } from './ArtifactPanel'

interface Props {
  runId: string
  onBack: () => void
}

type Tab = 'artifacts' | 'timeline' | 'logs'

const TIMELINE_MAX = 120

export function RunDetail({ runId, onBack }: Props) {
  const [run, setRun] = useState<Run | null>(null)
  const [error, setError] = useState<{ message: string; trace: string | null } | null>(null)
  const [tab, setTab] = useState<Tab>('artifacts')
  const [selected, setSelected] = useState<ArtifactKind | null>(null)
  const [logs, setLogs] = useState<RunLogEntry[]>([])

  const load = useCallback(async () => {
    try {
      const fresh = await api.getRun(runId)
      setRun(fresh)
      setError(null)
      return fresh
    } catch (err) {
      setError(toDisplayError(err))
      return null
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load])

  // The stream only tells us *that* something changed; the REST snapshot stays the
  // source of truth so a missed frame cannot leave the UI permanently stale.
  const { events, connected, reconnects, error: streamError } = useRunEvents(
    runId,
    () => void load(),
    run?.status,
  )

  useEffect(() => {
    if (tab !== 'logs') return
    void api.getLogs(runId).then(setLogs).catch(() => undefined)
  }, [tab, runId, events.length])

  const timeline = useMemo(() => {
    return events
      .filter((e) => e.event !== 'log')
      .slice(-TIMELINE_MAX)
      .reverse()
  }, [events])

  if (!run) {
    return error ? (
      <Alert tone="error">
        {error.message}
        {error.trace && <div className="trace">trace id: {error.trace}</div>}
        <div style={{ marginTop: 'var(--s3)' }}>
          <button className="btn" onClick={onBack}>
            Back to runs
          </button>
        </div>
      </Alert>
    ) : (
      <Spinner label="Loading run…" />
    )
  }

  const done = isTerminal(run.status)
  const currentPhase = run.phases.find((p) => p.status === 'running')
  const readyArtifacts = run.artifacts.filter((a) => a.state === 'ready' && a.filename)
  const selectedArtifact = run.artifacts.find((a) => a.kind === selected)

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="row" style={{ marginBottom: 'var(--s2)' }}>
            <button className="btn" onClick={onBack}>
              ← Runs
            </button>
            <span className="badge pending mono">{run.id}</span>
          </div>
          <h1>
            {run.interface || run.message.slice(0, 80)} <span className="muted">· {run.repo_key}</span>
          </h1>
          <p>
            {/* The entry point is a result, not an input: the analysis chose it from the
                request. Shown as "resolving" until the analysis phase reports it. */}
            Target{' '}
            {run.entrypoint ? (
              <span className="mono">{run.entrypoint}</span>
            ) : (
              <span className="muted">resolved during analysis…</span>
            )}
            {run.commit_sha && (
              <>
                {' · pinned at '}
                <span className="mono">{run.commit_sha.slice(0, 12)}</span>
              </>
            )}
            {' · locale '}
            <span className="mono">{run.locale}</span>
          </p>
        </div>
        <div className="row">
          {!done && (
            <button
              className="btn"
              onClick={() => void api.cancelRun(runId).then(() => void load())}
            >
              Cancel
            </button>
          )}
          <button className="btn" onClick={() => void load()}>
            Refresh
          </button>
        </div>
      </div>

      {error && <Alert tone="error">{error.message}</Alert>}
      {streamError && <Alert tone="warn">{streamError}</Alert>}
      {run.status === 'failed' && run.error && (
        <Alert tone="error">
          <strong>Run failed.</strong> {run.error}
        </Alert>
      )}

      <Card title="Request">
        {/* Verbatim, because it is the run's actual input and the thing a reviewer will want to
            compare against the deliverables. */}
        <pre
          style={{
            whiteSpace: 'pre-wrap',
            font: 'inherit',
            margin: 0,
            background: 'var(--surface-2)',
            padding: 'var(--s3) var(--s4)',
            borderRadius: 'var(--radius-sm)',
          }}
        >
          {run.message || '(no request recorded)'}
        </pre>
      </Card>

      <Card
        title="Pipeline"
        actions={
          <span className="muted" style={{ fontSize: 12 }}>
            {connected ? (
              <>
                <span className="dot pulse" style={{ background: 'var(--ok)', display: 'inline-block' }} /> live
              </>
            ) : done ? (
              'finished'
            ) : reconnects > 0 ? (
              `reconnecting (attempt ${reconnects})`
            ) : (
              'connecting…'
            )}
          </span>
        }
      >
        <div className="pipeline">
          {run.phases.map((phase) => (
            <div className="phase" key={phase.name} data-status={phase.status}>
              <span className="marker" aria-hidden="true">
                {phase.status === 'done' ? '✓' : phase.status === 'failed' ? '✕' : ''}
              </span>
              <span>
                <span className="label">{phase.label}</span>{' '}
                <span className="detail">{phase.detail}</span>
              </span>
              <span className="detail mono">{formatDuration(phase.duration_ms)}</span>
            </div>
          ))}
        </div>

        {currentPhase && (
          <div style={{ marginTop: 'var(--s4)' }}>
            <div className="bar">
              <i style={{ width: `${Math.round(currentPhase.progress * 100)}%` }} />
            </div>
          </div>
        )}

        <dl className="kv" style={{ marginTop: 'var(--s5)' }}>
          <dt>Status</dt>
          <dd>
            <span className={`badge ${done ? 'done' : 'running'}`}>{run.status}</span>
          </dd>
          <dt>Started</dt>
          <dd>{formatTime(run.started_at)}</dd>
          <dt>Finished</dt>
          <dd>{formatTime(run.finished_at)}</dd>
          {run.usage.duration_s !== undefined && (
            <>
              <dt>Duration</dt>
              <dd>{run.usage.duration_s}s</dd>
            </>
          )}
          {run.usage.tool_calls !== undefined && (
            <>
              <dt>Tool calls</dt>
              <dd>
                {run.usage.tool_calls} across {run.usage.llm_calls ?? 0} model calls
              </dd>
            </>
          )}
          {run.usage.total_tokens ? (
            <>
              <dt>Tokens</dt>
              <dd>{run.usage.total_tokens.toLocaleString()}</dd>
            </>
          ) : null}
          <dt>Actor</dt>
          <dd className="mono">{run.actor}</dd>
        </dl>
      </Card>

      <Card
        title="Deliverables"
        actions={
          <div className="row">
            {(['artifacts', 'timeline', 'logs'] as Tab[]).map((name) => (
              <button
                key={name}
                className={`btn${tab === name ? ' primary' : ''}`}
                onClick={() => setTab(name)}
              >
                {name === 'artifacts' ? 'Artifacts' : name === 'timeline' ? 'Timeline' : 'Logs'}
              </button>
            ))}
          </div>
        }
      >
        {tab === 'artifacts' && (
          <ArtifactGrid
            artifacts={run.artifacts}
            runId={runId}
            selected={selected}
            onSelect={setSelected}
            ready={readyArtifacts.length}
          />
        )}

        {tab === 'timeline' && (
          <div className="timeline">
            {timeline.length === 0 && <span className="muted">No events yet.</span>}
            {timeline.map((item) => (
              <div className="item" key={item.seq}>
                <span className="ts">{formatTime(item.ts)}</span>
                <span className="kind">{item.event}</span>
                <span className="msg">{describeEvent(item.event, item.data)}</span>
              </div>
            ))}
          </div>
        )}

        {tab === 'logs' && (
          <div className="timeline">
            {logs.length === 0 && <span className="muted">No log entries yet.</span>}
            {logs.map((entry) => (
              <div className="item" key={entry.seq} data-level={entry.level}>
                <span className="ts">{formatTime(entry.ts)}</span>
                <span className="kind">{entry.level}</span>
                <span className="msg">{entry.message}</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      {tab === 'artifacts' && selected && (
        <Card
          title={selectedArtifact?.label ?? selected}
          actions={
            <div className="row">
              {selectedArtifact?.filename && (
                <button
                  className="btn"
                  onClick={() =>
                    void downloadArtifact(runId, selected, `${runId}-${selectedArtifact.filename}`)
                  }
                >
                  Download
                </button>
              )}
              <button className="btn" onClick={() => setSelected(null)}>
                Close
              </button>
            </div>
          }
        >
          <ArtifactPanel runId={runId} kind={selected} />
        </Card>
      )}
    </div>
  )
}

function describeEvent(event: string, data: Record<string, unknown>): string {
  if (event === 'phase') {
    return `${String(data.label ?? data.phase)} → ${String(data.status)}${
      data.detail ? ` (${String(data.detail)})` : ''
    }`
  }
  if (event === 'artifact') {
    return `${String(data.kind)} → ${String(data.state)}${data.summary ? ` · ${String(data.summary)}` : ''}`
  }
  if (event === 'tool') {
    if (data.name === 'usage') {
      return `usage: ${data.usage ? JSON.stringify(data.usage) : ''}`
    }
    return `${String(data.name)} ${data.ok === false ? '(failed)' : ''}`
  }
  if (event === 'run.status') return String(data.status)
  if (event === 'done') return `run ${String(data.status)}${data.error ? ` — ${String(data.error)}` : ''}`
  if (event === 'error') return String(data.message ?? JSON.stringify(data))
  return JSON.stringify(data).slice(0, 200)
}

function ArtifactGrid({
  artifacts,
  runId,
  selected,
  onSelect,
  ready,
}: {
  artifacts: Artifact[]
  runId: string
  selected: ArtifactKind | null
  onSelect: (kind: ArtifactKind) => void
  ready: number
}) {
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        {ready} of {artifacts.length} artifacts available. Intermediate artifacts come from the
        analysis skill; the last three are the deliverables.
      </p>
      <div className="grid-2">
        {artifacts.map((artifact) => {
          const openable = artifact.state === 'ready' && artifact.filename
          return (
            <div
              key={artifact.kind}
              className="card"
              style={{
                margin: 0,
                padding: 'var(--s4)',
                cursor: openable ? 'pointer' : 'default',
                borderColor: selected === artifact.kind ? 'var(--primary)' : undefined,
              }}
              onClick={() => openable && onSelect(artifact.kind)}
            >
              <div className="spread" style={{ marginBottom: 'var(--s2)' }}>
                <strong style={{ fontSize: 13 }}>{artifact.label}</strong>
                <ArtifactBadge state={artifact.state} />
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {artifact.state === 'failed'
                  ? artifact.error
                  : artifact.summary || 'not generated yet'}
              </div>
              {artifact.filename && (
                <div className="mono muted" style={{ fontSize: 11, marginTop: 'var(--s2)' }}>
                  {artifact.filename}
                  {artifact.size_bytes > 0 && ` · ${formatBytes(artifact.size_bytes)}`}
                </div>
              )}
            </div>
          )
        })}
      </div>
      <p className="muted" style={{ fontSize: 12 }}>
        Run directory: <span className="mono">{runId}</span>
      </p>
    </>
  )
}
