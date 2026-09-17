/** Connection-scoped navigation hint only; this never caches business data or authorization. */
export function createNavigationProgressReader<T extends { state: string }>(
  load: () => Promise<T>,
  enabled: () => boolean,
) {
  let generation = 0
  let ready: T | null = null
  let pending: Promise<T> | null = null

  function invalidate(): void {
    generation++
    ready = null
    pending = null
  }

  function read(): Promise<T> {
    // Preserve the browser application's fresh-read behavior.
    if (!enabled()) return load()
    if (ready) {
      const ticket = generation, value = ready
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
      // Ready is terminal for the current product scope. A box/session change
      // and every onboarding write already invalidate this reader explicitly.
      if (value.state === 'ready') ready = value
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
