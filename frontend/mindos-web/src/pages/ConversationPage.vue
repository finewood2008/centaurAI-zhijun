<script setup lang="ts">
// 对话页：一段持续关系。左栏会话列表，主区消息流 + 输入区。
// 流式回复（SSE）→ 出处条 → 抽取候选（轮询 inbox）→ 一键确认写回本体。
// 切页不截断：卸载时不中断正在进行的流（服务端把这轮生成完并落库），只有用户点「停止」才中断；
// 卸载后的回调用 alive 守卫，不再碰已销毁的状态。
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
  getInbox,
  getOntologyStats,
  getZhijunStatus,
  listClaims,
  listConversations,
  recordConversationOutcome,
  reviewClaim,
  type Claim,
  type Conversation,
  type ConversationOutcomes,
  type DecisionDraft,
  type DecisionDraftConfirmPayload,
  type DecisionDraftEvent,
  type ExtractionEvent,
  type GrowthDecision,
  type GrowthToday,
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
import { createSessionGate } from '@/composables/sessionGate'
import { reviewNote } from '@/shared/ontology'
import { MODEL_UNAVAILABLE_TEXT, channelShort, modelUnavailable } from '@/shared/model'
import {
  EXTRACTION_STILL_WORKING,
  ONBOARDING_TOTAL_TURNS,
  buildNextSteps,
  extractionSkipNote,
  hasConversationOutcomes,
  headerAggregateItems,
  isDueByToday,
  onboardingUserTurns,
} from '@/shared/labels'
import MessageBubble from '@/components/conversation/MessageBubble.vue'
import ClaimCandidateChip from '@/components/conversation/ClaimCandidateChip.vue'
import ProvenanceStrip from '@/components/conversation/ProvenanceStrip.vue'
import Composer from '@/components/conversation/Composer.vue'
import ConversationList from '@/components/conversation/ConversationList.vue'
import LiveObjectPanel from '@/components/conversation/LiveObjectPanel.vue'
import NudgeStrip from '@/components/conversation/NudgeStrip.vue'
import OutcomesCard from '@/components/conversation/OutcomesCard.vue'
import NextStepsPanel from '@/components/conversation/NextStepsPanel.vue'
import ReviewOutcomePanel from '@/components/conversation/ReviewOutcomePanel.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import OntologyExplainer from '@/components/ontology/OntologyExplainer.vue'
import SelfMap from '@/components/ontology/SelfMap.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'

interface UiMessage extends Message {
  provenance?: ProvenanceEvent | null
  turnMeta?: TurnMetaEvent | null
  candidates?: Claim[]
  streaming?: boolean
  // 抽取被跳过 / 还在整理时，这条回复下方的一行灰字
  extractionNote?: string
}

const route = useRoute()
const router = useRouter()
const toast = useToast()

const status = ref<ZhijunStatus | null>(null)
const stats = ref<OntologyStats | null>(null)
// stats 请求结束了没有（成功或失败）；失败时 stats 保持 null，绝不把 hasOntology 当真
const statsLoaded = ref(false)
// 组件还活着：卸载后流的回调不再写状态
let alive = true
let mounted = false
const conversations = ref<Conversation[]>([])
const conversationsLoading = ref(true)
const current = ref<Conversation | null>(null)
const messages = ref<UiMessage[]>([])
const messagesLoading = ref(false)
const messagesError = ref('')
const streaming = ref(false)
const listOpen = ref(false)
const deleteTarget = ref<Conversation | null>(null)
const deleting = ref(false)
const reviewBusy = reactive<Record<string, boolean>>({})
const listRef = ref<HTMLElement | null>(null)
const composerRef = ref<InstanceType<typeof Composer> | null>(null)
const nudgeRef = ref<InstanceType<typeof NudgeStrip> | null>(null)

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

