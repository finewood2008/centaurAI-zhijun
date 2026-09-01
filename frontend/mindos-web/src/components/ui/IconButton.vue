<script setup lang="ts">
// 图标按钮：所有纯图标操作必须有 aria-label；悬停显示 Tooltip。
// 用法：<IconButton label="刷新" :loading="loading" @click="..."><RefreshCw :size="16" /></IconButton>
import { computed } from 'vue'
import { Loader2 } from 'lucide-vue-next'
import Tooltip from './Tooltip.vue'

const props = withDefaults(
  defineProps<{
    label: string
    disabled?: boolean
    loading?: boolean
    variant?: 'default' | 'primary' | 'danger'
    size?: 'sm' | 'md'
  }>(),
  {
    disabled: false,
    loading: false,
    variant: 'default',
    size: 'md',
  },
)

const emit = defineEmits<{ (e: 'click', ev: MouseEvent): void }>()

const rootClass = computed(() => [
  'ws-icon-btn',
  `ws-icon-btn--${props.variant}`,
  `ws-icon-btn--${props.size}`,
  { 'is-disabled': props.disabled || props.loading },
])

const isDisabled = computed(() => props.disabled || props.loading)

function onClick(ev: MouseEvent) {
  if (isDisabled.value) return
  emit('click', ev)
}
</script>

<template>
  <Tooltip :content="label">
    <button
      type="button"
      class="ws-icon-btn"
      :class="rootClass"
      :aria-label="label"
      :title="label"
      :disabled="isDisabled"
      @click="onClick"
    >
      <Loader2
        v-if="loading"
        class="ws-icon-btn__spinner"
        :size="size === 'sm' ? 14 : 16"
        aria-hidden="true"
      />
      <slot v-else />
    </button>
  </Tooltip>
</template>

<style scoped>
.ws-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: var(--ws-radius, 6px);
  background: transparent;
  color: var(--ws-text-secondary-color, #909399);
  cursor: pointer;
  transition:
    background 0.15s,
    color 0.15s,
    border-color 0.15s;
}

.ws-icon-btn--md {
  width: 32px;
  height: 32px;
}

.ws-icon-btn--sm {
  width: 26px;
  height: 26px;
}

.ws-icon-btn:hover:not(.is-disabled) {
  background: var(--ws-edit-color, rgba(0, 119, 255, 0.06));
  color: var(--ws-primary-color, #0077ff);
  border-color: var(--ws-border-color-2, #e4e7ed);
}

.ws-icon-btn--primary:not(.is-disabled) {
  color: var(--ws-primary-color, #0077ff);
}

.ws-icon-btn--danger:not(.is-disabled) {
  color: var(--ws-danger-color, #ff4918);
}
.ws-icon-btn--danger:hover:not(.is-disabled) {
  background: var(--ws-danger-color-bd, rgba(255, 73, 24, 0.06));
  border-color: transparent;
}

.ws-icon-btn.is-disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.ws-icon-btn__spinner {
  animation: ws-spin 0.8s linear infinite;
}

@keyframes ws-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
