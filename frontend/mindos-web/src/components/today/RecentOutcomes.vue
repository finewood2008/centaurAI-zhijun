<script setup lang="ts">
// 「最近留下的」：最近三段有产出的会话，每条一行：模式方印 · 标题 · 产出一行 · 相对时间。
import type { Conversation } from '@/services/api'
import { conversationModeLabel, outcomesLine, relativeTime } from '@/shared/labels'

defineProps<{ items: Conversation[] }>()
defineEmits<{ (e: 'open', id: string): void }>()

function titleOf(c: Conversation): string {
  return c.title || (c.mode === 'onboarding' ? '第一次对话' : '未命名对话')
}
</script>

<template>
  <section v-if="items.length" class="zj-today-section" data-testid="today-recent" aria-label="最近留下的">
    <h2 class="zj-today-section__title">最近留下的</h2>
    <ul class="zj-recent">
      <li v-for="c in items" :key="c.id">
        <button type="button" class="zj-recent__row" @click="$emit('open', c.id)">
          <span class="zj-seal zj-seal--muted zj-recent__seal">{{ conversationModeLabel(c.mode, !!c.outcomes?.decision) }}</span>
          <span class="zj-recent__title">{{ titleOf(c) }}</span>
          <span class="zj-recent__outcomes">{{ outcomesLine(c.outcomes) }}</span>
          <span class="zj-recent__time">{{ relativeTime(c.lastMessageAt || c.updatedAt) }}</span>
        </button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.zj-recent {
  display: grid;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.zj-recent__row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.6;
  text-align: left;
  cursor: pointer;
}
.zj-recent__row:hover {
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-recent__seal {
  flex: none;
}
.zj-recent__title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-recent__outcomes,
.zj-recent__time {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
  white-space: nowrap;
}
.zj-recent__time {
  color: var(--ws-text-placeholder-color, #a3a69f);
}
@media (max-width: 767px) {
  .zj-recent__row {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .zj-recent__outcomes,
  .zj-recent__time {
    grid-column: 2;
  }
}
</style>
