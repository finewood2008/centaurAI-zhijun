import { hasProductScope, isDesktopProduct, productScopeEpoch, workspaceRequestSignal } from '../shared/productScope.ts'

export interface UploadProgress {
  loaded: number
  total: number
  phase: 'uploading' | 'finalizing'
}

export interface ProductRequestInit extends RequestInit {
  onUploadProgress?: (progress: UploadProgress) => void
}

/** Progress is observational: a view callback must never fail or retry a write. */
export function reportUploadProgress(callback: ProductRequestInit['onUploadProgress'], progress: UploadProgress): void {
  try { callback?.(progress) } catch { /* The request remains owned by the transport. */ }
}

export type ProductTransport = (path: string, init?: ProductRequestInit) => Promise<Response>
let desktopTransport: ProductTransport | null = null

function uploadWithProgress(path: string, init: ProductRequestInit): Promise<Response> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    let settled = false
    let loaded = 0
    let total = 0
    const cleanup = () => {
      init.signal?.removeEventListener('abort', abort)
      xhr.onload = xhr.onerror = xhr.onabort = xhr.ontimeout = null
      xhr.upload.onprogress = xhr.upload.onload = null
    }
    const fail = (reason: unknown) => {
      if (settled) return
      settled = true
      cleanup()
      reject(reason)
    }
    const abort = () => {
      fail(init.signal?.reason ?? new DOMException('操作已取消', 'AbortError'))
      xhr.abort()
    }
    if (init.signal?.aborted) {
      fail(init.signal.reason ?? new DOMException('操作已取消', 'AbortError'))
      return
    }
    try {
      xhr.open(init.method ?? 'GET', path, true)
      xhr.responseType = 'arraybuffer'
      xhr.withCredentials = init.credentials === 'include'
      new Headers(init.headers).forEach((value, key) => xhr.setRequestHeader(key, value))
      xhr.upload.onprogress = event => {
        loaded = event.loaded
        total = event.lengthComputable ? event.total : 0
        reportUploadProgress(init.onUploadProgress, { loaded, total, phase: 'uploading' })
      }
      xhr.upload.onload = event => {
        loaded = event.loaded
        total = event.lengthComputable ? event.total : total
        // Bytes are sent, but the server may still be processing or reject the file.
        reportUploadProgress(init.onUploadProgress, { loaded, total, phase: 'finalizing' })
      }
      xhr.onload = () => {
        if (settled) return
        try {
          if (!xhr.status) throw new TypeError('Failed to fetch')
          const headers = new Headers()
          for (const line of xhr.getAllResponseHeaders().trim().split(/[\r\n]+/)) {
            const separator = line.indexOf(':')
            if (separator > 0) headers.append(line.slice(0, separator), line.slice(separator + 1).trim())
          }
          const body = [204, 205, 304].includes(xhr.status) ? null : xhr.response
          const response = new Response(body, { status: xhr.status, statusText: xhr.statusText, headers })
          settled = true
          cleanup()
          resolve(response)
        } catch (error) { fail(error) }
      }
      xhr.onerror = () => fail(new TypeError('Failed to fetch'))
      xhr.ontimeout = () => fail(new TypeError('Network request timed out'))
      xhr.onabort = () => fail(new DOMException('操作已取消', 'AbortError'))
      init.signal?.addEventListener('abort', abort, { once: true })
      xhr.send(init.body as FormData)
    } catch (error) { fail(error) }
  })
}

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
        // Preserve the in-process completion marker; an unqualified cancel
        // would turn successful SSE cleanup into remote task cancellation.
        await reader.cancel(reason)
      } finally {
        finish()
      }
    },
  })
  return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers })
}

/** The only HTTP dispatch seam. Desktop never falls through to browser networking. */
export async function transportRequest(path: string, init: ProductRequestInit = {}): Promise<Response> {
  if (!isDesktopProduct()) {
    if (init.onUploadProgress && init.body instanceof FormData) return uploadWithProgress(path, init)
    const { onUploadProgress: _progress, ...requestInit } = init
    return fetch(path, requestInit)
  }
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
