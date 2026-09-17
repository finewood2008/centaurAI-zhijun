import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/components/lifecycle/LifecycleDangerPanel.vue', import.meta.url), 'utf8')
const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'lifecycle-panel-test' }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const impact = dependencies => ({ confirmToken: 'preview-token', expectedRevision: 12, blockingDependencies: dependencies, cleanupSummary: { vectors: 2, derivedRecords: 3 } })
function fixture(api, props = {}) {
  const mounts = [], events = [], exports = {}
  new Function('require', 'exports', compiled)(id => id === 'vue'
    ? { ...Vue, onMounted: callback => mounts.push(callback) } : { api }, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup({ targetType: 'material', targetId: 'm-1', targetTitle: 'sample.txt', recycled: false, ...props }, { expose: () => {}, emit: (...event) => events.push(event) }))
  return { ui, mounts, events, close: () => scope.stop() }
}

test('list recycle is preview-only until all dependency decisions are explicitly confirmed', async () => {
  const calls = []
  const f = fixture({
    getMaterialDeletionImpact: async id => { calls.push(['preview', id]); return impact([{ type: 'knowledge', id: 'k-1', title: 'card', allowedActions: ['recycle'] }]) },
    recycleMaterial: async (id, payload) => calls.push(['recycle', id, payload]),
    purgeMaterial: async () => assert.fail('list entry must never purge'),
  }, { recycleOnly: true, autoPreview: true })
  try {
    f.mounts[0]()
    await Promise.resolve()
    assert.deepEqual(calls, [['preview', 'm-1']])
    await f.ui.execute()
    assert.equal(calls.length, 1)
    f.ui.choices.value['knowledge:k-1'] = 'recycle'
    await f.ui.execute()
    assert.deepEqual(calls[1], ['recycle', 'm-1', { confirmToken: 'preview-token', expectedRevision: 12, dependencyActions: [{ type: 'knowledge', id: 'k-1', action: 'recycle' }] }])
    assert.ok(f.events.some(event => event[0] === 'completed' && event[1] === 'recycle'))
    await f.ui.open('purge')
    assert.equal(f.ui.mode.value, null)
    assert.match(source, /v-if="!recycleOnly"[^>]+@click="open\('purge'\)"/)
  } finally { f.close() }
})

test('task lock blocks deletion; concurrency rejection retains material and duplicate clicks never issue twice', async () => {
  let release, calls = 0
  const f = fixture({ getMaterialDeletionImpact: async () => impact([{ type: 'draft', id: 'job', title: 'active job', allowedActions: [] }]),
    recycleMaterial: async () => { calls++; return new Promise((_, reject) => { release = () => reject(new Error('revision conflict')) }) },
  }, { recycleOnly: true })
  try {
    await f.ui.open('recycle')
    await f.ui.execute()
    assert.equal(calls, 0)
    f.ui.impact.value = impact([])
    const pending = f.ui.execute()
    await f.ui.execute()
    f.ui.close()
    assert.equal(calls, 1)
    assert.equal(f.ui.mode.value, 'recycle')
    release()
    await pending
    assert.equal(f.ui.error.value, 'revision conflict')
    assert.equal(f.events.some(event => event[0] === 'completed'), false)
    assert.deepEqual(f.events.filter(event => event[0] === 'busy-change').at(-1), ['busy-change', false])
  } finally { f.close() }
})

test('detail and recycle bin retain restore and permanent-delete functionality', async () => {
  const calls = []
  const f = fixture({ getMaterialDeletionImpact: async () => impact([]), purgeMaterial: async () => calls.push('purge'), unrecycleMaterial: async () => calls.push('restore') }, { recycled: true })
  try {
    await f.ui.restore()
    await f.ui.open('purge')
    await f.ui.execute()
    assert.deepEqual(calls, ['restore', 'purge'])
  } finally { f.close() }
})
