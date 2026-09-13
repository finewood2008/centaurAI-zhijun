import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createVisiblePoller } from '../src/composables/visiblePolling.ts'

function fakeTimers() {
  let next = 0
  const queued = new Map()
  return {
    queued,
    set(callback, delay) { const id = ++next; queued.set(id, { callback, delay }); return id },
    clear(id) { queued.delete(id) },
    fire() {
      const [id, task] = queued.entries().next().value
      queued.delete(id)
      task.callback()
    },
  }
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

test('closed panel does no work; opening reads models once, recurring reads only monitoring', async () => {
  const timers = fakeTimers()
  const calls = []
  const poller = createVisiblePoller(async initial => { calls.push(initial) }, 6000, timers)
  await poller.refresh()
  assert.deepEqual(calls, [])
  assert.equal(timers.queued.size, 0)
  poller.setActive(true)
  await poller.refresh()
  assert.deepEqual(calls, [true])
  assert.equal(timers.queued.size, 1)
  assert.equal([...timers.queued.values()][0].delay, 6000)
  timers.fire()
  await poller.refresh(false)
  assert.deepEqual(calls, [true, false])
  poller.dispose()
  assert.equal(timers.queued.size, 0)
})

test('slow reads coalesce manual refreshes and never queue overlapping polling', async () => {
  const timers = fakeTimers()
  const wait = deferred()
  let calls = 0
  const poller = createVisiblePoller(async () => { calls++; await wait.promise }, 6000, timers)
  poller.setActive(true)
  const first = poller.refresh()
  assert.equal(first, poller.refresh())
  await Promise.resolve()
  assert.equal(calls, 1)
  assert.equal(timers.queued.size, 0)
  wait.resolve()
  await first
  assert.equal(timers.queued.size, 1)
  poller.dispose()
})

test('closing or hiding clears polling, and showing the panel refreshes again', async () => {
  const timers = fakeTimers()
  let calls = 0
  const poller = createVisiblePoller(async () => { calls++ }, 6000, timers)
  poller.setActive(true)
  await poller.refresh()
  poller.setActive(false)
  assert.equal(timers.queued.size, 0)
  await poller.refresh()
  assert.equal(calls, 1)
  poller.setActive(true)
  await poller.refresh()
  assert.equal(calls, 2)
  poller.dispose()
})

test('a response arriving after hiding or unmounting cannot restart polling', async () => {
  for (const stop of ['hide', 'dispose']) {
    const timers = fakeTimers()
    const wait = deferred()
    const poller = createVisiblePoller(() => wait.promise, 6000, timers)
    poller.setActive(true)
    const pending = poller.refresh()
    await Promise.resolve()
    if (stop === 'hide') poller.setActive(false)
    else poller.dispose()
    wait.resolve()
    await pending
    assert.equal(timers.queued.size, 0, stop)
    poller.dispose()
    poller.setActive(true)
    await poller.refresh()
    assert.equal(timers.queued.size, 0)
  }
})

test('hiding before a queued read starts suppresses it, and failures still allow a later poll', async () => {
  const timers = fakeTimers()
  let calls = 0
  const poller = createVisiblePoller(async () => { calls++; throw new Error('offline') }, 6000, timers)
  poller.setActive(true)
  const pending = poller.refresh()
  poller.setActive(false)
  await pending
  assert.equal(calls, 0)
  poller.setActive(true)
  await poller.refresh()
  assert.equal(calls, 1)
  assert.equal(timers.queued.size, 1)
  poller.dispose()
})

test('actions during an older poll coalesce into one fresh model read and await its completion', async () => {
  const timers = fakeTimers()
  const oldRead = deferred()
  const freshRead = deferred()
  const calls = []
  const poller = createVisiblePoller(async initial => {
    calls.push(initial)
    if (calls.length === 2) await oldRead.promise
    if (calls.length === 3) await freshRead.promise
  }, 6000, timers)
  poller.setActive(true)
  await poller.refresh()
  timers.fire()
  await Promise.resolve()
  assert.deepEqual(calls, [true, false])
  let completed = false
  const first = poller.invalidate().then(() => { completed = true })
  const second = poller.invalidate()
  const previous = poller.refresh(false)
  oldRead.resolve()
  await previous
  await Promise.resolve()
  assert.deepEqual(calls, [true, false, true])
  assert.equal(completed, false)
  assert.equal(timers.queued.size, 0)
  freshRead.resolve()
  await Promise.all([first, second])
  assert.equal(completed, true)
  assert.deepEqual(calls, [true, false, true])
  assert.equal(timers.queued.size, 1)
  poller.dispose()
})

test('closing or disposing discards a pending post-action refresh', async () => {
  for (const stop of ['hide', 'dispose']) {
    const timers = fakeTimers()
    const oldRead = deferred()
    let calls = 0
    const poller = createVisiblePoller(async () => { calls++; await oldRead.promise }, 6000, timers)
    poller.setActive(true)
    await Promise.resolve()
    const invalidated = poller.invalidate()
    if (stop === 'hide') poller.setActive(false)
    else poller.dispose()
    oldRead.resolve()
    await invalidated
    assert.equal(calls, 1)
    assert.equal(timers.queued.size, 0)
    poller.dispose()
  }
})
