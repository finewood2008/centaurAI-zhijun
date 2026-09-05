<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import SideDrawer from '@/components/ui/SideDrawer.vue'
import ClaimCard from '@/components/ontology/ClaimCard.vue'
import CharterWorkspaceEditor from './CharterWorkspaceEditor.vue'
import { reviewClaim, type Claim, type CharterWorkspace, type GrowthCharter, type ReviewAction } from '@/services/api'
import { routingRequest } from '@/services/taskRouting'
const props = defineProps<{ conversationId: string; onboarding: boolean; messageId?: string | null; disabled?: boolean; claims: Claim[]; requested?: boolean }>()
interface State { workspace?: CharterWorkspace | null; charter: GrowthCharter | null; topics: Array<{ id: string; label: string; state: string }>; pending?: unknown[]; generationState?: string }
const emit = defineEmits<{ finished: []; attention: [boolean]; reviewed: []; topics: [State['topics']] }>()
const router = useRouter()
const state = ref<State | null>(null)
const open = ref(false)
const busy = ref(false)
const error = ref('')
let timer: ReturnType<typeof setTimeout> | null = null
let controller: AbortController | null = null
let startRequest = crypto.randomUUID()
const path = computed(() => `/mindos/conversations/${encodeURIComponent(props.conversationId)}/charter`)
const workspace = computed(() => state.value?.workspace ?? null)
const visible = computed(() => props.onboarding || props.requested || !!workspace.value || open.value)
const pendingCount = computed(() => workspace.value?.suggestions.filter(s => s.status === 'pending').length ?? 0)
const relevantClaims = computed(() => props.claims.filter(c => c.trustState === 'working' && c.evidence.some(e => e.conversationId === props.conversationId)))
// A closed drawer never replaces the ordinary conversation attention card.
watch(() => open.value && props.onboarding, value => emit('attention', value), { immediate: true })
async function refresh() {
  const id = props.conversationId
  controller?.abort(); controller = new AbortController()
  try {
    const result = await routingRequest<State>(path.value, 'GET', undefined, controller.signal)
    if (id !== props.conversationId) return
    if (result.workspace && workspace.value?.id === result.workspace.id && workspace.value.revision > result.workspace.revision) result.workspace = workspace.value
    state.value = result; emit('topics', result.topics ?? [])
  } catch (e) { if (id === props.conversationId && !(e instanceof DOMException && e.name === 'AbortError')) error.value = e instanceof Error ? e.message : '工作稿暂时不可用，对话仍保留' }
}
function schedule(attempt = 0) {
  if (timer) clearTimeout(timer)
  // Poll only an explicitly active session, never revive a historical charter conversation.
  if (attempt >= 12 || workspace.value?.status !== 'active') return
  timer = setTimeout(async () => { await refresh(); schedule(attempt + 1) }, 5000)
}
watch(() => [props.conversationId, props.messageId], async ([id], old) => {
  if (id !== old?.[0]) { controller?.abort(); if (timer) clearTimeout(timer); open.value = false; state.value = null; error.value = ''; busy.value = false; startRequest = crypto.randomUUID() }
  await refresh(); schedule()
}, { immediate: true })
async function start() {
  if (busy.value) return
  busy.value = true; error.value = ''
  const cid = props.conversationId
  try {
    const result = await routingRequest<{ workspace: CharterWorkspace; conversationId: string }>(path.value + '/workspace/start', 'POST', { requestId: startRequest })
    if (cid !== props.conversationId) return
    startRequest = crypto.randomUUID()
    if (result.conversationId !== cid) { await router.push({ path: `/c/${result.conversationId}`, query: { charter: '1' } }); return }
    if (state.value) state.value.workspace = result.workspace
    schedule()
  } catch (e) { if (cid === props.conversationId) error.value = e instanceof Error ? e.message : '暂时无法开始整理' }
  finally { if (cid === props.conversationId) busy.value = false }
}
function updated(value: CharterWorkspace) {
  if (state.value && (!workspace.value || workspace.value.id !== value.id || value.revision >= workspace.value.revision)) state.value.workspace = value
  schedule()
}
function published(value: GrowthCharter) { if (state.value && (!state.value.charter || value.version >= state.value.charter.version)) state.value.charter = value }
async function review(id: string, action: ReviewAction, edited?: string) {
  try { await reviewClaim(id, { action, editedContent: edited, surface: props.onboarding ? 'onboarding' : 'conversation', conversationId: props.conversationId }); emit('reviewed') }
  catch (e) { error.value = e instanceof Error ? e.message : '核对失败' }
}
onBeforeUnmount(() => { controller?.abort(); if (timer) clearTimeout(timer) })
</script>
<template>
  <section v-if="visible" class="charter-chat" data-testid="charter-conversation">
    <button class="charter-chat__open" @click="open = true" aria-haspopup="dialog">{{ onboarding ? '第一次认识小结' : '查看章程草稿' }} <span>{{ pendingCount ? '有新的正文待核对' : workspace?.status === 'active' ? '尚未生效' : workspace?.status === 'published' ? '已确认，保持稳定' : '按需查看' }}</span></button>
    <button v-if="onboarding" class="charter-chat__link" :disabled="disabled" @click="emit('finished')">先使用，稍后核对</button>
    <SideDrawer :open="open" :title="onboarding ? '第一次认识小结' : '人生章程'" @close="open = false">
      <div class="charter-chat">
        <p v-if="onboarding">保存章程与确认本体是两件事，不必一起完成。</p>
        <p v-if="onboarding && state" class="charter-chat__muted">{{ state.topics.filter(t => t.state !== 'pending').map(t => `${t.label}${t.state === 'skipped' ? '（先跳过）' : ''}`).join(' · ') || '先聊一点，也可以直接开始使用。' }}</p>
        <section v-if="onboarding"><h3>关于我的初步理解</h3><p>分别核对，不会因发布章程而自动确认或提高贴合度。</p><ClaimCard v-for="claim in relevantClaims" :key="claim.id" :claim="claim" @review="(action, edited) => review(claim.id, action, edited)" /><p v-if="!relevantClaims.length" class="charter-chat__muted">暂时没有需要核对的新理解。</p></section>
        <section><p v-if="state?.charter">当前生效：第 {{ state.charter.version }} 版。</p><p><RouterLink to="/me/charter">查看完整章程与版本</RouterLink></p>
          <button v-if="!workspace || workspace.status !== 'active'" :disabled="busy || disabled" @click="start">{{ workspace?.status === 'paused' ? '继续这份工作稿' : state?.charter ? '主动开始修改章程' : '开始边聊整理章程' }}</button>
          <p v-if="!workspace" class="charter-chat__muted">查看小结不会启动整理。点击开始后，才会将这段对话整理为待核对的工作稿。</p>
          <p v-if="state?.pending?.length" class="charter-chat__muted">整理在等待本次用途授权，可在工作稿中主动整理并核对授权；不影响继续聊天。</p>
          <p v-else-if="state?.generationState === 'failed'" class="charter-chat__muted">本次整理未完成，对话仍保留。可以重试或直接自己填写。</p>
          <CharterWorkspaceEditor v-if="workspace" :key="workspace.id" :workspace="workspace" @updated="updated" @published="published" />
        </section>
        <p v-if="error" role="alert">{{ error }}</p>
        <button v-if="onboarding" :disabled="disabled" @click="open = false; emit('finished')">先使用，稍后核对</button>
      </div>
    </SideDrawer>
  </section>
</template>
<style scoped>
.charter-chat { max-width:760px; margin:12px 0; font-size:13px; line-height:1.75; }.charter-chat button { font:inherit; color:inherit; border:1px solid var(--ws-border-color); background:transparent; border-radius:10px; padding:7px 12px; cursor:pointer; text-align:left; }.charter-chat button:disabled { opacity:.5; cursor:default; }.charter-chat__open span { color:var(--ws-text-secondary-color); margin-left:8px; font-size:12px; }.charter-chat .charter-chat__link { border:0; color:var(--ws-primary-color); }.charter-chat section { margin:20px 0; }.charter-chat h3 { font-size:17px; margin:16px 0 8px; }.charter-chat__muted { color:var(--ws-text-secondary-color); font-size:12px; }.charter-chat a { color:var(--ws-primary-color); }
</style>
