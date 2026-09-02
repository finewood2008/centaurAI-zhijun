<script setup lang="ts">
// 知识成品列表：搜索 / 标签与目录管理。
// P14-07：知识成品多级目录树——左侧 KNOWLEDGE 目录筛选侧栏 + 卡片移动 / 目录管理
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ChevronDown, ChevronRight, Folder, FolderInput, FolderPlus, Pencil, Plus, Trash2 } from 'lucide-vue-next'
import { api, type FolderNode, type KnowledgeCard } from '@/services/api'
import { formatDate } from '@/shared/format'
import { useToast } from '@/composables/useToast'
import BaseButton from '@/components/ui/BaseButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import IconButton from '@/components/ui/IconButton.vue'
import { createSessionGate } from '@/composables/sessionGate'
import { cardBodyPreview } from '@/shared/cardContent'

const router = useRouter()
const toast = useToast()
const items = ref<KnowledgeCard[]>([])
const query = ref('')
const tag = ref('')
const loading = ref(true)
const error = ref('')
const loadGate = createSessionGate()

// ---- P14-07：KNOWLEDGE 多级目录树（ID 驱动；null = 全部）----
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
    const res = await api.listFolderNodes('KNOWLEDGE')
    folderNodes.value = res.items
    // 默认展开全部包含子节点的目录
    const parentIds = new Set(res.items.map((n) => n.parentId).filter((p): p is number => p !== null))
    expandedIds.value = new Set(res.items.filter((n) => parentIds.has(n.id)).map((n) => n.id))
  } catch (e) {
    error.value = e instanceof Error ? e.message : '目录加载失败'
  }
}

async function load() {
  const requestSession = loadGate.next()
  loading.value = true
  error.value = ''
  try {
    const response = await api.listKnowledge({
      q: query.value.trim(),
      tag: tag.value.trim(),
      // P14-07：选中目录时按子树筛选（包含全部后代）
      folderId: selectedFolderId.value ?? undefined,
    })
    if (!loadGate.isCurrent(requestSession)) return
    items.value = response.items
  } catch (e) {
    if (loadGate.isCurrent(requestSession)) error.value = e instanceof Error ? e.message : '知识档案加载失败'
  } finally {
    if (loadGate.isCurrent(requestSession)) loading.value = false
  }
}

function selectFolder(id: number | null) {
  selectedFolderId.value = id
  load()
}

// ---- P14-07：移动知识卡片到目录（null = 知识根目录）----
const moveTarget = ref<KnowledgeCard | null>(null)
const moveFolderId = ref<number | ''>('')
const moving = ref(false)

function openMove(item: KnowledgeCard) {
  moveTarget.value = item
  moveFolderId.value = item.folderId ?? ''
}

async function confirmMove() {
  if (!moveTarget.value) return
  const card = moveTarget.value
  moveTarget.value = null
  moving.value = true
  try {
    await api.moveKnowledge(card.knowledgeId, moveFolderId.value === '' ? null : moveFolderId.value, card.revision)
    toast({ type: 'success', message: '已移动知识卡片' })
    if (selectedFolderId.value !== null) await load()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '移动失败' })
  } finally {
    moving.value = false
  }
}

// ---- P14-07：目录管理（创建 / 重命名 / 删除，规则与原材料目录一致）----
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
    await api.createFolderNode(name, createParentId.value === '' ? null : createParentId.value, 'KNOWLEDGE')
    showCreateFolder.value = false
    toast({ type: 'success', message: `已创建文件夹「${name}」` })
    await loadFolders()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '创建失败' })
  } finally {
    creating.value = false
  }
}

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
    if (selectedFolderId.value !== null) await load()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '重命名失败' })
  } finally {
    renaming.value = false
  }
}

const deleteTarget = ref<FolderNode | null>(null)
const deleteMode = ref<'root' | 'target'>('root')
const deleteTargetFolderId = ref<number | ''>('')
const deleting = ref(false)

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
    if (selectedFolderId.value !== null) await load()
    const notes: string[] = []
    if (result.movedMaterials > 0) notes.push(`${result.movedMaterials} 项资料迁移`)
    if (result.reparentedFolders > 0) notes.push(`${result.reparentedFolders} 个子目录迁移`)
    if (result.movedCards > 0) notes.push(`${result.movedCards} 张知识卡片迁移`)
    toast({ type: 'success', message: `已删除文件夹「${node.name}」${notes.length ? `（${notes.join('、')}）` : ''}` })
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '删除失败' })
  } finally {
    deleting.value = false
  }
}

function clearFilters() {
  query.value = ''
  tag.value = ''
  selectedFolderId.value = null
  load()
}

onMounted(async () => {
  await loadFolders()
  await load()
})
onBeforeUnmount(() => loadGate.invalidate())
</script>

