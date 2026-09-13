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
    a: { mode: { mode: 'online', revision: 1, service: 'service' }, service: { id: 'service', name: '合成服务', model: 'online-model', external: true }, localService: { id: 'local', name: '本机', model: 'qwen3.5:2b', external: false }, defaultAuthorization: { active: true, revision: 2, includeFiles: false }, pending: [{ task_key: 'extract_turn', preview_id: 'p', count: 8, reason: 'CONSENT_REQUIRED' }] },
    b: { mode: { mode: 'local', revision: 1 }, service: { id: 'service', model: 'online-model', external: true }, localService: { id: 'local', name: '本机', model: 'qwen3.5:2b', external: false }, defaultAuthorization: { active: false, revision: 0 }, pending: [] },
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
      if (method === 'PUT') {
        states[cid] = { ...states[cid], mode: { mode: body.mode, revision: states[cid].mode.revision + 1,
          ...(body.mode === 'online' ? { service: states[cid].service.id } : {}) } }
      }
      return copy(states[cid])
    },
    askRoute: async () => ({ action: 'allow', keys: ['x'] }),
    needsDeConsent: preview => preview.deConsentRequired === true,
    grantDefaultDeConsent: async () => false,
    ...overrides,
  }
  return { ...mount('RoutingPanel', { conversationId: 'a', disabled: false }, api), api, calls, states }
}

// Reading authoritative state must never look like an explicit user choice.
// A successful mode action (including re-selecting the active online pill) is
// the only signal that may override a pending system-prompt local route.
{
  const h = routing(); await flush()
  assert.equal(h.emits.filter(e => e[0] === 'mode-selected').length, 0)
  h.ui.chooseOnline()
  assert.deepEqual(h.emits.findLast(e => e[0] === 'mode-selected'), ['mode-selected', 'online'])
  h.ui.open.value = false
  await h.ui.change('local')
  assert.deepEqual(h.emits.findLast(e => e[0] === 'mode-selected'), ['mode-selected', 'local'])
  h.api.routingRequest = async () => { throw new Error('切换失败') }
  const selectedBeforeFailure = h.emits.filter(e => e[0] === 'mode-selected').length
  assert.equal(await h.ui.change('online'), false)
  assert.equal(h.emits.filter(e => e[0] === 'mode-selected').length, selectedBeforeFailure)
  h.close()
}

// Both choices stay visible. Local is one click; online still opens the
// acknowledgement drawer instead of silently sending content off-device.
{
  const h = routing(); await flush()
  assert.equal(h.ui.currentMode.value, 'online')
  assert.equal(h.ui.localModelLabel.value, 'qwen3.5:2b')
  assert.equal(await h.ui.useLocal(), true)
  const request = h.calls.findLast(c => c.method === 'PUT')
  assert.deepEqual(request.body, { mode: 'local', acknowledge: false, serviceId: 'service', expectedRevision: 1, freshContext: false })
  h.props.conversationId = 'b'; await flush()
  h.ui.chooseOnline()
  assert.equal(h.ui.open.value, true)
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 1, 'online requires the explicit drawer acknowledgement')
  h.close()
}

// The settings-page pause flow refreshes before trusting an apparently-local
// state and performs one bounded retry after a concurrent revision conflict.
{
  const h = routing(); await flush()
  h.ui.state.value = { ...h.ui.state.value, mode: { mode: 'local', revision: 1 } }
  h.states.a.mode = { mode: 'online', revision: 2, service: 'service' }
  assert.equal(await h.ui.ensureLocal(), true)
  assert.equal(h.calls.findLast(c => c.method === 'PUT').body.expectedRevision, 2)

  h.states.a.mode = { mode: 'online', revision: 4, service: 'service' }
  const original = h.api.routingRequest
  let conflicts = 0
  h.api.routingRequest = async (path, method = 'GET', body) => {
    if (method === 'PUT' && conflicts++ === 0) {
      h.states.a.mode = { mode: 'online', revision: 5, service: 'service' }
      throw new Error('模式已更新，请刷新')
    }
    return original(path, method, body)
  }
  assert.equal(await h.ui.ensureLocal(), true)
  assert.equal(conflicts, 2)
  assert.equal(h.calls.findLast(c => c.method === 'PUT').body.expectedRevision, 5)
  h.close()
}

