import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, extname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import * as Vue from 'vue'
import * as polling from '../src/composables/chatImportPolling.ts'

const read = path => readFile(new URL(path, import.meta.url), 'utf8')
const code = ts.transpileModule(await read('../src/composables/useChatImports.ts'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
function imports(desktop, { conversationId = null, apiModule, realPoller = false } = {}) {
  let pollerOptions
  let timerId = 0
  const notices = [], calls = [], exports = {}, cleanup = [], timers = new Map()
  const forbidden = new Proxy({}, { get: (_, name) => () => { calls.push(name); throw Error('Unexpected material request') } })
  new Function('require', 'exports', code)(id => {
    if (id === 'vue') return { ...Vue, onBeforeUnmount: callback => cleanup.push(callback) }
    if (id.includes('productScope')) return { isDesktopProduct: () => desktop }
    if (id.includes('services/api')) return apiModule ?? { api: forbidden, chatImports: forbidden }
    if (id.includes('validation')) return { validateImport: () => ({ status: 'ok' }) }
    if (id.includes('taskRouting')) return forbidden
    if (id.includes('chatImportPolling')) return { ...polling, hasTransitionalImports: realPoller ? polling.hasTransitionalImports : () => true, createChatImportPoller: options => {
      pollerOptions = options
      if (realPoller) return polling.createChatImportPoller({ ...options, timers: {
        set(callback, delayMs) { const id = ++timerId; timers.set(id, { callback, delayMs }); return id },
        clear: id => timers.delete(id),
      } })
      return { start() {}, stop() {}, refresh() {}, dispose() {} }
    } }
    throw Error(id)
  }, exports)
  const scope = Vue.effectScope()
  const currentConversation = Vue.ref(conversationId)
  const ui = scope.run(() => exports.useChatImports({ conversationId: currentConversation, ensure: async () => 'c1', refreshMessages: async () => true, notify: text => notices.push(text) }))
  return { ui, calls, notices, timers, currentConversation, poller: pollerOptions,
    close() { cleanup.forEach(callback => callback()); scope.stop() } }
}

// Load the real API/transport/client modules so both ApiError and ProductFailure
// follow the same route into the composable as they do in the desktop app.
function desktopApi() {
  const src = fileURLToPath(new URL('../src/', import.meta.url)), cache = new Map()
  function load(name) {
    let file = resolve(src, name)
    if (!extname(file)) file += '.ts'
    if (file.endsWith('.json')) return JSON.parse(readFileSync(file, 'utf8'))
    if (cache.has(file)) return cache.get(file).exports
    const module = { exports: {} }; cache.set(file, module)
    const native = createRequire(file)
    const compiled = ts.transpileModule(readFileSync(file, 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true,
    } }).outputText
    new Function('require', 'module', 'exports', compiled)(name => name.startsWith('.') ? load(resolve(dirname(file), name))
      : name.startsWith('@/') ? load(name.slice(2)) : native(name), module, module.exports)
    return module.exports
  }
  const scope = load('shared/productScope.ts')
  scope.enableDesktopProduct(); scope.setProductScope('test-workspace')
  return { api: load('services/api.ts'), transport: load('services/transport.ts'), load }
}

const flush = () => new Promise(resolve => setImmediate(resolve))
const historicalListing = { items: [{ id: 'old', state: 'queued', files: [{ id: 'f1', state: 'reading' }] }],
  selection: { refs: [{ materialId: 'm1', version: 1 }], localOnly: false }, service: null }

test('nonempty desktop conversation stops on a real API capacity error and manual refresh restores history', async () => {
  const f = desktopApi()
  let reads = 0
  const removeTransport = f.transport.installProductTransport(async path => {
    assert.equal(path, '/api/mindos/conversations/c1/imports')
    reads++
    return reads === 1
      ? Response.json({ detail: { code: 'WORKSPACE_OBJECT_LIMIT', message: 'Synthetic capacity failure' } }, { status: 503 })
      : Response.json(historicalListing)
  })
  const h = imports(true, { conversationId: 'c1', apiModule: f.api, realPoller: true })
  try {
    await flush()
    assert.equal(reads, 1, 'desktop must still read historical batches initially')
    assert.equal(h.ui.loadError.value, 'Synthetic capacity failure')
    assert.equal(h.timers.size, 0, 'capacity failure must not schedule any automatic retry')
    await h.ui.refresh()
    assert.equal(reads, 2)
    assert.equal(h.ui.loadError.value, '')
    assert.equal(h.ui.batches.value[0].id, 'old')
    assert.deepEqual(h.ui.references.value, [])
    assert.equal(h.timers.size, 0, 'desktop history never starts transitional import polling')
  } finally { h.close(); removeTransport() }
})

test('real desktop ProductFailure capacity errors stop even when status defaults to 503', async () => {
  for (const failure of [
    { code: 'WORKSPACE_STORAGE_FULL', remoteCode: 'WORKSPACE_QUOTA_EXCEEDED' },
    { code: 'BOX_BUSY', remoteCode: 'WORKSPACE_OPERATION_CAPACITY' },
    { code: 'TRANSPORT_UNAVAILABLE', remoteCode: 'WORKSPACE_OBJECT_LIMIT' },
  ]) {
    const f = desktopApi()
    let starts = 0
    const client = f.load('desktop/productClient.ts').createDesktopProductClient({ start: async () => {
      starts++
      return { ok: false, generation: 1, error: { ...failure, message: 'Synthetic desktop capacity failure' } }
    } }, () => ({ generation: 1, workspaceId: 'test-workspace' }))
    const removeTransport = f.transport.installProductTransport(client.request)
    const h = imports(true, { conversationId: 'c1', apiModule: f.api, realPoller: true })
    try {
      await flush()
      assert.equal(starts, 1)
      assert.equal(h.ui.loadError.value, 'Synthetic desktop capacity failure')
      assert.equal(h.timers.size, 0, JSON.stringify(failure))
    } finally { h.close(); removeTransport(); client.dispose() }
  }
})

test('desktop unknown failures have a finite budget and switching conversations clears it', async () => {
  const f = desktopApi()
  const reads = []
  const removeTransport = f.transport.installProductTransport(async path => { reads.push(path); throw new TypeError('Failed to fetch') })
  const h = imports(true, { conversationId: 'c1', apiModule: f.api, realPoller: true })
  try {
    await flush()
    for (const delay of [2500, 5000, 10000]) {
      const [id, timer] = h.timers.entries().next().value
      assert.equal(timer.delayMs, delay)
      h.timers.delete(id); timer.callback(); await flush()
    }
    assert.equal(reads.length, 4)
    assert.equal(h.timers.size, 0)
    h.currentConversation.value = 'c2'
    await Vue.nextTick(); await flush()
    assert.equal(reads.at(-1), '/api/mindos/conversations/c2/imports')
    assert.equal([...h.timers.values()][0].delayMs, 2500)
  } finally { h.close(); removeTransport() }
  assert.equal(h.timers.size, 0, 'unmount cancels scheduled retries')
})

test('desktop blocks all retired import actions before any network request', async () => {
  const h = imports(true)
  h.ui.stageFiles([{ name: 'sample.txt', size: 1 }])
  h.ui.stageMaterial({ materialId: 'm1', fileName: 'sample.txt' })
  await h.ui.openPicker()
  assert.equal(await h.ui.send('hello'), false)
  await h.ui.retry({ id: 'b1' })
  await h.ui.reupload({}, {}, {})
  await h.ui.chooseReferences([{ materialId: 'm1', version: 1 }])
  await h.ui.showPreview({ materialId: 'm1', version: 1 })
  await h.ui.showConsent()
  await h.ui.consent(false)
  await h.ui.confirmSensitive({})
  assert.deepEqual(h.calls, [])
  assert.deepEqual(h.ui.staged.value, [])
  assert.ok(h.notices.every(text => text.includes('Data Engine')))
  h.close()
})

test('workspace listing disables transitional polling and old attachment selection; legacy stays usable', () => {
  const data = { items: [{ id: 'old', state: 'processing', files: [] }], selection: { refs: [{ materialId: 'm1', version: 1 }], localOnly: false }, service: null }
  const h = imports(false)
  h.poller.apply(data)
  assert.equal(h.ui.retrievalOnly.value, false)
  assert.equal(h.ui.references.value.length, 1)
  assert.equal(h.poller.isTransitional(data), true)
  h.poller.apply({ ...data, retrievalOnly: true, uploadEnabled: false })
  assert.equal(h.ui.retrievalOnly.value, true)
  assert.deepEqual(h.ui.references.value, [])
  assert.equal(h.poller.isTransitional(data), false)
  assert.equal(h.ui.batches.value.length, 1, 'history is preserved')
  h.close()
})

test('restoring material management does not restore the legacy chat attachment bypass', async () => {
  const composer = await read('../src/components/conversation/Composer.vue')
  assert.match(composer, /v-if="!retrievalOnly" ref="filesInput"/)
  assert.match(composer, /v-if="voiceAvailable"/, 'box ASR is not a material import')
})
