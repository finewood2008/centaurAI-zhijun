<script setup lang="ts">
// 确认弹窗：明确受影响对象与后果；支持键盘 Escape 取消、取消/确认、焦点管理。
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import BaseButton from './BaseButton.vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    title?: string
    message?: string
    confirmText?: string
    cancelText?: string
    danger?: boolean
    loading?: boolean
  }>(),
  {
    title: '确认操作',
    message: '',
    confirmText: '确认',
    cancelText: '取消',
    danger: false,
    loading: false,
  },
)

const emit = defineEmits<{ (e: 'confirm'): void; (e: 'cancel'): void }>()

const panelRef = ref<HTMLElement | null>(null)
let lastFocused: HTMLElement | null = null

function focusConfirm() {
  const btn = panelRef.value?.querySelector<HTMLElement>('[data-confirm]')
  ;(btn ?? panelRef.value)?.focus?.()
}

function onKeydown(e: KeyboardEvent) {
  if (!props.open) return
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('cancel')
    return
  }
  // 简易焦点陷阱：Tab 在弹窗内循环
  if (e.key === 'Tab' && panelRef.value) {
    const focusables = Array.from(
      panelRef.value.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),
    ).filter((el) => !el.hasAttribute('disabled'))
    if (!focusables.length) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      lastFocused = document.activeElement as HTMLElement | null
      document.addEventListener('keydown', onKeydown)
      await nextTick()
      focusConfirm()
    } else {
      document.removeEventListener('keydown', onKeydown)
      lastFocused?.focus?.()
      lastFocused = null
    }
  },
)

onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Teleport to="body">
    <Transition name="ws-dialog">
      <div v-if="open" class="ws-dialog-mask" @click.self="emit('cancel')">
        <div
          ref="panelRef"
          class="ws-dialog"
          role="dialog"
          aria-modal="true"
          :aria-label="title"
        >
          <h3 class="ws-dialog__title">{{ title }}</h3>
          <div v-if="message" class="ws-dialog__message">{{ message }}</div>
          <div v-else-if="$slots.default" class="ws-dialog__body">
            <slot />
          </div>
          <div class="ws-dialog__actions">
            <BaseButton variant="secondary" :disabled="loading" @click="emit('cancel')">
              {{ cancelText }}
            </BaseButton>
            <BaseButton
              data-confirm
              :variant="danger ? 'danger' : 'primary'"
              :loading="loading"
              @click="emit('confirm')"
            >
              {{ confirmText }}
            </BaseButton>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.ws-dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 2500;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(29, 33, 31, 0.42);
}

.ws-dialog {
  width: 100%;
  max-width: 440px;
  padding: 20px;
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  box-shadow: var(--ws-shadow-lg, 0 16px 48px rgba(0, 0, 0, 0.18));
}

.ws-dialog__title {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}

.ws-dialog__message {
  font-size: 13px;
  line-height: 1.7;
  color: var(--ws-text-color, #3c403d);
  word-break: break-word;
}

.ws-dialog__body {
  font-size: 13px;
}

.ws-dialog__actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 20px;
}

.ws-dialog-enter-active,
.ws-dialog-leave-active {
  transition: opacity 0.2s ease;
}
.ws-dialog-enter-from,
.ws-dialog-leave-to {
  opacity: 0;
}
</style>
