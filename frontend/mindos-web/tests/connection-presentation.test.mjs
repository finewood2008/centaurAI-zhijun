import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import { connectionAttemptKey, connectionPresentation } from '../src/desktop/connectionPresentation.ts'

const capabilities = product => ({ materialsRead: false, streamChat: false, product, uploads: false, matters: false, provisioning: false })
const snapshot = (phase, options = {}) => ({
  protocolVersion: 1,
  environment: options.environment ?? 'production',
  phase,
  generation: options.generation ?? 7,
  sequence: options.sequence ?? 1,
  subject: options.subject ?? { accountId: 'account-private-id', deviceId: 'box-18', deviceName: '家里的盒子', selectedPath: options.path },
  capabilities: capabilities(options.product ?? false),
  ...(options.error ? { error: options.error } : {}),
})

test('three stages follow authoritative phases without time-based completion or invented paths', () => {
  const connecting = connectionPresentation(snapshot('connecting', { path: 'DIRECT' }), 99, true)
  assert.deepEqual(connecting.stages.map(item => item.state), ['active', 'pending', 'pending'])
  assert.equal(connecting.pathLabel, null, 'connecting has not established a selected path yet')
  assert.doesNotMatch(connecting.detail, /直连通道已建立|安全中继通道已建立/)
  assert.match(connecting.detail, /优先尝试直连，必要时使用加密中继/)
  assert.match(connecting.securityNotice, /将通过加密通道通信.*资料外发仍需单独授权/)
  assert.equal(connecting.deviceLabel, '家里的盒子')
  assert.doesNotMatch(connecting.title + connecting.detail, /account-private-id/)

  const direct = connectionPresentation(snapshot('authorizing', { path: 'DIRECT' }), 2, true)
  assert.deepEqual(direct.stages.map(item => item.state), ['complete', 'active', 'pending'])
  assert.equal(direct.pathLabel, '直连通道')
  const unknown = connectionPresentation(snapshot('authorizing'), 2, true)
  assert.equal(unknown.pathLabel, null)
  assert.match(unknown.detail, /连接通道已建立/)
  assert.doesNotMatch(unknown.detail, /直连/)

  const relay = connectionPresentation(snapshot('ready', { path: 'RELAY', product: true }), 2, false)
  assert.deepEqual(relay.stages.map(item => item.state), ['complete', 'complete', 'active'])
  assert.equal(relay.pathLabel, '安全中继通道')
  assert.match(relay.title, /正在打开工作区/)
})

test('ready without product and failed snapshots never claim current secure availability', () => {
  const unavailable = connectionPresentation(snapshot('ready', { path: 'DIRECT', product: false }), 30, false)
  assert.equal(unavailable.title, '工作区暂不可用')
  assert.deepEqual(unavailable.stages.map(item => item.state), ['complete', 'blocked', 'blocked'])
  assert.doesNotMatch(unavailable.detail, /权限已确认|正在打开/)
  assert.equal(unavailable.longWaitMessage, null)
  assert.equal(unavailable.elapsedLabel, null)

  const failed = connectionPresentation(snapshot('failed', {
    error: { code: 'TRANSPORT_UNAVAILABLE', message: '盒子暂时无法连接', recovery: 'user_reconnect' },
  }), 40, true)
  assert.equal(failed.detail, '盒子暂时无法连接')
  assert.equal(failed.stages.some(item => item.state === 'complete'), false)
  assert.equal(failed.elapsedLabel, null)
  assert.equal(failed.retryMessage, null)
})

test('elapsed time only changes waiting guidance and never fabricates percent or completion', () => {
  const early = connectionPresentation(snapshot('connecting'), 7, true)
  assert.equal(early.longWaitMessage, null)
  assert.equal(early.retryMessage, null)
  const slow = connectionPresentation(snapshot('connecting'), 8, true)
  assert.match(slow.longWaitMessage, /时间比平时稍长/)
  assert.equal(slow.retryMessage, null)
  const cancellable = connectionPresentation(snapshot('authorizing'), 20, true)
  assert.equal(cancellable.retryMessage, '你可以取消，稍后重新连接。')
  assert.equal(connectionPresentation(snapshot('authorizing'), 20, false).retryMessage, null)
  assert.equal(cancellable.elapsedLabel, '已等待 20 秒')
  assert.doesNotMatch(JSON.stringify(cancellable), /%|预计|剩余|正在中继/)
  assert.equal(cancellable.announcement.includes('20'), false, 'aria announcement is stable across timer ticks')
})

test('simulation is explicit and completed-looking steps are downgraded', () => {
  const view = connectionPresentation(snapshot('ready', { environment: 'simulation', path: 'DIRECT', product: true }), 1)
  assert.match(view.simulationNotice, /不代表已经建立真实安全连接/)
  assert.match(view.securityNotice, /当前模拟流程不证明真实连接.*资料外发也未获授权/)
  assert.equal(view.pathLabel, '模拟路径：直连通道')
  assert.deepEqual(view.stages.map(item => item.state), ['simulated', 'simulated', 'active'])
  assert.match(view.announcement, /^模拟环境/)
})

