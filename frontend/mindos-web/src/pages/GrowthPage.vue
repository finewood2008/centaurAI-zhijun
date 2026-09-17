<script setup lang="ts">
// 有明确理由的经历进入回看；事实与写入仍使用既有盒端接口。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { createConversation, getConversation, listConversations, type Conversation, type Message } from '@/services/api'
import { REVISIT_PROMPT, REVISIT_STORAGE_KEY, readRevisitPreferences, revisitEntries, revisitLabel, revisitReason, type RevisitEntry } from '@/shared/revisit'
import { productScopeEpoch, createProductLocalStorage } from '@/shared/productScope'
import { ChevronUp, Plus, ArrowRight, Clock3 } from 'lucide-vue-next'
import {
  api,
  ApiError,
  type GrowthDecision,
} from '@/services/api'
import { formatDate } from '@/shared/format'
import BaseButton from '@/components/ui/BaseButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const toast = useToast()

const decisions = ref<GrowthDecision[]>([])
const decisionsLoading = ref(true)
const decisionsError = ref('')
const showDecisionForm = ref(false)
const decisionStep = ref<1 | 2>(1)
const decisionSaving = ref(false)
const decisionTitle = ref('')
const decisionContext = ref('')
const decisionOptions = ref('')
const decisionChoice = ref('')
const decisionRationale = ref('')
const decisionConfidence = ref(70)
const decisionExpectedOutcome = ref('')
const decisionReviewAt = ref('')

const activeOutcomeId = ref('')
const outcomeResult = ref('')
const outcomeNotes = ref('')
const outcomeSavingId = ref('')

const activeReviewId = ref('')
const reviewReflection = ref('')
const reviewLessons = ref('')
const reviewNextAction = ref('')
const reviewSavingId = ref('')

const conversations = ref<Conversation[]>([])
const conversationsLoading = ref(false)
const conversationsError = ref('')
const hasMoreConversations = ref(false)
let conversationOffset = 0
let conversationRequest = 0
let decisionRequest = 0
let alive = true
const ownerEpoch = productScopeEpoch()
const requestAbort = new AbortController()
const currentOwner = () => alive && ownerEpoch === productScopeEpoch()
const arrangementStorage = createProductLocalStorage()
const arrangementError = ref('')
const preferences = ref(readRevisitPreferences(null))
try { preferences.value = readRevisitPreferences(arrangementStorage.getItem(REVISIT_STORAGE_KEY)) }
catch { arrangementError.value = '此设备的回看安排暂时无法读取。原对话和选择记录仍可查看。' }
const now = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | undefined
const entries = computed(() => revisitEntries(decisions.value, conversations.value, preferences.value, now.value))
const view = ref<'ready' | 'reviewed' | 'deferred'>('ready')
const readyEntries = computed(() => entries.value.filter(e => e.reason && e.reason !== 'reviewed' && !e.deferred))
const reviewedEntries = computed(() => entries.value.filter(e => e.reason === 'reviewed' && !e.deferred))
const deferredEntries = computed(() => entries.value.filter(e => e.reason && e.deferred))
const visibleEntries = computed(() => view.value === 'ready' ? readyEntries.value : view.value === 'reviewed' ? reviewedEntries.value : deferredEntries.value)
const selectedKey = ref('')
const selectedEntry = computed(() => entries.value.find(e => e.key === selectedKey.value) || null)
const showPicker = ref(false)
const pickerQuery = ref('')
const pickerEntries = computed(() => entries.value.filter(e => (!e.reason || e.deferred) && e.title.toLowerCase().includes(pickerQuery.value.trim().toLowerCase())))
const selectedMessages = ref<Message[]>([])
const detailLoading = ref(false)
const detailError = ref('')
const detailRequest = ref(0)
const savedEntriesError = ref('')
const busy = computed(() => !!(decisionSaving.value || outcomeSavingId.value || reviewSavingId.value || reviewOpeningId.value))

function saveArrangement(change: (next: typeof preferences.value) => void): boolean {
  if (!currentOwner()) return false
  const next = { selected: { ...preferences.value.selected }, deferred: { ...preferences.value.deferred } }
  change(next)
  try {
    arrangementStorage.setItem(REVISIT_STORAGE_KEY, JSON.stringify(next))
    preferences.value = next
    arrangementError.value = ''
    return true
  } catch {
    arrangementError.value = '回看安排没有保存成功，请检查此设备的存储空间或浏览器存储权限后重试。'
    return false
  }
}

function selectEntry(entry: RevisitEntry) { selectedKey.value = entry.key }
function changeView(next: typeof view.value) {
  view.value = next
  selectedKey.value = visibleEntries.value[0]?.key || ''
}
function includeEntry(entry: RevisitEntry) {
  if (!saveArrangement(next => { next.selected[entry.key] = new Date().toISOString(); delete next.deferred[entry.key] })) return
  view.value = entry.reason === 'reviewed' ? 'reviewed' : 'ready'
  selectedKey.value = entry.key
  showPicker.value = false
  toast({ type: 'success', message: '已放入回看' })
}
function deferEntry(entry: RevisitEntry) {
  if (!saveArrangement(next => { next.deferred[entry.key] = entry.revision })) return
  selectedKey.value = visibleEntries.value[0]?.key || ''
  toast({ type: 'info', message: '已暂放，需要时可从「暂放」找回来' })
}
function restoreEntry(entry: RevisitEntry) {
  if (!saveArrangement(next => { delete next.deferred[entry.key] })) return
  view.value = entry.reason === 'reviewed' ? 'reviewed' : 'ready'
  selectedKey.value = entry.key
}

