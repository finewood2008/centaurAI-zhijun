/** Page-local, in-flight-only sharing. Never caches results or retries writes. */
export function createInFlightReads() {
  const pending = new Map<string, Promise<unknown>>()
  let generation = 0
  function run<T>(key: string, read: () => Promise<T>): Promise<T> {
    const existing = pending.get(key)
    if (existing) return existing as Promise<T>
    const ticket = generation
    const request = Promise.resolve().then(() => {
      if (ticket !== generation) throw new DOMException('The view no longer needs this read', 'AbortError')
      return read()
    })
    pending.set(key, request)
    const release = () => { if (pending.get(key) === request) pending.delete(key) }
    void request.then(release, release)
    return request
  }
  // Navigation must not reuse a previous view's outstanding response.
  function clear() { generation++; pending.clear() }
  return { run, clear }
}
