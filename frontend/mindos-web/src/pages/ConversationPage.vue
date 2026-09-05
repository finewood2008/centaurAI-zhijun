<script setup lang="ts">
// 对话页：一段持续关系。左栏会话列表，主区消息流 + 输入区。
// 流式回复（SSE）→ 出处条 → 本会话按话题限频的理解核对；事件细节留在可选小结中。
// 切页不截断：卸载时不中断正在进行的流（服务端把这轮生成完并落库），只有用户点「停止」才中断；
// 卸载后的回调用 alive 守卫，不再碰已销毁的状态。
// 页头只有模型名与「还在整理 N 件事」；提醒、下一步等都在今日页。支持的 query：?say=（话头放进输入框，可配 ?deliberate=1 打开商量开关）与 ?onboarding=1（直接进建档态）。
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronDown, ChevronUp, Sparkles } from 'lucide-vue-next'
import {
  api,
  ApiError,
  confirmDecisionDraft,
  createConversation,
  deleteConversation,
  discardDecisionDraft,
  getConversation,
  getConversationOutcomes,
  getDecisionDraft,
  getConversationMemoryAttention,
  dismissConversationMemory,
  reviewConversationMemoryDraft,
  getOntologyStats,
  getZhijunStatus,
  listClaims,
  listConversations,
  recordConversationOutcome,
  reviewClaim,
  updateConversation,
  updateOnboarding,
  type Claim,
  type Conversation,
  type ConversationOutcomes,
  type ConversationMemoryAttention,
  type DecisionDraft,
  type DecisionDraftConfirmPayload,
  type DecisionDraftEvent,
  type ExtractionEvent,
  type GrowthDecision,
  type Message,
  type MessageDoneEvent,
  type OntologyStats,
  type ProvenanceEvent,
  type ReviewAction,
  type Section,
  type StreamErrorEvent,
  type TurnMetaEvent,
  type TurnMode,
  type ZhijunStatus,
} from '@/services/api'
import { streamPost } from '@/services/sse'
import { useToast } from '@/composables/useToast'
import { useChatImports } from '@/composables/useChatImports'
import ChatFilesPanel from '@/components/conversation/ChatFilesPanel.vue'
import ImportBatchCard from '@/components/conversation/ImportBatchCard.vue'
import { createSessionGate } from '@/composables/sessionGate'
import { reviewNote } from '@/shared/ontology'
import { MODEL_UNAVAILABLE_TEXT, modelUnavailable } from '@/shared/model'
import { extractionSkipNote, hasConversationOutcomes } from '@/shared/labels'
import { placeMemoryAttention } from '@/shared/memoryAttention'
import MessageBubble from '@/components/conversation/MessageBubble.vue'
import ClaimCandidateChip from '@/components/conversation/ClaimCandidateChip.vue'
import ReplyAssistance from '@/components/conversation/ReplyAssistance.vue'
import CharterConversation from '@/components/conversation/CharterConversation.vue'
import type { ReplyAssistanceInput } from '@/shared/replyAssistance'
import ProvenanceStrip from '@/components/conversation/ProvenanceStrip.vue'
import Composer from '@/components/conversation/Composer.vue'
import ConversationList from '@/components/conversation/ConversationList.vue'
import RenameConversationDialog from '@/components/conversation/RenameConversationDialog.vue'
import MoreMenu from '@/components/ui/MoreMenu.vue'
import { conversationActions, conversationListQuery, conversationTitleError, retainNewerConversationMetadata } from '@/shared/conversationManagement'
import LiveObjectPanel from '@/components/conversation/LiveObjectPanel.vue'
import OutcomesCard from '@/components/conversation/OutcomesCard.vue'
import ReviewOutcomePanel from '@/components/conversation/ReviewOutcomePanel.vue'
import LearningCard from '@/components/conversation/LearningCard.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import OntologyExplainer from '@/components/ontology/OntologyExplainer.vue'
import SelfMap from '@/components/ontology/SelfMap.vue'
import AlignmentCard from '@/components/ontology/AlignmentCard.vue'
import AlignmentPrivacy from '@/components/conversation/AlignmentPrivacy.vue'
import RoutingPanel from '@/components/conversation/RoutingPanel.vue'
import MemoryPending from '@/components/conversation/MemoryPending.vue'
import { prepareChatRoute, routingRequest, routePath } from '@/services/taskRouting'
import { contextNeedsReview, contextRetryBody, isContextReviewError } from '@/shared/contextRecovery'
import ErrorState from '@/components/ui/ErrorState.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import SideDrawer from '@/components/ui/SideDrawer.vue'
import MatterWorkspace from '@/components/matters/MatterWorkspace.vue'

interface UiMessage extends Message {
  provenance?: ProvenanceEvent | null
  turnMeta?: TurnMetaEvent | null
  streaming?: boolean
  // 抽取被跳过 / 还在整理时，这条回复下方的一行灰字
  extractionNote?: string
}

const route = useRoute()
const router = useRouter()
const toast = useToast()
const guidedOnboarding = computed(() => route.meta.onboardingFlow === true)
const charterAttention = ref(false)
const onboardingTopics = ref<Array<{ id: string; label: string; state: string }>>([])

const status = ref<ZhijunStatus | null>(null)
const stats = ref<OntologyStats | null>(null)
// stats 请求结束了没有（成功或失败）；失败时 stats 保持 null，绝不把 hasOntology 当真
const statsLoaded = ref(false)
// 组件还活着：卸载后流的回调不再写状态
let alive = true
let mounted = false
const conversations = ref<Conversation[]>([])
const conversationsLoading = ref(true)
const conversationsLoadingMore = ref(false)
const conversationsTotal = ref(0)
const conversationsHasMore = ref(false)
const conversationsError = ref('')
const conversationTab = ref<'active' | 'archived'>('active')
const conversationSearch = ref('')
const conversationSearchScope = ref<'all' | 'active' | 'archived'>('all')
const conversationListGate = createSessionGate()
let conversationListAbort: AbortController | null = null
let conversationSearchTimer: ReturnType<typeof setTimeout> | undefined
const metadataBusy = reactive<Record<string, boolean>>({})
const knownConversationMetadata = new Map<string, Conversation>()
const renameTarget = ref<Conversation | null>(null)
const renameError = ref('')
const archiveUndo = ref<{ conversation: Conversation; previousStatus: Conversation['status'] } | null>(null)
const highlightedMessage = ref<string | null>(null)
let highlightTimer: ReturnType<typeof setTimeout> | undefined
const current = ref<Conversation | null>(null)
const messages = ref<UiMessage[]>([])
const replyTarget = computed(() => {
  const last = messages.value.filter(m => m.role === 'user' || m.role === 'assistant').at(-1)
  return last?.role === 'assistant' && last.status === 'complete' && !last.streaming ? last.id : null
})
const alignmentLocalOnly = ref(false)
const routingMode = ref('legacy')
const routingPanel = ref<InstanceType<typeof RoutingPanel> | null>(null)
const prefillLocalOnly = ref(false)
const alignmentPrivacy = ref<InstanceType<typeof AlignmentPrivacy> | null>(null)
function onAlignmentUpdated(claim: Claim, saved = true) {
  toast({ type: saved ? 'success' : 'error', message: saved ? '自我校准已更新，事实记录保留' : '校准未保存，已读取最新记录；请重新核对' })
  const index = mapClaims.value.findIndex(c => c.id === claim.id)
  if (index >= 0) mapClaims.value.splice(index, 1, claim)
  if (saved) void dismissMemory('alignment', claim.id)
  else if (memoryAttention.value?.alignment?.id === claim.id) memoryAttention.value.alignment = claim
  void alignmentPrivacy.value?.refresh()
}
const messagesLoading = ref(false)
const messagesError = ref('')
const streaming = ref(false)
const listOpen = ref(false)
const deleteTarget = ref<Conversation | null>(null)
const deleting = ref(false)
const reviewBusy = reactive<Record<string, boolean>>({})
const listRef = ref<HTMLElement | null>(null)
const composerRef = ref<InstanceType<typeof Composer> | null>(null)
const matterWorkspace = ref<InstanceType<typeof MatterWorkspace> | null>(null)
const matterSuspension = computed(() => messages.value.filter(m => m.role === 'assistant' && m.status === 'complete').at(-1)?.provenance?.contextPlan?.matterSuspended)

// P2：判断草稿（商量模式）与回访会话
const draft = ref<DecisionDraft | null>(null)
const draftChanged = ref<string[]>([])
const draftBusy = ref(false)
const draftError = ref('')
// 真实模型下草稿是后台任务：SSE 只说「排队了」，这里轮询直到整理好（每 3 秒一次，最多 90 秒）
const draftPending = ref(false)
const draftTimedOut = ref(false)
const draftPollGate = createSessionGate()
const decision = ref<GrowthDecision | null>(null)
const outcomeBusy = ref(false)
const outcomeError = ref('')
const reviewSaving = ref(false)
const reviewSaveError = ref('')

// ?onboarding=1：从今日页过来直接进建档态；即使统计说已有本体也允许开始建档
const forceOnboarding = ref(false)
// 「这段对话留下的」：本会话这次聊完（message_done 之后）拉到的产出；切会话清空
const turnOutcomes = ref<ConversationOutcomes | null>(null)

