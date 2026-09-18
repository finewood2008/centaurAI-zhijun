<script setup lang="ts">
// 流里的一张卡：「我们把它记成一个判断？」内容就是原来的判断草稿面板（LiveObjectPanel），
// 数据与动作全部来自 activeTurn 桥接：这里不起自己的草稿逻辑，确认 / 放弃 / 重试都转回对话页。
import { computed } from 'vue'
import LiveObjectPanel from '@/components/conversation/LiveObjectPanel.vue'
import { useActiveTurn } from './activeTurn'

const activeTurn = useActiveTurn()
const turn = computed(() => activeTurn.value)
const visible = computed(() => !!turn.value && (!!turn.value.draft.value || turn.value.draftPending.value))
</script>

<template>
  <section v-if="visible && turn" class="zj-draft-card" aria-label="判断草稿" data-testid="draft-card">
    <header class="zj-draft-card__head">
      <span class="zj-seal zj-seal--accent">判断</span>
      <h3>我们把它记成一个判断？</h3>
    </header>
    <LiveObjectPanel
      :draft="turn.draft.value"
      :changed-fields="turn.draftChanged.value"
      :busy="turn.draftBusy.value"
      :error="turn.draftError.value"
      :pending="turn.draftPending.value"
      :timed-out="turn.draftTimedOut.value"
      @confirm="payload => turn?.onConfirmDraft(payload)"
      @discard="turn?.onDiscardDraft()"
      @retry="turn?.retryDraft()"
    />
  </section>
</template>

<style scoped>
.zj-draft-card {
  max-width: 760px;
  margin: 4px 0 18px 32px;
  padding: 12px 14px 8px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.zj-draft-card__head { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; }
.zj-draft-card__head h3 {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: 15px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
@media (max-width: 767px) {
  .zj-draft-card { margin-left: 0; }
}
</style>
