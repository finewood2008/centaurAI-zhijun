import assert from 'node:assert/strict'
import test from 'node:test'
import { createChatImportPoller, hasTransitionalImports } from '../src/composables/chatImportPolling.ts'

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function fakeTimers() {
  let sequence = 0
  const tasks = new Map()
  return {
    api: {
      set(callback, delayMs) { const id = ++sequence; tasks.set(id, { callback, delayMs }); return id },
      clear(id) { tasks.delete(id) },
    },
    delays: () => [...tasks.values()].map(task => task.delayMs),
    runNext() {
      const next = tasks.entries().next().value
      assert.ok(next, 'expected a scheduled poll')
      tasks.delete(next[0]); next[1].callback()
    },
    count: () => tasks.size,
  }
}

const listing = (signature, state = 'complete', fileState = 'ready') => ({
  signature,
  items: state === 'empty' ? [] : [{ state, files: [{ state: fileState }] }],
})

function fixture(read, refreshMessages, isTargetCurrent) {
  const clock = fakeTimers()
  const applied = [], changes = [], errors = []
  const poller = createChatImportPoller({
    read,
    apply: value => applied.push(value.signature),
    signature: value => value.signature,
    isTransitional: value => hasTransitionalImports(value.items),
    refreshMessages: refreshMessages || (async id => { changes.push(id); return true }),
    isTargetCurrent,
    onError: error => errors.push(error),
    intervalMs: 2500,
    maxBackoffMs: 10000,
    timers: clock.api,
  })
  return { poller, clock, applied, changes, errors }
}

test('first success establishes a baseline and only transitional imports keep polling', async () => {
  const replies = [listing('initial', 'queued', 'reading'), listing('changed', 'complete')]
  const f = fixture(async () => replies.shift())
  await f.poller.start('conversation-a')
  assert.deepEqual(f.applied, ['initial'])
  assert.deepEqual(f.changes, [], 'initial observation must not refresh messages')
  assert.deepEqual(f.clock.delays(), [2500])
  f.clock.runNext()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(f.applied, ['initial', 'changed'])
  assert.deepEqual(f.changes, ['conversation-a'])
  assert.equal(f.clock.count(), 0, 'all-terminal success stops polling')
})

test('empty and terminal listings stop until an explicit refresh resumes them', async () => {
  let reads = 0
  const f = fixture(async () => (++reads === 1 ? listing('empty', 'empty') : listing('active', 'waiting', 'ready')))
  await f.poller.start('conversation-a')
  assert.equal(f.clock.count(), 0)
  await f.poller.refresh()
  assert.equal(reads, 2)
  assert.deepEqual(f.clock.delays(), [2500])
})

test('refresh is single-flight and an explicit mutation queues exactly one fresh read', async () => {
  const first = deferred(), second = deferred()
  let reads = 0, concurrent = 0, maximum = 0
  const f = fixture(async () => {
    reads += 1; concurrent += 1; maximum = Math.max(maximum, concurrent)
    try { return await (reads === 1 ? first.promise : second.promise) }
    finally { concurrent -= 1 }
  })
  const initial = f.poller.start('conversation-a')
  const resumed = f.poller.refresh('conversation-a')
  assert.equal(reads, 1)
  first.resolve(listing('before', 'complete'))
  await initial
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(reads, 2, 'the mutation refresh must not be lost behind an older flight')
  second.resolve(listing('after', 'complete'))
  await resumed
  assert.equal(maximum, 1)
  assert.deepEqual(f.changes, ['conversation-a'])
})

test('failures back off exponentially to the cap and a success resets the delay', async () => {
  const replies = [new Error('one'), new Error('two'), new Error('three'), new Error('four'), listing('active', 'queued')]
  const f = fixture(async () => {
    const next = replies.shift()
    if (next instanceof Error) throw next
    return next
  })
  await f.poller.start('conversation-a')
  assert.deepEqual(f.clock.delays(), [2500])
  for (const expected of [5000, 10000, 10000]) {
    f.clock.runNext(); await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(f.clock.delays(), [expected])
  }
  f.clock.runNext(); await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(f.clock.delays(), [2500])
  assert.equal(f.errors.length, 4)
})

test('a failed message refresh keeps the old signature and retries with backoff', async () => {
  const replies = [listing('before', 'queued'), listing('after', 'queued'), listing('after', 'complete')]
  let messageRefreshes = 0
  const f = fixture(async () => replies.shift(), async () => {
    messageRefreshes += 1
    if (messageRefreshes === 1) throw new Error('message refresh failed')
    return true
  })
  await f.poller.start('conversation-a')
  f.clock.runNext(); await new Promise(resolve => setImmediate(resolve))
  assert.equal(f.errors.length, 1)
  assert.deepEqual(f.clock.delays(), [2500])
  f.clock.runNext(); await new Promise(resolve => setImmediate(resolve))
  assert.equal(messageRefreshes, 2)
  assert.equal(f.clock.count(), 0)
})

test('switching conversations and disposal suppress late results and scheduling', async () => {
  const reads = new Map([['conversation-a', deferred()], ['conversation-b', deferred()], ['conversation-c', deferred()]])
  const f = fixture(id => reads.get(id).promise)
  const old = f.poller.start('conversation-a')
  const current = f.poller.start('conversation-b')
  reads.get('conversation-b').resolve(listing('b', 'complete'))
  await current
  reads.get('conversation-a').resolve(listing('a', 'queued'))
  await old
  assert.deepEqual(f.applied, ['b'])
  assert.equal(f.clock.count(), 0)
  const pending = f.poller.start('conversation-c')
  f.poller.dispose()
  reads.get('conversation-c').resolve(listing('c', 'queued'))
  await pending
  assert.deepEqual(f.applied, ['b'])
  assert.equal(f.clock.count(), 0)
})

test('a changed external conversation ref suppresses a result before its watcher runs', async () => {
  const pending = deferred()
  let currentId = 'conversation-a'
  const f = fixture(() => pending.promise, undefined, id => id === currentId)
  const request = f.poller.start('conversation-a')
  currentId = 'conversation-b'
  pending.resolve(listing('old-a', 'queued'))
  await request
  assert.deepEqual(f.applied, [])
  assert.equal(f.clock.count(), 0)
})

test('transition classification covers upload, read and reply work only', () => {
  for (const state of ['uploading', 'queued', 'waiting', 'replying']) {
    assert.equal(hasTransitionalImports([{ state, files: [] }]), true, state)
  }
  for (const state of ['pending', 'uploading', 'saved', 'reading']) {
    assert.equal(hasTransitionalImports([{ state: 'complete', files: [{ state }] }]), true, state)
  }
  for (const state of ['complete', 'consent', 'failed', 'paused']) {
    assert.equal(hasTransitionalImports([{ state, files: [{ state: 'ready' }] }]), false, state)
  }
  assert.equal(hasTransitionalImports([]), false)
})
