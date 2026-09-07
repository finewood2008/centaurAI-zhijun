import assert from 'node:assert/strict'
import { createGeneratedDraftRefresher } from '../src/composables/useGeneratedDraftRefresh.ts'

function deferred() {
  let resolve
  const promise = new Promise((res) => { resolve = res })
  return { promise, resolve }
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

await testAppliesGeneratedDraft()
await testDelayedDraftCannotOverwriteAnotherMaterial()
await testLocalEditCannotBeOverwritten()
console.log('generated-draft-refresh: 3 tests OK')
