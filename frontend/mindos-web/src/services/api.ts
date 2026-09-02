// 类型化 API Service：MindOS 浏览器页面统一通过此模块访问 /api/...，
// 不依赖 window.api / Electron preload / ipcRenderer。
import type { HealthInfo } from '@/types'

const BASE = '/api'
const CSRF_HEADERS = { 'X-Requested-By': 'centaur-vdb' }

// 阶段 2：票据一次性交换后的受控会话凭证。业务请求携带它，不再逐请求携带票据
// （票据 nonce 单次使用，重放即拒绝）。凭证仅在本模块内存中持有（非持久化），
// 由 App/Electron 的 Consumer Client 经受控通道在每次页面生命周期注入；绝不写入
// localStorage 等可持久化存储——避免静态/持久化 Bearer 会话凭证泄露后可直接访问
// MindOS，也符合「MindOS 不承担账号/Owner/认领控制面」。
const SESSION_HEADER = 'X-MindOS-Session'

// 会话凭证仅本次页面生命周期存活，刷新即失效，须由宿主重新注入。
let sessionToken: string | null = null

export function setMindosSessionToken(token: string | null): void {
  sessionToken = token || null
}

export function getMindosSessionToken(): string | null {
  return sessionToken
}

export class ApiError extends Error {
  readonly status: number
  readonly code?: string
  readonly details?: string[]

  constructor(message: string, status: number, code?: string, details?: string[]) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

export interface MindosAccessContext {
  mode: 'local_debug' | 'connectivity_ticket_required'
  localDebug: boolean
  scope?: 'mindos:local-debug'
  reason?: 'runtime_not_development' | 'local_debug_disabled' | 'server_not_loopback'
  // 票据模式下返回本机设备标识（供认领/换票闭环使用）；调试模式不返回。
  deviceId?: string
}

/** API 根路径；SSE 流式客户端（services/sse.ts）与 request 共用。 */
export const API_BASE = BASE

/**
 * 统一请求头：system-models 的读取与写入接口均要求 X-Requested-By——它让跨站请求
 * 触发 CORS 预检，而后端只接受 loopback 请求。统一在 API 边界注入，避免 GET 漏带；
 * 票据模式再附上会话凭证。SSE 客户端复用同一函数，保证三层 gate 行为一致。
 */
export function buildHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers)
  headers.set('X-Requested-By', 'centaur-vdb')
  const token = getMindosSessionToken()
  if (token) headers.set(SESSION_HEADER, token)
  return headers
}

/** 把非 2xx 响应解析成 ApiError 并抛出（支持三种后端错误体形状）。 */
export async function throwApiError(res: Response): Promise<never> {
  let message = `请求失败（${res.status}）`
  let code: string | undefined
  let details: string[] | undefined
  try {
    const body = await res.json()
    if (body && typeof body.detail === 'string') message = body.detail
    else if (body && body.detail && typeof body.detail === 'object') {
      if (typeof body.detail.detail === 'string') message = body.detail.detail
      else if (typeof body.detail.message === 'string') message = body.detail.message
      code = typeof body.detail.code === 'string' ? body.detail.code : undefined
      const parsedDetails = Array.isArray(body.detail.details)
        ? body.detail.details.filter((item: unknown): item is string => typeof item === 'string')
        : []
      details = parsedDetails.length ? parsedDetails : undefined
    }
    else if (body && typeof body.message === 'string') {
      // P1：system-models 统一错误体 {code, message, details?}——附加 details 便于定位
      code = typeof body.code === 'string' ? body.code : undefined
      const parsedDetails = Array.isArray(body.details)
        ? body.details.filter((item: unknown): item is string => typeof item === 'string')
        : []
      details = parsedDetails.length ? parsedDetails : undefined
      message = parsedDetails.length ? `${body.message}（${parsedDetails.join('；')}）` : body.message
    }
    if (!code && body && typeof body.code === 'string') code = body.code
  } catch {
    // 忽略非 JSON 响应体
  }
  throw new ApiError(message, res.status, code, details)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = buildHeaders(init)
  const res = await fetch(`${BASE}${path}`, { ...init, headers })
  if (!res.ok) await throwApiError(res)
  return res.json() as Promise<T>
}

export interface ImportValidationResult {
  status: 'ok' | 'oversize' | 'unsupported' | 'audio_pending'
  category: 'document' | 'image' | 'audio' | 'unknown'
  message: string
}

// MindOS 上传/处理状态（uploaded 上传中 / queued 等待处理 / processing 处理中 / available 已完成）
// 文案与语义色统一映射见 src/shared/status.ts
export type MaterialStatus = 'uploaded' | 'queued' | 'processing' | 'available' | 'failed' | 'deleted'
export type MaterialKnowledgeCardState = 'waiting' | 'generating' | 'draft' | 'confirming' | 'indexing' | 'available' | 'failed' | 'recycled' | 'unknown'

export interface UploadResult {
  materialId: string
  fileName: string
  fileType: 'document' | 'image' | 'audio'
  status: MaterialStatus
  jobId: string
  errorMessage: string | null
  errorCode?: string | null
  oldIndexPreserved?: boolean | null
  indexDegraded?: boolean
  // P14-06：folder 为兼容读字段；folderId 是目录树唯一事实来源（null=未分类）
  folder: string
  folderId: number | null
  createdAt: string
  materialFamilyId: string
  versionNumber: number
  supersedesMaterialId: string | null
  supersededByMaterialId: string | null
  versionNote: string | null
  // P15-05：回收站资料不出现在默认列表；详情与回收站仍会返回该标记。
  recycled?: boolean
  // 原材料列表的知识卡片投影；由后端从草稿和卡片台账一次性汇总。
  knowledgeCard?: {
    state: MaterialKnowledgeCardState
    knowledgeId: string | null
    indexState: string | null
    errorCode: string | null
  }
}

export interface MaterialImpactCard {
  knowledgeId: string
  title: string
  archived: boolean
  // P15-05：是否已回收（回收卡片同样计入影响，但不作为活跃阻塞依赖）
  recycled?: boolean
}

export interface MaterialImpact {
  materialId: string
  oldMaterialId?: string
  ready?: boolean
  status?: MaterialStatus
  activeKnowledgeCards: MaterialImpactCard[]
  archivedKnowledgeCards: MaterialImpactCard[]
  // P15-05：已回收卡片（版本影响 / 归档影响展示）
  recycledKnowledgeCards?: MaterialImpactCard[]
  activeKnowledgeCardCount: number
  archivedKnowledgeCardCount: number
  recycledKnowledgeCardCount?: number
  corrections: Array<{ correctionId: string; title: string; status: string }>
  drafts: Array<{ draftId: string; type: string; status: string }>
}

// P15-04/05：删除影响预览与受控回收 / 永久清除
export type LifecycleTargetType = 'material' | 'knowledge'
export type LifecycleDependencyType = 'knowledge' | 'correction' | 'draft' | 'editDraft' | 'pendingUpdate'

export interface BlockingDependency {
  type: LifecycleDependencyType
  id: string
  title: string
  status: string
  allowedActions: string[]
}

export interface DeletionImpactCard {
  knowledgeId: string
  title: string
  archived: boolean
  recycled: boolean
}

export interface DeletionImpactRef {
  id: string
  title: string
  status: string
}

export interface CleanupSummary {
  vectors: number
  derivedRecords: number
  embeddedImages: number
}

export interface DeletionImpactCardGroup {
  active: DeletionImpactCard[]
  archived: DeletionImpactCard[]
  recycled: DeletionImpactCard[]
  activeCount: number
  archivedCount: number
  recycledCount: number
}

// GET /materials/{id}/deletion-impact 与 /knowledge/{id}/deletion-impact 统一结构
export interface DeletionImpact {
  target: { type: LifecycleTargetType; id: string; title: string }
  recycled: boolean
  archived: boolean
  canRecycle: boolean
  canPurge: boolean
  confirmToken: string
  expectedRevision?: string
  blockingDependencies: BlockingDependency[]
  workingEditDraft?: { exists: boolean; revision?: string; updatedAt?: number }
  pendingCardUpdate?: { exists: boolean; state?: string; revision?: string; updatedAt?: number }
  requiredDecisions?: Array<'discard_edit_draft' | 'pending_update_must_finish'>
  knowledgeCards?: DeletionImpactCardGroup
  referencingKnowledgeCards?: DeletionImpactCardGroup
  corrections: DeletionImpactRef[]
  drafts: Array<{ draftId: string; type: string; status: string }>
  governanceItems: DeletionImpactRef[]
  cleanupSummary: CleanupSummary
}

export interface DependencyActionPayload {
  type: LifecycleDependencyType
  id: string
  action: string
  /** P15-04：替换来源可为原材料或知识卡片。 */
  replacementSource?: { sourceType: 'material' | 'knowledge'; id: string } | null
  /** @deprecated 请改用 replacementSource；保留以兼容首版调用方。 */
  replacementMaterialId?: string | null
}

