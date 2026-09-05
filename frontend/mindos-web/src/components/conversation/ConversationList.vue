<script setup lang="ts">
// 会话列表：新建 + 按最近活动排序的会话项；当前项高亮并标注 aria-current。
// 每条带模式方印（建档 / 商量 / 回访 / 对话），下面一行灰字是这段对话留下了什么（全零不显示）。
import { Pin, Plus, Search, X } from 'lucide-vue-next'
import type { Conversation } from '@/services/api'
import MoreMenu from '@/components/ui/MoreMenu.vue'
import { conversationActions } from '@/shared/conversationManagement'
import { formatDate } from '@/shared/format'
import { conversationModeLabel, outcomesLine, stripLabels } from '@/shared/labels'

function sealText(c: Conversation): string {
  return conversationModeLabel(c.mode, !!c.outcomes?.decision)
}

withDefaults(defineProps<{
  items: Conversation[]
  currentId: string | null
  loading?: boolean
  loadingMore?: boolean
  total?: number
  hasMore?: boolean
  error?: string
  query?: string
  tab?: 'active' | 'archived'
  searchScope?: 'all' | 'active' | 'archived'
  busyIds?: Record<string, boolean>
  createLabel?: string
  allowRemove?: boolean
}>(), {
  createLabel: '新对话',
  allowRemove: true,
  query: '',
  tab: 'active',
  searchScope: 'all',
})

const emit = defineEmits<{
  (e: 'select', id: string, messageId?: string): void
  (e: 'create'): void
  (e: 'remove', id: string): void
  (e: 'manage', conversation: Conversation, action: string): void
  (e: 'query', value: string): void
  (e: 'tab', value: 'active' | 'archived'): void
  (e: 'scope', value: 'all' | 'active' | 'archived'): void
  (e: 'more'): void
  (e: 'retry'): void
}>()
</script>

