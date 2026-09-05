<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { getAlignmentState, grantAlignmentConsent, type AlignmentState, type Claim } from '@/services/api'
import { ALIGNMENT_LEVELS } from '@/shared/alignment'
const props = defineProps<{ conversationId: string; streaming?: boolean; managed?: boolean }>()
const emit = defineEmits<{ proposals: [claims: Claim[]]; localOnly: [value: boolean] }>()
const state = ref<AlignmentState | null>(null)
const selected = ref<string[]>([])
const busy = ref(false)
const error = ref('')
let timer: ReturnType<typeof setTimeout> | undefined
let epoch = 0
let stopped = false
const missing = computed(() => state.value?.sources.filter(s => !s.allowed) ?? [])

async function refresh() {
  const id = props.conversationId, ticket = ++epoch
  try {
    const result = await getAlignmentState(id)
    if (stopped || ticket !== epoch || id !== props.conversationId) return
    state.value = result
    emit('localOnly', !!result.state.local_only || result.sources.some(s => !s.allowed))
    selected.value = selected.value.filter(key => result.sources.some(s => s.fingerprint === key && !s.blocked))
    emit('proposals', result.proposals)
  } catch { /* transient poll failures leave the last state visible */ }
}
async function poll() {
  if (stopped) return
  if (!props.streaming) await refresh()
  if (!stopped) timer = setTimeout(poll, 5000)
}
watch(() => props.conversationId, () => {
  ++epoch; state.value = null; selected.value = []; emit('proposals', []); emit('localOnly', false)
  void refresh()
}, { immediate: true })
timer = setTimeout(poll, 5000)
onBeforeUnmount(() => { stopped = true; ++epoch; clearTimeout(timer) })
defineExpose({ refresh })

async function consent(localOnly: boolean) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    state.value = await grantAlignmentConsent(props.conversationId, { localOnly, serviceId: state.value?.service?.id,
      refs: state.value?.sources.filter(s => selected.value.includes(s.fingerprint) && !s.blocked).map(s => ({ claimId: s.claimId, fingerprint: s.fingerprint })) ?? [] })
    selected.value = []
    emit('localOnly', !!state.value.state.local_only || state.value.sources.some(s => !s.allowed))
  } catch (err) { error.value = err instanceof Error ? err.message : '授权未保存' }
  finally { busy.value = false }
}
</script>

<template>
  <aside v-if="!managed && state && (state.sources.length || state.state.status)" class="alignment-privacy" data-testid="alignment-privacy">
    <details>
      <summary>自我画像 · {{ state.state.local_only || missing.length ? '默认本机处理' : '查看校准与授权' }}</summary>
      <p v-if="state.state.detail">{{ state.state.detail }}</p>
      <p>深层画像不等于普通资料。未授权的内容及其对话衍生内容不会外发。</p>
      <p v-if="state.service?.external">外部服务：{{ state.service.name }} · {{ state.service.model }}</p>
      <div v-for="source in state.sources" :key="source.fingerprint" class="alignment-privacy__source">
        <label><input v-model="selected" type="checkbox" :value="source.fingerprint" :disabled="source.blocked || busy">{{ source.content }}</label>
        <p>第 {{ source.revision }} 版 · {{ source.level == null ? '尚未校准' : ALIGNMENT_LEVELS[source.level] }} · {{ source.allowed ? '当前服务已授权' : '未授权' }}</p>
        <p v-if="source.historical">历史版本：此前回复可能包含它的衍生内容，需单独选择才会授权。</p>
        <p v-if="source.proposal">当时的待确认提议：{{ source.proposal.reason }}（不是用户已认可的判断）</p>
        <p v-if="source.reason">说明：{{ source.reason }}</p>
        <details><summary>此次涉及的证据</summary><blockquote v-for="e in source.evidence" :key="e.id">{{ e.quote }}</blockquote></details>
        <p v-if="source.blocked">旧版本、已撤回或不可外发的画像：含这些历史内容的对话只能本地继续。</p>
      </div>
      <div class="alignment-privacy__actions">
        <button v-if="state.service?.external" type="button" :disabled="busy || !selected.length" @click="consent(false)">允许所选画像用于该服务</button>
        <button type="button" :disabled="busy" @click="consent(true)">仅本地处理</button>
      </div>
      <p v-if="!state.sources.length">校准后的具体画像会在这里逐项展示，再由你决定是否授权。</p>
      <p>文件权限仍需单独确认；修改画像、换服务或撤销授权后重新核对。</p>
    </details>
    <p v-if="error" role="alert">{{ error }}</p>
  </aside>
</template>

<style scoped>
.alignment-privacy { font-size: 12px; color: #66695f; padding: 7px 14px; background: #f8f4eb; border-bottom: 1px solid #e4ded1; max-height: 40vh; overflow: auto; flex-shrink: 0; }
summary { cursor: pointer; }
.alignment-privacy__source { padding: 10px; margin: 8px 0; border: 1px solid #ddd6c8; border-radius: 8px; overflow-wrap: anywhere; }
blockquote { margin: 7px; padding-left: 8px; border-left: 2px solid #ccc4b4; }
.alignment-privacy__actions { display: flex; flex-wrap: wrap; gap: 8px; }
button { font: inherit; padding: 6px 9px; background: #fffdf8; border: 1px solid #d7cfbf; border-radius: 5px; cursor: pointer; }
button:disabled { opacity: .5; }
</style>
