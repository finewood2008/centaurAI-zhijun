<script setup lang="ts">
import ProductImage from '@/components/ui/ProductImage.vue'
import PdfPreview from '@/components/PdfPreview.vue'
import { productPreview, releaseProductPreview, saveProductResource } from '@/services/productFiles'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { FileText } from 'lucide-vue-next'
import { api, type ContentPart, type DerivedRelations, type DerivedTagSuggestions, type DerivedEntities, type EmbeddedImage, type EntityExtraction, type EntityType, type MaterialAnalysis, type MaterialDetail, type MaterialDraftCard, type MaterialImpact, type RelatedRecommendation, type RelationExtraction, type TranscriptSegment, type UploadResult } from '@/services/api'
import { createSummaryPoller } from '@/composables/useSummaryPolling'
import { createAnalysisPoller } from '@/composables/useAnalysisPolling'
import { createGeneratedDraftPoller } from '@/composables/useGeneratedDraftRefresh'
import { createSessionGate } from '@/composables/sessionGate'
import { createEntityTagAdder } from '@/composables/useEntityTagAdd'
import { materialStatusLabel } from '@/shared/status'
import { formatDate, formatFileSize } from '@/shared/format'
import { applyVersionSourceAction } from '@/shared/versionSources'
import { useToast } from '@/composables/useToast'
import LifecycleDangerPanel from '@/components/lifecycle/LifecycleDangerPanel.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import RedactionPanel from '@/components/RedactionPanel.vue'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const detail = ref<MaterialDetail | null>(null)
const loading = ref(true)
const error = ref('')
const draft = ref<MaterialDraftCard | null>(null)
const draftTitle = ref('')
const draftContent = ref('')
const draftSnapshot = ref({ title: '', content: '', revision: '' })
const savingDraft = ref(false)
const confirmingDraft = ref(false)
const retryingIndex = ref(false)
const draftError = ref('')
const draftDirty = computed(() => Boolean(
  draft.value && !draft.value.confirmed && (
    draftTitle.value !== draftSnapshot.value.title
    || draftContent.value !== draftSnapshot.value.content
  ),
))
let leaveResolve: ((ok: boolean) => void) | null = null
const showLeaveConfirm = ref(false)

function takeDraftSnapshot() {
  draftSnapshot.value = {
    title: draftTitle.value,
    content: draftContent.value,
    revision: draft.value?.revision ?? '',
  }
}

