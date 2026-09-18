<script setup lang="ts">
// 「昔」：时间线。上面是「我们一起走过」（关系时间线），下面是判断与回看的条目，按日期排列；点一条回到那天的对话。
// 到期的判断可以直接开一段回访会话。暂放 / 选入不在这里做，去完整回看页。
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  api, createConversation, getZhijunHome, listConversations,
  type Conversation, type GrowthDecision, type HomeSourceRef, type HomeTimelineEvent,
} from '@/services/api'
import { REVISIT_PROMPT, REVISIT_STORAGE_KEY, readRevisitPreferences, revisitEntries, revisitLabel, revisitReason, type RevisitEntry, type RevisitPreferences } from '@/shared/revisit'
import { createProductLocalStorage } from '@/shared/productScope'
import { formatDate } from '@/shared/format'
import SideDrawer from '@/components/ui/SideDrawer.vue'
import DecisionStepper from '@/components/growth/DecisionStepper.vue'
import RelationshipTimeline from '@/components/today/RelationshipTimeline.vue'
import { useToast } from '@/composables/useToast'
import { useActiveTurn } from '../activeTurn'
import { useDrawerLoad } from '../composables/useDrawerLoad'
import { useSheetPlacement } from '../composables/useSheetPlacement'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'reveal', conversationId: string): void }>()
const router = useRouter()
const toast = useToast()
const placement = useSheetPlacement()
const turn = useActiveTurn()

const decisions = ref<GrowthDecision[]>([])
const conversations = ref<Conversation[]>([])
const timeline = ref<HomeTimelineEvent[]>([])
const preferences = ref<RevisitPreferences>({ selected: {}, deferred: {} })
const error = ref('')
const opening = ref('')
let session = 0

function message(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

function readPreferences(): RevisitPreferences {
  try { return readRevisitPreferences(createProductLocalStorage().getItem(REVISIT_STORAGE_KEY)) } catch { return { selected: {}, deferred: {} } }
}

async function load() {
  const current = ++session
  error.value = ''
  const [decisionsRes, conversationsRes, homeRes] = await Promise.allSettled([
    api.listGrowthDecisions(),
    listConversations({ status: 'all', limit: 40 }),
    getZhijunHome(),
  ])
  if (current !== session) return
  if (decisionsRes.status === 'fulfilled') decisions.value = decisionsRes.value.items
  else error.value = message(decisionsRes.reason, '判断暂时读不到')
  if (conversationsRes.status === 'fulfilled') conversations.value = conversationsRes.value.items
  timeline.value = homeRes.status === 'fulfilled' ? homeRes.value.timeline ?? [] : []
  preferences.value = readPreferences()
}

const { loaded, loading, reload } = useDrawerLoad(() => props.open, load, () => turn.value?.lastTurnAt.value ?? null)

// 判断都列出来；聊过的经历只在有理由（选入 / 到期 / 有结果 / 已复盘）时出现；暂放的不在这里
const entries = computed(() => revisitEntries(decisions.value, conversations.value, preferences.value)
  .filter(e => !e.deferred && (e.kind === 'decision' || e.reason)))

function conversationOf(entry: RevisitEntry): string | null {
  if (entry.kind === 'conversation') return entry.conversation.id
  return entry.conversations.find(c => c.mode === 'review')?.id ?? entry.sourceIds[0] ?? entry.conversations[0]?.id ?? null
}

function reveal(entry: RevisitEntry) {
  const id = conversationOf(entry)
  if (!id) return
  emit('close')
  emit('reveal', id)
}

async function openReview(decision: GrowthDecision) {
  if (opening.value) return
  opening.value = decision.id
  try {
    const conv = await createConversation({ mode: 'review', decisionId: decision.id })
    emit('close')
    await router.push({ path: `/c/${encodeURIComponent(conv.id)}`, query: { say: REVISIT_PROMPT } })
  } catch (err) {
    toast({ type: 'error', message: message(err, '无法开始回访') })
  } finally {
    opening.value = ''
  }
}

function openSource(source: HomeSourceRef) {
  emit('close')
  if (source.sourceType === 'decision') void router.push({ path: '/review', query: { decisionId: source.id.replace(/^decision:/, '') } })
  else void router.push({ path: '/me', query: { claim: source.id.replace(/^claim:/, '') } })
}

function sealTone(entry: RevisitEntry): string {
  return entry.reason === 'due' ? 'zj-seal--accent' : entry.reason === 'reviewed' ? 'zj-seal--ink' : 'zj-seal--muted'
}
</script>

<template>
  <SideDrawer :open="open" title="昔 · 判断与回看" :placement="placement" @close="emit('close')">
    <div class="zj-drawer zj-past" data-testid="past-timeline">
      <p v-if="loading && !loaded" class="zj-drawer__quiet">正在翻找…</p>
      <p v-else-if="error && !entries.length" class="zj-drawer__quiet" role="status">{{ error }} <button type="button" class="zj-drawer__link" @click="reload">重试</button></p>
      <template v-else>
        <RelationshipTimeline :items="timeline" @open="openSource" />
        <p v-if="!entries.length" class="zj-drawer__quiet">还没有留下判断或回看。聊到拿主意的事时，它们会出现在这里。</p>
        <ol v-else class="zj-past__list" aria-label="判断与回看">
          <li v-for="entry in entries" :key="entry.key" class="zj-past__entry">
            <div class="zj-past__head">
              <strong class="zj-past__title">{{ entry.title }}</strong>
              <time class="zj-past__time" :datetime="entry.at">{{ formatDate(entry.at) }}</time>
            </div>
            <p class="zj-past__meta">
              <span class="zj-seal" :class="sealTone(entry)">{{ revisitLabel(entry) }}</span>
              <span class="zj-past__reason">{{ revisitReason(entry) }}</span>
            </p>
            <DecisionStepper v-if="entry.kind === 'decision'" :status="entry.decision.status" />
            <div class="zj-past__actions">
              <button v-if="entry.kind === 'decision' && entry.reason === 'due'" type="button" class="zj-drawer__link" :disabled="!!opening" @click="openReview(entry.decision)">{{ opening === entry.decision.id ? '正在打开…' : '开始回访' }}</button>
              <button v-if="conversationOf(entry)" type="button" class="zj-drawer__link" @click="reveal(entry)">回到那天</button>
            </div>
          </li>
        </ol>
      </template>
      <footer class="zj-drawer__foot">
        <RouterLink to="/review" class="zj-drawer__link" @click="emit('close')">完整回看 →</RouterLink>
      </footer>
    </div>
  </SideDrawer>
</template>
