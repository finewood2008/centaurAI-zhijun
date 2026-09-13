<script setup lang="ts">
import { computed } from 'vue'
import MainLayout from '@/layouts/MainLayout.vue'
import ToastHost from '@/components/ui/ToastHost.vue'
import RoutingConsent from '@/components/conversation/RoutingConsent.vue'
import RagSensitiveDialog from '@/components/conversation/RagSensitiveDialog.vue'
import { ragQuestion, type RagV2Decision } from '@/services/taskRouting'

const ragPrompt = computed(() => ragQuestion.value?.prompt)
const chooseRag = (choice: RagV2Decision) => ragQuestion.value?.done(choice)
</script>

<template>
  <ToastHost>
    <MainLayout>
      <template v-if="$slots.content" #content><slot name="content" /></template>
      <template v-if="$slots.topbar" #topbar="controls"><slot name="topbar" v-bind="controls" /></template>
    </MainLayout>
    <RoutingConsent />
    <Teleport to="body">
      <div v-if="ragPrompt" class="rag-sensitive-host" @click.self="chooseRag('cancel')">
        <RagSensitiveDialog
          :status="ragPrompt.status"
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
