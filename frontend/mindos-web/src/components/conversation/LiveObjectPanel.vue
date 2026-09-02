<script setup lang="ts">
// 判断草稿面板：对话写、对象存、用户确认。知君只填它整理出来的部分；
// 选择 / 理由 / 把握 / 预期结果只能由用户填，知君的看法永远不进判断簿。
import { computed, reactive, ref, watch } from 'vue'
import { api, type DecisionDraft, type DecisionDraftConfirmPayload, type GrowthDecision } from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import { FIELD_LABELS, USER_REQUIRED_FIELDS, defaultReviewDate, draftMissingFields, reviewDateToIso } from '@/shared/decisionDraft'

const props = defineProps<{
  // 真实模型下草稿由后台整理：还没整理好时 draft 为 null，pending / timedOut 说明面板该显示什么
  draft: DecisionDraft | null
  changedFields?: string[]
  busy?: boolean
  error?: string
  pending?: boolean
  timedOut?: boolean
}>()

const emit = defineEmits<{
  (e: 'confirm', payload: DecisionDraftConfirmPayload): void
  (e: 'discard'): void
  (e: 'retry'): void
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
    if (!d) return
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
  const fields = props.draft?.fields
  const quotes = fields?.userQuotes ?? []
  const has = (v: string | null | undefined) => !!v && quotes.some((q) => q && (q.includes(v) || v.includes(q)))
  return {
    leaning: has(fields?.leaning),
    choice: has(fields?.choice),
    rationale: has(fields?.rationale),
    expectedOutcome: has(fields?.expectedOutcome),
  }
})

// 和这件事有关的过去判断：后端只给 id，这里从判断簿取标题 / 当时的选择 / 状态
const related = ref<GrowthDecision[]>([])
let decisionCache: GrowthDecision[] | null = null
watch(
  () => (props.draft?.fields.relatedDecisionIds ?? []).join(','),
  async (joined) => {
    const ids = joined ? joined.split(',') : []
    if (!ids.length) {
      related.value = []
      return
    }
    try {
      const cached = decisionCache ?? []
      if (!decisionCache || ids.some((id) => !cached.some((d) => d.id === id))) decisionCache = (await api.listGrowthDecisions()).items
      const byId = new Map(decisionCache.map((d) => [d.id, d]))
      related.value = ids.map((id) => byId.get(id)).filter((d): d is GrowthDecision => !!d)
    } catch {
      related.value = []
    }
  },
  { immediate: true },
)

