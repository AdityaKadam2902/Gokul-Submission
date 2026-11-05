/* Shared routing types: hash-based navigation without extra dependencies. */
export type Route = 'sources' | 'source' | 'snapshot' | 'facts' | 'prompt-runs' | 'health'
export type Loc = { page: Route; id?: number }
export type Nav = (loc: Loc) => void

export function parseHash(): Loc {
  const hash = window.location.hash.replace(/^#\/?/, '')
  const [page, rawId] = hash.split('/')
  const id = rawId ? Number(rawId) : undefined
  if (page === 'source' && Number.isFinite(id)) return { page: 'source', id }
  if (page === 'snapshot' && Number.isFinite(id)) return { page: 'snapshot', id }
  if (page === 'facts') return { page: 'facts' }
  if (page === 'prompt-runs') return { page: 'prompt-runs' }
  if (page === 'health') return { page: 'health' }
  return { page: 'sources' }
}

export function navTo(loc: Loc) {
  const hash = loc.page === 'source' && loc.id != null
    ? `#/source/${loc.id}`
    : loc.page === 'snapshot' && loc.id != null
      ? `#/snapshot/${loc.id}`
      : `#/${loc.page}`
  window.location.hash = hash
}

