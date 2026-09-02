<script setup lang="ts">
// 今日：打开应用的首屏。安静，无红点无计数徽章；没有内容的区块整块不渲染。
// 自上而下：问候一行 → 知君想跟你聊的（提醒）→ 下一步 → 最近留下的 → 带一件事来。
// 还没建档时只剩问候 + 「先让我认识真实的你」；有没聊完的建档会话则在起手卡上方给「继续建档」。
// 数据一次 Promise.allSettled 并行拉取，谁失败只影响谁的那块；拉完之前显示灰块骨架。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  api,
  getOntologyStats,
  listClaims,
  listConversations,
  type Claim,
  type Conversation,
  type GrowthToday,
  type OntologyStats,
} from '@/services/api'
import {
  ONBOARDING_TOTAL_TURNS,
  buildNextSteps,
  greetingLine,
  isDueByToday,
  nicknameFromClaims,
  onboardingUserTurns,
  recentOutcomeConversations,
  todaySummaryLine,
} from '@/shared/labels'
import GreetingLine from '@/components/today/GreetingLine.vue'
import FirstMeetCard from '@/components/today/FirstMeetCard.vue'
import TodayNudges from '@/components/today/TodayNudges.vue'
import RecentOutcomes from '@/components/today/RecentOutcomes.vue'
import BringSomething from '@/components/today/BringSomething.vue'
import NextStepsPanel from '@/components/conversation/NextStepsPanel.vue'

const router = useRouter()

const loading = ref(true)
let alive = true

// 各块数据：请求失败保持 null / 空，绝不把 hasOntology 当真
const stats = ref<OntologyStats | null>(null)
const conversations = ref<Conversation[]>([])
const growthToday = ref<GrowthToday | null>(null)
const claims = ref<Claim[]>([])

// 建档的七个问题（与对话页一致）：用来算「还差 N 问」
const ONBOARDING_QUESTIONS = 7

const nickname = computed(() => nicknameFromClaims(claims.value))
const nudgeCount = ref(0)
const greeting = computed(() => greetingLine(nickname.value))

const dueCommitments = computed(() => claims.value.filter((c) => c.predicate === 'committed_to' && isDueByToday(c.validTo)))

const summary = computed(() =>
  todaySummaryLine({
    dueReview: growthToday.value?.dueDecisions?.length ?? 0,
    inbox: stats.value?.inbox ?? 0,
    dueCommitments: dueCommitments.value.length,
    pendingReviews: growthToday.value?.pendingReviews?.length ?? 0,
    nudges: nudgeCount.value,
  }),
)

const nextSteps = computed(() => {
  const g = growthToday.value
  return buildNextSteps({
    overdue: (g?.dueDecisions ?? []).filter((d) => d.dueState === 'overdue').map((d) => ({ id: d.id, title: d.title })),
    pendingReviews: (g?.pendingReviews ?? []).map((d) => ({ id: d.id, title: d.title })),
    dueCommitments: dueCommitments.value.map((c) => ({ id: c.id, content: c.content })),
    inbox: stats.value?.inbox,
  })
})

const recent = computed(() => recentOutcomeConversations(conversations.value, 3))

// 建档状态：不存在 mode=onboarding 的会话，且（stats 未知 或 hasOntology 为假）→ 只剩「先认识你」
const onboardingConversation = computed(() => conversations.value.find((c) => c.mode === 'onboarding') ?? null)
const showFirstMeet = computed(() => !loading.value && !onboardingConversation.value && (stats.value === null || !stats.value.hasOntology))
// 没聊完的建档会话（用户轮数 < 8）：「继续建档 · 还差 N 问」
const pendingOnboarding = computed(() => {
  const conv = onboardingConversation.value
  if (!conv) return null
  const turns = onboardingUserTurns(conv.messageCount)
  if (turns >= ONBOARDING_TOTAL_TURNS) return null
  return { id: conv.id, remaining: ONBOARDING_QUESTIONS - Math.max(0, turns - 1) }
})

function goChat(say: string, deliberate = false) {
  router.push({ path: '/chat', query: deliberate ? { say, deliberate: '1' } : { say } })
}
function openConversation(id: string) {
  router.push(`/c/${encodeURIComponent(id)}`)
}
function startOnboarding() {
  router.push({ path: '/chat', query: { onboarding: '1' } })
}

async function loadAll() {
  const [s, c, g, cl] = await Promise.allSettled([
    getOntologyStats(),
    listConversations(50),
    api.getGrowthToday(),
    listClaims({ trust: ['confirmed', 'working'], limit: 500 }),
  ])
  if (!alive) return
  stats.value = s.status === 'fulfilled' ? s.value : null
  conversations.value = c.status === 'fulfilled' ? c.value.items : []
  growthToday.value = g.status === 'fulfilled' ? g.value : null
  claims.value = cl.status === 'fulfilled' ? cl.value.items : []
  loading.value = false
}

onMounted(() => {
  void loadAll()
})
onBeforeUnmount(() => {
  alive = false
})
</script>

<template>
  <div class="zj-today">
    <GreetingLine :line="greeting" :summary="summary" :loading="loading" />

    <div v-if="loading" class="zj-today__skeleton" aria-hidden="true">
      <span class="zj-today__block" />
      <span class="zj-today__block is-short" />
      <span class="zj-today__block" />
    </div>

    <FirstMeetCard v-else-if="showFirstMeet" @start="startOnboarding" />

    <template v-else>
      <TodayNudges  @count="(n) => (nudgeCount = n)" />

      <section v-if="nextSteps.length" class="zj-today-section zj-today__next" aria-label="下一步">
        <NextStepsPanel :items="nextSteps" @say="(t) => goChat(t)" />
      </section>

      <RecentOutcomes :items="recent" @open="openConversation" />

      <BringSomething :pending-onboarding="pendingOnboarding" @pick="goChat" @resume="openConversation" />
    </template>
  </div>
</template>

<style>
/* 今日页各区块共用的标题（子组件里也用，故不加 scoped）：小字、字距略宽、灰色，不做徽章 */
.zj-today-section__title {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.06em;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
</style>

<style scoped>
.zj-today {
  display: flex;
  flex-direction: column;
  gap: 28px;
  max-width: 760px;
  margin: 0 auto;
  padding: 8px 0 32px;
}
/* NextStepsPanel 自带标题与上边距；这里把它的上边距抵掉，和其它区块一样对齐 */
.zj-today__next :deep(.zj-next) {
  margin-top: 0;
}
.zj-today__next :deep(.zj-next__title) {
  margin-bottom: 8px;
}
.zj-today__skeleton {
  display: grid;
  gap: 10px;
}
.zj-today__block {
  display: block;
  height: 56px;
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-border-color-4, #f1eee6);
}
.zj-today__block.is-short {
  height: 40px;
  width: 70%;
}
</style>
