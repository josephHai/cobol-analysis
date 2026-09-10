/**
 * Console shell: navigation and view routing.
 *
 * Routing is deliberately a small piece of state rather than a router library: the console
 * has five views and one parameter. If deep links become a requirement, this is the one file
 * that changes.
 *
 * There is no authentication step. Identity is established by the proxy in front of the app
 * (the Vite dev proxy, or the SSO gateway in production), and a rejected caller shows up as a
 * normal API error rather than a login screen.
 */

import { useEffect, useState } from 'react'

import { api, toDisplayError } from './shared/api/client'
import type { Health, Run } from './shared/api/types'
import { Alert, Card } from './shared/ui/primitives'
import { KbConsole } from './features/kb/KbConsole'
import { RunComposer } from './features/runs/RunComposer'
import { RunDetail } from './features/runs/RunDetail'
import { RunList } from './features/runs/RunList'

type View =
  | { name: 'runs' }
  | { name: 'new' }
  | { name: 'run'; id: string }
  | { name: 'kb' }
  | { name: 'about' }

export function App() {
  const [view, setView] = useState<View>({ name: 'runs' })
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)

  useEffect(() => {
    api
      .health()
      .then((h) => {
        setHealth(h)
        setHealthError(null)
      })
      .catch((err: unknown) => {
        setHealthError(toDisplayError(err).message)
      })
  }, [])

  const openRun = (id: string) => setView({ name: 'run', id })

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <strong>COBOL Analysis</strong>
          <span>Legacy system intelligence</span>
        </div>

        <nav className="nav">
          <button aria-current={view.name === 'runs'} onClick={() => setView({ name: 'runs' })}>
            Runs
          </button>
          <button aria-current={view.name === 'new'} onClick={() => setView({ name: 'new' })}>
            New analysis
          </button>
          <button aria-current={view.name === 'kb'} onClick={() => setView({ name: 'kb' })}>
            Knowledge base
          </button>
          <button aria-current={view.name === 'about'} onClick={() => setView({ name: 'about' })}>
            Service status
          </button>
        </nav>

        <div style={{ marginTop: 'auto', fontSize: 12 }} className="muted">
          {healthError ? (
            <span style={{ color: 'var(--danger)' }}>API unreachable</span>
          ) : health ? (
            <>
              <div>
                model <span className="mono">{health.model}</span>
              </div>
              <div>
                {health.repos} repo(s) · {health.kb.chunks} chunks
              </div>
              <div>{health.disk_usage_mb} MB on disk</div>
            </>
          ) : (
            'connecting…'
          )}
          {/* Identity comes from the proxy in front of the app, so there is nothing for the
              operator to enter here. Stated explicitly because an empty sidebar with no
              hint reads like a broken login. */}
          <div style={{ marginTop: 8 }}>authenticated by the gateway</div>
        </div>
      </aside>

      <main className="main">
        {view.name === 'runs' && <RunList onOpen={openRun} onNew={() => setView({ name: 'new' })} />}

        {view.name === 'new' && (
          <>
            <div className="page-head">
              <div>
                <h1>New analysis</h1>
                <p>
                  Pick a pinned revision, name the entry point, and the pipeline produces a design
                  document, a test case workbook and a data mapping workbook.
                </p>
              </div>
            </div>
            <RunComposer
              onCreated={(run: Run) => {
                setView({ name: 'run', id: run.id })
              }}
            />
          </>
        )}

        {view.name === 'run' && (
          <RunDetail runId={view.id} onBack={() => setView({ name: 'runs' })} />
        )}

        {view.name === 'kb' && (
          <>
            <div className="page-head">
              <div>
                <h1>Knowledge base</h1>
                <p>Query the deliverables that finished runs produced.</p>
              </div>
            </div>
            <KbConsole onOpenRun={openRun} />
          </>
        )}

        {view.name === 'about' && <About health={health} error={healthError} />}
      </main>
    </div>
  )
}

function About({ health, error }: { health: Health | null; error: string | null }) {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Service status</h1>
          <p>What the API reports about itself.</p>
        </div>
      </div>
      {error && <Alert tone="error">{error}</Alert>}
      {health && (
        <Card title="Runtime">
          <dl className="kv">
            <dt>Status</dt>
            <dd>{health.status}</dd>
            <dt>Model</dt>
            <dd className="mono">{health.model}</dd>
            <dt>Gateway</dt>
            <dd className="mono">{health.llm_base_url}</dd>
            <dt>Data root</dt>
            <dd className="mono">{health.data_root}</dd>
            <dt>Repositories</dt>
            <dd>{health.repos}</dd>
            <dt>Disk usage</dt>
            <dd>{health.disk_usage_mb} MB</dd>
            <dt>Indexed runs</dt>
            <dd>{health.kb.runs_indexed}</dd>
            <dt>Chunks</dt>
            <dd>
              {health.kb.chunks} via <span className="mono">{health.kb.retriever}</span>
            </dd>
          </dl>
        </Card>
      )}
    </>
  )
}