export interface DeletionExecutePayload {
  confirmToken: string
  dependencyActions: DependencyActionPayload[]
  expectedRevision?: string
}

export interface LifecycleExecuteResult {
  materialId?: string
  knowledgeId?: string
  recycled?: boolean
  purged?: boolean
  status?: string
  item?: KnowledgeCard
}

export interface MaterialVersionUploadResult {
  oldMaterialId: string
  newMaterialId: string
  materialFamilyId: string
  versionNumber: number
  status: MaterialStatus
}

export interface MaterialListResponse {
  items: UploadResult[]
  total: number
  // DEPRECATED：旧字符串文件夹名（后端兼容字段）；目录树请用 listFolderNodes()
  folders: string[]
}

// P14-06：目录树节点（扁平数组；parentId=null 为根节点）
export interface FolderNode {
  id: number
  parentId: number | null
  name: string
  scope: string
  materialCount: number
  subtreeMaterialCount: number
  createdAt: string
  updatedAt: string
}

export interface FolderListResponse {
  items: FolderNode[]
}

export interface FolderDeleteResult {
  folderId: number
  movedMaterials: number
  reparentedFolders: number
  // P14-07：删除 KNOWLEDGE 目录时同步迁移的知识卡片数（RAW 目录恒为 0）
  movedCards: number
}

// 音频逐字稿片段（来自索引块 start_time/end_time，单位秒）
export interface TranscriptSegment {
  start: number
  end: number
  text: string
}

// P14-01/02：结构化内容部分与内嵌图片的来源位置（按来源类型取不同键）
export type ContentPartType = 'paragraph' | 'table' | 'page'
export interface ContentPartLocation {
  page?: number
  section?: number
  table?: number
  paragraph?: number
  row?: number
  column?: number
  occurrence?: number
  // PDF 图片实际放置矩形 [x0, y0, x1, y1]（PDF 坐标）
  bbox?: number[]
  [key: string]: number | number[] | undefined
}
export interface ContentPart {
  partId: string
  partType: ContentPartType
  ordinal: number
  text: string
  location: ContentPartLocation
  // 仅表格 part 返回：按行列切分的单元格（TSV 解析结果，空单元格为空串）
  rows?: string[][]
}

// P14-02：DOCX/PDF 内嵌图片（受控预览 + OCR 文本/状态）
export type EmbeddedImageOcrStatus = 'ok' | 'empty' | 'unavailable'
export interface EmbeddedImage {
  partId: string
  previewUrl: string
  location: ContentPartLocation
  ocrText: string
  ocrStatus: EmbeddedImageOcrStatus
  mime?: string
  width?: number | null
  height?: number | null
}

// P14-03：自动摘要（派生数据，status ok 才有文本）
export type SummaryStatus = 'pending' | 'ok' | 'failed' | 'unavailable' | 'skipped'
export interface MaterialSummary {
  text: string
  status: SummaryStatus
  generatedAt: string | null
}

// P14-04：派生分析（标签候选 / 实体抽取）共用派生状态词
export type DerivedStatus = 'pending' | 'ok' | 'failed' | 'unavailable' | 'skipped'

// P14-04：标签候选（只读建议，用户确认后才写入正式 tags）
export interface TagCandidate {
  suggestionId: string
  name: string
  confirmed: boolean
}

// P14-04：抽取实体（confidence 仅供排序/调试，不暗示事实正确性）
export type EntityType = 'person' | 'place' | 'organization' | 'term'
export interface EntityExtraction {
  entityId: string
  type: EntityType
  name: string
  confidence: number
  evidence: string
}

// P14-04：派生结果来源（llm=模型生成；fallback=模型不可用/输出不合规时本地降级）
export type DerivedSource = 'llm' | 'fallback'

export interface DerivedTagSuggestions {
  status: DerivedStatus
  source: DerivedSource | null
  items: TagCandidate[]
  generatedAt: string | null
}

export interface DerivedEntities {
  status: DerivedStatus
  source: DerivedSource | null
  items: EntityExtraction[]
  generatedAt: string | null
}

// P0-1：关系三元组（主体 / 谓词白名单 / 客体），端点 type 与实体产物一致
export type RelationPredicateValue =
  | '替代' | '衍生' | '属于' | '任职于' | '采用' | '提出' | '比对' | '组成'
export interface RelationEndpoint {
  type: EntityType
  name: string
}
export interface RelationExtraction {
  relationId: string
  subject: RelationEndpoint
  predicate: RelationPredicateValue
  object: RelationEndpoint
  confidence: number
  evidence: string
}
export interface DerivedRelations {
  status: DerivedStatus
  source: DerivedSource | null
  items: RelationExtraction[]
  generatedAt: string | null
}

export interface MaterialAnalysis {
  materialId: string
  summary: MaterialSummary
  tagSuggestions: DerivedTagSuggestions
  entities: DerivedEntities
  relations: DerivedRelations
}

export interface MaterialDraftCard {
  cardState: 'draft' | 'confirmed'
  // 草稿生成的内部任务状态，不作为知识卡片的用户状态展示。
  status: 'pending' | 'ok' | 'failed' | 'confirming' | 'confirmed'
  title: string
  content: string
  revision: string | null
  snapshotVersion: number | null
  snapshotId?: string | null
  origin?: 'minimal' | 'model' | 'user'
  userEdited?: boolean
  confirmed?: boolean
  knowledgeId?: string | null
  errorCode?: string | null
  indexState?: 'none' | 'indexing' | 'indexed' | 'index_failed'
  indexErrorCode?: string | null
}

export interface TagSuggestionConfirmResponse {
  suggestionId: string
  name: string
  confirmed: boolean
  tags: string[]
}

export interface MaterialTagSuggestions {
  materialId: string
  status: DerivedStatus
  source: DerivedSource | null
  items: TagCandidate[]
  generatedAt: string | null
}

export interface MaterialDetail extends UploadResult {
  previewUrl: string
  folderPath: string
  metadata: { fileSize: number | null; modifiedAt: string | null }
  summary: MaterialSummary
  // 纯文本预览（截断），仅作预览展示，不代表 AI 摘要
  excerpt: string
  topic: string
  text: string
  textLabel: string
  tags: string[]
  draftCard: MaterialDraftCard | null
  readOnly: true
  // 音频资料的结构化逐字稿；非音频或无可定位片段时为空数组（后端必然返回）
  transcript: TranscriptSegment[]
  // P14-01：结构化内容部分与表格计数；非文档或尚无解析结果时为空数组 / 0
  contentParts: ContentPart[]
  tableCount: number
  // P14-02：内嵌图片（受控预览 + OCR）；非文档或无内嵌图片时为空数组
  embeddedImages: EmbeddedImage[]
}

export interface KnowledgeEditDraft {
  knowledgeId: string
  baseRevision: string
  draftRevision: string
  updatedAt: number
  title: string
  content: string
  tags: string[]
  folderId: number | null
  sourceRefs: Array<{ sourceType: 'material' | 'knowledge'; id: string }>
}

export interface KnowledgeEditDraftSave {
  expectedDraftRevision: string
  title: string
  content: string
  tags: string[]
  folderId: number | null
  sourceRefs: Array<{ sourceType: 'material' | 'knowledge'; id: string }>
}

export interface KnowledgeCard {
  knowledgeId: string
  title: string
  content: string
  // P14-06 兼容字段：目录末段名（未归入任何子树时为 Resources）
  folder: string
  // P14-07：KNOWLEDGE 目录 ID（目录服务不可用时为 null）
  folderId: number | null
  // P14-07：目录全路径，如「知识库/专题」；根目录为 Resources
  folderPath: string
  updatedAt: string
  revision?: string
  vectorSyncState?: 'clean' | 'pending' | 'failed'
  approvalState?: 'draft' | 'confirming' | 'confirmed'
  indexState?: 'none' | 'indexing' | 'indexed' | 'index_failed'
  ragEligible?: boolean
  metadataStatus?: 'valid' | 'invalid'
  sourceLabel: string
  // P14-10：来源带类型追溯（material → /materials/{id}，knowledge → /knowledge/{id}）
  sources: Array<{
    sourceType: 'material' | 'knowledge'
    id: string
    materialId?: string
    knowledgeId?: string
    title: string
    fileName?: string
    archived: boolean
  }>
  tags: string[]
  readOnly: false
  isArchived: boolean
  isMerged: boolean
  // P15-05：回收卡片仅在回收站列表与详情中可见。
  isRecycled?: boolean
  editDraft?: { exists: boolean; baseRevision?: string; draftRevision?: string; updatedAt?: number }
  pendingUpdate?: { state: 'indexing' | 'recovering' | 'index_failed'; phase?: string | null; targetRevision?: string | null; errorCode?: string | null } | null
}

