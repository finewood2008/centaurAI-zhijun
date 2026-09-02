<script setup lang="ts">
// 判断草稿面板：对话写、对象存、用户确认。知君只填它整理出来的部分；
// 选择 / 理由 / 把握 / 预期结果只能由用户填，知君的看法永远不进判断簿。
import { computed, reactive, ref, watch } from 'vue'
import type { DecisionDraft, DecisionDraftConfirmPayload } from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import { FIELD_LABELS, USER_REQUIRED_FIELDS, defaultReviewDate, draftMissingFields, reviewDateToIso } from '@/shared/decisionDraft'

const props = defineProps<{
  draft: DecisionDraft
  changedFields?: string[]
  busy?: boolean
  error?: string
}>()

const emit = defineEmits<{
  (e: 'confirm', payload: DecisionDraftConfirmPayload): void
  (e: 'discard'): void
}>()

const form = reactive({
  choice: '',
  rationale: '',
  confidence: null as number | null,
  expectedOutcome: '',
  reviewDate: '',
})
const touched = reactive<Record<string, boolean>>({})
const collapsed = ref(false)
const flash = ref<Set<string>>(new Set())
let flashTimer: number | null = null

// 草稿更新时，只把用户没碰过的字段同步进表单，避免覆盖正在编辑的内容。
watch(
  () => props.draft,
  (d) => {
    const f = d.fields
    if (!touched.choice) form.choice = f.choice ?? ''
    if (!touched.rationale) form.rationale = f.rationale ?? ''
    if (!touched.confidence) form.confidence = f.confidence ?? null
    if (!touched.expectedOutcome) form.expectedOutcome = f.expectedOutcome ?? ''
    if (!touched.reviewDate) form.reviewDate = f.reviewAt ? f.reviewAt.slice(0, 10) : ''
  },
  { immediate: true, deep: true },
)

watch(
  () => props.changedFields,
  (keys) => {
    if (!keys || !keys.length) return
    flash.value = new Set(keys)
    if (flashTimer) window.clearTimeout(flashTimer)
    flashTimer = window.setTimeout(() => {
      flash.value = new Set()
    }, 1600)
  },
)

const fromUser = computed(() => {
  const quotes = props.draft.fields.userQuotes ?? []
  const has = (v: string | null | undefined) => !!v && quotes.some((q) => q && (q.includes(v) || v.includes(q)))
  return {
    leaning: has(props.draft.fields.leaning),
    choice: has(props.draft.fields.choice),
    rationale: has(props.draft.fields.rationale),
    expectedOutcome: has(props.draft.fields.expectedOutcome),
  }
})

const missing = computed(() =>
  draftMissingFields({
    choice: form.choice,
    rationale: form.rationale,
    confidence: form.confidence,
    expectedOutcome: form.expectedOutcome,
  }),
)

const missingText = computed(() => missing.value.map((k) => FIELD_LABELS[k]).join('、'))
const defaultDate = defaultReviewDate()

function mark(key: string) {
  touched[key] = true
}

function submit() {
  if (props.busy) return
  const payload: DecisionDraftConfirmPayload = {
    choice: form.choice.trim() || undefined,
    rationale: form.rationale.trim() || undefined,
    confidence: form.confidence === null || form.confidence === undefined ? undefined : Math.round(form.confidence),
    expectedOutcome: form.expectedOutcome.trim() || undefined,
    reviewAt: reviewDateToIso(form.reviewDate),
  }
  emit('confirm', payload)
}

function isFlash(key: string) {
  return flash.value.has(key)
}
</script>

