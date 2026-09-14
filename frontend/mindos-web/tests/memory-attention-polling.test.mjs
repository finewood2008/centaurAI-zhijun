import assert from 'node:assert/strict'
import { createMemoryAttentionPoller } from '../src/composables/useMemoryAttentionPolling.ts'

const flush = async () => { await new Promise(resolve => setImmediate(resolve)) }
function fixture(poll, onSettled = () => {}) {
  let current = 'a', nextId = 0
  const timers = new Map(), calls = [], timeouts = []
  const observer = createMemoryAttentionPoller({
    isCurrent: cid => cid === current,
    poll: async (cid, ids) => { calls.push([cid, ids]); return poll(cid, ids) },
    onTimeout: cid => timeouts.push(cid),
    onSettled,
    schedule: callback => { const id = ++nextId; timers.set(id, callback); return id },
    cancel: id => timers.delete(id), maxAttempts: 5,
  })
  return { observer, timers, calls, timeouts, setCurrent(cid) { current = cid }, async tick() {
    const first = timers.entries().next().value
    assert.ok(first, 'a bounded observation tick is scheduled')
    timers.delete(first[0]); first[1](); await flush()
  } }
}

// Final-state reads still run after pending count reaches zero, including one
// brief zero between requeue and worker claim; a resumed job keeps observation.
{
  const pending = [false, true, false, false, false]
  const h = fixture(() => pending.shift())
  h.observer.start('a', ['fresh'])
  for (let i = 0; i < 5; i++) await h.tick()
  assert.equal(h.calls.length, 5)
  assert.equal(h.timers.size, 0)
  assert.deepEqual(h.timeouts, [])
  assert.ok(h.calls.every(([, ids]) => ids.join() === 'fresh'))
}

// New turns merge only observed IDs; an old page cannot enqueue its late event.
{
  const h = fixture(() => true)
  h.observer.start('a', ['one']); h.observer.start('a', ['two'])
  await h.tick()
  assert.deepEqual(h.calls[0], ['a', ['one', 'two']])
  h.setCurrent('b'); h.observer.start('b', ['new']); h.observer.start('a', ['late'])
  await h.tick()
  assert.deepEqual(h.calls.at(-1), ['b', ['new']])
  h.observer.stop(); assert.equal(h.timers.size, 0)
}

// A late response after navigation/stop must never schedule another old poll.
{
  let resolve
  const h = fixture(() => new Promise(done => { resolve = done }))
  h.observer.start('a', ['old']); await h.tick()
  h.setCurrent('b'); h.observer.stop(); resolve(true); await flush()
  assert.equal(h.timers.size, 0)
  assert.deepEqual(h.timeouts, [])
}

// Errors do not pretend completion, and prolonged observation ends visibly.
{
  const h = fixture(() => { throw Error('read failed') })
  h.observer.start('a')
  for (let i = 0; i < 5; i++) await h.tick()
  assert.equal(h.timers.size, 0)
  assert.deepEqual(h.timeouts, ['a'])
}

// Even a task that finishes before the first pending-count sample gets a final
// candidate/outcome refresh after the last read and before observation settles.
{
  let visible = false
  const h = fixture(() => false, async cid => { assert.equal(cid, 'a'); visible = true })
  h.observer.start('a', ['fast'])
  await h.tick(); await h.tick(); assert.equal(visible, false)
  await h.tick(); assert.equal(visible, true)
  assert.equal(h.timers.size, 0)
}
console.log('memory attention polling: final-state refresh, bounded recovery, tracked jobs and session isolation passed')
