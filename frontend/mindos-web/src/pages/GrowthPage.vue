<script setup lang="ts">
// 判断 → 结果 → 复盘；人生章程由「我的本体」管理，这里只引用当时版本。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { createConversation, listConversations, type Conversation } from '@/services/api'
import { REVISIT_PROMPT, revisitEntries } from '@/shared/revisit'
import { productScopeEpoch } from '@/shared/productScope'
import { BookOpenCheck, ChevronUp, Plus, RotateCcw, Sprout } from 'lucide-vue-next'
import {
  api,
  ApiError,
  type GrowthDecision,
} from '@/services/api'
import { formatDate } from '@/shared/format'
import BaseButton from '@/components/ui/BaseButton.vue'
import DecisionStepper from '@/components/growth/DecisionStepper.vue'
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
const entries = computed(() => revisitEntries(decisions.value, conversations.value))
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

onBeforeUnmount(() => { alive = false; requestAbort.abort() })

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
    resetDecisionForm()
    showDecisionForm.value = false
    if (!currentOwner()) return
    decisions.value = [created, ...decisions.value.filter((item) => item.id !== created.id)]
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

function isOverdue(decision: GrowthDecision): boolean {
  if (decision.status !== 'open' || !decision.reviewAt) return false
  const at = new Date(decision.reviewAt).valueOf()
  return Number.isFinite(at) && at < Date.now()
}

async function applyRouteIntent() {
  if (route.query.create === 'decision') showDecisionForm.value = true
  const decisionId = typeof route.query.decisionId === 'string' ? route.query.decisionId : ''
  const action = route.query.action
  if (decisionId) {
    const decision = decisions.value.find((item) => item.id === decisionId)
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
  await Promise.allSettled([loadDecisions(), loadConversations()])
  if (currentOwner()) await applyRouteIntent()
})
</script>

