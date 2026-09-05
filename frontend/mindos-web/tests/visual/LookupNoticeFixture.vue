<script setup lang="ts">
import { computed, ref } from 'vue'
import ProvenanceStrip from '../../src/components/conversation/ProvenanceStrip.vue'
import type { ContextItem, ProvenanceEvent } from '../../src/services/api'

const awaiting = ref(false)
const synthetic: ContextItem = {
  citationId: 'p1', kind: 'claim', id: 'synthetic-claim', version: 'synthetic-v1',
  title: '合成理解：希望先做小范围试用', text: '相比一次完成全部功能，我更希望先邀请少量用户试用，再根据反馈调整。',
  ref: { kind: 'claim', id: 'synthetic-claim', version: 'synthetic-v1' }, category: 'ontology',
}
const provenance = computed<ProvenanceEvent>(() => ({
  contextPlan: {
    revision: 'synthetic-lookup-v1', stage: 'lookup_unavailable',
    delivery: awaiting.value ? 'awaiting_authorization' : 'provided',
    lookupNotice: '额外补查暂未完成，本轮使用已读取且已授权的信息回答。', lookupAttempts: 2,
    background: [], evidence: [synthetic],
    providedRefs: awaiting.value ? [] : ['p1'], citedRefs: awaiting.value ? [] : ['p1'],
    excluded: awaiting.value ? [{ id: synthetic.id, title: synthetic.title, reason: '等待核对授权，尚未提供给模型。' }] : [],
  },
  confirmedClaims: [], workingClaims: [], materials: [], retractedNotices: 0,
  charterVersion: null, promptChars: awaiting.value ? 0 : 420,
}))
</script>

<template>
  <main class="lookup-fixture">
    <header>
      <h1>补查轻提示</h1>
      <p>仅合成资料 · 加载真实来源组件 · 禁止真实接口请求</p>
      <div class="lookup-fixture__controls" aria-label="切换合成状态">
        <button type="button" :aria-pressed="!awaiting" @click="awaiting = false">正常回答已完成</button>
        <button type="button" :aria-pressed="awaiting" @click="awaiting = true">等待核对授权</button>
      </div>
    </header>
    <section aria-label="合成对话" class="lookup-fixture__conversation">
      <p class="lookup-fixture__user">我想先做一个能试用的版本，你觉得下一步怎么安排？</p>
      <article class="lookup-fixture__answer">
        <h2>知君</h2>
        <p v-if="awaiting" data-testid="fixture-answer">这轮回复尚未开始，等待你核对授权。下面仅展示来源区域的待处理状态。</p>
        <p v-else data-testid="fixture-answer">可以先选一个最重要的使用场景，做出能完整走通的小版本，再邀请少量用户试用。这与你之前提到的“小范围试用后再调整”一致。[p1]</p>
      </article>
      <ProvenanceStrip :key="String(awaiting)" :provenance="provenance" />
    </section>
    <label class="lookup-fixture__composer">输入框预览（不会发送）
      <textarea rows="3" placeholder="你可以继续补充，也可以暂时不回答…" />
    </label>
  </main>
</template>

<style scoped>
.lookup-fixture { width: min(100%, 880px); margin: 0 auto; padding: 24px; color: var(--ws-text-color); }
h1 { font-size: 22px; margin: 0 0 8px; }
header > p { color: var(--ws-text-secondary-color); font-size: 12px; }
.lookup-fixture__controls { display: flex; gap: 8px; flex-wrap: wrap; margin: 16px 0 24px; }
button { padding: 8px 12px; border: 1px solid var(--ws-border-color); border-radius: 16px; background: transparent; color: inherit; cursor: pointer; }
button[aria-pressed="true"] { border-color: var(--ws-primary-color); color: var(--ws-primary-color); }
.lookup-fixture__conversation { min-width: 0; }
.lookup-fixture__user { margin: 0 0 16px auto; max-width: 580px; padding: 14px 18px; background: var(--ws-surface-2); border-radius: 12px; line-height: 1.8; }
.lookup-fixture__answer { padding: 18px; border: 1px solid var(--ws-border-color); border-radius: 12px; background: var(--ws-bg-color); }
.lookup-fixture__answer h2 { font-size: 13px; font-weight: 400; color: var(--ws-text-secondary-color); margin: 0 0 12px; }
.lookup-fixture__answer p { margin: 0; line-height: 1.9; overflow-wrap: anywhere; }
.lookup-fixture__composer { display: block; margin-top: 24px; font-size: 12px; color: var(--ws-text-secondary-color); }
textarea { display: block; width: 100%; margin-top: 8px; padding: 14px; font: inherit; font-size: 15px; line-height: 1.6; background: var(--ws-bg-color); color: var(--ws-text-color); border: 1px solid var(--ws-border-color); border-radius: 12px; resize: vertical; }
@media (max-width: 480px) { .lookup-fixture { padding: 16px; } .lookup-fixture__answer { padding: 14px; } }
</style>