// 页头聚合行与空白态「下一步」的数据：判断页的今日概况 + 到期的承诺（取不到就当没有）
const growthToday = ref<GrowthToday | null>(null)
const dueCommitments = ref<Claim[]>([])
// 「这段对话留下的」：本会话这次聊完（message_done 之后）拉到的产出；切会话清空
const turnOutcomes = ref<ConversationOutcomes | null>(null)

const isReview = computed(() => current.value?.mode === 'review')
const isOnboarding = computed(() => current.value?.mode === 'onboarding')
const showDraftPanel = computed(() => (!!draft.value && draft.value.status !== 'discarded') || draftPending.value || draftTimedOut.value)
const showSidePanel = computed(() => showDraftPanel.value || (isReview.value && !!decision.value) || isOnboarding.value)

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
const mapClaims = ref<Claim[]>([])
const newClaimIds = ref<Set<string>>(new Set())
let glowTimer: number | null = null
const mapPollGate = createSessionGate()

const highlightSection = computed<Section | null>(() => {
  const step = onboardingStep.value
  if (!step || step < 1 || step > ONBOARDING_STEPS.length) return null
  return ONBOARDING_STEPS[step - 1].section
})
// 收尾那一轮还在生成时不算「聊完」：面板要等 message_done 之后才出现
const closingStreaming = ref(false)
const onboardingDone = computed(() => (onboardingStep.value ?? 0) >= ONBOARDING_STEPS.length + 1 && !closingStreaming.value)
const onboardingCounts = computed(() => ({
  confirmed: stats.value?.claims?.confirmed ?? mapClaims.value.filter((c) => c.trustState === 'confirmed').length,
  working: stats.value?.inbox ?? mapClaims.value.filter((c) => c.trustState === 'working').length,
}))

