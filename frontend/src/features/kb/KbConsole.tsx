/** Knowledge base console: ask a question, get a grounded answer plus citations. */

import { useState } from 'react'

import { api, toDisplayError } from '../../shared/api/client'
import type { KbQueryResponse } from '../../shared/api/types'
import { Alert, Card, Empty, Spinner } from '../../shared/ui/primitives'

interface Props {
  /** Opens a cited run. Citations can come from any indexed run. */
  onOpenRun?: (runId: string) => void
}

const EXAMPLES = [
  'Which program reads ACCT_NO from the CUSTOMER table?',
  'What happens when the customer record is not found?',
  'Which fields are mapped from the copybook to the interface?',
]

export function KbConsole({ onOpenRun }: Props) {
  const [question, setQuestion] = useState('')
  const [result, setResult] = useState<KbQueryResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<{ message: string; trace: string | null } | null>(null)

  const ask = async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed) return
    setLoading(true)
    setError(null)
    try {
      setResult(await api.kbQuery(trimmed))
    } catch (err) {
      setError(toDisplayError(err))
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="stack">
      {error && (
        <Alert tone="error">
          {error.message}
          {error.trace && <div className="trace">trace id: {error.trace}</div>}
        </Alert>
      )}

      <Card title="Ask the knowledge base">
        <div className="row" style={{ alignItems: 'flex-start' }}>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void ask(question)
            }}
            placeholder={EXAMPLES[0]}
          />
          <button className="btn primary" onClick={() => void ask(question)} disabled={loading || !question.trim()}>
            {loading ? 'Searching…' : 'Ask'}
          </button>
        </div>
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          Searching every indexed run. Name a program, field, rule id or interface for the best
          match.
        </p>
        <div className="row" style={{ marginTop: 'var(--s3)' }}>
          {EXAMPLES.map((example) => (
            <button
              key={example}
              className="btn"
              style={{ fontSize: 12 }}
              onClick={() => {
                setQuestion(example)
                void ask(example)
              }}
            >
              {example}
            </button>
          ))}
        </div>
      </Card>

      {loading && <Spinner label="Retrieving evidence…" />}

      {result && (
        <Card title="Answer">
          {/* The answer text is assembled server-side from retrieved evidence and is
              rendered as plain text — never as HTML. */}
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              font: 'inherit',
              margin: 0,
              background: 'var(--surface-2)',
              padding: 'var(--s4)',
              borderRadius: 'var(--radius-sm)',
            }}
          >
            {result.answer}
          </pre>
          <p className="muted" style={{ fontSize: 12 }}>
            Retriever: <span className="mono">{result.provider}</span> · {result.citations.length}{' '}
            citation(s)
          </p>

          {result.citations.length > 0 && (
            <>
              <h3>Citations</h3>
              <div className="stack" style={{ gap: 'var(--s2)' }}>
                {result.citations.map((citation, index) => (
                  <div
                    key={`${citation.run_id}-${index}`}
                    className="card"
                    style={{ margin: 0, padding: 'var(--s3) var(--s4)' }}
                  >
                    <div className="spread">
                      <strong style={{ fontSize: 13 }}>{citation.title}</strong>
                      <span className="badge pending">{citation.kind}</span>
                    </div>
                    <div className="mono muted" style={{ fontSize: 11, margin: '4px 0' }}>
                      run {citation.run_id}
                      {citation.commit_sha && ` · commit ${citation.commit_sha.slice(0, 10)}`}
                      {citation.locator && ` · ${citation.locator}`}
                    </div>
                    <div style={{ fontSize: 12.5 }}>{citation.snippet}</div>
                    {onOpenRun && (
                      <button
                        className="link"
                        style={{ fontSize: 12, marginTop: 6 }}
                        onClick={() => onOpenRun(citation.run_id)}
                      >
                        Open run →
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </Card>
      )}

      {!result && !loading && (
        <Empty>
          Answers are assembled only from generated artifacts. Every statement carries a citation you
          can trace back to a run, a commit and a location, so the console never asserts something the
          analysis did not produce.
        </Empty>
      )}
    </div>
  )
}