onBeforeRouteLeave(() => {
  if (!draftDirty.value || savingDraft.value) return true
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

function onDraftBeforeUnload(event: BeforeUnloadEvent) {
  if (!draftDirty.value) return
  event.preventDefault()
  event.returnValue = ''
}
const newTag = ref('')
const tagError = ref('')
const related = ref<RelatedRecommendation[]>([])
const relatedNote = ref('')
const relatedLoading = ref(false)
const versions = ref<UploadResult[]>([])
const versionImpact = ref<MaterialImpact | null>(null)
const showVersionUpload = ref(false)
const versionFile = ref<File | null>(null)
const versionNote = ref('')
const uploadingVersion = ref(false)
const applyingVersionAction = ref('')
const versionActions = ref<Record<string, 'keep' | 'replace' | 'keepBoth' | 'manual'>>({})
let versionPollTimer: ReturnType<typeof setTimeout> | null = null
let cardIndexPollTimer: ReturnType<typeof setTimeout> | null = null
let cardIndexPollSession = 0
// P14-04/P0-1 智能分析（标签候选 / 实体抽取 / 关系三元组）：来自派生缓存，异步生成，轮询防串台。
// 候选与正式标签在 UI/API/数据上严格区分：候选仅有建议语义，用户逐条确认后才写入。
const analysis = ref<{ tagSuggestions: DerivedTagSuggestions; entities: DerivedEntities; relations: DerivedRelations } | null>(null)
const analysisLoading = ref(false)
const analysisError = ref('')
const analysisWaitExpired = ref(false)
const summaryWaitExpired = ref(false)
const draftWaitExpired = ref(false)
// 首次解析和手动重新解析均由四项派生产物驱动。只要其中任一项仍在后台生成，
// 就不能再次提交重解析，避免已发出的 LLM 任务后面再排入一轮重复任务。
const derivedGenerationPending = computed(() => {
  if (detail.value?.summary.status === 'pending' && !summaryWaitExpired.value) return true
  const current = analysis.value
  return Boolean(
    !analysisWaitExpired.value && current && (
      current.tagSuggestions.status === 'pending'
      || current.entities.status === 'pending'
      || current.relations.status === 'pending'
    ),
  )
})
const privacyReviewRequired = computed(() => detail.value?.privacyStatus?.state === 'review_required')
const privacyProcessing = computed(() => detail.value?.privacyStatus?.state === 'processing')
const privacyFailed = computed(() => detail.value?.privacyStatus?.state === 'failed')
const privacyBlocking = computed(() => privacyReviewRequired.value || privacyProcessing.value || privacyFailed.value)
const draftGenerationPending = computed(() => draft.value?.status === 'pending' && !draftWaitExpired.value)
const draftEditingBlocked = computed(() => privacyBlocking.value)
const summaryGenerationWaitExpired = computed(() => Boolean(
  summaryWaitExpired.value && detail.value?.summary.status === 'pending',
))
const analysisGenerationWaitExpired = computed(() => Boolean(
  analysisWaitExpired.value && analysis.value && (
    analysis.value.tagSuggestions.status === 'pending'
    || analysis.value.entities.status === 'pending'
    || analysis.value.relations.status === 'pending'
  ),
))
const draftGenerationWaitExpired = computed(() => Boolean(
  draftWaitExpired.value && draft.value?.status === 'pending',
))
const generationWaitExpired = computed(() => (
  summaryGenerationWaitExpired.value
  || analysisGenerationWaitExpired.value
  || draftGenerationWaitExpired.value
))
const draftRetryBlockedByEdits = computed(() => Boolean(
  draftGenerationWaitExpired.value
  && draftDirty.value
  && !summaryGenerationWaitExpired.value
  && !analysisGenerationWaitExpired.value,
))
const draftBadge = computed(() => {
  if (draft.value?.confirmed) return '已确认'
  if (privacyReviewRequired.value) return '待隐私复核'
  if (privacyProcessing.value || draft.value?.status === 'pending') return '生成中'
  if (privacyFailed.value || draft.value?.status === 'failed') return '生成失败'
  return '草稿'
})
// 正在确认的候选 suggestionId（同一时刻只允许一个确认请求在途）
const confirming = ref('')
// P14-04：正在把实体作为标签写入的 entityId（同一时刻只允许一个在途）
const addingEntityTag = ref('')
// 音频播放器：逐字稿跳转与当前片段高亮
const audioEl = ref<HTMLAudioElement | null>(null)
const currentTime = ref(0)
// P14-01：结构化内容部分按文档顺序（ordinal）排列，文本/表格混排渲染
const contentParts = computed<ContentPart[]>(() =>
  [...(detail.value?.contentParts ?? [])].sort((a, b) => a.ordinal - b.ordinal),
)
const parsingLabel = computed(() => {
  const parsing = detail.value?.parsing
  const method = parsing?.contentFormat === 'ocr' || detail.value?.fileType === 'image'
    ? 'OCR'
    : parsing?.contentFormat === 'transcript' || detail.value?.fileType === 'audio'
      ? '音频转写'
      : parsing?.contentFormat === 'mixed'
        ? '混合内容解析'
        : '正文解析'
  if (parsing?.status === 'ok') return `${method}成功`
  if (parsing?.status === 'empty') return `${method}完成，但未识别到可用文字`
  if (parsing?.status === 'failed') return `${method}失败`
  if (parsing?.status === 'pending') return `${method}处理中`
  if (detail.value?.status === 'available' && detail.value?.text) return `${method}成功`
  return '尚无解析结果'
})
// P14-02：内嵌图片（受控预览 + OCR）
const embeddedImages = computed<EmbeddedImage[]>(() => detail.value?.embeddedImages ?? [])
// 摘要轮询（初次进入仍在生成的材料）；手动刷新统一通过“重新解析”。
// 详情加载请求代次：防「资料 A 的详情请求延迟返回后覆盖已切换的资料 B」
const detailLoadGate = createSessionGate()
// 关联内容加载请求代次：防「A 的关联请求延迟返回后覆盖已切换的资料 B 的相关内容」
const relatedLoadGate = createSessionGate()
// P14-04 智能分析加载请求代次：防「A 的分析结果延迟返回后覆盖已切换的资料 B 的候选/实体」
const analysisLoadGate = createSessionGate()
const generatedDraftPoller = createGeneratedDraftPoller<MaterialDraftCard & { materialId?: string }>({
  fetch: (materialId) => api.getMaterialDraftCard(materialId),
  currentMaterialId: () => detail.value?.materialId ?? null,
  currentDraft: () => draft.value,
  isDirty: () => draftDirty.value,
  apply: (latest) => {
    draft.value = latest
    draftTitle.value = latest.title
    draftContent.value = latest.content
    draftWaitExpired.value = false
    takeDraftSnapshot()
  },
  onTimeout: (materialId) => {
    if (detail.value?.materialId === materialId && draft.value?.status === 'pending' && !draftDirty.value) {
      draftWaitExpired.value = true
      draftError.value = '知识卡片生成时间较长，请稍后刷新处理状态。'
    }
  },
})
const summaryPoller = createSummaryPoller({
  fetch: (materialId) => api.getMaterialSummary(materialId),
  onResult: (materialId, result) => {
    // 二次校验：仅当当前详情仍是该资料时才写回（防止旧请求覆盖新资料摘要）
    if (detail.value && detail.value.materialId === materialId) {
      detail.value.summary = { text: result.text, status: result.status, generatedAt: result.generatedAt }
    }
  },
  onTimeout: (materialId) => {
    if (detail.value && detail.value.materialId === materialId) {
      summaryWaitExpired.value = true
    }
  },
})

// P14-04：智能分析轮询（复用摘要轮询器的 session token 防串台）。
// onResult 写回候选 / 实体 / 摘要前，二次校验当前详情仍是该资料。
const analysisPoller = createAnalysisPoller({
  fetch: (materialId) => api.getMaterialAnalysis(materialId),
  onResult: (materialId, result) => {
    if (detail.value && detail.value.materialId === materialId) {
      analysis.value = { tagSuggestions: result.tagSuggestions, entities: result.entities, relations: result.relations }
      detail.value.summary = { text: result.summary.text, status: result.summary.status, generatedAt: result.summary.generatedAt }
      analysisWaitExpired.value = false
    }
  },
  onTimeout: (materialId) => {
    if (detail.value && detail.value.materialId === materialId) {
      analysisWaitExpired.value = true
    }
  },
})

// P14-04：实体按类型分组展示（人物 / 地点 / 组织 / 术语）
const entityGroupOrder: EntityType[] = ['person', 'place', 'organization', 'term']
const entityGroupLabels: Record<EntityType, string> = {
  person: '人物',
  place: '地点',
  organization: '组织',
  term: '术语',
}
const entityGroups = computed(() => {
  const groups = entityGroupOrder.map((type) => ({ type, label: entityGroupLabels[type], items: [] as EntityExtraction[] }))
  for (const it of analysis.value?.entities.items ?? []) {
    const group = groups.find((g) => g.type === it.type)
    if (group) group.items.push(it)
  }
  return groups.filter((g) => g.items.length)
})

// P14-02：内嵌图片的来源位置文案（区分 PDF 页 / DOCX 段落 / 表格单元格 / 重复出现）
function imageLocationLabel(img: EmbeddedImage): string {
  const loc = img.location
  const parts: string[] = []
  if (loc.table != null) {
    parts.push(`表格 ${loc.table}`)
    if (loc.row != null && loc.column != null) {
      parts.push(`第 ${loc.row} 行第 ${loc.column} 列`)
    }
  } else if (loc.page != null) {
    parts.push(`第 ${loc.page} 页`)
  } else if (loc.paragraph != null) {
    parts.push(`段落 ${loc.paragraph}`)
  }
  if (loc.occurrence != null) {
    parts.push(`图 ${loc.occurrence}`)
  }
  return parts.join(' · ')
}

function formatSeconds(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '00:00'
  const total = Math.floor(value)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function onTimeUpdate() {
  currentTime.value = audioEl.value?.currentTime ?? 0
}

function seekTo(seconds: number) {
  if (!audioEl.value) return
  audioEl.value.currentTime = Math.max(0, seconds)
  audioEl.value.play().catch(() => {})
}

function isActiveSegment(seg: TranscriptSegment): boolean {
  const t = currentTime.value
  return t >= seg.start && t < seg.end
}

// P14-04：写入一次聚合分析结果（双重校验：仍是发起时的资料；调用方已过请求代次校验）
function setAnalysis(materialId: string, result: MaterialAnalysis): boolean {
  if (!detail.value || detail.value.materialId !== materialId) return false
  analysis.value = { tagSuggestions: result.tagSuggestions, entities: result.entities, relations: result.relations }
  detail.value.summary = { text: result.summary.text, status: result.summary.status, generatedAt: result.summary.generatedAt }
  return true
}

// P14-04：候选或实体仍在生成中时启动轮询，直到两者都离开 pending
function startAnalysisPollingIfPending(materialId: string, result: MaterialAnalysis): void {
  if (result.summary.status === 'pending' || result.tagSuggestions.status === 'pending' || result.entities.status === 'pending' || result.relations.status === 'pending') {
    analysisPoller.start(materialId)
  }
}

// P14-04：首次加载（详情打开时）读取聚合分析；pending 时自动轮询到终态
async function loadAnalysis() {
  if (!detail.value || analysisLoading.value) return
  const materialId = detail.value.materialId
  analysisLoading.value = true
  analysisError.value = ''
  analysisWaitExpired.value = false
  const requestSession = analysisLoadGate.next()
  try {
    const result = await api.getMaterialAnalysis(materialId)
    // 请求代次 + 当前资料二次校验：防切换后旧资料的分析结果覆盖新资料
    if (!analysisLoadGate.isCurrent(requestSession) || !detail.value || detail.value.materialId !== materialId) return
    setAnalysis(materialId, result)
    startAnalysisPollingIfPending(materialId, result)
  } catch (e) {
    if (analysisLoadGate.isCurrent(requestSession) && detail.value?.materialId === materialId) {
      analysisError.value = e instanceof Error ? e.message : '智能分析加载失败'
    }
  } finally {
    if (analysisLoadGate.isCurrent(requestSession) && detail.value?.materialId === materialId) {
      analysisLoading.value = false
    }
  }
}

// 用户明确点击“重新解析”时，强制重新生成摘要、标签、实体和关系。
// 后端会先把四项状态置为 pending，避免前端读到旧 ok 结果后过早结束轮询。
async function reparseMaterial() {
  if (!detail.value || draftEditingBlocked.value || draftRetryBlockedByEdits.value || analysisLoading.value || derivedGenerationPending.value || draftGenerationPending.value) return
  const materialId = detail.value.materialId
  const retryDraft = draftGenerationWaitExpired.value && !draftDirty.value && !draft.value?.userEdited
  const retryAnalysis = !retryDraft || summaryGenerationWaitExpired.value || analysisGenerationWaitExpired.value
  analysisLoading.value = true
  analysisError.value = ''
  if (retryDraft) draftError.value = ''
  analysisWaitExpired.value = false
  const requestSession = analysisLoadGate.next()
  let activeAction: 'analysis' | 'draft' = retryAnalysis ? 'analysis' : 'draft'
  try {
    if (retryAnalysis) {
      const result = await api.reparseMaterial(materialId)
      if (!analysisLoadGate.isCurrent(requestSession) || !detail.value || detail.value.materialId !== materialId) return
      setAnalysis(materialId, result)
      // 提交后即使请求完成得很快，也先展示进行中；轮询以服务端 pending/终态为准。
      detail.value.summary = { ...result.summary, status: 'pending', generatedAt: null }
      analysis.value = {
        tagSuggestions: { ...result.tagSuggestions, status: 'pending' },
        entities: { ...result.entities, status: 'pending' },
        relations: { ...result.relations, status: 'pending' },
      }
      summaryWaitExpired.value = false
      summaryPoller.start(materialId)
      analysisPoller.start(materialId)
    }
    if (retryDraft) {
      activeAction = 'draft'
      const latestDraft = await api.regenerateMaterialDraft(materialId)
      if (!analysisLoadGate.isCurrent(requestSession) || detail.value?.materialId !== materialId || draftDirty.value) return
      draft.value = latestDraft
      draftTitle.value = latestDraft.title
      draftContent.value = latestDraft.content
      draftWaitExpired.value = false
      takeDraftSnapshot()
      generatedDraftPoller.start(materialId)
    }
  } catch (e) {
    if (analysisLoadGate.isCurrent(requestSession) && detail.value?.materialId === materialId) {
      const message = e instanceof Error ? e.message : activeAction === 'draft' ? '草稿重新生成失败' : '重新解析失败'
      if (activeAction === 'draft') draftError.value = message
      else analysisError.value = message
    }
  } finally {
    if (analysisLoadGate.isCurrent(requestSession) && detail.value?.materialId === materialId) {
      analysisLoading.value = false
    }
  }
}

// P14-04：确认候选 → 写入正式标签（后端校验归属 + 审计 + 幂等）；
// 返回后候选标记为已确认并保留在候选区，候选与正式标签依然严格区分。
async function confirmSuggestion(suggestionId: string) {
  if (!detail.value || confirming.value) return
  const materialId = detail.value.materialId
  confirming.value = suggestionId
  tagError.value = ''
  try {
    const result = await api.confirmTagSuggestion(materialId, suggestionId)
    // 请求前已固定资料 ID；返回后若已切换资料则直接结束，不得误操作新资料
    if (!detail.value || detail.value.materialId !== materialId) return
    detail.value.tags = result.tags
    if (analysis.value) {
      analysis.value = {
        ...analysis.value,
        tagSuggestions: {
          ...analysis.value.tagSuggestions,
          items: analysis.value.tagSuggestions.items.map((it) =>
            it.suggestionId === suggestionId ? { ...it, confirmed: true } : it,
          ),
        },
      }
    }
    loadRelated(materialId)
  } catch (e) {
    if (detail.value && detail.value.materialId === materialId) {
      tagError.value = e instanceof Error ? e.message : '确认候选标签失败'
    }
  } finally {
    confirming.value = ''
  }
}

// P14-04：实体「作为标签添加」→ 复用正式标签 add 链路（不伪装成候选确认）。
// 请求前固定资料 ID；返回后校验当前详情仍一致，避免误写已切换的资料。
const addEntityAsTagCore = createEntityTagAdder({
  getDetail: () => (detail.value ? { materialId: detail.value.materialId, tags: detail.value.tags } : null),
  isBusy: () => addingEntityTag.value !== '',
  setBusyEntityId: (entityId) => {
    addingEntityTag.value = entityId
  },
  setMaterialTags: (materialId, tags, action) => api.setMaterialTags(materialId, tags, action),
  applyTags: (materialId, tags) => {
    if (detail.value && detail.value.materialId === materialId) {
      detail.value.tags = tags
      loadRelated(materialId)
    }
  },
  onError: (message) => {
    tagError.value = message
  },
})

async function addEntityAsTag(entity: EntityExtraction) {
  tagError.value = ''
  await addEntityAsTagCore(entity)
}

async function loadVersions(materialId: string) {
  try {
    const result = await api.listMaterialVersions(materialId)
    if (detail.value?.materialId === materialId) versions.value = result.items
  } catch {
    if (detail.value?.materialId === materialId) versions.value = []
  }
}

async function loadVersionImpact(materialId: string) {
  if (!detail.value?.supersedesMaterialId || detail.value.status !== 'available') return
  try {
    const result = await api.getMaterialVersionImpact(materialId)
    if (detail.value?.materialId !== materialId) return
    versionImpact.value = result
    for (const card of result.activeKnowledgeCards) {
      if (!versionActions.value[card.knowledgeId]) versionActions.value[card.knowledgeId] = 'keep'
    }
  } catch (e) {
    if (detail.value?.materialId === materialId) error.value = e instanceof Error ? e.message : '加载版本影响失败'
  }
}

function stopVersionPolling() {
  if (versionPollTimer) clearTimeout(versionPollTimer)
  versionPollTimer = null
}

function stopCardIndexPolling() {
  cardIndexPollSession += 1
  if (cardIndexPollTimer) clearTimeout(cardIndexPollTimer)
  cardIndexPollTimer = null
}

function pollCardIndexUntilTerminal(materialId: string) {
  stopCardIndexPolling()
  const pollSession = ++cardIndexPollSession
  const poll = async () => {
    if (pollSession !== cardIndexPollSession || detail.value?.materialId !== materialId) return
    try {
      const result = await api.getMaterialDetail(materialId)
      if (pollSession !== cardIndexPollSession || detail.value?.materialId !== materialId) return
      const next = result.draftCard
      if (!next?.confirmed) return
      draft.value = next
      if (next.indexState === 'indexing') cardIndexPollTimer = setTimeout(poll, 1800)
    } catch {
      if (pollSession === cardIndexPollSession && detail.value?.materialId === materialId) {
        cardIndexPollTimer = setTimeout(poll, 3000)
      }
    }
  }
  cardIndexPollTimer = setTimeout(poll, 1200)
}

function pollVersionUntilReady(materialId: string) {
  stopVersionPolling()
  const poll = async () => {
    try {
      const result = await api.getUploadStatus(materialId)
      if (detail.value?.materialId !== materialId) return
      Object.assign(detail.value, result)
      if (result.status === 'available') {
        await loadDetail(materialId)
        return
      }
      if (result.status === 'failed') return
    } catch {
      return
    }
    versionPollTimer = setTimeout(poll, 2000)
  }
  versionPollTimer = setTimeout(poll, 1500)
}

function onVersionFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  versionFile.value = input.files?.[0] ?? null
}

async function uploadNewVersion() {
  if (!detail.value || !versionFile.value || uploadingVersion.value) return
  const oldMaterialId = detail.value.materialId
  uploadingVersion.value = true
  error.value = ''
  try {
    const result = await api.uploadMaterialVersion(oldMaterialId, versionFile.value, versionNote.value, detail.value.folderId)
    showVersionUpload.value = false
    versionFile.value = null
    versionNote.value = ''
    toast({ type: 'success', message: `已提交 V${result.versionNumber}，等待处理完成` })
    router.replace(`/materials/${result.newMaterialId}`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '上传新版本失败'
  } finally {
    uploadingVersion.value = false
  }
}

async function applyVersionAction(cardId: string) {
  if (!detail.value || !versionImpact.value || applyingVersionAction.value) return
  const action = versionActions.value[cardId] ?? 'keep'
  if (action === 'keep') return
  if (action === 'manual') {
    router.push(`/knowledge/${cardId}`)
    return
  }
  const oldMaterialId = versionImpact.value.oldMaterialId
  if (!oldMaterialId) return
  applyingVersionAction.value = cardId
  try {
    const current = await api.getKnowledgeSources(cardId)
    const refs = current.sourceRefs.map((ref) => ({ sourceType: ref.sourceType, id: ref.id }))
    await api.putKnowledgeSources(cardId, {
      sourceRefs: applyVersionSourceAction(refs, oldMaterialId, detail.value.materialId, action),
    })
    toast({ type: 'success', message: action === 'replace' ? '已替换为新版本' : '已同时保留两个版本' })
    await loadVersionImpact(detail.value.materialId)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '更新卡片来源失败'
  } finally {
    applyingVersionAction.value = ''
  }
}

async function applyAllVersionActions() {
  if (!versionImpact.value || applyingVersionAction.value) return
  for (const card of versionImpact.value.activeKnowledgeCards) {
    const action = versionActions.value[card.knowledgeId] ?? 'keep'
    if (action === 'replace' || action === 'keepBoth') await applyVersionAction(card.knowledgeId)
  }
}

async function saveDraft(): Promise<boolean> {
  if (!detail.value || !draft.value || savingDraft.value || draft.value.confirmed) return false
  if (!draftContent.value.trim()) {
    draftError.value = '草稿正文不能为空'
    return false
  }
  if (!draftDirty.value && draft.value.status === 'ok') {
    toast({ type: 'success', message: '草稿已保存' })
    return true
  }
  savingDraft.value = true
  draftError.value = ''
  const materialId = detail.value.materialId
  const expectedRevision = draft.value.revision ?? ''
  const title = draftTitle.value
  const content = draftContent.value
  try {
    // 单次 CAS 写入即可：后端会让显式用户保存覆盖尚未编辑的 AI 增强版本，
    // 但仍拒绝覆盖其它用户会话的修改。避免保存前额外跨盒 GET 带来的整轮延迟。
    const savedDraft = await api.saveMaterialDraftCard(materialId, {
      expectedRevision, title, content,
    })
    if (detail.value?.materialId !== materialId) return false
    draft.value = savedDraft
    draftTitle.value = savedDraft.title
    draftContent.value = savedDraft.content
    takeDraftSnapshot()
    toast({ type: 'success', message: '草稿已保存' })
    return true
  } catch (e) {
    if (detail.value?.materialId === materialId) {
      draftError.value = e instanceof Error ? e.message : '保存草稿失败'
    }
    return false
  } finally {
    savingDraft.value = false
  }
}

async function confirmDraft() {
  if (!detail.value || !draft.value || confirmingDraft.value || draft.value.confirmed) return
  if (!(await saveDraft())) return
  if (!detail.value || !draft.value) return
  const materialId = detail.value.materialId
  const revision = draft.value.revision ?? ''
  confirmingDraft.value = true
  draftError.value = ''
  try {
    const result = await api.confirmMaterialDraftCard(
      materialId, revision, crypto.randomUUID(),
    )
    if (detail.value?.materialId !== materialId || draft.value?.revision !== revision) return
    draft.value = { ...draft.value, status: 'confirmed', confirmed: true, knowledgeId: result.knowledgeId, indexState: 'indexing', indexErrorCode: null }
    pollCardIndexUntilTerminal(materialId)
    toast({ type: 'success', message: '卡片已确认，正在建立索引' })
  } catch (e) {
    if (detail.value?.materialId === materialId) {
      draftError.value = e instanceof Error ? e.message : '确认卡片失败'
    }
  } finally {
    confirmingDraft.value = false
  }
}

const indexStatusText = computed(() => {
  switch (draft.value?.indexState) {
    case 'indexed': return '索引已完成，可参与检索。'
    case 'index_failed': return `索引失败${draft.value.indexErrorCode ? `：${draft.value.indexErrorCode}` : ''}，暂不参与检索。`
    case 'indexing': return '索引正在排队或执行，完成后即可检索。'
    default: return '索引尚未建立，暂不参与检索。'
  }
})

async function retryCardIndex() {
  if (!detail.value || !draft.value?.knowledgeId || retryingIndex.value) return
  const materialId = detail.value.materialId
  const knowledgeId = draft.value.knowledgeId
  retryingIndex.value = true
  draftError.value = ''
  try {
    const card = await api.getKnowledge(knowledgeId)
    if (!card.revision) throw new Error('无法读取卡片版本，暂不能重试索引')
    await api.retryKnowledgeIndex(knowledgeId, card.revision)
    if (detail.value?.materialId !== materialId || draft.value?.knowledgeId !== knowledgeId) return
    draft.value = { ...draft.value, indexState: 'indexing', indexErrorCode: null }
    pollCardIndexUntilTerminal(materialId)
    toast({ type: 'success', message: '索引任务已重新入队' })
  } catch (e) {
    draftError.value = e instanceof Error ? e.message : '重试索引失败'
  } finally {
    retryingIndex.value = false
  }
}

async function rethinkDraft() {
  if (!draft.value || draft.value.confirmed) return
  if (!(await saveDraft())) return
  toast({ type: 'info', message: '草稿已保存，尚未确认' })
}

function onLifecycleCompleted(action: 'recycle' | 'purge' | 'unrecycle') {
  if (action === 'unrecycle') {
    loadDetail(String(route.params.materialId))
    toast({ type: 'success', message: '资料已恢复' })
    return
  }
  toast({ type: 'success', message: action === 'purge' ? '资料已永久清除' : '资料已移至回收站' })
  router.replace('/materials')
}

async function addTag() {
  if (!detail.value || !newTag.value.trim()) return
  tagError.value = ''
  try {
    const result = await api.setMaterialTags(detail.value.materialId, [newTag.value.trim()], 'add')
    detail.value.tags = result.tags
    newTag.value = ''
    loadRelated(detail.value.materialId)
  } catch (e) {
    tagError.value = e instanceof Error ? e.message : '添加标签失败'
  }
}

async function removeTag(tag: string) {
  if (!detail.value) return
  tagError.value = ''
  try {
    const result = await api.setMaterialTags(detail.value.materialId, [tag], 'remove')
    detail.value.tags = result.tags
    loadRelated(detail.value.materialId)
  } catch (e) {
    tagError.value = e instanceof Error ? e.message : '移除标签失败'
  }
}

function openRelated(item: RelatedRecommendation) {
  router.push(item.sourceType === 'material' ? `/materials/${item.id}` : `/knowledge/${item.id}`)
}

async function loadRelated(materialId: string) {
  relatedLoading.value = true
  const requestSession = relatedLoadGate.next()
  try {
    const result = await api.getMaterialRelated(materialId)
    // 请求代次校验 + 当前资料校验：防旧资料的关联结果覆盖新资料的相关内容
    if (!relatedLoadGate.isCurrent(requestSession) || detail.value?.materialId !== materialId) {
      return
    }
    related.value = result.items
    relatedNote.value = result.note
  } catch {
    if (relatedLoadGate.isCurrent(requestSession) && detail.value?.materialId === materialId) {
      related.value = []
      relatedNote.value = ''
    }
  } finally {
    if (relatedLoadGate.isCurrent(requestSession) && detail.value?.materialId === materialId) {
      relatedLoading.value = false
    }
  }
}

async function loadDetail(materialId: string, options: { background?: boolean } = {}) {
  const background = options.background === true
  // A delayed analysis read for the previous route must not keep the reused
  // component's loading flag true and prevent the new material from loading.
  analysisLoadGate.invalidate()
  analysisLoading.value = false
  analysisError.value = ''
  analysisWaitExpired.value = false
  if (detail.value?.materialId !== materialId) analysis.value = null
  if (!background) {
    loading.value = true
    error.value = ''
  }
  draftError.value = ''
  currentTime.value = 0
  stopVersionPolling()
  stopCardIndexPolling()
  versionImpact.value = null
  versionActions.value = {}
  // 路由切换：先取消旧资料的摘要轮询，避免旧结果覆盖新页面
  summaryPoller.stop()
  analysisPoller.stop()
  generatedDraftPoller.stop()
  summaryWaitExpired.value = false
  draftWaitExpired.value = false
  const requestSession = detailLoadGate.next()
  try {
    const result = await api.getMaterialDetail(materialId)
    // 请求代次校验：仅最新请求且路由仍为该资料时才写入详情 / 关联 / 轮询
    if (!detailLoadGate.isCurrent(requestSession) || route.params.materialId !== materialId) {
      return
    }
    detail.value = result
    draft.value = result.draftCard
    draftTitle.value = result.draftCard?.title ?? ''
    draftContent.value = result.draftCard?.content ?? ''
    takeDraftSnapshot()
    if (result.draftCard?.status === 'pending' && !privacyBlocking.value) {
      generatedDraftPoller.start(materialId)
    }
    loadVersions(materialId)
    if (result.supersedesMaterialId && result.status === 'available') loadVersionImpact(materialId)
    if (result.status === 'uploaded' || result.status === 'queued' || result.status === 'processing') {
      pollVersionUntilReady(materialId)
    }
    if (result.draftCard?.confirmed && result.draftCard.indexState === 'indexing') {
      pollCardIndexUntilTerminal(materialId)
    }
    loadRelated(detail.value.materialId)
    // 摘要仍在后台生成时自动轮询，直到 ok/failed/unavailable/skipped
    if (detail.value.summary.status === 'pending' && !privacyBlocking.value) {
      summaryPoller.start(detail.value.materialId)
    }
    // P14-04：读取聚合分析（标签候选 / 实体），pending 时内部启动轮询
    if (!privacyBlocking.value) loadAnalysis()
  } catch (e) {
    if (detailLoadGate.isCurrent(requestSession) && route.params.materialId === materialId) {
      const message = e instanceof Error ? e.message : '资料详情加载失败'
      if (background) draftError.value = message
      else error.value = message
    }
  } finally {
    // loading 只允许最新请求归位，避免旧请求误关新请求的加载态
    if (detailLoadGate.isCurrent(requestSession) && route.params.materialId === materialId) {
      if (!background) loading.value = false
    }
  }
}

// Vue Router 会复用同一路由组件；参数变化时重新加载详情与关联内容。
watch(
  () => route.params.materialId,
  (id) => {
    if (id) loadDetail(String(id))
  }
)

onMounted(() => {
  window.addEventListener('beforeunload', onDraftBeforeUnload)
  loadDetail(String(route.params.materialId))
})

// 组件卸载时取消摘要/分析轮询并使加载代次失效，避免异步结果写回已销毁的页面
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onDraftBeforeUnload)
  summaryPoller.stop()
  analysisPoller.stop()
  generatedDraftPoller.stop()
  stopVersionPolling()
  stopCardIndexPolling()
  detailLoadGate.invalidate()
  analysisLoadGate.invalidate()
})