const isReview = computed(() => current.value?.mode === 'review')
const isOnboarding = computed(() => current.value?.mode === 'onboarding')
const showDraftPanel = computed(() => (!!draft.value && draft.value.status !== 'discarded') || draftPending.value || draftTimedOut.value)
const workspaceOpen = ref(false)
const workspaceTab = ref<'draft' | 'map' | 'review' | 'memory'>('draft')
function openWorkspace(tab: 'draft' | 'map' | 'review' | 'memory') {
  workspaceTab.value = tab
  workspaceOpen.value = true
}
function workspaceKey(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const tabs = [...(event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('[role=tab]')]
  const index = tabs.indexOf(event.target as HTMLButtonElement)
  if (index < 0) return
  event.preventDefault()
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
  tabs[next]?.click(); tabs[next]?.focus()
}
watch(() => current.value?.id, () => { workspaceOpen.value = false })

// ---- 建档：一边聊，本体图一边亮起来
const ONBOARDING_STEPS: { title: string; section: Section }[] = [
  { title: '称呼与角色', section: 'who' },
  { title: '手头的事', section: 'matters' },
  { title: '在意的人', section: 'people' },
  { title: '最近一次判断', section: 'ways' },
  { title: '一条原则', section: 'principles' },
  { title: '一两年后', section: 'direction' },
  { title: '不想让 AI 碰的', section: 'principles' },
]
const onboardingStep = ref<number | null>(null)
const onboardingTransitioning = ref(false)
const mapClaims = ref<Claim[]>([])
const newClaimIds = ref<Set<string>>(new Set())
let glowTimer: number | null = null
const mapPollGate = createSessionGate()

const highlightSection = computed<Section | null>(() => {
  const currentTopic = onboardingTopics.value.find(t => t.state === 'pending')?.id
  const sections: Record<string, Section> = { situation: 'who', focus: 'matters', direction: 'direction', support: 'ways', boundaries: 'principles' }
  return currentTopic ? sections[currentTopic] ?? null : null
})
// 收尾那一轮还在生成时不算「聊完」：面板要等 message_done 之后才出现
const closingStreaming = ref(false)
const onboardingDone = computed(() => (onboardingStep.value ?? 0) >= ONBOARDING_STEPS.length + 1 && !closingStreaming.value)
const onboardingCounts = computed(() => ({
  confirmed: stats.value?.claims?.confirmed ?? mapClaims.value.filter((c) => c.trustState === 'confirmed').length,
  working: stats.value?.inbox ?? mapClaims.value.filter((c) => c.trustState === 'working').length,
}))

function stepFromMessages(): number | null {
  // 新流程由知君先问第 1 问，用户答完 k 条后正在第 k+1 问；旧会话仍按原来的轮次估算。
  const userTurns = messages.value.filter((m) => m.role === 'user' && (m.meta?.replyAssistance as any)?.kind !== 'control').length
  const hasOpening = messages.value.some((m) => m.role === 'assistant' && m.meta?.kind === 'onboarding_open')
  if (hasOpening) return Math.min(userTurns + 1, ONBOARDING_STEPS.length + 1)
  if (userTurns <= 0) return null
  return Math.min(userTurns, ONBOARDING_STEPS.length + 1)
}

async function loadMapClaims(): Promise<Set<string>> {
  try {
    const res = await listClaims({ trust: ['confirmed', 'working'], limit: 500 })
    mapClaims.value = res.items
    return new Set(res.items.map((c) => c.id))
  } catch {
    return new Set(mapClaims.value.map((c) => c.id))
  }
}

function flashNew(ids: Set<string>) {
  if (!ids.size) return
  newClaimIds.value = new Set([...newClaimIds.value, ...ids])
  if (glowTimer) window.clearTimeout(glowTimer)
  glowTimer = window.setTimeout(() => {
    newClaimIds.value = new Set()
    glowTimer = null
  }, 2400)
}

async function pollMap() {
  const session = mapPollGate.next()
  const before = new Set(mapClaims.value.map((c) => c.id))
  for (let i = 0; i < 10; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000))
    if (!mapPollGate.isCurrent(session)) return
    const after = await loadMapClaims()
    if (!mapPollGate.isCurrent(session)) return
    const fresh = new Set([...after].filter((id) => !before.has(id)))
    if (fresh.size) {
      flashNew(fresh)
      void loadStats()
      return
    }
  }
  void loadStats()
}

function pushNote(content: string, meta: Record<string, unknown>) {
  if (!current.value) return
  messages.value.push(
    reactive<UiMessage>({
      id: `local-note-${Date.now()}-${messages.value.length}`,
      conversationId: current.value.id,
      seq: messages.value.length + 1,
      role: 'system',
      content,
      status: 'complete',
      createdAt: new Date().toISOString(),
      meta,
    }),
  )
}

async function onConfirmDraft(payload: DecisionDraftConfirmPayload) {
  if (!current.value || draftBusy.value) return
  draftBusy.value = true
  draftError.value = ''
  try {
    const result = await confirmDecisionDraft(current.value.id, payload)
    draft.value = result.draft
    decision.value = result.decision
    pushNote(`你记下了一个判断：${result.decision.title}（选了「${result.decision.choice}」，把握 ${result.decision.confidence}%）`, {
      kind: 'decision_confirmed',
      decisionId: result.decision.id,
    })
    toast({ type: 'success', message: '已记进判断簿，到期知君会来回访' })
    void refreshOutcomes(current.value.id, true)
    await scrollToBottom()
  } catch (err) {
    if (err instanceof ApiError && err.status === 400) draftError.value = err.message
    else draftError.value = friendlyError(err, '记录失败')
  } finally {
    draftBusy.value = false
  }
}

async function onDiscardDraft() {
  if (!current.value || draftBusy.value) return
  draftBusy.value = true
  try {
    await discardDecisionDraft(current.value.id)
    draft.value = null
    draftChanged.value = []
    toast({ type: 'info', message: '草稿已放弃，再商量时会重新整理' })
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '操作失败') })
  } finally {
    draftBusy.value = false
  }
}

async function pollDraft(conversationId: string) {
  const session = draftPollGate.next()
  const previousRevision = draft.value?.revision ?? 0
  for (let i = 0; i < 30; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 3000))
    if (!draftPollGate.isCurrent(session)) return
    try {
      const got = await getDecisionDraft(conversationId)
      if (!draftPollGate.isCurrent(session)) return
      if (got && got.fields && got.status !== 'discarded' && got.revision > previousRevision) {
        draft.value = got
        draftChanged.value = []
        draftError.value = ''
        draftPending.value = false
        draftTimedOut.value = false
        return
      }
    } catch {
      // 还没生成（404）或暂时不可用：继续等
    }
  }
  if (!draftPollGate.isCurrent(session)) return
  draftPending.value = false
  draftTimedOut.value = true
}

function retryDraft() {
  if (!current.value) return
  draftTimedOut.value = false
  draftPending.value = true
  void pollDraft(current.value.id)
}

async function onRecordOutcome(payload: { result: string; notes: string }) {
  if (!current.value || outcomeBusy.value) return
  outcomeBusy.value = true
  outcomeError.value = ''
  try {
    const res = await recordConversationOutcome(current.value.id, payload)
    decision.value = res.decision
    pushNote(`你记下了结果：${payload.result.slice(0, 200)}`, { kind: 'outcome_recorded', decisionId: res.decision.id })
    toast({ type: 'success', message: '结果已记下，接着在右边复盘' })
    void refreshOutcomes(current.value.id, true)
    await scrollToBottom()
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) outcomeError.value = '这个判断已经记过结果'
    else outcomeError.value = friendlyError(err, '记录失败')
  } finally {
    outcomeBusy.value = false
  }
}

async function onSubmitReview(payload: { reflection: string; lessons: string[]; nextAction: string }) {
  const d = decision.value
  if (!current.value || !d || reviewSaving.value) return
  reviewSaving.value = true
  reviewSaveError.value = ''
  try {
    const result = await api.createGrowthReview({ decisionId: d.id, ...payload })
    if (!alive) return
    decision.value = result.decision
    pushNote('复盘已记下，经验会保留为这次情境的候选理解，不自动变成长期原则', { kind: 'review_recorded', decisionId: d.id, reviewId: result.review.id })
    void refreshOutcomes(current.value.id, true)
    await scrollToBottom()
  } catch (err) {
    if (!alive) return
    if (err instanceof ApiError && err.status === 409) reviewSaveError.value = '这个判断已经复盘过了'
    else reviewSaveError.value = friendlyError(err, '记录失败')
  } finally {
    reviewSaving.value = false
  }
}

// 这段对话留下了什么：刷新会话列表里该项的产出摘要（接口不存在时静默）；
// showCard 为真且还停在这个会话时，同时更新消息流底部那张「这段对话留下的」小卡
async function refreshOutcomes(conversationId: string, showCard = false) {
  try {
    const o = await getConversationOutcomes(conversationId)
    if (!alive || !o) return
    if (showCard && current.value?.id === conversationId) turnOutcomes.value = o
    const brief = {
      confirmed: o.confirmedClaims?.length ?? 0,
      working: o.workingClaims?.length ?? 0,
      decision: !!o.decision,
      commitments: o.commitments?.length ?? 0,
    }
    conversations.value = conversations.value.map((c) => (c.id === conversationId ? { ...c, outcomes: brief } : c))
  } catch {
    // 旧后端没有这个接口
  }
}

const showOutcomesCard = computed(() => !streaming.value && !!current.value && hasConversationOutcomes(turnOutcomes.value))

const loadGate = createSessionGate()
const pollGate = createSessionGate()
const memoryLoadGate = createSessionGate()
const memoryAttention = ref<ConversationMemoryAttention | null>(null)
const memoryDraftBusy = ref(false)
const memoryDraftError = ref('')
const memoryPlacement = computed(() => placeMemoryAttention(memoryAttention.value, messages.value, current.value?.id ?? null))
const memoryDraft = computed(() => memoryAttention.value?.draft ?? null)
let memoryTimer: ReturnType<typeof setTimeout> | undefined
let abortController: AbortController | null = null
// 新建会话后先本地替换路由，再由本页继续流式；此时跳过 watcher 的重新加载。
let skipLoadFor: string | null = null

const currentId = computed(() => {
  const id = route.params.conversationId
  return typeof id === 'string' && id ? id : null
})

// 建档入口只由显式引导路由 / query 打开。正常 /chat 已由全局引导状态守卫放行，
// 不能再根据「没有会话 / 本体为空」倒推出尚未完成引导（用户可能刚删完对话）。
const onboardingConversation = computed(() => conversations.value.find((c) => c.mode === 'onboarding') ?? null)
const displayedConversations = computed(() =>
  guidedOnboarding.value ? conversations.value.filter((c) => c.mode === 'onboarding') : conversations.value,
)
const landingReady = computed(() => !conversationsLoading.value && statsLoaded.value)
const showIntro = computed(
  () =>
    !currentId.value &&
    landingReady.value &&
    (guidedOnboarding.value || forceOnboarding.value),
)
const showBlank = computed(() => !currentId.value && !messages.value.length && landingReady.value && !showIntro.value)
// 没聊完的建档会话（用户轮数 < 8）：空白态给一张「继续建档」卡
const pendingOnboarding = computed(() => {
  const conv = onboardingConversation.value
  return conv && guidedOnboarding.value ? { id: conv.id } : null
})

const headerTitle = computed(() => {
  if (current.value) return current.value.title || (current.value.mode === 'onboarding' ? '第一次对话' : '对话')
  return guidedOnboarding.value ? '第一次认识' : '对话'
})

// 模型没配置 / 不可用：页头改成「还没配置模型 · 去偏好」，输入区禁用并显示同一句
const modelBlocked = computed(() => modelUnavailable(status.value))
// 后台还没整理完的事（抽取 / 草稿 / 摘要），页头只用一句淡字提示
const pendingJobs = computed(() => status.value?.pendingJobs ?? 0)

// ---- 空白态的三张起手卡：点一下把话头放进输入框，不自动发送
interface Starter { title: string; desc: string; text: string; deliberate?: boolean }
const STARTERS: Starter[] = [
  { title: '一起想清楚一件事', desc: '理清目标与取舍，需要时形成判断，由你核对后保存', text: '我在考虑一件事：', deliberate: true },
  { title: '准备一次重要沟通', desc: '围绕目标、对方与顾虑，一起准备可修改的谈话提纲', text: '我想准备一次重要沟通：' },
  { title: '最近发生了……', desc: '说说这周让你在意的事，知君会把它和你以前说过的连起来', text: '最近发生了一件事，' },
  { title: '你怎么看我？', desc: '让知君用它目前对你的认识说说看，不对的地方你直接改', text: '基于你目前对我的认识，说说你眼中的我，哪些地方你其实不确定？' },
]
function useStarter(s: Starter) {
  composerRef.value?.setDeliberate(!!s.deliberate)
  composerRef.value?.appendText(s.text)
}

