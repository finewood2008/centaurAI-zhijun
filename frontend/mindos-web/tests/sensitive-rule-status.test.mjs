import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as Vue from 'vue'
import ts from 'typescript'

const serviceSource = await readFile(new URL('../src/services/sensitiveRuleStatus.ts', import.meta.url), 'utf8')
const panelSource = await readFile(new URL('../src/components/settings/SensitiveRulesPanel.vue', import.meta.url), 'utf8')
const compile = source => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const serviceCode = compile(serviceSource)
const panelCode = compile(compileScript(parse(panelSource).descriptor, { id: 'rule-status-test' }).content)
const flush = async () => { await Vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code }
}
function service(transport = async () => ({ ok: true, json: async () => ({ state: 'active', applying: false }) })) {
  const exports = {}, timers = new Map(), calls = []
  let nextId = 0
  const dependencies = id => {
    if (id === './api') return { ApiError, buildHeaders: () => new Headers(), throwApiError: async response => { throw response.error } }
    if (id === './transport.ts') return { transportRequest: async (...args) => { calls.push(args); return transport(...args) } }
    throw Error('Unexpected dependency ' + id)
  }
  new Function('require', 'exports', 'setTimeout', 'clearTimeout', serviceCode)(dependencies, exports,
    (run, delay) => { const id = ++nextId; timers.set(id, { run, delay }); return id }, id => timers.delete(id))
  return { ...exports, timers, calls, tick() {
    const [id, timer] = timers.entries().next().value
    timers.delete(id); timer.run()
  } }
}

test('status service uses the facade without caching and rejects extra, malformed or contradictory state', async () => {
  const h = service(), controller = new AbortController()
  assert.deepEqual(await h.getSensitiveRuleStatus(controller.signal), { state: 'active', applying: false })
  assert.equal(h.calls[0][0], '/api/mindos/settings/sensitive-rules/status')
  assert.equal(h.calls[0][1].cache, 'no-store')
  assert.equal(h.calls[0][1].signal, controller.signal)
  for (const value of [{}, { state: 'active', applying: 0 }, { state: 'active', applying: true },
    { state: 'applying', applying: false }, { state: 'failed', applying: false },
    { state: 'active', applying: false, internalRevision: 4 }]) {
    await assert.rejects(service(async () => ({ ok: true, json: async () => value })).getSensitiveRuleStatus(),
      { code: 'INVALID_SENSITIVE_RULE_STATUS_RESPONSE' })
  }
  const upstreamError = Object.assign(new ApiError('not available', 503), { retryAfter: 120 })
  await assert.rejects(service(async () => ({ ok: false, error: upstreamError })).getSensitiveRuleStatus(), error => error === upstreamError)
})

test('polling starts only when enabled, uses a low frequency, and stops at active', async () => {
  const h = service(), updates = [], errors = []
  let reads = 0
  const p = h.createSensitiveRuleStatusPoller({ apply: value => updates.push(value), onError: e => errors.push(e),
    read: async () => ++reads === 1 ? { state: 'applying', applying: true } : { state: 'active', applying: false } })
  p.refresh(); await flush()
  assert.equal(reads, 0)
  p.setEnabled(true); await flush()
  assert.equal(reads, 1)
  assert.equal(h.timers.values().next().value.delay, 30_000)
  h.tick(); await flush()
  assert.equal(reads, 2)
  assert.equal(updates.at(-1).state, 'active')
  assert.equal(h.timers.size, 0)
  p.dispose()
})

test('hidden/unmounted scopes cancel requests and a save rechecks without overlapping an unfinished read', async () => {
  const h = service(), pending = [], signals = [], updates = []
  const p = h.createSensitiveRuleStatusPoller({ apply: value => updates.push(value), onError() {}, read: signal => {
    const d = deferred(); pending.push(d); signals.push(signal); return d.promise
  } })
  p.setEnabled(true); await flush()
  p.refresh(); p.refresh(); await flush()
  assert.equal(signals[0].aborted, true)
  assert.equal(pending.length, 1, 'an ignored abort never permits concurrent reads')
  pending[0].resolve({ state: 'active', applying: false }); await flush()
  assert.equal(pending.length, 2)
  assert.deepEqual(updates, [], 'the pre-save response is stale')
  p.setEnabled(false)
  assert.equal(signals[1].aborted, true)
  pending[1].resolve({ state: 'applying', applying: true }); await flush()
  assert.equal(h.timers.size, 0)
  assert.deepEqual(updates, [])
  p.setEnabled(true); await flush()
  p.dispose()
  assert.equal(signals[2].aborted, true)
  pending[2].resolve({ state: 'active', applying: false }); await flush()
  assert.deepEqual(updates, [])
})

test('optional errors preserve last state, respect Retry-After and stop on insufficient capability', async () => {
  const h = service(), updates = [], errors = []
  let reads = 0
  const p = h.createSensitiveRuleStatusPoller({ apply: v => updates.push(v), onError: e => errors.push(e), read: async () => {
    if (++reads === 1) return { state: 'applying', applying: true }
    if (reads === 2) throw Object.assign(new ApiError('temporary', 503), { retryAfter: 120 })
    throw new ApiError('denied', 403, 'CAPABILITY_DENIED')
  } })
  p.setEnabled(true); await flush(); h.tick(); await flush()
  assert.equal(updates.at(-1).state, 'applying')
  assert.equal(errors.at(-1).status, 503)
  assert.equal(h.timers.values().next().value.delay, 120_000)
  h.tick(); await flush()
  assert.equal(h.timers.size, 0)
  p.refresh(); p.setEnabled(false); p.setEnabled(true); await flush()
  assert.equal(reads, 3, 'capability denial stops status calls until a fresh component/account scope')
  p.dispose()
})

