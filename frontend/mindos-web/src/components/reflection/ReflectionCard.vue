<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { reviewReflection, type Reflection, type ReflectionFeedback } from '@/services/api'
import { reflectionLabels, reflectionResponses } from '@/shared/reflections'
import { productScopeEpoch } from '@/shared/productScope'

const props = defineProps<{ reflection: Reflection; disabled?: boolean }>()
const emit = defineEmits<{ updated: [reflection: Reflection] }>()
const busy = ref(false)
const error = ref('')
const notice = ref('')
const editing = ref(false)
const note = ref(props.reflection.feedback.note ?? '')
const reviewed = computed(() => !['candidate', 'surfaced'].includes(props.reflection.status))
let alive = true
let attempt = { signature: '', requestId: '' }
onBeforeUnmount(() => { alive = false })
watch(() => props.reflection.id, () => { note.value = props.reflection.feedback.note ?? ''; editing.value = false; error.value = ''; notice.value = ''; attempt = { signature: '', requestId: '' } })

async function save(action: ReflectionFeedback) {
  if (busy.value || props.disabled) return
  if (action === 'contextual' && !note.value.trim()) { editing.value = true; return }
  const id = props.reflection.id, epoch = productScopeEpoch()
  const payload = { action, note: note.value.trim(), expectedRevision: props.reflection.revision }
  const signature = JSON.stringify({ id, ...payload })
  if (attempt.signature !== signature) attempt = { signature, requestId: crypto.randomUUID() }
  busy.value = true
  error.value = ''
  try {
    const result = await reviewReflection(id, { ...payload, requestId: attempt.requestId })
    if (!alive || epoch !== productScopeEpoch() || props.reflection.id !== id) return
    notice.value = reflectionResponses[action]
    editing.value = false
    emit('updated', result)
  } catch (err) {
    if (alive && epoch === productScopeEpoch() && props.reflection.id === id) error.value = err instanceof Error ? err.message : '这次反馈还没有保存，请稍后再试'
  } finally { if (alive) busy.value = false }
}
</script>

<template>
  <article class="reflection-card" data-testid="reflection-card" :aria-busy="busy">
    <header><span class="reflection-eyebrow">照见 · {{ reflection.type === 'change' ? '留意到的变化' : '几段经历之间' }}</span><span>{{ reflectionLabels[reflection.status] }}</span></header>
    <h3>{{ reflection.title }}</h3>
    <p class="reflection-observation">{{ reflection.observation }}</p>
    <p class="reflection-hint">这是一个可以修正的观察，你最了解其中的情境。</p>
    <div v-if="reflection.feedback.note" class="reflection-correction"><strong>你的补充</strong><p>{{ reflection.feedback.note }}</p></div>
    <details class="reflection-evidence">
      <summary>为什么这样想 · {{ reflection.evidence.length }} 段经历</summary>
      <ol><li v-for="e in reflection.evidence" :key="e.messageId">
        <time :datetime="e.date">{{ e.date.slice(0, 10) }}</time><blockquote>{{ e.quote }}</blockquote>
        <RouterLink :to="{ path: `/c/${encodeURIComponent(e.conversationId)}`, query: { message: e.messageId } }">回到这段对话</RouterLink>
      </li></ol>
      <p><strong>也可能是：</strong>{{ reflection.alternative }}</p>
    </details>
    <div v-if="!reviewed || editing" class="reflection-actions" aria-label="这条观察贴近你吗">
      <button type="button" :disabled="busy || disabled" @click="save('accepted')">像我</button>
      <button type="button" :disabled="busy || disabled" @click="editing = true">看情况</button>
      <button type="button" :disabled="busy || disabled" @click="save('rejected')">不像我</button>
      <button type="button" :disabled="busy || disabled" @click="save('observing')">再观察看看</button>
    </div>
    <button v-if="!editing" class="reflection-text-button" type="button" :disabled="busy || disabled" @click="editing = true">{{ reviewed ? '修改我的反馈' : '也可以直接说说，哪里需要补充或修正' }}</button>
    <div v-if="editing" class="reflection-editor">
      <label :for="`reflection-note-${reflection.id}`">什么情况下适用？或者，你想怎么修正？</label>
      <textarea :id="`reflection-note-${reflection.id}`" v-model="note" rows="3" maxlength="1000" :disabled="busy || disabled" placeholder="例如：主要针对新合作伙伴，熟悉的人不用反复确认。" />
      <div class="reflection-actions"><button type="button" :disabled="busy || disabled || !note.trim()" @click="save('contextual')">保存我的补充</button><button type="button" :disabled="busy" @click="editing = false">先不改</button></div>
    </div>
    <p v-if="notice" role="status" class="reflection-notice">{{ notice }}</p>
    <p v-if="error" role="alert" class="reflection-error">{{ error }}</p>
    <details v-if="reflection.history.length" class="reflection-history"><summary>理解是怎样变化的</summary><ol><li v-for="(event, i) in reflection.history" :key="i">{{ event.at.slice(0, 10) }} · {{ reflectionLabels[event.action] }}<p v-if="event.note">{{ event.note }}</p></li></ol></details>
    <footer><RouterLink to="/reflections">回看照见</RouterLink><button type="button" :disabled="busy || disabled" @click="save('retired')">撤回这条照见</button></footer>
  </article>
