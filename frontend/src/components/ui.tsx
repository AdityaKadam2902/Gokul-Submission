/* Shared UI primitives: badges, panels and the four first-class states
   (loading / empty / error / backend-unavailable). */
import type { ReactNode } from 'react'

export function Badge({ kind, children }: { kind: 'fixture' | 'policy' | 'blocked' | 'ok' | 'neutral'; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>
}

export function PolicyBadge({ policy }: { policy: string }) {
  if (policy === 'blocked') return <Badge kind="blocked">blocked</Badge>
  if (policy === 'approved_live') return <Badge kind="ok">approved live</Badge>
  if (policy === 'fixture_only') return <Badge kind="policy">fixture-only</Badge>
  return <Badge kind="policy">manual fixture · pending policy check</Badge>
}

export function FixtureBadge() {
  return <Badge kind="fixture">SYNTHETIC FIXTURE</Badge>
}

export function Panel({ title, actions, children, tight = false }: { title?: string; actions?: ReactNode; children: ReactNode; tight?: boolean }) {
  return (
    <section className="panel">
      {(title || actions) && (
        <div className="panel-head">
          <div className="panel-title">{title}</div>
          {actions && <div>{actions}</div>}
        </div>
      )}
      <div className={`panel-body${tight ? ' tight' : ''}`}>{children}</div>
    </section>
  )
}

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="state">
      <div className="spinner" aria-label="loading" />
      <p>{label}</p>
    </div>
  )
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="state">
      <h3>{title}</h3>
      {hint && <p>{hint}</p>}
      {action}
    </div>
  )
}

export function ErrorState({ title, message, retry }: { title: string; message?: string; retry?: () => void }) {
  return (
    <div className="state">
      <h3>{title}</h3>
      {message && <p className="mono small">{message}</p>}
      {retry && (
        <button className="btn" onClick={retry}>
          Retry
        </button>
      )}
    </div>
  )
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
  } catch {
    return iso
  }
}

export function shortHash(hash: string | null | undefined): string {
  if (!hash) return '—'
  return hash.slice(0, 12)
}
