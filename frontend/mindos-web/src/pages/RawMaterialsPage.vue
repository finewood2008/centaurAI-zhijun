<script setup lang="ts">
// 原材料资料库：桌面端多级目录树筛选侧栏 + 高密表格（B2 FE-UI-011 / P14-06 目录树）
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronDown, ChevronRight, Eye, Folder, FolderPlus, Pencil, FolderInput, Play, Plus, Trash2, Upload, X } from 'lucide-vue-next'
import { api, type FolderNode, type UploadResult } from '@/services/api'
import { materialStatusMeta } from '@/shared/status'
import { formatDate, formatFileType } from '@/shared/format'
import { useToast } from '@/composables/useToast'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import IconButton from '@/components/ui/IconButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import { createSessionGate } from '@/composables/sessionGate'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const items = ref<UploadResult[]>([])
type DisplayMaterial = UploadResult & { transientUpload?: boolean }
const transientUploads = ref<DisplayMaterial[]>([])
const displayItems = computed<DisplayMaterial[]>(() => [...transientUploads.value, ...items.value])
const loading = ref(true)
const error = ref('')
const type = ref('')
// 支持从首页失败任务等入口带筛选参数进入（/materials?status=failed）
const status = ref(typeof route.query.status === 'string' ? route.query.status : '')
const keyword = ref(typeof route.query.keyword === 'string' ? route.query.keyword : '')
const tag = ref('')
const importInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | null = null
const materialLoadGate = createSessionGate()

function hasActiveMaterial(items: UploadResult[]) {
  return items.some((item) => item.status === 'uploaded' || item.status === 'queued' || item.status === 'processing')
}

const knowledgeCardStateMeta: Record<NonNullable<UploadResult['knowledgeCard']>['state'], { label: string; className: string }> = {
  waiting: { label: '待处理', className: 'is-muted' },
  generating: { label: '正在生成草稿', className: 'is-pending' },
  draft: { label: '草稿待确认', className: 'is-draft' },
  confirming: { label: '确认中', className: 'is-pending' },
  indexing: { label: '已确认，索引中', className: 'is-pending' },
  available: { label: '已确认，可检索', className: 'is-ready' },
  failed: { label: '已确认，索引失败', className: 'is-failed' },
  recycled: { label: '卡片在回收站', className: 'is-muted' },
  unknown: { label: '已确认，待修复', className: 'is-failed' },
}

function knowledgeCardMeta(item: UploadResult) {
  return knowledgeCardStateMeta[item.knowledgeCard?.state ?? 'waiting']
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

function folderDisplayName(id?: number | null): string {
  if (id == null) return '未分类'
  return nameById.value.get(id) ?? '未分类'
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
  try {
    const res = await api.listFolderNodes('RAW')
    folderNodes.value = res.items
    // 默认展开全部包含子节点的目录
    const parentIds = new Set(res.items.map((n) => n.parentId).filter((p): p is number => p !== null))
    expandedIds.value = new Set(res.items.filter((n) => parentIds.has(n.id)).map((n) => n.id))
  } catch (e) {
    error.value = e instanceof Error ? e.message : '目录加载失败'
  }
}

async function loadMaterials() {
  const requestSession = materialLoadGate.next()
  loading.value = true
  error.value = ''
  try {
    const response = await api.listMaterials({
      type: type.value,
      status: status.value,
      keyword: keyword.value.trim(),
      // P14-06：选中目录时按子树筛选（包含全部后代）
      folderId: selectedFolderId.value ?? undefined,
      tag: tag.value.trim(),
    })
    if (!materialLoadGate.isCurrent(requestSession)) return
    items.value = response.items
    if (hasActiveMaterial(items.value) && refreshTimer === null) {
      refreshTimer = setInterval(loadMaterials, 1800)
    } else if (!hasActiveMaterial(items.value) && refreshTimer !== null) {
      clearInterval(refreshTimer)
      refreshTimer = null
    }
  } catch (e) {
    if (materialLoadGate.isCurrent(requestSession)) error.value = e instanceof Error ? e.message : '原材料加载失败'
  } finally {
    if (materialLoadGate.isCurrent(requestSession)) loading.value = false
  }
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
      }
      transientUploads.value.push(transient)
      try {
        await api.uploadFile(file, selectedFolderId.value ?? undefined)
        accepted += 1
      } catch (e) {
        failed += 1
        toast({ type: 'error', message: `${file.name} 导入失败：${e instanceof Error ? e.message : '未知错误'}` })
      } finally {
        transientUploads.value = transientUploads.value.filter((item) => item.materialId !== transient.materialId)
      }
    }
    await loadMaterials()
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

