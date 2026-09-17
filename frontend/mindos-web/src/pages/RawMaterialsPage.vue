<script setup lang="ts">
import DataHubBackLink from '@/components/ui/DataHubBackLink.vue'
// 原材料资料库：桌面端多级目录树筛选侧栏 + 高密表格（B2 FE-UI-011 / P14-06 目录树）
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ChevronDown, ChevronRight, Eye, Folder, FolderPlus, Pencil, FolderInput, Play, Plus, Trash2, Upload, X } from 'lucide-vue-next'
import { api, type FolderNode, type UploadResult } from '@/services/api'
import { materialSensitiveScanStatusMeta, materialStatusMeta } from '@/shared/status'
import { formatDate, formatFileType } from '@/shared/format'
import { useToast } from '@/composables/useToast'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import MaterialPager from '@/components/ui/MaterialPager.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import IconButton from '@/components/ui/IconButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import LifecycleDangerPanel from '@/components/lifecycle/LifecycleDangerPanel.vue'
import { createSessionGate } from '@/composables/sessionGate'
import type { UploadProgress } from '@/services/transport'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const items = ref<UploadResult[]>([])
const total = ref(0)
const pageOffset = ref(0)
const pageSize = 50
const refreshing = ref(false)
let lastFilters = ''
type DisplayMaterial = UploadResult & { transientUpload?: boolean; uploadProgress?: UploadProgress }
const transientUploads = ref<DisplayMaterial[]>([])
const awaitingList = ref<Array<{ item: UploadResult; until: number }>>([])
const showAwaitingList = computed(() => pageOffset.value === 0 && !type.value && !status.value && !keyword.value.trim() && !tag.value.trim() && selectedFolderId.value === null)
const displayItems = computed<DisplayMaterial[]>(() => {
  const persisted = new Set(items.value.map(item => item.materialId))
  const awaiting = showAwaitingList.value ? awaitingList.value.map(entry => entry.item).filter(item => !persisted.has(item.materialId)) : []
  return [...transientUploads.value, ...awaiting, ...items.value]
})
const loading = ref(true)
const error = ref('')
const folderError = ref('')
const type = ref('')
// 支持从首页失败任务等入口带筛选参数进入（/materials?status=failed）
const status = ref(typeof route.query.status === 'string' ? route.query.status : '')
const keyword = ref(typeof route.query.keyword === 'string' ? route.query.keyword : '')
const tag = ref('')
const importInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)
let refreshTimer: ReturnType<typeof setTimeout> | null = null
let loadedOnce = false
let disposed = false
const materialLoadGate = createSessionGate()

const activeKnowledgeCardStates = new Set<NonNullable<UploadResult['knowledgeCard']>['state']>([
  'generating', 'confirming', 'indexing', 'recycling', 'restoring', 'purging',
])

function hasActiveMaterial(items: UploadResult[]) {
  return items.some((item) => (
    item.status === 'uploaded'
    || item.status === 'queued'
    || item.status === 'processing'
    || item.status === 'restoring'
    || item.status === 'purging'
    || item.sensitiveScan?.state === 'queued'
    || item.sensitiveScan?.state === 'processing'
    || Boolean(item.knowledgeCard && activeKnowledgeCardStates.has(item.knowledgeCard.state))
  ))
}

function stopRefreshTimer() {
  if (refreshTimer !== null) clearTimeout(refreshTimer)
  refreshTimer = null
}

function scheduleRefresh() {
  stopRefreshTimer()
  if (disposed || !(hasActiveMaterial(items.value) || (showAwaitingList.value && awaitingList.value.some(entry => entry.until > Date.now())))) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    void loadMaterials()
  }, 1800)
}

const knowledgeCardStateMeta: Record<NonNullable<UploadResult['knowledgeCard']>['state'], { label: string; className: string }> = {
  waiting: { label: '等待资料处理', className: 'is-muted' },
  generating: { label: '正在生成草稿', className: 'is-pending' },
  draft: { label: '草稿待确认', className: 'is-draft' },
  confirming: { label: '确认中', className: 'is-pending' },
  indexing: { label: '已确认，索引中', className: 'is-pending' },
  available: { label: '已确认，可检索', className: 'is-ready' },
  failed: { label: '处理失败', className: 'is-failed' },
  recycled: { label: '卡片在回收站', className: 'is-muted' },
  unknown: { label: '已确认，待修复', className: 'is-failed' },
  draft_failed: { label: '草稿生成失败', className: 'is-failed' },
  index_failed: { label: '已确认，索引失败', className: 'is-failed' },
  state_conflict: { label: '卡片状态待修复', className: 'is-failed' },
  recycling: { label: '正在回收', className: 'is-pending' },
  restoring: { label: '正在恢复', className: 'is-pending' },
  purging: { label: '正在删除', className: 'is-pending' },
  purged: { label: '卡片已删除', className: 'is-muted' },
  merged: { label: '已合并到其他卡片', className: 'is-ready' },
}

function knowledgeCardMeta(item: DisplayMaterial) {
  if (item.transientUpload) return { label: '上传后处理', className: 'is-muted' }
  if (!item.knowledgeCard) return { label: '状态未提供', className: 'is-muted' }
  if (item.knowledgeCard.errorCode === 'draft_missing') return { label: '尚未创建卡片', className: 'is-muted' }
  return knowledgeCardStateMeta[item.knowledgeCard.state] ?? { label: '状态待核对', className: 'is-failed' }
}

