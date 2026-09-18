<script setup lang="ts">
// 一天：日头、今天的来信（当天第一条知君消息）、按创建时间排列的历史块，最后是当前块（#active 槽）。
// 跨午夜的细分隔由块自己插；这里只管一天的顺序。
import type { DayGroup } from './dayStream'
import type { DayLetter } from './composables/useDayStream'
import ConversationBlock from './ConversationBlock.vue'
import LetterBubble from './LetterBubble.vue'

withDefaults(defineProps<{
  day: DayGroup
  letter?: DayLetter | null
  letterRefreshing?: boolean
  highlightedId?: string | null
  hasActive?: boolean
  now?: Date
}>(), { letter: null, letterRefreshing: false, highlightedId: null, hasActive: false, now: () => new Date() })
</script>

<template>
  <section class="zj-day" :class="{ 'zj-day--today': day.isToday }" :data-day="day.key" :aria-label="day.label">
    <h2 class="zj-day__label" data-testid="day-label"><span>{{ day.label }}</span></h2>
    <LetterBubble v-if="day.isToday && letter" :brief="letter.brief" :next-action="letter.nextAction" :refreshing="letterRefreshing" />
    <ConversationBlock v-for="conv in day.items" :key="conv.id" :conversation="conv" :highlighted="highlightedId === conv.id" :now="now" />
    <slot v-if="hasActive" name="active" />
  </section>
</template>
