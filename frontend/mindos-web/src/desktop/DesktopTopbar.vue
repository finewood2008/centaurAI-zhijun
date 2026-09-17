<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import type { Phase } from '../../../shared/desktop-contract'
import { Menu } from 'lucide-vue-next'
import { useDesktopWorkspace } from './workspace'
const props = defineProps<{ workspaceReady: boolean }>()
const emit = defineEmits<{ (e: 'toggle-menu'): void }>()
const { state } = useDesktopWorkspace()
const route = useRoute()
const phase = computed(() => state.value.snapshot?.phase)
const labels: Record<Phase, string> = { signed_out: '尚未登录', authenticating: '正在登录', selecting_device: '未连接', connecting: '正在连接盒子', authorizing: '正在连接', ready: '正在打开工作区', disconnecting: '正在断开', failed: '连接未就绪' }
const connectionLabel = computed(() => props.workspaceReady ? '已连接' : phase.value ? labels[phase.value] : '正在初始化')
const title = computed(() => typeof route.meta.title === 'string' ? route.meta.title : '知君')
</script>
<template>
  <header class="product-topbar">
    <button class="product-menu ws-topbar__menu" aria-label="打开导航菜单" @click="emit('toggle-menu')"><Menu :size="20" /></button>
    <h1>{{ title }}</h1>
    <div class="product-connection" role="status"><span class="product-dot" :class="{ connected: workspaceReady }" />
      {{ connectionLabel }}
    </div>
  </header>
</template>
<style scoped>
.product-topbar{display:flex;align-items:center;gap:12px;min-height:56px;padding:8px 24px;border-bottom:1px solid var(--ws-border-color-3);background:var(--ws-body-bg);flex-shrink:0}
h1{margin:0;font-size:16px;font-family:var(--ws-font-display);white-space:nowrap}.product-connection{display:flex;align-items:center;gap:7px;margin-left:auto;font-size:12px;color:var(--ws-text-secondary-color);min-width:0}.product-dot{width:7px;height:7px;border-radius:50%;background:#aaa49a;flex-shrink:0}.product-dot.connected{background:#42835a}button{font:inherit;font-size:12px;cursor:pointer;background:transparent;border:1px solid var(--ws-border-color-3);border-radius:5px;padding:6px 10px;color:var(--ws-text-color);white-space:nowrap}button:disabled{opacity:.5}.product-menu{display:none}
@media(max-width:900px){.product-topbar{padding:8px 12px;gap:8px}}@media(max-width:767px){.product-menu{display:flex}.product-connection{font-size:11px}}
</style>