<template>
  <nav class="zj-convs" aria-label="会话列表">
    <button type="button" class="zj-convs__new" @click="emit('create')">
      <Plus :size="15" aria-hidden="true" />{{ createLabel }}
    </button>
    <div class="zj-convs__search">
      <Search :size="14" aria-hidden="true" />
      <input :value="query" type="search" maxlength="100" aria-label="搜索对话标题和正文" placeholder="搜索对话" @input="emit('query', ($event.target as HTMLInputElement).value)">
      <button v-if="query" type="button" aria-label="清除搜索" @click="emit('query', '')"><X :size="14" aria-hidden="true" /></button>
    </div>
    <div v-if="query.trim()" class="zj-convs__tabs" role="group" aria-label="搜索范围">
      <button v-for="option in [{ value: 'all', label: '全部' }, { value: 'active', label: '最近' }, { value: 'archived', label: '已归档' }] as const" :key="option.value" type="button" :aria-pressed="searchScope === option.value" @click="emit('scope', option.value)">{{ option.label }}</button>
    </div>
    <div v-else class="zj-convs__tabs" role="group" aria-label="对话分组">
      <button type="button" :aria-pressed="tab === 'active'" @click="emit('tab', 'active')">最近</button>
      <button type="button" :aria-pressed="tab === 'archived'" @click="emit('tab', 'archived')">已归档</button>
    </div>
    <slot name="feedback" />
    <p v-if="error" class="zj-convs__hint" role="alert">{{ error }} <button type="button" class="zj-convs__text" @click="emit('retry')">重试</button></p>
    <p v-if="loading" class="zj-convs__hint">正在加载…</p>
    <p v-else-if="!items.length && !error" class="zj-convs__hint">{{ query.trim() ? '没有找到匹配的对话' : tab === 'archived' ? '还没有归档的对话' : '还没有对话' }}</p>
    <div v-if="!loading && items.length" class="zj-convs__results">
    <p class="zj-convs__hint">{{ query.trim() ? '找到' : '共' }} {{ total ?? items.length }} 段对话</p>
    <ul class="zj-convs__list">
      <li v-for="c in items" :key="c.id">
        <div class="zj-convs__row" :class="{ 'is-active': c.id === currentId }">
          <button
            type="button"
            class="zj-convs__item"
            :aria-current="c.id === currentId ? 'true' : undefined"
            @click="emit('select', c.id, c.searchMatch?.messageId || undefined)"
          >
            <span class="zj-convs__line">
              <span class="zj-seal zj-seal--muted zj-convs__seal">{{ sealText(c) }}</span>
              <span class="zj-convs__title">{{ c.title || (c.mode === 'onboarding' ? '第一次对话' : '未命名对话') }}</span>
            </span>
            <span v-if="c.pinnedAt || c.status === 'archived'" class="zj-convs__markers"><span v-if="c.pinnedAt"><Pin :size="11" aria-hidden="true" />置顶</span><span v-if="c.status === 'archived'">已归档</span></span>
            <span v-if="query.trim() && c.searchMatch" class="zj-convs__match">{{ c.searchMatch.field === 'message' ? '正文：' : '标题：' }}{{ stripLabels(c.searchMatch.snippet) }}</span>
            <span v-if="outcomesLine(c.outcomes)" class="zj-convs__outcomes">{{ outcomesLine(c.outcomes) }}</span>
            <span class="zj-convs__time">{{ formatDate(c.lastMessageAt || c.updatedAt) }}</span>
          </button>
          <MoreMenu class="zj-convs__more" boundary="viewport" :items="conversationActions(c, allowRemove)" :disabled="busyIds?.[c.id]" :label="`管理对话 ${c.title || '未命名对话'}`" @select="action => action === 'delete' ? emit('remove', c.id) : emit('manage', c, action)" />
        </div>
      </li>
    </ul>
    <button v-if="hasMore" type="button" class="zj-convs__load" :disabled="loadingMore" @click="emit('more')">{{ loadingMore ? '正在加载…' : '加载更多' }}</button>
    </div>
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
.zj-convs__search { display:flex; align-items:center; gap:6px; padding:7px 8px; border:1px solid var(--ws-border-color-3,#ebe7de); border-radius:6px; color:var(--ws-text-secondary-color,#686b66); }
.zj-convs__search input { min-width:0; width:100%; padding:0; border:0; outline:none; background:transparent; color:inherit; font:inherit; font-size:13px; }
.zj-convs__search:focus-within { border-color:var(--ws-primary-color,#a6452e); }
.zj-convs__search input::-webkit-search-cancel-button { display:none; }
.zj-convs__search button { display:grid; place-items:center; padding:0; border:0; background:none; color:inherit; cursor:pointer; }
.zj-convs__tabs { display:flex; gap:4px; }
.zj-convs__tabs button { flex:1; border:0; padding:6px 4px; border-radius:5px; background:transparent; color:var(--ws-text-secondary-color,#686b66); font:inherit; font-size:12px; cursor:pointer; }
.zj-convs__tabs button[aria-pressed=true] { color:var(--ws-primary-color,#a6452e); background:var(--ws-surface-2,#fbf8f1); }
.zj-convs__results { min-height:0; overflow-y:auto; }
.zj-convs__load, .zj-convs__text { border:0; background:transparent; color:var(--ws-primary-color,#a6452e); font:inherit; font-size:12px; cursor:pointer; }
.zj-convs__load { width:100%; padding:10px; }.zj-convs__load:disabled { opacity:.6; }
.zj-convs__markers { display:flex; gap:7px; font-size:11px; color:var(--ws-text-secondary-color,#686b66); }
.zj-convs__markers span { display:inline-flex; align-items:center; gap:3px; }
.zj-convs__match { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; overflow-wrap:anywhere; font-size:12px; line-height:1.5; color:var(--ws-text-secondary-color,#686b66); }
.zj-convs__more { align-self:flex-start; margin:7px 3px 0 0; flex:none; }
.zj-convs__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
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
.zj-convs__line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.zj-convs__seal {
  flex: none;
  font-size: 11px;
  line-height: 1.4;
}
.zj-convs__title {
  min-width: 0;
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zj-convs__outcomes {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.is-active .zj-convs__title {
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}
.zj-convs__time {
  font-size: 12px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
</style>
