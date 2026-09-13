import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as Vue from 'vue'

const src = fileURLToPath(new URL('../src/', import.meta.url))
function modules() {
  const cache = new Map()
  function load(filename) {
    if (!extname(filename)) filename += '.ts'
    if (filename.endsWith('.json')) return JSON.parse(readFileSync(filename, 'utf8'))
    if (cache.has(filename)) return cache.get(filename).exports
    const module = { exports: {} }; cache.set(filename, module)
    const code = ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText
    const native = createRequire(filename)
    new Function('require', 'module', 'exports', code)(name => name.startsWith('.') ? load(resolve(dirname(filename), name)) : native(name), module, module.exports)
    return module.exports
  }
  return name => load(resolve(src, name))
}
const binding = { generation: 7, workspaceId: 'synthetic-workspace' }
const identity = { accountId: 'synthetic-account', deviceId: 'synthetic-device', workspaceId: binding.workspaceId }
const ready = { phase: 'ready', generation: 7, subject: identity }
const connecting = { phase: 'connecting', generation: 8, subject: { accountId: identity.accountId, deviceId: identity.deviceId } }
const restored = { ...ready, generation: 8 }
const ok = data => ({ ok: true, generation: 7, data })
const defer = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const jobId = 'a'.repeat(32)
const tick = () => new Promise(resolve => setImmediate(resolve))
function fixture() {
  const load = modules(), scope = load('shared/productScope.ts')
  scope.enableDesktopProduct(); scope.setProductScope('synthetic-scope-7')
  const start = defer(), poll = defer(), followingPoll = defer()
  let starts = 0, polls = 0
  const client = load('desktop/productClient.ts').createDesktopProductClient({
    start: () => { starts++; return start.promise }, poll: () => ++polls === 1 ? poll.promise : followingPoll.promise,
    cancel: async () => ok({}), uploadCancel: async () => ok({}),
  }, () => binding)
  return { load, scope, client, start, poll, counts: () => ({ starts, polls }) }
}
const write = client => client.request('/api/mindos/zhijun/onboarding', { method: 'POST', body: '{}' })

test('pending write start is captured before scope abort; stale response stays suppressed and warning survives recovery', async () => {
  const f = fixture(), request = write(f.client)
  const rejected = assert.rejects(request, { name: 'AbortError' })
  const { nextWriteUncertainty } = f.load('desktop/writeUncertainty.ts')
  assert.equal(f.client.hasPendingMutations(binding), true)
  const notice = nextWriteUncertainty(ready, connecting, null, f.client.hasPendingMutations(binding))
  assert.deepEqual(notice, identity)
  f.scope.setProductScope(null)
  await rejected
  f.start.resolve(ok({ id: jobId, state: 'queued', cursor: 0 }))
  await tick()
  assert.equal(f.client.hasPendingMutations(binding), false)
  assert.deepEqual(nextWriteUncertainty(connecting, restored, notice, false), identity)
  assert.equal(f.counts().starts, 1, 'no replay after scope invalidation')
  assert.equal(f.counts().polls, 0, 'late response cannot create a stale job stream')
  f.client.dispose()
})

test('mutating response stream remains pending after headers until completion', async () => {
  const f = fixture(), request = write(f.client)
  f.start.resolve(ok({ id: jobId, state: 'running', cursor: 0 }))
  f.poll.resolve(ok({ id: jobId, state: 'running', cursor: 1, hasMore: false,
    events: [{ seq: 1, kind: 'headers', status: 200, headers: {} }] }))
  // A second poll must remain outstanding rather than reusing the first page.
  const response = await request
  const reading = response.text()
  const rejected = assert.rejects(reading)
  assert.equal(f.client.hasPendingMutations(binding), true)
  const { nextWriteUncertainty } = f.load('desktop/writeUncertainty.ts')
  assert.deepEqual(nextWriteUncertainty(ready, connecting, null, true), identity)
  f.scope.setProductScope(null)
  await rejected
  f.client.dispose()
})

test('pure reads and completed writes never generate a reconnect warning', async () => {
  for (const mutating of [false, true]) {
    const f = fixture()
    const request = mutating ? write(f.client) : f.client.request('/api/mindos/materials')
    assert.equal(f.client.hasPendingMutations(binding), mutating)
    f.start.resolve(ok({ id: jobId, state: 'queued', cursor: 0 }))
    f.poll.resolve(ok({ id: jobId, state: 'succeeded', cursor: 2, hasMore: false,
      events: [{ seq: 1, kind: 'headers', status: 200, headers: {} }, { seq: 2, kind: 'end' }] }))
    await (await request).text()
    assert.equal(f.client.hasPendingMutations(binding), false)
    assert.equal(f.load('desktop/writeUncertainty.ts').nextWriteUncertainty(ready, connecting, null, false), null)
    f.client.dispose()
  }
})

test('warning is ownership-only, clears on logout or owner changes, and never crosses workspace', () => {
  const { nextWriteUncertainty, uncertainWriteMessage } = modules()('desktop/writeUncertainty.ts')
  for (const subject of [null, { ...identity, accountId: 'another-account' },
    { ...identity, deviceId: 'another-device' }, { ...identity, workspaceId: 'another-workspace' }]) {
    assert.equal(nextWriteUncertainty(ready, { ...restored, subject }, identity, true), null)
  }
  assert.deepEqual(Object.keys(identity).sort(), ['accountId', 'deviceId', 'workspaceId'])
  assert.equal(uncertainWriteMessage.includes('synthetic'), false)
})

