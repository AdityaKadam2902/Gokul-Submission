/* Sources overview + list: registration form (fixture-first), action table.
   All data comes from the backend; nothing is fabricated. */
import { useEffect, useState } from 'react'
import { ApiError, api, FIXTURE_OPTIONS, type SourceRead } from '../api/client'
import {
  Badge, EmptyState, ErrorState, FixtureBadge, LoadingState, Panel, PolicyBadge,
  formatTimestamp, shortHash,
} from '../components/ui'
import type { Nav } from '../routing'

type Loadable<T> = { kind: 'loading' } | { kind: 'ready'; data: T } | { kind: 'error'; message: string }

export function SourcesPage({ nav }: { nav: Nav }) {
  const [state, setState] = useState<Loadable<SourceRead[]>>({ kind: 'loading' })
  const [actionError, setActionError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)

  const load = async () => {
    setState({ kind: 'loading' })
    try {
      setState({ kind: 'ready', data: await api.listSources() })
    } catch (err) {
      setState({ kind: 'error', message: err instanceof Error ? err.message : String(err) })
    }
  }
  useEffect(() => { void load() }, [])

  const act = async (run: () => Promise<unknown>, successMsg: string) => {
    setActionError(null)
    try {
      await run()
      await load()
      setActionError(null)
      if (successMsg) setActionError(`${successMsg}`.startsWith('OK:') ? successMsg : '')
      void successMsg
    } catch (err) {
      if (err instanceof ApiError) setActionError(`Action failed (${err.code}): ${err.message}`)
      else setActionError(`Action failed: ${String(err)}`)
    }
  }

  const ingest = async (s: SourceRead) => {
    setActionError(null)
    try {
      const result = await api.ingest(s.id)
      await load()
      if (result.snapshot_id) nav({ page: 'snapshot', id: result.snapshot_id })
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    }
  }

  const remove = async (s: SourceRead) => {
    if (!window.confirm(`Delete source #${s.id} and its snapshots? This cascades per the retention policy (D8).`)) return
    await act(async () => { await api.deleteSource(s.id) }, '')
  }

  return (
    <div>
      <div className="page-head-row">
        <div>
          <h1 className="page-title">Sources</h1>
          <p className="page-sub">
            Registry of ingestation sources. Live collection requires manual policy approval —
            in this milestone only clearly-labelled synthetic fixtures are ingested.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" onClick={() => void load()}>Refresh</button>
          <button className="btn primary" onClick={() => setShowForm((v) => !v)}>{showForm ? 'Close form' : 'Register source'}</button>
        </div>
      </div>

      {actionError && <div className="alert error">{actionError}</div>}

      {showForm && <RegisterForm onRegistered={(id) => { void (async () => { setShowForm(false); await load(); nav({ page: 'source', id }) })() }} />}

      {state.kind === 'loading' && <Panel><LoadingState label="Loading sources…" /></Panel>}
      {state.kind === 'error' && (
        <Panel>
          <ErrorState
            title="Backend unavailable"
            message={state.message}
            retry={() => void load()}
          />
        </Panel>
      )}
      {state.kind === 'ready' && (
        <Panel title={`Sources (${state.data.length})`}>
          {state.data.length === 0 ? (
            <EmptyState
              title="No sources registered yet"
              hint="Register a synthetic fixture source to exercise the ingestion pipeline."
            />
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="data">
                <thead>
                  <tr>
                    <th>ID</th><th>Source</th><th>Page type</th><th>Provenance</th><th>Policy</th><th>Intended fields</th><th>Last snapshot</th><th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {state.data.map((s) => (
                    <tr key={s.id} className="rowlink" onClick={() => nav({ page: 'source', id: s.id })}>
                      <td><span className="muted">#{s.id}</span></td>
                      <td>
                        <strong>{s.label ?? (s.fixture_id ? `Fixture ${s.fixture_id}` : (s.url ?? '—'))}</strong>
                        <div className="muted small">{s.kind === 'fixture' ? `fixture corpus ${s.fixture_id ?? ''}` : s.domain ?? ''}</div>
                      </td>
                      <td>{s.page_type}</td>
                      <td>{s.kind === 'fixture' ? <FixtureBadge /> : <Badge kind="neutral">declared URL</Badge>}</td>
                      <td><PolicyBadge policy={s.policy_status} /></td>
                      <td>
                        <div>
                          {s.intended_fields.length === 0
                            ? <span className="muted small">none declared</span>
                            : s.intended_fields.map((f) => <span className="chip" key={f}>{f}</span>)}
                        </div>
                      </td>
                      <td>
                        <div className="small muted">{formatTimestamp(s.last_snapshot_at)}</div>
                        <code className="hash">{shortHash(s.last_snapshot_hash)}</code>
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                          <button className="btn sm primary" onClick={() => void ingest(s)}>{s.last_snapshot_id ? 'Re-ingest' : 'Ingest'}</button>
                          {s.last_snapshot_id && (
                            <button className="btn sm" onClick={() => nav({ page: 'snapshot', id: s.last_snapshot_id! })}>Snapshot</button>
                          )}
                          <button className="btn sm danger-ghost" onClick={() => void remove(s)}>Delete</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}
    </div>
  )
}

function RegisterForm({ onRegistered }: { onRegistered: (id: number) => void }) {
  const [kind, setKind] = useState<'fixture' | 'url'>('fixture')
  const [fixtureId, setFixtureId] = useState<string>('F3')
  const [pageType, setPageType] = useState<string>('contact')
  const [label, setLabel] = useState('')
  const [url, setUrl] = useState('')
  const [fields, setFields] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const onFixtureChange = (id: string) => {
    setFixtureId(id)
    const found = FIXTURE_OPTIONS.find((f) => f.id === id)
    if (found) setPageType(found.archetype)
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const payload = kind === 'fixture'
        ? {
            kind: 'fixture' as const,
            page_type: pageType,
            fixture_id: fixtureId,
            label: label.trim() || null,
            intended_fields: fields.split(',').map((s) => s.trim()).filter(Boolean),
            approval_note: note.trim() || null,
          }
        : {
            kind: 'url' as const,
            page_type: pageType,
            url: url.trim() || null,
            label: label.trim() || null,
            intended_fields: fields.split(',').map((s) => s.trim()).filter(Boolean),
            approval_note: note.trim() || null,
          }
      const created = await api.createSource(payload)
      onRegistered(created.id)
    } catch (err) {
      setError(err instanceof Error ? `${err.message}` : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel title="Register source">
      {error && <div className="alert error small">{error}</div>}
      <form onSubmit={submit}>
        <div className="form-grid">
          <label className="field">
            Source kind
            <select value={kind} onChange={(e) => setKind(e.target.value as 'fixture' | 'url')}>
              <option value="fixture">Synthetic fixture (recommended in M1)</option>
              <option value="url">Declared URL (registered as manual_fixture_pending_policy_check)</option>
            </select>
            <span className="hint">URL sources are registered for documentation only; they are never fetchable in M1 (no approved_live sources exist).</span>
          </label>

          {kind === 'fixture' ? (
            <label className="field">
              Fixture (from the M0 synthetic corpus)
              <select value={fixtureId} onChange={(e) => onFixtureChange(e.target.value)}>
                {FIXTURE_OPTIONS.map((f) => <option key={f.id} value={f.id}>{f.id} — {f.label}</option>)}
              </select>
            </label>
          ) : (
            <label className="field">
              URL
              <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://… (never fetched in M1)" />
            </label>
          )}

          <label className="field">
            Page type
            <select value={pageType} onChange={(e) => setPageType(e.target.value)}>
              {['home', 'services', 'contact', 'team', 'vacancies', 'about', 'other'].map((p) => <option key={p}>{p}</option>)}
            </select>
          </label>
          <label className="field">
            Label (optional)
            <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder='e.g. "Alara Park synthetic contact page"' />
          </label>
          <label className="field">
            Intended fields (comma-separated; data-minimisation allowlist, D6)
            <textarea value={fields} onChange={(e) => setFields(e.target.value)} rows={2} placeholder="organisation_name, public_phone, public_address" />
          </label>
          <label className="field full">
            Approval note (optional)
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Context recorded with the registration" />
          </label>
        </div>
        <div style={{ marginTop: 14, display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="btn primary" type="submit" disabled={busy}>{busy ? 'Registering…' : 'Register source'}</button>
          <span className="hint">Ingestion is policy-gated in the backend; nothing here triggers network access.</span>
        </div>
      </form>
    </Panel>
  )
}
