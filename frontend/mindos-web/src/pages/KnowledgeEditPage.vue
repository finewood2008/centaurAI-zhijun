<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch, computed } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { api, type FolderNode, type KnowledgeSourceRef, type RelatedRecommendation, type RelatedResult } from '@/services/api'
import { createRelatedLoader } from '@/composables/useRelatedLoader'
import { createCardUpdatePoller } from '@/composables/useCardUpdatePoller'
import { useToast } from '@/composables/useToast'
import { canManageKnowledgeSources } from '@/shared/sourceManagement'
import { stripCardFrontmatter, stripCardHeading } from '@/shared/cardContent'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import LifecycleDangerPanel from '@/components/lifecycle/LifecycleDangerPanel.vue'
const route = useRoute(); const router = useRouter()
const toast = useToast()
const title = ref(''); const content = ref(''); const tags = ref<string[]>([])
const loading = ref(true); const saving = ref(false); const error = ref(''); const saved = ref(false)
const sources = ref<KnowledgeSourceRef[]>([])
const newTag = ref(''); const tagError = ref('')
const related = ref<RelatedRecommendation[]>([]); const relatedNote = ref(''); const relatedLoading = ref(false)
const cardArchived = ref(false); const cardMerged = ref(false); const cardRecycled = ref(false)
const cardApprovalState = ref<'draft' | 'confirming' | 'confirmed'>('draft')
const cardIndexState = ref<'none' | 'indexing' | 'indexed' | 'index_failed'>('none')
const pendingUpdateState = ref<'indexing' | 'recovering' | 'index_failed' | ''>('')
const revision = ref('')
const workingDraftRevision = ref('')
const workingDraftBaseRevision = ref('')
const creatingEditDraft = ref(false)
const confirmingInitial = ref(false)
const confirmingUpdate = ref(false)
const retryingUpdate = ref(false)
const showEditDraftConfirm = ref(false)
const isNew = () => route.name === 'knowledge-new'
const updatePoller = createCardUpdatePoller({
  fetch: (knowledgeId) => api.getKnowledge(knowledgeId),
  onResult: (knowledgeId, item) => {
    if (String(route.params.knowledgeId) !== knowledgeId) return
    pendingUpdateState.value = item.pendingUpdate?.state ?? ''
    if (!item.pendingUpdate || item.pendingUpdate.state === 'index_failed') loadCard(knowledgeId)
  },
  onTimeout: (knowledgeId) => {
    if (String(route.params.knowledgeId) === knowledgeId) {
      toast({ type: 'info', message: '新版本仍在后台处理，可稍后刷新查看结果' })
    }
  },
})

// ---- P14-07：KNOWLEDGE 目录选择（'' = 知识根目录 Resources）----
const folderNodes = ref<FolderNode[]>([])
const folderLoading = ref(false)
const folderId = ref<number | ''>('')

// 拍平的 KNOWLEDGE 目录树（depth 供缩进），供选择器使用
const flatFolders = computed(() => {
  const byParent = new Map<number | null, FolderNode[]>()
  for (const n of folderNodes.value) {
    const arr = byParent.get(n.parentId) ?? []
    arr.push(n)
    byParent.set(n.parentId, arr)
  }
  for (const arr of byParent.values()) arr.sort((a, b) => a.name.localeCompare(b.name, 'zh'))
  const out: Array<FolderNode & { depth: number }> = []
  const walk = (parentId: number | null, depth: number) => {
    for (const n of byParent.get(parentId) ?? []) {
      out.push({ ...n, depth })
      if (byParent.get(n.id)?.length) walk(n.id, depth + 1)
    }
  }
  walk(null, 0)
  return out
})

// '' 选择映射为 Resources 根节点 ID（不存在时返回 null，写入路径按服务端缺省处理）
function resolveFolderParam(): number | null {
  if (folderId.value !== '') return folderId.value
  return folderNodes.value.find((n) => n.parentId === null && n.name === 'Resources')?.id ?? null
}

async function loadFolders() {
  folderLoading.value = true
  try {
    const res = await api.listFolderNodes('KNOWLEDGE')
    folderNodes.value = res.items
  } catch {
    folderNodes.value = []
  } finally {
    folderLoading.value = false
  }
}

