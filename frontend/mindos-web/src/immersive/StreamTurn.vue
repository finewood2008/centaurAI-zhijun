<script setup lang="ts">
// 流里的一条消息（嵌入模式）：气泡 + 呼吸知印 + 一行状态字 + 记印 / 留印；细节都收在印后面，点开才见。
// 这里没有轮次逻辑：发送、流式、核对、撤回全部由 ConversationPage 经事件处理。
// paced 时正文经 usePacedReply 按段落浮现（只揭示边界已到的段；历史消息与减少动效直通）。
import { computed, ref, watch } from 'vue'
import { usePacedReply } from './composables/usePacedReply'
import type { Claim, ConversationOutcomes, ReviewAction } from '@/services/api'
import type { ReplyAssistanceInput } from '@/shared/replyAssistance'
import type { MemoryPlacement } from '@/shared/memoryAttention'
import { contextNeedsReview } from '@/shared/contextRecovery'
import { hasConversationOutcomes } from '@/shared/labels'
import MessageBubble from '@/components/conversation/MessageBubble.vue'
import ImportBatchCard from '@/components/conversation/ImportBatchCard.vue'
import ProvenanceStrip from '@/components/conversation/ProvenanceStrip.vue'
import ClaimCandidateChip from '@/components/conversation/ClaimCandidateChip.vue'
import AlignmentCard from '@/components/ontology/AlignmentCard.vue'
import OutcomesCard from '@/components/conversation/OutcomesCard.vue'
import ReplyAssistance from '@/components/conversation/ReplyAssistance.vue'
import SealMark from './SealMark.vue'
import MicroSeal from './MicroSeal.vue'
import StatusLine from './StatusLine.vue'
import type { StreamMessage } from './streamTypes'
import type { ChatImportsModel } from './activeTurn'

const props = withDefaults(defineProps<{
  message: StreamMessage
  placement?: MemoryPlacement | null
  reviewBusy?: Record<string, boolean>
  imports?: ChatImportsModel | null
  outcomes?: ConversationOutcomes | null
  isLastAssistant?: boolean
  paced?: boolean
  highlighted?: boolean
  allowSave?: boolean
  charterAttention?: boolean
  streaming?: boolean
  assistanceReady?: boolean
  conversationId?: string | null
  showChannel?: boolean
}>(), {
  placement: null,
  reviewBusy: () => ({}),
  imports: null,
  outcomes: null,
  isLastAssistant: false,
  paced: false,
  highlighted: false,
  allowSave: false,
  charterAttention: false,
  streaming: false,
  assistanceReady: false,
  conversationId: null,
  showChannel: false,
})

const emit = defineEmits<{
  (e: 'cite', index: number): void
  (e: 'save'): void
  (e: 'review', claim: Claim, action: ReviewAction, editedContent?: string): void
  (e: 'dismiss', claimId: string): void
  (e: 'retry', localOnly: boolean): void
  (e: 'checkSaved'): void
  (e: 'askFiles', prompt: string): void
  (e: 'insertReply', text: string, origin: ReplyAssistanceInput): void
  (e: 'write'): void
  (e: 'alignmentUpdated', claim: Claim): void
  (e: 'alignmentRefreshed', claim: Claim): void
  (e: 'refreshOutcomes'): void
}>()

const isAssistant = computed(() => props.message.role === 'assistant')
const bubbleRole = computed(() => props.message.role === 'system' && props.message.meta?.kind === 'review_open' ? 'assistant' : props.message.role)
// 分段浮现：paced 时读揭示到的部分（流结束后 ≤900ms 追平）；否则原样透传
const { revealed } = usePacedReply(
  () => ({ id: props.message.id, content: props.message.content, streaming: props.message.streaming, status: props.message.status }),
  () => props.paced && isAssistant.value,
)
const content = computed(() => (props.paced && isAssistant.value ? revealed.value : props.message.content))
const initiated = computed(() => isAssistant.value && props.message.meta?.kind === 'zhijun_initiated'
  ? { whyNow: typeof props.message.meta?.whyNow === 'string' ? props.message.meta.whyNow : '' } : null)
