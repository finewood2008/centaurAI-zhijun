import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/pages/RawMaterialsPage.vue', import.meta.url), 'utf8')
const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'raw-materials-test' }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const material = (materialId, status = 'available') => ({
  materialId, fileName: `${materialId}.txt`, fileType: 'document', status, jobId: '', errorMessage: null,
  folder: '', folderId: null, createdAt: '2026-09-07T00:00:00Z', materialFamilyId: materialId,
  versionNumber: 1, supersedesMaterialId: null, supersededByMaterialId: null, versionNote: null,
})

function fixture(api) {
  const mounts = [], unmounts = [], toasts = []
  const gate = (() => {
    let revision = 0
    return { next: () => ++revision, isCurrent: value => value === revision, invalidate: () => { revision++ } }
  })()
  const exports = {}
  const require = id => {
    if (id === 'vue') return { ...Vue, onMounted: callback => mounts.push(callback), onBeforeUnmount: callback => unmounts.push(callback) }
    if (id === 'vue-router') return { useRoute: () => ({ query: {} }), useRouter: () => ({ push: () => {} }) }
    if (id === '@/services/api') return { api }
    if (id === '@/shared/status') return { materialStatusMeta: () => ({}) }
    if (id === '@/shared/format') return { formatDate: String, formatFileType: String }
    if (id === '@/composables/useToast') return { useToast: () => value => toasts.push(value) }
    if (id === '@/composables/sessionGate') return { createSessionGate: () => gate }
    return {}
  }
  new Function('require', 'exports', compiled)(require, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup({}, { expose: () => {} }))
  return { ui, toasts, close() { unmounts.forEach(callback => callback()); scope.stop() } }
}

test('a background material refresh keeps the populated table visible until the keyed row updates', async () => {
  const pending = deferred()
  let reads = 0
  const f = fixture({ listMaterials: async () => (++reads === 1 ? { items: [material('same-row')] } : pending.promise) })
  await f.ui.loadMaterials()
  const original = f.ui.items.value[0]
  const refresh = f.ui.loadMaterials()
  assert.equal(f.ui.loading.value, false, 'only the first load may replace the table with a loader')
  assert.equal(f.ui.items.value[0], original, 'the current keyed row remains mounted while the refresh is pending')
  pending.resolve({ items: [material('same-row', 'failed')] })
  await refresh
  assert.equal(f.ui.items.value[0].materialId, 'same-row')
  assert.equal(f.ui.items.value[0].status, 'failed')
  f.close()
})

test('an accepted upload replaces its transient row before the follow-up listing settles', async () => {
  const pendingList = deferred()
  let reads = 0
  const uploaded = material('accepted-upload', 'uploaded')
  const f = fixture({
    listMaterials: async () => (++reads === 1 ? { items: [material('existing')] } : pendingList.promise),
    uploadFile: async () => uploaded,
  })
  await f.ui.loadMaterials()
  const importing = f.ui.importFiles([{ name: 'accepted-upload.txt', type: 'text/plain' }])
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(f.ui.items.value.map(item => item.materialId), ['accepted-upload', 'existing'])
  assert.equal(f.ui.transientUploads.value.length, 0, 'the real server row takes over without an empty gap')
  assert.equal(f.ui.loading.value, false)
  pendingList.resolve({ items: [material('accepted-upload'), material('existing')] })
  await importing
  assert.equal(f.ui.importing.value, false)
  assert.equal(f.toasts.at(-1).type, 'success')
  f.close()
})

assert.match(source, /v-else-if="error && !displayItems\.length"/)
assert.match(source, /:key="item\.materialId"/)
