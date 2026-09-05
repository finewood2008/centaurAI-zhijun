<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { calibrateAlignment, getClaim, requestAlignmentProposal, revokeAlignmentConsent, type Claim, type AlignmentFraming } from '@/services/api'
import { ALIGNMENT_LEVELS, ALIGNMENT_FRAMES, alignmentLabel, alignmentFrame } from '@/shared/alignment'

const props = defineProps<{ claim: Claim; conversationId?: string; messageId?: string }>()
const emit = defineEmits<{ updated: [claim: Claim]; refreshed: [claim: Claim] }>()
const opened = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const level = ref<number | null>(null)
const framing = ref<AlignmentFraming>('long_term')
const note = ref('')
const feedback = ref('')
const a = computed(() => props.claim.selfAlignment)
const proposal = computed(() => a.value?.proposal)
let attempt = { payload: '', id: '' }
watch(() => `${props.claim.id}:${a.value?.revision}`, () => {
  level.value = proposal.value?.level ?? a.value?.level ?? null
  framing.value = proposal.value?.framing ?? a.value?.framing ?? 'long_term'
  if (props.claim.scope === 'context_only') framing.value = 'context_only'
  else if (props.claim.layer === 'aspirational' && framing.value === 'long_term') framing.value = 'aspirational'
  note.value = a.value?.reason ?? ''
  if (proposal.value) opened.value = true
}, { immediate: true })

async function save(action: 'calibrate' | 'defer' | 'clear') {
  if (busy.value || !a.value) return
  busy.value = true; error.value = ''; notice.value = ''
  const data = { action, level: level.value, framing: framing.value, note: note.value,
    conversationId: props.conversationId, proposalId: proposal.value?.id }
  const signature = JSON.stringify({ ...data, revision: a.value.revision })
  if (attempt.payload !== signature) attempt = { payload: signature, id: crypto.randomUUID() }
  try {
    const claim = await calibrateAlignment(props.claim, { ...data, requestId: attempt.id })
    emit('updated', claim)
    notice.value = action === 'defer' ? '先不判断；没有新证据不会重复追问。' : action === 'clear' ? '已恢复为尚未校准，事实记录保留。' : '已保存你的校准，事实记录仍然保留。'
    opened.value = false
  } catch (err) {
    error.value = err instanceof Error ? err.message : '校准未保存，请重试'
    try { emit('refreshed', await getClaim(props.claim.id)) } catch { /* keep last visible claim */ }
  } finally { busy.value = false }
}

async function propose() {
  if (!props.conversationId || !props.messageId || busy.value) return
  busy.value = true; error.value = ''
  try {
    await requestAlignmentProposal(props.claim.id, props.conversationId, props.messageId, feedback.value)
    notice.value = '正在本机整理修正提议，尚未改变正式等级；你可以继续聊天。'
    feedback.value = ''
  } catch (err) { error.value = err instanceof Error ? err.message : '暂时无法生成提议' }
  finally { busy.value = false }
}

async function revoke() {
  busy.value = true; error.value = ''
  try { await revokeAlignmentConsent(props.claim.id); notice.value = '已撤销这条画像的所有外部服务授权。' }
  catch (err) { error.value = err instanceof Error ? err.message : '撤销失败' }
  finally { busy.value = false }
}
</script>