watch(draftDirty, (dirty) => {
  if (dirty) generatedDraftPoller.stop()
})

const mainPreviewUrl = ref('')
const mainPreviewError = ref('')
let previewController: AbortController | null = null
let previewRevision = 0
function clearMainPreview() {
  previewRevision++; previewController?.abort(); previewController = null
  audioEl.value?.pause()
  if (mainPreviewUrl.value) releaseProductPreview(mainPreviewUrl.value)
  mainPreviewUrl.value = ''
}
async function openMainPreview() {
  clearMainPreview()
  mainPreviewError.value = ''
  const value = detail.value
  if (!value || (value.fileType !== 'audio' && !value.fileName.toLowerCase().endsWith('.pdf'))) return
  const ticket = previewRevision
  previewController = new AbortController()
  try {
    const url = await productPreview(value.previewUrl, previewController.signal)
    if (ticket !== previewRevision) releaseProductPreview(url)
    else mainPreviewUrl.value = url
  } catch (e) { if (ticket === previewRevision) mainPreviewError.value = e instanceof Error ? e.message : '原件预览不可用' }
}
watch(() => detail.value?.previewUrl, () => {
  clearMainPreview(); mainPreviewError.value = ''
  if (detail.value?.fileType === 'audio') void openMainPreview()
})
onBeforeUnmount(clearMainPreview)
async function saveOriginal() {
  const value = detail.value
  if (!value) return
  try { await saveProductResource(value.previewUrl, value.fileName) }
  catch (e) { toast({ type: 'error', message: e instanceof Error ? e.message : '原件保存失败' }) }
}

