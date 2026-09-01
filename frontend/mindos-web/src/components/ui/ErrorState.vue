<script setup lang="ts">
// 错误状态：请求失败时给出可执行的重试入口。
import { AlertTriangle } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    message?: string
    retryLabel?: string
    showRetry?: boolean
  }>(),
  {
    message: '加载失败，请稍后重试',
    retryLabel: '重试',
    showRetry: true,
  },
)

const emit = defineEmits<{ (e: 'retry'): void }>()
</script>

<template>
  <div class="ws-error">
    <span class="ws-error__icon" aria-hidden="true">
      <AlertTriangle :size="26" />
    </span>
    <div class="ws-error__message">{{ message }}</div>
    <button
      v-if="showRetry"
      type="button"
      class="ws-error__retry"
      @click="emit('retry')"
    >
      {{ retryLabel }}
    </button>
  </div>
</template>

<style scoped>
.ws-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 40px 20px;
  text-align: center;
}

.ws-error__icon {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ws-danger-color, #ff4918);
}

.ws-error__message {
  font-size: 13px;
  color: var(--ws-text-color, #606266);
  max-width: 480px;
  line-height: 1.6;
}

.ws-error__retry {
  padding: 6px 14px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-body-bg, #fff);
  color: var(--ws-primary-color, #0077ff);
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s;
}
.ws-error__retry:hover {
  border-color: var(--ws-primary-color, #0077ff);
}
</style>