// 从今日页 / 其它页带着话头过来：?say=… → 放进输入框（?deliberate=1 时同时打开「商量」开关），然后把 query 清掉
watch(
  () => route.query.say,
  (v) => {
    if (typeof v !== 'string' || !v) return
    prefillLocalOnly.value = route.query.localOnly === '1'
    const d = route.query.deliberate
    const deliberate = d === '1' || d === 'true'
    void nextTick(() => {
      composerRef.value?.setDeliberate(deliberate)
      composerRef.value?.setText(v)
      void router.replace({ path: route.path, query: route.query.charter === '1' ? { charter: '1' } : {} })
    })
  },
  { immediate: true },
)

// 从今日页点「开始建档」过来：?onboarding=1 → 直接进 intro / 建档态（已有本体也允许），然后把 query 清掉；
// 一旦进了某个会话就不再强制
watch(
  () => route.query.onboarding,
  (v) => {
    if (v !== '1' && v !== 'true') return
    forceOnboarding.value = true
    void router.replace({ path: route.path, query: {} })
  },
  { immediate: true },
)

function friendlyError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 409 && err.code === 'TURN_IN_FLIGHT') return '这段对话还在生成中，请稍等再发。'
    if (err.status === 429) return '模型正忙，请稍后再试。'
    if (err.status === 503) return '模型服务不可用，请到「偏好」检查模型配置。'
    return err.message || fallback
  }
  return err instanceof Error && err.message ? err.message : fallback
}

let statusTimer: number | null = null
async function loadStatus() {
  let next: ZhijunStatus | null = null
  try {
    next = await getZhijunStatus()
  } catch {
    next = null
  }
  if (!alive) return
  status.value = next
  // 后台还在整理时每 8 秒看一眼，整理完就停
  if (statusTimer) window.clearTimeout(statusTimer)
  statusTimer = (status.value?.pendingJobs ?? 0) > 0 ? window.setTimeout(() => void loadStatus(), 8000) : null
}

async function loadStats() {
  try {
    const next = await getOntologyStats()
    if (!alive) return
    stats.value = next
  } catch {
    // 取不到就当「未知」：不能把 hasOntology 当真
    if (!alive) return
    stats.value = null
  } finally {
    statsLoaded.value = true
  }
}

async function loadConversations(append = false) {
  clearTimeout(conversationSearchTimer)
  if (append && (conversationsLoading.value || conversationsLoadingMore.value || !conversationsHasMore.value)) return
  conversationListAbort?.abort()
  const ticket = conversationListGate.next()
  const controller = new AbortController()
  conversationListAbort = controller
  if (append) conversationsLoadingMore.value = true
  else { conversationsLoading.value = true; conversationsLoadingMore.value = false }
  conversationsError.value = ''
  try {
    const params = conversationListQuery(conversationTab.value, conversationSearch.value, conversationSearchScope.value, append ? conversations.value.length : 0)
    const res = await listConversations(params, controller.signal)
    if (!alive || !conversationListGate.isCurrent(ticket)) return
    const items = res.items.map(rememberConversationMetadata)
    conversations.value = append ? [...new Map([...conversations.value, ...items].map(c => [c.id, c])).values()] : items
    conversationsTotal.value = res.total
    conversationsHasMore.value = res.hasMore
  } catch (err) {
    if (!alive || !conversationListGate.isCurrent(ticket) || controller.signal.aborted) return
    conversationsError.value = friendlyError(err, '会话列表加载失败')
  } finally {
    if (alive && conversationListGate.isCurrent(ticket)) {
      conversationsLoading.value = false
      conversationsLoadingMore.value = false
    }
  }
}

watch([conversationSearch, conversationTab, conversationSearchScope], ([query], [previous]) => {
  conversationListGate.invalidate()
  conversationListAbort?.abort()
  clearTimeout(conversationSearchTimer)
  conversationsLoading.value = true
  if (query.trim() && !previous.trim()) conversationSearchScope.value = 'all'
  if (query !== previous && query.trim()) conversationSearchTimer = setTimeout(() => void loadConversations(), 250)
  else void loadConversations()
})

function rememberConversationMetadata(next: Conversation): Conversation {
  const safe = retainNewerConversationMetadata(next, knownConversationMetadata.get(next.id))
  knownConversationMetadata.set(safe.id, safe)
  return safe
}

function applyConversationMetadata(next: Conversation) {
  const safe = rememberConversationMetadata(next)
  conversations.value = conversations.value.map(c => c.id === safe.id ? { ...c, ...safe } : c)
  if (current.value?.id === safe.id) current.value = { ...current.value, ...safe }
}

// Refresh metadata only: a newly persisted user message may restore an archive,
// but this must never replace in-flight messages or the user's composer text.
async function refreshCurrentMetadata(conversationId: string) {
  try {
    const detail = await getConversation(conversationId)
    if (!alive) return
    applyConversationMetadata(detail.conversation)
  } catch { /* a failed metadata refresh must not interrupt a successful turn */ }
}

async function manageConversation(conversation: Conversation, action: string) {
  const candidate = current.value?.id === conversation.id && current.value.metadataRevision >= conversation.metadataRevision ? current.value : conversation
  if (metadataBusy[candidate.id]) return
  if (action === 'rename') { renameTarget.value = candidate; renameError.value = ''; return }
  if (action === 'delete') { deleteTarget.value = candidate; return }
  if (!['pin', 'archive', 'restore'].includes(action)) return
  metadataBusy[candidate.id] = true
  try {
    const change = action === 'pin' ? { pinned: !candidate.pinnedAt } : { status: action === 'archive' ? 'archived' as const : 'active' as const }
    const updated = await updateConversation(candidate.id, { expectedRevision: candidate.metadataRevision, ...change })
    if (!alive) return
    applyConversationMetadata(updated)
    if (action !== 'pin') archiveUndo.value = { conversation: updated, previousStatus: candidate.status }
    void loadConversations()
  } catch (err) {
    if (!alive) return
    toast({ type: 'error', message: err instanceof ApiError && err.status === 409 ? '对话已在别处更新；已读取最新状态，请重新选择。' : friendlyError(err, '没有保存这次修改') })
    void refreshCurrentMetadata(candidate.id)
    void loadConversations()
  } finally { delete metadataBusy[candidate.id] }
}

async function saveConversationName(title: string) {
  const target = renameTarget.value
  if (!target || metadataBusy[target.id]) return
  renameError.value = conversationTitleError(title)
  if (renameError.value) return
  metadataBusy[target.id] = true
  try {
    const updated = await updateConversation(target.id, { expectedRevision: target.metadataRevision, title })
    if (!alive) return
    applyConversationMetadata(updated)
    renameTarget.value = null
    void loadConversations()
  } catch (err) {
    if (!alive) return
    if (err instanceof ApiError && err.status === 409) {
      renameError.value = '对话已在别处更新。你输入的名称仍保留，请核对后再次保存。'
      try {
        const detail = await getConversation(target.id)
        if (renameTarget.value?.id === target.id) renameTarget.value = rememberConversationMetadata(detail.conversation)
        applyConversationMetadata(detail.conversation)
      } catch { /* preserve typed name and revision on refresh failure */ }
    } else renameError.value = friendlyError(err, '名称未保存，请重试')
  } finally { delete metadataBusy[target.id] }
}

async function undoArchive() {
  const undo = archiveUndo.value
  if (!undo || metadataBusy[undo.conversation.id]) return
  metadataBusy[undo.conversation.id] = true
  try {
    const updated = await updateConversation(undo.conversation.id, { expectedRevision: undo.conversation.metadataRevision, status: undo.previousStatus })
    if (!alive) return
    applyConversationMetadata(updated)
    if (archiveUndo.value === undo) archiveUndo.value = null
    void loadConversations()
  } catch (err) {
    toast({ type: 'error', message: err instanceof ApiError && err.status === 409 ? '对话已有新的更改，未覆盖；请从菜单重新选择处理方式。' : friendlyError(err, '撤销未完成，请重试') })
    if (err instanceof ApiError && err.status === 409 && archiveUndo.value === undo) archiveUndo.value = null
    void refreshCurrentMetadata(undo.conversation.id)
  } finally { delete metadataBusy[undo.conversation.id] }
}

function toUi(m: Message): UiMessage {
  // 历史回复：用后端由回执还原的出处播种，让旧回复也能展示出处条与出处小图；直播的 SSE provenance 会覆盖它。
  const seeded = m.role === 'assistant' && m.provenance ? (m.provenance as ProvenanceEvent) : null
  return reactive({ ...m, provenance: seeded, turnMeta: null, streaming: false }) as UiMessage
}

async function loadConversation(id: string) {
  const session = loadGate.next()
  clearMemoryAttention()
  messagesLoading.value = true
  messagesError.value = ''
  try {
    const detail = await getConversation(id)
    if (!loadGate.isCurrent(session)) return
    current.value = rememberConversationMetadata(detail.conversation)
    messages.value = detail.messages.map(toUi)
    draft.value = detail.decisionDraft && detail.decisionDraft.status !== 'discarded' ? detail.decisionDraft : null
    draftChanged.value = []
    draftError.value = ''
    draftPending.value = false
    draftTimedOut.value = false
    draftPollGate.invalidate()
    decision.value = detail.decision ?? null
    outcomeError.value = ''
    reviewSaveError.value = ''
    closingStreaming.value = false
    turnOutcomes.value = null
    // 成果不是一次性提示：重新打开旧会话时也要能核对这段对话留下了什么。
    void refreshOutcomes(id, true)
    void loadMapClaims()
    void refreshMemoryAttention(id)
    if (detail.conversation.mode === 'onboarding') {
      onboardingStep.value = stepFromMessages()
    } else {
      onboardingStep.value = null
    }
    if (typeof route.query.message === 'string') await revealMessage(route.query.message)
    else await scrollToBottom()
  } catch (err) {
    if (!loadGate.isCurrent(session)) return
    if (err instanceof ApiError && err.status === 404) {
      toast({ type: 'error', message: '会话不存在' })
      router.replace(guidedOnboarding.value ? '/onboarding' : '/chat')
      return
    }
    messagesError.value = friendlyError(err, '会话加载失败')
  } finally {
    if (loadGate.isCurrent(session)) messagesLoading.value = false
  }
}

function resetToLanding() {
  loadGate.invalidate()
  clearMemoryAttention()
  current.value = null
  messages.value = []
  messagesError.value = ''
  messagesLoading.value = false
  draft.value = null
  draftChanged.value = []
  draftError.value = ''
  draftPending.value = false
  draftTimedOut.value = false
  draftPollGate.invalidate()
  decision.value = null
  outcomeError.value = ''
  reviewSaveError.value = ''
  closingStreaming.value = false
  turnOutcomes.value = null
  onboardingStep.value = null
  mapClaims.value = []
  // 从会话回到空白态：建档入口按最新的统计判断（首次进页由 onMounted 负责，不重复拉）
  if (mounted) void loadStats()
  newClaimIds.value = new Set()
  mapPollGate.invalidate()
}

