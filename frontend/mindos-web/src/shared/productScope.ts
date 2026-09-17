/** Desktop workspace lifetime. Web pages keep their existing storage behavior. */
let desktop = false
let scope: string | null = null
let epoch = 0
const resets = new Set<() => void>()
const controllers = new Set<AbortController>()
const objectUrls = new Set<string>()
const storageKeys = new Set<string>()

export function isDesktopProduct(): boolean { return desktop }
export function enableDesktopProduct(): void { desktop = true }
export function productScopeEpoch(): number { return epoch }
export function hasProductScope(): boolean { return !desktop || scope !== null }
export function onProductScopeReset(reset: () => void): () => void {
  resets.add(reset)
  return () => resets.delete(reset)
}
export function setProductScope(next: string | null): void {
  if (scope === next) return
  scope = next
  epoch++
  for (const controller of controllers) controller.abort(new DOMException('连接已变化', 'AbortError'))
  controllers.clear()
  for (const reset of resets) { try { reset() } catch { /* Complete every cleanup. */ } }
  for (const url of objectUrls) URL.revokeObjectURL(url)
  objectUrls.clear()
  for (const key of storageKeys) { try { sessionStorage.removeItem(key) } catch { /* Storage can be unavailable. */ } }
  storageKeys.clear()
}
export function workspaceRequestSignal(signal?: AbortSignal | null): { signal: AbortSignal; abort: () => void; dispose: () => void } {
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  if (signal?.aborted) abort()
  else signal?.addEventListener('abort', abort, { once: true })
  if (desktop) controllers.add(controller)
  return { signal: controller.signal, abort: () => controller.abort(new DOMException('操作已取消', 'AbortError')), dispose: () => {
    controllers.delete(controller)
    signal?.removeEventListener('abort', abort)
  } }
}
export function trackProductObjectUrl(url: string): string { if (desktop) objectUrls.add(url); return url }
export function releaseProductObjectUrl(url: string): void { objectUrls.delete(url); URL.revokeObjectURL(url) }

/** Capture the owning epoch at component creation, including delayed unmount writes. */
export function createProductSessionStorage(): Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> {
  const ownerEpoch = epoch
  const owner = scope
  const valid = () => !desktop || (owner !== null && ownerEpoch === epoch && owner === scope)
  const keyFor = (key: string) => desktop ? `zhijun.desktop.${encodeURIComponent(owner ?? '')}.${key}` : key
  return {
    getItem(key) { return valid() ? sessionStorage.getItem(keyFor(key)) : null },
    setItem(key, value) {
      if (!valid()) return
      const target = keyFor(key)
      sessionStorage.setItem(target, value)
      if (desktop) storageKeys.add(target)
    },
    removeItem(key) { if (valid()) sessionStorage.removeItem(keyFor(key)) },
  }
}
