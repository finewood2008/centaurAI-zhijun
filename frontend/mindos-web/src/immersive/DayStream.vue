<script setup lang="ts">
// 流的滚动容器：按天分节，今天在最下；上滑到顶（scrollTop < 400）再读更早的一页：
// 插入前记下 scrollHeight，nextTick 后补偿 scrollTop；浏览器自带的 overflow-anchor 兜底。
// #active 槽是当前块（嵌入的 ConversationPage），落在当前会话创建的那一天的末尾，之后是流内的判断草稿卡。
// 壳经 defineExpose 的 el 拿到滚动容器（scroller()）。
import { nextTick, ref } from 'vue'
import { useRouter } from 'vue-router'
import ConversationBlock from './ConversationBlock.vue'
import DaySection from './DaySection.vue'
import DraftCard from './DraftCard.vue'
import { useDayStream } from './composables/useDayStream'

const props = withDefaults(defineProps<{ muted?: boolean }>(), { muted: false })

const router = useRouter()
const el = ref<HTMLElement | null>(null)
const stream = useDayStream(() => el.value)
const { days, letter, letterRefreshing, loadingOlder, hasMore, lonely, highlightedId, now, activeDayKey, replyingTo, empty, error } = stream

const LOAD_THRESHOLD_PX = 400
let loadingByScroll = false

async function loadOlderAnchored() {
  const node = el.value
  if (!node || loadingByScroll || !hasMore.value) return
  loadingByScroll = true
  const heightBefore = node.scrollHeight
  const topBefore = node.scrollTop
  try {
    const added = await stream.loadOlder()
    if (!added) return
    await nextTick()
    const expected = topBefore + (node.scrollHeight - heightBefore)
    // 浏览器已按 overflow-anchor 补偿过就不再动；没有（Safari）才手动补
    if (Math.abs(node.scrollTop - expected) > 1) node.scrollTop = expected
  } finally {
    loadingByScroll = false
  }
}

function onScroll() {
  const node = el.value
  if (!node || props.muted || node.scrollTop >= LOAD_THRESHOLD_PX) return
  void loadOlderAnchored()
}

function backToToday() {
  void router.push('/chat')
}

defineExpose({ el, revealConversation: stream.revealConversation, refreshList: stream.refreshList })
</script>

<template>
  <div
    ref="el"
    class="zj-stream zj-daystream"
    role="log"
    aria-label="对话"
    :aria-hidden="muted || undefined"
    data-testid="day-stream"
    @scroll.passive="onScroll"
  >
    <div class="zj-stream__inner">
      <p v-if="loadingOlder" class="zj-daystream__more" role="status">在翻更早的日子…</p>
      <p v-else-if="hasMore" class="zj-daystream__more"><button type="button" @click="loadOlderAnchored">更早的日子</button></p>

      <section v-if="lonely" class="zj-day zj-day--lonely" aria-label="更早的日子">
        <h2 class="zj-day__label" data-testid="day-label"><span>……更早的日子……</span></h2>
        <ConversationBlock :conversation="lonely" :highlighted="highlightedId === lonely.id" :now="now" />
      </section>

      <DaySection
        v-for="day in days"
        :key="day.key"
        :day="day"
        :letter="day.isToday ? letter : null"
        :letter-refreshing="letterRefreshing"
        :highlighted-id="highlightedId"
        :has-active="day.key === activeDayKey"
        :now="now"
      >
        <template #active>
          <div class="zj-daystream__active" aria-live="polite"><slot name="active" /></div>
          <DraftCard />
        </template>
      </DaySection>

      <p v-if="empty" class="zj-daystream__empty" data-testid="stream-empty">从眼下在意的事聊起就好。</p>
      <p v-if="error && !days.some(day => day.items.length)" class="zj-daystream__note">{{ error }}</p>

      <div v-if="replyingTo" class="zj-daystream__replying" data-testid="replying-chip">
        <span>回复：对「{{ replyingTo.title }}」的{{ replyingTo.mode === 'review' ? '回访' : '对话' }}</span>
        <button type="button" @click="backToToday">回到今天</button>
      </div>
    </div>
  </div>
</template>
