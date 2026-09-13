import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

function setup(t) {
  const originalXHR = globalThis.XMLHttpRequest
  const originalFetch = globalThis.fetch
  const requests = []
  let fetches = 0
  class FakeXHR {
    upload = {}
    headers = {}
    sends = 0
    aborts = 0
    constructor() { requests.push(this) }
    open(method, path) { this.method = method; this.path = path }
    setRequestHeader(name, value) { this.headers[name] = value }
    send(body) { this.body = body; this.sends++ }
    abort() { this.aborts++; this.onabort?.() }
    getAllResponseHeaders() { return 'Content-Type: application/json\r\nX-Request-Id: test-upload\r\n' }
    progress(loaded, total) { this.upload.onprogress?.({ loaded, total, lengthComputable: total > 0 }) }
    uploaded(loaded, total) { this.upload.onload?.({ loaded, total, lengthComputable: total > 0 }) }
    respond(status, content = '') {
      this.status = status
      this.statusText = status === 200 ? 'OK' : 'Rejected'
      this.response = new TextEncoder().encode(content).buffer
      this.onload?.()
    }
  }
  globalThis.XMLHttpRequest = FakeXHR
  globalThis.fetch = async () => { fetches++; return new Response('fetch') }
  t.after(() => { globalThis.XMLHttpRequest = originalXHR; globalThis.fetch = originalFetch })
  const source = readFileSync(new URL('../src/services/transport.ts', import.meta.url), 'utf8')
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const module = { exports: {} }
  new Function('require', 'module', 'exports', code)(() => ({ isDesktopProduct: () => false }), module, module.exports)
  const body = new FormData()
  body.append('file', new Blob(['test']), 'example.txt')
  return { ...module.exports, requests, body, fetches: () => fetches }
}

test('multipart upload reports actual bytes and remains pending after upload completion', async t => {
  const { transportRequest, requests, body, fetches } = setup(t)
  const progress = []
  let resolved = false
  const pending = transportRequest('/upload', {
    method: 'POST', body, headers: { 'X-Client': 'web' }, credentials: 'include',
    onUploadProgress: value => progress.push(value),
  }).then(response => { resolved = true; return response })
  const xhr = requests[0]
  assert.equal(xhr.method, 'POST')
  assert.equal(xhr.withCredentials, true)
  assert.equal(xhr.headers['x-client'], 'web')
  xhr.progress(123, 500)
  xhr.uploaded(500, 500)
  await Promise.resolve()
  assert.equal(resolved, false, 'upload completion is not business success')
  assert.deepEqual(progress, [
    { loaded: 123, total: 500, phase: 'uploading' },
    { loaded: 500, total: 500, phase: 'finalizing' },
  ])
  xhr.respond(200, '{"id":"uploaded"}')
  const response = await pending
  assert.deepEqual(await response.json(), { id: 'uploaded' })
  assert.equal(response.headers.get('x-request-id'), 'test-upload')
  assert.equal(xhr.upload.onprogress, null)
  assert.equal(xhr.sends, 1)
  assert.equal(fetches(), 0)
})

test('HTTP rejection keeps status and error body without retrying a completed upload', async t => {
  const { transportRequest, requests, body, fetches } = setup(t)
  const pending = transportRequest('/upload', { method: 'POST', body, onUploadProgress() {} })
  requests[0].uploaded(500, 500)
  requests[0].respond(413, '{"error":"too large"}')
  const response = await pending
  assert.equal(response.ok, false)
  assert.equal(response.status, 413)
  assert.deepEqual(await response.json(), { error: 'too large' })
  assert.equal(requests[0].sends, 1)
  assert.equal(fetches(), 0)
})

test('network failure does not fall back to fetch or retry the upload', async t => {
  const { transportRequest, requests, body, fetches } = setup(t)
  const pending = transportRequest('/upload', { method: 'POST', body, onUploadProgress() {} })
  requests[0].onerror()
  await assert.rejects(pending, TypeError)
  assert.equal(requests[0].sends, 1)
  assert.equal(requests[0].upload.onprogress, null)
  assert.equal(fetches(), 0)
})

test('abort interrupts the pending request and clears handlers without retry', async t => {
  const { transportRequest, requests, body, fetches } = setup(t)
  const controller = new AbortController()
  const pending = transportRequest('/upload', {
    method: 'POST', body, signal: controller.signal, onUploadProgress() {},
  })
  controller.abort()
  await assert.rejects(pending, { name: 'AbortError' })
  assert.equal(requests[0].aborts, 1)
  assert.equal(requests[0].sends, 1)
  assert.equal(requests[0].onload, null)
  assert.equal(requests[0].upload.onload, null)
  assert.equal(fetches(), 0)
})

test('already aborted uploads never send', async t => {
  const { transportRequest, requests, body } = setup(t)
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(transportRequest('/upload', {
    method: 'POST', body, signal: controller.signal, onUploadProgress() {},
  }), { name: 'AbortError' })
  assert.equal(requests[0].sends, 0)
})

test('throwing view callbacks and unknown lengths cannot fail or duplicate writes', async t => {
  const { transportRequest, requests, body, reportUploadProgress } = setup(t)
  const progress = []
  const callback = value => { progress.push(value); throw new Error('unmounted view') }
  assert.doesNotThrow(() => reportUploadProgress(callback, { loaded: 0, total: 0, phase: 'uploading' }))
  const pending = transportRequest('/upload', { method: 'POST', body, onUploadProgress: callback })
  requests[0].progress(4, 0)
  requests[0].uploaded(4, 0)
  requests[0].respond(204)
  assert.equal((await pending).status, 204)
  assert.equal(requests[0].sends, 1)
  assert.deepEqual(progress[1], { loaded: 4, total: 0, phase: 'uploading' })
})

test('ordinary requests and uploads without observers keep using fetch', async t => {
  const { transportRequest, requests, body, fetches } = setup(t)
  await transportRequest('/upload', { method: 'POST', body })
  await transportRequest('/json', { method: 'POST', body: '{}', onUploadProgress() {} })
  assert.equal(fetches(), 2)
  assert.equal(requests.length, 0)
})