// P15-01：知识卡片来源（sourceType 仅 material / knowledge；archived 供 UI 展示归档状态）
export interface KnowledgeSourceRef {
  sourceType: 'material' | 'knowledge'
  id: string
  materialId?: string
  knowledgeId?: string
  title: string
  fileName?: string
  archived: boolean
}

// P15-01：来源接口响应（GET/PUT /knowledge/{id}/sources）
export interface KnowledgeSourcesResponse {
  knowledgeId: string
  sourceRefs: KnowledgeSourceRef[]
  updatedAt: string
  revision?: string
}

// P15-01：PUT /sources 请求体（提交去重前的完整来源列表，顺序即用户意图）
export interface KnowledgeSourcesUpdatePayload {
  sourceRefs: Array<{ sourceType: 'material' | 'knowledge'; id: string }>
  expectedRevision?: string
}

export interface HomeOverview {
  recentMaterials: UploadResult[]
  recentKnowledge: KnowledgeCard[]
  failedCount: number
  pendingGovernance: number
}

// =====================================================================
// 知君成长闭环 MVP：人生章程、判断、结果与复盘
// =====================================================================

export interface GrowthCharterPayload {
  vision: string
  roles: string[]
  principles: string[]
  boundaries: string[]
  goals: string[]
  challengeStyle: string
  quietDomains: string[]
}

export interface GrowthCharter extends GrowthCharterPayload {
  id: string
  version: number
  createdAt: string
}

export interface GrowthCharterHistory {
  currentCharter: GrowthCharter | null
  versions: GrowthCharter[]
}

export type GrowthDecisionStatus = 'open' | 'outcome_recorded' | 'reviewed'

export interface GrowthDecisionOutcome {
  result: string
  notes: string
  evidenceRefs: string[]
  recordedAt: string
}

export interface GrowthDecisionPayload {
  title: string
  context: string
  options: string[]
  choice: string
  rationale: string
  confidence: number
  expectedOutcome: string
  reviewAt: string | null
  relatedEntityIds: string[]
  evidenceRefs: string[]
}

export interface GrowthDecision extends GrowthDecisionPayload {
  id: string
  status: GrowthDecisionStatus
  charterId: string | null
  charterVersion: number | null
  outcome: GrowthDecisionOutcome | null
  review: GrowthReview | null
  createdAt: string
  updatedAt: string
}

export interface GrowthDecisionList {
  items: GrowthDecision[]
  total: number
}

export interface RecordGrowthOutcomePayload {
  result: string
  notes: string
  evidenceRefs: string[]
}

export interface GrowthReviewPayload {
  decisionId: string
  reflection: string
  lessons: string[]
  nextAction: string
}

export interface GrowthReview extends GrowthReviewPayload {
  id: string
  createdAt: string
}

export interface GrowthReviewResult {
  review: GrowthReview
  decision: GrowthDecision
}

export interface GrowthDueDecision extends GrowthDecision {
  dueState: 'overdue' | 'due_soon'
}

export interface GrowthTodayStats {
  charterVersion: number | null
  totalDecisions: number
  openDecisions: number
  dueSoonDecisions: number
  overdueDecisions: number
  pendingReviews: number
  reviewedDecisions: number
  totalReviews: number
}

export interface GrowthTodayItem {
  type: 'decision_due' | 'pending_review'
  urgency: 'overdue' | 'due_soon' | 'pending_review'
  decision: GrowthDecision
}

export interface GrowthToday {
  generatedAt: string
  currentCharter: GrowthCharter | null
  todayItems: GrowthTodayItem[]
  dueDecisions: GrowthDueDecision[]
  pendingReviews: GrowthDecision[]
  latestReview: GrowthReview | null
  stats: GrowthTodayStats
}

// P14-09：可信 Top-3 关联推荐。同一对象多来源命中合并为一项（reasons 并列全部依据）；
// 只暴露 scoreBand（高/中），不暴露不可解释的原始模型分。
export type RelatedSourceType = 'material' | 'knowledge'
export type ScoreBand = 'high' | 'medium'

export interface RelatedRecommendation {
  id: string
  sourceType: RelatedSourceType
  title: string
  snippet: string
  reason: string
  reasons: string[]
  scoreBand: ScoreBand
}

export interface RelatedResult {
  items: RelatedRecommendation[]
  recommendedLimit: number
  total: number
  // 不足 recommendedLimit 时的原因说明（如“仅 N 项达到阈值”）；达标时为空串
  note: string
}

export type GraphRelation = 'source' | 'shared-tag' | 'similar' | 'semantic'

export interface GraphNode {
  id: string
  type: 'material' | 'knowledge'
  label: string
  fileType?: string
  tags: string[]
  referenceCount: number
}

export interface GraphEdge {
  source: string
  target: string
  relation: GraphRelation
  reason: string
  // 语义边方向：true=主语→宾语有明确方向；false=材料↔资源无方向；非语义边恒为 true
  directed?: boolean
}

export interface GraphStats {
  totalNodes: number
  materials: number
  knowledge: number
  totalEdges: number
  sourceEdges: number
  sharedTagEdges: number
  similarEdges: number
  semanticEdges: number
  isolatedNodes: number
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  stats: GraphStats
}

export type GovernanceKind = 'duplicate' | 'outdated' | 'relation' | 'conflict'
export type GovernanceStatus = 'pending' | 'processing' | 'ignored' | 'merged' | 'archived'

export interface GovernanceItem {
  id: string
  kind: GovernanceKind
  title: string
  reason: string
  snippet: string
  sourceKnowledgeId: string | null
  targetKnowledgeId: string | null
  materialId: string | null
  score: number
  status: GovernanceStatus
  note: string
  createdAt: string
  resolvedAt: string | null
}

export interface GovernanceListResponse {
  items: GovernanceItem[]
  total: number
}

export interface GovernanceStats {
  total: number
  pending: number
  processing: number
  ignored: number
  merged: number
  archived: number
  duplicate: number
  outdated: number
  relation: number
  conflict: number
}

// P14-08：matchMode 为命中依据类别；当前阶段视觉命中恒为 'visual'（CLIP 图文语义），
// 'ocr' | 'caption' 为枚举类别，供后续扩展——绝不把 OCR 命中误标为视觉。
export type VisualMatchMode = 'visual' | 'ocr' | 'caption'

export interface UnifiedVisualHit {
  materialId: string
  title: string
  fileType: 'image'
  // 命中依据片段（VLM 描述 / OCR / 用户说明）；纯图无文字时为空串，前端如实展示「纯图无文字」
  snippet: string
  // CLIP 图文余弦相似度；已按 IMAGE_SIM_THRESHOLD 过滤，弱信号不算命中
  score: number
  matchMode: VisualMatchMode
  previewUrl: string
}

export interface UnifiedSearchResult {
  query: string
  knowledge: Array<{ knowledgeId: string; title: string; snippet: string; score: number }>
  materials: Array<{ materialId: string; title: string; fileType: 'document' | 'image' | 'audio'; snippet: string; score: number }>
  // 不可用材料只按文件名等元数据匹配，绝不携带旧向量/正文片段或相关度。
  unavailableMaterials: Array<{
    materialId: string
    title: string
    fileType: 'document' | 'image' | 'audio'
    status: MaterialStatus
    reason: string
    errorCode?: string | null
    actions: Array<'resume' | 'retry'>
    createdAt: string
  }>
  // 图片语义命中，与文本命中属不同向量空间——总分（total）不含视觉命中，禁止与 BGE 分混合排序
  visualMaterials: UnifiedVisualHit[]
  // capabilities.visualSearch=false 表示 CLIP/VLM 未就绪（显式降级），此时 visualMaterials 恒为空
  capabilities: { visualSearch: boolean }
  total: number
  unavailableTotal: number
}

export type QaStatus = 'ANSWERED' | 'PARTIAL_ANSWER' | 'INSUFFICIENT_EVIDENCE'

export interface QaCitation {
  citationId: string
  sourceType: 'material' | 'knowledge'
  materialId: string | null
  knowledgeId: string | null
  title: string
  snippet: string
}

// P14-12：问答纠错提醒（问题或证据命中已纠正观点时非空；无命中恒为 []）
export interface CorrectionNotice {
  correctionId: string
  title: string
  correctedClaim: string
  sourceIds: string[]
}

// 双通道 meta：provider 为实际生成通道（openai 外部 / ollama 本地）；fallbackUsed 表示外部失败后已回落本地
export interface QaResponseMeta {
  model: string | null
  retrievedCount: number
  usedEvidenceCount: number
  provider: 'openai' | 'ollama' | null
  fallbackUsed: boolean
}

export interface QaResponse {
  status: QaStatus
  question: string
  answer: string
  citations: QaCitation[]
  correctionNotices: CorrectionNotice[]
  meta: QaResponseMeta
}

