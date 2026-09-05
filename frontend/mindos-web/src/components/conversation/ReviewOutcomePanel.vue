<script setup lang="ts">
// 回访会话的右栏：当时的判断卡 → 第一步「记下结果」→ 第二步「复盘」（反思 / 经验 / 下一步）→ 已复盘则只读摘要。
// 结果与复盘都由用户亲自填写，知君只在对话里引导；复盘走和判断页同一个 createGrowthReview。
import { computed, ref } from 'vue'
import type { GrowthDecision } from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import { formatDate } from '@/shared/format'

const props = defineProps<{
  decision: GrowthDecision
  busy?: boolean
  error?: string
  reviewBusy?: boolean
  reviewError?: string
}>()

const emit = defineEmits<{
  (e: 'record', payload: { result: string; notes: string }): void
  (e: 'review', payload: { reflection: string; lessons: string[]; nextAction: string }): void
}>()

const result = ref('')
const notes = ref('')

const reflection = ref('')
const lessons = ref<string[]>([''])
const nextAction = ref('')

const step = computed<'outcome' | 'review' | 'done'>(() => {
  if (props.decision.status === 'reviewed') return 'done'
  if (props.decision.status === 'outcome_recorded') return 'review'
  return 'outcome'
})
const statusLabel = computed(() => {
  switch (props.decision.status) {
    case 'open':
      return '等结果'
    case 'outcome_recorded':
      return '已记结果，待复盘'
    case 'reviewed':
      return '已复盘'
    default:
      return props.decision.status
  }
})

const cleanLessons = computed(() => lessons.value.map((l) => l.trim()).filter(Boolean))
const reviewReady = computed(() => !!reflection.value.trim() && cleanLessons.value.length > 0 && !!nextAction.value.trim())

function submit() {
  const text = result.value.trim()
  if (!text || props.busy || step.value !== 'outcome') return
  emit('record', { result: text, notes: notes.value.trim() })
}

function addLesson() {
  lessons.value.push('')
}

function removeLesson(i: number) {
  if (lessons.value.length <= 1) {
    lessons.value = ['']
    return
  }
  lessons.value.splice(i, 1)
}

function submitReview() {
  if (!reviewReady.value || props.reviewBusy || step.value !== 'review') return
  emit('review', { reflection: reflection.value.trim(), lessons: cleanLessons.value, nextAction: nextAction.value.trim() })
}
</script>

<template>
  <aside class="zj-review" aria-label="回访的判断">
    <header class="zj-review__head">
      <span class="zj-review__lead">当时的判断</span>
      <span class="zj-seal zj-seal--muted">{{ statusLabel }}</span>
    </header>
    <dl class="zj-review__facts">
      <div><dt>标题</dt><dd>{{ decision.title }}</dd></div>
      <div><dt>当时选了</dt><dd>{{ decision.choice }}</dd></div>
      <div><dt>把握</dt><dd>{{ decision.confidence }}%</dd></div>
      <div><dt>预期</dt><dd>{{ decision.expectedOutcome }}</dd></div>
      <div><dt>回访日</dt><dd>{{ decision.reviewAt ? formatDate(decision.reviewAt) : '未定' }}</dd></div>
    </dl>

    <ol class="zj-review__steps" aria-label="回访的两步">
      <li :class="{ 'is-done': step !== 'outcome', 'is-current': step === 'outcome' }">记下结果</li>
      <li :class="{ 'is-done': step === 'done', 'is-current': step === 'review' }">复盘</li>
    </ol>

    <section v-if="step !== 'outcome' && decision.outcome" class="zj-review__outcome">
      <strong>已记下的结果</strong>
      <p>{{ decision.outcome.result }}</p>
      <p v-if="decision.outcome.notes" class="zj-review__notes">{{ decision.outcome.notes }}</p>
    </section>

    <form v-if="step === 'outcome'" class="zj-review__form" @submit.prevent="submit">
      <label>
        <span class="zj-review__label">结果 <span aria-hidden="true">*</span></span>
        <textarea v-model="result" rows="3" maxlength="10000" :disabled="busy" placeholder="实际发生了什么？和预期比差在哪？" required />
      </label>
      <label>
        <span class="zj-review__label">备注 <span class="zj-review__hint">可留空</span></span>
        <textarea v-model="notes" rows="2" maxlength="10000" :disabled="busy" />
      </label>
      <p v-if="error" class="zj-review__error" role="alert">{{ error }}</p>
      <div class="zj-review__actions">
        <BaseButton type="submit" variant="primary" size="sm" :loading="busy" :disabled="!result.trim()">记下结果</BaseButton>
      </div>
    </form>

    <form v-else-if="step === 'review'" class="zj-review__form" data-testid="review-reflect" @submit.prevent="submitReview">
      <p class="zj-review__intro">从预期和真实结果的差异开始，写下你现在怎么看。</p>
      <label>
        <span class="zj-review__label">反思 <span aria-hidden="true">*</span></span>
        <textarea v-model="reflection" rows="3" maxlength="10000" :disabled="reviewBusy" placeholder="我现在怎么看这次判断" required />
      </label>
      <div class="zj-review__lessons">
        <span class="zj-review__label">经验 <span aria-hidden="true">*</span> <span class="zj-review__hint">可以多条</span></span>
        <div v-for="(_, i) in lessons" :key="i" class="zj-review__lesson">
          <input v-model="lessons[i]" type="text" maxlength="2000" :disabled="reviewBusy" :placeholder="i === 0 ? '值得留下的一条经验' : '再来一条'" />
          <button type="button" class="zj-review__lesson-remove" :disabled="reviewBusy" aria-label="删掉这条经验" @click="removeLesson(i)">×</button>
        </div>
        <button type="button" class="zj-review__lesson-add" :disabled="reviewBusy" @click="addLesson">再加一条</button>
      </div>
      <label>
        <span class="zj-review__label">下一步 <span aria-hidden="true">*</span></span>
        <textarea v-model="nextAction" rows="2" maxlength="5000" :disabled="reviewBusy" placeholder="接下来打算怎么做" required />
      </label>
      <p v-if="reviewError" class="zj-review__error" role="alert">{{ reviewError }}</p>
      <div class="zj-review__actions">
        <BaseButton type="submit" variant="primary" size="sm" :loading="reviewBusy" :disabled="!reviewReady">记下复盘</BaseButton>
      </div>
    </form>

    <section v-else-if="decision.review" class="zj-review__done" data-testid="review-summary">
      <strong>复盘</strong>
      <p>{{ decision.review.reflection }}</p>
      <div class="zj-review__done-lessons">
        <span class="zj-review__label">留下的经验</span>
        <ul>
          <li v-for="(l, i) in decision.review.lessons" :key="i">{{ l }}</li>
        </ul>
      </div>
      <p class="zj-review__done-next"><span class="zj-review__label">下一步</span>{{ decision.review.nextAction }}</p>
      <p class="zj-review__hint">经验先保留为这次情境的候选理解，不自动变成长期原则。新的经历，可以帮我们核对旧理解。</p>
    </section>
  </aside>
