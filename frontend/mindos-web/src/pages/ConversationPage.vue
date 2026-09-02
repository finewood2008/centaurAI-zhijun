<script setup lang="ts">
// 对话页：一段持续关系。左栏会话列表，主区消息流 + 输入区。
// 流式回复（SSE）→ 出处条 → 抽取候选（轮询 inbox）→ 一键确认写回本体。
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronDown, ChevronUp, Sparkles } from 'lucide-vue-next'
import {
  ApiError,
  confirmDecisionDraft,
  createConversation,
  deleteConversation,
  discardDecisionDraft,
  getConversation,
  getInbox,
  getOntologyStats,
  getZhijunStatus,
  listConversations,
  recordConversationOutcome,
  reviewClaim,
  type Claim,
  type Conversation,
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
  type StreamErrorEvent,
  type TurnMetaEvent,
  type TurnMode,
  type ZhijunStatus,
} from '@/services/api'
import { streamPost } from '@/services/sse'
import { useToast } from '@/composables/useToast'
import { createSessionGate } from '@/composables/sessionGate'
import { reviewNote } from '@/shared/ontology'
import MessageBubble from '@/components/conversation/MessageBubble.vue'
import ClaimCandidateChip from '@/components/conversation/ClaimCandidateChip.vue'
import ProvenanceStrip from '@/components/conversation/ProvenanceStrip.vue'
import Composer from '@/components/conversation/Composer.vue'
import ConversationList from '@/components/conversation/ConversationList.vue'
import LiveObjectPanel from '@/components/conversation/LiveObjectPanel.vue'
import NudgeStrip from '@/components/conversation/NudgeStrip.vue'
import ReviewOutcomePanel from '@/components/conversation/ReviewOutcomePanel.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'

interface UiMessage extends Message {
  provenance?: ProvenanceEvent | null
  turnMeta?: TurnMetaEvent | null
  candidates?: Claim[]
  streaming?: boolean
}

const route = useRoute()
const router = useRouter()
const toast = useToast()

const status = ref<ZhijunStatus | null>(null)
const stats = ref<OntologyStats | null>(null)
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
const decision = ref<GrowthDecision | null>(null)
const outcomeBusy = ref(false)
const outcomeError = ref('')

const isReview = computed(() => current.value?.mode === 'review')
const showDraftPanel = computed(() => !!draft.value && draft.value.status !== 'discarded')
const showSidePanel = computed(() => showDraftPanel.value || (isReview.value && !!decision.value))

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

async function onRecordOutcome(payload: { result: string; notes: string }) {
  if (!current.value || outcomeBusy.value) return
  outcomeBusy.value = true
  outcomeError.value = ''
  try {
    const res = await recordConversationOutcome(current.value.id, payload)
    decision.value = res.decision
    pushNote(`你记下了结果：${payload.result.slice(0, 200)}`, { kind: 'outcome_recorded', decisionId: res.decision.id })
    toast({ type: 'success', message: '结果已记下，知君会引导你复盘' })
    nudgeRef.value?.reload()
    await scrollToBottom()
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) outcomeError.value = '这个判断已经记过结果'
    else outcomeError.value = friendlyError(err, '记录失败')
  } finally {
    outcomeBusy.value = false
  }
}

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

const showIntro = computed(
  () => !currentId.value && !conversationsLoading.value && conversations.value.length === 0 && stats.value !== null && !stats.value.hasOntology,
)

const headerTitle = computed(() => {
  if (current.value) return current.value.title || (current.value.mode === 'onboarding' ? '第一次对话' : '对话')
  return '对话'
})

const statusLine = computed(() => {
  if (!status.value) return ''
  const channel = status.value.provider === 'fake' ? '演示' : status.value.external ? '外部模型' : '本地模型'
  return `${channel} · ${status.value.model}`
})

function friendlyError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return '这段对话还在生成中，请稍等再发。'
    if (err.status === 429) return '模型正忙，请稍后再试。'
    if (err.status === 503) return '模型服务不可用，请到「资料与边界 → 设置」检查模型配置。'
    return err.message || fallback
  }
  return err instanceof Error && err.message ? err.message : fallback
}

async function loadStatus() {
  try {
    status.value = await getZhijunStatus()
  } catch {
    status.value = null
  }
}

async function loadStats() {
  try {
    stats.value = await getOntologyStats()
  } catch {
    stats.value = { hasOntology: true, entities: 0, claims: { working: 0, confirmed: 0, retracted: 0, superseded: 0 }, bySection: { who: { confirmed: 0, working: 0 }, people: { confirmed: 0, working: 0 }, matters: { confirmed: 0, working: 0 }, principles: { confirmed: 0, working: 0 }, ways: { confirmed: 0, working: 0 }, direction: { confirmed: 0, working: 0 } }, inbox: 0 }
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
    conversations.value = res.items
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '会话列表加载失败') })
  } finally {
    conversationsLoading.value = false
  }
}

function toUi(m: Message): UiMessage {
  return reactive({ ...m, provenance: null, turnMeta: null, candidates: [], streaming: false }) as UiMessage
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
    decision.value = detail.decision ?? null
    outcomeError.value = ''
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
  decision.value = null
  outcomeError.value = ''
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
  streaming.value = true
  let conv: Conversation
  try {
    conv = await ensureConversation('chat')
  } catch (err) {
    streaming.value = false
    toast({ type: 'error', message: friendlyError(err, '无法创建会话') })
    return
  }
  await streamTurn(conv, content, depth, mode)
}

