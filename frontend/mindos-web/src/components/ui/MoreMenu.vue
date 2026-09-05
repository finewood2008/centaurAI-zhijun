<script setup lang="ts">
// 「···」菜单：把不常用的动作收起来，每个动作带一句解释。点外面 / Esc 关闭。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MoreHorizontal } from 'lucide-vue-next'
import { menuPlacement } from '@/shared/menuPlacement'

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
  boundary?: 'scroll-container' | 'viewport'
}>()

const emit = defineEmits<{ (e: 'select', action: string): void }>()

const open = ref(false)
const trigger = ref<HTMLButtonElement | null>(null)
const menu = ref<HTMLElement | null>(null)

function close(restoreFocus = false) {
  if (menu.value?.matches(':popover-open')) menu.value.hidePopover()
  open.value = false
  if (restoreFocus) trigger.value?.focus({ preventScroll: true })
}

function toggle() {
  if (open.value) { close(true); return }
  const button = trigger.value
  const popup = menu.value
  if (!button || !popup || props.disabled) return

  // A native popover escapes scroll clipping and also works inside a dialog drawer.
  const bounds = { left: 8, top: 8, right: window.innerWidth - 8, bottom: window.innerHeight - 8 }
  for (let parent = props.boundary === 'viewport' ? null : button.parentElement; parent; parent = parent.parentElement) {
    const css = getComputedStyle(parent)
    const rect = parent.getBoundingClientRect()
    if (/(auto|scroll|hidden|clip)/.test(css.overflowX)) {
      bounds.left = Math.max(bounds.left, rect.left + 4)
      bounds.right = Math.min(bounds.right, rect.right - 4)
    }
    if (/(auto|scroll|hidden|clip)/.test(css.overflowY)) {
      bounds.top = Math.max(bounds.top, rect.top + 4)
      bounds.bottom = Math.min(bounds.bottom, rect.bottom - 4)
    }
  }
  popup.style.maxWidth = `${Math.max(0, bounds.right - bounds.left)}px`
  popup.style.maxHeight = 'none'
  popup.showPopover()
  const position = menuPlacement(button.getBoundingClientRect(), popup.getBoundingClientRect(), bounds)
  popup.style.left = `${position.left}px`
  popup.style.top = `${position.top}px`
  popup.style.maxHeight = `${position.maxHeight}px`
  open.value = true
  popup.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus({ preventScroll: true })
}

function choose(action: string) {
  close(true)
  emit('select', action)
}

function onToggle() {
  open.value = !!menu.value?.matches(':popover-open')
}
function onKey(e: KeyboardEvent) {
  if (!open.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    close(true)
  } else if (e.key === 'Tab') {
    close(true)
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
    e.preventDefault()
    const items = Array.from(menu.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
    if (!items.length) return
    const index = items.indexOf(document.activeElement as HTMLButtonElement)
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1
      : (index + (e.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length
    items[next]?.focus({ preventScroll: true })
  }
}
function onViewportChange(e: Event) {
  // Scrolling the menu itself is allowed; scrolling its anchor dismisses it.
  if (open.value && !(e.target instanceof Node && menu.value?.contains(e.target))) close()
}

watch(() => props.disabled, disabled => { if (disabled) close() })
onMounted(() => {
  document.addEventListener('scroll', onViewportChange, true)
  window.addEventListener('resize', onViewportChange)
})
onBeforeUnmount(() => {
  close()
  document.removeEventListener('scroll', onViewportChange, true)
  window.removeEventListener('resize', onViewportChange)
})
</script>

<template>
  <div class="zj-more">
    <button
      ref="trigger"
      type="button"
      class="zj-more__btn"
      :aria-label="label || '更多'"
      :title="label || '更多'"
      aria-haspopup="menu"
      :aria-expanded="open"
      :disabled="disabled"
      @click.stop="toggle"
      @keydown.down.prevent="!open && toggle()"
    >
      <MoreHorizontal :size="16" aria-hidden="true" />
    </button>
    <div ref="menu" popover="auto" class="zj-more__menu" role="menu" :aria-label="label || '更多'" @toggle="onToggle" @keydown="onKey">
      <button
        v-for="it in props.items"
        :key="it.action"
        type="button"
        role="menuitem"
        tabindex="-1"
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
  position: fixed;
  inset: auto;
  margin: 0;
  box-sizing: border-box;
  width: 224px;
  overflow-y: auto;
  overscroll-behavior: contain;
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
.zj-more__item:focus-visible {
  outline: 1px solid var(--ws-primary-color, #a6452e);
  outline-offset: -1px;
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