// 路由对应卡片 id：新建路由无 :knowledgeId 参数，统一归一为 'new'
function currentId() {
  return isNew() ? 'new' : String(route.params.knowledgeId)
}

const editingWorkingDraft = computed(() => Boolean(workingDraftRevision.value))
const locked = computed(() => cardArchived.value || cardMerged.value || cardRecycled.value || (cardApprovalState.value !== 'draft' && !editingWorkingDraft.value))
const canManageSources = computed(() => canManageKnowledgeSources(isNew(), locked.value))

// ---- 未保存离开保护：初始快照 + 脏状态 + 路由守卫 + beforeunload ----
// 正文保存与来源保存相互独立：来源变更单独追踪（sourceKeysSnapshot 仅在加载/来源保存后刷新，
// 避免点一次「保存」正文就把未保存的来源修改吞掉）。
const snapshot = ref({ title: '', content: '', tags: [] as string[], folderId: '' as number | '' })
const sourceKeysSnapshot = ref('')

const sourceKeys = computed(() =>
  sources.value.map((s) => `${s.sourceType}:${s.id}`).join('|'),
)

function refreshSourceKeysSnapshot() {
  sourceKeysSnapshot.value = sourceKeys.value
}

const dirty = computed(() => {
  const s = snapshot.value
  if (title.value !== s.title) return true
  if (content.value !== s.content) return true
  if (folderId.value !== s.folderId) return true
  if (JSON.stringify(tags.value) !== JSON.stringify(s.tags)) return true
  // P15-01：来源修改未保存（来源保存独立于正文保存）
  if (sourceKeysSnapshot.value !== sourceKeys.value) return true
  return false
})

function takeSnapshot() {
  snapshot.value = { title: title.value, content: content.value, tags: [...tags.value], folderId: folderId.value }
}

// 保存提示仅在“已保存且当前无未保存修改”时显示，避免编辑后仍残留“已保存”
const showSaved = computed(() => saved.value && !dirty.value)

// 挂起导航，等待用户在确认弹窗中决定
let leaveResolve: ((ok: boolean) => void) | null = null
const showLeaveConfirm = ref(false)

onBeforeRouteLeave(() => {
  if (!dirty.value || saving.value) return true
  return new Promise<boolean>((resolve) => {
    leaveResolve = resolve
    showLeaveConfirm.value = true
  })
})

function confirmLeave() {
  showLeaveConfirm.value = false
  leaveResolve?.(true)
  leaveResolve = null
}

function cancelLeave() {
  showLeaveConfirm.value = false
  leaveResolve?.(false)
  leaveResolve = null
}

// 浏览器关闭 / 刷新保护
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', onBeforeUnload))
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
  // 组件卸载时使在途关联请求全部失效，防止结果写回已离开的页面
  relatedLoader.invalidate()
  updatePoller.stop()
})

function resetEditor() {
  title.value = '未命名知识卡片'
  content.value = ''
  tags.value = []
  sources.value = []
  related.value = []
  relatedNote.value = ''
  cardArchived.value = false
  cardMerged.value = false
  cardRecycled.value = false
  cardApprovalState.value = 'draft'
  cardIndexState.value = 'none'
  pendingUpdateState.value = ''
  workingDraftRevision.value = ''
  workingDraftBaseRevision.value = ''
  folderId.value = ''
  sourceKeysSnapshot.value = ''
}