// Individually fetch previously selected chats beyond the first list page. No copied private text in local storage.
async function loadSavedConversations() {
  savedEntriesError.value = ''
  const ids = Object.keys(preferences.value.selected).filter(k => k.startsWith('conversation:')).map(k => k.slice(13))
    .filter(id => !conversations.value.some(c => c.id === id))
  // Bound concurrency even when a user has accumulated many selections.
  for (let i = 0; i < ids.length; i += 4) {
    if (!currentOwner()) return
    const results = await Promise.allSettled(ids.slice(i, i + 4).map(id => getConversation(id, requestAbort.signal)))
    if (!currentOwner()) return
    for (const result of results) {
      if (result.status === 'fulfilled') conversations.value = [...conversations.value.filter(c => c.id !== result.value.conversation.id), result.value.conversation]
      else savedEntriesError.value = '有些主动选中的经历暂时无法打开，可能已删除或连接尚未恢复。'
    }
  }
}

async function loadSelectedMessages() {
  const request = ++detailRequest.value
  const entry = selectedEntry.value
  selectedMessages.value = []
  detailError.value = ''
  detailLoading.value = false
  if (entry?.kind !== 'conversation') return
  detailLoading.value = true
  try {
    const detail = await getConversation(entry.id, requestAbort.signal)
    if (!currentOwner() || request !== detailRequest.value) return
    selectedMessages.value = detail.messages.filter(m => m.role === 'user' && m.content.trim())
  } catch {
    if (currentOwner() && request === detailRequest.value) detailError.value = '这段经历的原话暂时没有打开'
  } finally { if (currentOwner() && request === detailRequest.value) detailLoading.value = false }
}
watch(selectedKey, () => { void loadSelectedMessages() })
watch(visibleEntries, items => {
  if (!selectedKey.value && items.length) selectedKey.value = items[0].key
})
const initialLoading = computed(() => decisionsLoading.value || (conversationsLoading.value && !conversations.value.length))

async function loadConversations(append = false) {
  if (conversationsLoading.value) return
  const request = ++conversationRequest
  conversationsLoading.value = true
  conversationsError.value = ''
  try {
    const result = await listConversations({ status: 'all', limit: 40, offset: append ? conversationOffset : 0 }, requestAbort.signal)
    if (!currentOwner() || request !== conversationRequest) return
    conversations.value = [...new Map([...(append ? conversations.value : []), ...result.items].map(c => [c.id, c])).values()]
    conversationOffset = (append ? conversationOffset : 0) + result.items.length
    hasMoreConversations.value = !!result.hasMore
  } catch (error) {
    if (currentOwner() && request === conversationRequest) conversationsError.value = error instanceof Error ? error.message : '经历暂时没有加载出来'
  } finally {
    if (currentOwner() && request === conversationRequest) conversationsLoading.value = false
  }
}

function revisitConversation(conversation: Conversation) {
  return { path: `/c/${encodeURIComponent(conversation.id)}`,
    query: conversation.status === 'archived' ? {} : { say: REVISIT_PROMPT } }
}

onBeforeUnmount(() => { alive = false; requestAbort.abort(); clearInterval(clockTimer) })