watch(
  currentId,
  (id) => {
    // 进了任何一个会话（含刚从强制建档态新建的），?onboarding=1 的强制就结束
    if (id) forceOnboarding.value = false
    if (streaming.value && id && skipLoadFor === id) {
      skipLoadFor = null
      return
    }
    skipLoadFor = null
    if (id) loadConversation(id)
    else resetToLanding()
  },
  { immediate: true },
)

async function scrollToBottom() {
  await nextTick()
  if (typeof route.query.message === 'string') return
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function revealMessage(messageId: string) {
  await nextTick()
  const scroller = listRef.value
  const target = scroller?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(messageId)}"]`)
  if (!scroller || !target) return
  scroller.scrollTop += target.getBoundingClientRect().top - scroller.getBoundingClientRect().top - Math.min(80, scroller.clientHeight / 4)
  highlightedMessage.value = messageId
  clearTimeout(highlightTimer)
  highlightTimer = setTimeout(() => { highlightedMessage.value = null }, 8000)
}

watch(() => route.query.message, value => {
  if (typeof value === 'string' && current.value?.id === currentId.value && !messagesLoading.value) void revealMessage(value)
  else if (!value) highlightedMessage.value = null
})

function selectConversation(id: string, messageId?: string) {
  listOpen.value = false
  const query = messageId ? { message: messageId } : {}
  if (id !== currentId.value) {
    const prefix = guidedOnboarding.value ? '/onboarding/c' : '/c'
    router.push({ path: `${prefix}/${encodeURIComponent(id)}`, query })
  } else {
    void router.replace({ path: route.path, query })
    if (messageId) void revealMessage(messageId)
  }
}

function newConversation() {
  listOpen.value = false
  if (guidedOnboarding.value) {
    void skipOnboarding()
    return
  }
  if (currentId.value) router.push('/chat')
  nextTick(() => composerRef.value?.focus())
}

function askDelete(id: string) {
  deleteTarget.value = conversations.value.find((c) => c.id === id) ?? (current.value?.id === id ? current.value : null)
}

async function confirmDelete() {
  const target = deleteTarget.value
  if (!target) return
  deleting.value = true
  try {
    await deleteConversation(target.id)
    conversations.value = conversations.value.filter((c) => c.id !== target.id)
    if (archiveUndo.value?.conversation.id === target.id) archiveUndo.value = null
    void loadConversations()
    if (currentId.value === target.id) router.replace('/chat')
    toast({ type: 'success', message: '会话已删除' })
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '删除失败') })
  } finally {
    deleting.value = false
    deleteTarget.value = null
  }
}

async function ensureConversation(mode: 'chat' | 'onboarding'): Promise<Conversation> {
  if (current.value) return current.value
  if (mode === 'onboarding') {
    const progress = await updateOnboarding('start')
    if (!progress.conversationId) throw new Error('建档会话没有创建成功')
    const detail = await getConversation(progress.conversationId)
    const conv = detail.conversation
    current.value = rememberConversationMetadata(conv)
    messages.value = detail.messages.map(toUi)
    draft.value = detail.decisionDraft && detail.decisionDraft.status !== 'discarded' ? detail.decisionDraft : null
    onboardingStep.value = stepFromMessages()
    if (!conversations.value.some((item) => item.id === conv.id)) conversations.value = [conv, ...conversations.value]
    skipLoadFor = conv.id
    await router.replace(`/onboarding/c/${encodeURIComponent(conv.id)}`)
    await scrollToBottom()
    return conv
  }
  const conv = await createConversation({ mode })
  current.value = rememberConversationMetadata(conv)
  conversations.value = [conv, ...conversations.value]
  skipLoadFor = conv.id
  await router.replace(`/c/${encodeURIComponent(conv.id)}`)
  return conv
}

async function send(content: string, depth: 'brief' | 'deep', mode: TurnMode = 'chat', origin?: ReplyAssistanceInput) {
  if (streaming.value) return
  if (route.query.message) {
    const query = { ...route.query }
    delete query.message
    await router.replace({ path: route.path, query })
    highlightedMessage.value = null
  }
  if (imports.staged.length) {
    await imports.send(content, origin)
    if (imports.staged.length) composerRef.value?.setText(content, origin)
    if (current.value) void refreshCurrentMetadata(current.value.id)
    void loadConversations()
    return
  }
  // The task router checks the actual selected channel, not global online health.
  // 「先认识你」态下直接打字：进的是建档会话，不是普通对话
  const wantOnboarding = showIntro.value
  streaming.value = true
  let conv: Conversation
  try {
    conv = await ensureConversation(wantOnboarding ? 'onboarding' : 'chat')
  } catch (err) {
    streaming.value = false
    composerRef.value?.setText(content, origin)
    toast({ type: 'error', message: friendlyError(err, '无法创建会话') })
    return
  }
  await streamTurn(conv, content, depth, wantOnboarding ? 'chat' : mode, origin)
}

async function startOnboarding() {
  if (streaming.value) return
  streaming.value = true
  try {
    await ensureConversation('onboarding')
    nextTick(() => composerRef.value?.focus())
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '无法开始第一次对话') })
  } finally {
    streaming.value = false
  }
}

async function finishProfileConversation() {
  if (!current.value || onboardingTransitioning.value) return
  onboardingTransitioning.value = true
  try {
    await updateOnboarding('profile_ready', current.value.id)
    await router.push('/onboarding')
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '暂时无法打开档案核对') })
  } finally {
    onboardingTransitioning.value = false
  }
}

async function skipOnboarding() {
  if (onboardingTransitioning.value) return
  onboardingTransitioning.value = true
  try {
    await updateOnboarding('skip', current.value?.id)
    toast({ type: 'info', message: '已暂时跳过，以后仍可以回来继续认识' })
    await router.push('/chat')
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '暂时无法跳过引导') })
  } finally {
    onboardingTransitioning.value = false
  }
}

async function finishLightOnboarding() {
  try {
    await updateOnboarding('finish', current.value?.id)
    await router.push(current.value ? `/c/${current.value.id}` : '/chat')
    toast({ type: 'success', message: '已经可以开始使用；未核对的内容仍保留，今后继续完善。' })
  } catch (err) { toast({ type: 'error', message: friendlyError(err, '暂时无法结束引导，内容仍保留') }) }
}

async function streamTurn(conv: Conversation, content: string, depth: 'brief' | 'deep', mode: TurnMode = 'chat', origin?: ReplyAssistanceInput) {
  let sendBody: Record<string, unknown> | null
  try {
    const routeState = await routingRequest(routePath(conv.id))
    routingMode.value = routeState.mode.mode
    sendBody = await prepareChatRoute(conv.id, { content, depth, mode, materialRefs: imports.references, replyAssistance: origin,
      localOnly: prefillLocalOnly.value || (routingMode.value === 'legacy' && (imports.localOnly || alignmentLocalOnly.value)) })
    if (!sendBody) { composerRef.value?.setText(content, origin); streaming.value = false; return }
  } catch (err) {
    streaming.value = false; composerRef.value?.setText(content, origin)
    toast({ type: 'error', message: friendlyError(err, '未能完成外发预览') }); return
  }
  const now = new Date().toISOString()
  const seqBase = messages.value.length
  const userMsg = reactive<UiMessage>({
    id: `local-user-${Date.now()}`,
    conversationId: conv.id,
    seq: seqBase + 1,
    role: 'user',
    content,
    meta: { materialRefs: sendBody.materialRefs || [], ...(origin ? { replyAssistance: { ...origin, kind: origin.selections.length ? 'assisted' : 'control' } } : {}) },
    status: 'complete',
    createdAt: now,
  })
  const assistant = reactive<UiMessage>({
    id: `local-assistant-${Date.now()}`,
    conversationId: conv.id,
    seq: seqBase + 2,
    role: 'assistant',
    content: '',
    status: 'complete',
    createdAt: now,
    provenance: null,
    turnMeta: null,
    meta: { requestId: sendBody.requestId, depth, turnMode: mode },
    streaming: true,
  })
  messages.value.push(userMsg, assistant)
  await scrollToBottom()

  // 首个 token 之前失败：把原文放回输入框，并撤掉刚插入的两个气泡
  let gotToken = false
  const rollback = () => {
    if (!alive) return
    messages.value = messages.value.filter((m) => m !== userMsg && m !== assistant)
    composerRef.value?.setText(content, origin)
  }

  abortController = new AbortController()
  const signal = abortController.signal
  try {
    await streamPost(
      `/mindos/conversations/${encodeURIComponent(conv.id)}/messages`,
      sendBody,
      {
        decision_draft: (d) => {
          const e = d as DecisionDraftEvent
          if (e.state === 'queued' || !e.fields || !e.draftId) {
            draftPending.value = true
            draftTimedOut.value = false
            void pollDraft(conv.id)
            return
          }
          draftPending.value = false
          draftTimedOut.value = false
          draftPollGate.invalidate()
          draft.value = {
            id: e.draftId,
            conversationId: conv.id,
            messageId: assistant.id.startsWith('local-') ? null : assistant.id,
            revision: e.revision ?? 1,
            status: e.status,
            decisionId: null,
            fields: e.fields,
            createdAt: draft.value?.createdAt ?? new Date().toISOString(),
            updatedAt: new Date().toISOString(),
          }
          draftChanged.value = [...(e.changedFields ?? [])]
          draftError.value = ''
        },
        meta: (d) => {
          const m = d as TurnMetaEvent
          assistant.id = m.messageId
          userMsg.id = m.userMessageId
          assistant.meta = { ...assistant.meta, replyTo: m.userMessageId, turnMode: m.turnMode || mode, depth: m.depth }
          assistant.turnMeta = m
          assistant.provider = m.provider
          assistant.model = m.model
          assistant.external = m.external
          if (!alive) return
          void refreshCurrentMetadata(conv.id)
          if (m.mode === 'onboarding') {
            onboardingStep.value = m.onboardingStep ?? stepFromMessages()
            closingStreaming.value = (onboardingStep.value ?? 0) >= ONBOARDING_STEPS.length + 1
            if (!mapClaims.value.length) void loadMapClaims()
          }
        },
        provenance: (d) => {
          assistant.provenance = d as ProvenanceEvent
        },
        token: (d) => {
          gotToken = true
          assistant.content += (d as { t: string }).t ?? ''
          if (alive) void scrollToBottom()
        },
        extraction: (d) => {
          const e = d as ExtractionEvent
          if (!alive) return
          if (e.state === 'queued') {
            pollMemoryAttention(conv.id)
            if (conv.mode === 'onboarding') void pollMap()
          } else if (e.state === 'skipped') {
            assistant.extractionNote = extractionSkipNote(e.reason)
          }
        },
        message_done: (d) => {
          const e = d as MessageDoneEvent
          assistant.status = e.status
          if (e.messageId) assistant.id = e.messageId
          if (!alive) return
          closingStreaming.value = false
          void loadStatus()
          if (conv.mode === 'onboarding' && e.status === 'complete' && (onboardingStep.value ?? 0) >= ONBOARDING_STEPS.length + 1) {
            // 先把流程状态落盘；用户点击右侧按钮时会再次幂等确认并进入核对页。
            void updateOnboarding('profile_ready', conv.id).catch(() => undefined)
          }
        },
        error: (d) => {
          const e = d as StreamErrorEvent
          assistant.status = 'error'
          if (e.messageId) assistant.id = e.messageId
          if (e.userMessageId) userMsg.id = e.userMessageId
          if (e.requestId) assistant.meta = { ...assistant.meta, requestId: e.requestId }
          if (isContextReviewError(e) && !assistant.id.startsWith('local-')) {
            assistant.meta = { ...assistant.meta, replyTo: e.userMessageId || userMsg.id, contextStage: e.stage || 'supplemented', contextPending: { code: e.code, stage: e.stage || 'supplemented' } }
            assistant.content ||= e.message || '补充信息需要核对，原消息已保留。'
            return
          }
          if (!alive) return
          if (assistant.id.startsWith('local-')) rollback()
          else assistant.content = assistant.content || e.message || '生成已暂停，可重试'
          toast({ type: 'error', message: e.message || '生成失败' })
        },
      },
      signal,
    )
  } catch (err) {
    if (signal.aborted) {
      assistant.status = 'aborted'
    } else {
      assistant.status = 'error'
      if (alive) {
        if (assistant.id.startsWith('local-')) rollback()
        if (err instanceof ApiError && isContextReviewError({ code: err.code || '', preview: err.preview }) && !assistant.id.startsWith('local-')) {
          assistant.meta = { ...assistant.meta, contextStage: 'supplemented', contextPending: { code: err.code, stage: 'supplemented' } }
          assistant.content ||= err.message
          return
        }
        if (err instanceof ApiError && err.code === 'ATTACHMENT_CONSENT_REQUIRED') void imports.showConsent()
        toast({ type: 'error', message: friendlyError(err, '生成失败') })
      }
    }
  } finally {
    assistant.streaming = false
    streaming.value = false
    closingStreaming.value = false
    abortController = null
    if (alive) {
      void loadConversations()
      void refreshCurrentMetadata(conv.id)
      void refreshOutcomes(conv.id, true)
      void routingPanel.value?.refresh()
      void refreshMemoryAttention(conv.id)
      await scrollToBottom()
    }
  }
}