// P14-12：纠错本记录（用户确认的「错误观点 → 已纠正观点」，只允许 active/archived）
export type CorrectionStatus = 'active' | 'archived'

export interface Correction {
  id: string
  title: string
  incorrectClaim: string
  correctedClaim: string
  keywords: string[]
  sourceIds: string[]
  status: CorrectionStatus
  createdAt: string
  updatedAt: string
}

export interface CorrectionCreatePayload {
  title: string
  incorrectClaim: string
  correctedClaim: string
  sourceIds: string[]
}

// P14-10：内容生成草稿（基于所选来源生成，草稿不进入检索/问答，直到用户另存为卡片）
export type GenerationType = 'study_note' | 'article_summary' | 'podcast_script'

export interface GenerationCitation {
  sourceType: 'material' | 'knowledge'
  id: string
  title: string
}

export interface GenerationResult {
  draftId: string
  content: string
  citations: GenerationCitation[]
  status: 'ok'
}

export interface CreateKnowledgeFromDraftResult {
  item: KnowledgeCard
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS },
    body: JSON.stringify(body),
  })
}

function putJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS },
    body: JSON.stringify(body),
  })
}

// =====================================================================
// P1 模型与运行时（§6 system-models 管理 API）
// =====================================================================

export interface OllamaHealth {
  reachable: boolean
  version: string | null
  modelInstalled: boolean
  modelRunning: boolean
  checkedAt: string
}

export interface MaterialRuntimeConfig {
  revision: number
  baseUrl: string
  model: string
  timeoutSeconds: number
  source: 'defaults' | 'runtime_settings'
  appliesTo: string[]
  health: OllamaHealth
}

export interface MaterialRuntimePutPayload {
  baseUrl: string
  model: string
  timeoutSeconds: number
  revision?: number | null
}

export interface MaterialRuntimeTestPayload {
  baseUrl?: string
  model?: string
  timeoutSeconds?: number
}

export interface RuntimeTestResult {
  ok: boolean
  model: string
  latencyMs: number
  errorCode?: string
  modelInstalled?: boolean
  models?: string[]
  testType?: 'connectivity' | 'inference'
}

export interface ChatProviderConfig {
  revision: number
  provider: 'ollama' | 'openai'
  externalEnabled: boolean
  baseUrl: string | null
  model: string | null
  apiKeyConfigured: boolean
  apiKeyHint: string | null
  timeoutSeconds: number
  totalBudgetSeconds: number
  fallbackOllama: boolean
  source: 'defaults' | 'runtime_settings'
  effectiveProvider: 'ollama' | 'openai'
}

export interface ChatProviderPutPayload {
  provider: 'ollama' | 'openai'
  externalEnabled: boolean
  baseUrl?: string | null
  model?: string | null
  timeoutSeconds: number
  totalBudgetSeconds: number
  fallbackOllama: boolean
  apiKey?: string | null
  clearApiKey?: boolean
  revision?: number | null
}

export interface ChatProviderTestPayload {
  provider?: 'ollama' | 'openai'
  externalEnabled?: boolean
  baseUrl?: string | null
  model?: string | null
  apiKey?: string | null
  timeoutSeconds?: number
  totalBudgetSeconds?: number
  fallbackOllama?: boolean
}

export interface ChatProviderTestResult {
  ok: boolean
  provider: string
  model: string
  latencyMs: number
  errorCode?: string
}

// =====================================================================
// P2 模型任务 / 运行监控（§7 / §8 system-models 管理 API）
// =====================================================================
// 资源采样统一约定「能力探测而非平台假设」：available=false 表示该能力不可用、
// 未采样或暂时失败，绝不据此伪造数值 0（§8）。前端需区分「0、未采样、不可用」三类。

export interface OllamaModel {
  name: string
  sizeBytes: number | null
  modifiedAt: string | null
  family: string | null
  parameterSize: string | null
  quantization: string | null
  running: boolean
}

export interface MaterialModelsResponse {
  reachable: boolean
  errorCode?: string
  models: OllamaModel[]
}

export interface MonitorCpu {
  available: boolean
  value?: number | null
  stale?: boolean
  errorCode?: string
}

export interface MonitorMemory {
  available: boolean
  totalBytes?: number
  availableBytes?: number
  usedPercent?: number | null
  errorCode?: string
}

export interface MonitorGpu {
  available: boolean
  name?: string
  utilizationPercent?: number
  memoryUsedBytes?: number
  memoryTotalBytes?: number
  errorCode?: string
  errorMessageSafe?: string
}

export interface MindosPipelineMonitor {
  materialQueue: Record<string, number>
  ollamaScheduler: { running: boolean; workers: number; maxWorkers: number; queued: number; deduplicatedPending: number }
  cardIndexQueue: { states: Record<string, number>; total: number }
  indexHealth: string
  legacyMaterialRag: { legacyReadEnabled: boolean; cleanupPlans: Record<string, number> }
}

export interface MonitorOllama {
  available: boolean
  reachable: boolean
  version?: string | null
  runningCount?: number
  installedCount?: number
  errorCode?: string
}

export interface MonitorResource {
  sampledAt: string
  cpu: MonitorCpu
  memory: MonitorMemory
  gpu: MonitorGpu
  ollama: MonitorOllama
}

// 模型任务状态机（与后端 model_job_store 对齐）
export type ModelJobState =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancel_requested'
  | 'cancelled'
export type ModelJobType = 'pull' | 'load' | 'unload'

export interface ModelJob {
  jobId: string
  type: ModelJobType
  state: ModelJobState
  targetModel: string
  progressCurrent?: number
  progressTotal?: number
  attempts?: number
  errorCode?: string
  errorMessageSafe?: string
  createdAt?: string
  startedAt?: string
  finishedAt?: string
}

export interface MonitorResponse {
  sampledAt: string
  resource: MonitorResource
  indexQueue: { active: number | null; total: number | null }
  modelJobs: ModelJob[]
  worker: { running: boolean }
}

export interface ListModelJobsResponse {
  items: ModelJob[]
  nextCursor?: string | null
}

export interface ModelActionResponse {
  jobId: string
  state: ModelJobState
  deduplicated: boolean
}

