<script setup lang="ts">
import { inject } from 'vue'
import WorkspaceSettings from '@/components/settings/WorkspaceSettings.vue'
import ExternalAgentsPanel from '@/components/settings/ExternalAgentsPanel.vue'
import ShellPreferenceCard from '@/components/settings/ShellPreferenceCard.vue'
import { immersiveKey } from '@/immersive/shellContext'

// Desktop supplies readiness and its connection slot through its own route.
// Keep all native modules outside the browser build.
withDefaults(defineProps<{ workspaceReady?: boolean; workspaceKey?: string }>(), {
  workspaceReady: true,
  workspaceKey: 'web',
})
// 沉浸壳里这张卡放在偏好抽屉的「高级」组，页面上不再重复
const embedded = !!inject(immersiveKey, null)
</script>

<template>
  <div class="page settings-page">
    <header class="page-head">
      <h1>设置</h1>
      <p>管理连接，调整知君与你相处的方式。</p>
    </header>
    <slot name="connection" />
    <ShellPreferenceCard v-if="!embedded" />
    <ExternalAgentsPanel v-if="workspaceReady" :key="`agents-${workspaceKey}`" />
    <WorkspaceSettings v-if="workspaceReady" :key="workspaceKey" />
    <p v-else class="settings-unavailable" role="status">连接盒子后，可调整提醒、记忆整理、隐私和模型设置。</p>
  </div>
</template>

<style scoped>
.page { max-width: 1080px; margin: 0 auto; padding: 32px; }
.page-head { margin-bottom: 28px; }
h1 { margin: 0 0 8px; font-family: var(--ws-font-display); font-size: 28px; }
.page-head p, .settings-unavailable { color: var(--ws-text-secondary-color); line-height: 1.7; }
.settings-unavailable { margin: 24px 0; }
@media (max-width: 767px) { .page { padding: 20px 16px; } }
</style>
