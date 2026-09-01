<script setup lang="ts">
// 基础按钮：primary / secondary / text / danger / success，支持 loading、disabled。
import { computed } from 'vue'
import { Loader2 } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'text' | 'danger' | 'success'
    size?: 'sm' | 'md'
    loading?: boolean
    disabled?: boolean
    type?: 'button' | 'submit'
    block?: boolean
  }>(),
  {
    variant: 'secondary',
    size: 'md',
    loading: false,
    disabled: false,
    type: 'button',
    block: false,
  },
)

const emit = defineEmits<{ (e: 'click', ev: MouseEvent): void }>()

const rootClass = computed(() => [
  'ws-btn',
  `ws-btn--${props.variant}`,
  `ws-btn--${props.size}`,
  {
    'is-loading': props.loading,
    'is-disabled': props.disabled || props.loading,
    'ws-btn--block': props.block,
  },
])

function onClick(ev: MouseEvent) {
  if (props.disabled || props.loading) return
  emit('click', ev)
}
</script>

<template>
  <button
    :type="type"
    class="ws-btn"
    :class="rootClass"
    :disabled="disabled || loading"
    :aria-busy="loading"
    @click="onClick"
  >
    <Loader2
      v-if="loading"
      class="ws-btn__spinner"
      :size="size === 'sm' ? 14 : 16"
      aria-hidden="true"
    />
    <span class="ws-btn__content"><slot /></span>
  </button>
</template>

<style scoped>
.ws-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border-radius: var(--ws-radius, 6px);
  font-family: inherit;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition:
    background 0.15s,
    border-color 0.15s,
    color 0.15s,
    opacity 0.15s;
}

.ws-btn--md {
  height: 36px;
  padding: 0 16px;
  font-size: 13px;
}

.ws-btn--sm {
  height: 30px;
  padding: 0 12px;
  font-size: 12px;
}

.ws-btn--block {
  width: 100%;
}

.ws-btn__content {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.ws-btn__spinner {
  animation: ws-btn-spin 0.8s linear infinite;
}

@keyframes ws-btn-spin {
  to {
    transform: rotate(360deg);
  }
}

/* primary：主操作 */
.ws-btn--primary {
  background: var(--ws-button-primary-background, #0077ff);
  border: 1px solid transparent;
  color: var(--ws-button-color, #fff);
}
.ws-btn--primary:hover:not(.is-disabled) {
  background: var(--ws-button-hover-primary-background, #298dff);
}
.ws-btn--primary:active:not(.is-disabled) {
  background: var(--ws-button-active-primary-background, #006ae5);
}

/* secondary：常规操作 */
.ws-btn--secondary {
  background: var(--ws-body-bg, #fff);
  border: 1px solid var(--ws-button-plain-border-color, #dcdfe6);
  color: var(--ws-button-plain-color, #606266);
}
.ws-btn--secondary:hover:not(.is-disabled) {
  border-color: var(--ws-button-plain-hover-border-color, #298dff);
  color: var(--ws-button-plain-hover-border-color, #298dff);
}
.ws-btn--secondary:active:not(.is-disabled) {
  border-color: var(--ws-button-plain-active-border-color, #006ae5);
  color: var(--ws-button-plain-active-border-color, #006ae5);
}

/* text：文字链接型 */
.ws-btn--text {
  background: transparent;
  border: 1px solid transparent;
  color: var(--ws-button-text-color, #1b99ff);
}
.ws-btn--text:hover:not(.is-disabled) {
  color: var(--ws-button-text-hover-color, #298cff);
}

/* danger：危险操作 */
.ws-btn--danger {
  background: var(--ws-button-danger-background, #ff4918);
  border: 1px solid transparent;
  color: var(--ws-button-color, #fff);
}
.ws-btn--danger:hover:not(.is-disabled) {
  background: var(--ws-button-hover-danger-background, #ff551c);
}
.ws-btn--danger:active:not(.is-disabled) {
  background: var(--ws-button-active-danger-background, #ff4216);
}

/* success：成功/正向操作 */
.ws-btn--success {
  background: var(--ws-button-success-background, #12cd3d);
  border: 1px solid transparent;
  color: var(--ws-button-color, #fff);
}
.ws-btn--success:hover:not(.is-disabled) {
  background: var(--ws-button-hover-success-background, #15d547);
}

.ws-btn.is-disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
</style>
