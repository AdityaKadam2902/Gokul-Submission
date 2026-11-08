/* Thin fetch helpers over the documented API — response types come from the
 * OpenAPI-generated schema (src/api/schema.d.ts, D19). No hand-written API types
 * beyond narrow local unions for rendering.
 */
import type { components, paths as _paths } from './schema'

export type SourceRead = components['schemas']['SourceRead']
export type SourceDetailRead = components['schemas']['SourceDetailRead']
export type SnapshotRead = components['schemas']['SnapshotRead']
export type IngestionResult = components['schemas']['IngestionResult']
export type HealthStatus = components['schemas']['HealthStatus']
export type HeadingSpan = components['schemas']['HeadingSpan']
export type FactRead = components['schemas']['FactRead']
export type PromptRunRead = components['schemas']['PromptRunRead']
export type ExtractionResultRead = components['schemas']['ExtractionResultRead']
export type ReviewQueueItemRead = components['schemas']['ReviewQueueItemRead']
export type ReviewQueueResponse = components['schemas']['ReviewQueueResponse']
export type ReviewMetricsRead = components['schemas']['ReviewMetricsRead']
export type ReviewItemDetailRead = components['schemas']['ReviewItemDetailRead']
export type ReviewActionCreate = components['schemas']['ReviewActionCreate']
export type ReviewActionRead = components['schemas']['ReviewActionRead']
export type ReviewHistoryResponse = components['schemas']['ReviewHistoryResponse']
export type PracticeLifecycleEventRead = components['schemas']['PracticeLifecycleEventRead']
export type LifecycleScanResult = components['schemas']['LifecycleScanResult']

const BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

export class ApiError extends Error {
  status: number
  code: string
  details: Record<string, unknown>
  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch (cause) {
    throw new ApiError(0, 'backend_unavailable', `backend not reachable at ${BASE}`, { cause: String(cause) })
  }
  if (response.status === 204) return undefined as T
  const bodyText = await response.text()
  let body: unknown = undefined
  try {
    body = bodyText ? JSON.parse(bodyText) : undefined
  } catch {
    throw new ApiError(response.status, 'non_json_response', 'backend returned a non-JSON response')
  }
  if (!response.ok) {
    const err = (body as { error?: { code?: string; message?: string; details?: Record<string, unknown> } })?.error
    throw new ApiError(response.status, err?.code ?? 'http_error', err?.message ?? `HTTP ${response.status}`, err?.details)
  }
  return body as T
}

export const api = {
  health: () => request<HealthStatus>('/api/health'),
  listSources: () => request<SourceRead[]>('/api/sources'),
  getSource: (id: number) => request<SourceDetailRead>(`/api/sources/${id}`),
  createSource: (payload: {
    kind: 'fixture' | 'url'
    page_type: string
    fixture_id?: string | null
    url?: string | null
    label?: string | null
    intended_fields?: string[]
    approval_note?: string | null
  }) =>
    request<SourceRead>('/api/sources', { method: 'POST', body: JSON.stringify(payload) }),
  patchSource: (id: number, payload: { label?: string; page_type?: string; intended_fields?: string[]; approval_note?: string | null }) =>
    request<SourceRead>(`/api/sources/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteSource: (id: number) => request<void>(`/api/sources/${id}`, { method: 'DELETE' }),
  ingest: (sourceId: number) => request<IngestionResult>(`/api/ingestions/${sourceId}`, { method: 'POST' }),
  getSnapshot: (id: number) => request<SnapshotRead>(`/api/snapshots/${id}`),
  extractSnapshot: (snapshotId: number, force = false) =>
    request<ExtractionResultRead>(`/api/snapshots/${snapshotId}/extract?force=${force}`, { method: 'POST' }),
  getSnapshotFacts: (snapshotId: number) => request<FactRead[]>(`/api/snapshots/${snapshotId}/facts`),
  getSnapshotPromptRuns: (snapshotId: number) => request<PromptRunRead[]>(`/api/snapshots/${snapshotId}/prompt-runs`),
  listFacts: (params?: { status?: string; field_key?: string; snapshot_id?: number }) => {
    const q = new URLSearchParams()
    if (params?.status) q.set('status', params.status)
    if (params?.field_key) q.set('field_key', params.field_key)
    if (params?.snapshot_id) q.set('snapshot_id', String(params.snapshot_id))
    return request<FactRead[]>(`/api/facts${q.toString() ? `?${q}` : ''}`)
  },
  listPromptRuns: (snapshotId?: number) => {
    const q = snapshotId ? `?snapshot_id=${snapshotId}` : ''
    return request<PromptRunRead[]>(`/api/prompt-runs${q}`)
  },
  getReviewQueue: (params?: { entity_type?: string; status?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams()
    if (params?.entity_type) q.set('entity_type', params.entity_type)
    if (params?.status) q.set('status', params.status)
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    return request<ReviewQueueResponse>(`/api/reviews/queue${q.toString() ? `?${q}` : ''}`)
  },
  getReviewMetrics: () => request<ReviewMetricsRead>('/api/reviews/metrics'),
  getReviewItemDetail: (entityType: string, entityId: number) =>
    request<ReviewItemDetailRead>(`/api/reviews/${entityType}/${entityId}`),
  submitReviewAction: (entityType: string, entityId: number, payload: ReviewActionCreate) =>
    request<ReviewActionRead>(`/api/reviews/${entityType}/${entityId}/actions`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getReviewItemHistory: (entityType: string, entityId: number) =>
    request<ReviewHistoryResponse>(`/api/reviews/${entityType}/${entityId}/history`),
  listReviewActions: (params?: { entity_type?: string; reviewer?: string; action?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams()
    if (params?.entity_type) q.set('entity_type', params.entity_type)
    if (params?.reviewer) q.set('reviewer', params.reviewer)
    if (params?.action) q.set('action', params.action)
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    return request<ReviewHistoryResponse>(`/api/reviews/actions${q.toString() ? `?${q}` : ''}`)
  },
  listLifecycleEvents: (params?: { organisation_id?: number; event_type?: string; status?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams()
    if (params?.organisation_id) q.set('organisation_id', String(params.organisation_id))
    if (params?.event_type) q.set('event_type', params.event_type)
    if (params?.status) q.set('status', params.status)
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    return request<PracticeLifecycleEventRead[]>(`/api/lifecycle/events${q.toString() ? `?${q}` : ''}`)
  },
  getLifecycleEvent: (id: number) => request<PracticeLifecycleEventRead>(`/api/lifecycle/events/${id}`),
  scanLifecycle: (payload?: { source_id?: number; organisation_id?: number }) =>
    request<LifecycleScanResult>('/api/lifecycle/scan', {
      method: 'POST',
      body: JSON.stringify(payload ?? {}),
    }),
}

// Static inventory of the M0 fixture corpus (ids + archetypes match the
// manifest; the manifest itself is a repository artefact, not an API).
export const FIXTURE_OPTIONS = [
  { id: 'F1', archetype: 'home', label: 'Home page archetype' },
  { id: 'F2', archetype: 'services', label: 'Services page archetype' },
  { id: 'F3', archetype: 'contact', label: 'Contact page archetype (multi-scope hours)' },
  { id: 'F4', archetype: 'team', label: 'Team page archetype (role categories)' },
  { id: 'F5', archetype: 'vacancies', label: 'Vacancies page archetype' },
  { id: 'F6', archetype: 'policies', label: 'Policies page archetype (adversarial)' },
] as const
