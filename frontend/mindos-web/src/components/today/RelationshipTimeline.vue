<script setup lang="ts">
import { ChevronRight } from 'lucide-vue-next'
import type { HomeSourceRef, HomeTimelineEvent } from '@/services/api'
import { relativeTime } from '@/shared/labels'

defineProps<{ items: HomeTimelineEvent[] }>()
defineEmits<{ (e: 'open', ref: HomeSourceRef): void }>()

const kindMark: Record<HomeTimelineEvent['kind'], string> = {
  remembered: '记',
  decision: '判',
  outcome: '果',
  review: '省',
}
</script>

<template>
  <section v-if="items.length" class="zj-journey" aria-labelledby="journey-title">
    <header class="zj-journey__head">
      <div>
        <p>每一次认真聊过，都留下了位置</p>
        <h2 id="journey-title">我们一起走过</h2>
      </div>
      <span>最近 {{ items.length }} 个变化</span>
    </header>
    <ol class="zj-journey__list">
      <li v-for="item in items" :key="item.id">
        <button type="button" @click="$emit('open', item.sourceRef)">
          <span class="zj-journey__mark">{{ kindMark[item.kind] }}</span>
          <span class="zj-journey__copy">
            <strong>{{ item.title }}</strong>
            <span>{{ item.detail }}</span>
          </span>
          <time>{{ relativeTime(item.occurredAt) }}</time>
          <ChevronRight :size="15" aria-hidden="true" />
        </button>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.zj-journey {
  padding: 24px 26px;
  border-top: 1px solid var(--ws-border-color-3, #e8e2d7);
}
.zj-journey__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.zj-journey__head p { margin: 0 0 4px; color: var(--ws-text-secondary-color, #686b66); font-size: 11px; }
.zj-journey__head h2 { margin: 0; font-family: var(--ws-font-display, serif); font-size: 21px; color: var(--ws-text-primary-color, #1d211f); }
.zj-journey__head > span { color: var(--ws-text-placeholder-color, #92958f); font-size: 11px; }
.zj-journey__list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 24px; margin: 0; padding: 0; list-style: none; }
.zj-journey__list li { border-top: 1px solid var(--ws-border-color-3, #e8e2d7); }
.zj-journey__list button { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; align-items: center; gap: 10px; width: 100%; padding: 13px 2px; border: 0; background: transparent; color: inherit; font-family: inherit; text-align: left; cursor: pointer; }
.zj-journey__mark { display: grid; width: 26px; height: 26px; place-items: center; border: 1px solid rgba(166, 69, 46, 0.35); color: var(--ws-primary-color, #a6452e); font-family: var(--ws-font-display, serif); font-size: 12px; }
.zj-journey__copy { display: grid; min-width: 0; gap: 2px; }
.zj-journey__copy strong { color: var(--ws-text-primary-color, #1d211f); font-size: 12px; font-weight: 600; }
.zj-journey__copy > span { overflow: hidden; color: var(--ws-text-secondary-color, #686b66); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.zj-journey time { color: var(--ws-text-placeholder-color, #92958f); font-size: 10px; white-space: nowrap; }
.zj-journey button > svg { color: var(--ws-text-placeholder-color, #92958f); }
.zj-journey button:hover strong,
.zj-journey button:hover > svg { color: var(--ws-primary-color, #a6452e); }
.zj-journey button:focus-visible { outline: 2px solid var(--ws-primary-color, #a6452e); outline-offset: -2px; }
@media (max-width: 700px) {
  .zj-journey { padding: 22px 4px; }
  .zj-journey__list { grid-template-columns: 1fr; }
}
</style>