test('Retry-After beyond one day, including fractional seconds, retains its full delay across visibility changes', async () => {
  for (const retryAfter of [90_000, 90_000.125]) {
    const h = service()
    let reads = 0
    const p = h.createSensitiveRuleStatusPoller({ apply() {}, onError() {}, read: async () => {
      reads++
      throw Object.assign(new ApiError('temporary', 503), { retryAfter })
    } })
    p.setEnabled(true); await flush()
    assert.equal(h.timers.values().next().value.delay, retryAfter * 1000)
    p.setEnabled(false); p.setEnabled(true); await flush()
    assert.equal(reads, 1, 'showing the page cannot bypass a pending retry delay')
    assert.ok(h.timers.values().next().value.delay > 86_400_000)
    p.dispose()
  }
})

test('Retry-After beyond the timer limit stops automatic polling until an explicit refresh', async () => {
  for (const retryAfter of [2_147_483.648, Number.MAX_VALUE]) {
    const h = service()
    let reads = 0
    const p = h.createSensitiveRuleStatusPoller({ apply() {}, onError() {}, read: async () => {
      if (++reads === 1) throw Object.assign(new ApiError('temporary', 503), { retryAfter })
      return { state: 'active', applying: false }
    } })
    p.setEnabled(true); await flush()
    assert.equal(h.timers.size, 0, 'an oversized delay must never reach setTimeout')
    p.setEnabled(false); p.setEnabled(true); await flush()
    assert.equal(reads, 1, 'visibility changes do not resume paused automatic polling')
    p.refresh(); await flush()
    assert.equal(reads, 2, 'the existing manual refresh remains available')
    assert.equal(h.timers.size, 0)
    p.dispose()
  }
})

function panel({ statusRead, hidden = false } = {}) {
  const h = service(), mounted = [], cleanups = [], resets = [], listeners = new Map(), exports = {}, writes = []
  const document = { visibilityState: hidden ? 'hidden' : 'visible',
    addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }
  const rule = { ruleId: 'custom-project', source: 'custom', immutable: false, revision: 1,
    name: '项目代号', description: '识别尚未公开的内部项目代号', examples: ['虚构项目'], counterExamples: [],
    enabled: true, deliveryMode: 'confirm', allowOriginalAfterConfirm: false }
  const api = {
    getSensitiveRules: async () => ({ items: [rule], customCount: 1, maxCustomRules: 4, detectorPromptWithinLimit: true }),
    updateSensitiveRule: async (id, value) => { writes.push([id, value]); return { ...rule, ...value, revision: 2 } },
  }
  new Function('require', 'exports', 'document', panelCode)(id => {
    if (id === 'vue') return { ...Vue, onMounted: fn => mounted.push(fn), onUnmounted: fn => cleanups.push(fn) }
    if (id.endsWith('/api')) return { api, ApiError }
    if (id.endsWith('/productScope')) return { onProductScopeReset: fn => { resets.push(fn); return () => resets.splice(resets.indexOf(fn), 1) } }
    if (id.endsWith('/sensitiveRuleStatus')) return { createSensitiveRuleStatusPoller: options => h.createSensitiveRuleStatusPoller({ ...options, read: statusRead }) }
    throw Error('Unexpected dependency ' + id)
  }, exports, document)
  const scope = Vue.effectScope(), props = Vue.reactive({ enabled: true })
  const ui = scope.run(() => exports.default.setup(props, { expose() {} }))
  mounted.forEach(fn => fn())
  return { ...h, ui, props, document, writes,
    visible(value) { document.visibilityState = value ? 'visible' : 'hidden'; listeners.get('visibilitychange')?.() },
    reset() { [...resets].forEach(fn => fn()) },
    close() { cleanups.forEach(fn => fn()); scope.stop(); assert.equal(listeners.size, 0) } }
}

test('settings keeps optional status failures separate from CRUD and refreshes status after saving', async () => {
  let reads = 0
  const h = panel({ statusRead: async () => { reads++; throw new ApiError('unavailable', 503) } })
  await flush()
  assert.equal(h.ui.error.value, '')
  assert.match(h.ui.statusError.value, /当前活动规则仍继续服务/)
  assert.equal(h.ui.customRemaining.value, 3)
  await h.ui.toggle(h.ui.rules.value[0]); await flush()
  assert.equal(h.writes.length, 1)
  assert.equal(h.writes[0][1].expectedRevision, 1)
  assert.ok(h.writes[0][1].requestId)
  assert.ok(reads >= 2)
  assert.match(h.ui.notice.value, /识别语义变更会在后台应用/)
  assert.equal(h.ui.error.value, '')
  h.close()
})

test('settings status is idle while hidden or disabled and discards results after account changes', async () => {
  const pending = [], signals = []
  const h = panel({ hidden: true, statusRead: signal => {
    const d = deferred(); pending.push(d); signals.push(signal); return d.promise
  } })
  await flush(); assert.equal(pending.length, 0)
  h.visible(true); await flush(); assert.equal(pending.length, 1)
  h.props.enabled = false; await flush(); assert.equal(signals[0].aborted, true)
  pending[0].resolve({ state: 'active', applying: false }); await flush()
  assert.equal(h.ui.applicationStatus.value, null)
  h.props.enabled = true; await flush(); assert.equal(pending.length, 2)
  h.reset(); assert.equal(signals[1].aborted, true)
  pending[1].resolve({ state: 'applying', applying: true }); await flush()
  assert.equal(h.ui.applicationStatus.value, null)
  assert.equal(h.timers.size, 0)
  h.close()
})
