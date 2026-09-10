/**
 * Artifact preview: the panel the run page opens for one artifact.
 *
 * Two deliberate choices:
 *
 * 1. **Workbooks arrive as rows, not as a binary.** The server converts the XLSX and
 *    this component renders a table, so the browser never parses an untrusted binary
 *    (DESIGN.md §4.7).
 * 2. **Markdown is rendered through `rehype-sanitize`.** Generated documents embed
 *    text taken from repository contents, which is untrusted input. Without the
 *    sanitiser a crafted comment inside a COBOL source file could inject markup into
 *    the console.
 */

import { useEffect, useState } from 'react'
import Markdown from 'react-markdown'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'

import { api, toDisplayError } from '../../shared/api/client'
import type { ArtifactKind, ArtifactPreview, WorkbookSheet } from '../../shared/api/types'
import { Alert, Spinner } from '../../shared/ui/primitives'

interface Props {
  runId: string
  kind: ArtifactKind
}

export function ArtifactPanel({ runId, kind }: Props) {
  const [preview, setPreview] = useState<ArtifactPreview | null>(null)
  const [error, setError] = useState<{ message: string; trace: string | null } | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .previewArtifact(runId, kind)
      .then((result) => {
        if (!cancelled) setPreview(result)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(toDisplayError(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [runId, kind])

  if (loading) return <Spinner label="Loading artifact…" />
  if (error) {
    return (
      <Alert tone="error">
        {error.message}
        {error.trace && <div className="trace">trace id: {error.trace}</div>}
      </Alert>
    )
  }
  if (!preview) return <Alert tone="warn">Artifact is not available.</Alert>

  if (preview.type === 'markdown') {
    return (
      <div className="markdown">
        <Markdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
          {preview.content}
        </Markdown>
      </div>
    )
  }

  if (preview.type === 'workbook') {
    return (
      <div className="stack">
        {preview.sheets.map((sheet) => (
          <SheetTable key={sheet.name} sheet={sheet} />
        ))}
      </div>
    )
  }

  if (preview.type === 'json') {
    return (
      <pre className="mono" style={{ maxHeight: '60vh', overflow: 'auto', margin: 0 }}>
        {JSON.stringify(preview.content, null, 2)}
      </pre>
    )
  }

  return (
    <pre className="mono" style={{ maxHeight: '60vh', overflow: 'auto', margin: 0 }}>
      {preview.content}
    </pre>
  )
}

function SheetTable({ sheet }: { sheet: WorkbookSheet }) {
  return (
    <div>
      <h3>
        {sheet.name}{' '}
        <span className="muted" style={{ textTransform: 'none', letterSpacing: 0 }}>
          ({sheet.rows.length} rows)
        </span>
      </h3>
      <div className="table-scroll">
        <table className="data">
          <thead>
            <tr>
              <th style={{ width: 42 }}>#</th>
              {sheet.columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sheet.rows.map((row, index) => (
              // Rows have no natural key; the index is stable for a given preview.
              <tr key={index}>
                <td className="muted mono">{index + 1}</td>
                {sheet.columns.map((_, colIndex) => (
                  <td key={colIndex}>{renderCell(row[colIndex])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {sheet.truncated && (
        <p className="muted" style={{ fontSize: 12 }}>
          The preview stops at {sheet.rows.length} rows; the workbook holds the full data set.
        </p>
      )}
    </div>
  )
}

function renderCell(value: string | number | boolean | null | undefined) {
  if (value === null || value === undefined || value === '') return <span className="muted">—</span>
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}
