export interface CardUpdateSnapshot {
  pendingUpdate?: { state: string; errorCode?: string | null } | null
}

export function createCardUpdatePoller<T extends CardUpdateSnapshot>(options: {
  fetch: (knowledgeId: string) => Promise<T>
  onResult: (knowledgeId: string, result: T) => void
  onTimeout?: (knowledgeId: string) => void
  fastDelayMs?: number
  slowDelayMs?: number
  fastPeriodMs?: number
  timeoutMs?: number
}) {
  let generation = 0
  let timer: ReturnType<typeof setTimeout> | null = null

  function stop() {
    generation += 1
    if (timer) clearTimeout(timer)
    timer = null
  }

  function start(knowledgeId: string) {
    stop()
    const current = generation
    const startedAt = Date.now()
    const fastDelay = options.fastDelayMs ?? 1000
    const slowDelay = options.slowDelayMs ?? 3000
    const fastPeriod = options.fastPeriodMs ?? 30000
    const timeout = options.timeoutMs ?? 120000

    const poll = async () => {
      try {
        const result = await options.fetch(knowledgeId)
        if (current !== generation) return
        options.onResult(knowledgeId, result)
        const pending = result.pendingUpdate
        if (!pending || pending.state === 'index_failed') return
      } catch {
        if (current !== generation) return
      }
      const elapsed = Date.now() - startedAt
      if (elapsed >= timeout) {
        options.onTimeout?.(knowledgeId)
        return
      }
      timer = setTimeout(poll, elapsed < fastPeriod ? fastDelay : slowDelay)
    }
    timer = setTimeout(poll, fastDelay)
  }

  return { start, stop }
}
