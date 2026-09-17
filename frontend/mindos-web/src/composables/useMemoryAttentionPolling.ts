/** Session-local observation: never discover old jobs for automatic recovery. */
export function createMemoryAttentionPoller(options: {
  isCurrent: (conversationId: string) => boolean
  poll: (conversationId: string, jobIds: string[]) => Promise<boolean>
  onTimeout: (conversationId: string) => void
  onSettled?: (conversationId: string) => void | Promise<void>
  onActiveChange?: (active: boolean) => void
  schedule?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>
  cancel?: (timer: ReturnType<typeof setTimeout>) => void
  intervalMs?: number
  maxIntervalMs?: number
  maxAttempts?: number
  maxDurationMs?: number
  now?: () => number
}) {
  const schedule = options.schedule ?? setTimeout
  const cancel = options.cancel ?? clearTimeout
  const now = options.now ?? Date.now
  let session = 0
  let conversation = ''
  let timer: ReturnType<typeof setTimeout> | undefined
  const tracked = new Set<string>()
  let active = false
  let revision = 0
  function setActive(value: boolean) {
    if (active === value) return
    active = value
    options.onActiveChange?.(value)
  }
  function stop() {
    session++
    if (timer !== undefined) cancel(timer)
    timer = undefined
    conversation = ''
    tracked.clear()
    setActive(false)
  }
  function start(conversationId: string, jobIds: string[] = []) {
    if (!options.isCurrent(conversationId)) return
    // A second event from the same turn/page joins the observation already in
    // flight. Restarting here used to overlap reads and reset the timeout.
    if (active && conversation === conversationId) {
      for (const id of jobIds) if (typeof id === 'string' && id) tracked.add(id)
      revision++
      return
    }
    stop()
    if (timer !== undefined) cancel(timer)
    conversation = conversationId
    for (const id of jobIds) if (typeof id === 'string' && id) tracked.add(id)
    const ticket = ++session
    const started = now()
    setActive(true)
    let attempts = 0
    let idle = 0
    const valid = () => ticket === session && options.isCurrent(conversationId)
    const finish = () => { timer = undefined; setActive(false) }
    const exhausted = () => attempts >= (options.maxAttempts ?? 40)
      || now() - started >= (options.maxDurationMs ?? 120_000)
    const tick = async () => {
      timer = undefined
      if (!valid()) { if (ticket === session) finish(); return }
      if (exhausted()) { finish(); options.onTimeout(conversationId); return }
      const observedRevision = revision
      let pending = true
      try { pending = await options.poll(conversationId, [...tracked]) }
      catch { /* A transient read error is not a completed job. */ }
      if (!valid()) { if (ticket === session) finish(); return }
      attempts++
      idle = pending || revision !== observedRevision ? 0 : idle + 1
      if (idle >= 3) {
        const settledRevision = revision
        try { await options.onSettled?.(conversationId) }
        catch { if (valid()) options.onTimeout(conversationId) }
        if (!valid()) { if (ticket === session) finish(); return }
        if (revision === settledRevision) { finish(); return }
        // Another turn can arrive while summaries are refreshing. Preserve its
        // observation without resetting this session's attempt/deadline budget.
        idle = 0
      }
      if (exhausted()) {
        finish()
        options.onTimeout(conversationId)
        return
      }
      // Pending or temporarily unreadable tasks don't need a three-read burst
      // every three seconds. Keep the first check quick, then back off boundedly.
      const interval = Math.min((options.intervalMs ?? 3000) * 2 ** Math.min(attempts, 2), options.maxIntervalMs ?? 12_000)
      timer = schedule(() => { void tick() }, Math.min(interval, Math.max(0, (options.maxDurationMs ?? 120_000) - (now() - started))))
    }
    timer = schedule(() => { void tick() }, options.intervalMs ?? 3000)
  }
  return { start, stop, isActive: () => active }
}