export const api = {
  health: () => request<HealthInfo>('/health'),
  mindosAccessContext: () => request<MindosAccessContext>('/mindos/access-context'),
  // 后端同一套导入校验规则（与 mindos.validation.validate_import 一致）。
  // P1 前端本地校验用于即时反馈；P2 “开始上传”前将用此接口批量复核，避免仅依赖浏览器。
  validateImport: (filename: string, size: number) =>
    postJson<ImportValidationResult>('/mindos/validate', { filename, size }),
  // P2：真实上传 + 进入处理链路；校验失败由后端拒绝（不落盘、不建任务）
  // P14-06：folderId 为目录树节点 ID（null/省略 = 未分类）
  uploadFile: (file: File, folderId?: number | null) => {
    const form = new FormData()
    form.append('file', file)
    if (folderId != null && folderId > 0) form.append('folderId', String(folderId))
    return request<UploadResult>('/mindos/uploads', { method: 'POST', headers: CSRF_HEADERS, body: form })
  },
  // P2：轮询处理状态
  getUploadStatus: (materialId: string) => request<UploadResult>(`/mindos/uploads/${materialId}`),
  // P2：失败后重试（重新进入处理流程）
  retryUpload: (materialId: string) => postJson<UploadResult>(`/mindos/uploads/${materialId}/retry`, {}),
  // 服务中断后显式继续持久化的暂停任务。
  resumeUpload: (materialId: string) => postJson<UploadResult>(`/mindos/uploads/${materialId}/resume`, {}),
  listMaterials: (params: { type?: string; status?: string; keyword?: string; folderId?: number; folder?: string; tag?: string; recycled?: boolean } = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value))
    }
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<MaterialListResponse>(`/mindos/materials${suffix}`)
  },
  getMaterialDetail: (materialId: string) => request<MaterialDetail>(`/mindos/materials/${encodeURIComponent(materialId)}`),
  getMaterialDraftCard: (materialId: string) => request<MaterialDraftCard & { materialId: string }>(`/mindos/materials/${encodeURIComponent(materialId)}/draft-card`),
  saveMaterialDraftCard: (materialId: string, payload: { expectedRevision: string; title: string; content: string }) =>
    putJson<MaterialDraftCard & { materialId: string }>(`/mindos/materials/${encodeURIComponent(materialId)}/draft-card`, payload),
  confirmMaterialDraftCard: (materialId: string, expectedRevision: string, idempotencyKey: string) =>
    request<{ materialId: string; knowledgeId: string; vectorJobId?: string; idempotent: boolean }>(`/mindos/materials/${encodeURIComponent(materialId)}/draft-card/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS, 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ expectedRevision }),
    }),
  getMaterialImpact: (materialId: string) => request<MaterialImpact>(`/mindos/materials/${encodeURIComponent(materialId)}/impact`),
  listMaterialVersions: (materialId: string) => request<{ materialId: string; items: UploadResult[] }>(`/mindos/materials/${encodeURIComponent(materialId)}/versions`),
  uploadMaterialVersion: (materialId: string, file: File, versionNote = '', targetFolderId?: number | null) => {
    const form = new FormData()
    form.append('file', file)
    if (versionNote.trim()) form.append('versionNote', versionNote.trim())
    if (targetFolderId != null) form.append('targetFolderId', String(targetFolderId))
    return request<MaterialVersionUploadResult>(`/mindos/materials/${encodeURIComponent(materialId)}/versions`, { method: 'POST', headers: CSRF_HEADERS, body: form })
  },
  getMaterialVersionImpact: (materialId: string) => request<MaterialImpact>(`/mindos/materials/${encodeURIComponent(materialId)}/version-impact`),
  getMaterialSummary: (materialId: string) => request<{ materialId: string; text: string; status: SummaryStatus; generatedAt: string | null }>(`/mindos/materials/${encodeURIComponent(materialId)}/summary`),
  // P14-04：聚合分析（摘要 / 标签候选 / 实体及其状态）
  getMaterialAnalysis: (materialId: string) => request<MaterialAnalysis>(`/mindos/materials/${encodeURIComponent(materialId)}/analysis`),
  reparseMaterial: (materialId: string) =>
    postJson<MaterialAnalysis>(`/mindos/materials/${encodeURIComponent(materialId)}/regenerate`, { item: 'parse' }),
  // P14-04：读取异步缓存的标签候选；缺失时触发后台重算并返回 pending
  getMaterialTagSuggestions: (materialId: string) => request<MaterialTagSuggestions>(`/mindos/materials/${encodeURIComponent(materialId)}/tag-suggestions`),
  // P14-04：确认候选 → 写入正式标签（后端校验 suggestionId 归属并审计；幂等）
  confirmTagSuggestion: (materialId: string, suggestionId: string) =>
    postJson<TagSuggestionConfirmResponse>(`/mindos/materials/${encodeURIComponent(materialId)}/tag-suggestions/${encodeURIComponent(suggestionId)}/confirm`, {}),
  setMaterialTags: (materialId: string, tags: string[], action: 'add' | 'remove') =>
    postJson<{ tags: string[] }>(`/mindos/materials/${encodeURIComponent(materialId)}/tags`, { tags, action }),
  getMaterialRelated: (materialId: string) => request<RelatedResult>(`/mindos/materials/${encodeURIComponent(materialId)}/related`),
  listKnowledge: (params: { q?: string; tag?: string; folderId?: number; recycled?: boolean } = {}) => {
    const query = new URLSearchParams()
    if (params.q) query.set('q', params.q)
    if (params.tag) query.set('tag', params.tag)
    // P15-05：回收站入口（recycled=true 仅返回已回收卡片）
    if (params.recycled !== undefined) query.set('recycled', String(params.recycled))
    // P14-07：KNOWLEDGE 目录 ID；提供时仅返回归入该目录（含全部后代子树）的卡片
    if (params.folderId != null) query.set('folderId', String(params.folderId))
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<{ items: KnowledgeCard[]; total: number }>(`/mindos/knowledge${suffix}`)
  },
  getKnowledge: (id: string) => request<KnowledgeCard>(`/mindos/knowledge/${encodeURIComponent(id)}`),
  beginKnowledgeEditDraft: (id: string, expectedRevision: string) =>
    postJson<KnowledgeEditDraft>(`/mindos/knowledge/${encodeURIComponent(id)}/edit-draft`, { expectedRevision }),
  getKnowledgeEditDraft: (id: string) => request<KnowledgeEditDraft>(`/mindos/knowledge/${encodeURIComponent(id)}/edit-draft`),
  saveKnowledgeEditDraft: (id: string, payload: KnowledgeEditDraftSave) =>
    request<KnowledgeEditDraft>(`/mindos/knowledge/${encodeURIComponent(id)}/edit-draft`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS }, body: JSON.stringify(payload),
    }),
  confirmKnowledgeEditDraft: (id: string, expectedDraftRevision: string, idempotencyKey: string) =>
    request<{ knowledgeId: string; vectorJobId?: string; idempotent: boolean; indexState: 'updating' }>(`/mindos/knowledge/${encodeURIComponent(id)}/edit-draft/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS, 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ expectedDraftRevision }),
    }),
  confirmKnowledge: (id: string, expectedRevision: string, idempotencyKey: string) =>
    request<{ knowledgeId: string; vectorJobId?: string; idempotent: boolean; indexState: 'indexing' }>(`/mindos/knowledge/${encodeURIComponent(id)}/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS, 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ expectedRevision }),
    }),
  retryKnowledgeEditDraft: (id: string) =>
    postJson<{ knowledgeId: string; vectorJobId?: string; indexState: 'updating' }>(`/mindos/knowledge/${encodeURIComponent(id)}/edit-draft/retry`, {}),
  retryKnowledgeIndex: (id: string, expectedRevision: string) =>
    postJson<{ knowledgeId: string; vectorJobId?: string; indexState: string }>(`/mindos/knowledge/${encodeURIComponent(id)}/retry-index`, { expectedRevision }),
  // P15-01：读取 / 整表替换知识卡片来源（独立接口，与正文保存解耦）
  getKnowledgeSources: (id: string) => request<KnowledgeSourcesResponse>(`/mindos/knowledge/${encodeURIComponent(id)}/sources`),
  putKnowledgeSources: (id: string, payload: KnowledgeSourcesUpdatePayload) =>
    request<KnowledgeSourcesResponse>(`/mindos/knowledge/${encodeURIComponent(id)}/sources`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS }, body: JSON.stringify(payload),
    }),
  createKnowledge: (title: string, content = '', tags: string[] = [], folderId?: number | null) =>
    postJson<{ item: KnowledgeCard }>('/mindos/knowledge', { title, content, tags, folderId: folderId ?? null }),
  updateKnowledge: (id: string, title: string, content: string, tags: string[] = [], folderId?: number | null, expectedRevision?: string) => request<{ item: KnowledgeCard }>(`/mindos/knowledge/${encodeURIComponent(id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS }, body: JSON.stringify({ title, content, tags, folderId: folderId ?? null, expectedRevision }),
  }),
  // P14-07：移动知识卡片到 KNOWLEDGE 目录（folderId=null 移回资源根目录）
  moveKnowledge: (id: string, folderId: number | null, expectedRevision?: string) =>
    postJson<{ item: KnowledgeCard }>(`/mindos/knowledge/${encodeURIComponent(id)}/move`, { folderId, expectedRevision }),
  setKnowledgeTags: (id: string, tags: string[], action: 'add' | 'remove') =>
    postJson<{ tags: string[] }>(`/mindos/knowledge/${encodeURIComponent(id)}/tags`, { tags, action }),
  getKnowledgeRelated: (id: string) => request<RelatedResult>(`/mindos/knowledge/${encodeURIComponent(id)}/related`),
  getGraph: () => request<GraphData>('/mindos/graph'),
  search: (query: string) => request<UnifiedSearchResult>(`/mindos/search?q=${encodeURIComponent(query)}`),
  askQuestion: (question: string) => postJson<QaResponse>('/mindos/qa', { question }),
  // P14-12：纠错本（用户确认的「错误观点 → 已纠正观点」）
  listCorrections: (status?: 'active' | 'archived') => {
    const suffix = status ? `?status=${status}` : ''
    return request<{ items: Correction[] }>(`/mindos/corrections${suffix}`)
  },
  createCorrection: (payload: CorrectionCreatePayload) => postJson<Correction>('/mindos/corrections', payload),
  getCorrection: (corrId: string) => request<Correction>(`/mindos/corrections/${encodeURIComponent(corrId)}`),
  updateCorrection: (corrId: string, payload: Partial<CorrectionCreatePayload>) =>
    request<Correction>(`/mindos/corrections/${encodeURIComponent(corrId)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS }, body: JSON.stringify(payload),
    }),
  archiveCorrection: (corrId: string) => postJson<Correction>(`/mindos/corrections/${encodeURIComponent(corrId)}/archive`, {}),
  // P14-10：基于所选资料 / 知识卡片生成内容草稿（学习笔记 / 文章摘要 / 播客脚本）
  createGeneration: (type: GenerationType, sourceIds: string[], instruction = '') =>
    postJson<GenerationResult>('/mindos/generations', { type, sourceIds, instruction }),
  // P14-10：草稿「另存为知识卡片」（仅用户主动调用；来源 ID 写入 frontmatter）
  createKnowledgeFromDraft: (draftId: string, payload: { title?: string; content: string; tags?: string[] }) =>
    postJson<CreateKnowledgeFromDraftResult>(`/mindos/generations/${encodeURIComponent(draftId)}/create-knowledge`, payload),
  listGovernance: (params: { status?: string; kind?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.status) query.set('status', params.status)
    if (params.kind) query.set('kind', params.kind)
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<GovernanceListResponse>(`/mindos/governance${suffix}`)
  },
  getGovernanceStats: () => request<GovernanceStats>('/mindos/governance/stats'),
  rescanGovernance: () => postJson<{ scanned: number; created: number }>('/mindos/governance/rescan', {}),
  resolveGovernance: (id: string, action: 'ignore' | 'merge', note = '', keepKnowledgeId?: string) =>
    postJson<GovernanceItem>(`/mindos/governance/${encodeURIComponent(id)}/resolve`, { action, note, keepKnowledgeId }),
  getHome: () => request<HomeOverview>('/mindos/home'),
  getGrowthCharter: () => request<GrowthCharterHistory>('/mindos/growth/charter'),
  saveGrowthCharter: (payload: GrowthCharterPayload) =>
    postJson<GrowthCharter>('/mindos/growth/charter', payload),
  listGrowthDecisions: (status?: GrowthDecisionStatus) => {
    const suffix = status ? `?status=${encodeURIComponent(status)}` : ''
    return request<GrowthDecisionList>(`/mindos/growth/decisions${suffix}`)
  },
  createGrowthDecision: (payload: GrowthDecisionPayload) =>
    postJson<GrowthDecision>('/mindos/growth/decisions', payload),
  recordGrowthDecisionOutcome: (decisionId: string, payload: RecordGrowthOutcomePayload) =>
    postJson<GrowthDecision>(`/mindos/growth/decisions/${encodeURIComponent(decisionId)}/outcome`, payload),
  createGrowthReview: (payload: GrowthReviewPayload) =>
    postJson<GrowthReviewResult>('/mindos/growth/reviews', payload),
  getGrowthToday: () => request<GrowthToday>('/mindos/growth/today'),
  moveMaterial: (materialId: string, folderId: number | null) =>
    postJson<{ materialId: string; folderId: number | null; folder: string }>(`/mindos/materials/${encodeURIComponent(materialId)}/move`, { folderId }),
  // P14-06：目录树 API（ID 驱动；旧字符串 folders API 已由后端废弃移除）
  listFolderNodes: (scope = 'RAW') => request<FolderListResponse>(`/mindos/folders?scope=${encodeURIComponent(scope)}`),
  createFolderNode: (name: string, parentId?: number | null, scope = 'RAW') =>
    postJson<FolderNode>('/mindos/folders', { name, parentId: parentId ?? null, scope }),
  renameFolderNode: (folderId: number, name: string) =>
    request<FolderNode>(`/mindos/folders/${folderId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS }, body: JSON.stringify({ name }) }),
  moveFolderNode: (folderId: number, parentId: number | null) =>
    postJson<FolderNode>(`/mindos/folders/${folderId}/move`, { parentId }),
  deleteFolderNode: (folderId: number, opts: { targetFolderId?: number; moveToRoot?: boolean } = {}) => {
    const query = new URLSearchParams()
    if (opts.targetFolderId != null) query.set('targetFolderId', String(opts.targetFolderId))
    if (opts.moveToRoot) query.set('moveToRoot', 'true')
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<FolderDeleteResult>(`/mindos/folders/${folderId}${suffix}`, { method: 'DELETE', headers: CSRF_HEADERS })
  },
  cancelUpload: (materialId: string) => postJson<UploadResult>(`/mindos/uploads/${encodeURIComponent(materialId)}/cancel`, {}),
  removeMaterialFromQueue: (materialId: string) =>
    request<{ materialId: string; removed: boolean }>(`/mindos/materials/${encodeURIComponent(materialId)}/queue`, { method: 'DELETE', headers: CSRF_HEADERS }),
  getKnowledgeTagSuggestions: (id: string) => request<{ knowledgeId: string; suggestions: string[] }>(`/mindos/knowledge/${encodeURIComponent(id)}/tag-suggestions`),
  // P15-04/05：删除影响预览 + 受控回收 / 恢复 / 永久清除
  getMaterialDeletionImpact: (materialId: string) =>
    request<DeletionImpact>(`/mindos/materials/${encodeURIComponent(materialId)}/deletion-impact`),
  getKnowledgeDeletionImpact: (knowledgeId: string) =>
    request<DeletionImpact>(`/mindos/knowledge/${encodeURIComponent(knowledgeId)}/deletion-impact`),
  recycleMaterial: (materialId: string, payload: DeletionExecutePayload) =>
    postJson<LifecycleExecuteResult>(`/mindos/materials/${encodeURIComponent(materialId)}/recycle`, payload),
  unrecycleMaterial: (materialId: string) =>
    postJson<LifecycleExecuteResult>(`/mindos/materials/${encodeURIComponent(materialId)}/unrecycle`, {}),
  purgeMaterial: (materialId: string, payload: DeletionExecutePayload) =>
    postJson<LifecycleExecuteResult>(`/mindos/materials/${encodeURIComponent(materialId)}/purge`, payload),
  recycleKnowledge: (knowledgeId: string, payload: DeletionExecutePayload) =>
    postJson<LifecycleExecuteResult>(`/mindos/knowledge/${encodeURIComponent(knowledgeId)}/recycle`, payload),
  unrecycleKnowledge: (knowledgeId: string) =>
    postJson<LifecycleExecuteResult>(`/mindos/knowledge/${encodeURIComponent(knowledgeId)}/unrecycle`, {}),
  purgeKnowledge: (knowledgeId: string, payload: DeletionExecutePayload) =>
    postJson<LifecycleExecuteResult>(`/mindos/knowledge/${encodeURIComponent(knowledgeId)}/purge`, payload),
  // P1（§6）：模型与运行时管理。GET/PUT 支持 revision 乐观锁；test 提交表单暂存值不持久化。
  getMaterialRuntime: () => request<MaterialRuntimeConfig>('/system/models/material-runtime'),
  putMaterialRuntime: (payload: MaterialRuntimePutPayload) =>
    putJson<MaterialRuntimeConfig>('/system/models/material-runtime', payload),
  testMaterialRuntime: (payload: MaterialRuntimeTestPayload) =>
    postJson<RuntimeTestResult>('/system/models/material-runtime/test', payload),
  testMaterialRuntimeInference: (payload: MaterialRuntimeTestPayload) =>
    postJson<RuntimeTestResult>('/system/models/material-runtime/test-inference', payload),
  getChatProvider: () => request<ChatProviderConfig>('/system/models/chat-provider'),
  putChatProvider: (payload: ChatProviderPutPayload) =>
    putJson<ChatProviderConfig>('/system/models/chat-provider', payload),
  testChatProvider: (payload: ChatProviderTestPayload) =>
    postJson<ChatProviderTestResult>('/system/models/chat-provider/test', payload),
  // P2（§7 / §8）：运行监控 / 模型任务。全部走 require_local 管理路由，统一错误体。
  getMaterialModels: () => request<MaterialModelsResponse>('/system/models/material-runtime/models'),
  getMonitor: () => request<MonitorResponse>('/system/models/monitor'),
  getMindosPipelineMonitor: () => request<MindosPipelineMonitor>('/system/mindos-pipeline/status'),
  listModelJobs: (params: { state?: string; type?: ModelJobType; cursor?: string; limit?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.state) query.set('state', params.state)
    if (params.type) query.set('type', params.type)
    if (params.cursor) query.set('cursor', params.cursor)
    if (params.limit) query.set('limit', String(params.limit))
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<ListModelJobsResponse>(`/system/models/jobs${suffix}`)
  },
  getModelJob: (jobId: string) => request<ModelJob>(`/system/models/jobs/${encodeURIComponent(jobId)}`),
  cancelModelJob: (jobId: string) =>
    postJson<ModelJob>(`/system/models/jobs/${encodeURIComponent(jobId)}/cancel`, {}),
  pullModel: (model: string) => postJson<ModelActionResponse>('/system/models/material-runtime/pull', { model }),
  loadModel: (model: string) => postJson<ModelActionResponse>('/system/models/material-runtime/load', { model }),
  unloadModel: (model: string) => postJson<ModelActionResponse>('/system/models/material-runtime/unload', { model }),
}

