<script setup lang="ts">
import { computed } from 'vue'
import { LockKeyhole } from 'lucide-vue-next'
import BaseButton from '@/components/ui/BaseButton.vue'
import DesktopConnection from './DesktopConnection.vue'
import { useDesktopWorkspace } from './workspace'
import { connectedDeviceLabel } from './deviceDisplay'

const { controller, state, ready } = useDesktopWorkspace()
const deviceLabel = computed(() => connectedDeviceLabel(state.value.snapshot?.subject ?? null))
const connectionPathLabel = computed(() => state.value.snapshot?.subject?.selectedPath === 'DIRECT' ? '直连'
  : state.value.snapshot?.subject?.selectedPath === 'RELAY' ? '安全中继' : '')
const secureConnection = computed(() => ready.value
  && state.value.snapshot?.environment === 'production' && !!connectionPathLabel.value)
const securityExplanation = computed(() => state.value.snapshot?.subject?.selectedPath === 'RELAY'
  ? '电脑与盒子之间的通道已加密。中继转发加密数据；在线模型的资料外发仍需单独授权。'
  : '电脑与盒子之间的通道已加密，业务数据通过直连传输。在线模型的资料外发仍需单独授权。')
</script>

<template>
  <section class="box-settings" aria-labelledby="box-settings-title" data-testid="box-settings">
    <header class="box-settings__head">
      <div>
        <h2 id="box-settings-title">账号与盒子</h2>
        <p class="box-settings__account" data-testid="account">账号：{{ state.snapshot?.subject?.accountId }}</p>
      </div>
      <BaseButton size="sm" :disabled="state.controlPending" data-testid="sign-out" @click="controller.control('signOut')">退出登录</BaseButton>
    </header>
    <div v-if="ready" class="box-settings__connected">
      <h3>{{ deviceLabel || '当前盒子' }}</h3>
      <p class="box-settings__status">
        <span>已连接</span>
        <LockKeyhole v-if="secureConnection" :size="14" role="img" aria-label="已建立加密连接" :title="securityExplanation" />
        <span v-if="connectionPathLabel" data-testid="connection-path">{{ connectionPathLabel }}</span>
      </p>
      <p v-if="secureConnection" class="box-settings__detail">{{ securityExplanation }}</p>
      <div class="box-settings__actions">
        <BaseButton size="sm" :disabled="state.controlPending" data-testid="switch-box" @click="controller.control('disconnect')">切换盒子</BaseButton>
        <BaseButton size="sm" variant="text" :disabled="state.controlPending" data-testid="disconnect" @click="controller.control('disconnect')">断开连接</BaseButton>
      </div>
      <p v-if="state.error" role="alert" class="box-settings__error">{{ state.error.message }}</p>
    </div>
    <DesktopConnection v-else embedded settings />
  </section>
</template>

<style scoped>
.box-settings { padding: 24px; margin-bottom: 28px; border: 1px solid var(--ws-border-color-3); border-radius: 12px; background: var(--ws-surface-1, var(--ws-body-bg)); }
.box-settings__head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 22px; }
h2 { margin: 0 0 8px; font-family: var(--ws-font-display); font-size: 20px; }
h3 { margin: 0 0 10px; font-size: 16px; overflow-wrap: anywhere; }
.box-settings__account, .box-settings__detail { margin: 0; font-size: 12px; color: var(--ws-text-secondary-color); line-height: 1.7; overflow-wrap: anywhere; }
.box-settings__status { display: flex; align-items: center; gap: 8px; margin: 0 0 10px; font-size: 13px; color: var(--ws-text-secondary-color); }
.box-settings__status > span:first-child { color: #42835a; }
.box-settings__actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; }
.box-settings__error { color: var(--ws-error-color, #a6452e); }
@media (max-width: 767px) { .box-settings { padding: 16px; } .box-settings__head { gap: 10px; } }
</style>
