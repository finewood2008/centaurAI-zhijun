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

test('material pages use scoped list filters and reset to the first page when filters change', async () => {
  const requests = []
  const f = fixture({ listMaterials: async params => {
    requests.push(params)
    return { items: [material(`page-${params.offset}`)], total: 121 }
  } })
  try {
    await f.ui.loadMaterials()
    assert.equal(requests[0].limit, 50)
    assert.equal(requests[0].offset, 0)
    assert.equal(f.ui.total.value, 121)
    await f.ui.changePage(50)
    assert.equal(requests.at(-1).offset, 50)
    f.ui.keyword.value = ' 查询 '
    f.ui.selectedFolderId.value = 7
    await f.ui.loadMaterials()
    assert.equal(requests.at(-1).offset, 0)
    assert.equal(requests.at(-1).keyword, '查询')
    assert.equal(requests.at(-1).folderId, 7)
    await f.ui.changePage(-50)
    await f.ui.changePage(150)
    assert.equal(requests.length, 3)
  } finally { f.close() }
})

test('material pagination backs up when the last page disappears and rejects stale page responses', async () => {
  const stale = deferred()
  let count = 120
  let deferredRead = false
  const requests = []
  const f = fixture({ listMaterials: async params => {
    requests.push(params.offset)
    if (deferredRead) { deferredRead = false; return stale.promise }
    return { items: params.offset < count ? [material(`page-${params.offset}`)] : [], total: count }
  } })
  try {
    await f.ui.loadMaterials()
    await f.ui.changePage(100)
    count = 60
    await f.ui.loadMaterials()
    assert.equal(f.ui.pageOffset.value, 50)
    assert.deepEqual(requests.slice(-2), [100, 50])
    deferredRead = true
    const old = f.ui.loadMaterials()
    f.ui.keyword.value = 'new'
    await f.ui.loadMaterials()
    stale.resolve({ items: [material('stale')], total: 999 })
    await old
    assert.equal(f.ui.items.value[0].materialId, 'page-0')
    assert.equal(f.ui.total.value, 60)
    assert.equal(f.ui.refreshing.value, false)
  } finally { f.close() }
})

test('active sensitive scans and material lifecycle transitions continue polling', () => {
  const f = fixture({})
  try {
    for (const state of ['queued', 'processing']) {
      assert.equal(f.ui.hasActiveMaterial([{ ...material('scan'), sensitiveScan: { state } }]), true)
    }
    for (const state of ['completed', 'failed', 'canceled']) {
      assert.equal(f.ui.hasActiveMaterial([{ ...material('scan'), sensitiveScan: { state } }]), false)
    }
    assert.equal(f.ui.hasActiveMaterial([material('restoring', 'restoring')]), true)
    assert.equal(f.ui.hasActiveMaterial([material('purging', 'purging')]), true)
  } finally { f.close() }
})

test('upload from a filtered later page resets pagination and keeps accepted rows through delayed list visibility', async () => {
  let observed = false
  const accepted = material('new-upload', 'uploaded')
  const requests = []
  const f = fixture({
    listMaterials: async params => {
      requests.push(params)
      return { items: observed ? [accepted] : [material('old')], total: 150 }
    },
    uploadFile: async () => accepted,
  })
  try {
    f.ui.keyword.value = 'old'
    f.ui.selectedFolderId.value = 7
    await f.ui.loadMaterials()
    await f.ui.changePage(50)
    await f.ui.importFiles([{ name: 'new.txt', type: 'text/plain', size: 3 }])
    assert.equal(requests.at(-1).offset, 0)
    assert.equal(requests.at(-1).keyword, '')
    assert.equal(requests.at(-1).folderId, undefined)
    assert.equal(f.ui.awaitingList.value.length, 1)
    assert.deepEqual(f.ui.displayItems.value.map(item => item.materialId), ['new-upload', 'old'])
    observed = true
    await f.ui.loadMaterials()
    assert.equal(f.ui.awaitingList.value.length, 0)
    assert.deepEqual(f.ui.displayItems.value.map(item => item.materialId), ['new-upload'])
  } finally { f.close() }
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
    if (id === 'vue-router') return { onBeforeRouteLeave: () => {}, useRoute: () => ({ query: {} }), useRouter: () => ({ push: () => {} }) }
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

test('list recycle hides transient, recycled and task-locked rows without bypassing lifecycle preview', () => {
  const f = fixture({})
  try {
    assert.equal(f.ui.canRecycle(material('available')), true)
    assert.equal(f.ui.canRecycle(material('failed', 'failed')), true)
    for (const status of ['uploaded', 'queued', 'processing', 'restoring', 'purging', 'recycled', 'deleted']) {
      assert.equal(f.ui.canRecycle(material('locked', status)), false)
    }
    for (const row of [
      { ...material('transient'), transientUpload: true }, { ...material('recycled'), recycled: true },
      { ...material('scan'), sensitiveScan: { state: 'processing' } },
      { ...material('card'), knowledgeCard: { state: 'confirming' } },
    ]) assert.equal(f.ui.canRecycle(row), false)
    f.ui.openRecycle(material('first'))
    f.ui.openRecycle(material('second'))
    assert.equal(f.ui.recycleTarget.value.materialId, 'first')
    f.ui.recycleBusy.value = true
    f.ui.closeRecycle()
    assert.equal(f.ui.recycleTarget.value.materialId, 'first')
    f.ui.recycleBusy.value = false
    f.ui.closeRecycle()
    assert.equal(f.ui.recycleTarget.value, null)
    assert.match(source, /label="移至回收站"[^>]+@click\.stop="openRecycle\(item\)"/)
    assert.match(source, /<LifecycleDangerPanel[^>]+recycle-only auto-preview/)
    assert.doesNotMatch(source, /api\.(?:recycleMaterial|purgeMaterial)\(/)
  } finally { f.close() }
})

test('recycle removes accepted-upload projection and ignores earlier list responses; refresh shows restored material', async () => {
  const oldList = deferred(), newList = deferred()
  let reads = 0
  const row = material('recyclable')
  const f = fixture({
    listMaterials: async () => ++reads === 1 ? oldList.promise : reads === 2 ? newList.promise : { items: [row], total: 1 },
    listFolderNodes: async () => ({ items: [] }),
  })
  try {
    f.ui.items.value = [row]
    f.ui.total.value = 1
    f.ui.awaitingList.value = [{ item: row, until: Date.now() + 5000 }]
    const staleRequest = f.ui.loadMaterials()
    f.ui.openRecycle(row)
    const recycling = f.ui.onMaterialRecycled('recycle')
    assert.equal(f.ui.items.value.length, 0)
    assert.equal(f.ui.awaitingList.value.length, 0)
    oldList.resolve({ items: [row], total: 1 })
    await staleRequest
    assert.equal(f.ui.items.value.length, 0)
    newList.resolve({ items: [], total: 0 })
    await recycling
    assert.equal(f.ui.total.value, 0)
    assert.equal(f.ui.recycleTarget.value, null)
    await f.ui.loadMaterials()
    assert.equal(f.ui.items.value[0].materialId, row.materialId)
  } finally { f.close() }
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
