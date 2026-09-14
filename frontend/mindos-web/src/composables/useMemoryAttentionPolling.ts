/** Session-local observation: never discover old jobs for automatic recovery. */
export function createMemoryAttentionPoller(options: {
  isCurrent: (conversationId: string) => boolean
  poll: (conversationId: string, jobIds: string[]) => Promise<boolean>
  onTimeout: (conversationId: string) => void
  onSettled?: (conversationId: string) => void | Promise<void>
  schedule?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>
  cancel?: (timer: ReturnType<typeof setTimeout>) => void
  intervalMs?: number
  maxAttempts?: number
}) {
  const schedule = options.schedule ?? setTimeout
  const cancel = options.cancel ?? clearTimeout
  let session = 0
  let conversation = ''
  let timer: ReturnType<typeof setTimeout> | undefined
  const tracked = new Set<string>()
  function stop() {
    session++
    if (timer !== undefined) cancel(timer)
    timer = undefined
    conversation = ''
    tracked.clear()
  }
  function start(conversationId: string, jobIds: string[] = []) {
    if (!options.isCurrent(conversationId)) return
    if (conversation !== conversationId) stop()
    if (timer !== undefined) cancel(timer)
    conversation = conversationId
    for (const id of jobIds) if (typeof id === 'string' && id) tracked.add(id)
    const ticket = ++session
    let attempts = 0
    let idle = 0
    const valid = () => ticket === session && options.isCurrent(conversationId)
    const tick = async () => {
      if (!valid()) return
      let pending = true
      try { pending = await options.poll(conversationId, [...tracked]) }
      catch { /* A transient read error is not a completed job. */ }
      if (!valid()) return
      attempts++
      idle = pending ? 0 : idle + 1
      if (idle >= 3) {
        timer = undefined
        try { await options.onSettled?.(conversationId) }
        catch { if (valid()) options.onTimeout(conversationId) }
        return
      }
      if (attempts >= (options.maxAttempts ?? 40)) {
        timer = undefined
        options.onTimeout(conversationId)
        return
      }
      timer = schedule(() => { void tick() }, options.intervalMs ?? 3000)
    }
    timer = schedule(() => { void tick() }, options.intervalMs ?? 3000)
  }
  return { start, stop }
}