<template>
  <div class="page growth-page">
    <div class="page-head growth-head">
      <div><h1>回看</h1><p>回看经历，理解自己的选择。</p></div>
      <div class="form-actions"><BaseButton variant="primary" @click="router.push({ path: '/chat', query: { say: '我想一起回看最近的一段经历：' } })">聊一段经历</BaseButton><BaseButton variant="text" @click="showDecisionForm = true"><Plus :size="15" aria-hidden="true" />记下一个选择</BaseButton></div>
    </div>
    <p class="revisit-intro">从一段聊过的经历开始。核对当时的想法，补充后来发生的事，再和知君一起看看有没有新的理解。观察与修正都留在对话里。</p>

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
      <p v-if="initialLoading" role="status">正在打开经历与选择…</p>
      <ErrorState v-if="decisionsError" :message="decisionsError" @retry="loadDecisions" />
      <ErrorState v-if="conversationsError" :message="conversationsError" @retry="loadConversations(conversationOffset > 0)" />
      <EmptyState v-if="!initialLoading && !decisionsError && !conversationsError && !entries.length"
        title="从一段经历开始" description="聊过的事情会留在这里，不需要先作出一个选择。想回看时，我们再一起慢慢想。" />
      <ol class="revisit-list">
        <li v-for="entry in entries" :key="`${entry.kind}:${entry.id}`" :data-testid="`revisit-${entry.id}`">
          <time :datetime="entry.at">{{ formatDate(entry.at) }}</time>
          <article v-if="entry.kind === 'conversation'" class="conversation-card">
            <div class="conversation-card__head"><h3>{{ entry.conversation.title || '一段聊过的经历' }}</h3><span>{{ entry.conversation.status === 'archived' ? '已归档' : '聊过的经历' }}</span></div>
            <p>回到原对话，看看当时在意什么，也可以补充后来发生的事。</p>
            <div class="decision-actions"><RouterLink :to="`/c/${encodeURIComponent(entry.id)}`">查看原对话</RouterLink><RouterLink v-if="entry.conversation.status !== 'archived'" :to="revisitConversation(entry.conversation)">和知君一起回看</RouterLink></div>
          </article>
          <template v-else>
          <article v-for="decision in [entry.decision]" :id="`decision-${decision.id}`" :key="decision.id" class="decision-card" :class="{ 'is-overdue': isOverdue(decision) }">
            <div class="decision-card__head"><h4>{{ decision.title }}</h4><span v-if="isOverdue(decision)" class="overdue-tag">可以回看了</span></div>
            <DecisionStepper :status="decision.status" />
            <nav v-if="entry.conversations.length" class="revisit-sources" aria-label="这件事的原对话"><RouterLink v-for="source in entry.conversations" :key="source.id" :to="`/c/${encodeURIComponent(source.id)}`">{{ source.title || '原对话' }}</RouterLink></nav>
            <p class="decision-choice"><small>当时选了</small>{{ decision.choice }}</p>
            <div class="decision-chips">
              <span class="chip"><small>把握</small>{{ decision.confidence }}%</span>
              <span v-if="decision.status === 'open'" class="chip"><small>回访</small>{{ decision.reviewAt ? formatDate(decision.reviewAt) : '未定' }}</span>
            </div>
            <details class="decision-details"><summary>当时的完整记录</summary><dl><dt>背景</dt><dd>{{ decision.context }}</dd><dt>考虑过的选项</dt><dd><ul><li v-for="option in decision.options" :key="option">{{ option }}</li></ul></dd><dt>理由与假设</dt><dd>{{ decision.rationale }}</dd><dt>预期结果</dt><dd>{{ decision.expectedOutcome }}</dd><dt>记录时间</dt><dd>{{ formatDate(decision.createdAt) }}<template v-if="decision.charterVersion"> · <RouterLink :to="{ path: '/me/charter', query: { version: decision.charterVersion } }">参考人生章程第 {{ decision.charterVersion }} 版</RouterLink></template></dd></dl></details>

            <section v-if="decision.outcome" class="outcome-block"><div class="outcome-block__title"><BookOpenCheck :size="16" aria-hidden="true" />真实结果 <small>{{ formatDate(decision.outcome.recordedAt) }}</small></div><p>{{ decision.outcome.result }}</p><p v-if="decision.outcome.notes" class="muted">补充：{{ decision.outcome.notes }}</p></section>
            <section v-if="decision.review" class="review-block">
              <div class="outcome-block__title"><Sprout :size="16" aria-hidden="true" />复盘 <small>{{ formatDate(decision.review.createdAt) }}</small></div>
              <p>{{ decision.review.reflection }}</p>
              <div class="review-lessons"><strong>留下的经验</strong><ul><li v-for="lesson in decision.review.lessons" :key="lesson">{{ lesson }}</li></ul></div>
              <p class="review-next"><strong>下一步</strong>{{ decision.review.nextAction }}</p>
            </section>

            <div class="decision-actions"><button type="button" class="decision-review-link" :disabled="!!reviewOpeningId" @click="openReviewConversation(decision)">{{ reviewOpeningId === decision.id ? '正在打开…' : '和知君一起回看' }}</button><BaseButton v-if="decision.status === 'open' && activeOutcomeId !== decision.id" variant="primary" size="sm" @click="startOutcome(decision)"><RotateCcw :size="14" aria-hidden="true" />记结果</BaseButton></div>
            <form v-if="decision.status === 'open' && activeOutcomeId === decision.id" class="inline-form" @submit.prevent="recordOutcome(decision)">
              <h5>当初的预期是：{{ decision.expectedOutcome }}</h5><label :for="`outcome-result-${decision.id}`">真实结果 <span aria-hidden="true">*</span></label><textarea :id="`outcome-result-${decision.id}`" v-model="outcomeResult" rows="3" maxlength="10000" :disabled="outcomeSavingId === decision.id" required /><label :for="`outcome-notes-${decision.id}`">补充说明 <small>可留空</small></label><textarea :id="`outcome-notes-${decision.id}`" v-model="outcomeNotes" rows="2" maxlength="10000" :disabled="outcomeSavingId === decision.id" /><div class="form-actions"><BaseButton variant="secondary" size="sm" :disabled="outcomeSavingId === decision.id" @click="activeOutcomeId = ''">取消</BaseButton><BaseButton type="submit" variant="primary" size="sm" :loading="outcomeSavingId === decision.id">保存结果</BaseButton></div>
            </form>

            <div v-if="decision.status === 'outcome_recorded' && activeReviewId !== decision.id" class="decision-actions"><BaseButton variant="primary" size="sm" @click="startReview(decision)"><BookOpenCheck :size="14" aria-hidden="true" />复盘</BaseButton></div>
            <form v-if="decision.status === 'outcome_recorded' && activeReviewId === decision.id" class="inline-form" @submit.prevent="completeReview(decision)">
              <h5>从预期与真实结果的差异开始</h5><label :for="`review-reflection-${decision.id}`">我现在怎么看这次选择 <span aria-hidden="true">*</span></label><textarea :id="`review-reflection-${decision.id}`" v-model="reviewReflection" rows="3" maxlength="10000" :disabled="reviewSavingId === decision.id" required /><label :for="`review-lessons-${decision.id}`">值得留下的经验 <small>每行一项</small> <span aria-hidden="true">*</span></label><textarea :id="`review-lessons-${decision.id}`" v-model="reviewLessons" rows="3" :disabled="reviewSavingId === decision.id" required /><label :for="`review-next-${decision.id}`">下一步行动 <span aria-hidden="true">*</span></label><textarea :id="`review-next-${decision.id}`" v-model="reviewNextAction" rows="2" maxlength="5000" :disabled="reviewSavingId === decision.id" required /><div class="form-actions"><BaseButton variant="secondary" size="sm" :disabled="reviewSavingId === decision.id" @click="activeReviewId = ''">取消</BaseButton><BaseButton type="submit" variant="primary" size="sm" :loading="reviewSavingId === decision.id">确认完成复盘</BaseButton></div>
            </form>
          </article>
          </template>
        </li>
      </ol>
      <BaseButton v-if="hasMoreConversations" variant="secondary" :loading="conversationsLoading" @click="loadConversations(true)">查看更多经历</BaseButton>
    </section>
  </div>
