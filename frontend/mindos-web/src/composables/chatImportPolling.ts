const TRANSITIONAL_BATCH_STATES = new Set(['uploading', 'queued', 'waiting', 'replying'])
const TRANSITIONAL_FILE_STATES = new Set(['pending', 'uploading', 'saved', 'reading'])

export interface ImportPollingBatch {
  readonly state: string
  readonly files: readonly { readonly state: string }[]
}

export function hasTransitionalImports(items: readonly ImportPollingBatch[]): boolean {
  return items.some(batch => TRANSITIONAL_BATCH_STATES.has(batch.state)
    || batch.files.some(file => TRANSITIONAL_FILE_STATES.has(file.state)))
}

interface TimerApi {
  set(callback: () => void, delayMs: number): unknown
  clear(handle: unknown): void
}

export interface ChatImportPollerOptions<T> {
  read: (conversationId: string) => Promise<T>
  apply: (value: T) => void
  signature: (value: T) => string
  isTransitional: (value: T) => boolean
  refreshMessages: (conversationId: string) => Promise<boolean>
  isTargetCurrent?: (conversationId: string) => boolean
  onError: (error: unknown) => void
  onSuccess?: () => void
  intervalMs?: number
  maxBackoffMs?: number
  timers?: TimerApi
}

export interface ChatImportPoller {
  start(conversationId: string | null | undefined): Promise<void>
  refresh(conversationId?: string | null): Promise<void>
  stop(): void
  dispose(): void
}

const defaultTimers: TimerApi = {
  set: (callback, delayMs) => setTimeout(callback, delayMs),
  clear: handle => clearTimeout(handle as ReturnType<typeof setTimeout>),
}

/** One request per conversation generation, with one queued refresh after an explicit mutation. */
export function createChatImportPoller<T>(options: ChatImportPollerOptions<T>): ChatImportPoller {
  const intervalMs = options.intervalMs ?? 2500
  const maxBackoffMs = options.maxBackoffMs ?? 30000
  const timers = options.timers ?? defaultTimers
  let alive = true
  let generation = 0
  let conversationId: string | null = null
  let baseline: string | null = null
  let failures = 0
  let timer: unknown
  let rerunRequested = false
  let flight: { generation: number; conversationId: string; promise: Promise<void> } | null = null

  const current = (epoch: number, id: string) => alive && generation === epoch && conversationId === id
    && (options.isTargetCurrent?.(id) ?? true)
  const clearTimer = () => {
    if (timer !== undefined) timers.clear(timer)
    timer = undefined
  }
  const schedule = (entry: { generation: number; conversationId: string }, delayMs: number) => {
    if (!current(entry.generation, entry.conversationId)) return
    clearTimer()
    timer = timers.set(() => {
      timer = undefined
      if (current(entry.generation, entry.conversationId)) void launch()
    }, delayMs)
  }

  function launch(): Promise<void> {
    if (!alive || !conversationId) return Promise.resolve()
    clearTimer()
    const entry = { generation, conversationId, promise: Promise.resolve() }
    entry.promise = (async () => {
      let nextDelay: number | null = null
      try {
        const value = await options.read(entry.conversationId)
        if (!current(entry.generation, entry.conversationId)) return
        options.apply(value)
        options.onSuccess?.()
        failures = 0
        const nextSignature = options.signature(value)
        if (baseline === null) {
          // The initial listing is observation, not a state transition.
          baseline = nextSignature
        } else if (nextSignature !== baseline) {
          await options.refreshMessages(entry.conversationId)
          if (!current(entry.generation, entry.conversationId)) return
          baseline = nextSignature
        }
        if (options.isTransitional(value)) nextDelay = intervalMs
      } catch (error) {
        if (!current(entry.generation, entry.conversationId)) return
        failures += 1
        options.onError(error)
        nextDelay = Math.min(intervalMs * (2 ** (failures - 1)), maxBackoffMs)
      } finally {
        if (flight === entry) flight = null
        if (!current(entry.generation, entry.conversationId)) return
        if (rerunRequested) {
          rerunRequested = false
          void launch()
        } else if (nextDelay !== null) schedule(entry, nextDelay)
      }
    })()
    flight = entry
    return entry.promise
  }

  function requestRefresh(explicit: boolean): Promise<void> {
    const activeFlight = flight
    if (activeFlight && current(activeFlight.generation, activeFlight.conversationId)) {
      if (!explicit) return activeFlight.promise
      rerunRequested = true
      return activeFlight.promise.then(() => flight?.promise ?? Promise.resolve())
    }
    return launch()
  }

  function reset(nextId: string | null): void {
    generation += 1
    clearTimer()
    conversationId = nextId
    baseline = null
    failures = 0
    rerunRequested = false
  }

  return {
    start(id) {
      reset(id || null)
      return conversationId ? requestRefresh(false) : Promise.resolve()
    },
    refresh(id = conversationId) {
      if (!alive || !id) return Promise.resolve()
      if (id !== conversationId) {
        reset(id)
        return requestRefresh(false)
      }
      return requestRefresh(true)
    },
    stop() { reset(null) },
    dispose() { alive = false; reset(null) },
  }
}