// =====================================================================
// 阶段 2：连接会话（受控投放 + 一次性票据交换）
//
// MindOS 前端不承担账号/Owner/认领控制面。登录、设备认领、连接票据的创建
// 全部由 App/Electron 的 Consumer Client 在宿主侧完成，然后经受控通道把
// 短期一次性连接票据投放给本模块（window.__MINDOS_ACCESS__.getTicket()）。
// 本模块只做 MindOS 自身的会话交换（/mindos/connectivity/sessions/exchange）
// 并把凭证保留在内存中。这样前端产物不再包含任何 Mock Consumer、固定验证码、
// 登录/认领/创建连接的调用——上述逻辑均不允许进入发布包（构建守卫复核）。
// =====================================================================

/** 宿主（App/Electron Consumer Client）注入的受控票据访问器；未注入返回 null。 */
interface HostConnectivityBridge {
  /**
   * 取一条短期一次性连接票据；无可用票据（本地调试/未就绪）返回 null。
   * 宿主桥实现可为异步（如 Electron preload 的 ipcRenderer.invoke 返回 Promise），
   * 消费方必须 await。
   */
  getTicket(): Promise<string | null> | string | null
}

export interface SessionExchangeResult {
  sessionToken: string
  sessionId: string
  deviceId: string
  accountId: string
  clientId: string
  epochGeneration: number
  expiresAt: number
}

