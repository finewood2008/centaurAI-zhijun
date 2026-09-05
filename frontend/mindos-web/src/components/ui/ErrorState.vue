<script setup lang="ts">
// 错误状态：请求失败时给出可执行的重试入口。
import { AlertTriangle } from 'lucide-vue-next'
import { computed, watch } from 'vue'
import { backendConnection, backendNoticeActive, isNetworkError, markBackendDisconnected } from '@/shared/backendConnection'

const props = withDefaults(
  defineProps<{
    message?: string
    retryLabel?: string
    showRetry?: boolean
    recoverOnReconnect?: boolean
  }>(),
  {
    message: '加载失败，请稍后重试',
    retryLabel: '重试',
    showRetry: true,
    recoverOnReconnect: false,
  },
)

const emit = defineEmits<{ (e: 'retry'): void }>()
const networkError = computed(() => isNetworkError(props.message))
const hideNetworkError = computed(() => networkError.value && backendNoticeActive.value)
const displayMessage = computed(() => networkError.value
  ? backendConnection.value === 'connected' ? '连接已恢复，可以重新打开这部分内容。' : '暂时无法连接知君，请稍后重试。'
  : props.message)
watch(() => props.message, message => { if (isNetworkError(message)) markBackendDisconnected() }, { immediate: true })
watch(backendConnection, (state, previous) => {
  // Opt-in for read-only reloads; generic retry handlers may submit or refresh.
  if (state === 'connected' && previous === 'disconnected' && props.recoverOnReconnect && networkError.value) emit('retry')
})
</script>

<template>
  <div v-if="!hideNetworkError" class="ws-error" role="alert">
    <span class="ws-error__icon" aria-hidden="true">
      <AlertTriangle :size="26" />
    </span>
    <div class="ws-error__message">{{ displayMessage }}</div>
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
  color: var(--ws-danger-color, #a6452e);
}

.ws-error__message {
  font-size: 13px;
  color: var(--ws-text-color, #3c403d);
  max-width: 480px;
  line-height: 1.6;
}

.ws-error__retry {
  padding: 6px 14px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-body-bg, #fff);
  color: var(--ws-primary-color, #a6452e);
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s;
}
.ws-error__retry:hover {
  border-color: var(--ws-primary-color, #a6452e);
}
</style>