</template>

<style scoped>
.revisit-intro{max-width:650px;color:var(--ws-text-secondary-color);line-height:1.8;margin:0 0 30px}
.revisit-list{list-style:none;padding:0;margin:0;display:grid;gap:26px}.revisit-list>li{min-width:0}.revisit-list time{display:block;font-size:12px;color:var(--ws-text-secondary-color);margin:0 0 9px}
.conversation-card{padding:20px;border:1px solid var(--ws-border-color-3);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg)}
.conversation-card__head{display:flex;justify-content:space-between;align-items:baseline;gap:12px}.conversation-card h3{font-family:var(--ws-font-display);margin:0;font-size:18px;overflow-wrap:anywhere}.conversation-card__head span{font-size:12px;white-space:nowrap;color:var(--ws-text-secondary-color)}
.conversation-card p{font-size:13px;color:var(--ws-text-secondary-color);line-height:1.8}.revisit-sources{display:flex;gap:12px;flex-wrap:wrap;font-size:12px}.revisit-records>.ws-btn{margin-top:24px}
.revisit-list .decision-card{padding:20px;gap:14px}.revisit-list .decision-card h4{font-size:18px}.decision-actions{flex-wrap:wrap;gap:14px}.decision-actions a{font-size:13px}.revisit-records{overflow-wrap:anywhere}