const failed = computed(() => isAssistant.value && !props.message.streaming && ['error', 'aborted'].includes(props.message.status))
const assistedNote = computed(() => {
  if (props.message.role !== 'user' || !props.message.meta?.replyAssistance) return ''
  const kind = (props.message.meta.replyAssistance as { kind?: string }).kind
  return kind === 'assisted' ? '由 AI 候选辅助起草，你已发送' : '对话操作'
})
const memoryHere = computed(() => !props.charterAttention && !props.message.streaming && props.placement && props.placement.messageId === props.message.id ? props.placement : null)
const outcomesHere = computed(() => props.outcomes && hasConversationOutcomes(props.outcomes) ? props.outcomes : null)
const batches = computed(() => props.imports?.batches.filter(b => b.messageId === props.message.id) ?? [])
const batchBusy = (id: string) => !!props.imports && (props.imports.busyBatch === id || props.imports.ragBusyBatch === id || props.imports.uploading)
const statusValue = computed(() => props.message.streaming ? 'streaming' as const : props.message.status)

const provOpen = ref(false)
const memoryOpen = ref(false)
const outcomesOpen = ref(false)
watch(() => memoryHere.value?.claim.id, () => { memoryOpen.value = false })
</script>

<template>
  <div
    class="zj-stream-turn"
    :class="[`zj-stream-turn--${message.role}`, { 'zj-turn--search-hit': highlighted }]"
    :data-message-id="message.id"
    :aria-label="highlighted ? '搜索命中消息' : undefined"
  >
    <div v-if="isAssistant" class="zj-stream-turn__seal"><SealMark :breathing="!!message.streaming" /></div>
    <div class="zj-stream-turn__body">
      <MessageBubble
        :role="bubbleRole"
        :content="content"
        :status="message.status"
        :pending-label="contextNeedsReview(message) ? '等待核对' : undefined"
        :streaming="message.streaming"
        :allow-save="allowSave && isAssistant"
        :initiated="initiated"
        @cite="n => emit('cite', n)"
        @save="emit('save')"
      />
      <template v-if="imports">
        <ImportBatchCard
          v-for="batch in batches" :key="batch.id"
          conversation :batch="batch" :busy="batchBusy(batch.id)"
          @preview="imports.showPreview($event)" @retry="imports.retry(batch, $event)"
          @consent="imports.showConsent($event)" @reference="imports.chooseReferences($event)"
          @rag="imports.confirmSensitive($event)"
          @reupload="(item, file) => imports?.reupload(batch, item, file)"
        />
      </template>
      <div v-if="message.meta?.importId && isAssistant" class="zj-stream-turn__followups">
        <span>对这批文件的反馈</span>
        <div>
          <button type="button" :disabled="streaming" @click="emit('askFiles', '请继续总结这些文件的重点')">内容总结</button>
          <button type="button" :disabled="streaming" @click="emit('askFiles', '请指出这些文件中的潜在问题，并给出依据')">潜在问题</button>
          <button type="button" :disabled="streaming" @click="emit('askFiles', '这些文件与我已经确认的信息有什么联系？请区分依据与推测')">联系已有资料</button>
        </div>
      </div>
      <p v-if="assistedNote" class="zj-stream-turn__note">{{ assistedNote }}</p>
      <div v-if="failed" class="zj-stream-turn__followups">
        <span>{{ message.replySyncFailed ? '回复同步未完成，请先核对已保存结果；不会自动重新发送。' : contextNeedsReview(message) ? '补充信息需要核对；原消息已保留，不会重新发送一条。' : '暂时无法回答，请重试。原消息已保留。' }}</span>
        <div>
          <button v-if="message.replySyncFailed" type="button" :disabled="streaming || message.replySyncing" @click="emit('checkSaved')">{{ message.replySyncing ? '正在核对…' : '核对已保存回复' }}</button>
          <button type="button" :disabled="streaming" @click="emit('retry', false)">{{ contextNeedsReview(message) ? '核对补充资料并继续' : '重试' }}</button>
        </div>
      </div>
      <p v-if="message.replySyncing && message.streaming" class="zj-stream-turn__note" role="status">连接读取中断，正在核对盒子已保存的回复…</p>
      <p v-if="isAssistant && message.extractionNote" class="zj-stream-turn__note" data-testid="extraction-note">{{ message.extractionNote }}</p>

      <div v-if="isAssistant && !message.streaming && !failed" class="zj-stream-turn__status">
        <StatusLine :provenance="message.provenance" :meta="message.turnMeta" :status="statusValue" :initiated="!!initiated" :channel="showChannel" :open="provOpen" @toggle="provOpen = !provOpen" />
        <MicroSeal v-if="memoryHere" kind="记" label="知君记下的理解，点开核对" lit :pressed="memoryOpen" @toggle="memoryOpen = !memoryOpen" />
        <MicroSeal v-if="outcomesHere" kind="留" label="这段对话留下的" :pressed="outcomesOpen" @toggle="outcomesOpen = !outcomesOpen" />
      </div>
      <ProvenanceStrip v-if="provOpen && message.provenance" conversation variant="faint" :provenance="message.provenance" :meta="message.turnMeta" />
      <template v-if="memoryOpen && memoryHere">
        <AlignmentCard v-if="memoryHere.kind === 'alignment'" :key="memoryHere.claim.id" :claim="memoryHere.claim" :conversation-id="conversationId || undefined" :message-id="message.id"
          @updated="c => emit('alignmentUpdated', c)" @refreshed="c => emit('alignmentRefreshed', c)" />
        <ClaimCandidateChip v-else :key="memoryHere.claim.id" :claim="memoryHere.claim" :busy="!!reviewBusy[memoryHere.claim.id]" dismissible
          @review="(action, edited) => memoryHere && emit('review', memoryHere.claim, action, edited)"
          @dismiss="memoryHere && emit('dismiss', memoryHere.claim.id)" />
      </template>
      <OutcomesCard v-if="outcomesOpen && outcomesHere" :outcomes="outcomesHere" :conversation-id="conversationId || undefined" @refresh="emit('refreshOutcomes')" />
      <ReplyAssistance v-if="isLastAssistant && assistanceReady && conversationId" :conversation-id="conversationId" :message-id="message.id" :disabled="streaming"
        @insert="(text, origin) => emit('insertReply', text, origin)" @write="emit('write')" />
    </div>
  </div>
