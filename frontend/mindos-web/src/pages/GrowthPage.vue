<script setup lang="ts">
// 知君成长闭环 MVP：人生章程 → 判断 → 结果 → 复盘。
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { BookOpenCheck, ChevronDown, ChevronUp, Plus, RotateCcw, Sprout, Target } from 'lucide-vue-next'
import {
  api,
  ApiError,
  type GrowthCharter,
  type GrowthDecision,
  type GrowthDecisionStatus,
} from '@/services/api'
import { formatDate } from '@/shared/format'
import BaseButton from '@/components/ui/BaseButton.vue'
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

function resetDecisionForm() {
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
      <div><h1>成长</h1><p>记下当时为什么这样选，再用真实结果校准你对自己和世界的理解。</p></div>
      <BaseButton variant="primary" @click="showDecisionForm = true"><Plus :size="15" aria-hidden="true" />记录判断</BaseButton>
    </div>

    <section class="growth-panel charter-panel" aria-labelledby="charter-heading">
      <div class="growth-panel__head">
        <div class="growth-panel__title"><span class="panel-icon"><Sprout :size="19" aria-hidden="true" /></span><div><h2 id="charter-heading">人生章程</h2><p>它是知君理解你想去哪里的用户授权依据。</p></div></div>
        <span v-if="charter" class="version-badge">第 {{ charter.version }} 版 · 共 {{ charterVersionCount }} 版</span>
      </div>
      <div v-if="charterLoading" class="loading-state" aria-live="polite">正在加载人生章程…</div>
      <ErrorState v-else-if="charterError" :message="charterError" @retry="loadCharter" />
      <div v-else-if="charter && !showCharterForm" class="charter-summary">
        <div class="charter-summary__vision"><small>我想成为</small><strong>{{ charter.vision }}</strong></div>
        <div class="charter-summary__grid">
          <div><small>重要角色</small><span>{{ charter.roles.length ? charter.roles.join('、') : '未填写' }}</span></div>
          <div><small>当前目标</small><span>{{ charter.goals.length ? charter.goals.join('、') : '未填写' }}</span></div>
          <div><small>长期原则</small><span>{{ charter.principles.length ? charter.principles.join('、') : '未填写' }}</span></div>
          <div><small>知君如何挑战我</small><span>{{ charter.challengeStyle }}</span></div>
          <div><small>AI 决策边界</small><span>{{ charter.boundaries.length ? charter.boundaries.join('、') : '未填写' }}</span></div>
          <div><small>静默领域</small><span>{{ charter.quietDomains.length ? charter.quietDomains.join('、') : '无' }}</span></div>
        </div>
        <div class="charter-summary__actions"><BaseButton variant="secondary" size="sm" @click="startCharterEdit">更新章程</BaseButton></div>
      </div>
      <form v-else-if="showCharterForm" class="growth-form" @submit.prevent="saveCharter">
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

    <section class="growth-panel" aria-labelledby="decisions-heading">
      <div class="growth-panel__head">
        <div class="growth-panel__title"><span class="panel-icon"><Target :size="19" aria-hidden="true" /></span><div><h2 id="decisions-heading">判断簿</h2><p>把事后解释变成当时可核对的记录。</p></div></div>
        <div class="decision-counts"><span>{{ statusCounts.open }} 进行中</span><span>{{ statusCounts.outcome }} 待复盘</span><span>{{ statusCounts.reviewed }} 已完成</span></div>
      </div>

      <form v-if="showDecisionForm" id="decision-create" class="growth-form decision-create" @submit.prevent="createDecision">
        <div class="subform-head"><div><h3>记录当下的判断</h3><p>只记你此刻真正知道和相信的内容，不要预写结果。</p></div><button type="button" class="collapse-button" aria-label="收起判断表单" :disabled="decisionSaving" @click="showDecisionForm = false"><ChevronUp :size="18" /></button></div>
        <label for="decision-title">判断主题 <span aria-hidden="true">*</span></label><input id="decision-title" v-model="decisionTitle" maxlength="300" :disabled="decisionSaving" placeholder="例如：是否在今季度进入新市场" required>
        <label for="decision-context">背景与当前约束 <span aria-hidden="true">*</span></label><textarea id="decision-context" v-model="decisionContext" rows="3" maxlength="10000" :disabled="decisionSaving" placeholder="发生了什么，当时有哪些时间、资源或关系约束" required />
        <label for="decision-options">我认真考虑过的选项 <small>每行一项</small> <span aria-hidden="true">*</span></label><textarea id="decision-options" v-model="decisionOptions" rows="3" :disabled="decisionSaving" placeholder="现在进入&#10;先小规模验证&#10;暂不进入" required />
        <div class="form-grid">
          <div class="field"><label for="decision-choice">我的选择 <span aria-hidden="true">*</span></label><textarea id="decision-choice" v-model="decisionChoice" rows="3" maxlength="2000" :disabled="decisionSaving" required /></div>
          <div class="field"><label for="decision-expected">我预期会看到什么 <span aria-hidden="true">*</span></label><textarea id="decision-expected" v-model="decisionExpectedOutcome" rows="3" maxlength="5000" :disabled="decisionSaving" required /></div>
        </div>
        <label for="decision-rationale">为什么这样选 <span aria-hidden="true">*</span></label><textarea id="decision-rationale" v-model="decisionRationale" rows="3" maxlength="10000" :disabled="decisionSaving" placeholder="关键事实、假设与取舍" required />
        <div class="form-grid compact-grid">
          <div class="field"><label for="decision-confidence">当时信心度（0–100）</label><input id="decision-confidence" v-model.number="decisionConfidence" type="number" min="0" max="100" :disabled="decisionSaving"></div>
          <div class="field"><label for="decision-review-at">何时回看结果 <small>可留空</small></label><input id="decision-review-at" v-model="decisionReviewAt" type="datetime-local" :disabled="decisionSaving"></div>
        </div>
        <div class="form-actions"><BaseButton variant="secondary" :disabled="decisionSaving" @click="showDecisionForm = false">取消</BaseButton><BaseButton type="submit" variant="primary" :loading="decisionSaving">确认记录</BaseButton></div>
      </form>
      <button v-else type="button" class="open-form-button" @click="showDecisionForm = true"><Plus :size="16" aria-hidden="true" />记录一次重要判断<ChevronDown :size="16" aria-hidden="true" /></button>

      <div v-if="decisionsLoading" class="loading-state" aria-live="polite">正在加载判断簿…</div>
      <ErrorState v-else-if="decisionsError" :message="decisionsError" @retry="loadDecisions" />
      <EmptyState v-else-if="!decisions.length" title="还没有判断记录" description="从一个当下正在做的真实选择开始，不需要一次写得完美。"><template #action><BaseButton variant="primary" size="sm" @click="showDecisionForm = true">记录第一次判断</BaseButton></template></EmptyState>
      <div v-else class="decision-list">
        <article v-for="decision in decisions" :id="`decision-${decision.id}`" :key="decision.id" class="decision-card">
          <div class="decision-card__head"><div><h3>{{ decision.title }}</h3><p>{{ formatDate(decision.createdAt) }}<template v-if="decision.charterVersion"> · 基于章程第 {{ decision.charterVersion }} 版</template></p></div><span class="decision-status" :class="`is-${decision.status}`">{{ statusLabel(decision.status) }}</span></div>
          <div class="decision-summary"><div><small>当时选择</small><strong>{{ decision.choice }}</strong></div><div><small>信心度</small><strong>{{ decision.confidence }}%</strong></div><div><small>观察时间</small><strong>{{ formatDate(decision.reviewAt) }}</strong></div></div>
          <details class="decision-details"><summary>查看当时的完整记录</summary><dl><dt>背景</dt><dd>{{ decision.context }}</dd><dt>考虑过的选项</dt><dd><ul><li v-for="option in decision.options" :key="option">{{ option }}</li></ul></dd><dt>理由与假设</dt><dd>{{ decision.rationale }}</dd><dt>预期结果</dt><dd>{{ decision.expectedOutcome }}</dd></dl></details>

          <section v-if="decision.outcome" class="outcome-block"><div class="outcome-block__title"><BookOpenCheck :size="16" aria-hidden="true" />真实结果 <small>{{ formatDate(decision.outcome.recordedAt) }}</small></div><p>{{ decision.outcome.result }}</p><p v-if="decision.outcome.notes" class="muted">补充：{{ decision.outcome.notes }}</p></section>
          <section v-if="decision.review" class="review-block">
            <div class="outcome-block__title"><Sprout :size="16" aria-hidden="true" />这次成长复盘 <small>{{ formatDate(decision.review.createdAt) }}</small></div>
            <p>{{ decision.review.reflection }}</p>
            <div class="review-lessons"><strong>留下的经验</strong><ul><li v-for="lesson in decision.review.lessons" :key="lesson">{{ lesson }}</li></ul></div>
            <p class="review-next"><strong>下一步</strong>{{ decision.review.nextAction }}</p>
          </section>

          <div v-if="decision.status === 'open' && activeOutcomeId !== decision.id" class="decision-actions"><BaseButton variant="primary" size="sm" @click="startOutcome(decision)"><RotateCcw :size="14" aria-hidden="true" />记录真实结果</BaseButton></div>
          <form v-if="decision.status === 'open' && activeOutcomeId === decision.id" class="inline-form" @submit.prevent="recordOutcome(decision)">
            <h4>当初的预期是：{{ decision.expectedOutcome }}</h4><label :for="`outcome-result-${decision.id}`">真实结果 <span aria-hidden="true">*</span></label><textarea :id="`outcome-result-${decision.id}`" v-model="outcomeResult" rows="3" maxlength="10000" :disabled="outcomeSavingId === decision.id" required /><label :for="`outcome-notes-${decision.id}`">补充说明 <small>可留空</small></label><textarea :id="`outcome-notes-${decision.id}`" v-model="outcomeNotes" rows="2" maxlength="10000" :disabled="outcomeSavingId === decision.id" /><div class="form-actions"><BaseButton variant="secondary" size="sm" :disabled="outcomeSavingId === decision.id" @click="activeOutcomeId = ''">取消</BaseButton><BaseButton type="submit" variant="primary" size="sm" :loading="outcomeSavingId === decision.id">保存结果</BaseButton></div>
          </form>

          <div v-if="decision.status === 'outcome_recorded' && activeReviewId !== decision.id" class="decision-actions"><BaseButton variant="primary" size="sm" @click="startReview(decision)"><BookOpenCheck :size="14" aria-hidden="true" />完成复盘</BaseButton></div>
          <form v-if="decision.status === 'outcome_recorded' && activeReviewId === decision.id" class="inline-form" @submit.prevent="completeReview(decision)">
            <h4>从预期与真实结果的差异开始</h4><label :for="`review-reflection-${decision.id}`">我现在怎么看这次判断 <span aria-hidden="true">*</span></label><textarea :id="`review-reflection-${decision.id}`" v-model="reviewReflection" rows="3" maxlength="10000" :disabled="reviewSavingId === decision.id" required /><label :for="`review-lessons-${decision.id}`">值得留下的经验 <small>每行一项</small> <span aria-hidden="true">*</span></label><textarea :id="`review-lessons-${decision.id}`" v-model="reviewLessons" rows="3" :disabled="reviewSavingId === decision.id" required /><label :for="`review-next-${decision.id}`">下一步行动 <span aria-hidden="true">*</span></label><textarea :id="`review-next-${decision.id}`" v-model="reviewNextAction" rows="2" maxlength="5000" :disabled="reviewSavingId === decision.id" required /><div class="form-actions"><BaseButton variant="secondary" size="sm" :disabled="reviewSavingId === decision.id" @click="activeReviewId = ''">取消</BaseButton><BaseButton type="submit" variant="primary" size="sm" :loading="reviewSavingId === decision.id">确认完成复盘</BaseButton></div>
          </form>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.charter-summary{padding:15px 17px}.charter-summary__vision{display:grid;gap:4px;padding-bottom:12px;border-bottom:1px solid var(--ws-border-color-3)}.charter-summary small{color:var(--ws-text-secondary-color);font-size:10px}.charter-summary__vision strong{font-size:15px;line-height:1.55;overflow-wrap:anywhere}.charter-summary__grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}.charter-summary__grid>div{display:grid;align-content:start;gap:4px;min-width:0}.charter-summary__grid span{color:var(--ws-text-color);font-size:12px;line-height:1.55;overflow-wrap:anywhere}.charter-summary__actions{display:flex;justify-content:flex-end;margin-top:13px}
