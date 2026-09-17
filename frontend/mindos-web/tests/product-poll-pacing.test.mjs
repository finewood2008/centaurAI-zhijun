import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, extname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import ts from 'typescript'

const src = fileURLToPath(new URL('../src/', import.meta.url))
function fixture() {
  let now = 0, nextTimer = 0
  const timers = new Map(), cache = new Map()
  class ClockDate extends Date { static now() { return now } }
  const clock = {
    now: () => now,
    advance: ms => { now += ms },
    setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, at: now + ms }); return id },
    clearTimeout: id => timers.delete(id),
    async flush() { for (let i = 0; i < 30; i++) await Promise.resolve() },
    async run(work) {
      let settled = false, result, error
      work.then(value => { settled = true; result = value }, caught => { settled = true; error = caught })
      for (let i = 0; i < 20000 && !settled; i++) {
        await clock.flush()
        if (settled) break
        if (timers.size) {
          const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0]
          now = Math.max(now, timer.at); timers.delete(id); timer.fn()
        }
      }
      assert.equal(settled, true, 'operation must finish within the bounded fake clock')
      if (error) throw error
      return result
    },
  }
  function load(name) {
    let file = resolve(src, name)
    if (!extname(file)) file += '.ts'
    if (file.endsWith('.json')) return JSON.parse(readFileSync(file, 'utf8'))
    if (cache.has(file)) return cache.get(file).exports
    const module = { exports: {} }; cache.set(file, module)
    const native = createRequire(file)
    const code = ts.transpileModule(readFileSync(file, 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true,
    } }).outputText
    new Function('require', 'module', 'exports', 'setTimeout', 'clearTimeout', 'Date', code)(
      name => name.startsWith('.') ? load(resolve(dirname(file), name)) : name.startsWith('@/') ? load(name.slice(2)) : native(name),
      module, module.exports, clock.setTimeout, clock.clearTimeout, ClockDate)
    return module.exports
  }
  const scope = load('shared/productScope.ts')
  scope.enableDesktopProduct(); scope.setProductScope('pacing-workspace')
  return { clock, timers, scope, load, client: product => load('desktop/productClient.ts').createDesktopProductClient(product,
    () => ({ generation: 7, workspaceId: 'pacing-workspace' })) }
}
const id = 'a'.repeat(32)
const ok = data => ({ ok: true, generation: 7, data })
const page = (cursor, events = [], state = 'running') => ok({ id, cursor, events, state, hasMore: false })

test('empty pacing accounts for poll duration, stays bounded and resets on progress', () => {
  const pacing = fixture().load('desktop/pollPacing.ts').createPollPacing()
  assert.deepEqual(Array.from({ length: 8 }, () => pacing.nextDelay(false, 250)), [0, 0, 250, 750, 1750, 1750, 1750, 1750])
  assert.equal(pacing.nextDelay(false, 8000), 0, 'real long polls are not delayed again')
  assert.equal(pacing.nextDelay(true, 0), 0)
  assert.equal(pacing.nextDelay(false, 0), 125)
})

test('a minute-empty renderer mutation uses fewer than 40 polls, then drains streaming pages immediately', async () => {
  const f = fixture(), calls = { starts: 0, polls: 0, empty: 0, active: 0, maximum: 0 }, progressTimes = []
  const client = f.client({
    start: async () => { calls.starts++; return ok({ id, cursor: 0, state: 'queued' }) },
    poll: async (_context, input) => {
      calls.active++; calls.maximum = Math.max(calls.maximum, calls.active); calls.polls++
      assert.equal(input.waitMs, 8000)
      await Promise.resolve(); calls.active--
      if (f.clock.now() < 60000) { f.clock.advance(250); calls.empty++; return page(0) }
      progressTimes.push(f.clock.now())
      if (input.after === 0) return page(1, [{ seq: 1, kind: 'headers', status: 200, headers: { 'content-type': 'text/event-stream' } }])
      if (input.after <= 240) return page(input.after + 1, [{ seq: input.after + 1, kind: 'chunk', data: new TextEncoder().encode('x') }])
      return page(242, [{ seq: 242, kind: 'end' }], 'succeeded')
    },
    cancel: async () => { throw Error('successful operation must not be cancelled') },
  })
  try {
    const result = await f.clock.run((async () => (await client.request('/api/mindos/conversations/c_test/messages', {
      method: 'POST', body: JSON.stringify({ content: 'synthetic' }),
    })).text())())
    assert.equal(result, 'x'.repeat(240))
    assert.ok(calls.empty < 40, `minute-empty task used ${calls.empty} polls`)
    assert.equal(calls.starts, 1, 'pacing never replays a mutation')
    assert.equal(calls.maximum, 1, 'polls must remain serial')
    assert.equal(new Set(progressTimes).size, 1, 'ready event pages have no pacing delay')
    assert.equal(f.timers.size, 0)
  } finally { client.dispose() }
})

