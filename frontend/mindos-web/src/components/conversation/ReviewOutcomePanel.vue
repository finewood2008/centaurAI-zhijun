<script setup lang="ts">
// 回访会话的右栏：当时的判断卡 + 「记下结果」表单。结果由用户亲自填写，知君只引导。
import { computed, ref } from 'vue'
import type { GrowthDecision } from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import { formatDate } from '@/shared/format'

const props = defineProps<{
  decision: GrowthDecision
  busy?: boolean
  error?: string
}>()

const emit = defineEmits<{ (e: 'record', payload: { result: string; notes: string }): void }>()

const result = ref('')
const notes = ref('')

const recorded = computed(() => props.decision.status !== 'open')
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

function submit() {
  const text = result.value.trim()
  if (!text || props.busy || recorded.value) return
  emit('record', { result: text, notes: notes.value.trim() })
}
</script>

<template>
  <aside class="zj-review" aria-label="回访的判断">
    <header class="zj-review__head">
      <span class="zj-review__lead">当时的判断</span>
      <span class="zj-review__badge">{{ statusLabel }}</span>
    </header>
    <dl class="zj-review__facts">
      <div><dt>标题</dt><dd>{{ decision.title }}</dd></div>
      <div><dt>当时选了</dt><dd>{{ decision.choice }}</dd></div>
      <div><dt>把握</dt><dd>{{ decision.confidence }}%</dd></div>
      <div><dt>预期</dt><dd>{{ decision.expectedOutcome }}</dd></div>
      <div><dt>回访日</dt><dd>{{ decision.reviewAt ? formatDate(decision.reviewAt) : '—' }}</dd></div>
    </dl>

    <section v-if="recorded && decision.outcome" class="zj-review__outcome">
      <strong>已记下的结果</strong>
      <p>{{ decision.outcome.result }}</p>
      <p v-if="decision.outcome.notes" class="zj-review__notes">{{ decision.outcome.notes }}</p>
      <p class="zj-review__hint">复盘在<router-link to="/judgments">判断页</router-link>完成。</p>
    </section>

    <form v-else class="zj-review__form" @submit.prevent="submit">
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
.zj-review__badge {
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  font-size: 12px;
  color: var(--ws-text-color, #3c403d);
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
.zj-review__outcome {
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
.zj-review__form label {
  display: grid;
  gap: 4px;
}
.zj-review__label {
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-color, #3c403d);
}
.zj-review__form textarea {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: #fff;
  font-family: inherit;
  font-size: 13px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-review__form textarea:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.zj-review__hint {
  font-size: 11px;
  font-weight: 400;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-review__hint a {
  color: var(--ws-primary-color, #a6452e);
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
</style>