<template>
  <section class="zj-alignment" data-testid="alignment-card" :aria-busy="busy">
    <header><strong>自我贴合度</strong><span>{{ alignmentLabel(claim) }}</span></header>
    <p v-if="conversationId" class="zj-alignment__claim">{{ claim.content }}</p>
    <p class="zj-alignment__hint">这条理解有多能代表你？不是在判断记录真假。</p>
    <p v-if="a?.level != null" class="zj-alignment__hint">{{ alignmentFrame(claim) }}</p>
    <p v-if="proposal" class="zj-alignment__proposal">知君的提议：{{ proposal.reason }}<br>建议「{{ ALIGNMENT_LEVELS[proposal.level] }}」，等你校准后才生效。</p>
    <button v-if="!opened && claim.trustState === 'confirmed'" type="button" @click="opened = true">{{ a?.level == null ? '校准一下' : '修改我的校准' }}</button>
    <div v-if="opened && claim.trustState === 'confirmed'" class="zj-alignment__editor">
      <fieldset :disabled="busy"><legend>{{ framing === 'context_only' ? '那次情境下，有多代表你的想法？' : '它有多能代表现在的你？' }}</legend>
        <label v-for="(label, index) in ALIGNMENT_LEVELS" :key="index" :class="{ selected: level === index }">
          <input v-model="level" type="radio" :name="`alignment-${claim.id}`" :value="index">{{ label }}
        </label>
      </fieldset>
      <label class="zj-alignment__field">适用范围
        <select v-model="framing" :disabled="busy || claim.scope === 'context_only'">
          <option v-for="frame in ALIGNMENT_FRAMES" :key="frame.value" :value="frame.value" :disabled="claim.layer === 'aspirational' && frame.value === 'long_term'">{{ frame.label }}</option>
        </select>
      </label>
      <label class="zj-alignment__field">你的说明（可选）<textarea v-model="note" rows="2" maxlength="500" :disabled="busy" placeholder="例如：这是工作安排，不是我的个人追求。" /></label>
      <p class="zj-alignment__preview">确认后：{{ level == null ? '请先选择一档' : ALIGNMENT_LEVELS[level] }} · {{ ALIGNMENT_FRAMES.find(f => f.value === framing)?.label }}</p>
      <div class="zj-alignment__actions">
        <button type="button" :disabled="busy || level == null" @click="save('calibrate')">确认保存校准</button>
        <button type="button" :disabled="busy" @click="save('defer')">先不判断</button>
        <button v-if="a?.level != null" type="button" :disabled="busy" @click="save('clear')">恢复待校准</button>
      </div>
      <details v-if="conversationId && messageId"><summary>用自己的话修正，让知君整理</summary>
        <label class="zj-alignment__field">想怎么修正？<textarea v-model="feedback" rows="2" maxlength="1000" placeholder="只是这次这样，不代表我一直这样想。" /></label>
        <button type="button" :disabled="busy || !feedback.trim()" @click="propose">在本机生成待确认提议</button>
      </details>
    </div>
    <details><summary>依据与校准记录</summary>
      <p v-if="a?.reason">你的说明：{{ a.reason }}</p>
      <ul><li v-for="e in claim.evidence.filter(e => (proposal?.evidenceIds ?? a?.evidenceIds ?? []).includes(e.id))" :key="e.id">
        「{{ e.quote }}」
        <RouterLink v-if="e.conversationId" :to="`/c/${encodeURIComponent(e.conversationId)}`">查看对话</RouterLink>
        <RouterLink v-else-if="e.materialId" :to="`/materials/${encodeURIComponent(e.materialId)}`">查看资料</RouterLink>
      </li></ul>
      <p v-if="!a?.history?.length">还没有校准记录。模型提议不会替你确认。</p>
      <ol><li v-for="(event, i) in [...(a?.history ?? [])].reverse()" :key="i">
        {{ event.at.slice(0, 10) }} · {{ event.actor === 'user' ? '你的校准' : '知君提议（未自动生效）' }}
        · {{ event.level == null ? '尚未校准' : ALIGNMENT_LEVELS[event.level] }}<span v-if="event.note">：{{ event.note }}</span>
      </li></ol>
      <button type="button" :disabled="busy" @click="revoke">撤销此画像的外部授权</button>
    </details>
    <p v-if="error" role="alert" class="zj-alignment__error">{{ error }}</p>
    <p v-if="notice" role="status" class="zj-alignment__hint">{{ notice }}</p>
  </section>
</template>

<style scoped>
.zj-alignment { margin: 12px 0; padding: 14px; border: 1px solid #ddd6c8; border-radius: 10px; background: #fbf8f1; color: #343932; font-size: 13px; line-height: 1.65; max-width: 760px; overflow-wrap: anywhere; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
header span { color: #93422e; }
.zj-alignment__hint { color: #6c6e65; margin: 5px 0; font-size: 12px; }
.zj-alignment__claim { font-size: 15px; }
.zj-alignment__proposal { padding-left: 10px; border-left: 2px solid #a6452e; }
fieldset { border: none; margin: 10px 0; padding: 0; display: flex; flex-wrap: wrap; gap: 7px; }
legend { margin-bottom: 6px; }
fieldset label { padding: 6px 9px; border: 1px solid #ddd6c8; border-radius: 16px; cursor: pointer; }
fieldset label.selected { background: #f2e4d9; border-color: #a6452e; }
input { margin-right: 4px; }
.zj-alignment__field { display: grid; gap: 5px; margin: 10px 0; }
textarea, select { box-sizing: border-box; width: 100%; padding: 8px; border: 1px solid #d9d3c6; border-radius: 6px; background: #fffdf9; font: inherit; color: inherit; }
button { font: inherit; padding: 6px 10px; border: 1px solid #d9d3c6; border-radius: 6px; background: #fffdf9; color: #94432f; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
.zj-alignment__actions { display: flex; flex-wrap: wrap; gap: 7px; }
details { margin-top: 10px; }
summary { cursor: pointer; color: #746356; }
ul, ol { padding-left: 20px; }
.zj-alignment__error { color: #a12e21; }
.zj-alignment__preview { padding: 8px; background: #f0eee5; }
</style>