async function retryMessage(message: UiMessage, localOnly: boolean) {
  if (!current.value || streaming.value) return
  const before = messages.value.slice(0, messages.value.findIndex(m => m.id === message.id))
  const user = messages.value.find(m => m.id === message.meta?.replyTo) || [...before].reverse().find(m => m.role === 'user')
  if (!user) return
  streaming.value = true
  try {
    const cid = current.value.id
    const body = await prepareChatRoute(cid, contextRetryBody(user, message, localOnly))
    if (!body) return
    message.meta = { ...message.meta, requestId: body.requestId }
    message.streaming = true
    abortController = new AbortController()
    let started = false
    await streamPost(`/mindos/conversations/${encodeURIComponent(cid)}/messages`, body, {
      meta: d => { const m = d as TurnMetaEvent; message.id = m.messageId; message.turnMeta = m; message.provider = m.provider; message.model = m.model; message.external = m.external; message.meta = { ...message.meta, replyTo: m.userMessageId, depth: m.depth, turnMode: m.turnMode || body.mode } },
      provenance: d => { message.provenance = d as ProvenanceEvent },
      token: d => { if (!started) { message.content = ''; started = true }; message.content += (d as { t: string }).t || '' },
      message_done: d => { const done = d as MessageDoneEvent; message.status = done.status; if (done.status === 'complete') message.meta = { ...message.meta, contextPending: undefined } },
      error: d => {
        const e = d as StreamErrorEvent
        message.status = 'error'
        if (e.requestId) message.meta = { ...message.meta, requestId: e.requestId }
        if (isContextReviewError(e)) {
          message.meta = { ...message.meta, contextStage: e.stage || 'supplemented', contextPending: { code: e.code, stage: e.stage || 'supplemented' } }
          if (!started) message.content = e.message || '补充信息仍需核对，原消息已保留。'
        } else toast({ type: 'error', message: e.message })
      },
    }, abortController.signal)
    if (alive && currentId.value === cid) await loadConversation(cid)
  } catch (e) {
    if (e instanceof ApiError && isContextReviewError({ code: e.code || '', preview: e.preview })) {
      message.status = 'error'
      message.meta = { ...message.meta, contextStage: 'supplemented', contextPending: { code: e.code, stage: 'supplemented' } }
    } else toast({ type: 'error', message: friendlyError(e, '重试失败，原消息仍然保留') })
  }
  finally { streaming.value = false; message.streaming = false; abortController = null }
}

// 只有用户显式点「停止」才中断；切页不中断，让服务端把这轮生成完并落库
function stop() {
  abortController?.abort()
}

function clearMemoryAttention() {
  pollGate.invalidate()
  memoryLoadGate.invalidate()
  clearTimeout(memoryTimer)
  memoryTimer = undefined
  memoryAttention.value = null
  memoryDraftError.value = ''
}

// 只读取当前会话由服务端选定的一个核对位。不抢焦点、不滚动，也不把别的会话的候选挂过来。
async function refreshMemoryAttention(conversationId = current.value?.id) {
  if (!conversationId || current.value?.id !== conversationId || currentId.value !== conversationId || !alive) return
  const ticket = memoryLoadGate.next()
  try {
    const next = await getConversationMemoryAttention(conversationId)
    if (!alive || !memoryLoadGate.isCurrent(ticket) || current.value?.id !== conversationId || currentId.value !== conversationId) return
    memoryAttention.value = next
  } catch {
    // 整理状态不可用不影响聊天，也不使用全局 inbox 作为替代。
  }
}

function onPendingMemoryChanged() {
  void refreshMemoryAttention()
  void loadStats()
  if (current.value) void refreshOutcomes(current.value.id, true)
}

function pollMemoryAttention(conversationId: string) {
  clearTimeout(memoryTimer)
  const ticket = pollGate.next()
  let attempts = 0
  const tick = async () => {
    if (!alive || !pollGate.isCurrent(ticket) || currentId.value !== conversationId) return
    await refreshMemoryAttention(conversationId)
    attempts += 1
    if (!alive || !pollGate.isCurrent(ticket) || currentId.value !== conversationId || attempts >= 40) return
    if (attempts >= 3 && pendingJobs.value === 0) return
    memoryTimer = setTimeout(tick, 3000)
  }
  memoryTimer = setTimeout(tick, 3000)
}

async function dismissMemory(kind: 'claim' | 'alignment', id: string, discard = false) {
  const conversationId = current.value?.id
  const attention = memoryAttention.value
  if (!conversationId || !attention) return
  if ((kind === 'claim' ? attention.candidate : attention.alignment)?.id !== id) return
  const topicId = attention.topicId
  reviewBusy[id] = true
  try {
    await dismissConversationMemory(conversationId, { topicId, kind, id, discard })
    if (current.value?.id !== conversationId || memoryAttention.value?.topicId !== topicId) return
    memoryLoadGate.invalidate()
    if (kind === 'claim') memoryAttention.value.candidate = null
    else memoryAttention.value.alignment = null
    // 消耗本话题的核对位；不接着弹出下一条，也不撤销原事实。
  } catch (err) {
    if (current.value?.id !== conversationId) return
    toast({ type: 'error', message: friendlyError(err, '没有保存这次选择，请重试') })
    void refreshMemoryAttention(conversationId)
  } finally { delete reviewBusy[id] }
}

async function reviewMemoryDraft(action: 'save' | 'dismiss') {
  const conversationId = current.value?.id
  const selected = memoryDraft.value
  if (!conversationId || !selected || memoryDraftBusy.value) return
  memoryDraftBusy.value = true
  memoryDraftError.value = ''
  try {
    const result = await reviewConversationMemoryDraft(conversationId, { draftId: selected.id, expectedRevision: selected.revision, action })
    if (current.value?.id !== conversationId) return
    if (memoryAttention.value?.draft?.id === selected.id) memoryAttention.value.draft = result.draft
    toast({ type: 'success', message: action === 'save' ? '只保存为这件事的记录，不设为长期原则' : '小结已放下，原对话保留' })
    if (action === 'save') {
      void loadStats()
      void refreshOutcomes(conversationId, true)
    }
  } catch (err) {
    if (current.value?.id !== conversationId) return
    memoryDraftError.value = err instanceof ApiError && err.status === 409
      ? '小结已有新的内容，请核对刷新后的版本再保存。'
      : friendlyError(err, '未保存，原对话和小结仍然保留')
    await refreshMemoryAttention(conversationId)
  } finally { memoryDraftBusy.value = false }
}

watch(pendingJobs, (n, old) => {
  if ((old ?? 0) > 0 && n === 0 && current.value) {
    void refreshMemoryAttention()
    // 「这段对话留下的」再刷一次：整理完的理解会补进来
    if (turnOutcomes.value) void refreshOutcomes(current.value.id, true)
  }
})

async function onReview(assistant: UiMessage, claim: Claim, action: ReviewAction, editedContent?: string) {
  if (!current.value) return
  const conversationId = current.value.id
  reviewBusy[claim.id] = true
  try {
    const result = await reviewClaim(claim.id, {
      action,
      editedContent,
      surface: current.value.mode === 'onboarding' ? 'onboarding' : 'conversation',
      conversationId,
      messageId: assistant.id.startsWith('local-') ? undefined : assistant.id,
    })
    if (!alive || current.value?.id !== conversationId) return
    await dismissMemory('claim', claim.id)
    if (!alive || current.value?.id !== conversationId) return
    const finalClaim = result.replacedBy ?? result.claim
    messages.value.push(
      reactive<UiMessage>({
        id: `local-note-${Date.now()}`,
        conversationId: current.value.id,
        seq: messages.value.length + 1,
        role: 'system',
        content: reviewNote(action, finalClaim.content),
        status: 'complete',
        createdAt: new Date().toISOString(),
        meta: { kind: 'review', claimId: finalClaim.id, action },
      }),
    )
    void loadStats()
    if (turnOutcomes.value) void refreshOutcomes(current.value.id, true)
    await scrollToBottom()
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '操作失败') })
  } finally {
    delete reviewBusy[claim.id]
  }
}

function onCite(assistant: UiMessage, index: number) {
  const m = assistant.provenance?.materials[index - 1]
  const file = imports.files.find(f => f.materialId === m?.materialId)
  if (file?.materialId && file.version) {
    void imports.showPreview({ materialId: file.materialId, version: file.version })
    return
  }
  if (m) router.push(`/materials/${encodeURIComponent(m.materialId)}`)
}

