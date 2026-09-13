/** Poll only while the owning panel is visible; one read may be in flight at a time. */
export function createVisiblePoller(
  read: (includeInitialData: boolean) => Promise<unknown>,
  intervalMs: number,
  timers = {
    set: (callback: () => void, delay: number) => setTimeout(callback, delay),
    clear: (timer: ReturnType<typeof setTimeout>) => clearTimeout(timer),
  },
) {
  let active = false
  let disposed = false
  let generation = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  let pending: Promise<void> | null = null

  function clearTimer() {
    if (timer !== null) timers.clear(timer)
    timer = null
  }

  function refresh(includeInitialData = true): Promise<void> {
    if (disposed || !active) return Promise.resolve()
    if (pending) return pending
    clearTimer()
    pending = Promise.resolve()
      .then(() => { if (!disposed && active) return read(includeInitialData) })
      .then(() => undefined, () => undefined)
      .finally(() => {
        pending = null
        if (!disposed && active) {
          timer = timers.set(() => {
            timer = null
            void refresh(false)
          }, intervalMs)
        }
      })
    return pending
  }

  return {
    refresh,
    // An action may finish while a poll that started before it is still running.
    // Coalesce those invalidations into one fresh read after that poll settles.
    invalidate(): Promise<void> {
      if (disposed || !active) return Promise.resolve()
      if (!pending) return refresh()
      const invalidatedGeneration = generation
      return pending.then(() => {
        if (!disposed && active && generation === invalidatedGeneration) return refresh()
      })
    },
    setActive(value: boolean) {
      if (disposed || active === value) return
      active = value
      generation++
      clearTimer()
      if (active) void refresh()
    },
    dispose() {
      disposed = true
      active = false
      generation++
      clearTimer()
    },
  }
}
