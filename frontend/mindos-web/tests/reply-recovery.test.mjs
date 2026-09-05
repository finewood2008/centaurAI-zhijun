// Real routing and SFC setup, with all transport and storage replaced by synthetic in-memory doubles.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import * as Vue from 'vue'
import ts from 'typescript'
import { parse, compileScript } from '@vue/compiler-sfc'
import * as recovery from '../src/composables/useReplyRecovery.ts'
import * as replies from '../src/shared/replyAssistance.ts'

const transpile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const error = code => Object.assign(new Error(code), { code, status: 409 })
const tick = async () => { await Vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }
const originalFetch = globalThis.fetch
const routing = {}
new Function('require', 'exports', transpile(await readFile(new URL('../src/services/taskRouting.ts', import.meta.url), 'utf8')))(id => {
  if (id === 'vue') return Vue
  if (id.includes('useReplyRecovery')) return recovery
  if (id === './api') return { buildHeaders: () => ({}), throwApiError: async response => { throw error((await response.json()).code) } }
  throw new Error('Unmocked routing import ' + id)
}, routing)
const preview = (revision, missing = []) => ({ revision, service: { id: 'synthetic-' + revision, external: true }, missing, sources: [], blocked: [] })
function transport(handle) {
  const calls = []
  globalThis.fetch = async (path, init) => {
    const body = JSON.parse(init.body || '{}')
    calls.push({ path, body, signal: init.signal })
    const result = await handle(path, body, calls.length)
    return { ok: !result.code, json: async () => result }
  }
  return calls
}
const origin = { messageId: 'assistant-1', selections: [{ batchId: 'old-batch', candidateId: 'old-option' }] }
try {
  {
    let reads = 0, grants = 0
    const questions = []
    const stop = Vue.watch(routing.routeQuestion, q => {
      if (q) { questions.push(q.preview.revision); q.done({ action: 'allow', keys: q.preview.missing }) }
    }, { flush: 'sync' })
    const calls = transport((path, body) => {
      if (path.endsWith('/preview')) return preview('r' + (++reads), reads < 3 ? ['scope-' + reads] : [])
      assert.ok(path.endsWith('/grant'))
      assert.deepEqual(body.keys, ['scope-' + (grants + 1)])
      return ++grants === 1 ? { code: 'PREVIEW_EXPIRED' } : { ok: true }
    })
    const body = await routing.prepareChatRoute('synthetic-chat', { content: '原话', requestId: 'same-nonce' })
    assert.equal(body.routeRevision, 'r3'); assert.equal(body.content, '原话'); assert.equal(body.requestId, 'same-nonce')
    assert.deepEqual(questions, ['r1', 'r2'], 'new service/scope is authorized again, not inherited from a stale grant')
    assert.ok(calls.filter(c => c.path.endsWith('/preview')).every(c => c.body.requestId === 'same-nonce'))
    stop()
  }
  {
    let reads = 0, executions = 0
    const calls = transport((_path, body) => body.previewOnly ? { routePreview: preview('p' + (++reads)) }
      : ++executions === 1 ? { code: 'ROUTE_CHANGED' } : { batch: { id: 'fresh-batch' } })
    const result = await routing.routedTask('synthetic-chat', '/synthetic-task', { requestId: 'task-nonce' })
    assert.equal(result.batch.id, 'fresh-batch'); assert.equal(reads, 2); assert.equal(executions, 2)
    assert.ok(calls.every(c => c.body.requestId === 'task-nonce'))
    assert.deepEqual(calls.filter(c => !c.body.previewOnly).map(c => c.body.routeRevision), ['p1', 'p2'])
  }
  {
    const calls = transport(() => ({ code: 'ROUTE_CHANGED' }))
    await assert.rejects(routing.prepareChatRoute('synthetic-chat', { content: 'still unchanged' }), { code: 'ROUTE_CHANGED' })
    assert.equal(calls.length, 2, 'a constantly changing route is retried only once')
  }
  for (const code of ['SOURCE_CHANGED', 'SOURCE_UNAVAILABLE', 'SOURCE_LIMIT']) {
    const calls = transport(() => ({ code }))
    await assert.rejects(routing.prepareChatRoute('synthetic-chat', { content: 'edited choice', replyAssistance: origin }), { code })
    assert.equal(calls.length, 1, 'real source changes are not automatically retried or stripped')
    assert.equal(calls[0].body.replyAssistance.selections[0].batchId, 'old-batch')
    assert.ok(recovery.replyNeedsRecovery('synthetic-chat', origin))
    assert.equal(recovery.replyNeedsRecovery('other-chat', origin), undefined)
    assert.equal(routing.canRefreshRoute(error(code)), false)
    assert.equal(routing.canRefreshRoute(error('ONLINE_SERVICE_CHANGED')), false)
  }
  {
    const abort = new AbortController(); abort.abort()
    const calls = transport(() => { throw new Error('must not start') })
    await assert.rejects(routing.prepareChatRoute('synthetic-chat', {}, abort.signal), { name: 'AbortError' })
    assert.equal(calls.length, 0)
    const next = new AbortController()
    const awaiting = transport(() => preview('consent', ['needed']))
    const stop = Vue.watch(routing.routeQuestion, q => { if (q) next.abort() }, { flush: 'sync' })
    assert.equal(await routing.prepareChatRoute('synthetic-chat', {}, next.signal), null)
    assert.equal(awaiting.length, 1); assert.equal(awaiting[0].signal, next.signal)
    assert.equal(routing.routeQuestion.value, null, 'abort closes the pending consent, no grant follows')
    stop()
  }
} finally { globalThis.fetch = originalFetch }