function displayStatus(item: UploadResult) {
  return item.status === 'uploaded'
    ? { label: '已上传，等待处理', tone: 'info' as const }
    : materialStatusMeta(item.status)
}

// ---- P14-06：多级目录树（ID 驱动；null = 全部，未分类由 folderId=null 的资料表示）----
const folderNodes = ref<FolderNode[]>([])
const selectedFolderId = ref<number | null>(null)
const expandedIds = ref<Set<number>>(new Set())

interface FlatFolder extends FolderNode {
  depth: number
  hasChildren: boolean
}

// 按父级分组并同层按名称排序，供侧栏 / 选择器复用
const byParent = computed(() => {
  const map = new Map<number | null, FolderNode[]>()
  for (const n of folderNodes.value) {
    const arr = map.get(n.parentId) ?? []
    arr.push(n)
    map.set(n.parentId, arr)
  }
  for (const arr of map.values()) arr.sort((a, b) => a.name.localeCompare(b.name, 'zh'))
  return map
})

// 展开状态下拍平为树形行（depth 供缩进）
const flatTree = computed<FlatFolder[]>(() => {
  const out: FlatFolder[] = []
  const walk = (parentId: number | null, depth: number) => {
    for (const n of byParent.value.get(parentId) ?? []) {
      const children = byParent.value.get(n.id) ?? []
      out.push({ ...n, depth, hasChildren: children.length > 0 })
      if (children.length && expandedIds.value.has(n.id)) walk(n.id, depth + 1)
    }
  }
  walk(null, 0)
  return out
})

const nameById = computed(() => new Map(folderNodes.value.map((n) => [n.id, n.name])))

function folderDisplayName(id?: number | null, fallback = ''): string {
  if (id == null) return '未分类'
  return nameById.value.get(id) ?? (fallback.trim() || '未分类')
}

// 自身 + 全部后代节点 ID（用于删除/选择目标时禁用）
function subtreeIds(id: number): Set<number> {
  const set = new Set<number>([id])
  let changed = true
  while (changed) {
    changed = false
    for (const n of folderNodes.value) {
      if (n.parentId !== null && set.has(n.parentId) && !set.has(n.id)) {
        set.add(n.id)
        changed = true
      }
    }
  }
  return set
}

function toggleExpand(id: number) {
  const next = new Set(expandedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedIds.value = next
}

async function loadFolders() {
  folderError.value = ''
  try {
    const res = await api.listFolderNodes('RAW')
    folderNodes.value = res.items
    // 默认展开全部包含子节点的目录
    const parentIds = new Set(res.items.map((n) => n.parentId).filter((p): p is number => p !== null))
    expandedIds.value = new Set(res.items.filter((n) => parentIds.has(n.id)).map((n) => n.id))
  } catch (e) {
    folderError.value = e instanceof Error ? e.message : '目录加载失败'
  }
}

async function loadMaterials() {
  stopRefreshTimer()
  const filters = {
    type: type.value, status: status.value, keyword: keyword.value.trim(),
    folderId: selectedFolderId.value ?? undefined, tag: tag.value.trim(),
  }
  const signature = JSON.stringify(filters)
  if (lastFilters !== signature) { pageOffset.value = 0; lastFilters = signature }
  const offset = pageOffset.value
  const requestSession = materialLoadGate.next()
  refreshing.value = true
  if (!loadedOnce) loading.value = true
  error.value = ''
  try {
    const response = await api.listMaterials({ ...filters, limit: pageSize, offset })
    if (!materialLoadGate.isCurrent(requestSession)) return
    const count = response.total ?? response.items.length
    if (offset > 0 && offset >= count) {
      pageOffset.value = Math.max(0, Math.floor((count - 1) / pageSize) * pageSize)
      await loadMaterials()
      return
    }
    total.value = count
    items.value = response.items
    const observed = new Set(response.items.map(item => item.materialId))
    awaitingList.value = awaitingList.value.filter(entry => !observed.has(entry.item.materialId))
    loadedOnce = true
  } catch (e) {
    if (materialLoadGate.isCurrent(requestSession)) error.value = e instanceof Error ? e.message : '原材料加载失败'
  } finally {
    if (materialLoadGate.isCurrent(requestSession)) {
      loading.value = false
      refreshing.value = false
      scheduleRefresh()
    }
  }
}

function changePage(offset: number) {
  if (refreshing.value || importing.value || !Number.isInteger(offset) || offset < 0 || offset >= total.value) return
  pageOffset.value = offset
  return loadMaterials()
}

async function importFiles(files: FileList | File[]) {
  if (importing.value) return
  const selected = Array.from(files)
  if (!selected.length) return
  importing.value = true
  let accepted = 0
  let failed = 0
  try {
    // 顺序上传使 material_jobs 的 created_at 与用户选择顺序一致；单 worker 将按 FIFO 处理。
    for (const file of selected) {
      const transient: DisplayMaterial = {
        materialId: `uploading-${crypto.randomUUID()}`,
        fileName: file.name,
        fileType: file.type.startsWith('image/') ? 'image' : file.type.startsWith('audio/') ? 'audio' : 'document',
        status: 'uploaded', jobId: '', errorMessage: null, folder: '', folderId: selectedFolderId.value,
        createdAt: new Date().toISOString(), materialFamilyId: '', versionNumber: 1,
        supersedesMaterialId: null, supersededByMaterialId: null, versionNote: null, transientUpload: true,
        uploadProgress: { loaded: 0, total: file.size, phase: 'uploading' },
      }
      transientUploads.value.push(transient)
      try {
        const uploaded = await api.uploadFile(file, selectedFolderId.value ?? undefined, progress => {
          // Mutate the reactive row, not the original object pushed into the ref.
          const row = transientUploads.value.find(item => item.materialId === transient.materialId)
          if (row && !disposed) row.uploadProgress = progress
        })
        if (disposed) return
        awaitingList.value.push({ item: uploaded, until: Date.now() + 300_000 })
        items.value = [uploaded, ...items.value.filter((item) => item.materialId !== uploaded.materialId)]
        loadedOnce = true
        accepted += 1
      } catch (e) {
        failed += 1
        toast({ type: 'error', message: `${file.name} 导入失败：${e instanceof Error ? e.message : '未知错误'}` })
      } finally {
        transientUploads.value = transientUploads.value.filter((item) => item.materialId !== transient.materialId)
      }
    }
    if (accepted) {
      pageOffset.value = 0
      type.value = ''; status.value = ''; keyword.value = ''; tag.value = ''
      selectedFolderId.value = null
    }
    if (!disposed) await loadMaterials()
    if (accepted) toast({ type: 'success', message: `${accepted} 个文件已上传，正在按顺序处理` })
  } finally {
    importing.value = false
  }
  if (failed && !accepted) toast({ type: 'error', message: '没有文件成功进入处理队列' })
}

function onImportPick(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files) void importFiles(input.files)
  input.value = ''
}