function stepFromMessages(): number | null {
  // 刷新后没有 SSE meta：用户已发 k 条 → 知君刚问的是第 k 个问题（k ≤ 7），之后是收尾
  const userTurns = messages.value.filter((m) => m.role === 'user').length
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
    pushNote(`你记下了一个判断：${result.decision.title}（选了「${result.decision.choice}」，把握 ${result.decision.confidence}%）`, {
      kind: 'decision_confirmed',
      decisionId: result.decision.id,
    })
    toast({ type: 'success', message: '已记进判断簿，到期知君会来回访' })
    void refreshOutcomes(current.value.id, true)
    void loadGrowthToday()
    await scrollToBottom()
  } catch (err) {
    if (err instanceof ApiError && err.status === 400) draftError.value = err.message
    else toast({ type: 'error', message: friendlyError(err, '记录失败') })
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
    nudgeRef.value?.reload()
    void refreshOutcomes(current.value.id, true)
    void loadGrowthToday()
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
    pushNote('复盘已记下，经验会变成你原则的候选', { kind: 'review_recorded', decisionId: d.id, reviewId: result.review.id })
    nudgeRef.value?.reload()
    void refreshOutcomes(current.value.id, true)
    void loadGrowthToday()
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

async function loadGrowthToday() {
  try {
    const next = await api.getGrowthToday()
    if (!alive) return
    growthToday.value = next
  } catch {
    if (!alive) return
    growthToday.value = null
  }
}

async function loadDueCommitments() {
  try {
    const res = await listClaims({ trust: ['confirmed', 'working'], limit: 500 })
    if (!alive) return
    dueCommitments.value = res.items.filter((c) => c.predicate === 'committed_to' && isDueByToday(c.validTo))
  } catch {
    if (!alive) return
    dueCommitments.value = []
  }
}

// 页头标题下那行灰字：待确认 / 待回访（逾期）/ 待复盘 / 还在整理；只列非零项，无红点无徽章
const headerItems = computed(() => {
  const g = growthToday.value?.stats
  return headerAggregateItems({
    inbox: stats.value?.inbox,
    dueReview: (g?.dueSoonDecisions ?? 0) + (g?.overdueDecisions ?? 0),
    overdue: g?.overdueDecisions,
    pendingReviews: g?.pendingReviews,
    pendingJobs: status.value?.pendingJobs,
  })
})

// 空白态「下一步」：最多三条
const nextSteps = computed(() => {
  const g = growthToday.value
  return buildNextSteps({
    overdue: (g?.dueDecisions ?? []).filter((d) => d.dueState === 'overdue').map((d) => ({ id: d.id, title: d.title })),
    pendingReviews: (g?.pendingReviews ?? []).map((d) => ({ id: d.id, title: d.title })),
    dueCommitments: dueCommitments.value.map((c) => ({ id: c.id, content: c.content })),
    inbox: stats.value?.inbox,
  })
})

const loadGate = createSessionGate()
const pollGate = createSessionGate()
const seenClaimIds = new Set<string>()
let abortController: AbortController | null = null
// 新建会话后先本地替换路由，再由本页继续流式；此时跳过 watcher 的重新加载。
let skipLoadFor: string | null = null

const currentId = computed(() => {
  const id = route.params.conversationId
  return typeof id === 'string' && id ? id : null
})

// 建档入口：不存在 mode=onboarding 的会话，且（stats 未知 或 hasOntology 为假）→ 先认识你
const onboardingConversation = computed(() => conversations.value.find((c) => c.mode === 'onboarding') ?? null)
const landingReady = computed(() => !conversationsLoading.value && statsLoaded.value)
const showIntro = computed(
  () => !currentId.value && landingReady.value && !onboardingConversation.value && (stats.value === null || !stats.value.hasOntology),
)
const showBlank = computed(() => !currentId.value && !messages.value.length && landingReady.value && !showIntro.value)
// 没聊完的建档会话（用户轮数 < 8）：空白态给一张「继续建档」卡
const pendingOnboarding = computed(() => {
  const conv = onboardingConversation.value
  if (!conv) return null
  const turns = onboardingUserTurns(conv.messageCount)
  if (turns >= ONBOARDING_TOTAL_TURNS) return null
  const remaining = ONBOARDING_STEPS.length - Math.max(0, turns - 1)
  return { id: conv.id, remaining }
})

const headerTitle = computed(() => {
  if (current.value) return current.value.title || (current.value.mode === 'onboarding' ? '第一次对话' : '对话')
  return '对话'
})

const statusLine = computed(() => channelShort(status.value))
// 模型没配置 / 不可用：页头改成「还没配置模型 · 去偏好」，输入区禁用并显示同一句
const modelBlocked = computed(() => modelUnavailable(status.value))
// 后台还没整理完的事（抽取 / 草稿 / 摘要），页头只用一句淡字提示
const pendingJobs = computed(() => status.value?.pendingJobs ?? 0)

// ---- 空白态的三张起手卡：点一下把话头放进输入框，不自动发送
interface Starter { title: string; desc: string; text: string; deliberate?: boolean }
const STARTERS: Starter[] = [
  { title: '我在考虑一件事', desc: '把选项、倾向和把握说清楚，聊完会落成判断簿里一条可回访的记录', text: '我在考虑一件事：', deliberate: true },
  { title: '最近发生了……', desc: '说说这周让你在意的事，知君会把它和你以前说过的连起来', text: '最近发生了一件事，' },
  { title: '你怎么看我？', desc: '让知君用它目前对你的认识说说看，不对的地方你直接改', text: '基于你目前对我的认识，说说你眼中的我，哪些地方你其实不确定？' },
]
function useStarter(s: Starter) {
  composerRef.value?.setDeliberate(!!s.deliberate)
  composerRef.value?.setText(s.text)
}

// 从提醒条 / 其它页带着话头过来：?say=… → 放进输入框，然后把 query 清掉
watch(
  () => route.query.say,
  (v) => {
    if (typeof v !== 'string' || !v) return
    void nextTick(() => {
      composerRef.value?.setText(v)
      void router.replace({ path: route.path, query: {} })
    })
  },
  { immediate: true },
)

function friendlyError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return '这段对话还在生成中，请稍等再发。'
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

async function seedSeenClaims() {
  try {
    const inbox = await getInbox(50)
    for (const item of inbox.items) seenClaimIds.add(item.id)
  } catch {
    // inbox 不可用不影响对话
  }
}

async function loadConversations() {
  try {
    const res = await listConversations(50)
    if (!alive) return
    conversations.value = res.items
  } catch (err) {
    if (!alive) return
    toast({ type: 'error', message: friendlyError(err, '会话列表加载失败') })
  } finally {
    conversationsLoading.value = false
  }
}

function toUi(m: Message): UiMessage {
  // 历史回复：用后端由回执还原的出处播种，让旧回复也能展示出处条与出处小图；直播的 SSE provenance 会覆盖它。
  const seeded = m.role === 'assistant' && m.provenance ? (m.provenance as ProvenanceEvent) : null
  return reactive({ ...m, provenance: seeded, turnMeta: null, candidates: [], streaming: false }) as UiMessage
}

async function loadConversation(id: string) {
  const session = loadGate.next()
  messagesLoading.value = true
  messagesError.value = ''
  try {
    const detail = await getConversation(id)
    if (!loadGate.isCurrent(session)) return
    current.value = detail.conversation
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
    if (detail.conversation.mode === 'onboarding') {
      onboardingStep.value = stepFromMessages()
      void loadMapClaims()
    } else {
      onboardingStep.value = null
    }
    await scrollToBottom()
  } catch (err) {
    if (!loadGate.isCurrent(session)) return
    if (err instanceof ApiError && err.status === 404) {
      toast({ type: 'error', message: '会话不存在' })
      router.replace('/')
      return
    }
    messagesError.value = friendlyError(err, '会话加载失败')
  } finally {
    if (loadGate.isCurrent(session)) messagesLoading.value = false
  }
}

function resetToLanding() {
  loadGate.invalidate()
  pollGate.invalidate()
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
  // 从会话回到空白态：页头聚合行与「下一步」用最新的数据（首次进页由 onMounted 负责，不重复拉）
  if (mounted) {
    void loadGrowthToday()
    void loadDueCommitments()
    void loadStats()
  }
  newClaimIds.value = new Set()
  mapPollGate.invalidate()
}

watch(
  currentId,
  (id) => {
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
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

function selectConversation(id: string) {
  listOpen.value = false
  if (id !== currentId.value) router.push(`/c/${encodeURIComponent(id)}`)
}

function newConversation() {
  listOpen.value = false
  if (currentId.value) router.push('/')
  nextTick(() => composerRef.value?.focus())
}

function askDelete(id: string) {
  deleteTarget.value = conversations.value.find((c) => c.id === id) ?? null
}

async function confirmDelete() {
  const target = deleteTarget.value
  if (!target) return
  deleting.value = true
  try {
    await deleteConversation(target.id)
    conversations.value = conversations.value.filter((c) => c.id !== target.id)
    if (currentId.value === target.id) router.replace('/')
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
  const conv = await createConversation({ mode })
  current.value = conv
  conversations.value = [conv, ...conversations.value]
  skipLoadFor = conv.id
  await router.replace(`/c/${encodeURIComponent(conv.id)}`)
  return conv
}

async function send(content: string, depth: 'brief' | 'deep', mode: TurnMode = 'chat') {
  if (streaming.value) return
  if (modelBlocked.value) {
    composerRef.value?.setText(content)
    toast({ type: 'error', message: `${MODEL_UNAVAILABLE_TEXT}，先到「偏好」里配置` })
    return
  }
  // 「先认识你」态下直接打字：进的是建档会话，不是普通对话
  const wantOnboarding = showIntro.value
  streaming.value = true
  let conv: Conversation
  try {
    conv = await ensureConversation(wantOnboarding ? 'onboarding' : 'chat')
  } catch (err) {
    streaming.value = false
    composerRef.value?.setText(content)
    toast({ type: 'error', message: friendlyError(err, '无法创建会话') })
    return
  }
  await streamTurn(conv, content, depth, wantOnboarding ? 'chat' : mode)
}

async function startOnboarding() {
  if (streaming.value) return
  if (modelBlocked.value) {
    toast({ type: 'error', message: `${MODEL_UNAVAILABLE_TEXT}，先到「偏好」里配置` })
    return
  }
  streaming.value = true
  let conv: Conversation
  try {
    conv = await ensureConversation('onboarding')
  } catch (err) {
    streaming.value = false
    toast({ type: 'error', message: friendlyError(err, '无法开始第一次对话') })
    return
  }
  await streamTurn(conv, '你好，我们开始吧', 'brief')
}

async function streamTurn(conv: Conversation, content: string, depth: 'brief' | 'deep', mode: TurnMode = 'chat') {
  const now = new Date().toISOString()
  const seqBase = messages.value.length
  const userMsg = reactive<UiMessage>({
    id: `local-user-${Date.now()}`,
    conversationId: conv.id,
    seq: seqBase + 1,
    role: 'user',
    content,
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
    candidates: [],
    streaming: true,
  })
  messages.value.push(userMsg, assistant)
  await scrollToBottom()

  // 首个 token 之前失败：把原文放回输入框，并撤掉刚插入的两个气泡
  let gotToken = false
  const rollback = () => {
    if (!alive) return
    messages.value = messages.value.filter((m) => m !== userMsg && m !== assistant)
    composerRef.value?.setText(content)
  }

  abortController = new AbortController()
  const signal = abortController.signal
  try {
    await streamPost(
      `/mindos/conversations/${encodeURIComponent(conv.id)}/messages`,
      { content, depth, mode },
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
          assistant.turnMeta = m
          assistant.provider = m.provider
          assistant.model = m.model
          assistant.external = m.external
          if (!alive) return
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
            void pollInbox(assistant)
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
        },
        error: (d) => {
          const e = d as StreamErrorEvent
          assistant.status = 'error'
          if (!alive) return
          if (!gotToken) rollback()
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
        if (!gotToken) rollback()
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
      void refreshOutcomes(conv.id, true)
      await scrollToBottom()
    }
  }
}

// 只有用户显式点「停止」才中断；切页不中断，让服务端把这轮生成完并落库
function stop() {
  abortController?.abort()
}

// 候选轮询：每 3 秒一次，最多 120 秒；超时还没出来就留一行灰字，等页头 pendingJobs 归零再补拉
async function pollInbox(assistant: UiMessage) {
  const session = pollGate.next()
  let found = false
  assistant.extractionNote = ''
  for (let i = 0; i < 40; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 3000))
    if (!pollGate.isCurrent(session) || !alive) return
    try {
      const inbox = await getInbox(20)
      if (!pollGate.isCurrent(session) || !alive) return
      const fresh = inbox.items.filter((c) => !seenClaimIds.has(c.id))
      if (fresh.length) {
        for (const c of fresh) seenClaimIds.add(c.id)
        assistant.candidates = [...(assistant.candidates ?? []), ...fresh]
        void loadStats()
        await scrollToBottom()
        if (found) return
        found = true
      } else if (found) {
        return
      }
    } catch {
      // 轮询失败不打扰用户
    }
  }
  if (!found && pollGate.isCurrent(session) && alive) assistant.extractionNote = EXTRACTION_STILL_WORKING
}

// 页头「还在整理」从 >0 变成 0：对最近 3 条知君回复各再拉一次候选挂上去（按证据里的 messageId 归位，对不上的挂最近一条）
async function attachLateCandidates() {
  const recent = messages.value.filter((m) => m.role === 'assistant' && !m.streaming).slice(-3)
  if (!recent.length) return
  try {
    const inbox = await getInbox(50)
    if (!alive) return
    const fresh = inbox.items.filter((c) => !seenClaimIds.has(c.id))
    if (!fresh.length) return
    const byId = new Map(recent.map((m) => [m.id, m] as const))
    const fallback = recent[recent.length - 1]
    for (const c of fresh) {
      seenClaimIds.add(c.id)
      const hit = (c.evidence ?? []).map((ev) => ev.messageId).find((id): id is string => !!id && byId.has(id))
      const target = hit ? byId.get(hit)! : fallback
      target.candidates = [...(target.candidates ?? []), c]
    }
    for (const m of recent) if (m.candidates?.length) m.extractionNote = ''
    void loadStats()
    await scrollToBottom()
  } catch {
    // 补拉失败不打扰
  }
}

watch(pendingJobs, (n, old) => {
  if ((old ?? 0) > 0 && n === 0 && current.value) {
    void attachLateCandidates()
    // 「这段对话留下的」再刷一次：整理完的理解会补进来
    if (turnOutcomes.value) void refreshOutcomes(current.value.id, true)
  }
})

async function onReview(assistant: UiMessage, claim: Claim, action: ReviewAction, editedContent?: string) {
  if (!current.value) return
  reviewBusy[claim.id] = true
  try {
    const result = await reviewClaim(claim.id, {
      action,
      editedContent,
      surface: current.value.mode === 'onboarding' ? 'onboarding' : 'conversation',
      conversationId: current.value.id,
      messageId: assistant.id.startsWith('local-') ? undefined : assistant.id,
    })
    assistant.candidates = (assistant.candidates ?? []).filter((c) => c.id !== claim.id)
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
  if (m) router.push(`/materials/${encodeURIComponent(m.materialId)}`)
}

onMounted(() => {
  mounted = true
  void loadStatus()
  void loadStats()
  void seedSeenClaims()
  void loadConversations()
  void loadGrowthToday()
  void loadDueCommitments()
})

onBeforeUnmount(() => {
  // 不 abort 正在进行的流：服务端会把这轮生成完并落库，回来时从服务端重载
  alive = false
  loadGate.invalidate()
  pollGate.invalidate()
  draftPollGate.invalidate()
  mapPollGate.invalidate()
  if (statusTimer) window.clearTimeout(statusTimer)
  if (glowTimer) window.clearTimeout(glowTimer)
})
</script>

<template>
  <div class="zj-page">
    <aside class="zj-page__side" :class="{ 'is-open': listOpen }">
      <button type="button" class="zj-page__side-toggle" :aria-expanded="listOpen" @click="listOpen = !listOpen">
        <span>会话（{{ conversations.length }}）</span>
        <component :is="listOpen ? ChevronUp : ChevronDown" :size="16" aria-hidden="true" />
      </button>
      <div class="zj-page__side-body">
        <ConversationList
          :items="conversations"
          :current-id="currentId"
          :loading="conversationsLoading"
          @select="selectConversation"
          @create="newConversation"
          @remove="askDelete"
        />
      </div>
    </aside>

    <section class="zj-page__main" :class="{ 'has-panel': showSidePanel }" aria-live="polite">
      <header class="zj-page__head">
        <div>
          <h1 class="zj-page__title">{{ headerTitle }}</h1>
          <p class="zj-page__status">
            <RouterLink v-if="modelBlocked" to="/settings" class="zj-page__model-link">{{ MODEL_UNAVAILABLE_TEXT }} · 去偏好</RouterLink>
            <span v-else-if="statusLine">{{ statusLine }}</span>
            <template v-for="(it, i) in headerItems" :key="it.key">
              <span v-if="i > 0 || modelBlocked || statusLine" class="zj-page__dot" aria-hidden="true">·</span>
              <RouterLink v-if="it.to" :to="it.to" class="zj-page__agg" :data-testid="`head-${it.key}`">{{ it.text }}</RouterLink>
              <span v-else class="zj-page__pending" :data-testid="`head-${it.key}`">{{ it.text }}</span>
            </template>
            <span v-if="isReview" class="zj-seal zj-seal--accent">回访</span>
            <span v-if="status && !status.workerRunning" class="zj-seal zj-seal--warning" title="知君暂时不会从对话里提出新的理解">整理暂停</span>
          </p>
        </div>
      </header>

      <NudgeStrip ref="nudgeRef" class="zj-page__nudges" @say="(t) => composerRef?.setText(t)" />

      <div class="zj-page__body">
      <div class="zj-page__stream">
      <div ref="listRef" class="zj-page__messages">
        <div v-if="showIntro" class="zj-intro">
          <Sparkles :size="22" aria-hidden="true" />
          <h2>先让我认识真实的你</h2>
          <p>知君不会凭空给建议，也不会只凭一次聊天定义你。先聊十几分钟：你是谁、在做什么、最在意的人、最近一个判断、一条你认同的原则、以及不想让 AI 碰的领域。</p>
          <OntologyExplainer class="zj-intro__explainer" compact />
          <p class="zj-intro__trust">原件留在设备内 · 只有经过你确认的理解才会留下</p>
          <BaseButton variant="primary" :loading="streaming" @click="startOnboarding">开始第一次对话</BaseButton>
          <p class="zj-intro__hint">也可以直接在下面打字，知君会从认识你开始。</p>
        </div>

        <div v-else-if="showBlank" class="zj-blank">
          <p class="zj-blank__lead">带一件正在拿主意的事来。聊完，它会变成一条你确认过、到期知君会来回访的判断。</p>
          <button v-if="pendingOnboarding" type="button" class="zj-blank__resume" data-testid="resume-onboarding" @click="selectConversation(pendingOnboarding.id)">
            <span class="zj-seal zj-seal--accent">建档</span>
            <span class="zj-blank__resume-title">继续建档 · 还差 {{ pendingOnboarding.remaining }} 问</span>
            <span class="zj-blank__resume-desc">上次认识你的对话还没聊完，接着聊，本体图会继续亮起来。</span>
          </button>
          <NextStepsPanel :items="nextSteps" @say="(t) => composerRef?.setText(t)" />
          <div class="zj-blank__cards" role="group" aria-label="起个头">
            <button v-for="s in STARTERS" :key="s.title" type="button" class="zj-blank__card" @click="useStarter(s)">
              <span class="zj-blank__card-title">{{ s.title }}</span>
              <span class="zj-blank__card-desc">{{ s.desc }}</span>
            </button>
          </div>
          <p class="zj-blank__hint">知君会基于你确认过的理解来回应，并标出哪些是你说过的、哪些只是它的推测。</p>
        </div>

        <div v-if="messagesLoading" class="loading-state">正在打开会话…</div>
        <ErrorState v-else-if="messagesError" :message="messagesError" retry-label="重试" @retry="currentId && loadConversation(currentId)" />

        <template v-for="m in messages" :key="m.id">
          <div class="zj-turn" :class="`zj-turn--${m.role}`">
            <MessageBubble
              :role="m.role === 'system' && m.meta?.kind === 'review_open' ? 'assistant' : m.role"
              :content="m.content"
              :status="m.status"
              :streaming="m.streaming"
              @cite="(n) => onCite(m, n)"
            />
            <ProvenanceStrip v-if="m.role === 'assistant' && m.provenance" :provenance="m.provenance" :meta="m.turnMeta" />
            <p v-if="m.role === 'assistant' && m.extractionNote" class="zj-turn__note" data-testid="extraction-note">{{ m.extractionNote }}</p>
            <template v-if="m.role === 'assistant' && m.candidates && m.candidates.length">
              <ClaimCandidateChip
                v-for="c in m.candidates"
                :key="c.id"
                :claim="c"
                :busy="!!reviewBusy[c.id]"
                @review="(action, edited) => onReview(m, c, action, edited)"
              />
            </template>
          </div>
        </template>
        <OutcomesCard v-if="showOutcomesCard && turnOutcomes" :outcomes="turnOutcomes" />
      </div>

      <div class="zj-page__composer">
        <Composer
          ref="composerRef"
          :streaming="streaming"
          :disabled="messagesLoading"
          :allow-deliberate="!isReview && current?.mode !== 'onboarding' && !showIntro"
          :notice="modelBlocked ? MODEL_UNAVAILABLE_TEXT : undefined"
          notice-to="/settings"
          :placeholder="current?.mode === 'onboarding' || showIntro ? '回答知君的问题，或者说说你想先聊什么…' : isReview ? '说说实际发生了什么，和预期比差在哪…' : undefined"
          @send="send"
          @stop="stop"
        />
      </div>
      </div>

      <div v-if="showSidePanel" class="zj-page__panel">
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
          <ol class="zj-onb__steps" data-testid="onboarding-steps" aria-label="认识你的七个问题">
            <li
              v-for="(s, i) in ONBOARDING_STEPS"
              :key="s.title"
              class="zj-onb__step"
              :class="{ 'is-done': (onboardingStep ?? 0) > i + 1, 'is-current': onboardingStep === i + 1 }"
              :aria-current="onboardingStep === i + 1 ? 'step' : undefined"
            >
              <span class="zj-onb__dot" aria-hidden="true" />
              <span class="zj-onb__label">{{ s.title }}</span>
            </li>
          </ol>
          <p class="zj-onb__hint">认识你的七个问题 · 每答一个，对应的那一片就会亮起来。</p>
          <div v-if="onboardingDone" class="zj-onb__done">
            <h3>这是我目前对你的认识</h3>
            <p>七个问题都聊过了。去核对一遍：对的点个头，不对的直接改。</p>
            <BaseButton variant="primary" @click="router.push('/me/inbox')">去核对</BaseButton>
          </div>
        </section>
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
        <LiveObjectPanel
          v-if="showDraftPanel"
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
      </div>
      </div>
    </section>

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
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  padding: 0 4px 12px;
}
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
.zj-page__agg {
  color: var(--ws-text-secondary-color, #686b66);
  text-decoration: none;
  border-bottom: 1px dotted var(--ws-border-color, #d8d3c8);
}
.zj-page__agg:hover {
  color: var(--ws-primary-color, #a6452e);
  border-bottom-color: currentColor;
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
.zj-page__nudges {
  margin: 0 4px 10px;
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
}
.zj-page__panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 35%;
  max-width: 420px;
  min-width: 280px;
  min-height: 0;
  overflow-y: auto;
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
  padding: 4px 4px 12px;
}
.zj-turn {
  display: flex;
  flex-direction: column;
}
.zj-turn--user {
  align-items: flex-end;
}
.zj-page__composer {
  padding-top: 8px;
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
.zj-intro__trust,
.zj-intro__hint {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-intro__hint {
  color: var(--ws-text-placeholder-color, #a3a69f);
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
  font-size: 13px;
  line-height: 1.7;
  color: var(--ws-text-secondary-color, #686b66);
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
  .zj-blank__cards {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 1199px) {
  .zj-page {
    grid-template-columns: 200px minmax(0, 1fr);
  }
}

@media (max-width: 1023px) {
  .zj-page__body {
    flex-direction: column;
    overflow-y: auto;
  }
  .zj-page__stream {
    flex: none;
    min-height: 50vh;
  }
  .zj-page__panel {
    width: auto;
    max-width: none;
    min-width: 0;
    overflow: visible;
  }
}

@media (max-width: 767px) {
  .zj-page {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
    gap: 10px;
    height: calc(100vh - 60px - 32px);
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
