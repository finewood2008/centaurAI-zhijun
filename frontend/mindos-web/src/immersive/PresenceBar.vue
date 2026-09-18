<script setup lang="ts">
// 上方一行：左边一枚「知」印（点开关于知君）与一句在场文字；右边由壳放印坞。
// 在场文字由 usePresence 决定（在想 / 在找资料 / 在整理 N 件事 / 在听 / 第 N 天 / 问候），
// 没配模型时接「去偏好」，断连时接「重新连接」，桌面未就绪时显示连接状态。
import { ref } from 'vue'
import AboutZhijun from '@/components/branding/AboutZhijun.vue'
import { useBackendHealth } from '@/composables/useBackendHealth'
import { usePresence } from './composables/usePresence'

const props = withDefaults(defineProps<{ workspaceReady?: boolean; connectionLabel?: string }>(), { workspaceReady: true, connectionLabel: '' })
const aboutOpen = ref(false)
const { checking, checkHealth, noticeActive } = useBackendHealth()
const { presence } = usePresence({
  workspaceReady: () => props.workspaceReady,
  connectionLabel: () => props.connectionLabel,
  backendDown: () => noticeActive.value,
})
</script>

<template>
  <header class="zj-presence">
    <button type="button" class="zj-presence__seal" aria-label="关于知君" aria-haspopup="dialog" title="关于知君" @click="aboutOpen = true">知</button>
    <p class="zj-presence__line" role="status" aria-live="polite">
      <span>{{ presence.text }}</span>
      <template v-if="presence.action === 'reconnect'">
        <span aria-hidden="true"> · </span><button type="button" :disabled="checking" @click="checkHealth">{{ checking ? '正在重连…' : '重新连接' }}</button>
      </template>
      <template v-else-if="presence.action === 'prefs'">
        <span aria-hidden="true"> · </span><RouterLink to="/settings">去偏好</RouterLink>
      </template>
    </p>
    <slot name="dock" />
  </header>
  <AboutZhijun v-model:open="aboutOpen" />
</template>
