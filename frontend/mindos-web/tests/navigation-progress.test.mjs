import assert from 'node:assert/strict'
import test from 'node:test'
import { createNavigationProgressReader } from '../src/services/navigationProgress.ts'

test('ready navigation expires at 30 seconds and concurrent reads share one request', async () => {
  let now = 0, calls = 0, resolve
  const reader = createNavigationProgressReader(() => {
    calls++
    return new Promise(done => { resolve = done })
  }, () => true, () => now)
  const first = reader.read(), second = reader.read()
  assert.equal(calls, 1)
  resolve({ state: 'ready' })
  await Promise.all([first, second])
  now = 29_999
  await reader.read()
  assert.equal(calls, 1)
  now = 30_000
  const expired = reader.read()
  assert.equal(calls, 2)
  resolve({ state: 'not_started' })
  await expired
  const incomplete = reader.read()
  assert.equal(calls, 3)
  resolve({ state: 'not_started' })
  await incomplete
})

test('invalidated late response cannot be delivered or replace a newer in-flight request', async () => {
  const completions = []
  const reader = createNavigationProgressReader(() => new Promise(done => completions.push(done)), () => true)
  const old = reader.read()
  reader.invalidate()
  const current = reader.read()
  completions[0]({ state: 'ready', conversationId: 'old-box' })
  await assert.rejects(old, { name: 'AbortError' })
  assert.equal(reader.read(), current)
  completions[1]({ state: 'ready', conversationId: 'new-box' })
  await current
  assert.equal((await reader.read()).conversationId, 'new-box')
  reader.invalidate()
  const fresh = reader.read()
  assert.equal(completions.length, 3)
  completions[2]({ state: 'not_started' })
  await fresh
})

test('failures are retried and browser reads never use the desktop hint', async () => {
  let calls = 0, enabled = true
  const reader = createNavigationProgressReader(async () => {
    if (++calls === 1) throw new Error('offline')
    return { state: 'ready' }
  }, () => enabled)
  await assert.rejects(reader.read(), /offline/)
  await reader.read()
  await reader.read()
  assert.equal(calls, 2)
  enabled = false
  await reader.read()
  await reader.read()
  assert.equal(calls, 4)
})

test('invalidation also rejects a ready cache hit before its asynchronous delivery', async () => {
  const reader = createNavigationProgressReader(async () => ({ state: 'ready', conversationId: 'old-box' }), () => true)
  await reader.read()
  const cached = reader.read()
  reader.invalidate()
  await assert.rejects(cached, { name: 'AbortError' })
})
