<script setup lang="ts">
// 壳的二选一在这里，不碰路由表：沉浸壳（ImmersiveShell）或经典壳（MainLayout）。
// 建档流程（route.meta.onboardingFlow / /onboarding*）暂留经典壳。
// 桌面端的连接状态只经 props 传入（workspaceReady / connectionLabel），沉浸壳不引入 src/desktop/*。
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import ImmersiveShell from '@/immersive/ImmersiveShell.vue'
import { immersiveShell } from '@/immersive/shellPreference'
import ToastHost from '@/components/ui/ToastHost.vue'
import RoutingConsent from '@/components/conversation/RoutingConsent.vue'
import RagSensitiveDialog from '@/components/conversation/RagSensitiveDialog.vue'
import { ragQuestion, type RagV2Choice } from '@/services/taskRouting'

withDefaults(defineProps<{ workspaceReady?: boolean; connectionLabel?: string }>(), { workspaceReady: true, connectionLabel: '' })

const ragPrompt = computed(() => ragQuestion.value?.prompt)
const chooseRag = (choice: RagV2Choice) => ragQuestion.value?.done(choice)
const route = useRoute()
const conversationPresentation = computed(() => ['conversation', 'conversation-detail', 'onboarding-chat', 'onboarding-conversation'].includes(String(route.name)))
const useImmersiveShell = computed(() => immersiveShell.value && route.meta.onboardingFlow !== true && !route.path.startsWith('/onboarding'))
</script>

<template>
  <ToastHost>
    <ImmersiveShell v-if="useImmersiveShell" :workspace-ready="workspaceReady" :connection-label="connectionLabel">
      <template v-if="$slots.content" #content><slot name="content" /></template>
    </ImmersiveShell>
    <MainLayout v-else>
      <template v-if="$slots.content" #content><slot name="content" /></template>
      <template v-if="$slots.topbar" #topbar="controls"><slot name="topbar" v-bind="controls" /></template>
    </MainLayout>
    <RoutingConsent :conversation="conversationPresentation" />
    <Teleport to="body">
      <div v-if="ragPrompt" class="rag-sensitive-host" @click.self="chooseRag('cancel')">
        <RagSensitiveDialog
          :conversation="conversationPresentation"
          :key="ragPrompt.interactionId"
          :interaction-id="ragPrompt.interactionId"
          :status="ragPrompt.status"
          :items="ragPrompt.items"
          :query="ragPrompt.query"
          :scope-label="ragPrompt.scopeLabel"
          :outcome="ragPrompt.outcome"
          :delivery-mode="ragPrompt.deliveryMode"
          :hits="ragPrompt.hits"
          :detection-notice="ragPrompt.detectionNotice"
          :can-read-original="ragPrompt.canReadOriginal"
          :passed-count="ragPrompt.passedCount"
          :risk-available="ragPrompt.riskAvailable"
          @masked="chooseRag('masked')"
          @original="chooseRag('original')"
          @continue-passed="chooseRag('continue-passed')"
          @retry="chooseRag('retry')"
          @risk-release="chooseRag('risk-release')"
          @use-selected="ids => chooseRag({ action: 'use-selected', selectedPreviewIds: ids })"
          @without-materials="chooseRag('without-materials')"
          @cancel="chooseRag('cancel')"
        />
      </div>
    </Teleport>
  </ToastHost>
</template>

<style scoped>
.rag-sensitive-host {
  position: fixed;
  z-index: 1000;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 16px;
  overflow: auto;
  background: rgb(20 18 15 / 48%);
}
</style>
