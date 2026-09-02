<script setup lang="ts">
// 知君成长闭环 MVP：人生章程 → 判断 → 结果 → 复盘。
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { createConversation } from '@/services/api'
import { BookOpenCheck, ChevronUp, Plus, RotateCcw, Sprout, Target } from 'lucide-vue-next'
import {
  api,
  ApiError,
  type GrowthCharter,
  type GrowthDecision,
  type GrowthDecisionStatus,
} from '@/services/api'
import { formatDate } from '@/shared/format'
import BaseButton from '@/components/ui/BaseButton.vue'
import DecisionStepper from '@/components/growth/DecisionStepper.vue'
import JudgmentTimeline from '@/components/growth/JudgmentTimeline.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const toast = useToast()

const charter = ref<GrowthCharter | null>(null)
const charterVersionCount = ref(0)
const charterLoading = ref(true)
const charterError = ref('')
const charterSaving = ref(false)
const showCharterForm = ref(true)
const charterVision = ref('')
const charterRoles = ref('')
const charterPrinciples = ref('')
const charterBoundaries = ref('')
const charterGoals = ref('')
const charterChallengeStyle = ref('')
const charterQuietDomains = ref('')

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

const statusCounts = computed(() => ({
  open: decisions.value.filter((item) => item.status === 'open').length,
  outcome: decisions.value.filter((item) => item.status === 'outcome_recorded').length,
  reviewed: decisions.value.filter((item) => item.status === 'reviewed').length,
}))

