/**
 * Run composer.
 *
 * Three inputs, in the order an operator actually thinks about them: which repository, which
 * revision, and what they want analysed. The third is free text on purpose — naming a
 * PROCEDURE or a PROGRAM-ID up front is a guess, whereas "the customer balance inquiry" is a
 * description the analysis skill can resolve against the code. The skill reports the entry
 * point it settled on, and the run page shows it.
 */

import { useEffect, useState } from 'react'

import { api, toDisplayError } from '../../shared/api/client'
import type { RepoInfo, Run, RunCreateRequest } from '../../shared/api/types'
import { Alert } from '../../shared/ui/primitives'

interface Props {
  onCreated: (run: Run) => void
}

const SKILLS: Array<{ key: NonNullable<RunCreateRequest['skills']>[number]; label: string; hint: string }> = [
  { key: 'fdd', label: 'Functional Design Document', hint: 'Markdown, traceable to code' },
  { key: 'test_case', label: 'Test case workbook', hint: 'One row per case, xlsx' },
  { key: 'data_mapping', label: 'Data mapping workbook', hint: 'Field-level mapping, xlsx' },
]

/** Two sentences is enough to describe an analysis; the field allows far more. */
const EXAMPLE = 'Analyse the customer balance inquiry: what does it do, which programs and transactions does it touch, and what are the business rules behind the balance calculation?'

