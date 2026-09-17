<script setup lang="ts">
import SettingsPage from '@/pages/SettingsPage.vue'
import DesktopBoxSettings from './DesktopBoxSettings.vue'
import { useDesktopWorkspace } from './workspace'

const { ready, scopeKey } = useDesktopWorkspace()
</script>

<template>
  <SettingsPage :workspace-ready="ready" :workspace-key="scopeKey ?? 'disconnected'">
    <template #connection><DesktopBoxSettings /></template>
  </SettingsPage>
</template>
