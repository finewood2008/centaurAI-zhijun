import type { CallContext, Result } from '../../../shared/desktop-contract'
import type { ProductDesktop, ProductOperationRequest, ProductStart } from '../../../shared/product-contract'
import { resolveProductOperation, type ProductOperation } from '../services/productCatalog'
import { onProductScopeReset, workspaceRequestSignal } from '../shared/productScope.ts'

interface Binding { generation: number; workspaceId: string }
class ProductFailure extends Error {
  constructor(message: string, readonly code = 'TRANSPORT_UNAVAILABLE', readonly status = 503) { super(message) }
}
const CHUNK_BYTES = 524288
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'interrupted'])
const cancelled = () => new DOMException('操作已取消或连接已变化', 'AbortError')

/** Adapt bounded host jobs to the original product's Response/ReadableStream contract. */
export function createDesktopProductClient(product: ProductDesktop, binding: () => Binding | null) {
  const media = new Map<string, { handle: string; generation: number }>()
  let disposed = false
  const current = (owner: Binding) => !disposed && binding()?.workspaceId === owner.workspaceId && binding()?.generation === owner.generation
  const owner = () => {
    const value = binding()
    if (disposed || !value) throw new ProductFailure('请先连接盒子，再使用知君。')
    return { ...value }
  }
  const context = (value: Binding): CallContext => ({ callId: crypto.randomUUID(), expectedGeneration: value.generation })
  function unwrap<T>(result: Result<T>, value: Binding): T {
    if (!current(value) || result.generation !== value.generation) throw cancelled()
    if (!result.ok) throw new ProductFailure(result.error.message, result.error.code)
    return result.data
  }
  async function rpc<T>(promise: Promise<Result<T>>, value: Binding, signal?: AbortSignal): Promise<T> {
    signal?.throwIfAborted()
    if (!signal) return unwrap(await promise, value)
    return new Promise<T>((resolve, reject) => {
      const abort = () => reject(signal.reason ?? cancelled())
      signal.addEventListener('abort', abort, { once: true })
      promise.then(result => {
        try { signal.throwIfAborted(); resolve(unwrap(result, value)) } catch (error) { reject(error) }
      }, () => reject(new ProductFailure('桌面服务未完成操作，请检查连接。'))).finally(() => signal.removeEventListener('abort', abort))
    })
  }
  const ignore = (work: Promise<unknown>) => { void work.catch(() => {}) }
  function requestDefinition(path: string, init: RequestInit): { definition: ProductOperationRequest; operation: ProductOperation } {
    const { operation, params, query } = resolveProductOperation(path, (init.method ?? 'GET').toUpperCase())
    const headers = new Headers(init.headers)
    let requestId: string = crypto.randomUUID()
    const idempotency = headers.get('Idempotency-Key')
    if (idempotency) {
      if (!operation.idempotencyHeader || !/^[A-Za-z0-9_-]{8,100}$/.test(idempotency)) throw new ProductFailure('操作编号无效', 'INVALID_REQUEST', 400)
      requestId = idempotency
    }
    let body: unknown = null
    if (operation.body === 'json') {
      if (typeof init.body !== 'string' || new TextEncoder().encode(init.body).byteLength > operation.maxRequestBytes) throw new ProductFailure('请求内容超出限制', 'INVALID_REQUEST', 400)
      try { body = JSON.parse(init.body) } catch { throw new ProductFailure('请求内容无效', 'INVALID_REQUEST', 400) }
      // Preserve existing stable user action keys across an explicit retry.
      const actionId = body && typeof body === 'object' && !Array.isArray(body) ? (body as { requestId?: unknown }).requestId : undefined
      if (!idempotency && typeof actionId === 'string' && /^[A-Za-z0-9_-]{8,100}$/.test(actionId)) requestId = actionId
    } else if (operation.body === 'none' && init.body != null) throw new ProductFailure('读取请求不能带正文', 'INVALID_REQUEST', 400)
    return { operation, definition: { version: 1, requestId, operationId: operation.id, params, query, body } }
  }
  async function upload(form: FormData, value: Binding, signal: AbortSignal, operation: ProductOperation, owned: string[]) {
    const fields: Record<string, string> = {}
    const files: Array<{ field: string; uploadId: string }> = []
    const allowed = operation.path.endsWith('/versions') ? ['versionNote', 'targetFolderId'] : operation.path === '/api/mindos/uploads' ? ['folderId'] : []
    for (const [field, entry] of form) {
      signal.throwIfAborted()
      if (typeof entry === 'string') {
        if (!allowed.includes(field) || Object.prototype.hasOwnProperty.call(fields, field) || entry.length > 4000) throw new ProductFailure('上传参数无效', 'INVALID_REQUEST', 400)
        fields[field] = entry
        continue
      }
      if (field !== 'file' || files.length || entry.size < 1 || entry.size > 209715200) throw new ProductFailure('文件大小或数量超出限制', 'INVALID_REQUEST', 400)
      const create = product.uploadCreate(context(value), { requestId: crypto.randomUUID(), fileName: entry.name, contentType: entry.type || 'application/octet-stream', size: entry.size })
      create.then(result => { if (signal.aborted && result.ok) ignore(product.uploadCancel(context(value), { id: result.data.id })) }, () => {})
      const created = await rpc(create, value, signal)
      owned.push(created.id)
      if (created.state !== 'open' || created.received !== 0 || created.nextIndex !== 0 || created.size !== entry.size) throw new ProductFailure('文件传输状态无效')
      let index = 0
      for (let offset = 0; offset < entry.size; offset += CHUNK_BYTES) {
        signal.throwIfAborted()
        const bytes = new Uint8Array(await entry.slice(offset, offset + CHUNK_BYTES).arrayBuffer())
        const chunk = await rpc(product.uploadChunk(context(value), { id: created.id, index, bytes }), value, signal)
        index++
        if (chunk.id !== created.id || chunk.nextIndex !== index || chunk.received !== offset + bytes.byteLength) throw new ProductFailure('文件分片状态无效')
      }
      const complete = await rpc(product.uploadComplete(context(value), { id: created.id }), value, signal)
      if (complete.id !== created.id || complete.state !== 'complete' || complete.received !== entry.size) throw new ProductFailure('文件传输未完成')
      files.push({ field, uploadId: created.id })
    }
    if (files.length !== 1) throw new ProductFailure('请选择要上传的文件', 'INVALID_REQUEST', 400)
    return { kind: 'multipart', fields, files }
  }

  async function request(path: string, init: RequestInit = {}): Promise<Response> {
    const value = owner()
    const { operation, definition: original } = requestDefinition(path, init)
    const lifetime = workspaceRequestSignal(init.signal)
    const signal = lifetime.signal
    const uploads: string[] = []
    let job: ProductStart | undefined
    let finished = false
    let cancelSent = false
    const cancelJob = () => {
      if (job && !finished && !cancelSent) {
        cancelSent = true
        ignore(product.cancel(context(value), { id: job.id, requestId: original.requestId }))
      }
    }
    const cleanup = () => {
      signal.removeEventListener('abort', cancelJob)
      lifetime.dispose()
      for (const id of uploads) ignore(product.uploadCancel(context(value), { id }))
    }
    try {
      signal.throwIfAborted()
      const definition = operation.body === 'multipart'
        ? { ...original, body: await upload(init.body instanceof FormData ? init.body : new FormData(), value, signal, operation, uploads) }
        : original
      const starting = product.start(context(value), definition)
      starting.then(result => {
        if (signal.aborted && result.ok) ignore(product.cancel(context(value), { id: result.data.id, requestId: definition.requestId }))
      }, () => {})
      job = await rpc(starting, value, signal)
      signal.addEventListener('abort', cancelJob, { once: true })
      let acceptHeaders!: (head: { status: number; headers: Record<string, string> }) => void
      let rejectHeaders!: (error: unknown) => void
      const headersReady = new Promise<{ status: number; headers: Record<string, string> }>((resolve, reject) => { acceptHeaders = resolve; rejectHeaders = reject })
      let receivedHeaders = false
      let receivedEnd = false
      let total = 0
      let cursor = 0
      const deadline = Date.now() + 600000
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          const enqueue = (bytes: Uint8Array) => {
            if (!(bytes instanceof Uint8Array) || !receivedHeaders || receivedEnd) throw new ProductFailure('响应顺序无效')
            total += bytes.byteLength
            if (total > operation.maxResponseBytes) throw new ProductFailure('响应内容超出此操作的大小限制', 'RESPONSE_TOO_LARGE', 413)
            controller.enqueue(bytes)
          }
          void (async () => {
            try {
              while (!receivedEnd) {
                signal.throwIfAborted()
                if (Date.now() > deadline) throw new ProductFailure('操作等待超时，写入结果可能需要重新核对', 'REQUEST_TIMEOUT', 504)
                const page = await rpc(product.poll(context(value), { id: job!.id, after: cursor, waitMs: 8000 }), value, signal)
                if (page.id !== job!.id || page.cursor < cursor) throw new ProductFailure('响应游标无效')
                for (const event of page.events) {
                  if (event.seq !== cursor + 1 || receivedEnd) throw new ProductFailure('响应事件顺序无效')
                  cursor = event.seq
                  if (event.kind === 'headers') {
                    if (receivedHeaders || event.status < 200 || event.status > 599) throw new ProductFailure('响应头无效')
                    receivedHeaders = true
                    acceptHeaders({ status: event.status, headers: { ...event.headers } })
                  } else if (event.kind === 'chunk') enqueue(event.data)
                  else if (event.kind === 'blob') {
                    if (event.blob.size > operation.maxResponseBytes) throw new ProductFailure('响应内容超出大小限制', 'RESPONSE_TOO_LARGE', 413)
                    let offset = 0
                    while (offset < event.blob.size) {
                      const part = await rpc(product.blobRead(context(value), { id: event.blob.id, offset, limit: CHUNK_BYTES }), value, signal)
                      if (part.id !== event.blob.id || part.offset !== offset || part.size !== event.blob.size || !part.data.byteLength) throw new ProductFailure('响应分片无效')
                      enqueue(part.data); offset += part.data.byteLength
                      if (part.hasMore !== (offset < part.size)) throw new ProductFailure('响应分片未完成')
                    }
                  } else if (event.kind === 'error') throw new ProductFailure(event.message, event.code)
                  else if (event.kind === 'end') {
                    if (!receivedHeaders) throw new ProductFailure('响应没有返回状态')
                    receivedEnd = true
                  }
                }
                if (page.cursor !== cursor) throw new ProductFailure('响应游标与事件不一致')
                if (TERMINAL.has(page.state) && !page.hasMore && !receivedEnd) throw new ProductFailure('操作已结束，但响应不完整')
                if (!receivedEnd && !page.events.length) await new Promise(resolve => setTimeout(resolve, 25))
              }
              finished = true
              controller.close()
            } catch (error) {
              cancelJob()
              rejectHeaders(error)
              try { controller.error(error) } catch { /* Reader already cancelled. */ }
            } finally { finished = true; cleanup() }
          })()
        },
        cancel() { cancelJob(); lifetime.abort(); finished = true; cleanup() },
      })
      const head = await headersReady
      return new Response(head.status === 204 || head.status === 205 || head.status === 304 ? null : stream, head)
    } catch (error) { cancelJob(); finished = true; cleanup(); throw error }
  }

  async function saveText(fileName: string, text: string, contentType: string): Promise<void> {
    const value = owner()
    const bytes = new TextEncoder().encode(text)
    if (bytes.byteLength > 16777216) throw new ProductFailure('导出文件超出 16 MiB 限制', 'RESPONSE_TOO_LARGE', 413)
    const saved = await rpc(product.save(context(value), { fileName, contentType, source: { kind: 'bytes', bytes } }), value)
    if (!saved.saved) throw new DOMException('已取消保存', 'AbortError')
  }
  async function saveResource(path: string, fileName: string): Promise<void> {
    const value = owner()
    const { definition, operation } = requestDefinition(path, { method: 'GET' })
    if (operation.response !== 'bytes') throw new ProductFailure('此操作不是原件下载', 'INVALID_REQUEST', 400)
    const lifetime = workspaceRequestSignal()
    let job: ProductStart | undefined
    let ended = false
    try {
      const starting = product.start(context(value), definition)
      starting.then(result => {
        if ((lifetime.signal.aborted || !current(value)) && result.ok) ignore(product.cancel(context(value), { id: result.data.id, requestId: definition.requestId }))
      }, () => {})
      job = await rpc(starting, value, lifetime.signal)
      let cursor = 0
      let status = 0
      let resource: import('../../../shared/product-contract').ProductBlob | undefined
      const deadline = Date.now() + 600000
      while (!ended) {
        if (Date.now() > deadline) throw new ProductFailure('原件准备超时')
        const page = await rpc(product.poll(context(value), { id: job.id, after: cursor, waitMs: 8000 }), value, lifetime.signal)
        if (page.id !== job.id || page.cursor < cursor) throw new ProductFailure('原件响应游标无效')
        for (const event of page.events) {
          if (event.seq !== cursor + 1 || ended) throw new ProductFailure('原件响应顺序无效')
          cursor = event.seq
          if (event.kind === 'headers') {
            if (status || event.status < 200 || event.status > 599) throw new ProductFailure('原件响应状态无效')
            status = event.status
          } else if (event.kind === 'blob') {
            if (!status || resource) throw new ProductFailure('原件响应重复或顺序无效')
            resource = event.blob
          }
          else if (event.kind === 'error') throw new ProductFailure(event.message, event.code)
          else if (event.kind === 'end') ended = true
        }
        if (page.cursor !== cursor) throw new ProductFailure('原件响应游标与事件不一致')
        if (!ended && !page.events.length) await new Promise(resolve => setTimeout(resolve, 25))
        if (TERMINAL.has(page.state) && !page.hasMore && !ended) throw new ProductFailure('原件准备未完成')
      }
      if (status !== 200 || !resource || resource.size > operation.maxResponseBytes) throw new ProductFailure('原件暂不可用')
      const saved = await rpc(product.save(context(value), { fileName, contentType: resource.contentType, source: { kind: 'blob', id: resource.id } }), value, lifetime.signal)
      if (!saved.saved) throw new DOMException('已取消保存', 'AbortError')
    } finally {
      if (job && !ended) ignore(product.cancel(context(value), { id: job.id, requestId: definition.requestId }))
      lifetime.dispose()
    }
  }
  async function preview(path: string, signal?: AbortSignal): Promise<string> {
    const value = owner()
    const { definition, operation } = requestDefinition(path, { method: 'GET' })
    if (operation.response !== 'bytes') throw new ProductFailure('此操作不是原件预览', 'INVALID_REQUEST', 400)
    const pending = product.openMedia(context(value), definition)
    pending.then(result => {
      if ((signal?.aborted || !current(value)) && result.ok) ignore(product.closeMedia(context(value), { handle: result.data.handle }))
    }, () => {})
    const resource = await rpc(pending, value, signal)
    if (!/^zhijun-media:\/\/session\/[a-f0-9]{32}$/.test(resource.url)) {
      ignore(product.closeMedia(context(value), { handle: resource.handle }))
      throw new ProductFailure('原件地址无效')
    }
    media.set(resource.url, { handle: resource.handle, generation: value.generation })
    return resource.url
  }
  function releasePreview(url: string): void {
    const resource = media.get(url)
    if (!resource) return
    media.delete(url)
    ignore(product.closeMedia({ callId: crypto.randomUUID(), expectedGeneration: resource.generation }, { handle: resource.handle }))
  }
  async function requestMicrophone(): Promise<boolean> {
    const value = owner()
    return (await rpc(product.requestMicrophone(context(value)), value)).allowed
  }
  const reset = () => { for (const url of [...media.keys()]) releasePreview(url) }
  const stopReset = onProductScopeReset(reset)
  return { request, requestMicrophone, saveText, saveResource, preview, releasePreview, dispose() { reset(); disposed = true; stopReset() } }
}
