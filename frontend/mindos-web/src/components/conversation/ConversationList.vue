<script setup lang="ts">
// 会话列表：新建 + 按最近活动排序的会话项；当前项高亮并标注 aria-current。
import { Plus, Trash2 } from 'lucide-vue-next'
import type { Conversation } from '@/services/api'
import { formatDate } from '@/shared/format'

defineProps<{
  items: Conversation[]
  currentId: string | null
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'create'): void
  (e: 'remove', id: string): void
}>()
</script>

<template>
  <nav class="zj-convs" aria-label="会话列表">
    <button type="button" class="zj-convs__new" @click="emit('create')">
      <Plus :size="15" aria-hidden="true" />新对话
    </button>
    <p v-if="loading" class="zj-convs__hint">正在加载…</p>
    <p v-else-if="!items.length" class="zj-convs__hint">还没有对话</p>
    <ul v-else class="zj-convs__list">
      <li v-for="c in items" :key="c.id">
        <div class="zj-convs__row" :class="{ 'is-active': c.id === currentId }">
          <button
            type="button"
            class="zj-convs__item"
            :aria-current="c.id === currentId ? 'true' : undefined"
            @click="emit('select', c.id)"
          >
            <span class="zj-convs__title">{{ c.title || (c.mode === 'onboarding' ? '第一次对话' : '未命名对话') }}</span>
            <span class="zj-convs__time">{{ formatDate(c.lastMessageAt || c.updatedAt) }}</span>
          </button>
          <button type="button" class="zj-convs__remove" :aria-label="`删除会话 ${c.title || ''}`" title="删除会话" @click="emit('remove', c.id)">
            <Trash2 :size="14" aria-hidden="true" />
          </button>
        </div>
      </li>
    </ul>
  </nav>
</template>

<style scoped>
.zj-convs {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}
.zj-convs__new {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 12px;
  border: 1px solid var(--ws-primary-color, #a6452e);
  border-radius: var(--ws-radius, 6px);
  background: transparent;
  color: var(--ws-primary-color, #a6452e);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.zj-convs__new:hover {
  background: var(--ws-bg, rgba(166, 69, 46, 0.05));
}
.zj-convs__hint {
  margin: 4px 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-convs__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
}
.zj-convs__row {
  display: flex;
  align-items: stretch;
  border-radius: var(--ws-radius, 6px);
}
.zj-convs__row.is-active {
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
}
.zj-convs__item {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  border: none;
  background: transparent;
  text-align: left;
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  cursor: pointer;
}
.zj-convs__item:hover {
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-convs__title {
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.is-active .zj-convs__title {
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}
.zj-convs__time {
  font-size: 11px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-convs__remove {
  display: inline-flex;
  align-items: center;
  padding: 0 8px;
  border: none;
  background: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
  cursor: pointer;
  opacity: 0;
}
.zj-convs__row:hover .zj-convs__remove,
.zj-convs__remove:focus-visible {
  opacity: 1;
}
.zj-convs__remove:hover {
  color: var(--ws-danger-color, #a6452e);
}
</style>
