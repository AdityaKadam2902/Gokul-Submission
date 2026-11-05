/* Candidate facts explorer: view and filter candidate facts across all snapshots (M2). */
import { useEffect, useState } from 'react'
import { api, type FactRead } from '../api/client'
import { ErrorState, LoadingState, Panel } from '../components/ui'
import type { Nav } from '../routing'

export function FactsPage({ nav }: { nav: Nav }) {
  const [facts, setFacts] = useState<FactRead[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<{ title: string; message: string } | null>(null)
  const [statusFilter, setStatusFilter] = useState<string>('all')

  const loadFacts = async () => {
    setLoading(true)
    setError(null)
    try {
      const filter = statusFilter === 'all' ? undefined : statusFilter
      const items = await api.listFacts({ status: filter })
      setFacts(items)
    } catch (err) {
      setError({ title: 'Unable to load candidate facts', message: err instanceof Error ? err.message : String(err) })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadFacts()
  }, [statusFilter])

  return (
    <div>
      <div className="page-head-row">
        <div>
          <h1 className="page-title">Candidate Facts</h1>
          <p className="page-sub">
            All AI-extracted facts remain unapproved candidates. Python verifies evidence quotations, offsets, and source allowlists.
          </p>
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <label style={{ fontSize: 13, fontWeight: 550, color: 'var(--text-2)' }}>Filter Status:</label>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            style={{ padding: '6px 10px', borderRadius: 8, border: '1px solid var(--border-strong)', background: 'var(--surface)' }}
          >
            <option value="all">All Statuses</option>
            <option value="candidate">Candidate (Verified)</option>
            <option value="needs_review">Needs Review</option>
            <option value="rejected">Rejected (Evidence Mismatch / Disallowed)</option>
          </select>
          <button className="btn sm" onClick={() => void loadFacts()}>Refresh</button>
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <span className="badge candidate" style={{ fontSize: 12 }}>
          UNAPPROVED CANDIDATE PROPOSALS · HUMAN REVIEW REQUIRED (M4 AUDIT QUEUE)
        </span>
      </div>

      {error ? (
        <Panel>
          <ErrorState title={error.title} message={error.message} retry={() => void loadFacts()} />
        </Panel>
      ) : loading ? (
        <Panel><LoadingState label="Loading candidate facts…" /></Panel>
      ) : facts.length === 0 ? (
        <Panel>
          <div className="state">
            <h3>No facts found</h3>
            <p>
              {statusFilter !== 'all'
                ? `No candidate facts found matching status '${statusFilter}'.`
                : 'No candidate facts extracted yet. Ingest a fixture and trigger AI extraction on its snapshot.'}
            </p>
          </div>
        </Panel>
      ) : (
        <Panel title={`Candidate facts (${facts.length})`}>
          <table className="data">
            <thead>
              <tr>
                <th>Status</th>
                <th>Snapshot</th>
                <th>Field Key</th>
                <th>Scope</th>
                <th>Raw Published Value</th>
                <th>Normalised</th>
                <th>Evidence Quote &amp; Offsets</th>
                <th>Model &amp; Prompt</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {facts.map((f) => (
                <tr key={f.id}>
                  <td>
                    <span className={`badge ${f.status}`}>{f.status}</span>
                    {f.needs_review_reason && (
                      <div className="small muted" style={{ marginTop: 2 }}>
                        {f.needs_review_reason}
                      </div>
                    )}
                  </td>
                  <td>
                    <a
                      onClick={() => nav({ page: 'snapshot', id: f.snapshot_id })}
                      style={{ color: 'var(--accent-strong)', cursor: 'pointer', fontWeight: 600 }}
                    >
                      Snapshot #{f.snapshot_id}
                    </a>
                  </td>
                  <td><code className="chip">{f.field_key}</code></td>
                  <td><span className="small">{f.scope_key || 'general'}</span></td>
                  <td><div style={{ fontWeight: 550 }}>{f.raw_value}</div></td>
                  <td><span className="small muted">{f.normalized_value ?? '—'}</span></td>
                  <td>
                    {f.evidence_quote ? (
                      <div>
                        <div style={{ fontStyle: 'italic', fontSize: 12 }}>"{f.evidence_quote}"</div>
                        {f.evidence_start_offset != null && f.evidence_end_offset != null && (
                          <code className="hash" style={{ fontSize: 11 }}>
                            [{f.evidence_start_offset}..{f.evidence_end_offset})
                          </code>
                        )}
                      </div>
                    ) : (
                      <span className="muted small">No evidence</span>
                    )}
                  </td>
                  <td>
                    <span className="small mono">{f.model_tag}</span>
                    <div className="small muted">{f.prompt_version}</div>
                  </td>
                  <td>
                    <button
                      className="btn sm"
                      onClick={() => nav({ page: 'snapshot', id: f.snapshot_id })}
                    >
                      View Snapshot
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  )
}
