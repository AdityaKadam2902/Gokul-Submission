/* Health panel: app / database / configuration status from /api/health.
   Auto-refresh keeps it honest; failures surface as a visible error state. */
import { useEffect, useState } from 'react'
import { api, type HealthStatus } from '../api/client'
import { Badge, ErrorState, LoadingState, Panel, formatTimestamp } from '../components/ui'

export function HealthPage() {
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [checkedAt, setCheckedAt] = useState<string | null>(null)

  const load = async () => {
    try {
      setHealth(await api.health())
      setError(null)
      setCheckedAt(new Date().toISOString())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }
  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 15000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <div>
      <div className="page-head-row">
        <div>
          <h1 className="page-title">Application health</h1>
          <p className="page-sub">Status of the local backend. No external services are contacted in this milestone.</p>
        </div>
        <button className="btn" onClick={() => void load()}>Check now</button>
      </div>

      {health?.database_ok === false && <div className="alert error">Database check failed — see backend logs for the SQLite error.</div>}

      {error && (
        <Panel>
          <ErrorState title="Backend unavailable" message={error} retry={() => void load()} />
        </Panel>
      )}
      {!error && !health && <Panel><LoadingState label="Contacting backend…" /></Panel>}
      {health && (
        <>
          <div className="stat-cards">
            <div className="stat-card">
              <div className="k">Application</div>
              <div className="v"><span style={{ color: 'var(--ok)' }}>● </span>{health.status}</div>
              <div className="s">v{health.version}</div>
            </div>
            <div className="stat-card">
              <div className="k">Database</div>
              <div className="v">{health.database_ok ? 'reachable' : 'ERROR'}</div>
              <div className="s small mono">{health.database_path}</div>
            </div>
            <div className="stat-card">
              <div className="k">Local Ollama AI</div>
              <div className="v" style={{ color: health.ollama_ok ? 'var(--ok)' : 'var(--warn)' }}>
                {health.ollama_ok ? 'Ready' : 'Offline'}
              </div>
              <div className="s small mono">llama3.2:3b</div>
            </div>
          </div>
          <Panel title="Runtime components" actions={<span className="muted small">checked {formatTimestamp(checkedAt)}</span>}>
            <dl className="kv">
              <dt>Ollama (AI runtime)</dt>
              <dd>
                <Badge kind={health.ollama_ok ? 'ok' : 'blocked'}>
                  {health.ollama_ok ? 'operational' : 'unavailable'}
                </Badge>{' '}
                <span className="small">{health.ollama}</span>
              </dd>
              <dt>Robots tool</dt>
              <dd><span className="small">{health.robots_tool}</span></dd>
            </dl>
          </Panel>
          <Panel title="Milestone posture">
            <ul className="small" style={{ margin: 0, paddingLeft: 18, color: 'var(--text-2)' }}>
              <li><strong>Zero-Cost &amp; Local-First:</strong> Local Ollama runtime (<code>llama3.2:3b</code>) performs all structured inference without cloud APIs or fees.</li>
              <li><strong>Evidence-Backed Extraction:</strong> Candidate facts require exact quotation verification and Python-calculated offsets before persistence.</li>
              <li><strong>Candidate-Only Integrity:</strong> Model output is always candidate; human reviewers hold exclusive approval authority (M4).</li>
              <li><strong>Deterministic Scoping &amp; Allowlist:</strong> Headings define operational scope; off-allowlist and testimonial contacts are rejected.</li>
            </ul>
          </Panel>
        </>
      )}
    </div>
  )
}