async function removeFromQueue(item: UploadResult) {
  try {
    await api.removeMaterialFromQueue(item.materialId)
    toast({ type: 'success', message: '已移出处理队列' })
    await loadMaterials()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '移出队列失败' })
  }
}

async function resumeProcessing(item: UploadResult) {
  try {
    await api.resumeUpload(item.materialId)
    toast({ type: 'success', message: '已继续处理资料' })
    await loadMaterials()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '继续处理失败' })
  }
}

function selectFolder(id: number | null) {
  selectedFolderId.value = id
  loadMaterials()
}

// ---- 移动资料到目录（folderId；'' = 未分类）----
const moveTarget = ref<UploadResult | null>(null)
const moveFolderId = ref<number | ''>('')
const moving = ref(false)

function openMove(item: UploadResult) {
  moveTarget.value = item
  moveFolderId.value = item.folderId ?? ''
}

async function confirmMove() {
  if (!moveTarget.value) return
  const materialId = moveTarget.value.materialId
  moveTarget.value = null
  moving.value = true
  try {
    await api.moveMaterial(materialId, moveFolderId.value === '' ? null : moveFolderId.value)
    toast({ type: 'success', message: '已移动资料' })
    if (selectedFolderId.value !== null) await loadMaterials()
  } catch (e) {
    const message = e instanceof Error ? e.message : '移动失败'
    toast({ type: 'error', message })
  } finally {
    moving.value = false
  }
}

// ---- 重命名目录 ----
const renameTarget = ref<FolderNode | null>(null)
const renameName = ref('')
const renaming = ref(false)

function openRenameFolder(node: FolderNode) {
  renameTarget.value = node
  renameName.value = node.name
}

async function confirmRenameFolder() {
  if (!renameTarget.value) return
  const node = renameTarget.value
  renameTarget.value = null
  renaming.value = true
  try {
    await api.renameFolderNode(node.id, renameName.value.trim())
    toast({ type: 'success', message: `已重命名文件夹「${node.name}」` })
    await loadFolders()
  } catch (e) {
    const message = e instanceof Error ? e.message : '重命名失败'
    toast({ type: 'error', message })
  } finally {
    renaming.value = false
  }
}

// ---- 创建目录（支持选择父级，默认根目录 / 触发节点的子目录）----
const showCreateFolder = ref(false)
const createFolderName = ref('')
const createParentId = ref<number | ''>('')
const creating = ref(false)

function openCreateFolder(parentId: number | null = null) {
  createFolderName.value = ''
  createParentId.value = parentId ?? ''
  showCreateFolder.value = true
}

async function confirmCreateFolder() {
  const name = createFolderName.value.trim()
  if (!name) return
  creating.value = true
  try {
    await api.createFolderNode(name, createParentId.value === '' ? null : createParentId.value)
    showCreateFolder.value = false
    toast({ type: 'success', message: `已创建文件夹「${name}」` })
    await loadFolders()
  } catch (e) {
    const message = e instanceof Error ? e.message : '创建失败'
    toast({ type: 'error', message })
  } finally {
    creating.value = false
  }
}

// ---- 删除目录（必须明确迁移去向：moveToRoot 或 targetFolderId）----
const deleteTarget = ref<FolderNode | null>(null)
const deleteMode = ref<'root' | 'target'>('root')
const deleteTargetFolderId = ref<number | ''>('')
const deleting = ref(false)

// 删除目标目录时禁用目标自身及其后代
const deleteDisabledIds = computed(() =>
  deleteTarget.value ? subtreeIds(deleteTarget.value.id) : new Set<number>(),
)

function openDelete(node: FolderNode) {
  deleteTarget.value = node
  deleteMode.value = 'root'
  deleteTargetFolderId.value = ''
}

