/* AI Prompt Runs Audit log (M2): inspect Ollama executions, latencies, tokens and errors. */
import { useEffect, useState } from 'react'
import { api, type PromptRunRead } from '../api/client'
import { ErrorState, LoadingState, Panel, formatTimestamp } from '../components/ui'
import type { Nav } from '../routing'

export function PromptRunsPage({ nav }: { nav: Nav }) {
  const [runs, setRuns] = useState<PromptRunRead[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<{ title: string; message: string } | null>(null)

  const loadRuns = async () => {
    setLoading(true)
    setError(null)
    try {
      const items = await api.listPromptRuns()
      setRuns(items)
    } catch (err) {
      setError({ title: 'Unable to load prompt runs', message: err instanceof Error ? err.message : String(err) })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadRuns()
  }, [])

  return (
    <div>
      <div className="page-head-row">
        <div>
          <h1 className="page-title">AI Prompt Runs &amp; Audit Trail</h1>
          <p className="page-sub">
            Auditing every local Ollama inference call: latency, tokens, JSON Schema validation, and evidence verification checks.
          </p>
        </div>
        <div>
          <button className="btn sm" onClick={() => void loadRuns()}>Refresh Audits</button>
        </div>
      </div>

      {error ? (
        <Panel>
          <ErrorState title={error.title} message={error.message} retry={() => void loadRuns()} />
        </Panel>
      ) : loading ? (
        <Panel><LoadingState label="Loading AI prompt runs…" /></Panel>
      ) : runs.length === 0 ? (
        <Panel>
          <div className="state">
            <h3>No prompt runs recorded</h3>
            <p>Trigger AI extraction on any page snapshot to see execution audits and latency metrics here.</p>
          </div>
        </Panel>
      ) : (
        <Panel title={`Audited inference runs (${runs.length})`}>
          <table className="data">
            <thead>
              <tr>
                <th>Run ID</th>
                <th>Snapshot</th>
                <th>Model</th>
                <th>Prompt Version</th>
                <th>Schema Status</th>
                <th>Latency</th>
                <th>Prompt Tokens</th>
                <th>Gen Tokens</th>
                <th>Speed (tok/s)</th>
                <th>Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td><strong>#{r.id}</strong></td>
                  <td>
                    <a
                      onClick={() => nav({ page: 'snapshot', id: r.snapshot_id })}
                      style={{ color: 'var(--accent-strong)', cursor: 'pointer', fontWeight: 600 }}
                    >
                      Snapshot #{r.snapshot_id}
                    </a>
                  </td>
                  <td><code className="chip">{r.model_tag}</code></td>
                  <td><span className="small">{r.prompt_version}</span></td>
                  <td>
                    <span className={`badge ${r.parse_ok ? 'ok' : 'blocked'}`}>
                      {r.parse_ok ? 'Valid Schema' : 'Validation Error'}
                    </span>
                    {r.validation_errors.length > 0 && (
                      <div className="small muted" style={{ marginTop: 2 }}>
                        {r.validation_errors.join('; ')}
                      </div>
                    )}
                  </td>
                  <td><strong>{r.latency_ms.toFixed(1)} ms</strong></td>
                  <td>{r.prompt_eval_count}</td>
                  <td>{r.eval_count}</td>
                  <td>{r.tokens_per_second.toFixed(1)}</td>
                  <td><span className="small muted">{formatTimestamp(r.created_at)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  )
}