const imports = reactive(useChatImports({
  conversationId: currentId,
  ensure: async () => (await ensureConversation(showIntro.value ? 'onboarding' : 'chat')).id,
  refreshMessages: async id => {
    if (streaming.value || messagesLoading.value || currentId.value !== id) return false
    const detail = await getConversation(id)
    if (!alive || streaming.value || currentId.value !== id) return false
    current.value = rememberConversationMetadata(detail.conversation)
    messages.value = detail.messages.map(toUi)
    void refreshMemoryAttention(id)
    void loadConversations()
    return true
  },
  notify: message => toast({ type: 'error', message }),
}))

function onFileDragOver(event: DragEvent) {
  if (event.dataTransfer?.types.includes('Files')) event.preventDefault()
}

async function askAboutFiles(message: UiMessage, prompt: string) {
  if (streaming.value) return
  const batch = imports.batches.find(b => b.id === message.meta?.importId)
  if (batch) await imports.chooseReferences([...new Map(batch.files.filter(f => f.state === 'ready' && f.materialId && f.version).map(f => [f.materialId, { materialId: f.materialId!, version: f.version! }])).values()])
  await send(prompt, 'brief')
}

onMounted(() => {
  mounted = true
  void loadStatus()
  void loadStats()
  void loadConversations()
})

onBeforeUnmount(() => {
  // 不 abort 正在进行的流：服务端会把这轮生成完并落库，回来时从服务端重载
  alive = false
  conversationListGate.invalidate()
  conversationListAbort?.abort()
  clearTimeout(conversationSearchTimer)
  clearTimeout(highlightTimer)
  loadGate.invalidate()
  clearMemoryAttention()
  draftPollGate.invalidate()
  mapPollGate.invalidate()
  if (statusTimer) window.clearTimeout(statusTimer)
  if (glowTimer) window.clearTimeout(glowTimer)
})
</script>

