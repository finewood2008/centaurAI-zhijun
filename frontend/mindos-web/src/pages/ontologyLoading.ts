import type { OntologyView } from '@/components/ontology/summary'

export interface OntologyLoadPlan {
  readonly claims: boolean
  readonly overview: boolean
  readonly stats: boolean
  // 摘要视图读后端核心画像（知君实际带着的那一页）；overview 仍要读，画像读不到时回退到前端分组
  readonly profile: boolean
}

/** Load only data rendered by the active ontology view. */
export function ontologyLoadPlan(view: OntologyView, surface: string): OntologyLoadPlan {
  if (surface === 'inbox' || surface === 'proposals') return { claims: true, overview: false, stats: true, profile: false }
  if (view === 'summary') return { claims: false, overview: true, stats: false, profile: true }
  if (view === 'map') return { claims: false, overview: true, stats: true, profile: false }
  return { claims: true, overview: false, stats: true, profile: false }
}

/** Deduplicate slow stats reads, discard stale values and stop retries after unmount. */
export function createOntologyStatsLoader<T>(read: () => Promise<T>, needed: () => boolean, accept: (value: T) => void) {
  let revision = 0
  let disposed = false
  let pending: Promise<void> | null = null
  function load(): Promise<void> {
    if (disposed) return Promise.resolve()
    if (pending) return pending
    const started = revision
    pending = (async () => {
      try {
        const value = await read()
        if (!disposed && started === revision) accept(value)
      } catch {
        // Stats never block the claims view; a later view entry may retry.
      } finally {
        pending = null
        if (!disposed && started !== revision && needed()) void load()
      }
    })()
    return pending
  }
  return {
    load,
    invalidate() { revision++; if (!disposed && !pending && needed()) void load() },
    dispose() { disposed = true; revision++ },
  }
}