onMounted(async () => {
  await loadFolders()
  await loadMaterials()
})

onBeforeUnmount(() => {
  materialLoadGate.invalidate()
  if (refreshTimer !== null) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1>原材料</h1>
      <p>浏览与管理原始资料（原文件只读，不可编辑）。</p>
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

        <div v-if="loading" class="loading-state">正在加载原材料…</div>
        <ErrorState v-else-if="error" :message="error" retry-label="重试" @retry="loadMaterials" />
        <EmptyState
          v-else-if="!displayItems.length"
          title="暂无原材料"
          description="上传文档或图片后，它们会出现在这里。"
        >
          <template #action>
            <BaseButton variant="primary" size="sm" :loading="importing" @click="importInput?.click()">
              <Upload :size="14" aria-hidden="true" />导入资料
            </BaseButton>
          </template>
        </EmptyState>

        <div v-else class="ws-table">
          <div class="ws-table__head">共 {{ displayItems.length }} 项资料</div>
          <div class="ws-table__scroll">
            <table class="ws-table__grid">
              <thead>
                <tr>
                  <th>文件名</th>
                  <th>类型</th>
                  <th>文件夹</th>
                  <th>状态</th>
                  <th>知识卡片</th>
                  <th>导入时间</th>
                  <th class="ws-table__ops-col"></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in displayItems" :key="item.materialId" @click="!item.transientUpload && openMaterial(item)">
                  <td class="ws-table__name" :title="item.fileName">{{ item.fileName }}</td>
                  <td>{{ formatFileType(item.fileType) }}</td>
                  <td>{{ folderDisplayName(item.folderId) }}</td>
                  <td><StatusBadge :meta="materialStatusMeta(item.status)" /></td>
                  <td>
                    <span
                      class="knowledge-card-state"
                      :class="knowledgeCardMeta(item).className"
                      :title="item.knowledgeCard?.errorCode || knowledgeCardMeta(item).label"
                    >{{ knowledgeCardMeta(item).label }}</span>
                  </td>
                  <td>{{ formatDate(item.createdAt) }}</td>
                  <td class="ws-table__ops">
                    <IconButton v-if="!item.transientUpload" label="查看详情" @click.stop="openMaterial(item)">
                      <Eye :size="16" aria-hidden="true" />
                    </IconButton>
                    <IconButton v-if="!item.transientUpload" label="移动文件夹" @click.stop="openMove(item)">
                      <FolderInput :size="16" aria-hidden="true" />
                    </IconButton>
                    <IconButton
                      v-if="item.status === 'queued' && item.errorCode === 'service_interrupted'"
                      label="继续处理"
                      @click.stop="resumeProcessing(item)"
                    >
                      <Play :size="16" aria-hidden="true" />
                    </IconButton>
                    <IconButton
                      v-if="item.status === 'uploaded' || item.status === 'queued' || item.status === 'failed'"
                      label="移出队列"
                      @click.stop="removeFromQueue(item)"
                    >
                      <X :size="16" aria-hidden="true" />
                    </IconButton>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

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
  width: 220px;
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

.ws-table__head {
  padding: 12px 16px;
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}

/* 复杂表格小屏可横向滚动 */
.ws-table__scroll {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.ws-table__grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.ws-table__grid th {
  text-align: left;
  padding: 10px 16px;
  color: var(--ws-text-secondary-color, #686b66);
  font-weight: 600;
  background: var(--ws-surface-2, #fbf8f1);
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
  white-space: nowrap;
}
.ws-table__grid td {
  padding: 10px 16px;
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

.ws-table__name {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}

.ws-table__ops-col {
  width: 120px;
}

.ws-table__ops {
  white-space: nowrap;
}

.knowledge-card-state {
  display: inline-block;
  white-space: nowrap;
  color: var(--ws-text-secondary-color, #686b66);
}
.knowledge-card-state.is-ready { color: #16803c; }
.knowledge-card-state.is-pending { color: #a66a1f; }
.knowledge-card-state.is-draft { color: #456d9b; }
.knowledge-card-state.is-failed { color: #c43d3d; }
.knowledge-card-state.is-muted { color: var(--ws-text-secondary-color, #686b66); }

/* <900px：侧栏移到内容区上方，保持可用 */
@media (max-width: 900px) {
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