<template>
  <aside class="zj-panel" :class="{ 'is-collapsed': collapsed }" aria-label="判断草稿">
    <header class="zj-panel__head">
      <div class="zj-panel__title">
        <span class="zj-panel__lead">判断草稿</span>
        <span class="zj-panel__badge" :class="{ 'is-confirmed': draft.status === 'confirmed' }">
          {{ draft.status === 'confirmed' ? '已记进判断簿' : draft.status === 'discarded' ? '已放弃' : '未确认' }}
        </span>
        <span class="zj-panel__rev">第 {{ draft.revision }} 版</span>
      </div>
      <button type="button" class="zj-panel__toggle" :aria-expanded="!collapsed" @click="collapsed = !collapsed">
        {{ collapsed ? '展开' : '收起' }}
      </button>
    </header>

    <div v-show="!collapsed" class="zj-panel__body">
      <p class="zj-panel__rule">选择、理由、把握只能由你填写；知君的看法不会写进判断簿。</p>

      <dl class="zj-panel__facts">
        <div :class="{ 'is-flash': isFlash('title') }">
          <dt>标题</dt>
          <dd>{{ draft.fields.title || '—' }}</dd>
        </div>
        <div :class="{ 'is-flash': isFlash('context') }">
          <dt>背景</dt>
          <dd>{{ draft.fields.context || '—' }}</dd>
        </div>
        <div :class="{ 'is-flash': isFlash('options') }">
          <dt>选项</dt>
          <dd>
            <span v-if="!draft.fields.options.length" class="zj-panel__need">需要你说</span>
            <span v-for="o in draft.fields.options" :key="o" class="zj-panel__chip">{{ o }}</span>
          </dd>
        </div>
        <div :class="{ 'is-flash': isFlash('leaning') }">
          <dt>倾向</dt>
          <dd>
            {{ draft.fields.leaning || '—' }}
            <span v-if="draft.fields.leaning && fromUser.leaning" class="zj-panel__from">来自你的话</span>
          </dd>
        </div>
        <div :class="{ 'is-flash': isFlash('keyQuestion') }">
          <dt>关键问题</dt>
          <dd>{{ draft.fields.keyQuestion || '—' }}</dd>
        </div>
        <div :class="{ 'is-flash': isFlash('zhijunView') }">
          <dt><span class="layer-badge layer-badge--view">知君的看法</span></dt>
          <dd>{{ draft.fields.zhijunView || '（还没有）' }}</dd>
        </div>
      </dl>

      <form v-if="draft.status === 'draft'" class="zj-panel__form" @submit.prevent="submit">
        <label :class="{ 'is-flash': isFlash('choice') }">
          <span class="zj-panel__label">
            选择 <span aria-hidden="true">*</span>
            <span v-if="fromUser.choice && !touched.choice" class="zj-panel__from">来自你的话</span>
            <span v-else-if="missing.includes('choice')" class="zj-panel__need">需要你说</span>
          </span>
          <textarea v-model="form.choice" rows="2" maxlength="2000" :disabled="busy" @input="mark('choice')" />
        </label>
        <label :class="{ 'is-flash': isFlash('rationale') }">
          <span class="zj-panel__label">
            理由 <span aria-hidden="true">*</span>
            <span v-if="fromUser.rationale && !touched.rationale" class="zj-panel__from">来自你的话</span>
            <span v-else-if="missing.includes('rationale')" class="zj-panel__need">需要你说</span>
          </span>
          <textarea v-model="form.rationale" rows="2" maxlength="10000" :disabled="busy" @input="mark('rationale')" />
        </label>
        <label :class="{ 'is-flash': isFlash('confidence') }">
          <span class="zj-panel__label">
            把握（0–100）<span aria-hidden="true">*</span>
            <span v-if="missing.includes('confidence')" class="zj-panel__need">需要你说</span>
          </span>
          <span class="zj-panel__conf">
            <input v-model.number="form.confidence" type="range" min="0" max="100" step="5" :disabled="busy" @input="mark('confidence')" />
            <input v-model.number="form.confidence" type="number" min="0" max="100" :disabled="busy" aria-label="把握，0 到 100" @input="mark('confidence')" />
            <span>%</span>
          </span>
        </label>
        <label :class="{ 'is-flash': isFlash('expectedOutcome') }">
          <span class="zj-panel__label">
            预期结果 <span aria-hidden="true">*</span>
            <span v-if="fromUser.expectedOutcome && !touched.expectedOutcome" class="zj-panel__from">来自你的话</span>
            <span v-else-if="missing.includes('expectedOutcome')" class="zj-panel__need">需要你说</span>
          </span>
          <textarea v-model="form.expectedOutcome" rows="2" maxlength="5000" :disabled="busy" @input="mark('expectedOutcome')" />
        </label>
        <label>
          <span class="zj-panel__label">回访日期 <span class="zj-panel__hint">留空默认 {{ defaultDate }}（14 天后）</span></span>
          <input v-model="form.reviewDate" type="date" :disabled="busy" @input="mark('reviewDate')" />
        </label>

        <p v-if="error" class="zj-panel__error" role="alert">{{ error }}</p>
        <p v-else-if="missing.length" class="zj-panel__hint">确认前还需要你填：{{ missingText }}</p>

        <div class="zj-panel__actions">
          <BaseButton variant="secondary" size="sm" :disabled="busy" @click="emit('discard')">先不记</BaseButton>
          <BaseButton type="submit" variant="primary" size="sm" :loading="busy" :disabled="missing.length > 0">记进判断簿</BaseButton>
        </div>
      </form>

      <p v-else-if="draft.status === 'confirmed'" class="zj-panel__done">
        已记进判断簿。<router-link to="/judgments">去判断页查看</router-link>
      </p>
    </div>
  </aside>