/** 从受控通道读取连接票据：优先宿主桥（App/Electron），无宿主/无票据时返回 null。
 *  宿主桥可为异步（preload 的 ipcRenderer.invoke 返回 Promise），此处一律 await。 */
async function readConnectivityTicket(): Promise<string | null> {
  const host = (window as unknown as { __MINDOS_ACCESS__?: HostConnectivityBridge }).__MINDOS_ACCESS__
  if (host && typeof host.getTicket === 'function') {
    const ticket = await host.getTicket()
    if (ticket && typeof ticket === 'string' && ticket) return ticket
  }
  return null
}

export async function exchangeTicketForSession(ticket: string): Promise<SessionExchangeResult> {
  return request<SessionExchangeResult>('/mindos/connectivity/sessions/exchange', {
    method: 'POST',
    headers: { Authorization: `Bearer ${ticket}` },
  })
}

/**
 * 在票据模式下用宿主注入的一次性票据建立 MindOS 会话（幂等）。
 * 本模块绝不执行登录/认领/换票创建——那些归属 App/Electron 的 Consumer Client。
 * 本机调试模式或宿主未注入票据时返回 null，页面不阻塞、不弹错。
 */
export async function provisionMindosSession(): Promise<{ deviceId: string } | null> {
  const ctx = await api.mindosAccessContext()
  if (ctx.mode !== 'connectivity_ticket_required') return null
  const ticket = await readConnectivityTicket()
  if (!ticket) return null // 无受控票据（如浏览器独立调试），静默跳过
  const exchanged = await exchangeTicketForSession(ticket)
  setMindosSessionToken(exchanged.sessionToken)
  return { deviceId: exchanged.deviceId }
}

// =====================================================================
// 知君 P1：对话 · 本体 · 确认（契约见 docs/development/zhijun-api-contract.md）
// =====================================================================

export type ConversationMode = 'chat' | 'onboarding' | 'review'
export type TurnMode = 'chat' | 'deliberate'

export interface Conversation {
  id: string
  title: string
  mode: ConversationMode
  status: 'active' | 'archived'
  decisionId: string | null
  messageCount: number
  createdAt: string
  updatedAt: string
  lastMessageAt: string | null
}

export type MessageRole = 'user' | 'assistant' | 'system'
export type MessageStatus = 'complete' | 'aborted' | 'error'

export interface Message {
  id: string
  conversationId: string
  seq: number
  role: MessageRole
  content: string
  status: MessageStatus
  provider?: string | null
  model?: string | null
  external?: boolean
  meta?: Record<string, unknown>
  // 历史回复：后端由本轮回执还原的出处（与 SSE provenance 同形，多 fromReceipt / channel）
  provenance?: (ProvenanceEvent & { fromReceipt?: boolean; channel?: string }) | null
  createdAt: string
}

export interface ConversationDetail {
  conversation: Conversation
  messages: Message[]
  decisionDraft?: DecisionDraft
  decision?: GrowthDecision | null
}

// ---- P2：判断草稿 / 提醒（契约 §6–§8）----
export interface DecisionDraftFields {
  title: string
  context: string
  options: string[]
  leaning: string | null
  choice: string | null
  rationale: string | null
  confidence: number | null
  expectedOutcome: string | null
  reviewAt: string | null
  keyQuestion: string | null
  zhijunView: string | null
  relatedEntityIds: string[]
  // 和这件事相似的、用户过去记下的判断（后端按标题 / 选择 / 经验的词面相似度挑出，最多 3 个）
  relatedDecisionIds?: string[]
  evidenceRefs: string[]
  userQuotes: string[]
}

export interface DecisionDraft {
  id: string
  conversationId: string
  messageId: string | null
  revision: number
  status: 'draft' | 'confirmed' | 'discarded'
  decisionId: string | null
  fields: DecisionDraftFields
  createdAt: string
  updatedAt: string
}

export interface DecisionDraftEvent {
  // ready：演示模型同步整理好了；queued：真实模型下草稿是后台任务，fields 为空，前端轮询 GET /decision-draft
  state?: 'ready' | 'queued'
  jobId?: string | null
  draftId: string | null
  revision: number | null
  status: DecisionDraft['status']
  fields: DecisionDraftFields | null
  changedFields: string[]
}

export interface DecisionDraftConfirmPayload {
  choice?: string
  rationale?: string
  confidence?: number
  expectedOutcome?: string
  reviewAt?: string
  title?: string
  options?: string[]
}

export interface Nudge {
  id: string
  kind: 'review_due' | 'commitment_due' | 'checkin' | 'principle_tension' | 'weekly_review'
  triggerKey: string
  triggerRef: { decisionId?: string; title?: string; principleId?: string; actionId?: string; claimId?: string; section?: string; summary?: string; weekStart?: string }
  whyNow: string
  message: string
  status: 'pending' | 'shown' | 'acted' | 'dismissed' | 'silenced'
  scheduledFor: string
  createdAt: string
}

export interface NudgePolicy {
  enabled: boolean
  maxPerDay: number
  silencedRefs: string[]
}

export interface TurnReceipt {
  messageId: string
  conversationId: string
  provider: string
  model: string
  external: boolean
  confirmedClaimIds: string[]
  workingClaimIds: string[]
  materialChunkKeys: string[]
  retractedNoticeCount: number
  promptChars: number
  extractionProvider: string | null
  createdAt: string
}

export type Section = 'who' | 'people' | 'matters' | 'principles' | 'ways' | 'direction'
export type Layer = 'observed' | 'self_declared' | 'aspirational' | 'hypothesis'
export type TrustState = 'working' | 'confirmed' | 'retracted' | 'superseded'
export type TrustOrigin = 'utterance' | 'user_confirm' | 'user_edit' | 'user_created' | 'material' | 'model'
export type ReviewAction = 'confirm' | 'partial' | 'context_only' | 'reject' | 'defer' | 'retract' | 'reaffirm'
export type ReviewSurface = 'conversation' | 'ontology_page' | 'onboarding'

export interface ClaimBrief {
  id: string
  content: string
  section: Section
  layer: Layer
}

export interface ClaimEvidence {
  id: string
  kind: 'conversation_turn' | 'material_span' | 'user_edit' | 'decision' | 'review'
  stance: 'supports' | 'contradicts' | 'background'
  conversationId: string | null
  messageId: string | null
  materialId: string | null
  chunkKey: string | null
  quote: string
  createdAt: string
}

export interface Claim {
  id: string
  subjectEntityId: string
  subjectName: string
  predicate: string
  objectEntityId: string | null
  objectName: string | null
  content: string
  section: Section
  layer: Layer
  trustState: TrustState
  trustOrigin: TrustOrigin
  confidence: number
  scope: 'long_term' | 'context_only'
  contextRef: string | null
  privacyLevel: 'public' | 'private' | 'sensitive' | 'restricted'
  exportAllowed: boolean
  firstSeen: string
  lastReaffirmed: string
  supersedesId: string | null
  supersededById: string | null
  retractedAt: string | null
  retractionReason: string | null
  challenged: boolean
  challengeNote?: string | null
  promotionReady?: boolean
  deferredUntil: string | null
  evidence: ClaimEvidence[]
}

// P3：整合与裁决
export interface MergeProposal {
  id: string
  fromEntityId: string
  intoEntityId: string
  fromName: string | null
  intoName: string | null
  reason: string
  score: number
  status: 'pending' | 'accepted' | 'rejected'
  createdAt: string
}

export interface Conflict {
  id: string
  kind: 'contradiction' | 'tension'
  claimA: Claim
  claimB: Claim
  verdictBy: string
  note: string
  status: 'pending' | 'resolved' | 'dismissed'
  resolution: 'a' | 'b' | 'both' | null
  createdAt: string
}

export interface ConsolidateReport {
  mergeProposals: number
  challenged: number
  conflicts: number
  merged: number
  tensions: number
  promoted: number
  decayed: number
  deferred: number
  pairsJudged: number
}

export interface OntologyExport {
  exportedAt: string
  schemaVersion: string
  entities: OntologyEntity[]
  claims: Claim[]
  reviewEvents: unknown[]
}

export interface PurgeResult {
  ontology: { purged: boolean; claims: number; entities: number }
  conversations?: { conversations: number; messages: number }
}

