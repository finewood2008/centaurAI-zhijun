<script setup lang="ts">
// 顶栏：移动端菜单按钮 + 当前页标题 + 全局搜索（桌面）+ 服务健康状态。
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Menu, Search } from 'lucide-vue-next'
import { api, type MindosAccessContext } from '@/services/api'

const emit = defineEmits<{ (e: 'toggle-menu'): void }>()

const route = useRoute()
const router = useRouter()

const title = computed(() => {
  const t = route.meta.title
  return typeof t === 'string' ? t : '知君'
})

const keyword = ref('')
const backendState = ref<'loading' | 'ok' | 'error'>('loading')
const backendDetail = ref('')
const accessContext = ref<MindosAccessContext | null>(null)

async function checkHealth() {
  backendState.value = 'loading'
  try {
    const h = await api.health()
    backendState.value = 'ok'
    backendDetail.value = `API 正常 · ${h.capabilities.text_model}`
  } catch (e) {
    backendState.value = 'error'
    backendDetail.value = e instanceof Error ? e.message : '无法连接后端服务'
  }
}

async function loadAccessContext() {
  try {
    accessContext.value = await api.mindosAccessContext()
  } catch {
    // 健康检查仍会展示后端状态；连接票据接入前不把状态接口失败伪装成本机调试。
    accessContext.value = null
  }
}

function submitSearch() {
  const value = keyword.value.trim()
  if (!value) return
  router.push({ path: '/search', query: { query: value } })
}

onMounted(() => {
  checkHealth()
  loadAccessContext()
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

    <form class="ws-topbar__search" role="search" @submit.prevent="submitSearch">
      <Search class="ws-topbar__search-icon" :size="16" aria-hidden="true" />
      <input
        v-model="keyword"
        class="ws-topbar__search-input"
        type="search"
        placeholder="搜索知识 / 原材料…"
        aria-label="全局搜索"
      >
    </form>

    <div class="ws-topbar__status">
      <span
        v-if="accessContext?.localDebug"
        class="ws-topbar__pill is-local-debug"
        title="仅限当前开发机，未连接真实 AI 盒子"
      >
        本机调试模式
      </span>
      <span
        v-else-if="accessContext"
        class="ws-topbar__pill is-ticket-required"
        title="本机调试未启用，需通过受控连接票据访问"
      >
        需要连接票据
      </span>
      <span class="ws-topbar__pill" :title="backendDetail">
        <span class="ws-status-dot" :class="`is-${backendState}`" aria-hidden="true" />
        <span v-if="backendState === 'loading'">连接后端…</span>
        <span v-else-if="backendState === 'ok'">服务正常</span>
        <span v-else>服务不可用</span>
      </span>
    </div>
  </header>
</template>

<style scoped>
.ws-topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 60px;
  padding: 0 20px;
  background: var(--ws-body-bg, #fff);
  border-bottom: 1px solid var(--ws-border-color-2, #e4e7ed);
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
  color: var(--ws-text-color, #606266);
  cursor: pointer;
}
.ws-topbar__menu:hover {
  background: var(--ws-card-bg, #f5f7fa);
  color: var(--ws-text-primary-color, #303133);
}

.ws-topbar__title {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: var(--ws-text-primary-color, #303133);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ws-topbar__search {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  max-width: 460px;
  margin-left: 24px;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-body-bg, #fff);
  transition: border-color 0.15s;
}
.ws-topbar__search:focus-within {
  border-color: var(--ws-input-focus-border-color, #1b99ff);
}

.ws-topbar__search-icon {
  flex-shrink: 0;
  color: var(--ws-text-secondary-color, #909399);
}

.ws-topbar__search-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--ws-text-primary-color, #303133);
  font-family: inherit;
  font-size: 13px;
}
.ws-topbar__search-input::placeholder {
  color: var(--ws-text-placeholder-color, #c0c4cc);
}

.ws-topbar__status {
  margin-left: auto;
  display: flex;
  align-items: center;
}

.ws-topbar__pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: 999px;
  background: var(--ws-card-bg, #f5f7fa);
  border: 1px solid var(--ws-border-color, #dcdfe6);
  font-size: 12px;
  color: var(--ws-text-color, #606266);
  white-space: nowrap;
}
.ws-topbar__pill.is-local-debug {
  color: #156e46;
  border-color: #9fd7b9;
  background: #edf8f1;
}
.ws-topbar__pill.is-ticket-required {
  color: #8a5a00;
  border-color: #edcf91;
  background: #fff8e8;
}

.ws-status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ws-text-placeholder-color, #c0c4cc);
}
.ws-status-dot.is-ok {
  background: var(--ws-success-color, #12cd3d);
}
.ws-status-dot.is-error {
  background: var(--ws-danger-color, #ff4918);
}

/* <768px：只保留菜单、标题与状态；隐藏搜索 */
@media (max-width: 767px) {
  .ws-topbar {
    padding: 0 12px;
    gap: 8px;
  }
  .ws-topbar__menu {
    display: inline-flex;
  }
  .ws-topbar__search {
    display: none;
  }
}
</style>
