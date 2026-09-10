/**
 * Live run events over SSE.
 *
 * Reconnection is left to `EventSource`: it retries by itself and re-sends `Last-Event-ID`, which
 * is what the server's replay cursor reads. An earlier revision closed the stream on every error
 * and reimplemented the retry with its own timers, which disabled the browser's own recovery and
 * then had to reproduce it. What is kept here is the part the browser does not offer: a count of
 * failed attempts, and a resync of REST state when the run settles.
 *
 * No credential is attached. Identity comes from the proxy in front of the app, and the
 * browser sends no token — which is also why this endpoint no longer needs a query-string
 * credential, where a secret would have ended up in access logs.
 */

import { useEffect, useRef, useState } from 'react'

import { isTerminal, type Run, type RunEvent } from '../api/types'

export interface EventStreamState {
  events: RunEvent[]
  connected: boolean
  /** Connection errors seen since the run started; the browser retries on its own. */
  reconnects: number
  error: string | null
}

const MAX_EVENTS = 500
/** After this many consecutive failures, say so instead of waiting silently. */
const GIVE_UP_AFTER = 8

export function useRunEvents(
  runId: string | null,
  onSettled: (() => void) | undefined,
  initialStatus: Run['status'] | undefined,
): EventStreamState {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [connected, setConnected] = useState(false)
  const [reconnects, setReconnects] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const settled = useRef(false)

  // Keep the callback in a ref so a re-render does not tear down the stream.
  const settledHandler = useRef(onSettled)
  useEffect(() => {
    settledHandler.current = onSettled
  }, [onSettled])

  useEffect(() => {
    if (!runId) return
    settled.current = false
    setEvents([])
    setReconnects(0)
    setError(null)
    if (initialStatus && isTerminal(initialStatus)) {
      // Nothing more will be emitted; the caller already has the final state.
      settled.current = true
      return
    }

    const source = new EventSource(`/api/v1/runs/${runId}/events`)
    let failures = 0

    source.onopen = () => {
      setConnected(true)
      setError(null)
      failures = 0
      setReconnects(0)
    }

    const handle = (raw: MessageEvent<string>) => {
      try {
        const envelope = JSON.parse(raw.data) as RunEvent
        setEvents((prev) => {
          const next = [...prev, envelope]
          return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next
        })
        if (envelope.event === 'done') {
          settled.current = true
          setConnected(false)
          source.close()
          // Re-sync from the server: the stream is a notification channel, the
          // REST snapshot is the source of truth.
          settledHandler.current?.()
        }
      } catch {
        // Ignore malformed frames rather than breaking the stream.
      }
    }

    for (const name of ['run.status', 'phase', 'tool', 'log', 'artifact', 'error', 'done']) {
      source.addEventListener(name, handle as EventListener)
    }

    source.onerror = () => {
      setConnected(false)
      if (settled.current) return
      failures += 1
      setReconnects(failures)
      if (failures >= GIVE_UP_AFTER) {
        setError(
          'Live updates stopped after repeated connection failures. Reload the page to resync.',
        )
      }
    }

    return () => {
      source.close()
      setConnected(false)
    }
  }, [runId, initialStatus])

  return { events, connected, reconnects, error }
}
