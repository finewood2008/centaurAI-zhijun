import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { CHAT_UNAVAILABLE, conversationNotice } from '../src/shared/conversationPresentation.ts'

const code = ts.transpileModule(await readFile(new URL('../src/services/taskRouting.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const settle = () => new Promise(resolve => setImmediate(resolve))
function harness({ mode = 'online', available = true, previewLocal = false, missing = [], conflict = null, putError = null } = {}) {
  const calls = [], resets = [], exports = {}
  let state = { mode: { mode, service: 'service', revision: 3 }, service: { id: 'service', external: available } }
  new Function('require', 'exports', code)(id => {
    if (id === 'vue') return Vue
    if (id.includes('productScope')) return { isDesktopProduct: () => false, onProductScopeReset: fn => resets.push(fn) }
    if (id.includes('transport')) return { transportRequest: async (path, options) => {
      const body = options.body && JSON.parse(options.body)
      calls.push({ path, method: options.method, body })
      if (options.method === 'PUT') {
        if (putError) throw putError
        state = { ...state, mode: { ...state.mode, mode: body.mode, revision: 4 } }
      }
      if (path.endsWith('/grant')) missing = []
      const value = path.endsWith('/preview') ? { revision: 'preview', service: { external: !previewLocal },
        missing, sources: missing.map(key => ({ key, text: '合成原话' })), excluded: [], charterConflict: conflict } : state
      return { ok: true, json: async () => value }
    } }
    if (id === './api') return { buildHeaders: value => value.headers, throwApiError: async r => { throw r.error } }
    if (id.includes('useReplyRecovery')) return { reportReplyFailure() {} }
    throw Error(id)
  }, exports)
  return { ...exports, calls, reset: () => resets.forEach(fn => fn()) }
}
test('ready conversation uses the current service without writing model settings or expanding grants', async () => {
  const h = harness(), body = await h.prepareChatRoute('c', { content: '合成问题' }, undefined, true)
  assert.equal(body.localOnly, false)
  assert.equal(body.routeRevision, 'preview')
  assert.equal(h.calls.filter(c => c.method === 'PUT' || c.path.endsWith('/grant')).length, 0)
})
test('legacy cancel preserves mode and never previews or sends content', async () => {
  const h = harness({ mode: 'local' }), pending = h.prepareChatRoute('old', { content: '草稿' }, undefined, true)
  await settle(); assert.equal(h.conversationSetup.value.protectedHistory, true)
  h.conversationSetup.value.done(false)
  assert.equal(await pending, null)
  assert.equal(h.calls.length, 1)
})
test('legacy opt-in updates only this conversation with a fresh context, without source grants', async () => {
  const h = harness({ mode: 'legacy' }), pending = h.prepareChatRoute('old', { content: '草稿' }, undefined, true)
  await settle(); h.conversationSetup.value.done(true)
  assert.equal((await pending).localOnly, false)
  const writes = h.calls.filter(c => c.method === 'PUT')
  assert.equal(writes.length, 1)
  assert.equal(writes[0].path, '/api/mindos/conversations/old/routing')
  assert.deepEqual(writes[0].body, { mode: 'online', acknowledge: true, serviceId: 'service', expectedRevision: 3, freshContext: true })
  assert.ok(h.calls.every(c => !/default|grant|handling/.test(c.path)))
})
test('a protected prefill needs confirmation even in a ready conversation', async () => {
  const h = harness(), pending = h.prepareChatRoute('c', { content: '草稿', localOnly: true }, undefined, true)
  await settle(); assert.ok(h.conversationSetup.value); h.conversationSetup.value.done(true)
  assert.equal((await pending).localOnly, false)
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 0)
})
test('unavailable service and automatic local fallback never reach message dispatch', async () => {
  const unavailable = harness({ available: false })
  await assert.rejects(unavailable.prepareChatRoute('c', {}, undefined, true), /暂时无法回答/)
  assert.equal(unavailable.calls.length, 1)
  const fallback = harness({ previewLocal: true })
  await assert.rejects(fallback.prepareChatRoute('c', {}, undefined, true), /资料暂时无法用于回答/)
  assert.ok(fallback.calls.every(c => !c.path.endsWith('/messages')))
})
test('missing-source confirmation keeps the existing per-source grant and cancellation boundary', async () => {
  const h = harness({ missing: ['source-1'] }), pending = h.prepareChatRoute('c', {}, undefined, true)
  await settle(); h.routeQuestion.value.done({ action: 'allow', keys: ['source-1'] })
  assert.ok(await pending)
  assert.deepEqual(h.calls.find(c => c.path.endsWith('/grant')).body, { revision: 'preview', keys: ['source-1'] })
  const c = harness({ missing: ['source-1'] }), cancelled = c.prepareChatRoute('c', {}, undefined, true)
  await settle(); c.routeQuestion.value.done({ action: 'cancel' })
  assert.equal(await cancelled, null)
  assert.ok(c.calls.every(c => !c.path.endsWith('/grant')))
})
test('abort and device-scope reset dismiss pending confirmation without writes', async () => {
  for (const reset of [false, true]) {
    const h = harness({ mode: 'local' }), abort = new AbortController()
    const pending = h.prepareChatRoute('c', {}, abort.signal, true)
    await settle(); if (reset) h.reset(); else abort.abort()
    assert.equal(await pending, null)
    assert.equal(h.conversationSetup.value, null)
    assert.equal(h.calls.length, 1)
  }
})
test('revision conflict stops safely; non-conversation callers retain their existing routing behavior', async () => {
  const error = new Error('模式已变化'), h = harness({ mode: 'local', putError: error })
  const pending = h.prepareChatRoute('c', {}, undefined, true)
  await settle(); h.conversationSetup.value.done(true)
  await assert.rejects(pending, e => e === error)
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 1)
  const standard = harness({ previewLocal: true })
  assert.ok(await standard.prepareChatRoute('c', { localOnly: true }))
  assert.equal(standard.calls.length, 1)
})
test('conversation notices hide provider diagnostics but preserve actionable data errors', () => {
  for (const value of ['DeepSeek timeout', '模型未配置', 'https://private.invalid/v1 failed', 'Ollama unavailable']) assert.equal(conversationNotice(value), CHAT_UNAVAILABLE)
  assert.equal(conversationNotice('资料已被撤回，请重新选择'), '资料已被撤回，请重新选择')
})
