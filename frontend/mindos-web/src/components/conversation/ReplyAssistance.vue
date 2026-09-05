<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { routedTask, routingRequest } from '@/services/taskRouting'
import { REPLY_CONTROLS, type ReplyAssistanceInput, type ReplyBatch } from '@/shared/replyAssistance'
import { isReplySourceError, replyRecoveries } from '@/composables/useReplyRecovery'
const props = defineProps<{ conversationId: string; messageId: string; disabled?: boolean }>()
const emit = defineEmits<{ (e: 'insert', text: string, origin: ReplyAssistanceInput): void; (e: 'write'): void }>()
const batch = ref<ReplyBatch | null>(null)
const expanded = ref(false)
const loading = ref(false)
const error = ref('')
const stale = ref(false)
let epoch = 0
let controller: AbortController | null = null
let requestId = crypto.randomUUID()
let pendingRequest: Record<string, unknown> | null = null
const path = () => `/mindos/conversations/${encodeURIComponent(props.conversationId)}/reply-assistance`
function cancel() { epoch++; controller?.abort(); controller = null; loading.value = false }
watch(() => [props.conversationId, props.messageId], async () => {
  cancel(); batch.value = null; expanded.value = false; error.value = ''; stale.value = false; requestId = crypto.randomUUID(); pendingRequest = null
  const attempt = epoch
  try {
    const result = await routingRequest<{ batch: ReplyBatch | null }>(path())
    if (attempt === epoch && result.batch?.messageId === props.messageId) batch.value = result.batch
  } catch { /* Retrieval failure must never block the normal composer. */ }
}, { immediate: true })
watch(() => props.disabled, disabled => { if (disabled) cancel() })
watch(() => replyRecoveries[props.conversationId], issue => {
  if (!issue || issue.messageId !== props.messageId || (batch.value && !issue.batchIds.includes(batch.value.id))) return
  cancel(); batch.value = null; pendingRequest = null; requestId = crypto.randomUUID(); stale.value = true
  expanded.value = true; error.value = '旧候选已失效。可以重新生成回答；输入框的原文和来源仍保留，不会被覆盖。'
})
onBeforeUnmount(cancel)

async function generate(change = false, localOnly = false, retry = false) {
  if (props.disabled || loading.value) return
  expanded.value = true; error.value = ''; stale.value = false
  if (batch.value && !change && !localOnly && !retry) return
  if (change || localOnly) requestId = crypto.randomUUID()
  if (!retry || !pendingRequest) pendingRequest = {
    messageId: props.messageId, requestId, localOnly, previousBatchId: change ? batch.value?.id : undefined,
  }
  const attempt = ++epoch
  const abort = new AbortController(); controller = abort; loading.value = true
  const timer = setTimeout(() => abort.abort(), 65000)
  try {
    const result = await routedTask<{ batch: ReplyBatch }>(props.conversationId, path(), pendingRequest, abort.signal)
    if (attempt !== epoch) return
    if (result.batch.messageId !== props.messageId) return
    batch.value = result.batch
  } catch (e) {
    if (attempt !== epoch) return
    error.value = abort.signal.aborted ? '等待超时，仍可自己输入或重试。' : e instanceof Error ? e.message : '暂时没有合适方向，仍可自己说。'
    if (isReplySourceError(e)) {
      batch.value = null; pendingRequest = null; requestId = crypto.randomUUID(); stale.value = true
      error.value += ' 已清理失效候选，可以重新生成；输入原文仍保留。'
    }
  } finally {
    clearTimeout(timer)
    if (attempt === epoch) { loading.value = false; controller = null }
  }
}
function insert(text: string, origin: ReplyAssistanceInput) {
  if (props.disabled) return
  emit('insert', text, origin)
}
function close() { cancel(); expanded.value = false }
</script>
<template>
  <section class="reply-help" aria-label="轻松回复" data-testid="reply-assistance">
    <div class="reply-help__bar">
      <button :disabled="disabled" @click="insert(REPLY_CONTROLS.rephrase, { messageId, selections: [], control: 'rephrase' })">换个说法</button>
      <button :disabled="disabled" @click="insert(REPLY_CONTROLS.pause, { messageId, selections: [], control: 'pause' })">先放一放</button>
      <button :disabled="disabled" :aria-expanded="expanded" @click="generate()">帮我开个头</button>
    </div>
    <div v-if="expanded" class="reply-help__expanded">
      <p class="reply-help__hint">你可以这样回答：选一句贴近你的，直接使用或改几个字，再发送。都不像也没关系。</p>
      <p v-if="loading" role="status">正在准备几种回答…你仍可自己输入。</p>
      <p v-if="error" role="alert" class="reply-help__error">{{ error }}</p>
      <div v-if="!loading && !error && batch" class="reply-help__options">
        <button v-for="candidate in batch.candidates" :key="candidate.id" :disabled="disabled"
          @click="insert(candidate.text, { messageId, selections: [{ batchId: batch.id, candidateId: candidate.id }] })">{{ candidate.text }}</button>
        <p v-if="!batch.candidates.length">暂时没有合适的方向，可以只说一点，或先不回答。</p>
      </div>
      <div class="reply-help__bar">
        <button :disabled="disabled || loading" @click="close(); emit('write')">都不太像，我自己说</button>
        <button v-if="batch && !error" :disabled="disabled || loading" @click="generate(true)">换一组</button>
        <button v-if="error" :disabled="disabled || loading" @click="generate(false, false, !stale)">{{ stale ? '重新生成回答' : '重试' }}</button>
        <button v-if="error" :disabled="disabled || loading" @click="generate(false, true)">改用本地</button>
        <button @click="close">{{ loading ? '取消' : '收起' }}</button>
      </div>
      <p v-if="batch && !loading" class="reply-help__hint">{{ batch.external ? '在线' : '本地' }}处理 · {{ batch.model }}<span v-if="batch.excluded.length"> · 部分历史未使用</span></p>
    </div>
  </section>
</template>
<style scoped>
.reply-help { max-width:760px; margin-top:8px; font-size:12px; line-height:1.6; color:var(--ws-text-secondary-color,#686b66); }
.reply-help__bar { display:flex; flex-wrap:wrap; gap:6px; }
.reply-help button { font:inherit; color:inherit; border:1px solid var(--ws-border-color-3,#ebe7de); background:transparent; padding:5px 10px; border-radius:16px; cursor:pointer; text-align:left; }
.reply-help button:hover:not(:disabled),.reply-help button:focus-visible { border-color:var(--ws-primary-color,#a6452e); color:var(--ws-primary-color,#a6452e); }
.reply-help button:disabled { opacity:.5; cursor:default; }
.reply-help__expanded { margin-top:8px; display:grid; gap:8px; padding:10px; border-left:2px solid var(--ws-border-color,#d8d3c8); }
.reply-help p { margin:0; }.reply-help__hint { font-size:11px; }.reply-help__error { color:var(--ws-danger-color,#a6452e); }
.reply-help__options { display:grid; gap:6px; }.reply-help__options button { border-radius:8px; background:var(--ws-card-bg,#fff); font-size:14px; overflow-wrap:anywhere; line-height:1.7; padding:8px 10px; }
</style>