</template>

<style scoped>
.zj-stream-turn { display: flex; gap: 10px; margin: 0 0 18px; }
.zj-stream-turn--user { justify-content: flex-end; }
.zj-stream-turn--system { justify-content: center; }
.zj-stream-turn__seal { flex: none; padding-top: 12px; }
.zj-stream-turn__body { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.zj-stream-turn--user .zj-stream-turn__body { flex: 0 1 auto; max-width: 82%; align-items: flex-end; }
.zj-stream-turn--system .zj-stream-turn__body { flex: 0 1 auto; }
.zj-stream-turn__status { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 4px 0 0 2px; }
.zj-stream-turn__note { max-width: 760px; margin: 4px 0 0 4px; font-size: 12px; color: var(--ws-text-placeholder-color, #a3a69f); }
.zj-stream-turn__followups { margin: 9px 0; display: grid; gap: 8px; font-size: 12px; color: var(--ws-text-secondary-color, #686b66); }
.zj-stream-turn__followups > div { display: flex; flex-wrap: wrap; gap: 8px; }
.zj-stream-turn__followups button { font: inherit; padding: 5px 10px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 14px; background: var(--ws-card-bg, #fff); color: var(--ws-primary-color, #a6452e); cursor: pointer; }
.zj-stream-turn__followups button:hover:not(:disabled) { background: var(--ws-surface-2, #fbf8f1); }
.zj-stream-turn__followups button:disabled { opacity: 0.5; cursor: default; }
.zj-turn--search-hit { background: var(--ws-surface-2, #fbf8f1); outline: 2px solid var(--ws-primary-color, #a6452e); outline-offset: 3px; border-radius: 8px; }
/* 知君的气泡没有头像、没有卡框：只有一枚印和正文 */
.zj-stream-turn :deep(.zj-msg__who) { display: none; }
.zj-stream-turn :deep(.zj-msg--assistant) { max-width: none; padding: 6px 0 6px 2px; border-color: transparent; background: transparent; }
.zj-stream-turn :deep(.zj-msg__body) { font-size: 16px; line-height: 1.7; }
.zj-stream-turn :deep(.zj-msg__meta:not(:has(> :not(.zj-msg__who)))) { display: none; }
.zj-stream-turn :deep(.zj-prov) { margin-left: 2px; }
.zj-stream-turn > .zj-stream-turn__body > * + * { margin-top: 6px; }
@media (max-width: 767px) {
  .zj-stream-turn--user .zj-stream-turn__body { max-width: 92%; }
}
</style>