<template>
  <div class="zj-page" @dragover="onFileDragOver" @drop="imports.drop" @paste="imports.paste">
    <aside class="zj-page__side" :class="{ 'is-open': listOpen }">
      <button type="button" class="zj-page__side-toggle" :aria-expanded="listOpen" @click="listOpen = !listOpen">
        <span>会话（{{ conversationsTotal }}）</span>
        <component :is="listOpen ? ChevronUp : ChevronDown" :size="16" aria-hidden="true" />
      </button>
      <div class="zj-page__side-body">
        <ConversationList
          :items="displayedConversations"
          :current-id="currentId"
          :loading="conversationsLoading"
          :loading-more="conversationsLoadingMore"
          :total="conversationsTotal"
          :has-more="conversationsHasMore"
          :error="conversationsError"
          :query="conversationSearch"
          :tab="conversationTab"
          :search-scope="conversationSearchScope"
          :busy-ids="metadataBusy"
          :create-label="guidedOnboarding ? '跳过引导，开始新对话' : '新对话'"
          :allow-remove="!guidedOnboarding"
          @select="selectConversation"
          @create="newConversation"
          @remove="askDelete"
          @manage="manageConversation"
          @query="conversationSearch = $event"
          @tab="conversationTab = $event"
          @scope="conversationSearchScope = $event"
          @more="loadConversations(true)"
          @retry="loadConversations()"
        >
          <template #feedback>
            <div v-if="archiveUndo" class="zj-archive-feedback" role="status">
              <span>{{ archiveUndo.conversation.status === 'archived' ? '已归档' : '已移回最近' }}：{{ archiveUndo.conversation.title }}</span>
              <div><button type="button" :disabled="metadataBusy[archiveUndo.conversation.id]" @click="undoArchive">撤销</button><button type="button" :disabled="metadataBusy[archiveUndo.conversation.id]" @click="archiveUndo = null">关闭提示</button></div>
            </div>
          </template>
        </ConversationList>
      </div>
    </aside>

    <section class="zj-page__main" aria-live="polite">
      <header class="zj-page__head">
        <div>
          <div class="zj-page__title-row"><h1 class="zj-page__title">{{ headerTitle }}</h1>
            <MoreMenu v-if="current" boundary="viewport" :items="conversationActions(current, !guidedOnboarding)" :disabled="metadataBusy[current.id]" label="管理当前对话" @select="action => current && manageConversation(current, action)" />
          </div>
          <p class="zj-page__status">
            <span v-if="current?.status === 'archived'" class="zj-page__archived" data-testid="current-archived" title="阅读不会移回最近；发送一条新消息后自动恢复">已归档</span>
            <span v-if="current?.pinnedAt" class="zj-page__archived">已置顶</span>
            <span v-if="archiveUndo && !listOpen" class="zj-page__mobile-undo" role="status">
              <span v-if="archiveUndo.conversation.id !== current?.id || archiveUndo.conversation.status !== 'archived'">{{ archiveUndo.conversation.status === 'archived' ? '已归档' : '已移回最近' }}</span>
              <button type="button" :disabled="metadataBusy[archiveUndo.conversation.id]" @click="undoArchive">撤销</button>
              <button type="button" :disabled="metadataBusy[archiveUndo.conversation.id]" aria-label="关闭归档提示" @click="archiveUndo = null">关闭</button>
            </span>
            <RouterLink v-if="modelBlocked && !imports.localOnly && !alignmentLocalOnly && !prefillLocalOnly" to="/settings" class="zj-page__model-link">{{ MODEL_UNAVAILABLE_TEXT }} · 去偏好</RouterLink>
            <span v-else-if="prefillLocalOnly">本机模型 · 系统话头</span>
            <template v-if="pendingJobs > 0">
              <span v-if="modelBlocked || prefillLocalOnly" class="zj-page__dot" aria-hidden="true">·</span>
              <span class="zj-page__pending" data-testid="head-pending">还在整理 {{ pendingJobs }} 件事</span>
            </template>
            <span v-if="isReview" class="zj-seal zj-seal--accent">回访</span>
            <span v-if="status && !status.workerRunning" class="zj-seal zj-seal--warning" title="知君暂时不会从对话里提出新的理解">整理暂停</span>
          </p>
        </div>
        <div class="zj-page__tools">
          <RoutingPanel ref="routingPanel" :conversation-id="currentId || undefined" :disabled="streaming" @mode="routingMode = $event" />
          <MatterWorkspace v-if="currentId && !guidedOnboarding" ref="matterWorkspace" :conversation-id="currentId" :suspension="matterSuspension" :disabled="streaming || messagesLoading" @prepare="text => composerRef?.appendText(text)" />
          <button v-if="showDraftPanel" class="zj-page__tool zj-page__tool--draft" aria-haspopup="dialog" @click="openWorkspace('draft')">判断草稿<span>{{ draftPending ? '整理中' : draft?.status === 'confirmed' ? '已记录' : '待查看' }}</span></button>
          <button v-if="isOnboarding" class="zj-page__tool" aria-haspopup="dialog" @click="openWorkspace('map')">本体与进度</button>
          <button v-if="decision" class="zj-page__tool" aria-haspopup="dialog" @click="openWorkspace('review')">观察与复盘</button>
          <MemoryPending v-if="current" :key="current.id" :conversation-id="current.id" :pending-count="memoryAttention?.pendingCount" @changed="onPendingMemoryChanged" />
          <button v-if="memoryDraft && memoryDraft.status !== 'dismissed'" class="zj-page__tool" data-testid="memory-draft-entry" aria-haspopup="dialog" @click="openWorkspace('memory')">这件事的小结<span v-if="memoryDraft.status === 'saved'">已保存</span></button>
          <BaseButton v-if="guidedOnboarding" variant="text" size="sm" :loading="onboardingTransitioning" @click="skipOnboarding">跳过引导</BaseButton>
        </div>
      </header>

      <div class="zj-page__body">
      <div class="zj-page__stream">
      <AlignmentPrivacy v-if="currentId" ref="alignmentPrivacy" :conversation-id="currentId" :streaming="streaming" :managed="routingMode !== 'legacy'" @local-only="alignmentLocalOnly = $event" />
      <div ref="listRef" class="zj-page__messages">
        <div v-if="showIntro" class="zj-intro">
          <Sparkles :size="22" aria-hidden="true" />
          <h2>先让我认识真实的你</h2>
          <p>先聊 3～5 个小话题：你现在的处境、在意的事、想往哪里走，以及希望我怎样帮助你。没想清楚可以跳过，随时开始使用，以后再慢慢完善。</p>
          <OntologyExplainer class="zj-intro__explainer" compact />
          <p class="zj-intro__trust">原件留在设备内 · 只有经过你确认的理解才会留下</p>
          <div class="zj-intro__actions">
            <BaseButton variant="primary" :loading="streaming" @click="startOnboarding">开始第一次对话</BaseButton>
            <BaseButton variant="text" :loading="onboardingTransitioning" @click="skipOnboarding">暂时跳过，先随便聊聊</BaseButton>
          </div>
          <button v-if="pendingOnboarding" type="button" class="zj-intro__resume" data-testid="intro-resume-onboarding" @click="selectConversation(pendingOnboarding.id)">
            继续上次的认识 · 也可以先开始使用
          </button>
          <p class="zj-intro__hint">也可以直接在下面打字，知君会从认识你开始。</p>
        </div>

        <div v-else-if="showBlank" class="zj-blank">
          <p class="zj-blank__lead">从你眼下在意的事聊起。可以一起想清楚、准备一份文稿，也可以只是说说，不必马上作决定。</p>
          <button v-if="pendingOnboarding" type="button" class="zj-blank__resume" data-testid="resume-onboarding" @click="selectConversation(pendingOnboarding.id)">
            <span class="zj-seal zj-seal--accent">建档</span>
            <span class="zj-blank__resume-title">继续完善我的方向</span>
            <span class="zj-blank__resume-desc">上次认识你的对话还没聊完，接着聊，本体图会继续亮起来。</span>
          </button>
          <div class="zj-blank__cards" role="group" aria-label="起个头">
            <button v-for="s in STARTERS" :key="s.title" type="button" class="zj-blank__card" @click="useStarter(s)">
              <span class="zj-blank__card-title">{{ s.title }}</span>
              <span class="zj-blank__card-desc">{{ s.desc }}</span>
            </button>
          </div>
          <p class="zj-blank__hint">回应基于你确认过的理解，会标出哪些是你说过的、哪些只是推测。</p>
        </div>

        <div v-if="messagesLoading" class="loading-state">正在打开会话…</div>
        <ErrorState v-else-if="messagesError" :message="messagesError" retry-label="重试" @retry="currentId && loadConversation(currentId)" />

        <template v-for="m in messages" :key="m.id">
          <div class="zj-turn" :class="[`zj-turn--${m.role}`, { 'zj-turn--search-hit': highlightedMessage === m.id }]" :data-message-id="m.id" :aria-label="highlightedMessage === m.id ? '搜索命中消息' : undefined">
            <MessageBubble
              :role="m.role === 'system' && m.meta?.kind === 'review_open' ? 'assistant' : m.role"
              :content="m.content"
              :status="m.status"
              :pending-label="contextNeedsReview(m) ? '等待核对' : undefined"
              :streaming="m.streaming"
              :allow-save="!!currentId && !guidedOnboarding && m.role === 'assistant'"
              @cite="(n) => onCite(m, n)"
              @save="matterWorkspace?.saveFromReply(m)"
            />
            <ImportBatchCard
              v-for="batch in imports.batches.filter(b => b.messageId === m.id)" :key="batch.id"
              :batch="batch" :busy="imports.busyBatch === batch.id || imports.uploading"
              @preview="imports.showPreview($event)" @retry="imports.retry(batch, $event)"
              @consent="imports.showConsent($event)" @reference="imports.chooseReferences($event)"
              @reupload="(item, file) => imports.reupload(batch, item, file)"
            />
            <div v-if="m.meta?.importId && m.role === 'assistant'" class="zj-file-followups">
              <span>{{ m.external ? '外部模型' : '本机模型' }} · {{ m.model }} · 对这批文件的反馈</span>
              <div>
                <button type="button" @click="askAboutFiles(m, '请继续总结这些文件的重点')">内容总结</button>
                <button type="button" @click="askAboutFiles(m, '请指出这些文件中的潜在问题，并给出依据')">潜在问题</button>
                <button type="button" @click="askAboutFiles(m, '这些文件与我已经确认的信息有什么联系？请区分依据与推测')">联系已有资料</button>
              </div>
            </div>
            <ProvenanceStrip v-if="m.role === 'assistant' && m.provenance" :provenance="m.provenance" :meta="m.turnMeta" />
            <p v-if="m.role === 'user' && m.meta?.replyAssistance" class="zj-turn__note">{{ (m.meta.replyAssistance as any).kind === 'assisted' ? '由 AI 候选辅助起草，你已发送' : '对话操作' }}</p>
            <ReplyAssistance v-if="currentId && m.id === replyTarget" :conversation-id="currentId" :message-id="m.id" :disabled="streaming || messagesLoading"
              @insert="(text, origin) => composerRef?.insertReply(text, origin)" @write="composerRef?.focus()" />
            <div v-if="m.role === 'assistant' && !m.streaming && ['error', 'aborted'].includes(m.status)" class="zj-file-followups">
              <span>{{ contextNeedsReview(m) ? '补充信息需要核对；原消息已保留，不会重新发送一条。' : '消息已保留，未自动切换模型。' }}</span>
              <div>
              <button :disabled="streaming" @click="retryMessage(m, false)">{{ contextNeedsReview(m) ? '核对补充资料并继续' : '重试当前模式' }}</button>
              <button :disabled="streaming" @click="retryMessage(m, true)">改用本地</button>
              </div>
            </div>
            <AlignmentCard v-if="!charterAttention && !m.streaming && memoryPlacement?.kind === 'alignment' && memoryPlacement.messageId === m.id"
              :key="memoryPlacement.claim.id" :claim="memoryPlacement.claim" :conversation-id="currentId || undefined" :message-id="m.id"
              @updated="onAlignmentUpdated" @refreshed="c => onAlignmentUpdated(c, false)" />
            <p v-if="m.role === 'assistant' && m.extractionNote" class="zj-turn__note" data-testid="extraction-note">{{ m.extractionNote }}</p>
            <ClaimCandidateChip v-if="!charterAttention && !m.streaming && memoryPlacement?.kind === 'claim' && memoryPlacement.messageId === m.id"
              :key="memoryPlacement.claim.id" :claim="memoryPlacement.claim" :busy="!!reviewBusy[memoryPlacement.claim.id]" dismissible
              @review="(action, edited) => memoryPlacement && onReview(m, memoryPlacement.claim, action, edited)"
              @dismiss="memoryPlacement && dismissMemory('claim', memoryPlacement.claim.id, true)" />
          </div>
        </template>
        <CharterConversation v-if="currentId" :key="currentId" :conversation-id="currentId" :onboarding="guidedOnboarding"
          :message-id="replyTarget" :disabled="streaming || messagesLoading" :claims="mapClaims" :requested="route.query.charter === '1'"
          @finished="finishLightOnboarding" @attention="charterAttention = $event" @topics="onboardingTopics = $event" @reviewed="loadMapClaims" />
        <OutcomesCard v-if="showOutcomesCard && turnOutcomes" :outcomes="turnOutcomes" />
      </div>

      <div class="zj-page__composer">
        <Composer
          ref="composerRef"
          :conversation-id="currentId"
          :streaming="streaming"
          :disabled="messagesLoading"
          :has-attachments="imports.staged.length > 0"
          :uploading="imports.uploading"
          :allow-deliberate="!isReview && !guidedOnboarding && !showIntro"
          :notice="modelBlocked && !imports.staged.length && !imports.localOnly && !alignmentLocalOnly && !prefillLocalOnly ? MODEL_UNAVAILABLE_TEXT : undefined"
          notice-to="/settings"
          :placeholder="current?.mode === 'onboarding' || showIntro ? '回答知君的问题，或者说说你想先聊什么…' : isReview ? '说说实际发生了什么，和预期比差在哪…' : undefined"
          @send="send"
          @stop="stop"
          @files="imports.stageFiles($event)"
          @pick-materials="imports.openPicker()"
        >
          <template #attachments><ChatFilesPanel :model="imports" /></template>
        </Composer>
      </div>
      </div>

      </div>
    </section>

    <SideDrawer :open="workspaceOpen" title="对话工作台" @close="workspaceOpen = false">
      <template #navigation>
        <div class="zj-workspace-tabs" role="tablist" aria-label="对话辅助信息" @keydown="workspaceKey">
          <button v-if="showDraftPanel" id="workspace-draft-tab" role="tab" aria-controls="workspace-draft" :aria-selected="workspaceTab === 'draft'" @click="workspaceTab = 'draft'">判断草稿</button>
          <button v-if="isOnboarding" id="workspace-map-tab" role="tab" aria-controls="workspace-map" :aria-selected="workspaceTab === 'map'" @click="workspaceTab = 'map'">本体与进度</button>
          <button v-if="decision" id="workspace-review-tab" role="tab" aria-controls="workspace-review" :aria-selected="workspaceTab === 'review'" @click="workspaceTab = 'review'">观察与复盘</button>
          <button v-if="memoryDraft && memoryDraft.status !== 'dismissed'" id="workspace-memory-tab" role="tab" aria-controls="workspace-memory" :aria-selected="workspaceTab === 'memory'" @click="workspaceTab = 'memory'">这件事的小结</button>
        </div>
      </template>
      <div class="zj-workspace-content">
        <section v-show="workspaceTab === 'memory'" id="workspace-memory" class="zj-memory-draft" role="tabpanel" aria-labelledby="workspace-memory-tab" data-testid="memory-draft-panel" :aria-busy="memoryDraftBusy">
          <template v-if="memoryDraft && memoryDraft.status !== 'dismissed'">
            <p class="zj-memory-draft__hint">只整理这次事情的进展，不推断你的长期性格或原则。没有保存前，它仍是会话小结。</p>
            <p class="zj-memory-draft__summary">{{ memoryDraft.summary }}</p>
            <div class="zj-memory-draft__record">
              <strong>{{ memoryDraft.status === 'saved' ? '已保存的事件记录' : '将保存的事件记录' }}</strong>
              <p>{{ memoryDraft.savedContent }}</p>
              <span>正文保留简短记录，完整原话作为来源保留，不自动提炼成长期原则。</span>
            </div>
            <details>
              <summary>核对原话与细节</summary>
              <ol><li v-for="(entry, i) in memoryDraft.entries" :key="`${entry.messageId}-${i}`">
                <p>{{ entry.content }}</p>
                <blockquote v-if="entry.quote">「{{ entry.quote }}」</blockquote>
              </li></ol>
            </details>
            <div v-if="memoryDraft.status === 'draft'" class="zj-memory-draft__actions">
              <BaseButton variant="primary" :loading="memoryDraftBusy" @click="reviewMemoryDraft('save')">只保存为这件事的记录</BaseButton>
              <BaseButton variant="text" :disabled="memoryDraftBusy" @click="reviewMemoryDraft('dismiss')">不用保存小结</BaseButton>
            </div>
            <p v-else class="zj-memory-draft__hint">已按这次情境保存，不会自动进入长期核心，也不会提高自我贴合度。</p>
          </template>
          <p v-else class="zj-memory-draft__hint">这份小结已放下。原对话仍然保留，可以继续聊。</p>
          <p v-if="memoryDraftError" role="alert">{{ memoryDraftError }}</p>
        </section>
        <div v-show="workspaceTab === 'map'" id="workspace-map" role="tabpanel" aria-labelledby="workspace-map-tab">
        <section v-if="isOnboarding" class="zj-onb" data-testid="onboarding-map" aria-label="建档进度与本体全景">
          <h2 class="zj-onb__title">知君眼中的你，正在成形</h2>
          <SelfMap
            :claims="mapClaims"
            :stats="stats"
            :highlight-section="highlightSection"
            :new-ids="newClaimIds"
            compact
            @select="(c) => router.push(`/me?section=${c.section}&claim=${encodeURIComponent(c.id)}`)"
          />
          <p class="zj-onb__counts">已经记下 {{ onboardingCounts.confirmed }} 条，等你点头 {{ onboardingCounts.working }} 条</p>
          <ol class="zj-onb__steps" data-testid="onboarding-steps" aria-label="轻量认识的话题，可随时跳过">
            <li
              v-for="s in onboardingTopics"
              :key="s.id"
              class="zj-onb__step"
              :class="{ 'is-done': s.state !== 'pending' }"
            >
              <span class="zj-onb__dot" aria-hidden="true" />
              <span class="zj-onb__label">{{ s.label }}{{ s.state === 'skipped' ? '（先跳过）' : '' }}</span>
            </li>
          </ol>
          <p class="zj-onb__hint">聊过不等于正式确认，没填完整也可以开始使用。</p>
          <div v-if="guidedOnboarding && !streaming" class="zj-onb__done">
            <h3>这是我目前对你的认识</h3>
            <p>对话下方可以查看第一次认识小结；未核对的内容仍是草稿。</p>
            <BaseButton variant="primary" :loading="onboardingTransitioning" @click="finishLightOnboarding">先使用，稍后核对</BaseButton>
          </div>
        </section>
        </div>
        <div v-show="workspaceTab === 'review'" id="workspace-review" role="tabpanel" aria-labelledby="workspace-review-tab">
        <LearningCard v-if="decision && currentId" :key="`learning-${currentId}-${decision.id}`" :conversation-id="currentId" :decision="decision" />
        <ReviewOutcomePanel
          v-if="isReview && decision"
          :decision="decision"
          :busy="outcomeBusy"
          :error="outcomeError"
          :review-busy="reviewSaving"
          :review-error="reviewSaveError"
          @record="onRecordOutcome"
          @review="onSubmitReview"
        />
        </div>
        <div v-show="workspaceTab === 'draft'" id="workspace-draft" role="tabpanel" aria-labelledby="workspace-draft-tab">
        <LiveObjectPanel
          v-if="showDraftPanel"
          :key="draft?.id || currentId || 'pending-draft'"
          :draft="draft && draft.status !== 'discarded' ? draft : null"
          :changed-fields="draftChanged"
          :busy="draftBusy"
          :error="draftError"
          :pending="draftPending"
          :timed-out="draftTimedOut"
          @confirm="onConfirmDraft"
          @discard="onDiscardDraft"
          @retry="retryDraft"
        />
        <p v-if="!showDraftPanel" class="zj-workspace-empty">这份草稿已处理完成。可以关闭侧栏继续聊。</p>
        </div>
      </div>
    </SideDrawer>

    <RenameConversationDialog :conversation="renameTarget" :busy="renameTarget ? metadataBusy[renameTarget.id] : false" :error="renameError" @save="saveConversationName" @close="renameTarget = null" />
    <ConfirmDialog
      :open="!!deleteTarget"
      title="删除这段对话？"
      :message="`会删除「${deleteTarget?.title || '这段对话'}」的全部消息。已经确认进入本体的理解不会被删除。`"
      confirm-text="删除"
      danger
      :loading="deleting"
      @confirm="confirmDelete"
      @cancel="deleteTarget = null"
    />
  </div>
