<script setup lang="ts">
// 顶栏：移动端菜单按钮 + 当前页标题。只有后端不可用时才出现一条提示；其他时候什么都不说。
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Menu } from 'lucide-vue-next'
import { api } from '@/services/api'

const emit = defineEmits<{ (e: 'toggle-menu'): void }>()

const route = useRoute()

const title = computed(() => {
  const t = route.meta.title
  return typeof t === 'string' ? t : '知君'
})

const backendState = ref<'loading' | 'ok' | 'error'>('loading')
const backendDetail = ref('')

async function checkHealth() {
  backendState.value = 'loading'
  try {
    await api.health()
    backendState.value = 'ok'
    backendDetail.value = ''
  } catch (e) {
    backendState.value = 'error'
    backendDetail.value = e instanceof Error ? e.message : '无法连接后端服务'
  }
}

onMounted(() => {
  checkHealth()
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

    <div v-if="backendState === 'error'" class="ws-topbar__alert" role="alert">
      <span>知君的后端没有响应{{ backendDetail ? `：${backendDetail}` : '' }}</span>
      <button type="button" class="ws-topbar__retry" @click="checkHealth">重试</button>
    </div>
  </header>
</template>

<style scoped>
.ws-topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 56px;
  padding: 0 24px;
  background: var(--ws-body-bg, #fffcf6);
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
  flex-shrink: 0;
}

.ws-topbar__menu {
  display: none;
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
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ws-topbar__retry {
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
    padding: 0 12px;
    gap: 8px;
  }
  .ws-topbar__menu {
    display: inline-flex;
  }
}
</style>