test('host unknown-write reply arriving before its snapshot remains capturable, definite rejection does not', async () => {
  for (const code of ['WRITE_OUTCOME_UNKNOWN', 'INVALID_REQUEST']) {
    const f = fixture(), request = write(f.client)
    f.start.resolve({ ok: false, generation: 7, error: { code, message: 'synthetic-safe-error' } })
    await assert.rejects(request, error => error.code === code)
    assert.equal(f.client.hasPendingMutations(binding), code === 'WRITE_OUTCOME_UNKNOWN')
    const { nextWriteUncertainty } = f.load('desktop/writeUncertainty.ts')
    assert.equal(Boolean(nextWriteUncertainty(ready, connecting, null, f.client.hasPendingMutations(binding))), code === 'WRITE_OUTCOME_UNKNOWN')
    f.scope.setProductScope(null)
    assert.equal(f.client.hasPendingMutations(binding), false)
    f.client.dispose()
  }
})

test('desktop wiring captures before abort and keeps the static warning outside the generation-keyed page', () => {
  const source = readFileSync(resolve(src, 'desktop/DesktopApp.vue'), 'utf8')
  assert.ok(source.indexOf('uncertainWrite.value = nextWriteUncertainty') < source.indexOf('setProductScope(scopeKey)'))
  assert.match(source, /<div v-if="showUncertainWrite" role="alert" data-testid="uncertain-write-notice"/)
  assert.ok(source.indexOf('data-testid="uncertain-write-notice"') < source.indexOf('<RouterView v-if="ready"'))
})

test('normal reconnect CTA retains the generic warning through device selection and clears a different owner', () => {
  const { nextWriteUncertainty, showWriteUncertainty } = modules()('desktop/writeUncertainty.ts')
  const failed = { phase: 'failed', generation: 10, subject: { accountId: identity.accountId } }
  const notice = nextWriteUncertainty(connecting, failed, identity, false)
  assert.deepEqual(notice, identity)
  assert.equal(showWriteUncertainty(notice, failed), true)
  assert.deepEqual(nextWriteUncertainty(failed, restored, notice, false), identity)
  assert.equal(showWriteUncertainty(notice, restored), true)
  let previous = failed, preserved = notice
  for (const phase of ['disconnecting', 'selecting_device', 'connecting', 'ready']) {
    const next = { ...(phase === 'ready' ? restored : phase === 'connecting' ? connecting : failed), phase }
    preserved = nextWriteUncertainty(previous, next, preserved, false)
    assert.deepEqual(preserved, identity)
    assert.equal(showWriteUncertainty(preserved, next), true)
    previous = next
  }
  assert.equal(nextWriteUncertainty(previous, { phase: 'signed_out', subject: null }, notice, false), null)
  assert.equal(nextWriteUncertainty(failed, { ...restored, subject: { ...identity, deviceId: 'other-device' } }, notice, false), null)
})

test('compiled DesktopApp captures pending ownership before scope reset and renders warning across actual reconnect snapshots', async () => {
  const load = modules(), events = []
  let observe, pending = false
  const initial = { ...ready, capabilities: { product: true } }
  class Controller {
    state = { snapshot: initial }
    observe(listener) { observe = listener; return () => {} }
    start() {} dispose() {}
  }
  const source = readFileSync(resolve(src, 'desktop/DesktopApp.vue'), 'utf8')
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'uncertainty-desktop-app' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const module = { exports: {} }
  const previousWindow = globalThis.window
  globalThis.window = { zhijunDesktop: { product: {} } }
  try {
    const req = name => {
      if (name === 'vue') return { ...Vue, onMounted() {}, onBeforeUnmount() {}, provide() {} }
      if (name === 'vue-router') return { useRouter: () => ({ replace: async () => {} }) }
      if (name === './controller') return { DesktopController: Controller }
      if (name === './productClient') return { createDesktopProductClient: () => ({
        hasPendingMutations() { events.push('capture'); return pending }, request() {}, dispose() {},
      }) }
      if (name === './writeUncertainty') return load('desktop/writeUncertainty.ts')
      if (name === '../shared/productScope') return { setProductScope() { events.push('reset'); pending = false } }
      if (name === '../services/transport') return { installProductTransport: () => () => {} }
      if (name === '../services/productFiles') return { installProductFiles: () => () => {} }
      return {}
    }
    new Function('require', 'module', 'exports', code)(req, module, module.exports)
    const view = module.exports.default.setup({}, { expose() {} })
    observe({ snapshot: initial })
    pending = true; events.length = 0
    observe({ snapshot: { ...connecting, capabilities: { product: false } } })
    assert.deepEqual(events.slice(0, 2), ['capture', 'reset'])
    assert.equal(view.showUncertainWrite.value, true)
    assert.deepEqual(view.uncertainWrite.value, identity)
    assert.equal(view.ready.value, false, 'old product page remains inaccessible')
    const failed = { phase: 'failed', generation: 9, subject: { accountId: identity.accountId }, capabilities: { product: false } }
    for (const phase of ['failed', 'disconnecting', 'selecting_device']) {
      observe({ snapshot: { ...failed, phase } })
      assert.equal(view.showUncertainWrite.value, true)
    }
    observe({ snapshot: { ...restored, capabilities: { product: true } } })
    await tick()
    assert.equal(view.showUncertainWrite.value, true)
    observe({ snapshot: { ...restored, subject: { ...identity, deviceId: 'other-device' }, capabilities: { product: true } } })
    assert.equal(view.showUncertainWrite.value, false)
    assert.equal(view.uncertainWrite.value, null)
  } finally {
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
  }
})