async function confirmDeleteFolder() {
  if (!deleteTarget.value) return
  const node = deleteTarget.value
  deleteTarget.value = null
  deleting.value = true
  try {
    const opts =
      deleteMode.value === 'root'
        ? { moveToRoot: true }
        : { targetFolderId: deleteTargetFolderId.value === '' ? undefined : deleteTargetFolderId.value }
    const result = await api.deleteFolderNode(node.id, opts)
    await loadFolders()
    if (selectedFolderId.value !== null) await loadMaterials()
    const movedNote = result.movedMaterials > 0 || result.reparentedFolders > 0 ? `（${result.movedMaterials} 项资料迁移）` : ''
    toast({ type: 'success', message: `已删除文件夹「${node.name}」${movedNote}` })
  } catch (e) {
    const message = e instanceof Error ? e.message : '删除失败'
    toast({ type: 'error', message })
  } finally {
    deleting.value = false
  }
}

function clearFilters() {
  type.value = ''
  status.value = ''
  selectedFolderId.value = null
  keyword.value = ''
  tag.value = ''
  loadMaterials()
}

function openMaterial(item: UploadResult) {
  router.push({ path: `/materials/${item.materialId}`, query: { name: item.fileName } })
}

// 列表只提供软删除；依赖和并发校验仍由统一生命周期预览及执行接口完成。
const recycleTarget = ref<UploadResult | null>(null)
const recycleBusy = ref(false)
const recycleDialog = ref<HTMLElement | null>(null)
let recycleTrigger: HTMLElement | null = null
onBeforeRouteLeave(() => !recycleBusy.value)
watch(recycleTarget, async (target) => {
  if (typeof document === 'undefined') return
  if (target) {
    recycleTrigger = document.activeElement as HTMLElement | null
    await nextTick()
    recycleDialog.value?.focus()
  } else {
    if (recycleTrigger?.isConnected) recycleTrigger.focus()
    recycleTrigger = null
  }
})

function onRecycleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); closeRecycle(); return }
  if (event.key !== 'Tab' || !recycleDialog.value) return
  const controls = Array.from(recycleDialog.value.querySelectorAll<HTMLElement>('button:not(:disabled), select:not(:disabled), input:not(:disabled), [href]'))
  const first = controls[0], last = controls.at(-1)
  if (!first || !last) { event.preventDefault(); return }
  if (event.shiftKey && (document.activeElement === first || document.activeElement === recycleDialog.value)) {
    event.preventDefault(); last.focus()
  } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === recycleDialog.value)) {
    event.preventDefault(); first.focus()
  }
}

function canRecycle(item: DisplayMaterial) {
  return Boolean(item.materialId) && !item.transientUpload && !item.recycled
    && (item.status === 'available' || item.status === 'failed')
    && !hasActiveMaterial([item])
}

function openRecycle(item: DisplayMaterial) {
  if (recycleTarget.value || recycleBusy.value || !canRecycle(item)) return
  recycleTarget.value = item
}

function closeRecycle() {
  if (!recycleBusy.value) recycleTarget.value = null
}

async function onMaterialRecycled(action: 'recycle' | 'purge' | 'unrecycle') {
  if (action !== 'recycle' || !recycleTarget.value) return
  const materialId = recycleTarget.value.materialId
  // 不让较早发出的列表请求把刚回收的行重新写回来。
  materialLoadGate.invalidate()
  stopRefreshTimer()
  items.value = items.value.filter(item => item.materialId !== materialId)
  awaitingList.value = awaitingList.value.filter(entry => entry.item.materialId !== materialId)
  total.value = Math.max(0, total.value - 1)
  recycleTarget.value = null
  toast({ type: 'success', message: '已移至回收站，可在回收站恢复' })
  await Promise.allSettled([loadMaterials(), loadFolders()])
}

onMounted(async () => {
  await Promise.allSettled([loadFolders(), loadMaterials()])
})

onBeforeUnmount(() => {
  disposed = true
  materialLoadGate.invalidate()
  stopRefreshTimer()
})
</script>

