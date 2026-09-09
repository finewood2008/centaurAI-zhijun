import assert from 'node:assert/strict'
import { createGeneratedDraftPoller, createGeneratedDraftRefresher } from '../src/composables/useGeneratedDraftRefresh.ts'

function deferred() {
  let resolve
  const promise = new Promise((res) => { resolve = res })
  return { promise, resolve }
}

async function waitFor(predicate, timeoutMs = 500) {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error('waitFor timed out')
    await new Promise(resolve => setTimeout(resolve, 1))
  }
}

async function testAppliesGeneratedDraft() {
  let materialId = 'A'
  let draft = { revision: 'r0', content: '', confirmed: false, userEdited: false }
  const refresher = createGeneratedDraftRefresher({
    fetch: async () => ({ ...draft, revision: 'r1', content: '生成正文' }),
    currentMaterialId: () => materialId,
    currentDraft: () => draft,
    isDirty: () => false,
    apply: (next) => { draft = next },
  })
  await refresher.refresh('A')
  assert.equal(draft.revision, 'r1')
  assert.equal(draft.content, '生成正文')
}

async function testDelayedDraftCannotOverwriteAnotherMaterial() {
  const pending = deferred()
  let materialId = 'A'
  let draft = { revision: 'r0', content: '', confirmed: false, userEdited: false }
  const applied = []
  const refresher = createGeneratedDraftRefresher({
    fetch: () => pending.promise,
    currentMaterialId: () => materialId,
    currentDraft: () => draft,
    isDirty: () => false,
    apply: (next) => applied.push(next),
  })
  const request = refresher.refresh('A')
  materialId = 'B'
  draft = { revision: 'b0', content: 'B', confirmed: false, userEdited: false }
  refresher.invalidate()
  pending.resolve({ revision: 'r1', content: 'A', confirmed: false, userEdited: false })
  await request
  assert.deepEqual(applied, [])
}

async function testLocalEditCannotBeOverwritten() {
  const pending = deferred()
  let dirty = false
  let draft = { revision: 'r0', content: '', confirmed: false, userEdited: false }
  const applied = []
  const refresher = createGeneratedDraftRefresher({
    fetch: () => pending.promise,
    currentMaterialId: () => 'A',
    currentDraft: () => draft,
    isDirty: () => dirty,
    apply: (next) => applied.push(next),
  })
  const request = refresher.refresh('A')
  dirty = true
  pending.resolve({ ...draft, revision: 'r1', content: '生成正文' })
  await request
  assert.deepEqual(applied, [])
}

async function testPollerRetriesErrorsAndPendingUntilTerminal() {
  let calls = 0
  let draft = { revision: 'r0', content: '', status: 'pending', confirmed: false, userEdited: false }
  const applied = []
  const poller = createGeneratedDraftPoller({
    fetch: async () => {
      calls += 1
      if (calls === 1) throw new Error('temporary transport failure')
      if (calls === 2) return { ...draft }
      return { ...draft, revision: 'r1', content: '最终正文', status: 'ok' }
    },
    currentMaterialId: () => 'A',
    currentDraft: () => draft,
    isDirty: () => false,
    apply: next => { draft = next; applied.push(next) },
    onTimeout: () => assert.fail('poller should finish before timeout'),
    intervalMs: 1,
    timeoutMs: 500,
  })

  poller.start('A')
  await waitFor(() => draft.status === 'ok')
  const callsAtTerminal = calls
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(calls, callsAtTerminal, 'terminal result must stop polling')
  assert.equal(applied.at(-1).content, '最终正文')
}

async function testPollerCancelsInflightReadOnNavigation() {
  const pending = deferred()
  let materialId = 'A'
  let draft = { revision: 'a0', content: '', status: 'pending', confirmed: false, userEdited: false }
  const applied = []
  const poller = createGeneratedDraftPoller({
    fetch: () => pending.promise,
    currentMaterialId: () => materialId,
    currentDraft: () => draft,
    isDirty: () => false,
    apply: next => applied.push(next),
    onTimeout: () => assert.fail('cancelled poller must not time out'),
    intervalMs: 1,
    timeoutMs: 500,
  })

  poller.start('A')
  materialId = 'B'
  draft = { revision: 'b0', content: 'B', status: 'pending', confirmed: false, userEdited: false }
  poller.stop()
  pending.resolve({ revision: 'a1', content: 'A', status: 'ok', confirmed: false, userEdited: false })
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(applied, [])
}

async function testPollerStopsWhenLocalEditStarts() {
  const pending = deferred()
  let calls = 0
  let dirty = false
  const draft = { revision: 'r0', content: '', status: 'pending', confirmed: false, userEdited: false }
  const applied = []
  const poller = createGeneratedDraftPoller({
    fetch: () => { calls += 1; return pending.promise },
    currentMaterialId: () => 'A',
    currentDraft: () => draft,
    isDirty: () => dirty,
    apply: next => applied.push(next),
    onTimeout: () => assert.fail('dirty poller must not time out'),
    intervalMs: 1,
    timeoutMs: 500,
  })

  poller.start('A')
  dirty = true
  pending.resolve({ ...draft })
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(calls, 1)
  assert.deepEqual(applied, [])
}

async function testPollerHasABoundedWait() {
  let calls = 0
  let timeouts = 0
  let draft = { revision: 'r0', content: '', status: 'pending', confirmed: false, userEdited: false }
  const poller = createGeneratedDraftPoller({
    fetch: async () => { calls += 1; return { ...draft } },
    currentMaterialId: () => 'A',
    currentDraft: () => draft,
    isDirty: () => false,
    apply: next => { draft = next },
    onTimeout: () => { timeouts += 1 },
    intervalMs: 1,
    timeoutMs: 15,
  })

  poller.start('A')
  await waitFor(() => timeouts === 1)
  const callsAtTimeout = calls
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(timeouts, 1)
  assert.equal(calls, callsAtTimeout, 'timeout must end the polling session')
}

await testAppliesGeneratedDraft()
await testDelayedDraftCannotOverwriteAnotherMaterial()
await testLocalEditCannotBeOverwritten()
await testPollerRetriesErrorsAndPendingUntilTerminal()
await testPollerCancelsInflightReadOnNavigation()
await testPollerStopsWhenLocalEditStarts()
await testPollerHasABoundedWait()
console.log('generated-draft-refresh: 7 tests OK')
