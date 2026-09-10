/** Run list with light polling while anything is still executing. */

import { useCallback, useEffect, useState } from 'react'

import { api, toDisplayError } from '../../shared/api/client'
import { isTerminal, type Run } from '../../shared/api/types'
import { Alert, Card, Empty, RunStatusBadge, Spinner, formatTime } from '../../shared/ui/primitives'

interface Props {
  onOpen: (runId: string) => void
  onNew: () => void
}

export function RunList({ onOpen, onNew }: Props) {
  const [runs, setRuns] = useState<Run[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<{ message: string; trace: string | null } | null>(null)

  const load = useCallback(async () => {
    try {
      setRuns(await api.listRuns())
      setError(null)
    } catch (err) {
      setError(toDisplayError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // Poll only while work is in flight; an idle console makes no requests.
  useEffect(() => {
    const active = runs.some((run) => !isTerminal(run.status))
    if (!active) return
    const timer = window.setInterval(() => void load(), 4000)
    return () => window.clearInterval(timer)
  }, [runs, load])

  return (
    <div className="stack">
      {error && (
        <Alert tone="error">
          {error.message}
          {error.trace && <div className="trace">trace id: {error.trace}</div>}
        </Alert>
      )}

      <Card
        title="Runs"
        actions={
          <div className="row">
            <button className="btn" onClick={() => void load()}>
              Refresh
            </button>
            <button className="btn primary" onClick={onNew}>
              New analysis
            </button>
          </div>
        }
      >
        {loading ? (
          <Spinner label="Loading runs…" />
        ) : runs.length === 0 ? (
          <Empty>
            No analyses yet. Start one to produce a design document, a test case workbook and a data
            mapping workbook from a COBOL repository.
          </Empty>
        ) : (
          <div className="table-scroll">
            <table className="data">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Repository</th>
                  <th>Request</th>
                  <th>Entry point</th>
                  <th>Commit</th>
                  <th>Artifacts</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const ready = run.artifacts.filter((a) => a.state === 'ready').length
                  const failed = run.artifacts.filter((a) => a.state === 'failed').length
                  return (
                    <tr
                      key={run.id}
                      className="run-row"
                      onClick={() => onOpen(run.id)}
                      tabIndex={0}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') onOpen(run.id)
                      }}
                    >
                      <td>
                        <RunStatusBadge status={run.status} />
                      </td>
                      <td>{run.repo_key}</td>
                      <td style={{ maxWidth: 420 }}>
                        {/* The request is the run's identity now, so it gets the space. The
                            entry point beside it is what the analysis resolved to. */}
                        <span title={run.message}>{run.interface || run.message}</span>
                      </td>
                      <td className="mono">
                        {run.entrypoint || <span className="muted">resolving…</span>}
                      </td>
                      <td className="mono">{run.commit_sha ? run.commit_sha.slice(0, 10) : '—'}</td>
                      <td>
                        {ready} ready
                        {failed > 0 && <span style={{ color: 'var(--danger)' }}>, {failed} failed</span>}
                      </td>
                      <td className="muted">{formatTime(run.created_at)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
