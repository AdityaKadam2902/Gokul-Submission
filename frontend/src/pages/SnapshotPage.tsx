/* Snapshot detail: parsed text with headings and exact offsets,
   plus AI candidate fact extraction, evidence verification, and prompt run audits (M2). */
import { useEffect, useState, useMemo } from 'react'
import {
  api,
  type SnapshotRead,
  type FactRead,
  type PromptRunRead,
  type ExtractionResultRead,
} from '../api/client'
import { ErrorState, FixtureBadge, LoadingState, Panel, formatTimestamp } from '../components/ui'
import type { Nav } from '../routing'

export function SnapshotPage({ id, nav }: { id: number; nav: Nav }) {
  const [snapshot, setSnapshot] = useState<SnapshotRead | null>(null)
  const [facts, setFacts] = useState<FactRead[]>([])
  const [promptRuns, setPromptRuns] = useState<PromptRunRead[]>([])
  const [error, setError] = useState<{ title: string; message: string } | null>(null)
  const [extracting, setExtracting] = useState(false)
  const [extractionMsg, setExtractionMsg] = useState<string | null>(null)
  const [highlightSpan, setHighlightSpan] = useState<{ start: number; end: number; label: string } | null>(null)

  const loadData = async () => {
    setError(null)
    try {
      const snap = await api.getSnapshot(id)
      setSnapshot(snap)
      const [fList, pRuns] = await Promise.all([
        api.getSnapshotFacts(id),
        api.getSnapshotPromptRuns(id),
      ])
      setFacts(fList)
      setPromptRuns(pRuns)
    } catch (err) {
      setError({ title: 'Unable to load snapshot details', message: err instanceof Error ? err.message : String(err) })
    }
  }

  useEffect(() => { void loadData() }, [id])

  const handleExtract = async (force = false) => {
    setExtracting(true)
    setExtractionMsg(null)
    try {
      const res: ExtractionResultRead = await api.extractSnapshot(id, force)
      setExtractionMsg(res.message)
      await loadData()
    } catch (err) {
      setError({ title: 'AI Extraction Failed', message: err instanceof Error ? err.message : String(err) })
    } finally {
      setExtracting(false)
    }
  }

  const renderedText = useMemo(() => {
    if (!snapshot) return null

    // If an evidence quote is highlighted, overlay that span
    if (highlightSpan && highlightSpan.start >= 0 && highlightSpan.end <= snapshot.text.length) {
      const { start, end } = highlightSpan
      return (
        <>
          <span>{snapshot.text.slice(0, start)}</span>
          <mark className="hl-evidence" title={`Evidence Quote [${start}..${end})`}>
            {snapshot.text.slice(start, end)}
          </mark>
          <span>{snapshot.text.slice(end)}</span>
        </>
      )
    }

    // Default: render heading highlights
    const spans = [...snapshot.headings].filter((h) => h.text.length > 0)
    const nodes: React.ReactNode[] = []
    let cursor = 0
    for (const h of spans) {
      const start = h.start_offset
      const end = h.end_offset
      if (start > snapshot.text.length || end > snapshot.text.length || start < cursor) {
        continue
      }
      if (start > cursor) nodes.push(<span key={`t${cursor}`}>{snapshot.text.slice(cursor, start)}</span>)
      nodes.push(
        <span key={`h${start}`} className="hl-heading" title={`h${h.level} · offsets ${start}–${end}`}>
          {snapshot.text.slice(start, end)}
        </span>
      )
      nodes.push('\n')
      cursor = end
    }
    if (cursor < snapshot.text.length) nodes.push(<span key="tail">{snapshot.text.slice(cursor)}</span>)
    return nodes
  }, [snapshot, highlightSpan])

  if (error) {
    return (
      <Panel>
        <ErrorState title={error.title} message={error.message} retry={() => void loadData()} />
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

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button
            className="btn primary"
            disabled={extracting}
            onClick={() => void handleExtract(false)}
          >
            {extracting ? 'Running Local Ollama…' : facts.length > 0 ? 'Re-extract Candidates' : 'Extract with Local AI'}
          </button>
          {facts.length > 0 && (
            <button
              className="btn sm"
              disabled={extracting}
              title="Force re-run AI model bypassing hash cache"
              onClick={() => void handleExtract(true)}
            >
              Force Re-run
            </button>
          )}
        </div>
      </div>

      {extractionMsg && (
        <div className="alert info">
          <strong>AI Extraction Status:</strong> {extractionMsg}
        </div>
      )}

      {/* AI Candidate Facts Section */}
      <Panel
        title={`AI-Extracted Candidate Facts (${facts.length})`}
        actions={
          facts.length > 0 && (
            <span className="badge candidate" style={{ fontSize: 11 }}>
              UNAPPROVED CANDIDATES · PENDING HUMAN REVIEW
            </span>
          )
        }
      >
        <div style={{ marginBottom: 12, fontSize: 12.5, color: 'var(--text-2)' }}>
          The local AI proposes structured facts; deterministic Python algorithms verify exact evidence quotes,
          compute offsets, enforce source allowlists, and preserve raw values.
        </div>

        {facts.length === 0 ? (
          <div className="state" style={{ padding: '24px 0' }}>
            <h3>No candidate facts extracted yet</h3>
            <p>Click "Extract with Local AI" to execute the local Ollama extraction and evidence verification pipeline.</p>
          </div>
        ) : (
          <table className="data" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>Status</th>
                <th>Field Key</th>
                <th>Scope</th>
                <th>Raw Extracted Value</th>
                <th>Normalised Value</th>
                <th>Evidence Quote &amp; Offsets</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {facts.map((fact) => {
                const isSelected =
                  highlightSpan &&
                  fact.evidence_start_offset === highlightSpan.start &&
                  fact.evidence_end_offset === highlightSpan.end
                return (
                  <tr key={fact.id} style={{ background: isSelected ? 'var(--accent-soft)' : undefined }}>
                    <td>
                      <span className={`badge ${fact.status}`}>
                        {fact.status}
                      </span>
                      {fact.needs_review_reason && (
                        <div className="small muted" style={{ marginTop: 3 }}>
                          {fact.needs_review_reason}
                        </div>
                      )}
                    </td>
                    <td>
                      <code className="chip">{fact.field_key}</code>
                    </td>
                    <td>
                      <span className="small">{fact.scope_key || 'general'}</span>
                    </td>
                    <td>
                      <div style={{ fontWeight: 550 }}>{fact.raw_value}</div>
                    </td>
                    <td>
                      <span className="small muted">{fact.normalized_value ?? '—'}</span>
                    </td>
                    <td>
                      {fact.evidence_quote ? (
                        <div>
                          <div style={{ fontStyle: 'italic', fontSize: 12, marginBottom: 2 }}>
                            "{fact.evidence_quote}"
                          </div>
                          {fact.evidence_start_offset != null && fact.evidence_end_offset != null && (
                            <code className="hash" style={{ fontSize: 11 }}>
                              [{fact.evidence_start_offset}..{fact.evidence_end_offset})
                            </code>
                          )}
                        </div>
                      ) : (
                        <span className="muted small">No evidence quote</span>
                      )}
                    </td>
                    <td>
                      {fact.evidence_start_offset != null && fact.evidence_end_offset != null && (
                        <button
                          className="btn sm"
                          onClick={() => {
                            if (isSelected) {
                              setHighlightSpan(null)
                            } else {
                              setHighlightSpan({
                                start: fact.evidence_start_offset!,
                                end: fact.evidence_end_offset!,
                                label: fact.field_key,
                              })
                            }
                          }}
                        >
                          {isSelected ? 'Clear' : 'View in Text'}
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Panel>

      {/* Parsed Text Viewer with Heading & Evidence Highlights */}
      <Panel
        title={
          highlightSpan
            ? `Parsed text — highlighting evidence for "${highlightSpan.label}" [${highlightSpan.start}..${highlightSpan.end})`
            : `Parsed text (${snapshot.text.length} chars) — with heading offsets`
        }
        actions={
          highlightSpan && (
            <button className="btn sm" onClick={() => setHighlightSpan(null)}>
              Reset Highlights
            </button>
          )
        }
      >
        <div className="snapshot-text">{renderedText}</div>
      </Panel>

      {/* AI Prompt Runs & Execution Audit */}
      {promptRuns.length > 0 && (
        <Panel title={`Local AI Prompt Runs & Latency Audits (${promptRuns.length})`}>
          <table className="data">
            <thead>
              <tr>
                <th>Run ID</th>
                <th>Model</th>
                <th>Prompt Version</th>
                <th>Status</th>
                <th>Latency (ms)</th>
                <th>Prompt Tokens</th>
                <th>Generated Tokens</th>
                <th>Speed (tok/s)</th>
                <th>Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {promptRuns.map((r) => (
                <tr key={r.id}>
                  <td>#{r.id}</td>
                  <td><code className="chip">{r.model_tag}</code></td>
                  <td><span className="small">{r.prompt_version}</span></td>
                  <td>
                    <span className={`badge ${r.parse_ok ? 'ok' : 'blocked'}`}>
                      {r.parse_ok ? 'Valid JSON Schema' : 'Parse Failed'}
                    </span>
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

      {/* Headings and Provenance */}
      <Panel title={`Heading structure (${snapshot.headings.length})`}>
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

      <Panel title="Provenance">
        <dl className="kv">
          <dt>Source</dt><dd>#{snapshot.source_id}{snapshot.source_label ? ` — ${snapshot.source_label}` : ''}</dd>
          <dt>Fixture provenance</dt><dd>{snapshot.fixture ? 'SYNTHETIC FIXTURE — data is invented, never a live scrape' : 'declared-source snapshot'}</dd>
          <dt>Content Hash</dt><dd><code className="hash">{snapshot.content_hash}</code></dd>
          <dt>Review Authority</dt><dd className="muted small">All AI proposals require human approval. No automated recruiter or live availability claim is made.</dd>
        </dl>
      </Panel>
    </div>
  )
}