async function loadCard(id: string) {
  loading.value = true
  error.value = ''
  saved.value = false
  if (id === 'new') { resetEditor(); takeSnapshot(); loading.value = false; return }
  try {
    const item = await api.getKnowledge(id)
    workingDraftRevision.value = ''
    workingDraftBaseRevision.value = ''
    title.value = item.title
    content.value = stripCardHeading(stripCardFrontmatter(item.content))
      // 兼容此前「引用摘要创建卡片」写入的纯模板标题；它不是用户正文，保存时
      // 会自然移除，避免成为后续检索语料的一部分。
      .replace(/^## 资料摘要（待编辑草稿）\s*\n+/u, '')
      .trim()
    tags.value = item.tags || []
    sources.value = item.sources
    cardArchived.value = item.isArchived
    cardMerged.value = item.isMerged
    cardRecycled.value = Boolean(item.isRecycled)
    cardApprovalState.value = item.approvalState ?? 'draft'
    cardIndexState.value = item.indexState ?? 'none'
    pendingUpdateState.value = item.pendingUpdate?.state ?? ''
    revision.value = item.revision || ''
    // P14-07：目录选择器初始化为卡片当前目录
    folderId.value = item.folderId ?? ''
    refreshSourceKeysSnapshot()
    takeSnapshot()
    if (item.editDraft?.exists && !item.pendingUpdate) await loadWorkingDraft(id)
    if (item.pendingUpdate && item.pendingUpdate.state !== 'index_failed') updatePoller.start(id)
    loadRelated(id)
  } catch (e) { error.value = e instanceof Error ? e.message : '知识档案加载失败' }
  finally { loading.value = false }
}

async function loadWorkingDraft(id: string) {
  const draft = await api.getKnowledgeEditDraft(id)
  workingDraftRevision.value = draft.draftRevision
  workingDraftBaseRevision.value = draft.baseRevision
  title.value = draft.title
  content.value = draft.content
  tags.value = draft.tags || []
  folderId.value = draft.folderId ?? ''
  // Source labels are presentation-only; retain existing labels for matching refs.
  const known = new Map(sources.value.map((s) => [`${s.sourceType}:${s.id}`, s]))
  sources.value = draft.sourceRefs.map((s) => known.get(`${s.sourceType}:${s.id}`) ?? {
    ...s, title: s.id, archived: false,
  })
  refreshSourceKeysSnapshot()
  takeSnapshot()
}

// Vue Router 会复用同一路由组件；参数变化时重新加载知识卡片与关联内容。
watch(
  () => route.fullPath,
  () => {
    loadCard(currentId())
  },
)

onMounted(() => {
  loadCard(currentId())
  loadFolders()
})

async function save(): Promise<boolean> {
  if (!title.value.trim() || saving.value || locked.value) return false
  saving.value = true; error.value = ''; saved.value = false
  try {
    if (editingWorkingDraft.value) {
      const result = await api.saveKnowledgeEditDraft(String(route.params.knowledgeId), {
        expectedDraftRevision: workingDraftRevision.value, title: title.value, content: content.value,
        tags: tags.value, folderId: resolveFolderParam(),
        sourceRefs: sources.value.map((s) => ({ sourceType: s.sourceType, id: s.id })),
      })
      workingDraftRevision.value = result.draftRevision
    } else {
      const result = isNew()
        ? await api.createKnowledge(title.value, content.value, tags.value, resolveFolderParam())
        : await api.updateKnowledge(String(route.params.knowledgeId), title.value, content.value, tags.value, resolveFolderParam(), revision.value)
      revision.value = result.item.revision || revision.value
      if (isNew()) router.replace(`/knowledge/${result.item.knowledgeId}`)
    }
    saved.value = true
    takeSnapshot()
    toast({ type: 'success', message: '已保存' })
    return true
  } catch (e) {
    error.value = e instanceof Error ? e.message : '保存失败'
    return false
  } finally { saving.value = false }
}

async function beginEditDraft() {
  if (isNew() || creatingEditDraft.value || cardApprovalState.value !== 'confirmed') return
  creatingEditDraft.value = true
  error.value = ''
  try {
    await api.beginKnowledgeEditDraft(String(route.params.knowledgeId), revision.value)
    await loadWorkingDraft(String(route.params.knowledgeId))
    toast({ type: 'info', message: '已创建修改草稿，当前已确认版本仍可检索' })
  } catch (e) {
    error.value = e instanceof Error ? e.message : '转为草稿失败'
  } finally {
    creatingEditDraft.value = false
  }
}

async function confirmUpdate() {
  if (isNew() || confirmingUpdate.value || !editingWorkingDraft.value) return
  // Always persist the complete working-copy payload first. `dirty` is a UI
  // hint, not a publication guarantee, and must not decide what gets confirmed.
  if (!(await save())) return
  confirmingUpdate.value = true
  error.value = ''
  try {
    const result = await api.confirmKnowledgeEditDraft(
      String(route.params.knowledgeId), workingDraftRevision.value, crypto.randomUUID(),
    )
    toast({ type: 'success', message: '已确认更新，旧版本将保持可用直到新索引切换完成' })
    await loadCard(String(route.params.knowledgeId))
    updatePoller.start(String(route.params.knowledgeId))
  } catch (e) {
    error.value = e instanceof Error ? e.message : '确认更新失败'
  } finally {
    confirmingUpdate.value = false
  }
}

async function confirmInitialDraft() {
  if (isNew() || confirmingInitial.value || editingWorkingDraft.value || cardApprovalState.value !== 'draft') return
  if (!(await save())) return
  confirmingInitial.value = true
  error.value = ''
  try {
    await api.confirmKnowledge(String(route.params.knowledgeId), revision.value, crypto.randomUUID())
    toast({ type: 'success', message: '卡片已确认，正在建立索引' })
    await loadCard(String(route.params.knowledgeId))
    updatePoller.start(String(route.params.knowledgeId))
  } catch (e) {
    error.value = e instanceof Error ? e.message : '确认卡片失败'
  } finally {
    confirmingInitial.value = false
  }
}

async function retryPendingUpdate() {
  if (retryingUpdate.value || pendingUpdateState.value !== 'index_failed') return
  retryingUpdate.value = true
  error.value = ''
  try {
    await api.retryKnowledgeEditDraft(String(route.params.knowledgeId))
    toast({ type: 'info', message: '已重新提交新版本索引，当前版本继续可用' })
    await loadCard(String(route.params.knowledgeId))
  } catch (e) {
    error.value = e instanceof Error ? e.message : '重新建立索引失败'
  } finally {
    retryingUpdate.value = false
  }
}

function onLifecycleCompleted(action: 'recycle' | 'purge' | 'unrecycle') {
  if (action === 'unrecycle') {
    loadCard(String(route.params.knowledgeId))
    toast({ type: 'success', message: '卡片已恢复' })
    return
  }
  toast({ type: 'success', message: action === 'purge' ? '卡片已永久清除' : '卡片已移至回收站' })
  router.replace('/knowledge')
}

async function addTag() {
  if (!newTag.value.trim() || locked.value) return
  if (isNew() || editingWorkingDraft.value) { tags.value.push(newTag.value.trim()); newTag.value = ''; return }
  tagError.value = ''
  try {
    const result = await api.setKnowledgeTags(String(route.params.knowledgeId), [newTag.value.trim()], 'add')
    tags.value = result.tags; newTag.value = ''
    loadRelated(String(route.params.knowledgeId))
  } catch (e) { tagError.value = e instanceof Error ? e.message : '添加标签失败' }
}

async function removeTag(tag: string) {
  if (locked.value) return
  if (isNew() || editingWorkingDraft.value) { tags.value = tags.value.filter(t => t !== tag); return }
  tagError.value = ''
  try {
    const result = await api.setKnowledgeTags(String(route.params.knowledgeId), [tag], 'remove')
    tags.value = result.tags
    loadRelated(String(route.params.knowledgeId))
  } catch (e) { tagError.value = e instanceof Error ? e.message : '移除标签失败' }
}

function openRelated(item: RelatedRecommendation) {
  router.push(item.sourceType === 'material' ? `/materials/${item.id}` : `/knowledge/${item.id}`)
}

// P14-10：来源按类型跳转（material → /materials/{id}，knowledge → /knowledge/{id}）
function openSource(source: KnowledgeSourceRef) {
  router.push(source.sourceType === 'knowledge' ? `/knowledge/${source.id}` : `/materials/${source.id}`)
}

// P14-09：关联加载防串台。快速切换卡片时，旧卡片的延迟关联请求不得覆盖新卡片结果：
// load() 每次开启新代次并清空旧结果，返回时校验“代次仍最新”且“当前路由目标仍为 id”才写回。
const relatedLoader = createRelatedLoader<RelatedResult>({
  fetch: (id) => api.getKnowledgeRelated(id),
  isCurrentTarget: (id) => currentId() === id,
  onResult: (result) => {
    related.value = result.items
    relatedNote.value = result.note
  },
  onEmpty: () => {
    related.value = []
    relatedNote.value = ''
  },
  onLoading: (loading) => {
    relatedLoading.value = loading
  },
})

async function loadRelated(id: string) {
  await relatedLoader.load(id)
}

// ---- P15-01：来源管理（添加 / 移除 / 替换；独立来源保存，与正文保存解耦）----
interface SourceOption {
  id: string
  kind: 'material' | 'knowledge'
  title: string
  meta: string
}

const sourceOptions = ref<SourceOption[]>([])
const sourcePickerOpen = ref(false)
const sourceLoading = ref(false)
const sourceError = ref('')
const sourcesSaved = ref(false)
const savingSources = ref(false)
// 替换模式下记录被替换来源的 key（type:id），null = 追加模式
const replaceKey = ref<string | null>(null)

function sourceKey(s: { sourceType: string; id: string }) {
  return `${s.sourceType}:${s.id}`
}

function markSourcesChanged() {
  sourcesSaved.value = false
  sourceError.value = ''
}

// 打开来源选择器：仅展示可用原材料（available）与活跃知识卡片；已归档来源可被替换但不可再选
async function openSourcePicker(mode: 'add' | 'replace', target?: KnowledgeSourceRef) {
  if (!canManageSources.value) return
  replaceKey.value = mode === 'replace' && target ? sourceKey(target) : null
  sourcePickerOpen.value = true
  sourceLoading.value = true
  sourceError.value = ''
  try {
    const [mres, kres] = await Promise.all([
      api.listMaterials(),
      api.listKnowledge(),
    ])
    const existing = new Set(sources.value.map(sourceKey))
    const currentId = String(route.params.knowledgeId)
    const items: SourceOption[] = []
    for (const m of mres.items) {
      if (m.status !== 'available') continue
      if (existing.has(`material:${m.materialId}`)) continue
      items.push({ id: m.materialId, kind: 'material', title: m.fileName, meta: '资料' })
    }
    for (const k of kres.items) {
      // 卡片不能引用自身（后端校验 400），选择器直接排除
      if (k.knowledgeId === currentId) continue
      if (existing.has(`knowledge:${k.knowledgeId}`)) continue
      items.push({ id: k.knowledgeId, kind: 'knowledge', title: k.title, meta: '卡片' })
    }
    sourceOptions.value = items
  } catch (e) {
    sourceError.value = e instanceof Error ? e.message : '来源加载失败'
  } finally {
    sourceLoading.value = false
  }
}

function pickSource(item: SourceOption) {
  const ref = { sourceType: item.kind, id: item.id, title: item.title, fileName: item.title, archived: false } as KnowledgeSourceRef
  if (replaceKey.value) {
    sources.value = sources.value.map((s) => (sourceKey(s) === replaceKey.value ? ref : s))
  } else {
    sources.value = [...sources.value, ref]
  }
  replaceKey.value = null
  sourcePickerOpen.value = false
  markSourcesChanged()
}

function removeSource(source: KnowledgeSourceRef) {
  sources.value = sources.value.filter((s) => sourceKey(s) !== sourceKey(source))
  replaceKey.value = null
  markSourcesChanged()
}

async function saveSources() {
  if (!canManageSources.value || savingSources.value) return
  if (editingWorkingDraft.value) {
    await save()
    return
  }
  const kid = String(route.params.knowledgeId)
  savingSources.value = true
  sourceError.value = ''
  try {
    const result = await api.putKnowledgeSources(kid, {
      sourceRefs: sources.value.map((s) => ({ sourceType: s.sourceType, id: s.id })),
      expectedRevision: revision.value || undefined,
    })
    sources.value = result.sourceRefs
    revision.value = result.revision || revision.value
    sourcesSaved.value = true
    refreshSourceKeysSnapshot()
    toast({ type: 'success', message: '来源已保存' })
  } catch (e) {
    sourceError.value = e instanceof Error ? e.message : '来源保存失败'
  } finally {
    savingSources.value = false
  }
}
</script>
<template>
  <div class="page">
    <div class="page-head">
      <button class="back-btn" type="button" @click="router.push('/knowledge')">返回知识档案</button>
      <h1>{{ isNew() ? '新建知识卡片' : '编辑知识卡片' }}</h1>
      <p>知识档案可编辑；原材料始终保持只读。</p>
    </div>
    <div v-if="loading" class="loading-state">正在加载…</div>
    <template v-else>
      <div v-if="locked" class="lock-notice">
        <span>{{ cardMerged ? '该卡片已合并，不能编辑。' : cardRecycled ? '该卡片已在回收站，请先恢复后再编辑。' : pendingUpdateState === 'indexing' || pendingUpdateState === 'recovering' ? `新版本${pendingUpdateState === 'recovering' ? '正在恢复' : '正在建立索引'}，当前已确认版本仍可参与检索。` : pendingUpdateState === 'index_failed' ? '新版本索引失败，当前已确认版本仍可参与检索。' : cardApprovalState === 'confirmed' ? `该卡片已确认；${cardIndexState === 'indexed' ? '当前版本可参与检索。' : cardIndexState === 'index_failed' ? '索引失败，暂不参与检索。' : '索引正在排队或执行。'}` : '该历史卡片当前不可编辑。' }}</span>
        <button v-if="cardApprovalState === 'confirmed' && !pendingUpdateState && !cardArchived && !cardMerged && !cardRecycled" class="secondary-btn sm" type="button" :disabled="creatingEditDraft" @click="showEditDraftConfirm = true">{{ creatingEditDraft ? '创建中…' : '编辑' }}</button>
        <button v-if="pendingUpdateState === 'index_failed'" class="secondary-btn sm" type="button" :disabled="retryingUpdate" @click="retryPendingUpdate">{{ retryingUpdate ? '重试中…' : '重新建立索引' }}</button>
      </div>

      <div class="ws-editor-layout">
        <!-- 编辑主区 -->
        <div class="ws-editor-main">
          <form id="knowledge-editor-form" class="knowledge-editor" @submit.prevent="save">
            <label>标题<input v-model="title" maxlength="200" required :disabled="locked"></label>
            <label>正文<textarea v-model="content" rows="20" placeholder="输入知识卡片正文" :disabled="locked"></textarea></label>
          </form>
        </div>

        <!-- 右侧属性栏：来源 / 标签 / 相关内容 -->
        <aside class="ws-editor-side">
          <!-- P15-01：来源资料（管理：添加 / 移除 / 替换；独立保存，与正文保存解耦） -->
          <section class="ws-side-panel">
            <div class="ws-side-panel__title">来源资料</div>
            <div class="ws-side-panel__body">
              <div v-if="!sources.length" class="tag-empty">暂无来源</div>
              <div v-else class="source-list">
                <div
                  v-for="source in sources"
                  :key="sourceKey(source)"
                  class="source-row"
                >
                  <button class="source-main" type="button" :title="source.title" @click="openSource(source)">
                    <span class="source-kind">{{ source.sourceType === 'material' ? '资料' : '卡片' }}</span>
                    <span class="source-name">{{ source.title }}</span>
                  </button>
                  <span class="source-actions">
                    <button class="source-op" type="button" :disabled="!canManageSources" @click="openSourcePicker('replace', source)">替换</button>
                    <button class="source-op source-op--danger" type="button" :disabled="!canManageSources" @click="removeSource(source)">移除</button>
                  </span>
                </div>
              </div>

              <span v-if="sourcesSaved && !dirty" class="saved-text">来源已保存</span>
              <span v-else-if="!isNew() && sourceKeysSnapshot !== sourceKeys" class="dirty-text">来源有未保存修改</span>
              <span v-if="sourceError" class="error-text">{{ sourceError }}</span>

              <span v-if="isNew()" class="tag-empty">请先保存卡片后再维护来源</span>
              <button class="secondary-btn sm" type="button" :disabled="!canManageSources" @click="openSourcePicker('add')">+ 添加来源</button>

              <!-- 来源选择器：仅展示可用原材料与活跃知识卡片 -->
              <div v-if="sourcePickerOpen" class="source-picker">
                <div v-if="sourceLoading" class="tag-empty">正在加载可选来源…</div>
                <div v-else-if="!sourceOptions.length" class="tag-empty">暂无可添加的来源</div>
                <div v-else class="source-picker-list">
                  <button
                    v-for="item in sourceOptions"
                    :key="`${item.kind}:${item.id}`"
                    class="source-picker-item"
                    type="button"
                    @click="pickSource(item)"
                  >
                    <span class="source-kind">{{ item.meta }}</span>
                    <span class="source-name">{{ item.title }}</span>
                  </button>
                </div>
                <div class="source-picker-foot">
                  <button class="secondary-btn sm" type="button" @click="sourcePickerOpen = false">取消</button>
                </div>
              </div>

              <button
                v-if="!isNew() && sourceKeysSnapshot !== sourceKeys"
                class="primary-btn sm"
                type="button"
                :disabled="savingSources || !canManageSources"
                @click="saveSources"
              >{{ savingSources ? '来源保存中…' : '保存来源' }}</button>
            </div>
          </section>

          <section class="ws-side-panel">
            <div class="ws-side-panel__title">标签</div>
            <div class="ws-side-panel__body">
              <div class="tag-list">
                <span v-for="tag in tags" :key="tag" class="tag-chip">{{ tag }}<button class="tag-remove" type="button" :aria-label="`移除标签 ${tag}`" :disabled="locked" @click="removeTag(tag)">×</button></span>
                <span v-if="!tags.length" class="tag-empty">暂无标签</span>
              </div>
              <div class="tag-input-row">
                <input v-model="newTag" class="tag-input" type="text" placeholder="输入标签后回车添加" maxlength="64" :disabled="locked" @keyup.enter="addTag">
                <button class="secondary-btn sm" type="button" :disabled="!newTag.trim() || locked" @click="addTag">添加</button>
              </div>
              <span v-if="tagError" class="error-text">{{ tagError }}</span>
            </div>
          </section>

          <section class="ws-side-panel">
            <div class="ws-side-panel__title">目录</div>
            <div class="ws-side-panel__body">
              <select
                v-model="folderId"
                class="knowledge-folder-select"
                aria-label="知识目录"
                :disabled="locked || folderLoading"
              >
                <option :value="''">知识根目录（Resources）</option>
                <option v-for="node in flatFolders" :key="node.id" :value="node.id">{{ '　'.repeat(node.depth) }}{{ node.name }}</option>
              </select>
              <span v-if="folderLoading" class="tag-empty">正在加载目录…</span>
            </div>
          </section>

          <section v-if="!isNew()" class="ws-side-panel">
            <div class="ws-side-panel__title">相关内容</div>
            <div class="ws-side-panel__body">
              <div v-if="relatedLoading" class="loading-state">正在查找相关内容…</div>
              <div v-else-if="!related.length" class="empty-sub">{{ relatedNote || '暂无达到阈值的关联' }}</div>
              <div v-else class="related-list">
                <button v-for="item in related" :key="item.id" class="related-item" type="button" @click="openRelated(item)">
                  <span class="related-main">
                    <strong>{{ item.title }}</strong>
                    <span v-if="item.snippet" class="related-snippet">{{ item.snippet }}</span>
                    <span class="related-reasons">依据：{{ item.reasons.join('、') }}</span>
                  </span>
                  <span class="related-meta">
                    <span class="related-band" :class="`related-band--${item.scoreBand}`">{{ item.scoreBand === 'high' ? '高可信' : '一般相关' }}</span>
                    <span class="related-type">{{ item.sourceType === 'material' ? '原材料' : '知识卡片' }}</span>
                  </span>
                </button>
                <div v-if="relatedNote" class="related-note">{{ relatedNote }}</div>
              </div>
            </div>
          </section>
          <LifecycleDangerPanel
            v-if="!isNew()"
            target-type="knowledge"
            :target-id="String(route.params.knowledgeId)"
            :target-title="title"
            :recycled="cardRecycled"
            @completed="onLifecycleCompleted"
          />
        </aside>
      </div>

      <!-- 固定保存栏 -->
      <div class="ws-savebar">
        <span v-if="error" class="error-text">{{ error }}</span>
        <span v-if="showSaved" class="saved-text">已保存</span>
        <button class="primary-btn" type="submit" form="knowledge-editor-form" :disabled="saving || confirmingInitial || !title.trim() || locked">{{ saving ? '保存中…' : '保存' }}</button>
        <button v-if="!isNew() && cardApprovalState === 'draft' && !editingWorkingDraft" class="primary-btn" type="button" :disabled="saving || confirmingInitial || !title.trim()" @click="confirmInitialDraft">{{ confirmingInitial ? '确认中…' : '确认' }}</button>
        <button v-if="!isNew() && editingWorkingDraft" class="primary-btn" type="button" :disabled="saving || confirmingUpdate || !title.trim()" @click="confirmUpdate">{{ confirmingUpdate ? '确认中…' : '确认更新' }}</button>
      </div>
    </template>

    <ConfirmDialog
      :open="showEditDraftConfirm"
      title="编辑已确认卡片"
      message="将创建持久化的修改草稿。当前已确认版本会继续参与检索和问知君，直到确认更新的新版本索引成功。"
      confirm-text="创建草稿"
      :loading="creatingEditDraft"
      @confirm="showEditDraftConfirm = false; beginEditDraft()"
      @cancel="showEditDraftConfirm = false"
    />

    <ConfirmDialog
      :open="showLeaveConfirm"
      title="离开前保存"
      message="当前有未保存的修改，离开将丢失这些改动。确定要离开吗？"
      confirm-text="离开"
      danger
      @confirm="confirmLeave"
      @cancel="cancelLeave"
    />
  </div>
</template>

<style scoped>
/* 编辑主区 + 右侧属性栏 */
.ws-editor-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 16px;
  align-items: start;
}