<template>
  <div class="page">
    <div class="page-head knowledge-head">
      <div>
        <h1>知识档案</h1>
        <p>独立于原材料的可编辑个人知识卡片。</p>
      </div>
    </div>

    <div class="ws-layout">
      <!-- P14-07：KNOWLEDGE 多级目录树筛选侧栏（桌面） -->
      <aside class="ws-folders">
        <div class="ws-folders__head">
          <h2 class="ws-folders__title">知识目录</h2>
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
        <div class="filter-bar">
          <input v-model="query" class="filter-input" type="search" placeholder="搜索标题或摘要" @keyup.enter="load">
          <input v-model="tag" class="filter-input" type="search" placeholder="按标签筛选…" @keyup.enter="load">
          <BaseButton variant="secondary" size="sm" @click="load">搜索</BaseButton>
          <BaseButton variant="secondary" size="sm" @click="clearFilters">清除筛选</BaseButton>
        </div>

        <div v-if="loading" class="loading-state">正在加载知识档案…</div>
        <ErrorState v-else-if="error" :message="error" retry-label="重试" @retry="load" />
        <EmptyState
          v-else-if="!items.length"
          title="暂无知识档案"
          description="创建第一张知识卡片，开始整理自己的知识。"
        />

        <div v-else class="knowledge-list">
          <div v-for="item in items" :key="item.knowledgeId" class="knowledge-item">
            <button class="knowledge-item-main" type="button" @click="router.push(`/knowledge/${item.knowledgeId}`)">
              <strong class="knowledge-item-title-row">
                <span class="knowledge-item-title">{{ item.title }}</span>
                <span v-if="item.folderPath" class="knowledge-item-folder">{{ item.folderPath }}</span>
              </strong>
              <span>{{ cardBodyPreview(item.content) || '暂无正文' }}</span>
            </button>
            <span class="knowledge-item-meta">{{ item.sourceLabel }} · {{ formatDate(item.updatedAt) }}</span>
            <span class="knowledge-item-actions">
              <span class="knowledge-item-rag" :class="item.ragEligible ? 'is-ready' : 'is-pending'">
                {{ item.ragEligible ? '可检索' : item.indexState === 'index_failed' ? '索引失败' : '暂不可检索' }}
              </span>
              <IconButton label="移动目录" size="sm" @click.stop="openMove(item)">
                <FolderInput :size="15" aria-hidden="true" />
              </IconButton>
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- P14-07：移动知识卡片到目录 -->
    <div v-if="moveTarget" class="gov-modal-mask" @click.self="moveTarget = null">
      <div class="gov-modal" role="dialog" aria-modal="true" aria-label="移动知识卡片">
        <h3>移动知识卡片</h3>
        <p class="gov-modal-hint">将「{{ moveTarget.title }}」移动至目录：</p>
        <select v-model="moveFolderId" class="ws-input ws-folder-select" aria-label="目标目录">
          <option :value="''">知识根目录（Resources）</option>
          <option v-for="node in flatTree" :key="node.id" :value="node.id">{{ '　'.repeat(node.depth) }}{{ node.name }}</option>
        </select>
        <div class="gov-modal-actions">
          <BaseButton variant="secondary" size="sm" :disabled="moving" @click="moveTarget = null">取消</BaseButton>
          <BaseButton variant="primary" size="sm" :disabled="moving" :loading="moving" @click="confirmMove">确认移动</BaseButton>
        </div>
      </div>
    </div>

    <!-- P14-07：创建知识目录（支持选择父级目录） -->
    <div v-if="showCreateFolder" class="gov-modal-mask" @click.self="showCreateFolder = false">
      <div class="gov-modal" role="dialog" aria-modal="true" aria-label="创建知识目录">
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

    <!-- P14-07：重命名知识目录 -->
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

    <!-- P14-07：删除知识目录确认（必须选择迁移去向；归入该目录的知识卡片同步迁移） -->
    <ConfirmDialog
      :open="!!deleteTarget"
      title="删除文件夹"
      confirm-text="确认删除"
      :loading="deleting"
      @confirm="confirmDeleteFolder"
      @cancel="deleteTarget = null"
    >
      <p class="ws-dialog-note">「{{ deleteTarget?.name || '' }}」中的资料与子目录将被迁移（原文件不删除），归入该目录的知识卡片将同步迁移，请选择去向：</p>
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

/* 文件夹侧栏（样式与原材料页一致） */
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

/* 右侧内容区 */
.ws-main {
  flex: 1;
  min-width: 0;
}

/* 卡片列表（保留原有样式并补充目录徽标） */
.knowledge-item-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.knowledge-item-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.knowledge-item-folder {
  flex-shrink: 0;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--ws-surface-2, #fbf8f1);
  color: var(--ws-text-secondary-color, #686b66);
  font-size: 12px;
  font-weight: 500;
}
.knowledge-item-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.knowledge-item-rag {
  font-size: 12px;
  white-space: nowrap;
}

.knowledge-item-rag.is-ready { color: var(--ws-success-color, #16a34a); }
.knowledge-item-rag.is-pending { color: var(--ws-text-secondary-color, #6b7280); }

/* 弹窗 / 表单（复用原材料治理弹窗样式） */
.move-folder-input {
  width: 100%;
  margin-top: 8px;
}

.ws-dialog-note {
  margin: 0 0 4px;
  font-size: 13px;
  color: var(--ws-text-color, #3c403d);
}

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

.ws-folder-select {
  width: 100%;
  height: 34px;
  margin-top: 8px;
}

.ws-field-margin {
  margin-top: 12px;
}

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