async function setup(name, props, mocks) {
  const source = await readFile(new URL('../src/components/conversation/' + name + '.vue', import.meta.url), 'utf8')
  const exports = {}, cleanup = [], exposed = {}, emitted = [], scope = Vue.effectScope()
  const code = transpile(compileScript(parse(source).descriptor, { id: 'reply-recovery-' + name }).content)
  new Function('require', 'exports', code)(id => {
    if (id === 'vue') return { ...Vue, onBeforeUnmount: fn => cleanup.push(fn) }
    if (id.includes('useReplyRecovery')) return recovery
    if (id.includes('shared/replyAssistance')) return replies
    if (id in mocks) return mocks[id]
    if (id.endsWith('.vue') || id === 'lucide-vue-next') return {}
    throw new Error('Unmocked component import: ' + id)
  }, exports)
  const reactiveProps = Vue.reactive(props)
  const ui = scope.run(() => exports.default.setup(reactiveProps, { expose: obj => Object.assign(exposed, obj), emit: (...args) => emitted.push(args) }))
  await tick()
  return { ui, exposed, emitted, props: reactiveProps, close() { cleanup.forEach(fn => fn()); scope.stop() } }
}
{
  const calls = [], old = { id: 'old-batch', messageId: 'assistant-1', candidates: [{ id: 'old-option', text: '原先的回答' }], model: 'synthetic', external: false, excluded: [] }
  const h = await setup('ReplyAssistance', { conversationId: 'synthetic-chat', messageId: 'assistant-1', disabled: false }, {
    '@/services/taskRouting': { routingRequest: async () => ({ batch: old }), routedTask: async (_cid, _path, body) => {
      calls.push(body)
      if (body.previousBatchId) throw error('SOURCE_CHANGED')
      return { batch: { ...old, id: 'new-batch' } }
    } },
  })
  assert.equal(h.ui.batch.value.id, 'old-batch')
  await h.ui.generate(true)
  assert.equal(h.ui.batch.value, null); assert.equal(h.ui.pendingRequest, null)
  assert.equal(h.ui.stale.value, true)
  await h.ui.generate(false, false, false)
  assert.equal(h.ui.batch.value.id, 'new-batch')
  assert.equal(calls[1].previousBatchId, undefined, 'invalid optional previous batch is not kept in a retry loop')
  assert.notEqual(calls[0].requestId, calls[1].requestId)
  recovery.reportReplyFailure('synthetic-chat', { ...origin, selections: [{ batchId: 'new-batch', candidateId: 'new-option' }] }, error('REPLY_SOURCE_CHANGED'))
  await tick(); assert.equal(h.ui.batch.value, null, 'send-time invalidation clears visible candidates')
  assert.equal(h.emitted.length, 0, 'recovery never overwrites or sends composer input')
  h.close()
}
{
  const storage = new Map(), previousSession = globalThis.sessionStorage, previousLocal = globalThis.localStorage
  const fakeStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  globalThis.sessionStorage = fakeStorage; globalThis.localStorage = fakeStorage
  const mocks = {
    '@/features/import/validation': { DOC_EXTENSIONS: [], IMAGE_EXTENSIONS: [], AUDIO_EXTENSIONS: [] },
    '@/composables/useToast': { useToast: () => () => {} }, '@/shared/decisionDraft': { intentHint: () => false },
    '@/shared/speech': { speechSupported: () => false },
  }
  const props = { conversationId: 'synthetic-composer', streaming: false, disabled: false }
  let h
  try {
    h = await setup('Composer', props, mocks)
    h.ui.text.value = '我自己写的开头'
    h.ui.insertReply('旧的辅助回答', origin)
    const originalText = h.ui.text.value
    h.ui.send(); await tick()
    assert.equal(h.ui.text.value, '')
    recovery.reportReplyFailure(props.conversationId, origin, error('SOURCE_CHANGED'))
    h.exposed.restoreSubmission(originalText, origin, props.conversationId); await tick()
    assert.equal(h.ui.text.value, originalText); assert.ok(h.ui.undo.value, 'failed sending restores the exact undo record')
    const fresh = { ...origin, selections: [{ batchId: 'fresh-batch', candidateId: 'fresh-option' }] }
    h.ui.insertReply('新回答', fresh)
    assert.equal(h.ui.text.value, originalText, 'new suggestions never silently merge invalid old provenance')
    assert.deepEqual(h.ui.expression.value.selections, origin.selections)
    h.close(); h = await setup('Composer', props, mocks)
    assert.equal(h.ui.text.value, originalText); assert.ok(h.ui.undo.value, 'refresh keeps the explicit safe undo available')
    h.ui.undoInsertion(); await tick()
    assert.equal(h.ui.text.value, '我自己写的开头'); assert.equal(h.ui.expression.value, undefined)
    h.ui.insertReply('新回答', fresh)
    assert.deepEqual(h.ui.expression.value.selections, fresh.selections, 'only removing the exact old insertion clears its lineage')
    recovery.reportReplyFailure(props.conversationId, fresh, error('SOURCE_CHANGED')); await tick()
    h.ui.text.value = h.ui.text.value.replace('新回答', '我改过的辅助回答')
    h.ui.undoInsertion()
    assert.ok(h.ui.text.value.includes('我改过的辅助回答'))
    assert.deepEqual(h.ui.expression.value.selections, fresh.selections, 'edited derived text keeps its original source restrictions')
    h.close()
    // Existing pre-update session drafts (without the new optional undo field) survive an HMR remount.
    const legacy = { text: '这是合成测试中已经写好的回答\n另一句已经选好的回答', origin }
    storage.set('zhijun.reply-input.legacy-chat', JSON.stringify(legacy))
    h = await setup('Composer', { ...props, conversationId: 'legacy-chat' }, mocks)
    assert.equal(h.ui.text.value, legacy.text); assert.deepEqual(h.ui.expression.value, legacy.origin)
    h.ui.send(); await tick()
    h.ui.text.value = '等待时继续写的新内容'
    h.exposed.restoreSubmission(legacy.text, origin, 'legacy-chat'); await tick()
    assert.ok(h.ui.text.value.startsWith('等待时继续写的新内容\n'))
    assert.ok(h.ui.text.value.endsWith(legacy.text)); assert.equal(JSON.stringify(h.ui.expression.value), JSON.stringify(origin))
    h.props.conversationId = 'other-chat'; await tick()
    h.ui.text.value = '另一段对话的当前输入'; await tick()
    h.exposed.restoreSubmission('上一段独立的原文', undefined, 'legacy-chat'); await tick()
    assert.equal(h.ui.text.value, '另一段对话的当前输入', 'an old request never writes into the new conversation')
    assert.ok(JSON.parse(storage.get('zhijun.reply-input.legacy-chat')).text.endsWith('上一段独立的原文'))
    const anotherOrigin = { messageId: 'other-question', selections: [{ batchId: 'another-batch', candidateId: 'other-option' }] }
    h.ui.expression.value = anotherOrigin
    h.exposed.restoreSubmission('不能与当前问题混合的旧辅助回答', origin, 'other-chat'); await tick()
    assert.equal(h.ui.text.value, '另一段对话的当前输入')
    assert.deepEqual(h.ui.expression.value, anotherOrigin)
    assert.equal(h.ui.failedDrafts.value.length, 1)
    h.close(); h = await setup('Composer', { ...props, conversationId: 'other-chat' }, mocks)
    assert.equal(h.ui.failedDrafts.value.length, 1, 'conflicting failure drafts persist independently across remount')
    h.ui.switchFailedDraft(); await tick()
    assert.equal(h.ui.text.value, '不能与当前问题混合的旧辅助回答'); assert.deepEqual(h.ui.expression.value, origin)
    assert.equal(h.ui.failedDrafts.value[0].text, '另一段对话的当前输入', 'explicit switching retains both originals and their separate origins')
  } finally { h?.close(); globalThis.sessionStorage = previousSession; globalThis.localStorage = previousLocal }
}
console.log('reply recovery: bounded re-preview/same nonce/new grants; real source blocks; abort; stale candidate cache; exact undo after failure/refresh; edited provenance preserved passed')
