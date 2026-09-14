import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import * as Vue from 'vue'

const read = path => readFile(new URL(path, import.meta.url), 'utf8')
const code = ts.transpileModule(await read('../src/composables/useChatImports.ts'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
function imports(desktop) {
  let pollerOptions
  const notices = [], calls = [], exports = {}
  const forbidden = new Proxy({}, { get: (_, name) => () => { calls.push(name); throw Error('Unexpected material request') } })
  new Function('require', 'exports', code)(id => {
    if (id === 'vue') return { ...Vue, onBeforeUnmount() {} }
    if (id.includes('productScope')) return { isDesktopProduct: () => desktop }
    if (id.includes('services/api')) return { api: forbidden, chatImports: forbidden }
    if (id.includes('validation')) return { validateImport: () => ({ status: 'ok' }) }
    if (id.includes('taskRouting')) return forbidden
    if (id.includes('chatImportPolling')) return { hasTransitionalImports: () => true, createChatImportPoller: options => {
      pollerOptions = options
      return { start() {}, stop() {}, refresh() {}, dispose() {} }
    } }
    throw Error(id)
  }, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.useChatImports({ conversationId: Vue.ref(null), ensure: async () => 'c1', refreshMessages: async () => true, notify: text => notices.push(text) }))
  return { ui, calls, notices, poller: pollerOptions, close: () => scope.stop() }
}

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
