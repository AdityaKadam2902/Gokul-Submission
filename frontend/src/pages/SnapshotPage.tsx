/* Snapshot detail: parsed text with the heading chain highlighted at exact
   stored offsets, plus provenance metadata. Read-only: snapshots immutable. */
import { useEffect, useState, useMemo } from 'react'
import { api, type SnapshotRead } from '../api/client'
import { ErrorState, FixtureBadge, LoadingState, Panel, formatTimestamp } from '../components/ui'
import type { Nav } from '../routing'

export function SnapshotPage({ id, nav }: { id: number; nav: Nav }) {
  const [snapshot, setSnapshot] = useState<SnapshotRead | null>(null)
  const [error, setError] = useState<{ title: string; message: string } | null>(null)

  const load = async () => {
    setError(null)
    try {
      setSnapshot(await api.getSnapshot(id))
    } catch (err) {
      setError({ title: 'Unable to load snapshot', message: err instanceof Error ? err.message : String(err) })
    }
  }
  useEffect(() => { void load() }, [id])

  const rendered = useMemo(() => {
    if (!snapshot) return null
    const spans = [...snapshot.headings].filter((h) => h.text.length > 0)
    const nodes: React.ReactNode[] = []
    let cursor = 0
    for (const h of spans) {
      const start = h.start_offset
      const end = h.end_offset
      if (start > snapshot.text.length || end > snapshot.text.length || start < cursor) {
        continue // defensive: never render against inconsistent offsets
      }
      if (start > cursor) nodes.push(<span key={`t${cursor}`}>{snapshot.text.slice(cursor, start)}</span>)
      nodes.push(<span key={`h${start}`} className="hl-heading" title={`h${h.level} · offsets ${start}–${end}`}>{snapshot.text.slice(start, end)}</span>)
      nodes.push('\n')
      cursor = end
    }
    if (cursor < snapshot.text.length) nodes.push(<span key="tail">{snapshot.text.slice(cursor)}</span>)
    return nodes
  }, [snapshot])

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
  if (!snapshot) return <Panel><LoadingState label="Loading snapshot…" /></Panel>

  return (
    <div>
      <div className="breadcrumb">
        <a onClick={() => nav({ page: 'sources' })}>Sources</a><span className="sep">/</span>
        <a onClick={() => nav({ page: 'source', id: snapshot.source_id })}>source #{snapshot.source_id}</a>
        <span className="sep">/</span>snapshot #{id}
      </div>
      <div className="page-head-row">
        <div>
          <h1 className="page-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {snapshot.title ?? `Snapshot #${id}`}
            <FixtureBadge />
          </h1>
          <div className="meta-row">
            <span>Retrieved {formatTimestamp(snapshot.fetched_at)}</span>
            <span className="mono small">parser {snapshot.parser_version}</span>
            <span className="mono small">hash {snapshot.content_hash.slice(0, 16)}…</span>
          </div>
        </div>
      </div>

      <Panel title={`Heading structure (${snapshot.headings.length}) — exact offsets into the stored text`}>
        {snapshot.headings.length === 0 ? (
          <span className="muted">No headings detected in this snapshot.</span>
        ) : (
          <ul className="headings-panel small">
            {snapshot.headings.map((h) => (
              <li key={`${h.start_offset}-${h.text}`}>
                <span className="muted">h{h.level}</span>{' '}
                <strong>{h.text}</strong>{' '}
                <code className="hash">[{h.start_offset}..{h.end_offset})</code>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Parsed text (normalised; hidden/script/style content already stripped)">
        <div className="snapshot-text">{rendered}</div>
      </Panel>

      <Panel title="Provenance">
        <dl className="kv">
          <dt>Source</dt><dd>#{snapshot.source_id}{snapshot.source_label ? ` — ${snapshot.source_label}` : ''}</dd>
          <dt>Fixture provenance</dt><dd>{snapshot.fixture ? 'SYNTHETIC FIXTURE — data is invented, never a live scrape' : 'declared-source snapshot'}</dd>
          <dt>Immutability</dt><dd className="muted small">Snapshots are append-only; unchanged content re-ingestion reuses the existing snapshot (hash short-circuit).</dd>
        </dl>
      </Panel>
    </div>
  )
}