</template>

<style scoped>
.zj-file-followups { margin: 9px 0; display: grid; gap: 8px; font-size: 12px; color: var(--ws-text-secondary-color, #686b66); }
.zj-file-followups > div { display: flex; flex-wrap: wrap; gap: 8px; }
.zj-file-followups button { font: inherit; padding: 5px 10px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 14px; background: var(--ws-card-bg, #fff); color: var(--ws-primary-color, #a6452e); cursor: pointer; }
.zj-file-followups button:hover { background: var(--ws-surface-2, #fbf8f1); }
.zj-page {
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);
  gap: 20px;
  height: calc(100vh - 60px - 48px);
  max-width: 1280px;
  margin: 0 auto;
}
.zj-page__side {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 12px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.zj-page__side-toggle {
  display: none;
}
.zj-page__side-body {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}
.zj-page__main {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}
.zj-page__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 4px 12px;
  flex-wrap: wrap;
}
.zj-page__title-row { display:flex; align-items:flex-start; gap:10px; min-width:0; }.zj-page__title-row :deep(.zj-more) { flex:none; }
.zj-page__title { overflow-wrap:anywhere; }
.zj-page__archived { color:var(--ws-text-secondary-color,#686b66); font-size:12px; }
.zj-page__mobile-undo { display:none; align-items:center; gap:7px; font-size:12px; }.zj-page__mobile-undo button { border:0; padding:0; background:transparent; color:var(--ws-primary-color,#a6452e); font:inherit; cursor:pointer; }
.zj-archive-feedback { padding:8px; border:1px solid var(--ws-border-color-3,#ebe7de); border-radius:6px; font-size:12px; line-height:1.5; color:var(--ws-text-secondary-color,#686b66); overflow-wrap:anywhere; }
.zj-archive-feedback > span { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.zj-archive-feedback > div { display:flex; flex-wrap:wrap; gap:10px; margin-top:5px; }.zj-archive-feedback button { padding:0; border:0; color:var(--ws-primary-color,#a6452e); background:transparent; font:inherit; cursor:pointer; }
.zj-turn--search-hit { background:var(--ws-surface-2,#fbf8f1); outline:2px solid var(--ws-primary-color,#a6452e); outline-offset:3px; border-radius:8px; }
.zj-page__tools { display:flex; flex-wrap:wrap; align-items:center; gap:6px; }
.zj-page__tool { display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border:1px solid var(--ws-border-color-3,#ebe7de); border-radius:20px; background:transparent; color:var(--ws-text-secondary-color,#686b66); font:inherit; font-size:12px; white-space:nowrap; cursor:pointer; }
.zj-page__tool--draft { color:var(--ws-primary-color,#a6452e); background:var(--ws-surface-2,#fbf8f1); }.zj-page__tool span { font-size:11px; opacity:.8; }
.zj-workspace-tabs { display:flex; gap:8px; flex-wrap:wrap; }.zj-workspace-tabs button { padding:8px 12px; font:inherit; font-size:14px; color:var(--ws-text-secondary-color,#686b66); border:1px solid transparent; border-radius:8px; background:transparent; cursor:pointer; }.zj-workspace-tabs button[aria-selected=true] { color:var(--ws-primary-color,#a6452e); background:var(--ws-surface-2,#fbf8f1); border-color:var(--ws-border-color,#d8d3c8); }
.zj-workspace-content :deep(.zj-panel__body) { overflow:visible; }
.zj-workspace-content :deep(.zj-panel__context),.zj-workspace-content :deep(.zj-panel__row),.zj-workspace-content :deep(.zj-panel__form textarea) { font-size:15px; line-height:1.8; }
.zj-workspace-content [role=tabpanel] > * + * { margin-top:20px; }.zj-workspace-empty { font-size:14px; line-height:1.8; }
.zj-memory-draft { font-size:15px; line-height:1.8; overflow-wrap:anywhere; }
.zj-memory-draft__hint, .zj-memory-draft__record span { font-size:13px; color:var(--ws-text-secondary-color,#686b66); }
.zj-memory-draft__summary { white-space:pre-wrap; }
.zj-memory-draft__record { padding:14px; border:1px solid var(--ws-border-color,#d8d3c8); border-radius:8px; background:var(--ws-card-bg,#fff); }
.zj-memory-draft__record p { margin:8px 0; }
.zj-memory-draft summary { cursor:pointer; color:var(--ws-text-secondary-color,#686b66); }
.zj-memory-draft ol { padding-left:20px; }
.zj-memory-draft li + li { margin-top:16px; }
.zj-memory-draft li p { margin:0; }
.zj-memory-draft blockquote { margin:6px 0 0; padding-left:10px; border-left:2px solid var(--ws-border-color,#d8d3c8); font-size:13px; color:var(--ws-text-secondary-color,#686b66); }
.zj-memory-draft__actions { display:flex; flex-wrap:wrap; gap:8px; }
.zj-page__title {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: var(--ws-display-2, 20px);
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-page__pending {
  color: var(--ws-muted-color, #8a8d88);
}
.zj-page__dot {
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-page__model-link {
  color: var(--ws-primary-color, #a6452e);
  text-decoration: underline;
}
.zj-turn__note {
  max-width: 760px;
  margin: 4px 0 0 4px;
  font-size: 12px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-page__status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-page__body {
  display: flex;
  flex: 1;
  min-height: 0;
  gap: 16px;
}
.zj-page__stream {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  max-width: 880px;
  margin: 0 auto;
}
.zj-onb {
  padding: 14px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.zj-onb__title {
  margin: 0 0 8px;
  font-family: var(--ws-font-display, serif);
  font-size: var(--ws-display-3, 16px);
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-onb__counts {
  margin: 8px 0 10px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
  text-align: center;
}
.zj-onb__steps {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.zj-onb__step {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  line-height: 1.3;
  text-align: center;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-onb__dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 1.5px solid var(--ws-border-color, #d8d3c8);
  background: transparent;
}
.zj-onb__step.is-done .zj-onb__dot {
  background: var(--ws-text-primary-color, #1d211f);
  border-color: var(--ws-text-primary-color, #1d211f);
}
.zj-onb__step.is-current {
  color: var(--ws-primary-color, #a6452e);
  font-weight: 700;
}
.zj-onb__step.is-current .zj-onb__dot {
  border-color: var(--ws-primary-color, #a6452e);
  background: var(--ws-primary-color, #a6452e);
  box-shadow: 0 0 0 3px rgba(166, 69, 46, 0.18);
}
.zj-onb__hint {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-onb__done {
  margin-top: 12px;
  padding: 12px;
  border: 1px dashed var(--ws-primary-color, #a6452e);
  border-radius: var(--ws-radius-lg, 8px);
}
.zj-onb__done h3 {
  margin: 0 0 6px;
  font-family: var(--ws-font-display, serif);
  font-size: 14px;
}
.zj-onb__done p {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-page__messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 4px 4px 24px;
  scroll-padding-block: 12px 24px;
}
.zj-turn {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.zj-turn--user {
  align-items: flex-end;
}
.zj-page__composer {
  padding-top: 8px;
  flex-shrink: 0;
}
.zj-intro {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  max-width: 640px;
  margin: 24px auto;
  padding: 28px 32px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  color: var(--ws-primary-color, #a6452e);
}
.zj-intro h2 {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: var(--ws-display-1, 26px);
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-intro p {
  margin: 0;
  font-size: 14px;
  line-height: 1.8;
  color: var(--ws-text-color, #3c403d);
}
.zj-intro__explainer {
  margin: 8px auto 4px;
}
.zj-intro__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.zj-intro__trust,
.zj-intro__hint {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-intro__hint {
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-intro__resume {
  padding: 0;
  border: none;
  background: transparent;
  font-family: inherit;
  font-size: 13px;
  color: var(--ws-primary-color, #a6452e);
  text-decoration: underline;
  cursor: pointer;
}
.zj-blank__resume {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  width: 100%;
  margin: 18px 0 0;
  padding: 14px;
  border: 1px dashed var(--ws-primary-color, #a6452e);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}
.zj-blank__resume:hover {
  border-style: solid;
}
.zj-blank__resume-title {
  font-family: var(--ws-font-display, serif);
  font-size: 15px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-blank__resume-desc {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-blank {
  margin: 48px auto 0;
  max-width: 560px;
  text-align: center;
}
.zj-blank__lead {
  margin: 0 0 8px;
  font-family: var(--ws-font-display, serif);
  font-size: 18px;
  line-height: 1.7;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-blank__hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-blank__cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 18px 0 14px;
  text-align: left;
}
.zj-blank__card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;
}
.zj-blank__card:hover {
  border-color: var(--ws-primary-color, #a6452e);
  transform: translateY(-1px);
}
.zj-blank__card-title {
  font-family: var(--ws-font-display, serif);
  font-size: 15px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-blank__card-desc {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
@media (max-width: 767px) {
  .zj-page__mobile-undo { display:inline-flex; }
  .zj-blank__cards {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 1199px) {
  .zj-page {
    grid-template-columns: 200px minmax(0, 1fr);
  }
}

@media (max-width: 767px) {
  .zj-page {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
    gap: 10px;
    height: calc(100dvh - 60px - 32px);
  }
  .zj-page__side {
    padding: 0;
  }
  .zj-page__side-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: 10px 12px;
    border: none;
    background: transparent;
    color: var(--ws-text-color, #3c403d);
    font-family: inherit;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }
  .zj-page__side-body {
    display: none;
    padding: 0 12px 12px;
    max-height: 40vh;
    overflow-y: auto;
  }
  .zj-page__side.is-open .zj-page__side-body {
    display: flex;
  }
  .zj-intro {
    padding: 20px;
    margin: 12px 0;
  }
}
</style>