</template>

<style scoped>
.zj-panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px dashed var(--ws-primary-color, #a6452e);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
}
.zj-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
}
.zj-panel.is-collapsed .zj-panel__head {
  border-bottom: none;
}
.zj-panel__title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.zj-panel__lead {
  font-family: var(--ws-font-display, serif);
  font-size: 16px;
  font-weight: 700;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-panel__badge {
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--ws-warning-color, #b8862b);
  color: var(--ws-warning-color, #b8862b);
  font-size: 12px;
}
.zj-panel__badge.is-confirmed {
  border-color: var(--ws-success-color, #4a7c59);
  color: var(--ws-success-color, #4a7c59);
}
.zj-panel__rev {
  font-size: 11px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-panel__toggle {
  border: none;
  background: transparent;
  color: var(--ws-primary-color, #a6452e);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-panel__body {
  overflow-y: auto;
  padding: 12px 14px 14px;
}
.zj-panel__rule {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-panel__facts {
  display: grid;
  gap: 8px;
  margin: 0 0 12px;
}
.zj-panel__facts > div {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 6px;
  padding: 4px 6px;
  border-radius: var(--ws-radius, 6px);
  font-size: 13px;
  line-height: 1.6;
}
.zj-panel__facts dt {
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-panel__facts dd {
  margin: 0;
  color: var(--ws-text-primary-color, #1d211f);
  overflow-wrap: anywhere;
}
.zj-panel__chip {
  display: inline-block;
  margin: 0 6px 4px 0;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--ws-card-bg, #f3efe6);
  border: 1px solid var(--ws-border-color, #d8d3c8);
  font-size: 12px;
}
.zj-panel__from,
.zj-panel__need {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 500;
}
.zj-panel__from {
  color: var(--ws-success-color, #4a7c59);
}
.zj-panel__need {
  color: var(--ws-primary-color, #a6452e);
}
.zj-panel__form {
  display: grid;
  gap: 10px;
}
.zj-panel__form label {
  display: grid;
  gap: 4px;
  padding: 4px 6px;
  border-radius: var(--ws-radius, 6px);
}
.zj-panel__label {
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-color, #3c403d);
}
.zj-panel__form textarea,
.zj-panel__form input[type='date'],
.zj-panel__form input[type='number'] {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: #fff;
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  font-size: 13px;
}
.zj-panel__form textarea:focus,
.zj-panel__form input:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.zj-panel__conf {
  display: flex;
  align-items: center;
  gap: 8px;
}
.zj-panel__conf input[type='range'] {
  flex: 1;
  accent-color: var(--ws-primary-color, #a6452e);
}
.zj-panel__conf input[type='number'] {
  width: 64px;
}
.zj-panel__hint {
  margin: 0;
  font-size: 11px;
  font-weight: 400;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-panel__error {
  margin: 0;
  font-size: 12px;
  color: var(--ws-danger-color, #a6452e);
}
.zj-panel__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.zj-panel__done {
  margin: 0;
  font-size: 13px;
  color: var(--ws-success-color, #4a7c59);
}
.zj-panel__done a {
  margin-left: 6px;
  color: var(--ws-primary-color, #a6452e);
}
.is-flash {
  animation: zj-flash 1.6s ease-out;
}
@keyframes zj-flash {
  0% {
    background: rgba(166, 69, 46, 0.14);
  }
  100% {
    background: transparent;
  }
}
</style>
