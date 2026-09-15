/** Pace empty job pages without holding the legacy serialized native channel.
 * The interval includes time already spent polling: real long polls need no
 * additional delay, and event-bearing pages are always drained immediately.
 */
export function createPollPacing() {
  let emptyPages = 0
  return {
    nextDelay(hasProgress: boolean, elapsedMs: number): number {
      if (hasProgress) { emptyPages = 0; return 0 }
      emptyPages = Math.min(emptyPages + 1, 5)
      const interval = Math.min(2000, 125 * 2 ** (emptyPages - 1))
      return Math.max(0, interval - Math.max(0, elapsedMs))
    },
  }
}

export function waitForPoll(milliseconds: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted()
  if (milliseconds <= 0) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', abort)
      reject(signal.reason ?? new DOMException('操作已取消或连接已变化', 'AbortError'))
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, milliseconds)
    signal.addEventListener('abort', abort, { once: true })
  })
}
