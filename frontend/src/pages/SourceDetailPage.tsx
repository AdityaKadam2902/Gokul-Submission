/* Source details: registry metadata + snapshot history (immutably stored). */
import { useEffect, useState } from 'react'
import { api, type SourceDetailRead } from '../api/client'
import { ErrorState, FixtureBadge, LoadingState, Panel, PolicyBadge, formatTimestamp, shortHash } from '../components/ui'
import type { Nav } from '../routing'

export function SourceDetailPage({ id, nav }: { id: number; nav: Nav }) {
  const [source, setSource] = useState<SourceDetailRead | null>(null)
  const [error, setError] = useState<{ title: string; message: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const load = async () => {
    setError(null)
    try {
      setSource(await api.getSource(id))
    } catch (err) {
      setError({ title: 'Unable to load source', message: err instanceof Error ? err.message : String(err) })
    }
  }
  useEffect(() => { void load() }, [id])

  const ingest = async () => {
    setBusy(true)
    setActionError(null)
    try {
      const result = await api.ingest(id)
      await load()
      if (result.snapshot_id) nav({ page: 'snapshot', id: result.snapshot_id })
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (error) {
    return (
      <Panel>
        <ErrorState title={error.title} message={error.message} retry={() => void load()} />
        <div style={{ textAlign: 'center', paddingBottom: 16 }}>
          <button className="btn" onClick={() => nav({ page: 'sources' })}>Back to sources</button>
        </div>
      </Panel>
    )
  }
  if (!source) return <Panel><LoadingState label="Loading source…" /></Panel>

  const snapshots = source.snapshots ?? []

  return (
    <div>
      <div className="breadcrumb"><a onClick={() => nav({ page: 'sources' })}>Sources</a><span className="sep">/</span>source #{id}</div>
      <div className="page-head-row">
        <div>
          <h1 className="page-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {source.label ?? (source.fixture_id ? `Fixture ${source.fixture_id}` : source.url ?? `Source ${id}`)}
            {source.kind === 'fixture' && <FixtureBadge />}
          </h1>
          <p className="page-sub">{source.kind === 'fixture' ? 'Synthetic fixture source — content is invented and labelled; never fetched from a live site.' : 'Declared-URL source — never fetched: policy approval is a documented manual procedure.'}</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" onClick={() => void load()}>Refresh</button>
          <button className="btn primary" disabled={busy || source.policy_status === 'blocked'} onClick={() => void ingest()}>
            {busy ? 'Ingesting…' : snapshots.length ? 'Re-ingest' : 'Ingest'}
          </button>
        </div>
      </div>

      {actionError && <div className="alert error">{actionError}</div>}

      <Panel title="Registration metadata">
        <dl className="kv">
          <dt>Kind</dt><dd><code className="hash">{source.kind}</code></dd>
          <dt>Fixture id</dt><dd>{source.fixture_id ?? '—'}</dd>
          <dt>URL</dt><dd>{source.url ?? '—'}</dd>
          <dt>Page type</dt><dd>{source.page_type}</dd>
          <dt>Policy status</dt><dd><PolicyBadge policy={source.policy_status} /></dd>
          <dt>Robots status</dt><dd>{source.robots_status}</dd>
          <dt>Intended fields (D6 allowlist)</dt>
          <dd>{source.intended_fields.length ? source.intended_fields.map((f) => <span className="chip" key={f}>{f}</span>) : <span className="muted">none declared</span>}</dd>
          <dt>Approval note</dt><dd>{source.approval_note ?? '—'}</dd>
          <dt>Reviewed by</dt><dd>{source.reviewed_by ?? '—'}</dd>
          <dt>Registered</dt><dd>{formatTimestamp(source.created_at)}</dd>
          <dt>Updated</dt><dd>{formatTimestamp(source.updated_at)}</dd>
        </dl>
      </Panel>

      <Panel title={`Snapshot history (${snapshots.length})`}>
        {snapshots.length === 0 ? (
          <div className="state">
            <h3>No snapshots yet</h3>
            <p>Run an ingestion to create the first snapshot of this source.</p>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }} className="panel-body tight">
            <table className="data">
              <thead>
                <tr><th>#</th><th>Retrieved</th><th>Title</th><th>Content hash</th><th>Parser</th><th></th></tr>
              </thead>
              <tbody>
                {snapshots.map((snap) => (
                  <tr key={snap.id} className="rowlink" onClick={() => nav({ page: 'snapshot', id: snap.id })}>
                    <td><span className="muted">#{snap.id}</span></td>
                    <td>{formatTimestamp(snap.fetched_at)}</td>
                    <td>{snap.title ?? '—'}</td>
                    <td><code className="hash">{shortHash(snap.content_hash)}</code></td>
                    <td className="small muted">{snap.parser_version}</td>
                    <td><button className="btn sm" onClick={(e) => { e.stopPropagation(); nav({ page: 'snapshot', id: snap.id }) }}>Open</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  )
}
