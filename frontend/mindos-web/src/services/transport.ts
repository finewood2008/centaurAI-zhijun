import { hasProductScope, isDesktopProduct, productScopeEpoch, workspaceRequestSignal } from '../shared/productScope.ts'

export type ProductTransport = (path: string, init?: RequestInit) => Promise<Response>
let desktopTransport: ProductTransport | null = null

export function installProductTransport(transport: ProductTransport): () => void {
  desktopTransport = transport
  return () => { if (desktopTransport === transport) desktopTransport = null }
}

function retainRequestUntilBodyEnds(response: Response, dispose: () => void): Response {
  if (!response.body) {
    dispose()
    return response
  }
  const reader = response.body.getReader()
  let finished = false
  const finish = () => {
    if (finished) return
    finished = true
    try { reader.releaseLock() } catch { /* A read or cancellation may still own the lock. */ }
    dispose()
  }
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { value, done } = await reader.read()
        if (done) {
          finish()
          controller.close()
        } else {
          controller.enqueue(value)
        }
      } catch (error) {
        finish()
        controller.error(error)
      }
    },
    async cancel(reason) {
      try {
        await reader.cancel(reason)
      } finally {
        finish()
      }
    },
  })
  return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers })
}

/** The only HTTP dispatch seam. Desktop never falls through to browser networking. */
export async function transportRequest(path: string, init: RequestInit = {}): Promise<Response> {
  if (!isDesktopProduct()) return fetch(path, init)
  if (!desktopTransport || !hasProductScope()) throw new Error('请先连接盒子，再使用知君。')
  const ticket = productScopeEpoch()
  const request = workspaceRequestSignal(init.signal)
  let bodyOwnsRequest = false
  try {
    request.signal.throwIfAborted()
    const response = await desktopTransport(path, { ...init, signal: request.signal })
    if (ticket !== productScopeEpoch()) throw new DOMException('连接已变化', 'AbortError')
    request.signal.throwIfAborted()
    if (!response.body) return response
    const retained = retainRequestUntilBodyEnds(response, request.dispose)
    bodyOwnsRequest = true
    return retained
  } finally {
    if (!bodyOwnsRequest) request.dispose()
  }
}
