<script setup lang="ts">
// 还没建档时今日页只剩这一张卡：文案沿用对话页的 intro，「开始」把用户带到对话页的建档态（/chat?onboarding=1）。
import { Sparkles } from 'lucide-vue-next'
import BaseButton from '@/components/ui/BaseButton.vue'
import OntologyExplainer from '@/components/ontology/OntologyExplainer.vue'

defineEmits<{ (e: 'start'): void }>()
</script>

<template>
  <section class="zj-meet" data-testid="today-first-meet" aria-label="先让我认识真实的你">
    <Sparkles :size="22" aria-hidden="true" />
    <h2>先让我认识真实的你</h2>
    <p>知君不会凭空给建议，也不会只凭一次聊天定义你。先聊十几分钟：你是谁、在做什么、最在意的人、最近一个判断、一条你认同的原则、以及不想让 AI 碰的领域。</p>
    <OntologyExplainer class="zj-meet__explainer" compact />
    <p class="zj-meet__trust">原件留在设备内 · 只有经过你确认的理解才会留下</p>
    <BaseButton variant="primary" @click="$emit('start')">开始</BaseButton>
  </section>
</template>

<style scoped>
.zj-meet {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  padding: 28px 32px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  color: var(--ws-primary-color, #a6452e);
}
.zj-meet h2 {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: var(--ws-display-2, 20px);
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-meet p {
  margin: 0;
  font-size: 14px;
  line-height: 1.8;
  color: var(--ws-text-color, #3c403d);
}
.zj-meet__explainer {
  margin: 8px auto 4px;
}
.zj-meet__trust {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
@media (max-width: 767px) {
  .zj-meet {
    padding: 20px;
  }
}
</style>
