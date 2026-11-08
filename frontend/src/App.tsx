/* App shell: header, hash-based navigation (no extra deps), backend
   availability banner. Routing state: #/sources, #/source/:id,
   #/snapshot/:id, #/health. */
import { useCallback, useEffect, useState } from 'react'
import { api } from './api/client'
import { HealthPage } from './pages/HealthPage'
import { SnapshotPage } from './pages/SnapshotPage'
import { SourceDetailPage } from './pages/SourceDetailPage'
import { SourcesPage } from './pages/SourcesPage'
import { FactsPage } from './pages/FactsPage'
import { PromptRunsPage } from './pages/PromptRunsPage'
import { ReviewQueuePage } from './pages/ReviewQueuePage'
import { parseHash, navTo, type Loc } from './routing'


export default function App() {
  const [loc, setLoc] = useState<Loc>(parseHash)
  const [backendUp, setBackendUp] = useState<boolean | null>(null)

  const nav = useCallback((target: Loc) => {
    navTo(target)
    setLoc(parseHash())
  }, [])

  useEffect(() => {
    const onHash = () => setLoc(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    let cancelled = false
    const probe = async () => {
      try {
        await api.health()
        if (!cancelled) setBackendUp(true)
      } catch {
        if (!cancelled) setBackendUp(false)
      }
    }
    void probe()
    const timer = window.setInterval(() => void probe(), 20000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [])

  const active = (page: string) => (loc.page === page ? 'active' : '')

  return (
    <>
      <header className="app-header">
        <div className="brand">
          <span className="brand-name">PracticeSignal</span>
          <span className="brand-tag">Healthcare Practice &amp; Workforce Intelligence · M2</span>
        </div>
        <nav className="top-nav">
          <a href="#/sources" className={active('sources') || active('source') || active('snapshot')}>Sources</a>
          <a href="#/facts" className={active('facts')}>Candidate Facts</a>
          <a href="#/reviews" className={active('reviews')}>Review Queue</a>
          <a href="#/prompt-runs" className={active('prompt-runs')}>Prompt Audits</a>
          <a href="#/health" className={active('health')}>Health</a>
        </nav>
        <div className="header-right">
          {backendUp === false
            ? <span className="badge blocked">backend offline</span>
            : backendUp === true
              ? <span className="badge ok">backend connected</span>
              : <span className="badge neutral">checking…</span>}
        </div>
      </header>

      <main className="main">
        {backendUp === false && <div className="alert warn">The backend is not reachable. Start it with: <code>python -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port 8000</code></div>}
        {loc.page === 'sources' && <SourcesPage nav={nav} />}
        {loc.page === 'source' && loc.id != null && <SourceDetailPage id={loc.id} nav={nav} />}
        {loc.page === 'snapshot' && loc.id != null && <SnapshotPage id={loc.id} nav={nav} />}
        {loc.page === 'facts' && <FactsPage nav={nav} />}
        {loc.page === 'reviews' && <ReviewQueuePage nav={nav} />}
        {loc.page === 'prompt-runs' && <PromptRunsPage nav={nav} />}
        {loc.page === 'health' && <HealthPage />}
      </main>

      <footer className="footer">
        <span>Local-first, zero-cost prototype — synthetic fixtures only in this milestone.</span>
        <span>Ingestion is policy-gated server-side (docs/SOURCE_REGISTER.md).</span>
        <span>All displayed data is clearly labelled as synthetic.</span>
      </footer>
    </>
  )
}
