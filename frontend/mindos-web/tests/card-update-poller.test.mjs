import assert from 'node:assert/strict'
import { createCardUpdatePoller } from '../src/composables/useCardUpdatePoller.ts'

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

{
  const states = ['indexing', 'recovering', 'done']
  const seen = []
  const poller = createCardUpdatePoller({
    fetch: async () => {
      const state = states.shift()
      return { pendingUpdate: state === 'done' ? null : { state } }
    },
    onResult: (_id, item) => seen.push(item.pendingUpdate?.state ?? 'done'),
    fastDelayMs: 2,
    slowDelayMs: 2,
    fastPeriodMs: 20,
    timeoutMs: 100,
  })
  poller.start('knowledge_a')
  await wait(50)
  assert.deepEqual(seen, ['indexing', 'recovering', 'done'])
  poller.stop()
}

{
  let calls = 0
  const poller = createCardUpdatePoller({
    fetch: async () => ({ pendingUpdate: { state: 'indexing' } }),
    onResult: () => { calls += 1 },
    fastDelayMs: 5,
    timeoutMs: 50,
  })
  poller.start('knowledge_a')
  poller.stop()
  await wait(15)
  assert.equal(calls, 0)
}

console.log('card-update-poller: 2 tests OK')
