import { useEffect, useState } from 'react'
import {
  api,
  type ReviewActionCreate,
  type ReviewActionRead,
  type ReviewItemDetailRead,
  type ReviewMetricsRead,
  type ReviewQueueItemRead,
} from '../api/client'
import { Badge, EmptyState, ErrorState, formatTimestamp, LoadingState, Panel } from '../components/ui'
import type { Nav } from '../routing'

export function ReviewQueuePage({ nav: _nav }: { nav: Nav }) {
  const [activeTab, setActiveTab] = useState<'queue' | 'history'>('queue')
  const [metrics, setMetrics] = useState<ReviewMetricsRead | null>(null)
  const [queueItems, setQueueItems] = useState<ReviewQueueItemRead[]>([])
  const [historyItems, setHistoryItems] = useState<ReviewActionRead[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Filters
  const [categoryFilter, setCategoryFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<string>('pending')

  // Selected item detail modal
  const [selectedEntity, setSelectedEntity] = useState<{ type: string; id: number } | null>(null)
  const [detail, setDetail] = useState<ReviewItemDetailRead | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  // Decision Form State
  const [reviewer, setReviewer] = useState<string>(() => localStorage.getItem('ps_reviewer_id') || 'local_analyst_1')
  const [chosenAction, setChosenAction] = useState<string>('')
  const [reasonCode, setReasonCode] = useState<string>('manual_evidence_verification')
  const [decisionNotes, setDecisionNotes] = useState<string>('')
  const [assignedOrgId, setAssignedOrgId] = useState<number | undefined>(undefined)
  const [confirmed, setConfirmed] = useState<boolean>(false)
  const [submitting, setSubmitting] = useState<boolean>(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const loadData = async () => {
    setLoading(true)
    setError(null)
    try {
      const [m, q, h] = await Promise.all([
        api.getReviewMetrics(),
        api.getReviewQueue({
          entity_type: categoryFilter !== 'all' ? categoryFilter : undefined,
          status: statusFilter,
          limit: 100,
        }),
        api.listReviewActions({ limit: 100 }),
      ])
      setMetrics(m)
      setQueueItems(q.items)
      setHistoryItems(h.items)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadData()
  }, [categoryFilter, statusFilter])

  const openDetail = async (type: string, id: number) => {
    setSelectedEntity({ type, id })
    setDetailLoading(true)
    setDetailError(null)
    setSubmitError(null)
    setChosenAction('')
    setConfirmed(false)
    setDecisionNotes('')
    setAssignedOrgId(undefined)
    try {
      const data = await api.getReviewItemDetail(type, id)
      setDetail(data)
      if (data.allowed_actions && data.allowed_actions.length > 0) {
        setChosenAction(data.allowed_actions[0] ?? '')
      }
      const ent = (data.entity_data || {}) as Record<string, any>
      if (ent.candidate_organisations && (ent.candidate_organisations as any[]).length > 0) {
        setAssignedOrgId((ent.candidate_organisations as any[])[0].id)
      }
    } catch (err) {
      setDetailError((err as Error).message)
    } finally {
      setDetailLoading(false)
    }
  }

  const handleCloseModal = () => {
    setSelectedEntity(null)
    setDetail(null)
  }

  const handleSubmitDecision = async () => {
    if (!selectedEntity || !chosenAction) return
    if (!reviewer.trim()) {
      setSubmitError('Reviewer identifier is required.')
      return
    }
    if (!reasonCode.trim()) {
      setSubmitError('Reason code is required.')
      return
    }
    if (['resolved_a', 'resolved_b', 'rejected', 'dismissed_benign'].includes(chosenAction) && !decisionNotes.trim()) {
      setSubmitError('Notes / explanatory rationale is required for this decision.')
      return
    }
    if (chosenAction === 'assigned_org' && !assignedOrgId) {
      setSubmitError('Please select a target organisation to assign.')
      return
    }
    if (!confirmed) {
      setSubmitError('Please check the confirmation box before submitting.')
      return
    }

    setSubmitting(true)
    setSubmitError(null)

    // Save reviewer ID to localStorage
    localStorage.setItem('ps_reviewer_id', reviewer.trim())

    const payload: ReviewActionCreate = {
      action: chosenAction,
      reason_code: reasonCode.trim(),
      reviewer: reviewer.trim(),
      notes: decisionNotes.trim() || undefined,
      evidence_id: detail?.evidence_id ?? undefined,
      decision_metadata: {
        ...(assignedOrgId ? { assigned_organisation_id: assignedOrgId } : {}),
      },
    }

    try {
      await api.submitReviewAction(selectedEntity.type, selectedEntity.id, payload)
      handleCloseModal()
      await loadData()
    } catch (err) {
      setSubmitError((err as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const getCategoryBadgeClass = (type: string) => {
    switch (type) {
      case 'match_candidate':
        return 'cat-match'
      case 'fact_ownership':
        return 'cat-ownership'
      case 'discrepancy':
        return 'cat-discrepancy'
      case 'opportunity':
        return 'cat-opportunity'
      case 'lifecycle_event':
        return 'cat-match'
      case 'change_event':
        return 'cat-change'
      case 'fact':
        return 'cat-fact'
      default:
        return 'neutral'
    }
  }

  const formatEntityType = (type: string) => {
    switch (type) {
      case 'match_candidate':
        return 'Organisation Match'
      case 'fact_ownership':
        return 'Fact Ownership'
      case 'discrepancy':
        return 'Factual Discrepancy'
      case 'opportunity':
        return 'Workforce Opportunity'
      case 'lifecycle_event':
        return 'Practice Lifecycle'
      case 'change_event':
        return 'Temporal Change'
      case 'fact':
        return 'Candidate Fact'
      case 'freshness':
        return 'Freshness'
      default:
        return type
    }
  }

  return (
    <div>
      <div className="page-head-row">
        <div>
          <h1 className="page-title">Human Review &amp; Auditable Decision Workspace</h1>
          <p className="page-sub">
            Inspect evidence-grounded suggestions, cross-source discrepancies, and record durable human conclusions.
          </p>
        </div>
        <div>
          <Badge kind="policy">Local Review Environment · Unauthenticated</Badge>
        </div>
      </div>

      <div className="alert info">
        <strong>Review Principles:</strong> AI extractions and match candidates are machine suggestions.
        Decisions record human conclusions in an immutable append-only audit trail without altering historical evidence.
      </div>

      {/* DASHBOARD METRICS */}
      {metrics && (
        <div className="stat-cards">
          <div className="stat-card">
            <div className="k">Pending Review</div>
            <div className="v" style={{ color: 'var(--accent-strong)' }}>
              {metrics.total_pending}
            </div>
            <div className="s">Items awaiting human evaluation</div>
          </div>
          <div className="stat-card">
            <div className="k">Under Review</div>
            <div className="v" style={{ color: 'var(--warn)' }}>
              {metrics.total_under_review}
            </div>
            <div className="s">Active investigations in progress</div>
          </div>
          <div className="stat-card">
            <div className="k">Deferred Items</div>
            <div className="v">{metrics.total_deferred}</div>
            <div className="s">Postponed for additional evidence</div>
          </div>
          <div className="stat-card">
            <div className="k">Recorded Decisions</div>
            <div className="v" style={{ color: 'var(--ok)' }}>
              {metrics.total_completed}
            </div>
            <div className="s">Append-only audit actions</div>
          </div>
        </div>
      )}

      {/* TAB BAR */}
      <div className="tab-bar">
        <button
          className={`tab-btn ${activeTab === 'queue' ? 'active' : ''}`}
          onClick={() => setActiveTab('queue')}
        >
          Review Queue ({queueItems.length})
        </button>
        <button
          className={`tab-btn ${activeTab === 'history' ? 'active' : ''}`}
          onClick={() => setActiveTab('history')}
        >
          Audit History ({historyItems.length})
        </button>
      </div>

      {/* TAB 1: REVIEW QUEUE */}
      {activeTab === 'queue' && (
        <>
          <div className="filter-bar">
            <label>
              Category:
              <select value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>
                <option value="all">All Domains</option>
                <option value="lifecycle_event">Practice Lifecycle (Closure/Relocation/Acquisition)</option>
                <option value="match_candidate">Organisation Matches</option>
                <option value="fact_ownership">Fact Ownership</option>
                <option value="discrepancy">Factual Discrepancies</option>
                <option value="opportunity">Workforce Opportunities</option>
                <option value="fact">Candidate Facts</option>
              </select>
            </label>

            <label>
              Status:
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="pending">Pending Items</option>
                <option value="under_review">Under Review</option>
                <option value="deferred">Deferred Items</option>
                <option value="completed">Completed Items</option>
                <option value="all">All States</option>
              </select>
            </label>

            <button className="btn sm" onClick={loadData} style={{ marginLeft: 'auto' }}>
              Refresh Queue
            </button>
          </div>

          {loading ? (
            <LoadingState label="Loading review queue..." />
          ) : error ? (
            <ErrorState title="Failed to load review queue" message={error} retry={loadData} />
          ) : queueItems.length === 0 ? (
            <EmptyState
              title="Review queue is clear"
              hint="No items currently require human decision in the selected filter."
            />
          ) : (
            <div>
              {queueItems.map((item) => (
                <div key={item.queue_id} className="queue-card">
                  <div className="queue-card-header">
                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                      <span className={`badge ${getCategoryBadgeClass(item.entity_type)}`}>
                        {formatEntityType(item.entity_type)}
                      </span>
                      <span className={`badge priority-${item.priority}`}>{item.priority.toUpperCase()} PRIORITY</span>
                    </div>
                    <span className="muted small">{formatTimestamp(item.created_at)}</span>
                  </div>

                  <div className="queue-card-title">{item.title}</div>
                  <div className="queue-card-summary">{item.summary}</div>

                  <div className="queue-card-flag">
                    <strong>Why flagged:</strong> {item.flagged_reason}
                  </div>

                  {item.evidence_quote && (
                    <blockquote className="evidence-quote">
                      &ldquo;{item.evidence_quote}&rdquo;
                    </blockquote>
                  )}

                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '6px' }}>
                    <div className="queue-card-meta">
                      {item.organisation_name && <span className="chip">Org: {item.organisation_name}</span>}
                      {item.source_url && <span className="chip">Source: {item.source_url}</span>}
                      <span className="badge neutral">State: {item.current_status}</span>
                    </div>
                    <button
                      className="btn primary sm"
                      onClick={() => openDetail(item.entity_type, item.entity_id)}
                    >
                      Inspect &amp; Decide
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {/* TAB 2: AUDIT HISTORY */}
      {activeTab === 'history' && (
        <Panel title="Complete Review Decision Audit History" tight>
          {loading ? (
            <LoadingState label="Loading audit history..." />
          ) : historyItems.length === 0 ? (
            <EmptyState title="No decisions recorded yet" hint="Human decisions will appear here as immutable audit records." />
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Entity</th>
                  <th>Action</th>
                  <th>Transition</th>
                  <th>Reason Code</th>
                  <th>Reviewer</th>
                  <th>Notes</th>
                  <th>Timestamp</th>
                </tr>
              </thead>
              <tbody>
                {historyItems.map((h) => (
                  <tr key={h.id}>
                    <td className="mono">#{h.id}</td>
                    <td>
                      <span className={`badge ${getCategoryBadgeClass(h.entity_type)}`}>
                        {formatEntityType(h.entity_type)} #{h.entity_id}
                      </span>
                    </td>
                    <td>
                      <span className="badge ok">{h.action}</span>
                    </td>
                    <td className="small">
                      <span className="muted">{h.previous_status}</span> &rarr; <strong>{h.new_status}</strong>
                    </td>
                    <td className="small mono">{h.reason_code}</td>
                    <td className="small font-semibold">{h.reviewer}</td>
                    <td className="small">{h.notes || '—'}</td>
                    <td className="small muted">{formatTimestamp(h.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      )}

      {/* REVIEW DETAIL & DECISION MODAL */}
      {selectedEntity && (
        <div className="modal-backdrop" onClick={handleCloseModal}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <div>
                <h3 style={{ margin: 0, fontSize: '16px' }}>
                  Inspect &amp; Decide: {detail?.title || 'Loading...'}
                </h3>
                <span className="muted small">
                  {formatEntityType(selectedEntity.type)} #{selectedEntity.id}
                </span>
              </div>
              <button className="btn sm" onClick={handleCloseModal}>
                &times; Close
              </button>
            </div>

            <div className="modal-body">
              {detailLoading ? (
                <LoadingState label="Loading evidence &amp; context..." />
              ) : detailError ? (
                <ErrorState title="Failed to load detail" message={detailError} />
              ) : detail ? (
                <>
                  <div className="alert warn" style={{ marginBottom: '14px' }}>
                    <strong>Flagged:</strong> {detail.flagged_reason}
                  </div>

                  {/* EVIDENCE QUOTE */}
                  {detail.evidence_quote && (
                    <div style={{ marginBottom: '16px' }}>
                      <div className="stat-card" style={{ background: '#f8fafc' }}>
                        <div className="k">Supporting Verbatim Evidence Quotation</div>
                        <blockquote className="evidence-quote">
                          &ldquo;{detail.evidence_quote}&rdquo;
                        </blockquote>
                        {detail.evidence_start_offset != null && detail.evidence_end_offset != null && (
                          <div className="s mono">
                            Character offsets: [{detail.evidence_start_offset} : {detail.evidence_end_offset}] in PageSnapshot #{detail.snapshot_id || 'n/a'}
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {/* LIFECYCLE EVENT CONTEXT */}
                  {detail.lifecycle_context && (() => {
                    const lc = detail.lifecycle_context as Record<string, any>
                    return (
                      <div style={{ marginBottom: '16px' }}>
                        <h4>Practice Lifecycle Event Details</h4>
                        <dl className="kv">
                          <dt>Event Type</dt>
                          <dd><strong>{lc.event_type?.replace(/_/g, ' ').toUpperCase()}</strong></dd>
                          <dt>Scope</dt>
                          <dd>{lc.is_branch_level ? `Branch (${lc.branch_locality})` : 'Entire Practice Organisation'}</dd>
                          {lc.effective_date && (
                            <>
                              <dt>Effective Date</dt>
                              <dd>{lc.effective_date}</dd>
                            </>
                          )}
                          {lc.old_value && (
                            <>
                              <dt>Previous Value / Location</dt>
                              <dd>{lc.old_value}</dd>
                            </>
                          )}
                          {lc.new_value && (
                            <>
                              <dt>Proposed New Value / Location</dt>
                              <dd>{lc.new_value}</dd>
                            </>
                          )}
                          {lc.acquiring_entity_name && (
                            <>
                              <dt>Acquiring Corporate Entity</dt>
                              <dd>{lc.acquiring_entity_name}</dd>
                            </>
                          )}
                        </dl>
                      </div>
                    )
                  })()}

                  {/* DISCREPANCY COMPARISON */}
                  {detail.discrepancy_context && (() => {
                    const disc = detail.discrepancy_context as Record<string, any>
                    const factA = disc.fact_a as Record<string, any> | undefined
                    const factB = disc.fact_b as Record<string, any> | undefined
                    return (
                      <div style={{ marginBottom: '16px' }}>
                        <h4>Side-by-Side Source Divergence</h4>
                        <div className="comparison-grid">
                          <div className="comparison-side">
                            <h4>Fact A (ID #{factA?.id})</h4>
                            <p><strong>Value:</strong> {factA?.raw_value}</p>
                            <p className="small muted">Source: {factA?.source_url}</p>
                            {factA?.evidence_quote && (
                              <blockquote className="evidence-quote small">
                                &ldquo;{factA.evidence_quote}&rdquo;
                              </blockquote>
                            )}
                          </div>
                          <div className="comparison-side">
                            <h4>Fact B (ID #{factB?.id})</h4>
                            <p><strong>Value:</strong> {factB?.raw_value}</p>
                            <p className="small muted">Source: {factB?.source_url}</p>
                            {factB?.evidence_quote && (
                              <blockquote className="evidence-quote small">
                                &ldquo;{factB.evidence_quote}&rdquo;
                              </blockquote>
                            )}
                          </div>
                        </div>
                      </div>
                    )
                  })()}

                  {/* MATCH SIGNALS */}
                  {selectedEntity.type === 'match_candidate' && (() => {
                    const entData = (detail.entity_data || {}) as Record<string, any>
                    const org = entData.suggested_org as Record<string, any> | undefined
                    if (!org) return null
                    return (
                      <div style={{ marginBottom: '16px' }}>
                        <h4>Suggested Organisation Details</h4>
                        <dl className="kv">
                          <dt>Name</dt>
                          <dd>{org.name}</dd>
                          <dt>Primary Domain</dt>
                          <dd>{org.primary_domain || '—'}</dd>
                          <dt>Location</dt>
                          <dd>
                            {org.postcode} {org.state}
                          </dd>
                          <dt>Phone</dt>
                          <dd>{org.phone || '—'}</dd>
                        </dl>
                      </div>
                    )
                  })()}

                  {/* DECISION PANEL */}
                  <div className="decision-panel">
                    <h4 style={{ margin: '0 0 10px', fontSize: '14px', color: 'var(--text)' }}>
                      Record Human Decision
                    </h4>

                    {submitError && <div className="alert error">{submitError}</div>}

                    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                      <label className="field">
                        Reviewer Identifier:
                        <input
                          type="text"
                          value={reviewer}
                          onChange={(e) => setReviewer(e.target.value)}
                          placeholder="e.g. analyst_1, dr_operator"
                        />
                        <span className="hint">
                          Stored immutably with every recorded decision.
                        </span>
                      </label>

                      <div>
                        <div style={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--text-2)', marginBottom: '6px' }}>
                          Choose Decision Action:
                        </div>
                        <div className="action-buttons">
                          {(detail.allowed_actions || []).map((act) => (
                            <button
                              key={act}
                              type="button"
                              className={`action-btn ${chosenAction === act ? 'selected' : ''} ${
                                act.includes('approved') || act.includes('accepted') ? 'approve' : ''
                              } ${act.includes('rejected') ? 'reject' : ''} ${act.includes('deferred') ? 'defer' : ''}`}
                              onClick={() => setChosenAction(act)}
                            >
                              {act.replace('_', ' ').toUpperCase()}
                            </button>
                          ))}
                        </div>
                      </div>

                      {/* FACT OWNERSHIP ASSIGNMENT DROPDOWN */}
                      {selectedEntity.type === 'fact_ownership' && chosenAction === 'assigned_org' && (
                        <label className="field">
                          Select Practice Organisation to Assign:
                          <select
                            value={assignedOrgId || ''}
                            onChange={(e) => setAssignedOrgId(Number(e.target.value))}
                          >
                            {((((detail.entity_data || {}) as Record<string, any>).candidate_organisations || []) as any[]).map((o: any) => (
                              <option key={o.id} value={o.id}>
                                {o.name} (Slug: {o.slug})
                              </option>
                            ))}
                          </select>
                        </label>
                      )}

                      <label className="field">
                        Reason Code:
                        <select value={reasonCode} onChange={(e) => setReasonCode(e.target.value)}>
                          <option value="manual_evidence_verification">manual_evidence_verification</option>
                          <option value="verified_practice_domain">verified_practice_domain</option>
                          <option value="distinct_branch_clinic">distinct_branch_clinic</option>
                          <option value="departmental_hours_distinction">departmental_hours_distinction</option>
                          <option value="verbatim_closure_confirmed">verbatim_closure_confirmed</option>
                          <option value="heading_scope_confirmed">heading_scope_confirmed</option>
                          <option value="insufficient_supporting_evidence">insufficient_supporting_evidence</option>
                        </select>
                      </label>

                      <label className="field">
                        Decision Notes / Rationale:
                        <textarea
                          rows={2}
                          value={decisionNotes}
                          onChange={(e) => setDecisionNotes(e.target.value)}
                          placeholder="Explain reasoning or specific findings..."
                        />
                        <span className="hint">
                          Mandatory for consequential decisions (resolve, reject, dismiss).
                        </span>
                      </label>

                      <label style={{ display: 'flex', gap: '8px', alignItems: 'center', fontSize: '13px', cursor: 'pointer' }}>
                        <input
                          type="checkbox"
                          checked={confirmed}
                          onChange={(e) => setConfirmed(e.target.checked)}
                        />
                        <span>
                          I have inspected original source evidence and confirm this decision.
                        </span>
                      </label>
                    </div>
                  </div>

                  {/* PREVIOUS REVIEW HISTORY ON THIS ITEM */}
                  {detail.review_history && detail.review_history.length > 0 && (
                    <div style={{ marginTop: '16px' }}>
                      <h4 style={{ margin: '0 0 8px', fontSize: '13px' }}>Decision History for this Entity</h4>
                      <table className="data small">
                        <thead>
                          <tr>
                            <th>Action</th>
                            <th>Reviewer</th>
                            <th>Transition</th>
                            <th>Notes</th>
                            <th>Date</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.review_history.map((h) => (
                            <tr key={h.id}>
                              <td>
                                <span className="badge ok">{h.action}</span>
                              </td>
                              <td>{h.reviewer}</td>
                              <td>
                                {h.previous_status} &rarr; {h.new_status}
                              </td>
                              <td>{h.notes || '—'}</td>
                              <td className="muted">{formatTimestamp(h.created_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              ) : null}
            </div>

            <div className="modal-foot">
              <button className="btn" onClick={handleCloseModal} disabled={submitting}>
                Cancel
              </button>
              <button
                className="btn primary"
                onClick={handleSubmitDecision}
                disabled={submitting || !confirmed || !chosenAction}
              >
                {submitting ? 'Recording...' : 'Record Decision'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
