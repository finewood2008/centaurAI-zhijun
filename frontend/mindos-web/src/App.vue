<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import ToastHost from '@/components/ui/ToastHost.vue'
import RoutingConsent from '@/components/conversation/RoutingConsent.vue'
import RagSensitiveDialog from '@/components/conversation/RagSensitiveDialog.vue'
import { ragQuestion, type RagV2Choice } from '@/services/taskRouting'

const ragPrompt = computed(() => ragQuestion.value?.prompt)
const chooseRag = (choice: RagV2Choice) => ragQuestion.value?.done(choice)
const route = useRoute()
const conversationPresentation = computed(() => ['conversation', 'conversation-detail', 'onboarding-chat', 'onboarding-conversation'].includes(String(route.name)))
</script>

<template>
  <ToastHost>
    <MainLayout>
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