test('cancellation during empty-page delay promptly cancels once and stops polling', async () => {
  const f = fixture(), abort = new AbortController()
  let polls = 0, cancels = 0
  const client = f.client({
    start: async () => ok({ id, cursor: 0, state: 'queued' }),
    poll: async () => { polls++; return page(0) },
    cancel: async () => { cancels++; return ok({}) },
  })
  const work = client.request('/api/mindos/conversations', { signal: abort.signal })
  const rejection = assert.rejects(work, error => error.name === 'AbortError')
  await f.clock.flush()
  assert.equal(f.timers.size, 1)
  abort.abort()
  await rejection; await f.clock.flush()
  assert.equal(f.timers.size, 0)
  assert.equal(polls, 1)
  assert.equal(cancels, 1)
  client.dispose()
})

test('raw-resource preparation uses the same pacing and is aborted on workspace reset', async () => {
  const f = fixture()
  let polls = 0, cancels = 0
  const client = f.client({
    start: async () => ok({ id, cursor: 0, state: 'queued' }),
    poll: async () => { polls++; return page(0) },
    cancel: async () => { cancels++; return ok({}) },
  })
  const catalog = f.load('services/productCatalog.ts')
  // This catalog path is also used by the original-material detail download.
  assert.equal(catalog.resolveProductOperation('/api/mindos/materials/m_test/file').operation.response, 'bytes')
  const work = client.saveResource('/api/mindos/materials/m_test/file', 'synthetic.pdf')
  const rejection = assert.rejects(work, error => error.name === 'AbortError')
  await f.clock.flush()
  assert.equal(f.timers.size, 1)
  f.scope.setProductScope('different-workspace')
  await rejection; await f.clock.flush()
  assert.equal(f.timers.size, 0); assert.equal(polls, 1); assert.equal(cancels, 1)
  client.dispose()
})

test('renderer keeps sanitized remote capacity diagnostics without retrying start', async () => {
  const f = fixture()
  let starts = 0
  const client = f.client({ start: async () => {
    starts++
    return { ok: false, generation: 7, error: { code: 'BOX_BUSY', message: '盒子繁忙', httpStatus: 429, remoteCode: 'WORKSPACE_OPERATION_CAPACITY' } }
  } })
  await assert.rejects(client.request('/api/mindos/conversations'), error => error.code === 'BOX_BUSY'
    && error.status === 429 && error.remoteCode === 'WORKSPACE_OPERATION_CAPACITY')
  assert.equal(starts, 1)
  client.dispose()
})

test('empty-page pacing retains the ten-minute deadline and cancels without mutation replay', async () => {
  const f = fixture()
  let starts = 0, cancels = 0, polls = 0
  const client = f.client({
    start: async () => { starts++; return ok({ id, cursor: 0, state: 'queued' }) },
    poll: async () => { polls++; f.clock.advance(250); return page(0) },
    cancel: async () => { cancels++; return ok({}) },
  })
  await assert.rejects(f.clock.run(client.request('/api/mindos/conversations', {
    method: 'POST', body: JSON.stringify({ title: 'synthetic' }),
  })), error => error.code === 'REQUEST_TIMEOUT' && error.status === 504)
  assert.equal(f.clock.now(), 600000)
  assert.ok(polls < 310)
  assert.equal(starts, 1); assert.equal(cancels, 1); assert.equal(f.timers.size, 0)
  client.dispose()
})

test('raw-resource readiness drains headers/blob/end and saves once after empty polling', async () => {
  const f = fixture(), saved = []
  let starts = 0, polls = 0
  const resource = { id: 'b'.repeat(32), contentType: 'application/pdf', size: 100 }
  const client = f.client({
    start: async () => { starts++; return ok({ id, cursor: 0, state: 'queued' }) },
    poll: async () => {
      polls++
      if (f.clock.now() < 10000) { f.clock.advance(250); return page(0) }
      return page(3, [
        { seq: 1, kind: 'headers', status: 200, headers: {} },
        { seq: 2, kind: 'blob', blob: resource },
        { seq: 3, kind: 'end' },
      ], 'succeeded')
    },
    save: async (_context, input) => { saved.push(input); return ok({ saved: true }) },
    cancel: async () => { throw Error('completed download must not be cancelled') },
  })
  await f.clock.run(client.saveResource('/api/mindos/materials/m_test/file', 'synthetic.pdf'))
  assert.equal(starts, 1); assert.ok(polls < 12)
  assert.deepEqual(saved, [{ fileName: 'synthetic.pdf', contentType: 'application/pdf', source: { kind: 'blob', id: resource.id } }])
  client.dispose()
})