export interface ReviewRequest {
  action: ReviewAction
  editedContent?: string
  contextRef?: string
  note?: string
  surface: ReviewSurface
  conversationId?: string
  messageId?: string
}

export interface ReviewResult {
  claim: Claim
  replacedBy?: Claim
}

export interface ClaimCreatePayload {
  content: string
  // 省略时由后端归类（按句子里的人 / 事 / 原则 / 愿望等线索）
  section?: Section
  layer?: 'self_declared' | 'aspirational'
  predicate?: string
}

export interface OntologyEntity {
  id: string
  type: 'me' | 'person' | 'organization' | 'project' | 'place' | 'topic' | 'event' | 'term'
  canonicalName: string
  aliases: string[]
  description: string
  status: 'active' | 'merged' | 'retracted'
  claimCount: number
}

export interface OntologyStats {
  hasOntology: boolean
  entities: number
  claims: { working: number; confirmed: number; retracted: number; superseded: number }
  bySection: Record<Section, { confirmed: number; working: number }>
  inbox: number
  proposals?: number
}

export interface OntologyProjection {
  markdown: string
  exportableMarkdown: string
  generatedAt: string
}

export interface ZhijunStatus {
  provider: 'fake' | 'ollama' | 'openai' | 'anthropic'
  model: string
  external: boolean
  extraction: 'enabled' | 'beta' | 'disabled'
  workerRunning: boolean
  // 后台还没跑完的整理任务数（抽取 / 草稿 / 摘要 / 第一次观察）
  pendingJobs?: number
}

// SSE 事件载荷（POST /mindos/conversations/{id}/messages）
export interface TurnMetaEvent {
  messageId: string
  userMessageId: string
  conversationId: string
  provider: string
  model: string
  external: boolean
  mode: ConversationMode
  turnMode?: TurnMode
  depth: 'brief' | 'deep'
  decisionId?: string | null
  // 建档会话：本轮在问第几个问题（1–7），8 = 收尾；其它模式为 null
  onboardingStep?: number | null
}

export interface ProvenanceMaterial {
  materialId: string
  title: string
  chunkKey?: string
  locator?: Record<string, unknown>
}

export interface ProvenanceEvent {
  confirmedClaims: ClaimBrief[]
  workingClaims: ClaimBrief[]
  materials: ProvenanceMaterial[]
  retractedNotices: number
  charterVersion: number | null
  promptChars: number
}

export interface ExtractionEvent {
  state: 'queued' | 'skipped'
  jobId?: string
}

export interface MessageDoneEvent {
  messageId: string
  status: MessageStatus
  usage?: Record<string, unknown>
  receiptId: string
}

export interface StreamErrorEvent {
  code: string
  message: string
  retryable: boolean
}

export function createConversation(payload: { mode?: ConversationMode; title?: string; decisionId?: string } = {}) {
  return postJson<Conversation>('/mindos/conversations', payload)
}

export function getDecisionDraft(conversationId: string) {
  return request<DecisionDraft>(`/mindos/conversations/${encodeURIComponent(conversationId)}/decision-draft`)
}

export function confirmDecisionDraft(conversationId: string, payload: DecisionDraftConfirmPayload) {
  return postJson<{ draft: DecisionDraft; decision: GrowthDecision }>(
    `/mindos/conversations/${encodeURIComponent(conversationId)}/decision-draft/confirm`,
    payload,
  )
}

export function discardDecisionDraft(conversationId: string) {
  return postJson<DecisionDraft>(`/mindos/conversations/${encodeURIComponent(conversationId)}/decision-draft/discard`, {})
}

export function recordConversationOutcome(conversationId: string, payload: { result: string; notes?: string }) {
  return postJson<{ decision: GrowthDecision; nudgesActed: number }>(
    `/mindos/conversations/${encodeURIComponent(conversationId)}/outcome`,
    { result: payload.result, notes: payload.notes ?? '' },
  )
}

export function getNudgesToday() {
  return request<{ items: Nudge[]; policy: NudgePolicy }>('/mindos/nudges/today')
}

export function scanNudges() {
  return postJson<{ created: number; checked: number }>('/mindos/nudges/scan', {})
}

export function dismissNudge(nudgeId: string) {
  return postJson<Nudge>(`/mindos/nudges/${encodeURIComponent(nudgeId)}/dismiss`, {})
}

export function silenceNudge(nudgeId: string) {
  return postJson<{ nudge: Nudge; policy: NudgePolicy }>(`/mindos/nudges/${encodeURIComponent(nudgeId)}/silence`, {})
}

export function getNudgePolicy() {
  return request<NudgePolicy>('/mindos/nudges/policy')
}

export function putNudgePolicy(payload: Partial<NudgePolicy>) {
  return putJson<NudgePolicy>('/mindos/nudges/policy', payload)
}

export function listConversations(limit = 50) {
  return request<{ items: Conversation[] }>(`/mindos/conversations?limit=${limit}`)
}

export function getConversation(conversationId: string) {
  return request<ConversationDetail>(`/mindos/conversations/${encodeURIComponent(conversationId)}`)
}

export function deleteConversation(conversationId: string) {
  return request<{ id: string; deleted: boolean }>(`/mindos/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'DELETE',
    headers: CSRF_HEADERS,
  })
}

export function getTurnReceipt(conversationId: string, messageId: string) {
  return request<TurnReceipt>(
    `/mindos/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/receipt`,
  )
}

export function getOntologyStats() {
  return request<OntologyStats>('/mindos/ontology/stats')
}

export function listClaims(params: { section?: Section; trust?: TrustState[]; limit?: number } = {}) {
  const query = new URLSearchParams()
  if (params.section) query.set('section', params.section)
  if (params.trust && params.trust.length) query.set('trust', params.trust.join(','))
  if (params.limit) query.set('limit', String(params.limit))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<{ items: Claim[] }>(`/mindos/ontology/claims${suffix}`)
}

export function getClaim(claimId: string) {
  return request<Claim>(`/mindos/ontology/claims/${encodeURIComponent(claimId)}`)
}

export function createClaim(payload: ClaimCreatePayload) {
  return postJson<Claim>('/mindos/ontology/claims', payload)
}

export function reviewClaim(claimId: string, payload: ReviewRequest) {
  return postJson<ReviewResult>(`/mindos/ontology/claims/${encodeURIComponent(claimId)}/review`, payload)
}

export function getInbox(limit = 20) {
  return request<{ items: Claim[] }>(`/mindos/ontology/inbox?limit=${limit}`)
}

export function listEntities(type?: OntologyEntity['type']) {
  const suffix = type ? `?type=${encodeURIComponent(type)}` : ''
  return request<{ items: OntologyEntity[] }>(`/mindos/ontology/entities${suffix}`)
}

export function getProjection() {
  return request<OntologyProjection>('/mindos/ontology/projection')
}

export function getZhijunStatus() {
  return request<ZhijunStatus>('/mindos/zhijun/status')
}

// ---- P3：整合与裁决、导出 / 全量删除
export function getProposals() {
  return request<{ merges: MergeProposal[]; conflicts: Conflict[]; total: number }>('/mindos/ontology/proposals')
}

export function resolveMergeProposal(proposalId: string, accept: boolean) {
  return postJson<MergeProposal>(`/mindos/ontology/proposals/merges/${encodeURIComponent(proposalId)}/resolve`, { accept })
}

export function resolveConflict(conflictId: string, keep: 'a' | 'b' | 'both') {
  return postJson<Conflict>(`/mindos/ontology/proposals/conflicts/${encodeURIComponent(conflictId)}/resolve`, { keep })
}

export function consolidateNow() {
  return postJson<ConsolidateReport>('/mindos/ontology/consolidate', {})
}

export function exportOntology(params: { sections?: Section[]; includeWorking?: boolean } = {}) {
  const query = new URLSearchParams()
  if (params.sections && params.sections.length) query.set('sections', params.sections.join(','))
  if (params.includeWorking) query.set('includeWorking', 'true')
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<OntologyExport>(`/mindos/ontology/export${suffix}`)
}

export function purgeOntology(payload: { confirm: string; includeConversations: boolean }) {
  return postJson<PurgeResult>('/mindos/ontology/purge', payload)
}

// ---- 知君 P4：可带走的认识（Context Pack）与逐条导出开关 ----
export interface ContextPackStatus {
  exportable: number
  receipts: { count: number; last: { generatedAt: string; consumer: string; purpose: string; included: number } | null }
  items: Claim[]
}

export async function setClaimExport(claimId: string, allowed: boolean): Promise<Claim> {
  return request<Claim>(`/mindos/ontology/claims/${encodeURIComponent(claimId)}/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ allowed }),
  })
}

export async function getContextPackStatus(): Promise<ContextPackStatus> {
  return request<ContextPackStatus>('/mindos/ontology/context-pack')
}
