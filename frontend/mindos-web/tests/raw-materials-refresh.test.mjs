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
const waitFor = async (predicate, timeoutMs = 200) => {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error('waitFor timed out')
    await new Promise(resolve => setImmediate(resolve))
  }
}
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
  return { ui, mounts, toasts, close() { unmounts.forEach(callback => callback()); scope.stop() } }
}

test('initial folder and material reads start together and materials render without waiting for folders', async () => {
  const pendingFolders = deferred()
  const pendingMaterials = deferred()
  let folderReads = 0
  let materialReads = 0
  const f = fixture({
    listFolderNodes: async () => { folderReads += 1; return pendingFolders.promise },
    listMaterials: async () => { materialReads += 1; return pendingMaterials.promise },
  })

  const mounted = f.mounts[0]()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(folderReads, 1)
  assert.equal(materialReads, 1, 'material loading must not wait for the folder request')

  pendingMaterials.resolve({ items: [{ ...material('ready-row'), folderId: 7, folder: '已有名称' }] })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(f.ui.loading.value, false, 'the table becomes usable while the folder tree is still pending')
  assert.equal(f.ui.folderDisplayName(7, '已有名称'), '已有名称')

  pendingFolders.resolve({ items: [{ id: 7, name: '最新名称', parentId: null, materialCount: 1, subtreeMaterialCount: 1 }] })
  await mounted
  assert.equal(f.ui.folderDisplayName(7, '已有名称'), '最新名称')
  f.close()
})

test('folder failure is isolated from a successful material listing', async () => {
  const f = fixture({
    listFolderNodes: async () => { throw new Error('目录连接失败') },
    listMaterials: async () => ({ items: [material('visible-row')] }),
  })
  await f.mounts[0]()
  assert.equal(f.ui.items.value.length, 1)
  assert.equal(f.ui.error.value, '')
  assert.equal(f.ui.folderError.value, '目录连接失败')
  f.close()
})

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

test('knowledge-card transition states keep refreshing after material processing is complete', async () => {
  const originalSetTimeout = globalThis.setTimeout
  const originalClearTimeout = globalThis.clearTimeout
  const timers = []
  let timerId = 0
  globalThis.setTimeout = (callback, delay) => {
    const timer = { id: ++timerId, callback, delay, active: true }
    timers.push(timer)
    return timer.id
  }
  globalThis.clearTimeout = (id) => {
    const timer = timers.find(candidate => candidate.id === id)
    if (timer) timer.active = false
  }

  let reads = 0
  const states = ['generating', 'confirming', 'indexing', 'draft']
  const card = state => ({ state, knowledgeId: null, indexState: null, errorCode: null })
  const f = fixture({
    listMaterials: async () => {
      reads += 1
      return {
        items: [{
          ...material('card-row'),
          knowledgeCard: card(states[reads - 1]),
        }],
      }
    },
  })
  try {
    await f.ui.loadMaterials()
    for (const [index, state] of states.slice(0, -1).entries()) {
      const scheduled = timers.find(timer => timer.active)
      assert.equal(scheduled?.delay, 1800, `a ${state} card must schedule another list read`)
      scheduled.active = false
      scheduled.callback()
      await waitFor(() => reads === index + 2)
    }
    assert.equal(
      timers.some(timer => timer.active),
      false,
      'a draft card is terminal for automatic list refresh',
    )
  } finally {
    f.close()
    globalThis.setTimeout = originalSetTimeout
    globalThis.clearTimeout = originalClearTimeout
  }
})

assert.match(source, /v-else-if="error && !displayItems\.length"/)
assert.match(source, /:key="item\.materialId"/)

test('missing or new card states are never silently presented as pending', () => {
  const f = fixture({})
  const row = material('state-row')
  assert.equal(f.ui.knowledgeCardMeta(row).label, '状态未提供')
  for (const [state, label] of Object.entries({ draft: '草稿待确认', draft_failed: '草稿生成失败', index_failed: '已确认，索引失败', purged: '卡片已删除', state_conflict: '卡片状态待修复', future_state: '状态待核对' })) {
    assert.equal(f.ui.knowledgeCardMeta({ ...row, knowledgeCard: { state } }).label, label)
  }
  assert.equal(f.ui.knowledgeCardMeta({ ...row, knowledgeCard: { state: 'draft_failed', errorCode: 'draft_missing' } }).label, '尚未创建卡片')
  f.close()
})

test('upload progress updates the reactive row and 100 percent still waits for server acceptance', async () => {
  const pending = deferred()
  let report
  const f = fixture({
    uploadFile: async (_file, _folder, progress) => { report = progress; return pending.promise },
    listMaterials: async () => ({ items: [material('accepted')] }),
  })
  const importing = f.ui.importFiles([{ name: 'file.txt', type: 'text/plain', size: 100 }])
  assert.equal(f.ui.transientUploads.value[0].uploadProgress.loaded, 0)
  report({ loaded: 50, total: 100, phase: 'uploading' })
  assert.equal(f.ui.displayItems.value[0].uploadProgress.loaded, 50)
  report({ loaded: 100, total: 100, phase: 'finalizing' })
  assert.equal(f.ui.importing.value, true)
  assert.equal(f.toasts.length, 0, 'byte transfer is not business success')
  pending.resolve(material('accepted'))
  await importing
  assert.equal(f.ui.transientUploads.value.length, 0)
  f.close()
})