</script>

<template>
  <div class="page">
    <div class="page-head">
      <button class="back-btn" type="button" @click="router.push('/materials')">返回原材料</button>
      <h1>{{ detail?.fileName || route.query.name || '原材料详情' }}</h1>
      <p v-if="detail">{{ detail.folderPath || '未分类' }} · {{ detail.fileType === 'image' ? '图片' : detail.fileType === 'audio' ? '音频' : '文档' }}</p>
    </div>
    <div v-if="loading" class="loading-state">正在加载资料详情…</div>
    <div v-else-if="error" class="error-state">{{ error }}</div>
    <template v-else-if="detail">
      <div class="detail-actions">
        <template v-if="detail.status === 'available' && draft && !draft.confirmed">
          <button class="secondary-btn" type="button" :disabled="draftEditingBlocked || draftRetryBlockedByEdits || analysisLoading || derivedGenerationPending || draftGenerationPending || savingDraft || confirmingDraft" @click="reparseMaterial">{{ privacyReviewRequired ? '等待隐私复核' : privacyProcessing ? '正在安全处理…' : privacyFailed ? '隐私处理失败' : draftRetryBlockedByEdits ? '请先保存草稿' : analysisLoading ? '正在读取状态…' : derivedGenerationPending || draftGenerationPending ? '正在生成…' : generationWaitExpired ? '重试生成' : '重新解析' }}</button>
          <button class="primary-btn" type="button" :disabled="draftEditingBlocked || savingDraft || confirmingDraft" @click="confirmDraft">{{ confirmingDraft ? '确认中…' : '确认' }}</button>
          <button class="secondary-btn" type="button" :disabled="draftEditingBlocked || savingDraft || confirmingDraft" @click="rethinkDraft">再想想</button>
          <button class="secondary-btn sm" type="button" :disabled="draftEditingBlocked || savingDraft || confirmingDraft" @click="saveDraft">{{ savingDraft ? '保存中…' : '保存草稿' }}</button>
        </template>
        <LifecycleDangerPanel
          compact
          target-type="material"
          :target-id="detail.materialId"
          :target-title="detail.fileName"
          :recycled="Boolean(detail.recycled)"
          @completed="onLifecycleCompleted"
        />
      </div>
      <ConfirmDialog
        :open="showLeaveConfirm"
        title="离开前保存"
        message="当前材料草稿有未保存的修改，离开将丢失这些改动。确定要离开吗？"
        confirm-text="离开"
        danger
        @confirm="confirmLeave"
        @cancel="cancelLeave"
      />
      <section v-if="detail.status === 'available' && draft" class="detail-panel draft-card-panel">
        <div class="panel-title">知识卡片 <span class="badge soon">{{ draftBadge }}</span></div>
        <template v-if="draft?.confirmed">
          <p class="detail-text">该卡片已确认。{{ indexStatusText }}</p>
          <button v-if="draft.indexState === 'index_failed' || draft.indexState === 'none'" class="secondary-btn sm" type="button" :disabled="retryingIndex" @click="retryCardIndex">{{ retryingIndex ? '重试中…' : '重试索引' }}</button>
          <button v-if="draft.knowledgeId" class="secondary-btn sm" type="button" @click="router.push(`/knowledge/${draft.knowledgeId}`)">查看知识卡片</button>
        </template>
        <template v-else>
          <div v-if="privacyReviewRequired" class="processing-notice" role="status">
            <strong>安全正文待复核</strong>
            <p>PDF 已解析完成，盒子正在等待受控隐私复核。复核完成后再刷新状态，安全正文和知识卡片会继续生成。</p>
            <button class="secondary-btn sm" type="button" @click="loadDetail(detail.materialId)">刷新处理状态</button>
          </div>
          <div v-else-if="privacyProcessing" class="processing-notice" role="status">
            <strong>正在生成安全正文</strong>
            <p>PDF 已解析完成，隐私处理完成后会继续生成摘要和知识卡片。</p>
            <button class="secondary-btn sm" type="button" @click="loadDetail(detail.materialId)">刷新处理状态</button>
          </div>
          <div v-else-if="privacyFailed" class="processing-notice error-text" role="alert">
            安全正文处理失败，请检查盒端隐私处理任务后重试。
          </div>
          <template v-else>
            <label class="draft-field">标题<input v-model="draftTitle" type="text" maxlength="200" :disabled="draftEditingBlocked || savingDraft || confirmingDraft"></label>
            <label class="draft-field">正文<textarea v-model="draftContent" rows="12" :placeholder="draftGenerationPending ? '正在生成知识卡片正文…' : ''" :disabled="draftEditingBlocked || savingDraft || confirmingDraft"></textarea></label>
            <p v-if="draftGenerationPending" class="detail-text" role="status">AI 正在补充草稿；当前内容已经可以编辑、保存或确认，保存后不会被后台结果覆盖。</p>
            <p class="detail-text">当前材料的标签、摘要、正文、实体和关系均保留在本详情页中，确认时以此草稿正文创建知识卡片。</p>
          </template>
        </template>
        <p v-if="draftError" class="error-text">{{ draftError }}</p>
      </section>
      <RedactionPanel
        v-if="detail.privacyRequired || (detail.privacyStatus && detail.privacyStatus.state !== 'not_required')"
        :material-id="detail.materialId"
        @updated="loadDetail(detail.materialId, { background: true })"
      />
      <div class="detail-grid">
        <section class="detail-panel preview-panel">
          <div class="panel-title">原始资料 <span class="badge soon">只读</span></div>
          <ProductImage v-if="detail.fileType === 'image'" :src="detail.previewUrl" :alt="detail.fileName" class="material-preview image-preview" />
          <div v-if="detail.fileName.toLowerCase().endsWith('.pdf')" class="document-open-state">
            <p v-if="mainPreviewError" role="status">{{ mainPreviewError }}</p>
            <button v-if="!mainPreviewUrl" class="secondary-btn" type="button" @click="openMainPreview">查看 PDF 原件</button>
            <template v-else>
              <button class="secondary-btn sm" type="button" @click="clearMainPreview">关闭 PDF 预览</button>
              <PdfPreview :src="mainPreviewUrl" :title="detail.fileName" />
            </template>
          </div>
         <!-- <div v-else-if="detail.fileType === 'document'" class="document-open-state">
            <p>该文档格式由系统安全托管，可在新窗口中只读打开。</p>
            <button class="secondary-btn" @click="saveOriginal">保存文档</button>
          </div>-->
          <div v-else-if="detail.fileType === 'audio'" class="audio-preview">
            <audio ref="audioEl" :src="mainPreviewUrl || undefined" controls preload="metadata" class="audio-player" @timeupdate="onTimeUpdate"></audio>
            <p class="audio-hint">播放时点击下方转写片段可跳转到对应时刻。</p>
          </div>
          <div class="panel-title text-title">{{ detail.textLabel }}</div>
          <p class="parse-state" role="status">{{ parsingLabel }}</p>
          <template v-if="detail.fileType === 'audio'">
            <div v-if="detail.transcript.length" class="transcript-list">
              <button
                v-for="seg in detail.transcript"
                :key="`${seg.start}-${seg.end}`"
                class="transcript-item"
                :class="{ 'is-active': isActiveSegment(seg) }"
                type="button"
                @click="seekTo(seg.start)"
              >
                <span class="transcript-time">{{ formatSeconds(seg.start) }}</span>
                <span class="transcript-text">{{ seg.text }}</span>
              </button>
            </div>
            <p v-else class="detail-text">
              <template v-if="detail.status === 'processing' || detail.status === 'uploaded'">转写处理中，完成后将显示逐字稿。</template>
              <template v-else>该音频暂无可定位的转写片段。</template>
            </p>
          </template>
          <template v-else-if="detail.fileType === 'document' && contentParts.length">
            <div v-for="part in contentParts" :key="part.partId" class="content-part">
              <pre v-if="part.partType !== 'table'" class="detail-text preformatted part-text">{{ part.text }}</pre>
              <div v-else class="content-part-table">
                <span v-if="part.location.page" class="table-location">第 {{ part.location.page }} 页</span>
                <table class="content-table">
                  <tbody>
                    <tr v-for="(row, ri) in part.rows ?? []" :key="ri">
                      <td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </template>
          <p v-else-if="detail.status === 'processing' || detail.status === 'uploaded' || detail.status === 'queued'" class="detail-text">
            正在解析文档正文。扫描版 PDF 需要逐页 OCR，处理完成后将显示解析文本。
          </p>
          <p v-else-if="privacyReviewRequired" class="detail-text">原文件已解析；安全正文等待盒端隐私复核，当前不会展示原始解析内容。</p>
          <p v-else-if="privacyProcessing" class="detail-text">原文件已解析；正在生成可安全展示的正文。</p>
          <p v-else-if="privacyFailed" class="detail-text error-text">安全正文处理失败，请检查盒端任务。</p>
          <pre v-else class="detail-text preformatted">{{ detail.text || '暂无解析结果。' }}</pre>
        </section>
        <section class="detail-panel">
          <div class="panel-title">处理信息</div>
          <dl class="metadata-list">
            <dt>状态</dt><dd>{{ materialStatusLabel(detail.status) }}</dd>
            <dt>文件大小</dt><dd>{{ formatFileSize(detail.metadata.fileSize) }}</dd>
            <dt>导入时间</dt><dd>{{ formatDate(detail.createdAt) }}</dd>
            <dt>修改时间</dt><dd>{{ formatDate(detail.metadata.modifiedAt) }}</dd>
          </dl>
          <div class="panel-title text-title">摘要</div>
          <template v-if="detail.summary.status === 'ok'">
            <p class="detail-text">{{ detail.summary.text }}</p>
          </template>
          <template v-else-if="detail.summary.status === 'pending'">
            <p v-if="privacyReviewRequired" class="detail-text">安全正文复核完成后再生成摘要。</p>
            <p v-else-if="privacyProcessing" class="detail-text">安全正文生成后再生成摘要。</p>
            <p v-else class="detail-text">{{ summaryWaitExpired ? '仍在后台生成摘要，可刷新页面查看' : '摘要生成中…' }}</p>
          </template>
          <template v-else-if="detail.summary.status === 'skipped'">
            <p class="detail-text">暂无摘要（该资料无可用文本）</p>
          </template>
          <template v-else>
            <p class="detail-text">摘要暂不可用，请使用上方“重新解析”重新生成。</p>
          </template>
          <p v-if="detail.summary.status !== 'ok' && detail.excerpt" class="detail-text excerpt-preview">正文预览：{{ detail.excerpt }}</p>
          <div class="panel-title text-title">主题</div>
          <p class="detail-text">{{ detail.topic || '暂无主题' }}</p>
          <p v-if="detail.errorMessage" class="error-text">{{ detail.errorMessage }}</p>
        </section>
      </div>
      <section class="detail-panel tag-panel">
        <div class="panel-title">标签</div>
        <div class="tag-list">
          <span v-for="tag in detail.tags" :key="tag" class="tag-chip">
            {{ tag }}
            <button class="tag-remove" type="button" :aria-label="`移除标签 ${tag}`" @click="removeTag(tag)">×</button>
          </span>
          <span v-if="!detail.tags.length" class="tag-empty">暂无标签</span>
        </div>
        <div class="tag-input-row">
          <input v-model="newTag" class="tag-input" type="text" placeholder="输入标签后回车添加" maxlength="64" @keyup.enter="addTag">
          <button class="secondary-btn sm" type="button" :disabled="!newTag.trim()" @click="addTag">添加</button>
        </div>
        <div class="tag-suggest">
          <div class="panel-title text-title">候选标签（AI 推荐，确认后写入）</div>
          <template v-if="analysis">
            <template v-if="analysis.tagSuggestions.status === 'pending'">
              <p class="detail-text">{{ analysisWaitExpired ? '仍在后台生成候选标签，请稍后刷新页面查看。' : '候选标签生成中…' }}</p>
            </template>
            <template v-else-if="analysis.tagSuggestions.status === 'ok'">
              <p v-if="analysis.tagSuggestions.source === 'fallback'" class="fallback-hint">已使用本地关键词/规则降级结果</p>
              <div v-if="analysis.tagSuggestions.items.length" class="candidate-list">
                <span v-for="cand in analysis.tagSuggestions.items" :key="cand.suggestionId" class="candidate-chip" :class="{ 'is-confirmed': cand.confirmed }">
                  <span class="candidate-name">{{ cand.name }}</span>
                  <button v-if="!cand.confirmed" class="candidate-confirm" type="button" :disabled="!!confirming" @click="confirmSuggestion(cand.suggestionId)">
                    {{ confirming === cand.suggestionId ? '确认中…' : '确认' }}
                  </button>
                  <span v-else class="candidate-done">已确认</span>
                </span>
              </div>
              <span v-else class="tag-empty">暂无可推荐标签</span>
            </template>
            <template v-else-if="analysis.tagSuggestions.status === 'skipped'">
              <p class="detail-text">暂无候选标签（该资料无可用文本）</p>
            </template>
            <template v-else>
              <p class="detail-text">候选标签暂不可用，请使用上方“重新解析”重新生成。</p>
            </template>
          </template>
          <p v-else-if="analysisLoading" class="detail-text">正在加载候选标签…</p>
          <span v-if="analysisError" class="error-text">{{ analysisError }}</span>
        </div>
        <span v-if="tagError" class="error-text">{{ tagError }}</span>
      </section>
      <section class="detail-panel entity-panel">
        <div class="panel-title">实体识别</div>
        <template v-if="analysis">
          <template v-if="analysis.entities.status === 'pending'">
            <p class="detail-text">{{ analysisWaitExpired ? '仍在后台识别实体，请稍后刷新页面查看。' : '实体识别中…' }}</p>
          </template>
          <template v-else-if="analysis.entities.status === 'ok'">
            <p v-if="analysis.entities.source === 'fallback'" class="fallback-hint">已使用本地关键词/规则降级结果</p>
            <template v-if="entityGroups.length">
              <div v-for="group in entityGroups" :key="group.type" class="entity-group">
                <div class="entity-group-title">{{ group.label }}（{{ group.items.length }}）</div>
                <div class="entity-list">
                  <div v-for="entity in group.items" :key="entity.entityId" class="entity-item">
                    <span class="entity-name">{{ entity.name }}</span>
                    <span class="entity-confidence">把握 {{ Math.round(entity.confidence * 100) }}%</span>
                    <span v-if="entity.evidence" class="entity-evidence">{{ entity.evidence }}</span>
                    <button
                      class="entity-tag-btn"
                      type="button"
                      :disabled="addingEntityTag === entity.entityId || detail.tags.includes(entity.name)"
                      @click="addEntityAsTag(entity)"
                    >
                      {{
                        detail.tags.includes(entity.name)
                          ? '已添加'
                          : addingEntityTag === entity.entityId
                            ? '添加中…'
                            : '作为标签添加'
                      }}
                    </button>
                  </div>
                </div>
              </div>
            </template>
            <span v-else class="tag-empty">未识别到实体</span>
          </template>
          <template v-else-if="analysis.entities.status === 'skipped'">
            <p class="detail-text">暂无实体（该资料无可用文本）</p>
          </template>
          <template v-else>
            <p class="detail-text">实体识别暂不可用，请使用上方“重新解析”重新生成。</p>
          </template>
        </template>
        <p v-else-if="analysisLoading" class="detail-text">正在加载实体…</p>
      </section>
      <section class="detail-panel relation-panel">
        <div class="panel-title">关系三元组</div>
        <template v-if="analysis">
          <template v-if="analysis.relations.status === 'pending'">
            <p class="detail-text">{{ analysisWaitExpired ? '仍在后台识别关系，请稍后刷新页面查看。' : '关系识别中…' }}</p>
          </template>
          <template v-else-if="analysis.relations.status === 'ok'">
            <p v-if="analysis.relations.source === 'fallback'" class="fallback-hint">已使用本地关键词/规则降级结果</p>
            <template v-if="analysis.relations.items.length">
              <div v-for="rel in analysis.relations.items" :key="rel.relationId" class="relation-item">
                <div class="relation-triple">
                  <span class="relation-endpoint">{{ rel.subject.name }}</span>
                  <span class="relation-predicate">{{ rel.predicate }}</span>
                  <span class="relation-endpoint">{{ rel.object.name }}</span>
                  <span class="relation-confidence">把握 {{ Math.round(rel.confidence * 100) }}%</span>
                </div>
                <p v-if="rel.evidence" class="relation-evidence">{{ rel.evidence }}</p>
              </div>
            </template>
            <span v-else class="tag-empty">未识别到关系</span>
          </template>
          <template v-else-if="analysis.relations.status === 'skipped'">
            <p class="detail-text">暂无关系（该资料无可用文本）</p>
          </template>
          <template v-else>
            <p class="detail-text">关系识别暂不可用，请使用上方“重新解析”重新生成。</p>
          </template>
        </template>
        <p v-else-if="analysisLoading" class="detail-text">正在加载关系…</p>
      </section>
      <section v-if="detail.fileType === 'document' && embeddedImages.length" class="detail-panel image-panel">
        <div class="panel-title">内嵌图片（{{ embeddedImages.length }}）</div>
        <div class="embedded-image-grid">
          <div v-for="img in embeddedImages" :key="img.partId" class="embedded-image-card">
            <ProductImage :src="img.previewUrl" :alt="`内嵌图片：${imageLocationLabel(img)}`" class="embedded-image-thumb" />
            <div class="embedded-image-meta">
              <span class="image-location">{{ imageLocationLabel(img) }}</span>
              <span v-if="img.width" class="image-size">{{ img.width }}×{{ img.height }}</span>
            </div>
            <p class="image-ocr" :class="{ 'is-muted': img.ocrStatus !== 'ok' }">
              <template v-if="img.ocrStatus === 'ok'">{{ img.ocrText }}</template>
              <template v-else-if="img.ocrStatus === 'empty'">未识别到文字</template>
              <template v-else>OCR 暂不可用</template>
            </p>
          </div>
        </div>
      </section>
      <section class="detail-panel related-panel">
        <div class="panel-title">相关内容</div>
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
      </section>
      <section class="detail-panel">
        <div class="panel-title">版本记录</div>
        <p class="detail-text">当前为 V{{ detail.versionNumber }}{{ detail.versionNote ? ` · ${detail.versionNote}` : '' }}</p>
        <div v-if="versions.length" class="related-list">
          <button v-for="version in versions" :key="version.materialId" class="related-item" type="button" @click="router.push(`/materials/${version.materialId}`)">
            <span class="related-main"><strong>V{{ version.versionNumber }} · {{ version.fileName }}</strong><span v-if="version.versionNote" class="related-snippet">{{ version.versionNote }}</span></span>
            <span class="related-meta">{{ materialStatusLabel(version.status) }}</span>
          </button>
        </div>
        <p v-else class="detail-text">暂无其他版本</p>
      </section>
      <section v-if="detail.supersedesMaterialId" class="detail-panel">
        <div class="panel-title">新版本影响处理</div>
        <p v-if="detail.status !== 'available'" class="detail-text">新版本处理中。处理成功前不会修改旧版本或任何卡片来源。</p>
        <template v-else-if="versionImpact?.ready">
          <p class="detail-text">默认保持旧版本。选择替换或同时保留后，才会更新对应卡片来源。</p>
          <div v-if="versionImpact.activeKnowledgeCards.length" class="related-list">
            <div v-for="card in versionImpact.activeKnowledgeCards" :key="card.knowledgeId" class="related-item">
              <span class="related-main"><strong>{{ card.title }}</strong></span>
              <span class="related-meta">
                <select v-model="versionActions[card.knowledgeId]" :disabled="!!applyingVersionAction">
                  <option value="keep">保持旧版本</option>
                  <option value="replace">替换为新版本</option>
                  <option value="keepBoth">同时保留</option>
                  <option value="manual">手工处理</option>
                </select>
                <button class="secondary-btn sm" type="button" :disabled="!!applyingVersionAction" @click="applyVersionAction(card.knowledgeId)">{{ applyingVersionAction === card.knowledgeId ? '保存中…' : '应用' }}</button>
              </span>
            </div>
          </div>
          <p v-else class="detail-text">没有仍引用旧版本的活跃知识卡片。</p>
          <button v-if="versionImpact.activeKnowledgeCards.length" class="secondary-btn sm" type="button" :disabled="!!applyingVersionAction" @click="applyAllVersionActions">应用全部非保持项</button>
          <p v-if="versionImpact.archivedKnowledgeCardCount" class="detail-text">另有 {{ versionImpact.archivedKnowledgeCardCount }} 张已归档卡片保持原引用。</p>
        </template>
      </section>
    </template>
    <div v-else class="empty-state">
      <div class="empty-icon"><FileText :size="30" aria-hidden="true" /></div>
      <div class="empty-title">资料不存在</div>
    </div>

    <div v-if="showVersionUpload" class="gov-modal-mask" @click.self="showVersionUpload = false">
      <div class="gov-modal" role="dialog" aria-modal="true" aria-label="上传新版本">
        <h3>上传新版本</h3>
        <label>文件<input type="file" @change="onVersionFileChange"></label>
        <label>版本说明（可选）<textarea v-model="versionNote" rows="3" maxlength="500" placeholder="例如：补充 2026 年 8 月数据"></textarea></label>
        <div class="gov-modal-actions">
          <button class="secondary-btn" type="button" :disabled="uploadingVersion" @click="showVersionUpload = false">取消</button>
          <button class="primary-btn" type="button" :disabled="!versionFile || uploadingVersion" @click="uploadNewVersion">{{ uploadingVersion ? '上传中…' : '开始上传' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
