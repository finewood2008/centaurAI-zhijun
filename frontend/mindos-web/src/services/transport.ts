import { hasProductScope, isDesktopProduct, productScopeEpoch, workspaceRequestSignal } from '../shared/productScope.ts'

export type ProductTransport = (path: string, init?: RequestInit) => Promise<Response>
let desktopTransport: ProductTransport | null = null

export function installProductTransport(transport: ProductTransport): () => void {
  desktopTransport = transport
  return () => { if (desktopTransport === transport) desktopTransport = null }
}

/** The only HTTP dispatch seam. Desktop never falls through to browser networking. */
export async function transportRequest(path: string, init: RequestInit = {}): Promise<Response> {
  if (!isDesktopProduct()) return fetch(path, init)
  if (!desktopTransport || !hasProductScope()) throw new Error('请先连接盒子，再使用知君。')
  const ticket = productScopeEpoch()
  const request = workspaceRequestSignal(init.signal)
  try {
    request.signal.throwIfAborted()
    const response = await desktopTransport(path, { ...init, signal: request.signal })
    if (ticket !== productScopeEpoch()) throw new DOMException('连接已变化', 'AbortError')
    request.signal.throwIfAborted()
    // Stream cancellation remains owned by the desktop driver until its body ends.
    return response
  } finally { request.dispose() }
}