.review-block{margin-top:10px;padding:12px;border:1px solid #cfe1ff;border-radius:var(--ws-radius);background:#f8fbff}.review-block .outcome-block__title{color:var(--ws-primary-color)}.review-block p{margin:7px 0 0;font-size:12px;line-height:1.65;white-space:pre-wrap}.review-lessons{margin-top:9px;color:var(--ws-text-color);font-size:12px}.review-lessons ul{margin:5px 0 0;padding-left:18px}.review-next{display:flex;gap:8px;padding-top:8px;border-top:1px solid #dceaff}.review-next strong{flex:none;color:var(--ws-primary-color)}
.growth-page{max-width:1040px}.growth-head,.growth-panel__head,.growth-panel__title,.decision-card__head,.decision-actions,.form-actions,.subform-head,.outcome-block__title{display:flex;align-items:center}.growth-head,.growth-panel__head,.decision-card__head,.form-actions,.subform-head{justify-content:space-between}.growth-head{gap:16px}.growth-panel{margin-bottom:18px;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius-lg);background:var(--ws-body-bg);overflow:hidden}.growth-panel__head{gap:12px;padding:15px 17px;border-bottom:1px solid var(--ws-border-color-3)}.growth-panel__title{gap:11px}.growth-panel__title h2{margin:0;font-size:16px}.growth-panel__title p{margin:3px 0 0;color:var(--ws-text-secondary-color);font-size:11px}.panel-icon{display:grid;width:36px;height:36px;place-items:center;border-radius:9px;background:var(--ws-edit-color);color:var(--ws-primary-color)}.version-badge,.decision-counts span{padding:4px 8px;border-radius:999px;background:var(--ws-card-bg);color:var(--ws-text-secondary-color);font-size:11px}.decision-counts{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:5px}
.growth-form{display:grid;gap:8px;padding:17px}.growth-form label,.field label,.inline-form label{color:var(--ws-text-color);font-size:12px;font-weight:650}.growth-form label small,.field label small,.inline-form label small{color:var(--ws-text-secondary-color);font-weight:400}.growth-form input,.growth-form textarea,.inline-form textarea{width:100%;padding:9px 11px;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius);background:var(--ws-body-bg);color:var(--ws-text-primary-color);font:inherit;font-size:13px;line-height:1.55}.growth-form input:focus,.growth-form textarea:focus,.inline-form textarea:focus{outline:0;border-color:var(--ws-primary-color);box-shadow:0 0 0 3px var(--accent-ring)}.growth-form textarea,.inline-form textarea{resize:vertical}.growth-form input:disabled,.growth-form textarea:disabled,.inline-form textarea:disabled{opacity:.6}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.field{display:grid;gap:8px}.compact-grid{align-items:end}.form-actions{justify-content:flex-end;gap:8px;margin-top:7px}.form-note{flex:1;color:var(--ws-text-secondary-color);font-size:11px}.decision-create{margin:14px;border:1px solid #b9d7ff;border-radius:var(--ws-radius-lg);background:#fbfdff}.subform-head{gap:12px;margin-bottom:3px}.subform-head h3{margin:0;font-size:14px}.subform-head p{margin:3px 0 0;color:var(--ws-text-secondary-color);font-size:11px}.collapse-button{display:grid;width:30px;height:30px;place-items:center;border:0;border-radius:6px;background:transparent;color:var(--ws-text-secondary-color)}.collapse-button:hover{background:var(--ws-card-bg)}.open-form-button{display:flex;align-items:center;justify-content:center;gap:7px;width:calc(100% - 28px);margin:14px;padding:11px;border:1px dashed #b9d7ff;border-radius:var(--ws-radius);background:#fbfdff;color:var(--ws-primary-color);font:inherit;font-size:12px;font-weight:650}
.decision-list{display:grid;gap:10px;padding:0 14px 14px}.decision-card{padding:15px;border:1px solid var(--ws-border-color-2);border-radius:var(--ws-radius-lg);background:var(--ws-body-bg)}.decision-card__head{align-items:flex-start;gap:10px}.decision-card__head h3{margin:0;font-size:15px;overflow-wrap:anywhere}.decision-card__head p{margin:4px 0 0;color:var(--ws-text-secondary-color);font-size:10px}.decision-status{flex:none;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:650}.decision-status.is-open{background:var(--ws-warning-color-bd);color:#9a5b00}.decision-status.is-outcome_recorded{background:var(--ws-edit-color);color:var(--ws-primary-color)}.decision-status.is-reviewed{background:var(--ws-success-color-bd);color:#09822a}.decision-summary{display:grid;grid-template-columns:minmax(0,2fr) repeat(2,minmax(120px,1fr));gap:8px;margin:12px 0}.decision-summary>div{display:grid;gap:3px;padding:9px 10px;border-radius:var(--ws-radius);background:var(--ws-card-bg)}.decision-summary small{color:var(--ws-text-secondary-color);font-size:10px}.decision-summary strong{font-size:12px;overflow-wrap:anywhere}.decision-details{padding:8px 0;border-top:1px solid var(--ws-border-color-3);border-bottom:1px solid var(--ws-border-color-3)}.decision-details summary{color:var(--ws-primary-color);font-size:11px;cursor:pointer}.decision-details dl{display:grid;grid-template-columns:100px 1fr;gap:7px 10px;margin-top:11px;font-size:12px;line-height:1.6}.decision-details dt{color:var(--ws-text-secondary-color)}.decision-details dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.decision-details ul{margin:0;padding-left:18px}.outcome-block{margin-top:10px;padding:11px 12px;border-radius:var(--ws-radius);background:#f7fbf8}.outcome-block__title{gap:6px;color:#09822a;font-size:12px;font-weight:700}.outcome-block__title small{margin-left:auto;color:var(--ws-text-secondary-color);font-weight:400}.outcome-block p{margin:7px 0 0;font-size:12px;line-height:1.6;white-space:pre-wrap}.outcome-block .muted{color:var(--ws-text-secondary-color)}.decision-actions{justify-content:flex-end;margin-top:11px}.inline-form{display:grid;gap:7px;margin-top:11px;padding:12px;border:1px solid #b9d7ff;border-radius:var(--ws-radius);background:#fbfdff}.inline-form h4{margin:0 0 3px;font-size:12px}.inline-form .form-actions{margin-top:3px}
@media(max-width:760px){.growth-head,.growth-panel__head{align-items:stretch;flex-direction:column}.growth-panel__head{gap:10px}.version-badge{align-self:flex-start}.decision-counts{justify-content:flex-start}.charter-summary__grid{grid-template-columns:1fr}.form-grid,.decision-summary{grid-template-columns:1fr}.form-actions{align-items:stretch;flex-wrap:wrap}.form-note{flex-basis:100%}.decision-details dl{grid-template-columns:1fr}.decision-details dt{margin-top:5px}}
</style>