</template>

<style scoped>
.reflection-card { max-width:760px; min-width:0; margin:18px 0; padding:24px; border:1px solid #dcded4; border-radius:16px; background:#fafaf6; color:#333e35; line-height:1.75; overflow-wrap:anywhere; }
header { display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; font-size:12px; color:#72796d; }
.reflection-eyebrow { color:#52634e; letter-spacing:.08em; }
h3 { margin:14px 0 10px; font-size:19px; font-weight:550; }
.reflection-observation { font-size:15px; margin:0 0 10px; white-space:pre-wrap; }
.reflection-hint, .reflection-history { font-size:12px; color:#73796e; }
.reflection-evidence { margin:18px 0; border-top:1px solid #e5e7de; padding-top:14px; font-size:13px; }
summary { cursor:pointer; }
ol { padding-left:22px; } li { padding:8px 0; } time { font-size:12px; color:#7a8075; }
blockquote { margin:6px 0; white-space:pre-wrap; } a { color:#536a4c; text-decoration:underline; text-underline-offset:3px; }
.reflection-actions { display:flex; gap:8px; flex-wrap:wrap; margin:14px 0 10px; }
button { font:inherit; cursor:pointer; color:#52634e; border:1px solid #d9decf; border-radius:8px; padding:7px 13px; background:#fff; }
button:hover:not(:disabled) { background:#edf1e7; } button:disabled { opacity:.5; cursor:default; }
.reflection-text-button { background:transparent; padding:4px 0; border:0; font-size:12px; text-align:left; }
.reflection-editor { margin-top:12px; } label { display:block; font-size:13px; margin-bottom:8px; }
textarea { width:100%; box-sizing:border-box; resize:vertical; font:inherit; font-size:14px; border:1px solid #cdd4c5; border-radius:8px; padding:10px; background:#fff; color:inherit; }
.reflection-correction { border-left:3px solid #9baa8b; padding:8px 14px; margin:16px 0; background:#f0f3e9; font-size:13px; } .reflection-correction p { margin:4px 0; white-space:pre-wrap; }
.reflection-notice { font-size:13px; color:#52634e; } .reflection-error { font-size:13px; color:#a33f2d; }
footer { display:flex; justify-content:space-between; gap:12px; margin-top:18px; font-size:12px; } footer button { border:0; background:transparent; padding:0; color:#7c8076; }
@media(max-width:480px) { .reflection-card { padding:18px; } h3 { font-size:18px; } }
</style>