</template>

<style scoped>
.zj-review {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px 14px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
}
.zj-review__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.zj-review__lead {
  font-family: var(--ws-font-display, serif);
  font-size: 16px;
  font-weight: 700;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-review__facts {
  display: grid;
  gap: 6px;
  margin: 0 0 12px;
}
.zj-review__facts > div {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 6px;
  font-size: 13px;
  line-height: 1.6;
}
.zj-review__facts dt {
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-review__facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.zj-review__steps {
  display: flex;
  gap: 14px;
  margin: 0 0 12px;
  padding: 0;
  list-style: none;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-review__steps li {
  display: flex;
  align-items: center;
  gap: 6px;
}
.zj-review__steps li::before {
  content: '';
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 1.5px solid var(--ws-border-color, #d8d3c8);
}
.zj-review__steps li.is-done::before {
  background: var(--ws-text-primary-color, #1d211f);
  border-color: var(--ws-text-primary-color, #1d211f);
}
.zj-review__steps li.is-current {
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}
.zj-review__steps li.is-current::before {
  background: var(--ws-primary-color, #a6452e);
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-review__outcome {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-success-color-bd, rgba(74, 124, 89, 0.08));
  font-size: 13px;
}
.zj-review__outcome p {
  margin: 6px 0 0;
  white-space: pre-wrap;
}
.zj-review__notes {
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-review__form {
  display: grid;
  gap: 10px;
}
.zj-review__form label,
.zj-review__lessons {
  display: grid;
  gap: 4px;
}
.zj-review__intro {
  margin: 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-review__label {
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-color, #3c403d);
}
.zj-review__form textarea,
.zj-review__form input {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-card-bg, #fff);
  font-family: inherit;
  font-size: 13px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-review__form textarea:focus,
.zj-review__form input:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.zj-review__lesson {
  display: flex;
  gap: 4px;
  align-items: center;
}
.zj-review__lesson-remove {
  flex: none;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-size: 14px;
  cursor: pointer;
}
.zj-review__lesson-remove:hover:not(:disabled) {
  color: var(--ws-danger-color, #a6452e);
}
.zj-review__lesson-add {
  justify-self: start;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--ws-primary-color, #a6452e);
  font-family: inherit;
  font-size: 12px;
  text-decoration: underline;
  cursor: pointer;
}
.zj-review__hint {
  font-size: 12px;
  font-weight: 400;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-review__error {
  margin: 0;
  font-size: 12px;
  color: var(--ws-danger-color, #a6452e);
}
.zj-review__actions {
  display: flex;
  justify-content: flex-end;
}
.zj-review__done {
  font-size: 13px;
  line-height: 1.6;
}
.zj-review__done p {
  margin: 6px 0 0;
  white-space: pre-wrap;
}
.zj-review__done-lessons {
  margin-top: 8px;
}
.zj-review__done-lessons ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.zj-review__done-next {
  display: flex;
  gap: 8px;
}
</style>
