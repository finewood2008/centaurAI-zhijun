<script setup lang="ts">
// 本体分区导航：六个抽屉 + 「知君最近学到的」（待确认收件箱）。
import type { OntologyStats, Section } from '@/services/api'
import { SECTIONS } from '@/shared/ontology'

export type NavKey = Section | 'inbox' | 'proposals'

defineProps<{
  stats: OntologyStats | null
  current: NavKey
}>()

const emit = defineEmits<{ (e: 'select', key: NavKey): void }>()
</script>

<template>
  <nav class="zj-secnav" aria-label="本体分区">
    <button
      type="button"
      class="zj-secnav__item zj-secnav__item--inbox"
      :class="{ 'is-active': current === 'inbox' }"
      :aria-current="current === 'inbox' ? 'page' : undefined"
      @click="emit('select', 'inbox')"
    >
      <span class="zj-secnav__label">知君最近学到的</span>
      <span class="zj-secnav__count" aria-label="待确认数量">{{ stats?.inbox ?? 0 }}</span>
    </button>
    <button
      v-if="current === 'proposals' || (stats?.proposals ?? 0) > 0"
      type="button"
      class="zj-secnav__item zj-secnav__item--proposals"
      :class="{ 'is-active': current === 'proposals' }"
      :aria-current="current === 'proposals' ? 'page' : undefined"
      @click="emit('select', 'proposals')"
    >
      <span class="zj-secnav__label">需要你裁决</span>
      <span class="zj-secnav__hint">同一个人？两条矛盾？</span>
      <span class="zj-secnav__count" aria-label="待裁决数量">{{ stats?.proposals ?? 0 }}</span>
    </button>
    <button
      v-for="s in SECTIONS"
      :key="s.key"
      type="button"
      class="zj-secnav__item"
      :class="{ 'is-active': current === s.key }"
      :aria-current="current === s.key ? 'page' : undefined"
      @click="emit('select', s.key)"
    >
      <span class="zj-secnav__label">{{ s.label }}</span>
      <span class="zj-secnav__hint">{{ s.hint }}</span>
      <span class="zj-secnav__count">
        {{ stats?.bySection?.[s.key]?.confirmed ?? 0 }}<span v-if="stats?.bySection?.[s.key]?.working" class="zj-secnav__pending" :title="`${stats.bySection[s.key].working} 条等你点头`">+{{ stats.bySection[s.key].working }}</span>
      </span>
    </button>
  </nav>
</template>

<style scoped>
.zj-secnav {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.zj-secnav__item {
  display: grid;
  grid-template-columns: 1fr auto;
  grid-template-areas:
    'label count'
    'hint hint';
  gap: 2px 8px;
  padding: 10px 12px;
  border: 1px solid transparent;
  border-radius: var(--ws-radius, 6px);
  background: transparent;
  text-align: left;
  font-family: inherit;
  cursor: pointer;
  color: var(--ws-text-color, #3c403d);
}
.zj-secnav__item:hover {
  background: var(--ws-surface-2, #fbf8f1);
}
.zj-secnav__pending {
  margin-left: 4px;
  color: var(--ws-primary-color, #a6452e);
}
.zj-secnav__item.is-active {
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  border-color: var(--ws-border-color-2, #e2ded4);
  color: var(--ws-primary-color, #a6452e);
}
.zj-secnav__item--inbox {
  border-style: dashed;
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-secnav__item--proposals {
  margin-bottom: 8px;
  border-style: dashed;
  border-color: var(--ws-border-color, #d8d3c8);
}
.zj-secnav__label {
  grid-area: label;
  font-family: var(--ws-font-display, serif);
  font-size: 15px;
  font-weight: 600;
}
.zj-secnav__hint {
  grid-area: hint;
  font-size: 12px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-secnav__count {
  grid-area: count;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
  white-space: nowrap;
}
@media (max-width: 767px) {
  .zj-secnav {
    flex-direction: row;
    flex-wrap: wrap;
  }
  .zj-secnav__item {
    flex: 1 1 45%;
  }
  .zj-secnav__hint {
    display: none;
  }
}
</style>
