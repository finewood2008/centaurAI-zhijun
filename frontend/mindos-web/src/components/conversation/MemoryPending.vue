<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { dismissConversationMemoryPending, getConversationMemoryPending, reviewClaim, type Claim, type ConversationMemoryPending, type ReviewAction } from '@/services/api'
import ClaimCandidateChip from '@/components/conversation/ClaimCandidateChip.vue'
import SideDrawer from '@/components/ui/SideDrawer.vue'

const props = defineProps<{ conversationId: string; pendingCount?: number }>()
const emit = defineEmits<{ (e: 'changed'): void }>()
const open = ref(false)
const loading = ref(false)
const error = ref('')
const items = ref<ConversationMemoryPending['items']>([])
const total = ref<number | null>(null)
const busyId = ref<string | null>(null)
let sequence = 0
let alive = true
async function load() {
  const cid = props.conversationId, ticket = ++sequence
  loading.value = true; error.value = ''
  try {
    const result = await getConversationMemoryPending(cid)
    if (!alive || cid !== props.conversationId || ticket !== sequence) return
    items.value = result.items; total.value = result.total
  } catch (e) {
    if (alive && cid === props.conversationId && ticket === sequence) error.value = e instanceof Error ? e.message : '暂时无法读取待核对内容'
  } finally {
    if (alive && cid === props.conversationId && ticket === sequence) loading.value = false
  }
}
function show() { open.value = true; void load() }
watch(() => props.conversationId, () => {
  sequence++; open.value = false; items.value = []; total.value = null; error.value = ''; loading.value = false; busyId.value = null
})
watch(() => props.pendingCount, () => { total.value = null; if (open.value && !busyId.value) void load() })
onBeforeUnmount(() => { alive = false; sequence++ })
async function review(claim: Claim, action: ReviewAction | 'dismiss', editedContent?: string) {
  if (busyId.value) return
  const cid = props.conversationId
  sequence++; loading.value = false; busyId.value = claim.id; error.value = ''
  try {
    if (action === 'dismiss') await dismissConversationMemoryPending(cid, claim.id)
    else await reviewClaim(claim.id, { action, editedContent, surface: 'conversation', conversationId: cid })
    if (!alive || cid !== props.conversationId) return
    // Reviewing the optional queue does not create messages or move the reading position.
    items.value = items.value.filter(item => item.claim.id !== claim.id)
    total.value = Math.max(0, (total.value ?? props.pendingCount ?? items.value.length + 1) - 1)
    emit('changed')
    await load()
  } catch (e) {
    if (alive && cid === props.conversationId) error.value = e instanceof Error ? e.message : '未保存，候选和原对话仍然保留'
  } finally {
    if (alive && cid === props.conversationId) busyId.value = null
  }
}
</script>
<template>
  <button class="memory-pending-entry" data-testid="memory-pending-entry" aria-haspopup="dialog" :aria-expanded="open" @click="show">待核对<span v-if="(total ?? pendingCount ?? 0) > 0"> {{ total ?? pendingCount }}</span></button>
  <SideDrawer :open="open" title="这段对话的待核对理解" @close="open = false">
    <div class="memory-pending" :aria-busy="loading || !!busyId">
      <p>这里汇总这段对话各话题中尚未确认的理解。你决定哪些值得留下，不必逐条完成；它们不会因你打开列表就自动确认。</p>
      <p v-if="loading" role="status">正在读取…</p>
      <p v-if="error" role="alert">{{ error }} <button :disabled="!!busyId" @click="load">重新读取</button></p>
      <p v-else-if="!loading && !items.length">暂时没有待核对理解。若后台个人理解暂停，可在「模型与授权」核对并继续。</p>
      <div v-for="item in items" :key="item.claim.id" class="memory-pending-item">
        <ClaimCandidateChip :claim="item.claim" :busy="!!busyId" dismissible @review="(action, text) => review(item.claim, action, text)" @dismiss="review(item.claim, 'dismiss')" />
      </div>
      <p class="memory-pending-foot">选择「不用记住」只撤下这条候选，原对话仍保留，也不表示你否认了事实。</p>
    </div>
  </SideDrawer>
</template>
<style scoped>
.memory-pending-entry { color:var(--ws-primary-color,#a6452e); background:transparent; border:1px solid var(--ws-border-color-3,#ebe7de); border-radius:20px; padding:6px 9px; font:inherit; font-size:12px; white-space:nowrap; cursor:pointer; }
.memory-pending { font-size:14px; line-height:1.75; }.memory-pending>p { color:var(--ws-text-secondary-color,#686b66); margin:0 0 18px; }.memory-pending-item { margin-bottom:14px; }.memory-pending .memory-pending-foot { font-size:12px; margin-top:20px; }.memory-pending button { font:inherit; color:inherit; cursor:pointer; }
</style>