function parseLines(value: string): string[] {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function joinLines(values: string[]): string {
  return values.join('\n')
}

function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

function applyCharter(value: GrowthCharter | null) {
  charter.value = value
  charterVision.value = value?.vision ?? ''
  charterRoles.value = joinLines(value?.roles ?? [])
  charterPrinciples.value = joinLines(value?.principles ?? [])
  charterBoundaries.value = joinLines(value?.boundaries ?? [])
  charterGoals.value = joinLines(value?.goals ?? [])
  charterChallengeStyle.value = value?.challengeStyle ?? ''
  charterQuietDomains.value = joinLines(value?.quietDomains ?? [])
}

async function loadCharter() {
  charterLoading.value = true
  charterError.value = ''
  try {
    const response = await api.getGrowthCharter()
    charterVersionCount.value = response.versions.length
    applyCharter(response.currentCharter)
    showCharterForm.value = response.currentCharter === null
  } catch (error) {
    charterError.value = error instanceof Error ? error.message : '人生章程加载失败'
  } finally {
    charterLoading.value = false
  }
}

function startCharterEdit() {
  applyCharter(charter.value)
  showCharterForm.value = true
}

function cancelCharterEdit() {
  applyCharter(charter.value)
  showCharterForm.value = charter.value === null
}

async function saveCharter() {
  const vision = charterVision.value.trim()
  const challengeStyle = charterChallengeStyle.value.trim()
  if (!vision || !challengeStyle) {
    toast({ type: 'error', message: '请填写人生愿景和知君挑战你的方式' })
    return
  }
  charterSaving.value = true
  try {
    const saved = await api.saveGrowthCharter({
      vision,
      roles: parseLines(charterRoles.value),
      principles: parseLines(charterPrinciples.value),
      boundaries: parseLines(charterBoundaries.value),
      goals: parseLines(charterGoals.value),
      challengeStyle,
      quietDomains: parseLines(charterQuietDomains.value),
    })
    applyCharter(saved)
    charterVersionCount.value = Math.max(charterVersionCount.value + 1, saved.version)
    showCharterForm.value = false
    toast({ type: 'success', message: saved.version === 1 ? '人生章程已创建' : '人生章程已生成新版本' })
  } catch (error) {
    if (isConflict(error)) {
      toast({ type: 'info', message: '章程已在其他位置更新，已为你加载最新版本' })
      await loadCharter()
    } else {
      toast({ type: 'error', message: error instanceof Error ? error.message : '章程保存失败' })
    }
  } finally {
    charterSaving.value = false
  }
}

async function loadDecisions() {
  decisionsLoading.value = true
  decisionsError.value = ''
  try {
    const response = await api.listGrowthDecisions()
    decisions.value = response.items
  } catch (error) {
    decisionsError.value = error instanceof Error ? error.message : '判断簿加载失败'
  } finally {
    decisionsLoading.value = false
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
    toast({ type: 'error', message: '请完整填写判断、背景、选项、选择、理由和预期结果' })
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
    decisions.value = [created, ...decisions.value.filter((item) => item.id !== created.id)]
    toast({ type: 'success', message: '判断已记录，知君会等真实结果回来' })
  } catch (error) {
    if (isConflict(error)) {
      toast({ type: 'info', message: '判断状态已变化，已刷新列表' })
      await loadDecisions()
    } else {
      toast({ type: 'error', message: error instanceof Error ? error.message : '判断创建失败' })
    }
  } finally {
    decisionSaving.value = false
  }
}

const router = useRouter()
const reviewOpeningId = ref('')

// P2：从判断簿直接开一段回访会话，知君在会话里问结果、引导复盘。
async function openReviewConversation(decision: GrowthDecision) {
  reviewOpeningId.value = decision.id
  try {
    const conv = await createConversation({ mode: 'review', decisionId: decision.id })
    router.push(`/c/${encodeURIComponent(conv.id)}`)
  } catch (error) {
    toast({ type: 'error', message: error instanceof Error ? error.message : '无法开始回访' })
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
    replaceDecision(updated)
    activeOutcomeId.value = ''
    toast({ type: 'success', message: '结果已记录，下一步可以完成复盘' })
  } catch (error) {
    if (isConflict(error)) {
      toast({ type: 'info', message: '该判断已被更新，已为你刷新' })
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
    replaceDecision(result.decision)
    activeReviewId.value = ''
    toast({ type: 'success', message: '复盘已完成，这次经验已留在你的成长轨迹中' })
  } catch (error) {
    if (isConflict(error)) {
      toast({ type: 'info', message: '该判断已被复盘，已为你刷新' })
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

function sortForBoard(items: GrowthDecision[]): GrowthDecision[] {
  return [...items].sort((a, b) => {
    const oa = isOverdue(a) ? 0 : 1
    const ob = isOverdue(b) ? 0 : 1
    if (oa !== ob) return oa - ob
    const ra = a.reviewAt ? new Date(a.reviewAt).valueOf() : Number.POSITIVE_INFINITY
    const rb = b.reviewAt ? new Date(b.reviewAt).valueOf() : Number.POSITIVE_INFINITY
    if (ra !== rb) return ra - rb
    return b.createdAt.localeCompare(a.createdAt)
  })
}

// 判断优先的三栏看板：进行中（逾期在前）/ 待复盘 / 已复盘
const boardColumns = computed(() => [
  { key: 'open', title: '进行中', hint: '等结果回来', empty: '没有正在等结果的判断', items: sortForBoard(decisions.value.filter((d) => d.status === 'open')) },
  { key: 'outcome', title: '待复盘', hint: '结果已记下', empty: '没有待复盘的判断', items: sortForBoard(decisions.value.filter((d) => d.status === 'outcome_recorded')) },
  { key: 'reviewed', title: '已复盘', hint: '经验已留下', empty: '还没有完成复盘的判断', items: decisions.value.filter((d) => d.status === 'reviewed') },
] as const)

const latestReviewed = computed(() => {
  const reviewed = decisions.value.filter((d) => d.review)
  if (!reviewed.length) return null
  return reviewed.sort((a, b) => String(b.review?.createdAt ?? '').localeCompare(String(a.review?.createdAt ?? '')))[0]
})

// 趋势（判断时间线）折叠：判断少于 5 个时默认收起；用户手动展开或收起后记住
const TREND_KEY = 'zhijun.judgments.trend'
function readTrend(): boolean | null {
  try {
    const v = localStorage.getItem(TREND_KEY)
    return v === 'open' ? true : v === 'closed' ? false : null
  } catch {
    return null
  }
}
const trendPref = ref<boolean | null>(readTrend())
const trendOpen = computed(() => trendPref.value ?? decisions.value.length >= 5)
function onTrendToggle(event: Event) {
  const open = (event.target as HTMLDetailsElement).open
  // 由绑定同步引起的 toggle 与当前状态一致，不算用户操作
  if (open === trendOpen.value) return
  trendPref.value = open
  try {
    localStorage.setItem(TREND_KEY, open ? 'open' : 'closed')
  } catch {
    // 无法持久化时忽略
  }
}

// 时间线点了某个判断：滚到卡片并高亮 1.5 秒
const highlightedId = ref('')
let highlightTimer: ReturnType<typeof setTimeout> | null = null
async function focusDecision(decisionId: string) {
  highlightedId.value = decisionId
  await nextTick()
  document.getElementById(`decision-${decisionId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  if (highlightTimer) clearTimeout(highlightTimer)
  highlightTimer = setTimeout(() => {
    if (highlightedId.value === decisionId) highlightedId.value = ''
  }, 1500)
}

function statusLabel(status: GrowthDecisionStatus): string {
  if (status === 'open') return '待观察结果'
  if (status === 'outcome_recorded') return '待复盘'
  return '已完成复盘'
}

async function applyRouteIntent() {
  if (route.query.create === 'decision') showDecisionForm.value = true
  const decisionId = typeof route.query.decisionId === 'string' ? route.query.decisionId : ''
  const action = route.query.action
  if (decisionId) {
    const decision = decisions.value.find((item) => item.id === decisionId)
    if (decision && action === 'outcome' && decision.status === 'open') startOutcome(decision)
    else if (decision && action === 'review' && decision.status === 'outcome_recorded') startReview(decision)
    else if (decision) toast({ type: 'info', message: '该判断状态已变化，已展示最新状态' })
  }
  await nextTick()
  const target = decisionId ? `decision-${decisionId}` : route.query.create === 'decision' ? 'decision-create' : ''
  if (target) document.getElementById(target)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

onMounted(async () => {
  await Promise.allSettled([loadCharter(), loadDecisions()])
  await applyRouteIntent()
})
</script>

<template>
  <div class="page growth-page">
    <div class="page-head growth-head">
      <div><h1>判断</h1><p>记下当时为什么这样选，等结果回来再一起复盘。</p></div>
      <BaseButton variant="primary" @click="showDecisionForm = true"><Plus :size="15" aria-hidden="true" />记录判断</BaseButton>
    </div>

    <form v-if="showDecisionForm" id="decision-create" class="growth-form decision-create" @submit.prevent="decisionStep === 1 ? nextDecisionStep() : createDecision()">
      <div class="subform-head"><div><h3>记下当下的判断</h3><p>{{ decisionStep === 1 ? '第一步：这件事是什么，你打算怎么选。' : '第二步：为什么，以及到时候怎么看对不对。' }}</p></div><button type="button" class="collapse-button" aria-label="收起判断表单" :disabled="decisionSaving" @click="showDecisionForm = false"><ChevronUp :size="18" /></button></div>
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

    <section class="board-section" aria-labelledby="decisions-heading">
      <div class="section-head">
        <div class="growth-panel__title"><span class="panel-icon"><Target :size="19" aria-hidden="true" /></span><div><h2 id="decisions-heading">判断簿</h2><p>把事后的解释，变成当时就写下、事后可核对的记录。</p></div></div>
        <div class="decision-counts"><span>{{ statusCounts.open }} 进行中</span><span>{{ statusCounts.outcome }} 待复盘</span><span>{{ statusCounts.reviewed }} 已复盘</span></div>
      </div>
      <div v-if="decisionsLoading" class="loading-state" aria-live="polite">正在加载判断簿…</div>
      <ErrorState v-else-if="decisionsError" :message="decisionsError" @retry="loadDecisions" />
      <EmptyState v-else-if="!decisions.length" title="还没有判断记录" description="从一个当下正在做的真实选择开始，不需要一次写得完美；在对话里打开「我在考虑…」也能记。"><template #action><BaseButton variant="primary" size="sm" @click="showDecisionForm = true">记录第一次判断</BaseButton></template></EmptyState>
      <div v-else class="board">
        <section v-for="column in boardColumns" :key="column.key" class="board__column" :aria-labelledby="`column-${column.key}`">
          <header class="board__column-head"><h3 :id="`column-${column.key}`">{{ column.title }}</h3><small>{{ column.hint }} · {{ column.items.length }}</small></header>
          <p v-if="!column.items.length" class="board__empty">{{ column.empty }}</p>
          <article v-for="decision in column.items" :id="`decision-${decision.id}`" :key="decision.id" class="decision-card" :class="{ 'is-overdue': isOverdue(decision), 'is-highlighted': highlightedId === decision.id }">
            <div class="decision-card__head"><h4>{{ decision.title }}</h4><span v-if="isOverdue(decision)" class="overdue-tag">已逾期</span></div>
            <DecisionStepper :status="decision.status" />
            <p class="decision-choice"><small>当时选了</small>{{ decision.choice }}</p>
            <div class="decision-chips">
              <span class="chip"><small>把握</small>{{ decision.confidence }}%</span>
              <span v-if="decision.status === 'open'" class="chip"><small>回访</small>{{ decision.reviewAt ? formatDate(decision.reviewAt) : '未定' }}</span>
            </div>
            <details class="decision-details"><summary>当时的完整记录</summary><dl><dt>背景</dt><dd>{{ decision.context }}</dd><dt>考虑过的选项</dt><dd><ul><li v-for="option in decision.options" :key="option">{{ option }}</li></ul></dd><dt>理由与假设</dt><dd>{{ decision.rationale }}</dd><dt>预期结果</dt><dd>{{ decision.expectedOutcome }}</dd><dt>记录时间</dt><dd>{{ formatDate(decision.createdAt) }}<template v-if="decision.charterVersion"> · 基于章程第 {{ decision.charterVersion }} 版</template></dd></dl></details>

            <section v-if="decision.outcome" class="outcome-block"><div class="outcome-block__title"><BookOpenCheck :size="16" aria-hidden="true" />真实结果 <small>{{ formatDate(decision.outcome.recordedAt) }}</small></div><p>{{ decision.outcome.result }}</p><p v-if="decision.outcome.notes" class="muted">补充：{{ decision.outcome.notes }}</p></section>
            <section v-if="decision.review" class="review-block">
              <div class="outcome-block__title"><Sprout :size="16" aria-hidden="true" />复盘 <small>{{ formatDate(decision.review.createdAt) }}</small></div>
              <p>{{ decision.review.reflection }}</p>
              <div class="review-lessons"><strong>留下的经验</strong><ul><li v-for="lesson in decision.review.lessons" :key="lesson">{{ lesson }}</li></ul></div>
              <p class="review-next"><strong>下一步</strong>{{ decision.review.nextAction }}</p>
            </section>

            <div v-if="decision.status === 'open' && activeOutcomeId !== decision.id" class="decision-actions"><button type="button" class="decision-review-link" :disabled="reviewOpeningId === decision.id" @click="openReviewConversation(decision)">和知君回访</button><BaseButton variant="primary" size="sm" @click="startOutcome(decision)"><RotateCcw :size="14" aria-hidden="true" />记结果</BaseButton></div>
            <form v-if="decision.status === 'open' && activeOutcomeId === decision.id" class="inline-form" @submit.prevent="recordOutcome(decision)">
              <h5>当初的预期是：{{ decision.expectedOutcome }}</h5><label :for="`outcome-result-${decision.id}`">真实结果 <span aria-hidden="true">*</span></label><textarea :id="`outcome-result-${decision.id}`" v-model="outcomeResult" rows="3" maxlength="10000" :disabled="outcomeSavingId === decision.id" required /><label :for="`outcome-notes-${decision.id}`">补充说明 <small>可留空</small></label><textarea :id="`outcome-notes-${decision.id}`" v-model="outcomeNotes" rows="2" maxlength="10000" :disabled="outcomeSavingId === decision.id" /><div class="form-actions"><BaseButton variant="secondary" size="sm" :disabled="outcomeSavingId === decision.id" @click="activeOutcomeId = ''">取消</BaseButton><BaseButton type="submit" variant="primary" size="sm" :loading="outcomeSavingId === decision.id">保存结果</BaseButton></div>
            </form>

            <div v-if="decision.status === 'outcome_recorded' && activeReviewId !== decision.id" class="decision-actions"><BaseButton variant="primary" size="sm" @click="startReview(decision)"><BookOpenCheck :size="14" aria-hidden="true" />复盘</BaseButton></div>
            <form v-if="decision.status === 'outcome_recorded' && activeReviewId === decision.id" class="inline-form" @submit.prevent="completeReview(decision)">
              <h5>从预期与真实结果的差异开始</h5><label :for="`review-reflection-${decision.id}`">我现在怎么看这次判断 <span aria-hidden="true">*</span></label><textarea :id="`review-reflection-${decision.id}`" v-model="reviewReflection" rows="3" maxlength="10000" :disabled="reviewSavingId === decision.id" required /><label :for="`review-lessons-${decision.id}`">值得留下的经验 <small>每行一项</small> <span aria-hidden="true">*</span></label><textarea :id="`review-lessons-${decision.id}`" v-model="reviewLessons" rows="3" :disabled="reviewSavingId === decision.id" required /><label :for="`review-next-${decision.id}`">下一步行动 <span aria-hidden="true">*</span></label><textarea :id="`review-next-${decision.id}`" v-model="reviewNextAction" rows="2" maxlength="5000" :disabled="reviewSavingId === decision.id" required /><div class="form-actions"><BaseButton variant="secondary" size="sm" :disabled="reviewSavingId === decision.id" @click="activeReviewId = ''">取消</BaseButton><BaseButton type="submit" variant="primary" size="sm" :loading="reviewSavingId === decision.id">确认完成复盘</BaseButton></div>
            </form>
          </article>
        </section>
      </div>
    </section>

    <details v-if="decisions.length" class="growth-trend" :open="trendOpen" data-testid="judgment-trend" @toggle="onTrendToggle">
      <summary>查看趋势</summary>
      <JudgmentTimeline :decisions="decisions" :selected-id="highlightedId || null" @select="focusDecision" />
    </details>

    <section class="growth-panel charter-panel" aria-labelledby="charter-heading">
      <div class="growth-panel__head">
        <div class="growth-panel__title"><span class="panel-icon"><Sprout :size="19" aria-hidden="true" /></span><div><h2 id="charter-heading">我的方向（人生章程）</h2><p>知君挑战你、提醒你时的依据；改动会生成新版本，不改写历史。</p></div></div>
        <div class="charter-panel__meta"><span v-if="charter" class="version-badge">第 {{ charter.version }} 版 · {{ formatDate(charter.createdAt) }}</span><BaseButton v-if="charter && !showCharterForm" variant="secondary" size="sm" @click="startCharterEdit">修改</BaseButton></div>
      </div>
      <div v-if="charterLoading" class="loading-state" aria-live="polite">正在加载人生章程…</div>
      <ErrorState v-else-if="charterError" :message="charterError" @retry="loadCharter" />
      <div v-else-if="charter && !showCharterForm" class="charter-summary">
        <p class="charter-summary__vision"><small>我想成为</small>{{ charter.vision }}</p>
        <div class="charter-summary__rows">
          <div><small>角色</small><span v-if="!charter.roles.length" class="muted">未填写</span><span v-for="item in charter.roles" :key="item" class="chip chip--plain">{{ item }}</span></div>
          <div><small>原则</small><span v-if="!charter.principles.length" class="muted">未填写</span><span v-for="item in charter.principles" :key="item" class="chip chip--plain">{{ item }}</span></div>
          <div><small>目标</small><span v-if="!charter.goals.length" class="muted">未填写</span><span v-for="item in charter.goals" :key="item" class="chip chip--plain">{{ item }}</span></div>
          <div><small>不该由 AI 决定</small><span v-if="!charter.boundaries.length" class="muted">未填写</span><span v-for="item in charter.boundaries" :key="item" class="chip chip--plain">{{ item }}</span></div>
          <div><small>知君可以如何挑战我</small><span class="charter-line">{{ charter.challengeStyle }}</span></div>
          <div><small>不要主动提起</small><span v-if="!charter.quietDomains.length" class="muted">无</span><span v-for="item in charter.quietDomains" :key="item" class="chip chip--plain">{{ item }}</span></div>
        </div>
      </div>
      <form v-else-if="showCharterForm" class="growth-form" @submit.prevent="saveCharter">
        <p v-if="!charter" class="form-intro">先写下你想成为谁，知君才有依据挑战你。</p>
        <label for="charter-vision">我想成为怎样的人 <span aria-hidden="true">*</span></label>
        <textarea id="charter-vision" v-model="charterVision" rows="3" maxlength="2000" :disabled="charterSaving" placeholder="用你自己的语言描述人生愿景" required />
        <div class="form-grid">
          <div class="field"><label for="charter-roles">当前重要角色 <small>每行一项</small></label><textarea id="charter-roles" v-model="charterRoles" rows="4" :disabled="charterSaving" placeholder="创业者&#10;父亲&#10;投资人" /></div>
          <div class="field"><label for="charter-goals">当前阶段目标 <small>每行一项</small></label><textarea id="charter-goals" v-model="charterGoals" rows="4" :disabled="charterSaving" placeholder="今年完成一项最重要的事" /></div>
          <div class="field"><label for="charter-principles">我确认的长期原则 <small>每行一项</small></label><textarea id="charter-principles" v-model="charterPrinciples" rows="4" :disabled="charterSaving" placeholder="诚实面对不确定性" /></div>
          <div class="field"><label for="charter-boundaries">AI 不应替我决定的事 <small>每行一项</small></label><textarea id="charter-boundaries" v-model="charterBoundaries" rows="4" :disabled="charterSaving" placeholder="重大人事决策" /></div>
        </div>
        <label for="charter-challenge">知君可以如何挑战我 <span aria-hidden="true">*</span></label>
        <textarea id="charter-challenge" v-model="charterChallengeStyle" rows="2" maxlength="1000" :disabled="charterSaving" placeholder="例如：直接指出我的理由与过往事实不一致，但先问我一个反向问题" required />
        <label for="charter-quiet">禁止主动提醒的领域 <small>每行一项，可留空</small></label>
        <textarea id="charter-quiet" v-model="charterQuietDomains" rows="2" :disabled="charterSaving" placeholder="例如：家庭关系" />
        <div class="form-actions"><span v-if="charter" class="form-note">更新会生成新版本，不改写历史。</span><BaseButton v-if="charter" variant="secondary" :disabled="charterSaving" @click="cancelCharterEdit">取消</BaseButton><BaseButton type="submit" variant="primary" :loading="charterSaving">{{ charter ? '保存为新版本' : '创建章程' }}</BaseButton></div>
      </form>
    </section>

    <section v-if="latestReviewed && latestReviewed.review" class="growth-panel" aria-labelledby="latest-review-heading">
      <div class="growth-panel__head"><div class="growth-panel__title"><span class="panel-icon"><BookOpenCheck :size="19" aria-hidden="true" /></span><div><h2 id="latest-review-heading">最近复盘</h2><p>{{ latestReviewed.title }} · {{ formatDate(latestReviewed.review.createdAt) }}</p></div></div></div>
      <div class="latest-review"><p>{{ latestReviewed.review.reflection }}</p><div class="review-lessons"><strong>留下的经验</strong><ul><li v-for="lesson in latestReviewed.review.lessons" :key="lesson">{{ lesson }}</li></ul></div><p class="review-next"><strong>下一步</strong>{{ latestReviewed.review.nextAction }}</p></div>
    </section>
  </div>
</template>

<style scoped>
.growth-page{max-width:1180px}
.growth-head,.section-head,.growth-panel__head,.growth-panel__title,.decision-card__head,.decision-actions,.form-actions,.subform-head,.outcome-block__title,.charter-panel__meta{display:flex;align-items:center}
.growth-head,.section-head,.growth-panel__head,.decision-card__head,.form-actions,.subform-head{justify-content:space-between}
.growth-head{gap:16px}.growth-head h1,.growth-panel__title h2,.board__column-head h3,.decision-card__head h4{font-family:var(--ws-font-display)}
.section-head{gap:12px;margin:6px 0 12px}
.growth-panel{margin-bottom:18px;border:1px solid var(--ws-border-color-3);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg);overflow:hidden}
.growth-panel__head{gap:12px;padding:15px 17px;border-bottom:1px solid var(--ws-border-color-3)}.growth-panel__title{gap:11px}.growth-panel__title h2{margin:0;font-size:17px}.growth-panel__title p{margin:3px 0 0;color:var(--ws-text-secondary-color);font-size:12px}
.panel-icon{display:grid;width:36px;height:36px;place-items:center;border-radius:9px;background:var(--ws-edit-color);color:var(--ws-primary-color)}
.version-badge,.decision-counts span{padding:2px 8px;border-radius:3px;border:1px solid var(--ws-border-color);background:transparent;color:var(--ws-text-secondary-color);font-size: 12px}.decision-counts{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:5px}.charter-panel__meta{gap:10px}
.growth-trend{margin:0 0 18px}.growth-trend>summary{display:inline-flex;align-items:center;gap:6px;margin-bottom:8px;color:var(--ws-text-secondary-color);font-size:12px;cursor:pointer;list-style:none}.growth-trend>summary::-webkit-details-marker{display:none}.growth-trend>summary::before{content:'';width:0;height:0;border-left:5px solid currentColor;border-top:4px solid transparent;border-bottom:4px solid transparent;transition:transform .15s}.growth-trend[open]>summary::before{transform:rotate(90deg)}.growth-trend>summary:hover{color:var(--ws-primary-color)}
.board{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:18px}
.board__column{display:grid;align-content:start;gap:10px;padding:12px;border:1px solid var(--ws-border-color-3);border-radius:var(--ws-radius-lg);background:var(--ws-surface-2)}
.board__column-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;padding:0 4px 4px}.board__column-head h3{margin:0;font-size:15px}.board__column-head small{color:var(--ws-text-secondary-color);font-size: 12px}
.board__empty{margin:0;padding:18px 8px;border:1px dashed var(--ws-border-color);border-radius:var(--ws-radius);color:var(--ws-text-secondary-color);font-size:12px;text-align:center}
.decision-card{display:grid;gap:9px;padding:13px;border:1px solid var(--ws-border-color-2);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg)}.decision-card.is-overdue{border-color:rgba(166,69,46,.45)}.decision-card.is-highlighted{outline:2px solid var(--ws-primary-color,#a6452e);outline-offset:2px;transition:outline-color .3s}
.decision-card__head{align-items:flex-start;gap:8px}.decision-card__head h4{margin:0;font-size:15px;line-height:1.45;overflow-wrap:anywhere}
.overdue-tag{flex:none;padding:1px 7px;border-radius:3px;border:1px solid var(--ws-primary-color);color:var(--ws-primary-color);font-size: 12px;font-weight:500}
.decision-choice{margin:0;font-size:13px;line-height:1.55;color:var(--ws-text-primary-color);overflow-wrap:anywhere}.decision-choice small{display:block;margin-bottom:2px;color:var(--ws-text-secondary-color);font-size:12px}
.decision-steps{display:flex;gap:14px;margin:0 0 4px;padding:0;list-style:none;font-size:12px;color:var(--ws-text-placeholder-color)}.decision-steps li{display:flex;align-items:center;gap:6px}.decision-steps li::before{content:'';width:8px;height:8px;border-radius:50%;border:1.5px solid currentColor}.decision-steps li.is-on{color:var(--ws-primary-color);font-weight:600}.decision-steps li.is-on::before{background:var(--ws-primary-color)}.decision-steps li.is-done{color:var(--ws-text-primary-color)}.decision-steps li.is-done::before{background:var(--ws-text-primary-color)}
.decision-chips{display:flex;flex-wrap:wrap;gap:6px}.chip{display:inline-flex;align-items:baseline;gap:4px;max-width:100%;padding:3px 8px;border-radius:999px;background:var(--ws-surface-2);color:var(--ws-text-primary-color);font-size: 12px;line-height:1.5;overflow-wrap:anywhere}.chip small{color:var(--ws-text-secondary-color);font-size: 12px}.chip--plain{margin:2px 6px 2px 0}
.decision-details{padding:6px 0;border-top:1px solid var(--ws-border-color-3)}.decision-details summary{color:var(--ws-primary-color);font-size: 12px;cursor:pointer}.decision-details dl{display:grid;grid-template-columns:76px 1fr;gap:6px 10px;margin-top:9px;font-size:12px;line-height:1.6}.decision-details dt{color:var(--ws-text-secondary-color)}.decision-details dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.decision-details ul{margin:0;padding-left:18px}
.outcome-block,.review-block,.latest-review{padding:11px 12px;border-radius:var(--ws-radius);background:var(--ws-surface-2)}.review-block{border:1px solid var(--ws-border-color-3)}.latest-review{margin:14px 17px}.outcome-block__title{gap:6px;color:var(--ws-success-color);font-size:12px;font-weight:600}.review-block .outcome-block__title{color:var(--ws-primary-color)}.outcome-block__title small{margin-left:auto;color:var(--ws-text-secondary-color);font-weight:400}.outcome-block p,.review-block p,.latest-review p{margin:7px 0 0;font-size:12px;line-height:1.65;white-space:pre-wrap}.muted{color:var(--ws-text-secondary-color)}
.review-lessons{margin-top:9px;color:var(--ws-text-color);font-size:12px}.review-lessons ul{margin:5px 0 0;padding-left:18px}.review-next{display:flex;gap:8px;padding-top:8px;border-top:1px solid var(--ws-border-color-3)}.review-next strong{flex:none;color:var(--ws-primary-color)}
.decision-actions{justify-content:flex-end;gap:10px}.decision-review-link{border:none;background:transparent;color:var(--ws-primary-color);font-family:inherit;font-size:12px;text-decoration:underline;cursor:pointer}.decision-review-link:disabled{opacity:.5;cursor:default}
.growth-form{display:grid;gap:8px;padding:17px}.form-intro{margin:0 0 4px;color:var(--ws-text-color);font-size:13px}.growth-form label,.field label,.inline-form label{color:var(--ws-text-color);font-size:12px;font-weight:600}.growth-form label small,.field label small,.inline-form label small{color:var(--ws-text-secondary-color);font-weight:400}.growth-form input,.growth-form textarea,.inline-form textarea{width:100%;padding:9px 11px;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius);background:var(--ws-body-bg);color:var(--ws-text-primary-color);font:inherit;font-size:13px;line-height:1.55}.growth-form input:focus,.growth-form textarea:focus,.inline-form textarea:focus{outline:0;border-color:var(--ws-primary-color);box-shadow:0 0 0 3px rgba(166,69,46,.15)}.growth-form textarea,.inline-form textarea{resize:vertical}.growth-form input:disabled,.growth-form textarea:disabled,.inline-form textarea:disabled{opacity:.6}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.field{display:grid;gap:8px}.compact-grid{align-items:end}.form-actions{justify-content:flex-end;gap:8px;margin-top:7px}.form-note{flex:1;color:var(--ws-text-secondary-color);font-size: 12px}
.decision-create{margin:0 0 18px;border:1px solid rgba(166,69,46,.35);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg)}.subform-head{gap:12px;margin-bottom:3px}.subform-head h3{margin:0;font-size:14px}.subform-head p{margin:3px 0 0;color:var(--ws-text-secondary-color);font-size: 12px}.collapse-button{display:grid;width:30px;height:30px;place-items:center;border:0;border-radius:6px;background:transparent;color:var(--ws-text-secondary-color)}.collapse-button:hover{background:var(--ws-surface-2)}
.inline-form{display:grid;gap:7px;padding:12px;border:1px solid rgba(166,69,46,.3);border-radius:var(--ws-radius);background:var(--ws-body-bg)}.inline-form h5{margin:0 0 3px;font-size:12px}.inline-form .form-actions{margin-top:3px}
.charter-summary{padding:15px 17px}.charter-summary__vision{margin:0 0 12px;padding-bottom:12px;border-bottom:1px solid var(--ws-border-color-3);font-size:15px;line-height:1.55;overflow-wrap:anywhere}.charter-summary__vision small,.charter-summary__rows small{display:block;margin-bottom:4px;color:var(--ws-text-secondary-color);font-size: 12px}.charter-summary__rows{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.charter-summary__rows>div{min-width:0}.charter-line{color:var(--ws-text-color);font-size:12px;line-height:1.55;overflow-wrap:anywhere}
@media(max-width:1023px){.board{grid-template-columns:1fr}.charter-summary__rows{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:760px){.growth-head,.section-head,.growth-panel__head{align-items:stretch;flex-direction:column}.growth-panel__head{gap:10px}.charter-panel__meta{justify-content:space-between}.decision-counts{justify-content:flex-start}.charter-summary__rows{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr}.form-actions{align-items:stretch;flex-wrap:wrap}.form-note{flex-basis:100%}.decision-details dl{grid-template-columns:1fr}.decision-details dt{margin-top:5px}}
</style>