export function RunComposer({ onCreated }: Props) {
  const [repos, setRepos] = useState<RepoInfo[]>([])
  const [repoKey, setRepoKey] = useState('')
  const [ref, setRef] = useState('')
  const [message, setMessage] = useState('')
  const [skills, setSkills] = useState<string[]>(['fdd', 'test_case', 'data_mapping'])
  const [locale, setLocale] = useState<'en' | 'zh'>('en')

  const [commit, setCommit] = useState('')
  const [branches, setBranches] = useState<string[]>([])
  const [resolving, setResolving] = useState(false)
  const [fetching, setFetching] = useState(false)
  const [fetchNote, setFetchNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<{ message: string; trace: string | null } | null>(null)

  useEffect(() => {
    api
      .listRepos()
      .then((list) => {
        setRepos(list)
        if (list[0]) setRepoKey(list[0].repo_key)
      })
      .catch((err: unknown) => {
        setError(toDisplayError(err))
      })
  }, [])

  const selected = repos.find((r) => r.repo_key === repoKey)

  /**
   * Resolve the revision as soon as a repository is chosen.
   *
   * Showing the pinned commit before the operator commits to a run is deliberate: it is the
   * one fact that makes a later dispute about "which code was analysed" answerable.
   */
  const resolve = async (nextRepo: string, nextRef: string) => {
    if (!nextRepo) return
    setResolving(true)
    setError(null)
    try {
      const result = await api.resolveRef(nextRepo, nextRef || undefined)
      setCommit(result.commit_sha)
      setBranches(result.branches)
    } catch (err) {
      setCommit('')
      setBranches([])
      setError(toDisplayError(err))
    } finally {
      setResolving(false)
    }
  }

  useEffect(() => {
    if (repoKey) void resolve(repoKey, ref)
    // Ref changes are resolved on blur / via the button, not on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoKey])

  const pullFromRemote = async () => {
    if (!repoKey) return
    setFetching(true)
    setError(null)
    setFetchNote('')
    try {
      const result = await api.fetchRemote(repoKey)
      setFetchNote(
        `Pulled ${result.branch_count} branch(es) in ${(result.duration_ms / 1000).toFixed(1)}s` +
          (result.default_branch ? ` · ${result.default_branch} at ${result.default_branch_sha.slice(0, 10)}` : ''),
      )
      // Re-resolve so the revision and branch list reflect what was just pulled.
      await resolve(repoKey, ref)
    } catch (err) {
      setError(toDisplayError(err))
    } finally {
      setFetching(false)
    }
  }

  const submit = async () => {
    if (!repoKey || message.trim().length < 8) return
    setSubmitting(true)
    setError(null)
    try {
      const run = await api.createRun({
        repo_key: repoKey,
        ref: ref || null,
        message: message.trim(),
        skills: skills as RunCreateRequest['skills'],
        locale,
      })
      onCreated(run)
    } catch (err) {
      setError(toDisplayError(err))
    } finally {
      setSubmitting(false)
    }
  }

  const canSubmit = Boolean(repoKey) && message.trim().length >= 8 && skills.length > 0

  return (
    <div className="stack">
      {error && (
        <Alert tone="error">
          {error.message}
          {error.trace && <div className="trace">trace id: {error.trace}</div>}
        </Alert>
      )}

      <section className="card">
        <h2>Repository</h2>
        <div className="grid-2">
          <label className="field">
            Repository
            <select value={repoKey} onChange={(e) => setRepoKey(e.target.value)}>
              {repos.length === 0 && <option value="">No repositories configured</option>}
              {repos.map((repo) => (
                <option key={repo.repo_key} value={repo.repo_key}>
                  {repo.name} ({repo.repo_key})
                </option>
              ))}
            </select>
            {selected && (
              <span className="hint">
                {selected.url || 'URL not set'}{' '}
                {selected.has_credential ? '· credential configured' : '· anonymous'}
              </span>
            )}
          </label>

          <label className="field">
            Branch, tag or commit <span className="hint">empty uses {selected?.default_branch ?? 'the default branch'}</span>
            <div className="row">
              <input
                list="branches"
                value={ref}
                placeholder={selected?.default_branch ?? 'main'}
                onChange={(e) => setRef(e.target.value)}
                onBlur={() => void resolve(repoKey, ref)}
              />
              <button className="btn" type="button" onClick={() => void pullFromRemote()} disabled={fetching || !repoKey}>
                {fetching ? 'Pulling…' : 'Pull from remote'}
              </button>
            </div>
            <datalist id="branches">
              {branches.map((b) => (
                <option key={b} value={b} />
              ))}
            </datalist>
          </label>
        </div>

        {fetchNote && (
          <div className="alert info" style={{ marginTop: 'var(--s3)', marginBottom: 0 }}>
            {fetchNote}
          </div>
        )}

        <div
          className={commit ? 'alert info' : 'alert warn'}
          style={{ marginTop: 'var(--s3)', marginBottom: 0 }}
        >
          {resolving ? (
            'Resolving revision…'
          ) : commit ? (
            <>
              Pinned revision: <span className="mono">{commit}</span> — the exact code the
              deliverables will be traceable to.
            </>
          ) : (
            'No revision resolved yet. Pick a repository — the default branch is resolved automatically.'
          )}
        </div>
      </section>

      <section className="card">
        <h2>What should be analysed?</h2>
        <label className="field">
          Request
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            rows={5}
            autoFocus
            placeholder={EXAMPLE}
            style={{ resize: 'vertical', fontFamily: 'inherit' }}
          />
          <span className="hint">
            Describe the interface, transaction or program in your own words. The analysis
            resolves which code that refers to and reports the entry point it chose — so you do
            not need to know a PROGRAM-ID up front. Naming one is fine if you do.
          </span>
        </label>
        {message.trim().length > 0 && message.trim().length < 8 && (
          <div className="alert warn" style={{ marginTop: 'var(--s3)', marginBottom: 0 }}>
            Describe the request in a little more detail — the analysis has nothing to resolve yet.
          </div>
        )}
      </section>

      <section className="card">
        <h2>Deliverables</h2>
        <div className="stack" style={{ gap: 'var(--s2)' }}>
          {SKILLS.map((skill) => (
            <label key={skill.key} className="row" style={{ gap: 'var(--s3)', fontWeight: 400 }}>
              <input
                type="checkbox"
                style={{ width: 'auto' }}
                checked={skills.includes(skill.key)}
                onChange={(e) =>
                  setSkills((prev) =>
                    e.target.checked ? [...prev, skill.key] : prev.filter((s) => s !== skill.key),
                  )
                }
              />
              <span>
                <strong>{skill.label}</strong> <span className="muted">— {skill.hint}</span>
              </span>
            </label>
          ))}
        </div>

        <label className="field" style={{ marginTop: 'var(--s4)', maxWidth: 260 }}>
          Output language
          <select value={locale} onChange={(e) => setLocale(e.target.value as 'en' | 'zh')}>
            <option value="en">English</option>
            <option value="zh">Chinese</option>
          </select>
          <span className="hint">The project baseline is English; other languages are opt-in.</span>
        </label>
      </section>

      <div className="row">
        <button className="btn primary" onClick={() => void submit()} disabled={submitting || !canSubmit}>
          {submitting ? 'Starting…' : 'Start analysis'}
        </button>
        <span className="muted">
          Runs execute in the background; you can close this and follow progress from the run list.
        </span>
      </div>
    </div>
  )
}