test('attempt identity resets on generation, account or device, not on sequence and phase', () => {
  const base = snapshot('connecting', { generation: 4, sequence: 1 })
  assert.equal(connectionAttemptKey(base), connectionAttemptKey({ ...base, phase: 'authorizing', sequence: 9 }))
  assert.notEqual(connectionAttemptKey(base), connectionAttemptKey({ ...base, generation: 5 }))
  assert.notEqual(connectionAttemptKey(base), connectionAttemptKey({ ...base, subject: { ...base.subject, accountId: 'other' } }))
  assert.notEqual(connectionAttemptKey(base), connectionAttemptKey({ ...base, subject: { ...base.subject, deviceId: 'other' } }))
})

async function componentFixture(initial) {
  const source = await readFile(new URL('../src/desktop/SecureConnectionProgress.vue', import.meta.url), 'utf8')
  const descriptor = parse(source, { filename: 'SecureConnectionProgress.vue' }).descriptor
  const code = ts.transpileModule(compileScript(descriptor, { id: 'secure-connection-progress-test' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const callbacks = []
  const emitted = []
  const module = { exports: {} }
  const icon = {}
  new Function('require', 'module', 'exports', code)(name => {
    if (name === 'vue') return { ...Vue, onBeforeUnmount: callback => callbacks.push(callback) }
    if (name === 'lucide-vue-next') return new Proxy({}, { get: () => icon })
    if (name === './connectionPresentation') return { connectionAttemptKey, connectionPresentation }
    throw new Error(`unexpected import: ${name}`)
  }, module, module.exports)
  const props = Vue.reactive({ snapshot: initial, canCancel: true })
  const scope = Vue.effectScope()
  const ui = scope.run(() => module.exports.default.setup(props, { emit: (...args) => emitted.push(args), expose() {} }))
  await Vue.nextTick()
  return { source, props, ui, callbacks, emitted, close() { callbacks.forEach(callback => callback()); scope.stop() } }
}

test('component timer uses monotonic time, resets for a new identity, and stops on failure/unmount', async () => {
  const previousPerformance = globalThis.performance
  const previousSetInterval = globalThis.setInterval
  const previousClearInterval = globalThis.clearInterval
  let now = 0
  let nextTimer = 0
  const intervalCallbacks = new Map()
  const cleared = []
  Object.defineProperty(globalThis, 'performance', { configurable: true, value: { now: () => now } })
  globalThis.setInterval = callback => { const id = ++nextTimer; intervalCallbacks.set(id, callback); return id }
  globalThis.clearInterval = id => { cleared.push(id); intervalCallbacks.delete(id) }
  try {
    const first = snapshot('connecting', { generation: 10 })
    const f = await componentFixture(first)
    assert.equal(f.ui.elapsedSeconds.value, 0)
    now = 8_400
    intervalCallbacks.get(1)()
    assert.equal(f.ui.elapsedSeconds.value, 8)

    f.props.snapshot = { ...first, phase: 'authorizing', sequence: 2, subject: { ...first.subject, selectedPath: 'DIRECT' } }
    await Vue.nextTick()
    assert.equal(f.ui.elapsedSeconds.value, 8, 'same connection identity keeps elapsed duration across phases')

    now = 9_000
    f.props.snapshot = snapshot('authorizing', { generation: 11, path: 'DIRECT' })
    await Vue.nextTick()
    assert.equal(f.ui.elapsedSeconds.value, 0, 'new generation starts a fresh timer')

    now = 12_500
    intervalCallbacks.get(1)()
    assert.equal(f.ui.elapsedSeconds.value, 3)
    f.props.snapshot = snapshot('failed', { generation: 11 })
    await Vue.nextTick()
    assert.deepEqual(cleared, [1])
    assert.equal(intervalCallbacks.size, 0)

    now = 20_000
    f.props.snapshot = snapshot('connecting', { generation: 11 })
    await Vue.nextTick()
    assert.equal(f.ui.elapsedSeconds.value, 0, 'recovery never inherits a failed attempt timer')
    assert.equal(intervalCallbacks.size, 1)
    f.close()
    assert.deepEqual(cleared, [1, 2])
    assert.match(f.source, /aria-live="polite"/)
    assert.match(f.source, /data-testid="connection-elapsed"[^>]*aria-hidden="true"/)
    assert.match(f.source, /prefers-reduced-motion/)
  } finally {
    Object.defineProperty(globalThis, 'performance', { configurable: true, value: previousPerformance })
    globalThis.setInterval = previousSetInterval
    globalThis.clearInterval = previousClearInterval
  }
})