function decisionStatusLabel(status: GrowthDecision['status']): string {
  if (status === 'reviewed') return '已复盘'
  if (status === 'outcome_recorded') return '已记结果'
  return '还没记结果'
}

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
        <span class="zj-seal" :class="!draft || draft.status === 'discarded' ? 'zj-seal--muted' : draft.status === 'confirmed' ? 'zj-seal--green' : 'zj-seal--warning'">
          {{ !draft ? (pending ? '整理中' : '还没好') : draft.status === 'confirmed' ? '已记进判断簿' : draft.status === 'discarded' ? '已放弃' : '还没记' }}
        </span>
      </div>
      <button type="button" class="zj-panel__toggle" :aria-expanded="!collapsed" @click="collapsed = !collapsed">
        {{ collapsed ? '展开' : '收起' }}
      </button>
    </header>

    <div v-show="!collapsed" class="zj-panel__body">
      <!-- 后台整理中 / 超时：还没有草稿可看 -->
      <div v-if="!draft" class="zj-panel__pending" role="status">
        <template v-if="pending"><span class="zj-panel__dot" aria-hidden="true" />知君在整理草稿…</template>
        <template v-else-if="timedOut">草稿还没整理好，稍后再看。<button type="button" class="zj-panel__link" @click="emit('retry')">再试</button></template>
      </div>
      <template v-else>
      <!-- 第一步：知君听到的 -->
      <section class="zj-panel__heard" aria-label="知君听到的">
        <h4 class="zj-panel__h">知君听到的</h4>
        <p v-if="pending" class="zj-panel__updating" role="status"><span class="zj-panel__dot" aria-hidden="true" />知君在更新草稿…</p>
        <p class="zj-panel__title-line" :class="{ 'is-flash': isFlash('title') }">{{ draft.fields.title || '（还没听出这件事叫什么）' }}</p>
        <p v-if="draft.fields.context" class="zj-panel__context" :class="{ 'is-flash': isFlash('context') }">{{ draft.fields.context }}</p>
        <div class="zj-panel__row" :class="{ 'is-flash': isFlash('options') }">
          <span class="zj-panel__k">选项</span>
          <span class="zj-panel__v">
            <span v-if="!draft.fields.options.length" class="zj-panel__need">还没说</span>
            <span v-for="o in draft.fields.options" :key="o" class="zj-panel__chip">{{ o }}</span>
          </span>
        </div>
        <div class="zj-panel__row" :class="{ 'is-flash': isFlash('leaning') }">
          <span class="zj-panel__k">你倾向</span>
          <span class="zj-panel__v">
            {{ draft.fields.leaning || '还没说' }}
            <span v-if="draft.fields.leaning && fromUser.leaning" class="zj-panel__from">你的原话</span>
          </span>
        </div>
        <div v-if="draft.fields.keyQuestion" class="zj-panel__row" :class="{ 'is-flash': isFlash('keyQuestion') }">
          <span class="zj-panel__k">关键问题</span>
          <span class="zj-panel__v">{{ draft.fields.keyQuestion }}</span>
        </div>
        <div v-if="draft.fields.zhijunView" class="zj-panel__view" :class="{ 'is-flash': isFlash('zhijunView') }">
          <span class="layer-badge layer-badge--view">知君的看法</span>
          <span>{{ draft.fields.zhijunView }}</span>
        </div>
        <div v-if="related.length" class="zj-panel__related" aria-label="和你过去的判断有关">
          <span class="zj-panel__k">和你过去的判断有关</span>
          <ul>
            <li v-for="d in related" :key="d.id">
              <router-link to="/judgments">{{ d.title }}</router-link> · 当时选了「{{ d.choice }}」 · {{ decisionStatusLabel(d.status) }}
            </li>
          </ul>
        </div>
      </section>

      <!-- 第二步：只有你能填的 -->
      <form v-if="draft.status === 'draft'" class="zj-panel__form" @submit.prevent="submit">
        <h4 class="zj-panel__h">只有你能填的</h4>
        <label :class="{ 'is-flash': isFlash('choice') }">
          <span class="zj-panel__label">
            我的选择
            <span v-if="fromUser.choice && !touched.choice" class="zj-panel__from">你的原话</span>
            <span v-else-if="missing.includes('choice')" class="zj-panel__need">需要你说</span>
          </span>
          <textarea v-model="form.choice" rows="2" maxlength="2000" :disabled="busy" placeholder="最后你打算怎么选" @input="mark('choice')" />
        </label>
        <label :class="{ 'is-flash': isFlash('rationale') }">
          <span class="zj-panel__label">
            为什么
            <span v-if="fromUser.rationale && !touched.rationale" class="zj-panel__from">你的原话</span>
            <span v-else-if="missing.includes('rationale')" class="zj-panel__need">需要你说</span>
          </span>
          <textarea v-model="form.rationale" rows="2" maxlength="10000" :disabled="busy" placeholder="关键的事实、假设和取舍" @input="mark('rationale')" />
        </label>
        <label :class="{ 'is-flash': isFlash('confidence') }">
          <span class="zj-panel__label">
            把握有几成
            <span v-if="missing.includes('confidence')" class="zj-panel__need">需要你说</span>
          </span>
          <span class="zj-panel__conf">
            <input v-model.number="form.confidence" type="range" min="0" max="100" step="5" :disabled="busy" aria-label="把握，0 到 100" @input="mark('confidence')" />
            <span class="zj-panel__conf-val">{{ form.confidence === null || form.confidence === undefined ? '—' : `${form.confidence}%` }}</span>
          </span>
        </label>
        <label :class="{ 'is-flash': isFlash('expectedOutcome') }">
          <span class="zj-panel__label">
            我预期会看到
            <span v-if="fromUser.expectedOutcome && !touched.expectedOutcome" class="zj-panel__from">你的原话</span>
            <span v-else-if="missing.includes('expectedOutcome')" class="zj-panel__need">需要你说</span>
          </span>
          <textarea v-model="form.expectedOutcome" rows="2" maxlength="5000" :disabled="busy" placeholder="到时候怎么判断这个选择对不对" @input="mark('expectedOutcome')" />
        </label>
        <details class="zj-panel__adv">
          <summary>回访日期 · 默认 {{ defaultDate }}（14 天后）</summary>
          <input v-model="form.reviewDate" type="date" :disabled="busy" aria-label="回访日期" @input="mark('reviewDate')" />
        </details>

        <p v-if="error" class="zj-panel__error" role="alert">{{ error }}</p>
        <p v-else-if="missing.length" class="zj-panel__hint">还差：{{ missingText }}</p>

        <div class="zj-panel__actions">
          <BaseButton variant="secondary" size="sm" :disabled="busy" @click="emit('discard')">先不记</BaseButton>
          <BaseButton type="submit" variant="primary" size="sm" :loading="busy" :disabled="missing.length > 0">记进判断簿</BaseButton>
        </div>
        <p class="zj-panel__rule">选择、理由、把握只能由你填；知君的看法不会写进判断簿。</p>
      </form>

      <p v-else-if="draft.status === 'confirmed'" class="zj-panel__done">
        已记进判断簿。<router-link to="/judgments">去判断页查看</router-link>
      </p>
      </template>
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
  background: var(--ws-card-bg, #fff);
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
  font-size: var(--ws-display-3, 16px);
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
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
.zj-panel__h {
  margin: 0 0 8px;
  font-family: var(--ws-font-display, serif);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-panel__heard {
  display: grid;
  gap: 6px;
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
}
.zj-panel__title-line {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: 15px;
  line-height: 1.5;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-panel__context {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-color, #3c403d);
}
.zj-panel__row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 6px;
  padding: 2px 4px;
  border-radius: var(--ws-radius, 6px);
  font-size: 13px;
  line-height: 1.6;
}
.zj-panel__k {
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-panel__v {
  color: var(--ws-text-primary-color, #1d211f);
  overflow-wrap: anywhere;
}
.zj-panel__view {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  padding: 6px 8px;
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-surface-2, #fbf8f1);
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-panel__view .layer-badge {
  flex: none;
  margin-top: 2px;
}
.zj-panel__chip {
  display: inline-block;
  margin: 0 6px 4px 0;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--ws-surface-2, #fbf8f1);
  border: 1px solid var(--ws-border-color, #d8d3c8);
  font-size: 12px;
}
.zj-panel__from,
.zj-panel__need {
  margin-left: 6px;
  font-size: 12px;
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
  padding: 2px 4px;
  border-radius: var(--ws-radius, 6px);
}
.zj-panel__label {
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-color, #3c403d);
}
.zj-panel__form textarea,
.zj-panel__form input[type='date'] {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-card-bg, #fff);
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
  gap: 10px;
}
.zj-panel__conf input[type='range'] {
  flex: 1;
  accent-color: var(--ws-primary-color, #a6452e);
}
.zj-panel__conf-val {
  min-width: 40px;
  font-family: var(--ws-font-display, serif);
  font-size: 14px;
  color: var(--ws-text-primary-color, #1d211f);
  text-align: right;
}
.zj-panel__adv {
  padding: 2px 4px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-panel__adv summary {
  cursor: pointer;
}
.zj-panel__adv input {
  margin-top: 6px;
}
.zj-panel__hint {
  margin: 0;
  font-size: 12px;
  font-weight: 400;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-panel__rule {
  margin: 4px 0 0;
  font-size: 12px;
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
.zj-panel__pending {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 4px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-panel__updating {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: -4px 0 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-panel__dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ws-primary-color, #a6452e);
  animation: zj-panel-pulse 1.6s ease-in-out infinite;
}
@keyframes zj-panel-pulse {
  0%,
  100% {
    opacity: 0.3;
  }
  50% {
    opacity: 1;
  }
}
.zj-panel__link {
  border: none;
  background: transparent;
  padding: 0 2px;
  color: var(--ws-primary-color, #a6452e);
  font: inherit;
  text-decoration: underline;
  cursor: pointer;
}
.zj-panel__related {
  display: grid;
  gap: 4px;
  padding: 6px 8px;
  border: 1px dashed var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  font-size: 13px;
  line-height: 1.6;
}
.zj-panel__related ul {
  margin: 0;
  padding-left: 16px;
}
.zj-panel__related a {
  color: var(--ws-primary-color, #a6452e);
}
@media (prefers-reduced-motion: reduce) {
  .zj-panel__dot {
    animation: none;
  }
}
</style>