<template>
  <div class="page">
    <DataHubBackLink />
    <div class="page-head">
      <h1>原材料</h1>
      <p>导入与管理原始资料，查看解析和索引状态。原文件保持只读。</p>
    </div>

    <div class="ws-layout">
      <!-- 多级目录树筛选侧栏（桌面） -->
      <aside class="ws-folders">
        <div class="ws-folders__head">
          <h2 class="ws-folders__title">文件夹</h2>
          <IconButton label="创建根目录" size="sm" @click="openCreateFolder(null)">
            <Plus :size="14" aria-hidden="true" />
          </IconButton>
        </div>
        <ul class="ws-folders__list">
          <li>
            <button
              class="ws-folders__item ws-folders__item--all"
              :class="{ 'is-active': selectedFolderId === null }"
              type="button"
              @click="selectFolder(null)"
            >
              <Folder :size="16" aria-hidden="true" />
              <span>全部文件夹</span>
              <span class="ws-folders__count">{{ folderNodes.length }}</span>
            </button>
          </li>
          <li v-for="node in flatTree" :key="node.id" class="ws-folders__li">
            <button
              class="ws-folders__item"
              :class="{ 'is-active': selectedFolderId === node.id }"
              :style="{ '--depth': String(node.depth) }"
              type="button"
              @click="selectFolder(node.id)"
            >
              <span v-if="node.hasChildren" class="ws-folders__caret" @click.stop="toggleExpand(node.id)">
                <ChevronDown v-if="expandedIds.has(node.id)" :size="14" aria-hidden="true" />
                <ChevronRight v-else :size="14" aria-hidden="true" />
              </span>
              <span v-else class="ws-folders__caret ws-folders__caret--empty"></span>
              <Folder :size="16" aria-hidden="true" />
              <span class="ws-folders__name">{{ node.name }}</span>
              <span class="ws-folders__count">{{ node.subtreeMaterialCount }}</span>
            </button>
            <div class="ws-folders__ops">
              <IconButton label="新建子目录" size="sm" @click.stop="openCreateFolder(node.id)">
                <FolderPlus :size="13" aria-hidden="true" />
              </IconButton>
              <IconButton label="重命名文件夹" size="sm" @click.stop="openRenameFolder(node)">
                <Pencil :size="13" aria-hidden="true" />
              </IconButton>
              <IconButton label="删除文件夹" size="sm" @click.stop="openDelete(node)">
                <Trash2 :size="13" aria-hidden="true" />
              </IconButton>
            </div>
          </li>
        </ul>
        <p v-if="folderError" class="ws-folders__error" role="alert">
          文件夹暂未更新
          <button type="button" @click="loadFolders">重试</button>
        </p>
      </aside>

      <!-- 右侧内容区 -->
      <div class="ws-main">
        <div class="ws-toolbar">
          <input
            v-model="keyword"
            class="ws-input"
            type="search"
            placeholder="搜索文件名…"
            aria-label="搜索文件名"
            @keyup.enter="loadMaterials"
          >
          <select v-model="type" class="ws-input ws-input--select" aria-label="资料类型" @change="loadMaterials">
            <option value="">全部类型</option>
            <option value="document">文档</option>
            <option value="image">图片</option>
            <option value="audio">音频</option>
          </select>
          <select v-model="status" class="ws-input ws-input--select" aria-label="处理状态" @change="loadMaterials">
            <option value="">全部状态</option>
            <option value="available">已完成</option>
            <option value="queued">等待处理</option>
            <option value="processing">处理中</option>
            <option value="uploaded">上传中</option>
            <option value="failed">失败</option>
          </select>
          <input
            v-model="tag"
            class="ws-input"
            type="search"
            placeholder="按标签筛选…"
            aria-label="按标签筛选"
            @keyup.enter="loadMaterials"
          >
          <BaseButton variant="secondary" size="sm" @click="clearFilters">清除筛选</BaseButton>
          <BaseButton variant="primary" size="sm" :loading="importing" @click="importInput?.click()">
            <Upload :size="14" aria-hidden="true" />导入资料
          </BaseButton>
          <input ref="importInput" type="file" multiple hidden @change="onImportPick" />
        </div>

        <p class="ws-material-note">资料由 Data Engine 处理；在对话中使用时，仍需确认材料及可用的脱敏方式。</p>
        <div v-if="loading" class="loading-state">正在加载原材料…</div>
        <ErrorState v-else-if="error && !displayItems.length" :message="error" retry-label="重试" @retry="loadMaterials" />
        <EmptyState
          v-else-if="!displayItems.length"
          title="暂无原材料"
          description="可导入文档、图片或音频；若已设置筛选条件，可清除筛选后查看。"
        >
          <template #action>
            <BaseButton variant="primary" size="sm" :loading="importing" @click="importInput?.click()">
              <Upload :size="14" aria-hidden="true" />导入资料
            </BaseButton>
          </template>
        </EmptyState>

        <div v-else class="ws-table">
          <p v-if="error" class="ws-table__refresh-error" role="alert">
            资料状态暂未更新：{{ error }}
            <button type="button" @click="loadMaterials">重试</button>
          </p>
          <div class="ws-table__head" aria-live="polite">
            <span>共 {{ total }} 项资料 · 本页 {{ items.length }} 项<span v-if="transientUploads.length"> · 正在上传 {{ transientUploads.length }} 项</span></span>
            <button type="button" :disabled="refreshing" @click="loadMaterials">{{ refreshing ? '更新中…' : '刷新状态' }}</button>
          </div>
          <p v-if="showAwaitingList && awaitingList.length" class="ws-table__sync" role="status">{{ awaitingList.length }} 项资料已上传，正在等待列表同步；暂未显示时可稍后刷新。</p>
          <div class="ws-table__scroll">
            <table class="ws-table__grid">
              <colgroup>
                <col class="ws-table__file-col">
                <col class="ws-table__type-col">
                <col class="ws-table__folder-col">
                <col class="ws-table__status-col">
                <col class="ws-table__date-col">
                <col class="ws-table__ops-col">
              </colgroup>
              <thead>
                <tr>
                  <th>文件名</th>
                  <th>类型</th>
                  <th>文件夹</th>
                  <th>状态</th>
                  <th>导入时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in displayItems" :key="item.materialId" @click="!item.transientUpload && openMaterial(item)">
                  <td class="ws-table__name" :title="item.fileName">
                    <button type="button" class="ws-material-name" :disabled="item.transientUpload" @click.stop="openMaterial(item)">{{ item.fileName }}</button>
                    <span v-if="item.knowledgeCard" class="knowledge-card-state" :class="knowledgeCardMeta(item).className">知识卡片 · {{ knowledgeCardMeta(item).label }}</span>
                  </td>
                  <td>{{ formatFileType(item.fileType) }}</td>
                  <td class="ws-table__folder" :title="folderDisplayName(item.folderId, item.folder)">{{ folderDisplayName(item.folderId, item.folder) }}</td>
                  <td>
                    <div v-if="item.transientUpload && item.uploadProgress" class="upload-progress" aria-live="polite">
                      <span>{{ item.uploadProgress.phase === 'finalizing' ? '上传 100% · 正在提交' : item.uploadProgress.total > 0 ? `上传中 ${Math.min(99, Math.floor(item.uploadProgress.loaded / item.uploadProgress.total * 100))}%` : '上传中…' }}</span>
                      <progress :value="item.uploadProgress.total > 0 ? item.uploadProgress.loaded : undefined" :max="Math.max(1, item.uploadProgress.total)" :aria-label="`${item.fileName} 上传进度`" />
                    </div>
                    <template v-else>
                      <StatusBadge :meta="displayStatus(item)" />
                      <span v-if="item.sensitiveScan" class="ws-scan-status" :class="{ 'is-failed': item.sensitiveScan.state === 'failed' }">{{ materialSensitiveScanStatusMeta(item.sensitiveScan)?.label }}</span>
                    </template>
                  </td>
                  <td>{{ formatDate(item.createdAt) }}</td>
                  <td class="ws-table__ops">
                    <div class="ws-table__actions">
                      <IconButton v-if="!item.transientUpload" label="查看详情" size="sm" @click.stop="openMaterial(item)">
                        <Eye :size="16" aria-hidden="true" />
                      </IconButton>
                      <IconButton v-if="!item.transientUpload" label="移动文件夹" size="sm" @click.stop="openMove(item)">
                        <FolderInput :size="16" aria-hidden="true" />
                      </IconButton>
                      <IconButton v-if="canRecycle(item)" label="移至回收站" size="sm" :disabled="!!recycleTarget || recycleBusy" @click.stop="openRecycle(item)">
                        <Trash2 :size="16" aria-hidden="true" />
                      </IconButton>
                      <IconButton
                        v-if="item.status === 'queued' && item.errorCode === 'service_interrupted'"
                        label="继续处理"
                        size="sm"
                        @click.stop="resumeProcessing(item)"
                      >
                        <Play :size="16" aria-hidden="true" />
                      </IconButton>
                      <IconButton
                        v-if="!item.transientUpload && (item.status === 'uploaded' || item.status === 'queued' || item.status === 'failed')"
                        label="移出队列"
                        size="sm"
                        @click.stop="removeFromQueue(item)"
                      >
                        <X :size="16" aria-hidden="true" />
                      </IconButton>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <MaterialPager v-if="total > pageSize || pageOffset > 0" :total="total" :offset="pageOffset" :size="pageSize" :busy="refreshing || importing" @change="changePage" />
      </div>
    </div>

    <Teleport to="body">
      <div v-if="recycleTarget" class="gov-modal-mask" @click.self="closeRecycle" @keydown.stop="onRecycleKeydown">
        <div ref="recycleDialog" class="gov-modal gov-modal--recycle" role="dialog" aria-modal="true" aria-label="移至回收站影响确认" tabindex="-1">
          <h3>移至回收站</h3>
          <p class="gov-modal-hint">回收后不再用于默认列表和检索，原材料可从回收站恢复。</p>
          <LifecycleDangerPanel :key="recycleTarget.materialId" target-type="material" :target-id="recycleTarget.materialId" :target-title="recycleTarget.fileName" :recycled="false" compact recycle-only auto-preview @busy-change="recycleBusy = $event" @completed="onMaterialRecycled" @cancel="closeRecycle" />
          <div class="gov-modal-actions">
            <BaseButton variant="secondary" size="sm" :disabled="recycleBusy" @click="closeRecycle">关闭</BaseButton>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 移动资料到目录 -->
    <div v-if="moveTarget" class="gov-modal-mask" @click.self="moveTarget = null">
      <div class="gov-modal" role="dialog" aria-modal="true" aria-label="移动资料">
        <h3>移动资料</h3>
        <p class="gov-modal-hint">将「{{ moveTarget.fileName }}」移动至目录：</p>
        <select v-model="moveFolderId" class="ws-input ws-folder-select" aria-label="目标目录">
          <option :value="''">未分类</option>
          <option v-for="node in flatTree" :key="node.id" :value="node.id">{{ '　'.repeat(node.depth) }}{{ node.name }}</option>
        </select>
        <div class="gov-modal-actions">
          <BaseButton variant="secondary" size="sm" :disabled="moving" @click="moveTarget = null">取消</BaseButton>
          <BaseButton variant="primary" size="sm" :disabled="moving" :loading="moving" @click="confirmMove">确认移动</BaseButton>
        </div>
      </div>
    </div>

    <!-- 重命名文件夹 -->
    <ConfirmDialog
      :open="!!renameTarget"
      title="重命名文件夹"
      confirm-text="确认重命名"
      :loading="renaming"
      @confirm="confirmRenameFolder"
      @cancel="renameTarget = null"
    >
      <p class="ws-dialog-note">将文件夹「{{ renameTarget?.name || '' }}」重命名为：</p>
      <input v-model="renameName" class="ws-input move-folder-input" type="text" placeholder="输入新文件夹名称" maxlength="120">
    </ConfirmDialog>

    <!-- 创建文件夹（支持选择父级目录） -->
    <div v-if="showCreateFolder" class="gov-modal-mask" @click.self="showCreateFolder = false">
      <div class="gov-modal" role="dialog" aria-modal="true" aria-label="创建文件夹">
        <h3>创建文件夹</h3>
        <label class="ws-field">
          <span>名称</span>
          <input
            v-model="createFolderName"
            class="ws-input"
            type="text"
            placeholder="输入文件夹名称"
            maxlength="120"
            @keyup.enter="confirmCreateFolder"
          >
        </label>
        <label class="ws-field">
          <span>父级目录</span>
          <select v-model="createParentId" class="ws-input ws-folder-select" aria-label="父级目录">
            <option :value="''">根目录（顶层）</option>
            <option v-for="node in flatTree" :key="node.id" :value="node.id">{{ '　'.repeat(node.depth) }}{{ node.name }}</option>
          </select>
        </label>
        <div class="gov-modal-actions">
          <BaseButton variant="secondary" size="sm" :disabled="creating" @click="showCreateFolder = false">取消</BaseButton>
          <BaseButton variant="primary" size="sm" :disabled="creating || !createFolderName.trim()" :loading="creating" @click="confirmCreateFolder">创建</BaseButton>
        </div>
      </div>
    </div>

    <!-- 删除文件夹确认（必须选择迁移去向） -->
    <ConfirmDialog
      :open="!!deleteTarget"
      title="删除文件夹"
      confirm-text="确认删除"
      :loading="deleting"
      @confirm="confirmDeleteFolder"
      @cancel="deleteTarget = null"
    >
      <p class="ws-dialog-note">「{{ deleteTarget?.name || '' }}」中的资料与子目录将被迁移（原文件不删除），请选择去向：</p>
      <div class="ws-radio">
        <label><input v-model="deleteMode" type="radio" value="root" /> 迁移到根目录（成为顶层目录）</label>
        <label><input v-model="deleteMode" type="radio" value="target" /> 迁移到指定目录</label>
      </div>
      <select
        v-if="deleteMode === 'target'"
        v-model="deleteTargetFolderId"
        class="ws-input ws-folder-select ws-field-margin"
        aria-label="目标目录"
      >
        <option disabled value="">请选择目标目录</option>
        <option
          v-for="node in flatTree"
          :key="node.id"
          :value="node.id"
          :disabled="deleteDisabledIds.has(node.id)"
        >
          {{ '　'.repeat(node.depth) }}{{ node.name }}
        </option>
      </select>
    </ConfirmDialog>
  </div>
