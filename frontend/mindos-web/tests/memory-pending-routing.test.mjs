// Execute real Vue setup functions against synthetic APIs only; never a live request.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const sources = Object.fromEntries(await Promise.all(['MemoryPending', 'RoutingPanel'].map(async name => [name, await readFile(new URL(`../src/components/conversation/${name}.vue`, import.meta.url), 'utf8')])))
const compiled = Object.fromEntries(Object.entries(sources).map(([name, source]) => [name, ts.transpileModule(compileScript(parse(source).descriptor, { id: name }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText]))
const flush = async () => { await Vue.nextTick(); await new Promise(r => setImmediate(r)) }
const defer = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const copy = value => JSON.parse(JSON.stringify(value))
function mount(name, initialProps, api) {
  const cleanups = [], emits = [], exports = {}
  new Function('require', 'exports', 'setInterval', 'clearInterval', compiled[name])(
    id => id === 'vue' ? { ...Vue, onBeforeUnmount: fn => cleanups.push(fn) } : api,
    exports, () => 1, () => {},
  )
  const props = Vue.reactive(initialProps), scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup(props, { expose() {}, emit(...args) { emits.push(args) } }))
  return { props, ui, emits, close() { cleanups.forEach(fn => fn()); scope.stop() } }
}
function routing(overrides = {}) {
  const calls = [], states = {
    a: { mode: { mode: 'online', revision: 1, service: 'service' }, service: { id: 'service', name: '合成服务', external: true }, defaultAuthorization: { active: true, revision: 2, includeFiles: false }, pending: [{ task_key: 'extract_turn', preview_id: 'p', count: 8, reason: 'CONSENT_REQUIRED' }] },
    b: { mode: { mode: 'local', revision: 1 }, service: { id: 'service', external: true }, defaultAuthorization: { active: false, revision: 0 }, pending: [] },
  }
  const api = {
    routePath: id => `/routing/${id}`,
    routingRequest: async (path, method = 'GET', body) => {
      calls.push({ path, method, body })
      const cid = path.split('/')[2]
      if (path.endsWith('/default-consent')) return { ...copy(states[cid]), defaultAuthorization: { ...body, active: body.enabled, revision: 3 } }
      if (path.includes('/pending/')) return { missing: ['x'], revision: 'new-preview' }
      if (path.endsWith('/resume')) return { queuedCount: 8, pendingCount: 0 }
      if (path.endsWith('/grant')) return {}
      return copy(states[cid])
    },
    askRoute: async () => ({ action: 'allow', keys: ['x'] }),
    ...overrides,
  }
  return { ...mount('RoutingPanel', { conversationId: 'a', disabled: false }, api), api, calls, states }
}

// Old defaults do not silently expand to charter content; explicit selection is sent.
{
  const h = routing(); await flush()
  assert.equal(h.ui.attentionLabel.value, '个人理解暂停 · 8 轮')
  h.ui.editDefault(); assert.equal(h.ui.includeCharter.value, false)
  h.ui.consentAcknowledge.value = true; await h.ui.saveDefault(true)
  assert.equal(h.calls.find(c => c.method === 'PUT').body.includeCharter, false)
  h.ui.editDefault(); h.ui.includeCharter.value = true; h.ui.consentAcknowledge.value = true
  await h.ui.saveDefault(true)
  assert.equal(h.calls.filter(c => c.method === 'PUT').at(-1).body.includeCharter, true)
  assert.equal(h.ui.policy.value.includeCharter, true)
  h.ui.state.value.pending[0].count = undefined
  assert.equal(h.ui.attentionLabel.value, '个人理解暂停', 'legacy groups must not pretend to count missed rounds')
  h.close()
}

// Expired previews only reprepare work; this action cannot grant sources.
{
  const h = routing({ askRoute: async () => { throw Error('must not open consent') } }); await flush()
  await h.ui.pending({ task_key: 'extract_turn', previewExpired: true }, true)
  assert.equal(h.calls.filter(c => c.path.endsWith('/resume')).length, 1)
  assert.equal(h.calls.filter(c => c.path.endsWith('/grant') || c.path.includes('/pending/')).length, 0)
  assert.match(h.ui.notice.value, /没有增加授权/)
  h.close()
}

// A dialog started for A must not grant or resume on B after navigation.
{
  let signal
  const choice = defer(), h = routing({ askRoute: async (_preview, _allowOmit, abortSignal) => { signal = abortSignal; return choice.promise } }); await flush()
  const request = h.ui.pending({ task_key: 'extract_turn', preview_id: 'p' }); await flush()
  h.props.conversationId = 'b'; await flush()
  assert.equal(signal.aborted, true, 'navigation must also close the now irrelevant permission dialog')
  choice.resolve({ action: 'allow', keys: ['x'] }); await request
  assert.equal(h.calls.some(c => c.path.endsWith('/grant') || c.path.endsWith('/resume')), false)
  assert.equal(h.ui.state.value.mode.mode, 'local'); assert.equal(h.ui.notice.value, '')
  h.close()
}

// A late settings write cannot replace another conversation's current mode/policy.
{
  const h = routing(); await flush(); const reply = defer(), original = h.api.routingRequest
  h.api.routingRequest = (path, method, body) => method === 'PUT' ? reply.promise : original(path, method, body)
  const operation = h.ui.saveDefault(true)
  h.props.conversationId = 'b'; await flush()
  reply.resolve(h.states.a); await operation
  assert.equal(h.ui.state.value.mode.mode, 'local'); assert.equal(h.ui.configureDefault.value, false)
  h.close()
}

function pending(overrides = {}) {
  const claims = [{ topicId: 'old-topic', claim: { id: 'claim-a', content: '我重视充分准备', trustState: 'working' } }, { topicId: 'new-topic', claim: { id: 'claim-b', content: '我希望多留时间给家人', trustState: 'working' } }]
  const calls = []
  const api = {
    getConversationMemoryPending: async cid => { calls.push(['get', cid]); return { items: copy(claims), total: claims.length } },
    reviewClaim: async (id, body) => { calls.push(['review', id, body]); claims.splice(claims.findIndex(c => c.claim.id === id), 1) },
    dismissConversationMemoryPending: async (cid, id) => { calls.push(['dismiss', cid, id]); claims.splice(claims.findIndex(c => c.claim.id === id), 1) },
    ...overrides,
  }
  return { ...mount('MemoryPending', { conversationId: 'a', pendingCount: 2 }, api), api, calls }
}

// The optional queue reads all topics without reserving attention or generating messages.
{
  const h = pending(); await flush(); assert.equal(h.calls.length, 0)
  h.ui.show(); await flush(); assert.equal(h.ui.items.value.length, 2)
  assert.equal(h.ui.items.value[0].topicId, 'old-topic')
  await h.ui.review(h.ui.items.value[0].claim, 'confirm')
  assert.equal(h.ui.items.value.length, 1); assert.equal(h.ui.total.value, 1)
  await h.ui.review(h.ui.items.value[0].claim, 'dismiss')
  assert.equal(h.ui.total.value, 0)
  assert.deepEqual(h.calls.filter(c => c[0] === 'dismiss')[0], ['dismiss', 'a', 'claim-b'])
  assert.equal(h.emits.filter(e => e[0] === 'changed').length, 2)
  h.close()
}

// Read errors/review failures preserve candidates, allow retry, never overwrite after switching.
{
  const h = pending({ reviewClaim: async () => { throw Error('保存失败') } })
  await h.ui.load(); await h.ui.review(h.ui.items.value[0].claim, 'partial', '编辑后的正文')
  assert.equal(h.ui.items.value.length, 2); assert.match(h.ui.error.value, /保存失败/)
  const reply = defer(); h.api.getConversationMemoryPending = () => reply.promise
  const request = h.ui.load(); h.props.conversationId = 'b'; await flush()
  reply.resolve({ items: [{ topicId: 'a', claim: { id: 'old' } }], total: 1 }); await request
  assert.deepEqual(h.ui.items.value, []); assert.equal(h.ui.open.value, false)
  h.close()
}

assert.match(sources.RoutingPanel, /人生章程与章程草稿（含必要的历史版本）/)
assert.match(sources.RoutingPanel, /previewExpired/)
assert.doesNotMatch(sources.MemoryPending, /getConversationMemoryAttention|scrollToBottom|\.focus\(|createMessage/)
assert.match(sources.MemoryPending, /原对话仍保留/)
console.log('memory/routing: explicit charter consent, accurate paused rounds, all-topic optional queue, safe restoration and navigation races passed')