async function startOnboarding() {
  if (streaming.value) return
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

  abortController = new AbortController()
  const signal = abortController.signal
  try {
    await streamPost(
      `/mindos/conversations/${encodeURIComponent(conv.id)}/messages`,
      { content, depth, mode },
      {
        decision_draft: (d) => {
          const e = d as DecisionDraftEvent
          draft.value = {
            id: e.draftId,
            conversationId: conv.id,
            messageId: assistant.id.startsWith('local-') ? null : assistant.id,
            revision: e.revision,
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
        },
        provenance: (d) => {
          assistant.provenance = d as ProvenanceEvent
        },
        token: (d) => {
          assistant.content += (d as { t: string }).t ?? ''
          void scrollToBottom()
        },
        extraction: (d) => {
          const e = d as ExtractionEvent
          if (e.state === 'queued') void pollInbox(assistant)
        },
        message_done: (d) => {
          const e = d as MessageDoneEvent
          assistant.status = e.status
          if (e.messageId) assistant.id = e.messageId
        },
        error: (d) => {
          const e = d as StreamErrorEvent
          assistant.status = 'error'
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
      toast({ type: 'error', message: friendlyError(err, '生成失败') })
    }
  } finally {
    assistant.streaming = false
    streaming.value = false
    abortController = null
    void loadConversations()
    await scrollToBottom()
  }
}

function stop() {
  abortController?.abort()
}

async function pollInbox(assistant: UiMessage) {
  const session = pollGate.next()
  let found = false
  for (let i = 0; i < 10; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 3000))
    if (!pollGate.isCurrent(session)) return
    try {
      const inbox = await getInbox(20)
      if (!pollGate.isCurrent(session)) return
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
}

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
  void loadStatus()
  void loadStats()
  void seedSeenClaims()
  void loadConversations()
})

onBeforeUnmount(() => {
  loadGate.invalidate()
  pollGate.invalidate()
  abortController?.abort()
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
            <span v-if="statusLine">{{ statusLine }}</span>
            <span v-if="status?.provider === 'fake'" class="zj-page__badge">演示模型</span>
            <span v-if="isReview" class="zj-page__badge">回访</span>
            <span v-if="status?.extraction === 'beta'" class="zj-page__badge">抽取 beta</span>
            <span v-if="status && !status.workerRunning" class="zj-page__badge is-warn">抽取暂停</span>
          </p>
        </div>
      </header>

      <NudgeStrip ref="nudgeRef" class="zj-page__nudges" />

      <div class="zj-page__body">
      <div class="zj-page__stream">
      <div ref="listRef" class="zj-page__messages">
        <div v-if="showIntro" class="zj-intro">
          <Sparkles :size="22" aria-hidden="true" />
          <h2>先让我认识真实的你</h2>
          <p>知君不会凭空给建议，也不会只凭一次聊天定义你。先聊十几分钟：你是谁、在做什么、最在意的人、最近一个判断、一条你认同的原则、以及不想让 AI 碰的领域。</p>
          <p class="zj-intro__trust">原件留在设备内 · 只有经过你确认的理解才会留下</p>
          <BaseButton variant="primary" :loading="streaming" @click="startOnboarding">开始第一次对话</BaseButton>
        </div>

        <div v-else-if="!currentId && !messages.length" class="zj-blank">
          <p class="zj-blank__lead">想聊什么，或者正在考虑什么决定？</p>
          <p class="zj-blank__hint">知君会基于「我的本体」里已确认的理解来回应，并标出哪些是你说过的、哪些只是它的推测。</p>
        </div>

        <div v-if="messagesLoading" class="loading-state">正在打开会话…</div>
        <ErrorState v-else-if="messagesError" :message="messagesError" retry-label="重试" @retry="currentId && loadConversation(currentId)" />

        <template v-for="m in messages" :key="m.id">
          <div class="zj-turn" :class="`zj-turn--${m.role}`">
            <MessageBubble
              :role="m.role"
              :content="m.content"
              :status="m.status"
              :streaming="m.streaming"
              @cite="(n) => onCite(m, n)"
            />
            <ProvenanceStrip v-if="m.role === 'assistant' && m.provenance" :provenance="m.provenance" :meta="m.turnMeta" />
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
      </div>

      <div class="zj-page__composer">
        <Composer
          ref="composerRef"
          :streaming="streaming"
          :disabled="messagesLoading"
          :allow-deliberate="!isReview && current?.mode !== 'onboarding'"
          :placeholder="current?.mode === 'onboarding' ? '回答知君的问题，或者说说你想先聊什么…' : isReview ? '说说实际发生了什么，和预期比差在哪…' : undefined"
          @send="send"
          @stop="stop"
        />
      </div>
      </div>

      <div v-if="showSidePanel" class="zj-page__panel">
        <ReviewOutcomePanel
          v-if="isReview && decision"
          :decision="decision"
          :busy="outcomeBusy"
          :error="outcomeError"
          @record="onRecordOutcome"
        />
        <LiveObjectPanel
          v-if="showDraftPanel && draft"
          :draft="draft"
          :changed-fields="draftChanged"
          :busy="draftBusy"
          :error="draftError"
          @confirm="onConfirmDraft"
          @discard="onDiscardDraft"
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
  background: var(--ws-body-bg, #fffcf6);
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
  font-size: 22px;
  font-weight: 700;
  color: var(--ws-text-primary-color, #1d211f);
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
.zj-page__badge {
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
}
.zj-page__badge.is-warn {
  color: var(--ws-warning-color, #b8862b);
  border-color: var(--ws-warning-color, #b8862b);
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
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-primary-color, #a6452e);
}
.zj-intro h2 {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: 24px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-intro p {
  margin: 0;
  font-size: 14px;
  line-height: 1.8;
  color: var(--ws-text-color, #3c403d);
}
.zj-intro__trust {
  font-size: 12px;
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
  font-size: 20px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-blank__hint {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--ws-text-secondary-color, #686b66);
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
