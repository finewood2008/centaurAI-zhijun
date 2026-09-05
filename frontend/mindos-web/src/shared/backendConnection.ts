import { computed, ref } from 'vue'

export const backendConnection = ref<'unknown' | 'connected' | 'disconnected'>('unknown')
export const connectionNoticeMounted = ref(false)
export const backendNoticeActive = computed(() => connectionNoticeMounted.value && backendConnection.value === 'disconnected')

export function isNetworkError(message: string): boolean {
  return /^(?:TypeError:\s*)?(?:Failed to fetch|Load failed|NetworkError when attempting to fetch resource\.?|Network request failed|The Internet connection appears to be offline\.?|网络连接失败)$/i.test(message.trim())
}

export function markBackendDisconnected() { backendConnection.value = 'disconnected' }
export function markBackendConnected() { backendConnection.value = 'connected' }