// A persisted online choice cannot look healthy after the online channel is
// disabled; the user receives a direct local-model action.
{
  const h = routing(); await flush()
  h.ui.state.value = { ...h.ui.state.value, service: { id: 'local', name: '本机', model: 'qwen3.5:2b', external: false } }
  assert.equal(h.ui.onlineAvailable.value, false)
  assert.equal(h.ui.onlineModelLabel.value, '未启用')
  assert.equal(h.ui.attentionLabel.value, '在线模型不可用')
  h.close()
}

// A configured-but-paused provider can be activated from settings without
// silently changing the routing mode or bypassing the egress acknowledgement.
{
  const h = routing(); await flush()
  h.states.a.mode = { mode: 'local', revision: 2 }
  h.states.a.service = { id: 'local', name: '本机', model: 'qwen3.5:2b', external: false }
  await h.ui.refresh()
  let activations = 0
  h.props.activateOnlineChannel = async () => {
    activations += 1
    h.states.a.service = { id: 'deepseek', name: 'DeepSeek', model: 'deepseek-v4-flash', external: true }
    return h.ui.refresh()
  }
  h.ui.acknowledge.value = true
  await h.ui.enableOnlineChannel()
  assert.equal(activations, 1)
  assert.equal(h.ui.onlineAvailable.value, true)
  assert.equal(h.ui.currentMode.value, 'local', 'channel activation must not silently switch the conversation')
  assert.equal(h.ui.acknowledge.value, false, 'the user must confirm egress after the service identity becomes authoritative')
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 0)
  h.close()
}

// Failed activation is shown next to the action that failed; it must not be
// hidden at the bottom of the long authorization drawer.
{
  const h = routing(); await flush()
  h.states.a.mode = { mode: 'local', revision: 2 }
  h.states.a.service = { id: 'local', name: '本机', model: 'qwen3.5:2b', external: false }
  await h.ui.refresh()
  h.props.activateOnlineChannel = async () => { throw new Error('还没有已保存的在线供应商。请关闭此面板，在下方添加供应商、Token 和模型。') }
  await h.ui.enableOnlineChannel()
  assert.match(h.ui.error.value, /还没有已保存的在线供应商/)
  assert.equal(h.ui.onlineAvailable.value, false)
  h.close()
}
assert.match(sources.RoutingPanel, /data-testid="routing-online-activation-error"/)
assert.ok(
  sources.RoutingPanel.indexOf('data-testid="routing-online-activation-error"') < sources.RoutingPanel.indexOf('资料来源默认授权'),
  'activation failure must render before the remaining long-form authorization settings',
)

// A pending task covered by the explicit auto-egress policy gets an exact
// receipt before resume without opening another dialog.
{
  let grants = 0
  const h = routing({
    grantDefaultDeConsent: async (_id, preview) => { grants++; assert.equal(preview.defaultAuthorization.applies, true); return true },
    askRoute: async () => { throw Error('must not open consent') },
  }); await flush()
  h.api.routingRequest = async (path, method = 'GET', body) => {
    h.calls.push({ path, method, body })
    if (path.includes('/pending/')) return { missing: [], blocked: [], sources: [], deConsentRequired: true, revision: 'auto-preview', defaultAuthorization: { autoEgress: true, applies: true, revision: 3 } }
    if (path.endsWith('/resume')) return { queuedCount: 1, pendingCount: 0 }
    return copy(h.states.a)
  }
  await h.ui.pending({ task_key: 'extract_turn', preview_id: 'p' })
  assert.equal(grants, 1)
  assert.equal(h.calls.filter(c => c.path.endsWith('/resume')).length, 1)
  h.close()
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
