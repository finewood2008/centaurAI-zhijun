<script setup lang="ts">
// 应用壳：侧栏分组导航 + 顶栏 + 受限内容容器。
// 移动端抽屉开关、Escape 关闭与路由切换后自动关闭。
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from '@/layouts/AppSidebar.vue'
import AppTopbar from '@/layouts/AppTopbar.vue'
import ErrorBoundary from '@/components/ErrorBoundary.vue'

const route = useRoute()
const sidebarOpen = ref(false)

function closeSidebar() {
  sidebarOpen.value = false
}

function toggleSidebar() {
  sidebarOpen.value = !sidebarOpen.value
}

// 路由切换后自动关闭移动端抽屉
watch(() => route.path, closeSidebar)

// 抽屉打开时支持 Escape 关闭
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && sidebarOpen.value) closeSidebar()
}
window.addEventListener('keydown', onKeydown)
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="ws-app">
    <AppSidebar
      :open="sidebarOpen"
      @close="closeSidebar"
      @navigate="closeSidebar"
    />
    <div class="ws-app__main">
      <AppTopbar @toggle-menu="toggleSidebar" />
      <main class="ws-app__content">
        <ErrorBoundary>
          <RouterView />
        </ErrorBoundary>
      </main>
    </div>
  </div>
</template>