function parseLines(value: string): string[] {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

async function loadDecisions() {
  const request = ++decisionRequest
  decisionsLoading.value = true
  decisionsError.value = ''
  try {
    const response = await api.listGrowthDecisions()
    if (!currentOwner() || request !== decisionRequest) return
    decisions.value = response.items
  } catch (error) {
    if (currentOwner() && request === decisionRequest) decisionsError.value = error instanceof Error ? error.message : '选择记录暂时没有加载出来'
  } finally {
    if (currentOwner() && request === decisionRequest) decisionsLoading.value = false
  }
}

function replaceDecision(updated: GrowthDecision) {
  const index = decisions.value.findIndex((item) => item.id === updated.id)
  if (index < 0) {
    decisions.value = [updated, ...decisions.value]
    return
  }
  decisions.value = decisions.value.map((item) => item.id === updated.id ? updated : item)
}

function nextDecisionStep() {
  const options = parseLines(decisionOptions.value)
  if (!decisionTitle.value.trim() || !decisionContext.value.trim() || !options.length || !decisionChoice.value.trim()) {
    toast({ type: 'error', message: '先把这件事、背景、选项和你的选择说清楚' })
    return
  }
  decisionStep.value = 2
}

function resetDecisionForm() {
  decisionStep.value = 1
  decisionTitle.value = ''
  decisionContext.value = ''
  decisionOptions.value = ''
  decisionChoice.value = ''
  decisionRationale.value = ''
  decisionConfidence.value = 70
  decisionExpectedOutcome.value = ''
  decisionReviewAt.value = ''
}

async function createDecision() {
  const options = parseLines(decisionOptions.value)
  if (!decisionTitle.value.trim() || !decisionContext.value.trim() || !options.length || !decisionChoice.value.trim() || !decisionRationale.value.trim() || !decisionExpectedOutcome.value.trim()) {
    toast({ type: 'error', message: '请完整填写选择、背景、选项、选择、理由和预期结果' })
    return
  }
  if (!Number.isFinite(decisionConfidence.value) || decisionConfidence.value < 0 || decisionConfidence.value > 100) {
    toast({ type: 'error', message: '信心度需在 0–100 之间' })
    return
  }
  let reviewAt: string | null = null
  if (decisionReviewAt.value) {
    const localDate = new Date(decisionReviewAt.value)
    if (Number.isNaN(localDate.valueOf())) {
      toast({ type: 'error', message: '请填写有效的观察时间' })
      return
    }
    // datetime-local 是用户本地时间；转 ISO 后显式携带时区，避免被后端当作 UTC。
    reviewAt = localDate.toISOString()
  }
  decisionSaving.value = true
  try {
    const created = await api.createGrowthDecision({
      title: decisionTitle.value.trim(),
      context: decisionContext.value.trim(),
      options,
      choice: decisionChoice.value.trim(),
      rationale: decisionRationale.value.trim(),
      confidence: Math.round(decisionConfidence.value),
      expectedOutcome: decisionExpectedOutcome.value.trim(),
      reviewAt,
      relatedEntityIds: [],
      evidenceRefs: [],
    })
    if (!currentOwner()) return
    resetDecisionForm()
    showDecisionForm.value = false
    decisions.value = [created, ...decisions.value.filter((item) => item.id !== created.id)]
    selectedKey.value = `decision:${created.id}`
    toast({ type: 'success', message: '选择已记录，等结果回来再一起回看' })
  } catch (error) {
    if (!currentOwner()) return
    if (isConflict(error)) {
      toast({ type: 'info', message: '记录状态已变化，已刷新列表' })
      await loadDecisions()
    } else {
      toast({ type: 'error', message: error instanceof Error ? error.message : '选择保存失败' })
    }
  } finally {
    decisionSaving.value = false
  }
}

const router = useRouter()
const reviewOpeningId = ref('')

// P2：从回看直接开一段回访会话，知君在会话里问结果、引导复盘。
async function openReviewConversation(decision: GrowthDecision) {
  reviewOpeningId.value = decision.id
  try {
    const conv = await createConversation({ mode: 'review', decisionId: decision.id })
    if (!currentOwner()) return
    await router.push({ path: `/c/${encodeURIComponent(conv.id)}`, query: { say: REVISIT_PROMPT } })
  } catch (error) {
    if (currentOwner()) toast({ type: 'error', message: error instanceof Error ? error.message : '无法开始回看' })
  } finally {
    reviewOpeningId.value = ''
  }
}

function startOutcome(decision: GrowthDecision) {
  activeReviewId.value = ''
  activeOutcomeId.value = decision.id
  outcomeResult.value = ''
  outcomeNotes.value = ''
}

function startReview(decision: GrowthDecision) {
  activeOutcomeId.value = ''
  activeReviewId.value = decision.id
  reviewReflection.value = ''
  reviewLessons.value = ''
  reviewNextAction.value = ''
}

async function recordOutcome(decision: GrowthDecision) {
  const result = outcomeResult.value.trim()
  if (!result) {
    toast({ type: 'error', message: '请填写真实结果' })
    return
  }
  outcomeSavingId.value = decision.id
  try {
    const updated = await api.recordGrowthDecisionOutcome(decision.id, { result, notes: outcomeNotes.value.trim(), evidenceRefs: [] })
    if (!currentOwner()) return
    replaceDecision(updated)
    activeOutcomeId.value = ''
    toast({ type: 'success', message: '结果已记录，下一步可以完成复盘' })
  } catch (error) {
    if (!currentOwner()) return
    if (isConflict(error)) {
      toast({ type: 'info', message: '这条记录已被更新，已为你刷新' })
      activeOutcomeId.value = ''
      await loadDecisions()
    } else {
      toast({ type: 'error', message: error instanceof Error ? error.message : '结果保存失败' })
    }
  } finally {
    outcomeSavingId.value = ''
  }
}

async function completeReview(decision: GrowthDecision) {
  const lessons = parseLines(reviewLessons.value)
  if (!reviewReflection.value.trim() || !lessons.length || !reviewNextAction.value.trim()) {
    toast({ type: 'error', message: '请填写复盘、至少一条经验和下一步行动' })
    return
  }
  reviewSavingId.value = decision.id
  try {
    const result = await api.createGrowthReview({ decisionId: decision.id, reflection: reviewReflection.value.trim(), lessons, nextAction: reviewNextAction.value.trim() })
    if (!currentOwner()) return
    replaceDecision(result.decision)
    activeReviewId.value = ''
    toast({ type: 'success', message: '复盘已完成，这次经验已留在你的成长轨迹中' })
  } catch (error) {
    if (!currentOwner()) return
    if (isConflict(error)) {
      toast({ type: 'info', message: '这条记录已被复盘，已为你刷新' })
      activeReviewId.value = ''
      await loadDecisions()
    } else {
      toast({ type: 'error', message: error instanceof Error ? error.message : '复盘保存失败' })
    }
  } finally {
    reviewSavingId.value = ''
  }
}

async function applyRouteIntent() {
  if (route.query.create === 'decision') showDecisionForm.value = true
  const decisionId = typeof route.query.decisionId === 'string' ? route.query.decisionId : ''
  const action = route.query.action
  if (decisionId) {
    const decision = decisions.value.find((item) => item.id === decisionId)
    if (decision) selectedKey.value = `decision:${decisionId}`
    if (decision && action === 'outcome' && decision.status === 'open') startOutcome(decision)
    else if (decision && action === 'review' && decision.status === 'outcome_recorded') startReview(decision)
    else if (decision && action) toast({ type: 'info', message: '这条记录状态已变化，已展示最新状态' })
  }
  await nextTick()
  if (!currentOwner()) return
  const target = decisionId ? `decision-${decisionId}` : route.query.create === 'decision' ? 'decision-create' : ''
  if (target) document.getElementById(target)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

watch(() => [route.query.decisionId, route.query.action, route.query.create], () => {
  if (!decisionsLoading.value && currentOwner()) void applyRouteIntent()
})

onMounted(async () => {
  clockTimer = setInterval(() => { now.value = Date.now() }, 60_000)
  await Promise.allSettled([loadDecisions(), loadConversations()])
  if (currentOwner()) await loadSavedConversations()
  if (currentOwner()) await applyRouteIntent()
})
</script>

<template>
  <div class="page growth-page">
    <div class="page-head growth-head">
      <div><h1>回看</h1><p>给值得再想一想的经历，留一个位置。</p></div>
      <div class="form-actions"><BaseButton variant="primary" :disabled="busy" :aria-expanded="showPicker" aria-controls="revisit-picker" @click="showPicker = !showPicker"><Plus :size="15" aria-hidden="true" />选一段经历</BaseButton><BaseButton variant="text" :disabled="busy" @click="showDecisionForm = true">记下一个选择</BaseButton></div>
    </div>
    <p class="revisit-intro">约好了再看、有了结果，或你想再理解一次。普通对话仍留在对话里。</p>
    <p v-if="arrangementError" class="arrangement-error" role="alert">{{ arrangementError }}</p>

    <section v-if="showPicker" id="revisit-picker" class="experience-picker" aria-label="选择值得回看的经历">
      <div class="subform-head"><h2>哪段经历，值得再想一想？</h2><BaseButton variant="text" @click="showPicker = false">取消</BaseButton></div>
      <p class="muted">由你选入。这里只保存回看安排，不改变原对话。</p>
      <label for="revisit-search">查找已加载的经历</label><input id="revisit-search" v-model="pickerQuery" type="search" placeholder="输入对话或选择的名称">
      <ul class="picker-list"><li v-for="entry in pickerEntries" :key="entry.key"><div><strong>{{ entry.title }}</strong><small>{{ entry.kind === 'decision' ? '已记录的选择' : entry.conversation.status === 'archived' ? '已归档的对话' : '聊过的经历' }} · {{ formatDate(entry.at) }}</small></div><BaseButton size="sm" :disabled="busy" :aria-label="`选入：${entry.title}`" @click="includeEntry(entry)">选入</BaseButton></li></ul>
      <p v-if="!pickerEntries.length && !conversationsLoading" class="muted">{{ pickerQuery ? '当前已加载的记录中没有匹配项。' : '当前没有其他可选经历。' }}</p>
      <BaseButton v-if="hasMoreConversations" variant="secondary" :loading="conversationsLoading" @click="loadConversations(true)">加载更早的经历</BaseButton>
      <p class="local-note">选入和暂放的安排仅保存在此设备。</p>
    </section>
    <form v-if="showDecisionForm" id="decision-create" class="growth-form decision-create" @submit.prevent="decisionStep === 1 ? nextDecisionStep() : createDecision()">
      <div class="subform-head"><div><h3>记下当时的选择</h3><p>{{ decisionStep === 1 ? '第一步：这件事是什么，你打算怎么选。' : '第二步：为什么，以及到时候怎么看对不对。' }}</p></div><button type="button" class="collapse-button" aria-label="收起判断表单" :disabled="decisionSaving" @click="showDecisionForm = false"><ChevronUp :size="18" /></button></div>
      <ol class="decision-steps" aria-label="两步"><li :class="{ 'is-on': decisionStep === 1, 'is-done': decisionStep === 2 }">这件事</li><li :class="{ 'is-on': decisionStep === 2 }">为什么</li></ol>
      <template v-if="decisionStep === 1">
        <label for="decision-title">这件事 <span aria-hidden="true">*</span></label><input id="decision-title" v-model="decisionTitle" maxlength="300" :disabled="decisionSaving" placeholder="例如：要不要这个季度进入新市场" required>
        <label for="decision-context">背景 <span aria-hidden="true">*</span></label><textarea id="decision-context" v-model="decisionContext" rows="3" maxlength="10000" :disabled="decisionSaving" placeholder="发生了什么，有哪些时间、资源或关系上的约束" required />
        <label for="decision-options">认真考虑过的选项 <small>每行一项</small> <span aria-hidden="true">*</span></label><textarea id="decision-options" v-model="decisionOptions" rows="3" :disabled="decisionSaving" placeholder="现在进入&#10;先小规模验证&#10;暂不进入" required />
        <label for="decision-choice">我的选择 <span aria-hidden="true">*</span></label><textarea id="decision-choice" v-model="decisionChoice" rows="2" maxlength="2000" :disabled="decisionSaving" required />
        <div class="form-actions"><BaseButton variant="secondary" :disabled="decisionSaving" @click="showDecisionForm = false">取消</BaseButton><BaseButton type="submit" variant="primary">下一步</BaseButton></div>
      </template>
      <template v-else>
        <label for="decision-rationale">为什么这样选 <span aria-hidden="true">*</span></label><textarea id="decision-rationale" v-model="decisionRationale" rows="3" maxlength="10000" :disabled="decisionSaving" placeholder="关键的事实、假设和取舍" required />
        <label for="decision-expected">我预期会看到什么 <span aria-hidden="true">*</span></label><textarea id="decision-expected" v-model="decisionExpectedOutcome" rows="2" maxlength="5000" :disabled="decisionSaving" placeholder="到时候怎么判断这个选择对不对" required />
        <div class="form-grid compact-grid">
          <div class="field"><label for="decision-confidence">把握有几成 <small>{{ decisionConfidence }}%</small></label><input id="decision-confidence" v-model.number="decisionConfidence" type="range" min="0" max="100" step="5" :disabled="decisionSaving"></div>
          <div class="field"><label for="decision-review-at">什么时候回来看结果 <small>可留空</small></label><input id="decision-review-at" v-model="decisionReviewAt" type="datetime-local" :disabled="decisionSaving"></div>
        </div>
        <div class="form-actions"><BaseButton variant="secondary" :disabled="decisionSaving" @click="decisionStep = 1">上一步</BaseButton><BaseButton type="submit" variant="primary" :loading="decisionSaving">记下</BaseButton></div>
      </template>
    </form>

    <section class="revisit-records" aria-label="经历与选择">
      <p v-if="initialLoading" role="status">正在打开值得回看的经历…</p>
      <ErrorState v-if="decisionsError" :message="decisionsError" @retry="loadDecisions" />
      <ErrorState v-if="conversationsError" :message="conversationsError" @retry="loadConversations(conversationOffset > 0)" />
      <ErrorState v-if="savedEntriesError" :message="savedEntriesError" @retry="loadSavedConversations" />
      <div class="revisit-views" role="group" aria-label="回看分类">
        <button type="button" :aria-pressed="view === 'ready'" :disabled="busy" @click="changeView('ready')">值得回看 <span>{{ readyEntries.length }}</span></button>
        <button type="button" :aria-pressed="view === 'reviewed'" :disabled="busy" @click="changeView('reviewed')">留下的经验 <span>{{ reviewedEntries.length }}</span></button>
        <button type="button" :aria-pressed="view === 'deferred'" :disabled="busy" @click="changeView('deferred')">暂放 <span>{{ deferredEntries.length }}</span></button>
      </div>
      <EmptyState v-if="!initialLoading && !decisionsError && !visibleEntries.length && !selectedEntry"
        :title="view === 'ready' ? '现在没有需要回看的经历' : view === 'reviewed' ? '理解可以慢慢形成' : '没有暂放的经历'"
        :description="view === 'ready' ? '不必为每段对话做总结。等约定时间到了、有了结果，或主动选一段想再理解的经历。' : view === 'reviewed' ? '有了想留下的经验，再把它记下来。还没有结论也没关系。' : '暂时不想回看的经历可以放在这里，随时找回来。'" />
      <ol class="experience-cards" aria-label="回看经历">
        <li v-for="entry in visibleEntries" :key="entry.key">
          <button type="button" :data-testid="`revisit-pick-${entry.id}`" class="experience-card" :aria-pressed="selectedKey === entry.key" :disabled="busy" @click="selectEntry(entry)">
            <span class="experience-reason"><Clock3 v-if="entry.reason === 'due'" :size="13" aria-hidden="true" />{{ entry.deferred ? '暂时放下' : revisitLabel(entry) }}</span>
            <strong>{{ entry.title }}</strong><time :datetime="entry.at">{{ formatDate(entry.at) }}</time><ArrowRight :size="17" class="experience-arrow" aria-hidden="true" />
          </button>
        </li>
      </ol>
      <article v-if="selectedEntry" :key="selectedEntry.key" :data-testid="`revisit-${selectedEntry.id}`" class="experience-detail" aria-label="这段经历的回看">
        <header class="experience-heading"><span class="experience-reason">{{ revisitLabel(selectedEntry) }}</span><h2>{{ selectedEntry.title }}</h2><p>{{ revisitReason(selectedEntry) }}</p></header>
        <template v-if="selectedEntry.kind === 'decision'">
          <div v-for="decision in [selectedEntry.decision]" :id="`decision-${decision.id}`" :key="decision.id">
            <ol class="experience-timeline" aria-label="当时、后来、现在">
              <li><span class="timeline-dot" /><h3>当时 <small>我的选择与预期</small></h3><p>{{ decision.choice }}</p><p class="muted">{{ decision.expectedOutcome }}</p><small class="source-note">来自当时的选择记录 · {{ formatDate(decision.createdAt) }}</small></li>
              <li :class="{ 'is-empty': !decision.outcome }"><span class="timeline-dot" /><h3>后来 <small>实际发生了什么</small></h3><template v-if="decision.outcome"><p>{{ decision.outcome.result }}</p><p v-if="decision.outcome.notes" class="muted">{{ decision.outcome.notes }}</p><small class="source-note">你记录的结果 · {{ formatDate(decision.outcome.recordedAt) }}</small></template><template v-else><p class="muted">还没有记录结果。</p><p class="muted">发生了什么，或事情仍在进行，都可以如实补充。</p></template></li>
              <li :class="{ 'is-empty': !decision.review }"><span class="timeline-dot" /><h3>现在 <small>我有什么新理解</small></h3><template v-if="decision.review"><p>{{ decision.review.reflection }}</p><small class="source-note">你留下的复盘 · {{ formatDate(decision.review.createdAt) }}</small></template><template v-else><p class="muted">可以有新的理解，也可以还没有答案。</p><p class="muted">不用急着给这段经历下结论。</p></template></li>
            </ol>
            <section v-if="decision.review" class="saved-learning" aria-label="留下的经验"><h3>留下的经验</h3><ul><li v-for="lesson in decision.review.lessons" :key="lesson">{{ lesson }}</li></ul><p><strong>下一步</strong> {{ decision.review.nextAction }}</p></section>
            <aside class="revisit-question"><span>一起想一想</span><p>{{ decision.review ? '现在再看，这次留下的经验还有什么需要补充，或只适用于当时？' : decision.outcome ? '把后来发生的事与当时的预期放在一起，哪一点最值得重新理解？' : '当时最在意的事情，后来怎么样了？有没有一件你没想到的事？' }}</p></aside>
            <div class="decision-actions"><BaseButton variant="primary" :loading="reviewOpeningId === decision.id" :disabled="busy" @click="openReviewConversation(decision)">和知君一起回看</BaseButton><BaseButton v-if="decision.status === 'open' && activeOutcomeId !== decision.id" variant="secondary" :disabled="busy" @click="startOutcome(decision)">记结果</BaseButton><BaseButton v-if="decision.status === 'outcome_recorded' && activeReviewId !== decision.id" :disabled="busy" @click="startReview(decision)">留下经验</BaseButton></div>
            <form v-if="decision.status === 'open' && activeOutcomeId === decision.id" class="inline-form" @submit.prevent="recordOutcome(decision)">
              <h5>当初的预期是：{{ decision.expectedOutcome }}</h5><label :for="`outcome-result-${decision.id}`">真实结果 <span aria-hidden="true">*</span></label><textarea :id="`outcome-result-${decision.id}`" v-model="outcomeResult" rows="3" maxlength="10000" :disabled="outcomeSavingId === decision.id" required /><label :for="`outcome-notes-${decision.id}`">补充说明 <small>可留空</small></label><textarea :id="`outcome-notes-${decision.id}`" v-model="outcomeNotes" rows="2" maxlength="10000" :disabled="outcomeSavingId === decision.id" /><div class="form-actions"><BaseButton variant="secondary" size="sm" :disabled="outcomeSavingId === decision.id" @click="activeOutcomeId = ''">取消</BaseButton><BaseButton type="submit" variant="primary" size="sm" :loading="outcomeSavingId === decision.id">保存结果</BaseButton></div>
            </form>

            <form v-if="decision.status === 'outcome_recorded' && activeReviewId === decision.id" class="inline-form" @submit.prevent="completeReview(decision)">
              <h5>有了想留下的理解，再记下来</h5>
              <p class="muted">还没有结论也没关系，可以取消，或先和知君聊聊。</p>
              <label :for="`review-reflection-${decision.id}`">我现在怎么看这次选择 <span aria-hidden="true">*</span></label>
              <textarea :id="`review-reflection-${decision.id}`" v-model="reviewReflection" rows="3" maxlength="10000" :disabled="reviewSavingId === decision.id" required />
              <label :for="`review-lessons-${decision.id}`">值得留下的经验 <small>每行一项</small> <span aria-hidden="true">*</span></label>
              <textarea :id="`review-lessons-${decision.id}`" v-model="reviewLessons" rows="3" :disabled="reviewSavingId === decision.id" required />
              <label :for="`review-next-${decision.id}`">下一步行动 <span aria-hidden="true">*</span></label>
              <textarea :id="`review-next-${decision.id}`" v-model="reviewNextAction" rows="2" maxlength="5000" :disabled="reviewSavingId === decision.id" required />
              <div class="form-actions"><BaseButton variant="secondary" size="sm" :disabled="reviewSavingId === decision.id" @click="activeReviewId = ''">取消</BaseButton><BaseButton type="submit" variant="primary" size="sm" :loading="reviewSavingId === decision.id">确认完成复盘</BaseButton></div>
            </form>

            <details class="decision-details"><summary>看看当时的记录与来源</summary><dl><dt>背景</dt><dd>{{ decision.context }}</dd><dt>考虑过的选项</dt><dd><ul><li v-for="option in decision.options" :key="option">{{ option }}</li></ul></dd><dt>理由与假设</dt><dd>{{ decision.rationale }}</dd><dt>当时的把握</dt><dd>{{ decision.confidence }}%</dd><dt>约定回看时间</dt><dd>{{ decision.reviewAt ? formatDate(decision.reviewAt) : '尚未约定' }}</dd><dt>记录时间</dt><dd>{{ formatDate(decision.createdAt) }}<template v-if="decision.charterVersion"> · <RouterLink :to="{ path: '/me/charter', query: { version: decision.charterVersion } }">参考人生章程第 {{ decision.charterVersion }} 版</RouterLink></template></dd></dl><nav v-if="selectedEntry.sourceIds.length" class="revisit-sources" aria-label="这件事的原对话"><RouterLink v-for="id in selectedEntry.sourceIds" :key="id" :to="`/c/${encodeURIComponent(id)}`">{{ selectedEntry.conversations.find(c => c.id === id)?.title || '查看原对话' }}</RouterLink></nav></details>
          </div>
        </template>
        <template v-else>
          <ol class="experience-timeline" aria-label="当时、后来、现在">
            <li><span class="timeline-dot" /><h3>当时 <small>回到我的原话</small></h3><p v-if="detailLoading" role="status">正在打开原话…</p><ErrorState v-else-if="detailError" :message="detailError" @retry="loadSelectedMessages" /><template v-else-if="selectedMessages.length"><blockquote>{{ selectedMessages[0].content.length > 220 ? selectedMessages[0].content.slice(0, 220) + '…' : selectedMessages[0].content }}</blockquote><small class="source-note">原话节选 · {{ formatDate(selectedMessages[0].createdAt) }}</small></template><p v-else class="muted">暂时没有可展示的原话，可以打开原对话核对。</p></li>
            <li class="is-empty"><span class="timeline-dot" /><h3>后来 <small>实际发生了什么</small></h3><p class="muted">这段对话未单独记录结果。</p><p class="muted">可以回到原对话，一起核对和补充后来的变化。</p></li>
            <li class="is-empty"><span class="timeline-dot" /><h3>现在 <small>我有什么新理解</small></h3><p class="muted">你想再理解哪一点？</p><p class="muted">观察和补充留在对话里，不急着形成结论。</p></li>
          </ol>
          <aside class="revisit-question"><span>一起想一想</span><p>这次再看，当时在意的事情有没有变化？你想从哪一句话说起？</p></aside>
          <div class="decision-actions"><RouterLink v-if="selectedEntry.conversation.status !== 'archived'" class="revisit-chat-link" :to="revisitConversation(selectedEntry.conversation)">和知君一起回看 <ArrowRight :size="15" aria-hidden="true" /></RouterLink><RouterLink :to="`/c/${encodeURIComponent(selectedEntry.id)}`">查看原对话{{ selectedEntry.conversation.status === 'archived' ? '（已归档）' : '' }}</RouterLink></div>
          <details v-if="selectedMessages.length" class="decision-details"><summary>看看当时的原话</summary><blockquote v-for="message in selectedMessages.slice(0, 4)" :key="message.id">{{ message.content }}<small class="source-note">{{ formatDate(message.createdAt) }}</small></blockquote><p class="muted">{{ selectedMessages.length > 4 ? '这里只展示前四段原话，' : '' }}完整上下文可在原对话中查看。</p></details>
        </template>
        <div class="defer-action"><BaseButton v-if="selectedEntry.deferred" variant="text" :disabled="busy" @click="restoreEntry(selectedEntry)">放回回看</BaseButton><BaseButton v-else-if="selectedEntry.reason" variant="text" :disabled="busy" @click="deferEntry(selectedEntry)">暂时不回看</BaseButton><BaseButton v-else variant="text" :disabled="busy" @click="includeEntry(selectedEntry)">主动选入回看</BaseButton><small>{{ selectedEntry.deferred ? '需要时，随时可以回来。' : '不必每次都有结论。' }}</small></div>
      </article>
      <p class="revisit-footer">只留下值得再想的经历。你的补充和纠正，仍由你确认。</p>
    </section>
  </div>
</template>

<style scoped>
.growth-page{max-width:1100px}.growth-head{display:flex;align-items:center;justify-content:space-between;gap:20px}.growth-head h1,.experience-heading h2,.experience-picker h2{font-family:var(--ws-font-display)}
.revisit-intro{color:var(--ws-text-secondary-color);line-height:1.8;margin:0 0 28px;font-size:13px}.form-actions,.decision-actions,.subform-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.subform-head{justify-content:space-between}.form-actions{justify-content:flex-end}
.revisit-views{display:flex;gap:22px;margin:20px 0;border-bottom:1px solid var(--ws-border-color-3)}.revisit-views button{font:inherit;font-size:13px;background:transparent;border:0;border-bottom:2px solid transparent;padding:12px 0;cursor:pointer;color:var(--ws-text-secondary-color)}.revisit-views button[aria-pressed=true]{border-color:var(--ws-primary-color);color:var(--ws-primary-color)}.revisit-views span{font-size:11px;margin-left:4px}
.experience-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;list-style:none;padding:0;margin:0 0 26px}.experience-cards li{min-width:0}.experience-card{position:relative;text-align:left;width:100%;height:100%;min-height:150px;padding:19px 20px;background:var(--ws-card-bg);border:1px solid var(--ws-border-color-3);border-radius:var(--ws-radius-lg);color:var(--ws-text-primary-color);cursor:pointer}.experience-card[aria-pressed=true]{border-color:var(--ws-primary-color);background:color-mix(in srgb,var(--ws-primary-color) 4%,var(--ws-card-bg))}.experience-card:hover{border-color:var(--ws-primary-color)}.experience-card strong{display:block;font-family:var(--ws-font-display);font-size:18px;font-weight:500;line-height:1.6;margin:12px 0 16px;overflow-wrap:anywhere}.experience-card time{font-size:11px;color:var(--ws-text-secondary-color)}.experience-arrow{position:absolute;bottom:20px;right:20px;color:var(--ws-primary-color)}
.experience-reason{display:flex;align-items:center;gap:6px;font-size:11px;letter-spacing:.05em;color:var(--ws-primary-color)}.experience-detail{padding:30px;border:1px solid var(--ws-border-color-3);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg);overflow-wrap:anywhere}.experience-heading h2{font-size:27px;font-weight:500;line-height:1.5;margin:12px 0}.experience-heading>p{font-size:13px;line-height:1.8;color:var(--ws-text-secondary-color);margin:0 0 34px}
.experience-timeline{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));list-style:none;padding:0;margin:0 0 28px}.experience-timeline>li{min-width:0;position:relative;padding:25px 22px 0 0;border-top:1px solid var(--ws-border-color-2)}.experience-timeline>li:last-child{padding-right:0}.timeline-dot{position:absolute;width:9px;height:9px;background:var(--ws-primary-color);border:2px solid var(--ws-card-bg);border-radius:50%;top:-6px;left:0;box-sizing:content-box}.is-empty .timeline-dot{background:var(--ws-card-bg);box-shadow:0 0 0 1px var(--ws-border-color-2);width:7px;height:7px;top:-5px}.experience-timeline h3{margin:0 0 17px;font-size:18px;font-weight:500;font-family:var(--ws-font-display)}.experience-timeline h3 small{display:block;margin-top:5px;font-size:12px;font-family:inherit;color:var(--ws-text-secondary-color)}.experience-timeline p,.experience-timeline blockquote{font-size:14px;line-height:1.9;white-space:pre-wrap;margin:0 0 12px}.muted{color:var(--ws-text-secondary-color)}.source-note{display:block;font-size:11px;line-height:1.7;color:var(--ws-text-secondary-color);margin-top:14px}
.revisit-question{padding:18px 22px;margin:24px 0;background:color-mix(in srgb,var(--ws-primary-color) 5%,var(--ws-card-bg));border-left:2px solid var(--ws-primary-color);border-radius:0 6px 6px 0}.revisit-question span{font-size:11px;letter-spacing:.08em;color:var(--ws-primary-color)}.revisit-question p{margin:8px 0 0;line-height:1.9;font-size:15px}.decision-actions{margin:18px 0}.decision-actions a{font-size:13px}.revisit-chat-link{display:inline-flex;align-items:center;gap:8px;padding:8px 15px;border-radius:6px;background:var(--ws-primary-color);color:var(--ws-button-color,#fff);text-decoration:none}.saved-learning{font-size:14px;line-height:1.9;padding:14px 20px;background:var(--ws-surface-2);border-radius:6px}.saved-learning h3{margin:0;font-size:14px}.saved-learning ul{padding-left:18px}.saved-learning strong{font-weight:500}
.defer-action{display:flex;align-items:center;gap:15px;flex-wrap:wrap;margin-top:16px}.defer-action small,.local-note,.revisit-footer{font-size:12px;line-height:1.8;color:var(--ws-text-secondary-color)}.revisit-footer{margin-top:28px}.revisit-sources{display:flex;gap:12px;flex-wrap:wrap;font-size:12px}.decision-details{margin-top:22px}.decision-details blockquote{margin:16px 0;border-left:2px solid var(--ws-border-color-2);padding-left:14px;white-space:pre-wrap;line-height:1.9;font-size:13px}
.experience-picker{padding:22px;margin:0 0 26px;border:1px solid var(--ws-border-color-2);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg)}.experience-picker h2{font-size:20px;font-weight:500;margin:0}.experience-picker p{font-size:13px}.experience-picker label{display:block;font-size:12px;margin:20px 0 8px}.experience-picker input{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid var(--ws-border-color-2);border-radius:5px;background:var(--ws-card-bg);color:inherit}.picker-list{list-style:none;margin:12px 0;padding:0;max-height:320px;overflow:auto}.picker-list li{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:15px 0;border-bottom:1px solid var(--ws-border-color-3)}.picker-list li>div{min-width:0;overflow-wrap:anywhere}.picker-list strong{font-size:14px;font-weight:500}.picker-list small{display:block;font-size:11px;margin-top:6px;color:var(--ws-text-secondary-color)}.arrangement-error{font-size:13px;color:var(--ws-primary-color);line-height:1.8}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--ws-primary-color);outline-offset:3px}button:disabled{cursor:default;opacity:.6}
.decision-steps{display:flex;gap:14px;margin:0 0 4px;padding:0;list-style:none;font-size:13px;color:var(--ws-text-placeholder-color)}.decision-steps li{display:flex;align-items:center;gap:6px}.decision-steps li::before{content:'';width:8px;height:8px;border-radius:50%;border:1.5px solid currentColor}.decision-steps li.is-on{color:var(--ws-primary-color);font-weight:600}.decision-steps li.is-on::before{background:var(--ws-primary-color)}.decision-steps li.is-done{color:var(--ws-text-primary-color)}.decision-steps li.is-done::before{background:var(--ws-text-primary-color)}
.decision-details{padding:6px 0;border-top:1px solid var(--ws-border-color-3)}.decision-details summary{color:var(--ws-primary-color);font-size: 12px;cursor:pointer}.decision-details dl{display:grid;grid-template-columns:76px 1fr;gap:6px 10px;margin-top:9px;font-size:12px;line-height:1.6}.decision-details dt{color:var(--ws-text-secondary-color)}.decision-details dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.decision-details ul{margin:0;padding-left:18px}
.growth-form{display:grid;gap:8px;padding:17px}.form-intro{margin:0 0 4px;color:var(--ws-text-color);font-size:13px}.growth-form label,.field label,.inline-form label{color:var(--ws-text-color);font-size:12px;font-weight:600}.growth-form label small,.field label small,.inline-form label small{color:var(--ws-text-secondary-color);font-weight:400}.growth-form input,.growth-form textarea,.inline-form textarea{width:100%;padding:9px 11px;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius);background:var(--ws-body-bg);color:var(--ws-text-primary-color);font:inherit;font-size:13px;line-height:1.55}.growth-form input:focus,.growth-form textarea:focus,.inline-form textarea:focus{outline:0;border-color:var(--ws-primary-color);box-shadow:0 0 0 3px rgba(166,69,46,.15)}.growth-form textarea,.inline-form textarea{resize:vertical}.growth-form input:disabled,.growth-form textarea:disabled,.inline-form textarea:disabled{opacity:.6}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.field{display:grid;gap:8px}.compact-grid{align-items:end}.form-actions{justify-content:flex-end;gap:8px;margin-top:7px}.form-note{flex:1;color:var(--ws-text-secondary-color);font-size: 12px}
.decision-create{margin:0 0 18px;border:1px solid rgba(166,69,46,.35);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg)}.subform-head{gap:12px;margin-bottom:3px}.subform-head h3{margin:0;font-size:14px}.subform-head p{margin:3px 0 0;color:var(--ws-text-secondary-color);font-size: 12px}.collapse-button{display:grid;width:30px;height:30px;place-items:center;border:0;border-radius:6px;background:transparent;color:var(--ws-text-secondary-color)}.collapse-button:hover{background:var(--ws-surface-2)}
.inline-form{display:grid;gap:7px;padding:12px;border:1px solid rgba(166,69,46,.3);border-radius:var(--ws-radius);background:var(--ws-body-bg)}.inline-form h5{margin:0 0 3px;font-size:12px}.inline-form .form-actions{margin-top:3px}
@media(max-width:760px){.growth-head{align-items:stretch;flex-direction:column}.form-grid{grid-template-columns:1fr}.form-actions{align-items:stretch;flex-wrap:wrap}.form-note{flex-basis:100%}.decision-details dl{grid-template-columns:1fr}.decision-details dt{margin-top:5px}}