</template>

<style scoped>
.ws-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

/* 文件夹侧栏 */
.ws-folders {
  width: 196px;
  flex-shrink: 0;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  overflow: hidden;
}

.ws-folders__title {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-secondary-color, #686b66);
  letter-spacing: 0.04em;
}

.ws-folders__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
}

.ws-folders__list {
  list-style: none;
  margin: 0;
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ws-folders__error {
  margin: 0;
  padding: 8px 12px 10px;
  border-top: 1px solid var(--ws-border-color-3, #ebe7de);
  color: var(--ws-danger-color, #a6452e);
  font-size: 12px;
}

.ws-folders__error button {
  margin-left: 6px;
  border: 0;
  padding: 0;
  color: inherit;
  background: transparent;
  text-decoration: underline;
  cursor: pointer;
}

.ws-folders__li {
  display: flex;
  align-items: center;
  gap: 2px;
}

.ws-folders__li .ws-folders__item {
  flex: 1;
  min-width: 0;
}

.ws-folders__li :deep(.icon-btn) {
  flex-shrink: 0;
}

/* 树形缩进：由行内 --depth 变量控制 */
.ws-folders__item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 7px 10px;
  padding-left: calc(10px + var(--depth, 0) * 16px);
  border: 1px solid transparent;
  border-radius: var(--ws-radius, 6px);
  background: transparent;
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition:
    background 0.15s,
    color 0.15s,
    border-color 0.15s;
}
.ws-folders__item:hover {
  background: var(--ws-surface-2, #fbf8f1);
}
.ws-folders__item.is-active {
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  color: var(--ws-primary-color, #a6452e);
  border-color: var(--ws-border-color-2, #e2ded4);
}
.ws-folders__item--all {
  font-weight: 600;
}
.ws-folders__caret {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  flex-shrink: 0;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
}
.ws-folders__caret--empty {
  color: transparent;
}
.ws-folders__name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-folders__count {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}

/* 行内操作按钮（hover 显示） */
.ws-folders__ops {
  display: none;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
.ws-folders__li:hover .ws-folders__ops {
  display: flex;
}

/* 移动 / 重命名弹窗输入框 */
.move-folder-input {
  width: 100%;
  margin-top: 8px;
}

.ws-dialog-note {
  margin: 0 0 4px;
  font-size: 13px;
  color: var(--ws-text-color, #3c403d);
}

/* 创建文件夹弹窗字段 */
.ws-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}
.ws-field > span {
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-secondary-color, #686b66);
}

/* 目录选择下拉 */
.ws-folder-select {
  width: 100%;
  height: 34px;
  margin-top: 8px;
}

.ws-field-margin {
  margin-top: 12px;
}

/* 删除迁移方式单选 */
.ws-radio {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 0 0;
  font-size: 13px;
  color: var(--ws-text-color, #3c403d);
}
.ws-radio label {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}

/* 复用治理弹窗样式（移动资料弹窗） */
.gov-modal-mask {
  position: fixed;
  inset: 0;
  z-index: 2500;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(29, 33, 31, 0.42);
}

.gov-modal {
  width: 100%;
  max-width: 440px;
  padding: 20px;
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  box-shadow: var(--ws-shadow-lg, 0 16px 48px rgba(0, 0, 0, 0.18));
}

.gov-modal h3 {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}

.gov-modal--recycle { max-width: 620px; max-height: calc(100dvh - 40px); overflow-y: auto; }

.gov-modal-hint {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--ws-text-color, #3c403d);
  word-break: break-word;
}

.gov-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}

/* 右侧内容区 */
.ws-main {
  flex: 1;
  min-width: 0;
  width: 100%;
}

.ws-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.ws-input {
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-body-bg, #fff);
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  font-size: 13px;
  min-width: 0;
}
.ws-input:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.ws-input--select {
  width: 120px;
}
.ws-input[type='search'] {
  flex: 1;
  min-width: 160px;
  max-width: 240px;
}

/* 视图切换 */
.ws-seg {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-surface-2, #fbf8f1);
  margin-bottom: 12px;
}

.ws-seg__btn {
  padding: 6px 14px;
  border: none;
  border-radius: var(--ws-radius-sm, 4px);
  background: transparent;
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition:
    color 0.15s,
    background 0.15s;
}
.ws-seg__btn:hover {
  color: var(--ws-text-primary-color, #1d211f);
}
.ws-seg__btn.is-active {
  background: var(--ws-body-bg, #fff);
  color: var(--ws-primary-color, #a6452e);
  box-shadow: var(--ws-shadow-sm, 0 1px 2px rgba(0, 0, 0, 0.06));
}

/* 表格 */
.ws-table {
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  overflow: hidden;
}

.ws-table__refresh-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 0;
  padding: 10px 16px;
  color: var(--ws-danger-color, #a6452e);
  background: var(--ws-danger-bg, #fff4f0);
  border-bottom: 1px solid var(--ws-danger-border, #efc4b8);
  font-size: 12px;
}

.ws-table__refresh-error button {
  border: 0;
  padding: 0;
  color: inherit;
  background: transparent;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}

.ws-table__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.ws-table__head button { border: 0; background: transparent; color: var(--ws-primary-color, #a6452e); font: inherit; cursor: pointer; padding: 4px; }
.ws-table__head button:disabled { opacity: .55; cursor: default; }
.ws-table__sync { margin: 0; padding: 10px 16px; font-size: 12px; color: var(--ws-text-secondary-color, #686b66); border-bottom: 1px solid var(--ws-border-color-3, #ebe7de); }
.ws-material-note { margin: 0 0 16px; color: var(--ws-text-secondary-color, #686b66); font-size: 12px; line-height: 1.6; }
.ws-material-name { display: block; max-width: 100%; padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.ws-material-name:focus-visible { outline: 2px solid var(--ws-primary-color, #a6452e); outline-offset: 2px; }
.ws-material-name:disabled { cursor: default; }
.ws-scan-status { display: block; margin-top: 5px; font-size: 11px; line-height: 1.5; color: var(--ws-text-secondary-color, #686b66); }
.ws-scan-status.is-failed { color: var(--ws-danger-color, #c43d3d); }

/* 复杂表格小屏可横向滚动 */
.ws-table__scroll {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.ws-table__grid {
  width: 100%;
  min-width: 620px;
  table-layout: fixed;
  border-collapse: collapse;
  font-size: 13px;
}
.ws-table__grid th {
  text-align: left;
  padding: 10px 8px;
  color: var(--ws-text-secondary-color, #686b66);
  font-weight: 600;
  background: var(--ws-surface-2, #fbf8f1);
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
  white-space: nowrap;
}
.ws-table__grid td {
  padding: 10px 8px;
  overflow-wrap: anywhere;
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
  color: var(--ws-text-color, #3c403d);
}
.ws-table__grid tbody tr {
  cursor: pointer;
  transition: background 0.15s;
}
.ws-table__grid tbody tr:hover {
  background: var(--ws-table-hover-bg, rgba(166, 69, 46, 0.03));
}
.ws-table__grid tbody tr:last-child td {
  border-bottom: none;
}

.ws-table__name,
.ws-table__folder {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-table__name {
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}

.ws-table__type-col { width: 48px; }
.ws-table__folder-col { width: 72px; }
.ws-table__status-col { width: 130px; }
.ws-table__date-col { width: 100px; }
.ws-table__ops-col { width: 80px; }

.ws-table__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}

/* 末列提示向左展开，避免透明提示框也撑出横向滚动范围。 */
.ws-table__actions :deep(.ws-tooltip--top .ws-tooltip__tip) {
  left: auto;
  right: 0;
  transform: translateY(2px);
}
.ws-table__actions :deep(.ws-tooltip--top:hover .ws-tooltip__tip),
.ws-table__actions :deep(.ws-tooltip--top:focus-within .ws-tooltip__tip) {
  transform: translateY(0);
}

.ws-table__grid :deep(.ws-badge) { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
.upload-progress { display: grid; gap: 5px; min-width: 0; font-size: 12px; color: #a66a1f; }
.upload-progress progress { width: 100%; min-width: 0; height: 6px; accent-color: #aa432c; }
.knowledge-card-state {
  display: block;
  margin-top: 5px;
  font-size: 11px;
  font-weight: 400;
  white-space: normal;
  overflow-wrap: anywhere;
  color: var(--ws-text-secondary-color, #686b66);
}
.knowledge-card-state.is-ready { color: #16803c; }
.knowledge-card-state.is-pending { color: #a66a1f; }
.knowledge-card-state.is-draft { color: #456d9b; }
.knowledge-card-state.is-failed { color: #c43d3d; }
.knowledge-card-state.is-muted { color: var(--ws-text-secondary-color, #686b66); }

/* 窄窗口优先给表格留宽度；手机保留局部滚动，不裁掉操作。 */
@media (max-width: 1100px) {
  .ws-layout {
    flex-direction: column;
  }
  .ws-folders {
    width: 100%;
  }
  .ws-folders__list {
    flex-direction: row;
    flex-wrap: wrap;
  }
  .ws-folders__item {
    width: auto;
  }
}
</style>