.growth-page{max-width:860px}
.growth-head,.decision-card__head,.decision-actions,.form-actions,.subform-head,.outcome-block__title{display:flex;align-items:center}
.growth-head,.decision-card__head,.form-actions,.subform-head{justify-content:space-between}
.growth-head{gap:16px}.growth-head h1,.decision-card__head h4{font-family:var(--ws-font-display)}
.decision-card{display:grid;gap:9px;padding:13px;border:1px solid var(--ws-border-color-2);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg)}.decision-card.is-overdue{border-color:rgba(166,69,46,.45)}.decision-card.is-highlighted{outline:2px solid var(--ws-primary-color,#a6452e);outline-offset:2px;transition:outline-color .3s}
.decision-card__head{align-items:flex-start;gap:8px}.decision-card__head h4{margin:0;font-size:15px;line-height:1.45;overflow-wrap:anywhere}
.overdue-tag{flex:none;padding:1px 7px;border-radius:3px;border:1px solid var(--ws-primary-color);color:var(--ws-primary-color);font-size: 12px;font-weight:500}
.decision-choice{margin:0;font-size:13px;line-height:1.55;color:var(--ws-text-primary-color);overflow-wrap:anywhere}.decision-choice small{display:block;margin-bottom:2px;color:var(--ws-text-secondary-color);font-size:12px}
.decision-steps{display:flex;gap:14px;margin:0 0 4px;padding:0;list-style:none;font-size:13px;color:var(--ws-text-placeholder-color)}.decision-steps li{display:flex;align-items:center;gap:6px}.decision-steps li::before{content:'';width:8px;height:8px;border-radius:50%;border:1.5px solid currentColor}.decision-steps li.is-on{color:var(--ws-primary-color);font-weight:600}.decision-steps li.is-on::before{background:var(--ws-primary-color)}.decision-steps li.is-done{color:var(--ws-text-primary-color)}.decision-steps li.is-done::before{background:var(--ws-text-primary-color)}
.decision-chips{display:flex;flex-wrap:wrap;gap:6px}.chip{display:inline-flex;align-items:baseline;gap:4px;max-width:100%;padding:3px 8px;border-radius:999px;background:var(--ws-surface-2);color:var(--ws-text-primary-color);font-size:13px;line-height:1.5;overflow-wrap:anywhere}.chip small{color:var(--ws-text-secondary-color);font-size:13px}.chip--plain{margin:2px 6px 2px 0}
.decision-details{padding:6px 0;border-top:1px solid var(--ws-border-color-3)}.decision-details summary{color:var(--ws-primary-color);font-size: 12px;cursor:pointer}.decision-details dl{display:grid;grid-template-columns:76px 1fr;gap:6px 10px;margin-top:9px;font-size:12px;line-height:1.6}.decision-details dt{color:var(--ws-text-secondary-color)}.decision-details dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.decision-details ul{margin:0;padding-left:18px}
.outcome-block,.review-block,.latest-review{padding:11px 12px;border-radius:var(--ws-radius);background:var(--ws-surface-2)}.review-block{border:1px solid var(--ws-border-color-3)}.latest-review{margin:14px 17px}.outcome-block__title{gap:6px;color:var(--ws-success-color);font-size:12px;font-weight:600}.review-block .outcome-block__title{color:var(--ws-primary-color)}.outcome-block__title small{margin-left:auto;color:var(--ws-text-secondary-color);font-weight:400}.outcome-block p,.review-block p,.latest-review p{margin:7px 0 0;font-size:12px;line-height:1.65;white-space:pre-wrap}.muted{color:var(--ws-text-secondary-color)}
.review-lessons{margin-top:9px;color:var(--ws-text-color);font-size:12px}.review-lessons ul{margin:5px 0 0;padding-left:18px}.review-next{display:flex;gap:8px;padding-top:8px;border-top:1px solid var(--ws-border-color-3)}.review-next strong{flex:none;color:var(--ws-primary-color)}
.decision-actions{justify-content:flex-end;gap:10px}.decision-review-link{border:none;background:transparent;color:var(--ws-primary-color);font-family:inherit;font-size:12px;text-decoration:underline;cursor:pointer}.decision-review-link:disabled{opacity:.5;cursor:default}
.growth-form{display:grid;gap:8px;padding:17px}.form-intro{margin:0 0 4px;color:var(--ws-text-color);font-size:13px}.growth-form label,.field label,.inline-form label{color:var(--ws-text-color);font-size:12px;font-weight:600}.growth-form label small,.field label small,.inline-form label small{color:var(--ws-text-secondary-color);font-weight:400}.growth-form input,.growth-form textarea,.inline-form textarea{width:100%;padding:9px 11px;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius);background:var(--ws-body-bg);color:var(--ws-text-primary-color);font:inherit;font-size:13px;line-height:1.55}.growth-form input:focus,.growth-form textarea:focus,.inline-form textarea:focus{outline:0;border-color:var(--ws-primary-color);box-shadow:0 0 0 3px rgba(166,69,46,.15)}.growth-form textarea,.inline-form textarea{resize:vertical}.growth-form input:disabled,.growth-form textarea:disabled,.inline-form textarea:disabled{opacity:.6}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.field{display:grid;gap:8px}.compact-grid{align-items:end}.form-actions{justify-content:flex-end;gap:8px;margin-top:7px}.form-note{flex:1;color:var(--ws-text-secondary-color);font-size: 12px}
.decision-create{margin:0 0 18px;border:1px solid rgba(166,69,46,.35);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg)}.subform-head{gap:12px;margin-bottom:3px}.subform-head h3{margin:0;font-size:14px}.subform-head p{margin:3px 0 0;color:var(--ws-text-secondary-color);font-size: 12px}.collapse-button{display:grid;width:30px;height:30px;place-items:center;border:0;border-radius:6px;background:transparent;color:var(--ws-text-secondary-color)}.collapse-button:hover{background:var(--ws-surface-2)}
.inline-form{display:grid;gap:7px;padding:12px;border:1px solid rgba(166,69,46,.3);border-radius:var(--ws-radius);background:var(--ws-body-bg)}.inline-form h5{margin:0 0 3px;font-size:12px}.inline-form .form-actions{margin-top:3px}
@media(max-width:760px){.growth-head{align-items:stretch;flex-direction:column}.form-grid{grid-template-columns:1fr}.form-actions{align-items:stretch;flex-wrap:wrap}.form-note{flex-basis:100%}.decision-details dl{grid-template-columns:1fr}.decision-details dt{margin-top:5px}}
</style>
