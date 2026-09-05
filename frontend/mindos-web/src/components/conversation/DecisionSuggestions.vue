<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { suggestDecisionDirections, type DecisionDraft, type DecisionDirection } from '@/services/api'
import { ASSISTABLE_FIELDS, type AssistableField } from '@/shared/decisionDraft'

const props = defineProps<{
  draft: DecisionDraft
  current: Record<AssistableField, string>
  disabled?: boolean
}>()
const emit = defineEmits<{ (e: 'choose', direction: DecisionDirection, onlyEmpty: boolean): void }>()
const candidates = ref<DecisionDirection[]>([])
const selected = ref<DecisionDirection | null>(null)
const loading = ref(false)
const error = ref('')
const model = ref('')
const external = ref(false)
let controller: AbortController | null = null
let epoch = 0
let timer: ReturnType<typeof setTimeout> | null = null

function reset() {
  epoch++
  controller?.abort()
  controller = null
  if (timer) clearTimeout(timer)
  loading.value = false
  candidates.value = []
  selected.value = null
  error.value = ''
}
watch(() => `${props.draft.id}:${props.draft.revision}:${props.draft.status}`, reset)
onBeforeUnmount(reset)

async function generate() {
  if (loading.value || props.disabled || props.draft.status !== 'draft') return
  const attempt = ++epoch
  const current = { ...props.current }
  const abort = new AbortController()
  controller = abort
  loading.value = true
  error.value = ''
  selected.value = null
  timer = setTimeout(() => abort.abort(), 65000)
  try {
    const result = await suggestDecisionDirections(props.draft.conversationId, {
      draftId: props.draft.id, expectedRevision: props.draft.revision, current,
      avoidChoices: candidates.value.map(c => c.choice),
    }, abort.signal)
    if (attempt !== epoch) return
    if (JSON.stringify(current) !== JSON.stringify(props.current)) {
      error.value = '你在生成期间修改了内容，请按最新内容重新生成；已填写的内容没有改变。'
      return
    }
    candidates.value = result.candidates
    model.value = result.model
    external.value = result.external
  } catch (err) {
    if (attempt !== epoch) return
    error.value = abort.signal.aborted ? '等待候选超时，可以重试或自己填写。' : err instanceof Error ? err.message : '候选生成失败，请重试或自己填写。'
  } finally {
    if (attempt === epoch) {
      if (timer) clearTimeout(timer)
      loading.value = false
      controller = null
    }
  }
}

function choose(candidate: DecisionDirection) {
  if (props.disabled || loading.value) return
  if (ASSISTABLE_FIELDS.some(key => props.current[key].trim())) selected.value = candidate
  else apply(candidate, false)
}
function apply(candidate: DecisionDirection, onlyEmpty: boolean) {
  if (props.disabled) return
  emit('choose', candidate, onlyEmpty)
  selected.value = null
  candidates.value = []
}
</script>

<template>
  <section class="directions" aria-label="AI 候选方向" data-testid="decision-suggestions" :aria-busy="loading">
    <button type="button" class="directions__generate" :disabled="disabled || loading" @click="generate">
      {{ loading ? '正在想几个方向…' : candidates.length ? '换一组方向' : '帮我想几个方向' }}
    </button>
    <p class="directions__hint">AI 起草，你来选或改，也可以都不用。{{ model ? `${external ? '在线' : '本地'}处理 · ${model}` : '按对话模式处理，使用受保护内容前单独授权。' }}</p>
    <p v-if="loading" class="directions__hint" role="status">不会自动填入或保存；你仍可自己填写。</p>
    <p v-if="error" class="directions__error" role="alert">{{ error }}</p>
    <div v-if="candidates.length" class="directions__list">
      <article v-for="(candidate, i) in candidates" :key="i" class="directions__card" data-testid="decision-direction">
        <h5>{{ candidate.title }}</h5>
        <dl>
          <dt>选择</dt><dd>{{ candidate.choice }}</dd>
          <dt>理由与取舍</dt><dd>{{ candidate.rationale }}</dd>
          <dt>预期观察</dt><dd>{{ candidate.expectedOutcome }}</dd>
        </dl>
        <button type="button" :disabled="disabled || loading" :aria-label="`使用这个方向：${candidate.title}`" @click="choose(candidate)">使用这个方向</button>
        <div v-if="selected === candidate" class="directions__replace" role="group" aria-label="确认如何填入候选">
          <p>已选「{{ selected.title }}」。你已经填写过内容，如何处理？</p>
          <p class="directions__hint">只补空白会保留已有内容，请检查前后是否一致。把握和回访日期都不会改变。</p>
          <button type="button" :disabled="disabled" @click="apply(selected, true)">只补空白</button>
          <button type="button" :disabled="disabled" @click="apply(selected, false)">替换选择、理由和预期</button>
          <button type="button" @click="selected = null">取消</button>
        </div>
      </article>
      <button type="button" class="directions__skip" :disabled="disabled || loading" @click="candidates = []; selected = null">都不合适，我自己写</button>
    </div>
  </section>
</template>

<style scoped>
.directions { display: grid; gap: 7px; padding: 10px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 8px; background: var(--ws-surface-2, #fbf8f1); }
.directions button { cursor: pointer; font: inherit; font-size: 12px; color: var(--ws-primary-color, #a6452e); border: 1px solid var(--ws-border-color, #d8d3c8); background: var(--ws-card-bg, #fff); border-radius: 6px; padding: 7px 9px; text-align: left; }
.directions button:hover:not(:disabled) { border-color: var(--ws-primary-color, #a6452e); }
.directions button:focus-visible { outline: 2px solid var(--ws-primary-color, #a6452e); outline-offset: 2px; }
.directions button:disabled { opacity: .5; cursor: default; }
.directions__generate { justify-self: start; }
.directions__hint { margin: 0; font-size: 11px; line-height: 1.6; color: var(--ws-text-secondary-color, #686b66); overflow-wrap: anywhere; }
.directions__list { display: grid; gap: 8px; }
.directions__card { padding: 10px; background: var(--ws-card-bg, #fff); border: 1px solid var(--ws-border-color-3, #ebe7de); border-radius: 6px; }
.directions__card h5 { margin: 0 0 6px; font-size: 13px; color: var(--ws-text-primary-color, #1d211f); }
.directions__card dl { margin: 0 0 8px; font-size: 12px; line-height: 1.65; overflow-wrap: anywhere; }
.directions__card dt { color: var(--ws-text-secondary-color, #686b66); font-size: 11px; margin-top: 5px; }
.directions__card dd { margin: 0; color: var(--ws-text-primary-color, #1d211f); }
.directions__replace { border-top: 1px solid var(--ws-border-color, #d8d3c8); padding-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; font-size: 12px; }
.directions__replace p { width: 100%; margin: 0; }
.directions__error { font-size: 12px; color: var(--ws-danger-color, #a33b2b); margin: 0; }
</style>
