<script setup lang="ts">
// 顶栏：移动端菜单按钮 + 当前页标题。只有后端不可用时才出现一条提示；其他时候什么都不说。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Menu } from 'lucide-vue-next'
import { api } from '@/services/api'
import { backendConnection, backendNoticeActive, connectionNoticeMounted, markBackendConnected, markBackendDisconnected } from '@/shared/backendConnection'

const emit = defineEmits<{ (e: 'toggle-menu'): void }>()

const route = useRoute()

const title = computed(() => {
  const t = route.meta.title
  return typeof t === 'string' ? t : '知君'
})

const checking = ref(false)
let alive = true
let timer: ReturnType<typeof setTimeout> | undefined
let request: AbortController | null = null

async function checkHealth() {
  if (checking.value || !alive) return
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
</script>

<template>
  <header class="ws-topbar">
    <button
      class="ws-topbar__menu"
      type="button"
      aria-label="打开导航菜单"
      @click="emit('toggle-menu')"
    >
      <Menu :size="20" aria-hidden="true" />
    </button>

    <h1 class="ws-topbar__title">{{ title }}</h1>

    <div v-if="backendNoticeActive" class="ws-topbar__alert" role="status" aria-live="polite">
      <span>知君暂时未连接，你可以保留当前输入。</span>
      <button type="button" class="ws-topbar__retry" :disabled="checking" @click="checkHealth">{{ checking ? '正在重连…' : '重新连接' }}</button>
    </div>
  </header>
</template>

<style scoped>
.ws-topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 56px;
  padding: 8px 24px;
  box-sizing: border-box;
  background: var(--ws-body-bg, #fffcf6);
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
  flex-shrink: 0;
}

.ws-topbar__menu {
  display: none;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: var(--ws-radius, 6px);
  background: transparent;
  color: var(--ws-text-color, #3c403d);
  cursor: pointer;
}
.ws-topbar__menu:hover {
  background: var(--ws-surface-2, #fbf8f1);
  color: var(--ws-text-primary-color, #1d211f);
}

.ws-topbar__title {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: var(--ws-display-3, 16px);
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--ws-text-primary-color, #1d211f);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ws-topbar__alert {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
  padding: 4px 12px;
  border: 1px solid var(--ws-danger-color, #a6452e);
  border-radius: 3px;
  color: var(--ws-danger-color, #a6452e);
  font-size: 12px;
  min-width: 0;
  line-height: 1.5;
}
.ws-topbar__alert span { overflow-wrap: anywhere; }
.ws-topbar__retry {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: inherit;
  font-family: inherit;
  font-size: 12px;
  text-decoration: underline;
  cursor: pointer;
}

@media (max-width: 767px) {
  .ws-topbar {
    padding: 8px 12px;
    gap: 8px;
  }
  .ws-topbar__menu {
    display: inline-flex;
  }
}
</style>
