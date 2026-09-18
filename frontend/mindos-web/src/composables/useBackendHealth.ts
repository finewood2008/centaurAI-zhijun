// 后端健康轮询：从 AppTopbar 抽出，经典顶栏与沉浸壳的在场行共用。
// 连上后 30 秒一查，断开后 5 秒一查；桌面端由盒子连接状态代替，不查。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '@/services/api'
import { isDesktopProduct } from '@/shared/productScope'
import { backendConnection, backendNoticeActive, connectionNoticeMounted, markBackendConnected, markBackendDisconnected } from '@/shared/backendConnection'

export function useBackendHealth() {
  const checking = ref(false)
  let alive = true
  let timer: ReturnType<typeof setTimeout> | undefined
  let request: AbortController | null = null

  async function checkHealth() {
    if (isDesktopProduct() || checking.value || !alive) return
    checking.value = true
    request = new AbortController()
    const timeout = setTimeout(() => request?.abort(), 5000)
    try {
      await api.health(request.signal)
      if (alive) markBackendConnected()
    } catch {
      if (alive) markBackendDisconnected()
    } finally {
      clearTimeout(timeout)
      request = null
      checking.value = false
      if (alive) {
        clearTimeout(timer)
        timer = setTimeout(checkHealth, backendConnection.value === 'disconnected' ? 5000 : 30000)
      }
    }
  }

  watch(backendConnection, state => {
    if (state === 'disconnected' && !checking.value) {
      clearTimeout(timer)
      timer = setTimeout(checkHealth, 1000)
    }
  })
  onMounted(() => {
    connectionNoticeMounted.value = true
    void checkHealth()
    window.addEventListener('online', checkHealth)
  })
  onBeforeUnmount(() => {
    alive = false; clearTimeout(timer); request?.abort()
    connectionNoticeMounted.value = false
    window.removeEventListener('online', checkHealth)
  })

  return { checking, checkHealth, noticeActive: backendNoticeActive }
}
