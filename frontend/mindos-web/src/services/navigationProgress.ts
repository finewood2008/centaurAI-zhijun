/** Short-lived navigation hint only; this never caches business data or authorization. */
export function createNavigationProgressReader<T extends { state: string }>(
  load: () => Promise<T>,
  enabled: () => boolean,
  now: () => number = () => performance.now(),
) {
  let generation = 0
  let ready: { value: T; expiresAt: number } | null = null
  let pending: Promise<T> | null = null

  function invalidate(): void {
    generation++
    ready = null
    pending = null
  }

  function read(): Promise<T> {
    // Preserve the browser application's fresh-read behavior.
    if (!enabled()) return load()
    if (ready && now() < ready.expiresAt) {
      const ticket = generation, value = ready.value
      return Promise.resolve().then(() => {
        if (ticket !== generation) throw new DOMException('连接或引导状态已变化', 'AbortError')
        return value
      })
    }
    if (pending) return pending
    const ticket = generation
    const request = load().then(value => {
      if (ticket !== generation) throw new DOMException('连接或引导状态已变化', 'AbortError')
      // Incomplete onboarding must keep observing server-side progress.
      if (value.state === 'ready') ready = { value, expiresAt: now() + 30_000 }
      else ready = null
      return value
    }).finally(() => {
      if (pending === request) pending = null
    })
    pending = request
    return request
  }

  return { read, invalidate, revision: () => generation }
}