.ws-editor-main {
  min-width: 0;
}

.ws-editor-main .knowledge-editor {
  max-width: none;
}

.ws-editor-side {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.ws-side-panel {
  padding: 12px 14px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
}

.ws-side-panel__title {
  margin: 0 0 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-secondary-color, #909399);
  letter-spacing: 0.04em;
}

.ws-side-panel__body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* P14-07：目录选择器 */
.knowledge-folder-select {
  width: 100%;
  height: 32px;
  padding: 0 8px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-body-bg, #fff);
  color: var(--ws-text-primary-color, #303133);
  font-family: inherit;
  font-size: 13px;
}
.knowledge-folder-select:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #1b99ff);
}
.knowledge-folder-select:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* P15-01：来源管理（类型 / 名称 / 归档状态 + 添加 / 移除 / 替换 + 独立保存） */
.source-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.source-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius, 6px);
}

.source-main {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
  border: 0;
  background: none;
  cursor: pointer;
  text-align: left;
  font: inherit;
  padding: 0;
}

.source-kind {
  flex: none;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--ws-border-color-2, #f0f2f5);
  color: var(--ws-text-secondary-color, #909399);
  font-size: 11px;
}

.source-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--ws-text-primary-color, #303133);
}


.source-badge {
  flex: none;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  color: #b88230;
  background: #fdf6ec;
}

