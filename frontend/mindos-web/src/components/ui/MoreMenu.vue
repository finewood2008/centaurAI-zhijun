<script setup lang="ts">
// 「···」菜单：把不常用的动作收起来，每个动作带一句解释。点外面 / Esc 关闭。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { MoreHorizontal } from 'lucide-vue-next'

export interface MoreItem {
  action: string
  label: string
  hint?: string
  danger?: boolean
}

const props = defineProps<{
  items: ReadonlyArray<MoreItem>
  disabled?: boolean
  label?: string
}>()

const emit = defineEmits<{ (e: 'select', action: string): void }>()

const open = ref(false)
const root = ref<HTMLElement | null>(null)

function choose(action: string) {
  open.value = false
  emit('select', action)
}

function onDocClick(e: MouseEvent) {
  if (!open.value) return
  if (root.value && !root.value.contains(e.target as Node)) open.value = false
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="root" class="zj-more">
    <button
      type="button"
      class="zj-more__btn"
      :aria-label="label || '更多'"
      :title="label || '更多'"
      aria-haspopup="menu"
      :aria-expanded="open"
      :disabled="disabled"
      @click.stop="open = !open"
    >
      <MoreHorizontal :size="16" aria-hidden="true" />
    </button>
    <div v-if="open" class="zj-more__menu" role="menu">
      <button
        v-for="it in props.items"
        :key="it.action"
        type="button"
        role="menuitem"
        class="zj-more__item"
        :class="{ 'is-danger': it.danger }"
        @click.stop="choose(it.action)"
      >
        <span class="zj-more__label">{{ it.label }}</span>
        <span v-if="it.hint" class="zj-more__hint">{{ it.hint }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.zj-more {
  position: relative;
  display: inline-flex;
}
.zj-more__btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 26px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
}
.zj-more__btn:hover:not(:disabled) {
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-primary-color, #a6452e);
}
.zj-more__btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.zj-more__menu {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  z-index: 30;
  min-width: 200px;
  padding: 4px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  box-shadow: var(--ws-shadow, 0 6px 24px rgba(29, 33, 31, 0.1));
}
.zj-more__item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  width: 100%;
  padding: 7px 10px;
  border: none;
  border-radius: var(--ws-radius, 6px);
  background: transparent;
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}
.zj-more__item:hover {
  background: var(--ws-surface-2, #fbf8f1);
}
.zj-more__item.is-danger .zj-more__label {
  color: var(--ws-danger-color, #a6452e);
}
.zj-more__label {
  font-size: 13px;
  font-weight: 500;
}
.zj-more__hint {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
</style>