@media(max-width:760px){.growth-head{align-items:flex-start;flex-direction:column}.growth-head .form-actions{justify-content:flex-start}.experience-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.experience-detail{padding:23px}.experience-timeline{grid-template-columns:1fr}.experience-timeline>li,.experience-timeline>li:last-child{border-top:0;border-left:1px solid var(--ws-border-color-2);padding:0 0 28px 24px}.experience-timeline>li:last-child{padding-bottom:0;border-left-color:transparent}.timeline-dot{top:5px;left:-7px}.is-empty .timeline-dot{top:6px;left:-6px}.experience-timeline h3{margin-bottom:12px}.experience-timeline h3 small{display:inline;margin-left:9px}.experience-heading h2{font-size:24px}.experience-timeline{margin-bottom:12px}.revisit-views{gap:18px}}
@media(max-width:440px){.experience-cards{grid-template-columns:1fr;gap:10px}.experience-card{min-height:0;padding:15px 18px}.experience-card strong{margin:8px 25px 9px 0;font-size:17px}.experience-detail,.experience-picker{padding:19px 16px}.revisit-question{padding:15px}.revisit-views{gap:14px}.revisit-views button{font-size:12px}.growth-head .form-actions{gap:8px}.decision-actions{gap:10px}.experience-heading h2{font-size:22px}}
</style>