.source-actions {
  flex: none;
  display: flex;
  gap: 4px;
}

.source-op {
  border: 0;
  background: none;
  padding: 2px 4px;
  color: var(--accent, #1b99ff);
  font-size: 12px;
  cursor: pointer;
}

.source-op--danger {
  color: #e13b3b;
}

.source-op:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.source-picker {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
  border: 1px dashed var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius, 6px);
}

.source-picker-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 180px;
  overflow-y: auto;
}

.source-picker-item {
  display: flex;
  align-items: center;
  gap: 6px;
  border: 0;
  background: none;
  padding: 5px 6px;
  border-radius: 4px;
  cursor: pointer;
  text-align: left;
  font: inherit;
}

.source-picker-item:hover {
  background: var(--ws-border-color-2, #f0f2f5);
}

.source-picker-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
}

.dirty-text {
  font-size: 12px;
  color: #b88230;
}

/* 固定保存栏 */
.ws-savebar {
  position: sticky;
  bottom: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 16px;
  padding: 12px 16px;
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  box-shadow: var(--ws-shadow-sm, 0 1px 2px rgba(0, 0, 0, 0.06));
}

/* 小屏：纵向布局 */
@media (max-width: 900px) {
  .ws-editor-layout {
    grid-template-columns: 1fr;
  }
}
</style>
