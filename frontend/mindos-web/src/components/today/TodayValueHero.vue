<script setup lang="ts">
// 今日页的价值首屏：先说清知君带来的长期收益，再用用户自己的积累证明。
// 没建档时不展示一排“0”，改为说明产品会怎样工作。
import { ArrowRight, ChevronRight } from 'lucide-vue-next'

interface ValueProof {
  key: 'understanding' | 'decision' | 'review'
  label: string
  value: string
  benefit: string
  target: 'ontology' | 'judgments'
}

const props = defineProps<{
  established: boolean
  confirmed: number
  decisions: number
  reviews: number
  primaryLabel: string
}>()

const emit = defineEmits<{
  (e: 'start'): void
  (e: 'open', target: 'ontology' | 'judgments'): void
}>()

function proofs(): ValueProof[] {
  if (!props.established) {
    return [
      { key: 'understanding', label: '记住你', value: '不用从头解释', benefit: '把你重视的人、事和原则带进下一次对话', target: 'ontology' },
      { key: 'decision', label: '陪你判断', value: '不只给一个答案', benefit: '把选项、倾向和把握说清楚，留下可回看的记录', target: 'judgments' },
      { key: 'review', label: '回看结果', value: '让经历成为经验', benefit: '在结果发生后回来核对，让下一次判断更稳', target: 'judgments' },
    ]
  }
  return [
    { key: 'understanding', label: '不必反复交代自己', value: `${props.confirmed} 条`, benefit: '已确认理解，会成为以后对话的上下文', target: 'ontology' },
    { key: 'decision', label: '重要选择不再凭印象', value: `${props.decisions} 个`, benefit: '判断的选项、倾向和把握已经被记录', target: 'judgments' },
    { key: 'review', label: '结果回来，经验留下', value: `${props.reviews} 次`, benefit: '结果回访已经沉淀，下一次可以直接复用', target: 'judgments' },
  ]
}
</script>

<template>
  <section class="zj-value" aria-labelledby="today-value-title">
    <div class="zj-value__promise">
      <p class="zj-value__kicker">你的长期思考伙伴</p>
      <h2 id="today-value-title" class="zj-value__title">
        把今天聊过的，<br>
        <span>变成明天用得上的判断。</span>
      </h2>
      <p class="zj-value__copy">
        知君不会只给一次答案。它会记住你在意什么，陪你把重要选择说清楚，再在结果发生后回来复盘。
      </p>
      <p class="zj-value__loop" aria-label="知君的价值闭环">
        <span>记住你</span><i>→</i><span>陪你判断</span><i>→</i><span>回看结果</span><i>→</i><span>越来越懂你</span>
      </p>
      <div class="zj-value__actions">
        <button type="button" class="zj-value__primary" @click="emit('start')">
          {{ primaryLabel }}
          <ArrowRight :size="16" aria-hidden="true" />
        </button>
        <button v-if="established" type="button" class="zj-value__secondary" @click="emit('open', 'ontology')">
          看看知君已了解我什么
        </button>
      </div>
    </div>

    <aside class="zj-value__evidence" :aria-label="established ? '知君已经为你积累的价值' : '知君会怎样帮助你'">
      <div class="zj-value__evidence-head">
        <span>{{ established ? '正在为你积累' : '它会怎样帮助你' }}</span>
        <small>{{ established ? '来自你的真实使用' : '从第一次对话开始' }}</small>
      </div>
      <button v-for="item in proofs()" :key="item.key" type="button" class="zj-value__proof" @click="emit('open', item.target)">
        <span class="zj-value__proof-main">
          <span class="zj-value__proof-label">{{ item.label }}</span>
          <strong>{{ item.value }}</strong>
        </span>
        <span class="zj-value__proof-benefit">{{ item.benefit }}</span>
        <ChevronRight :size="16" aria-hidden="true" />
      </button>
      <p class="zj-value__evidence-foot">这些积累会在未来的对话里再次被用到。</p>
    </aside>
  </section>
</template>

<style scoped>
.zj-value {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(300px, 0.75fr);
  min-height: 330px;
  overflow: hidden;
  border: 1px solid var(--ws-border-color-3, #e8e2d7);
  border-radius: 18px;
  background:
    radial-gradient(circle at 12% 18%, rgba(166, 69, 46, 0.11), transparent 30%),
    linear-gradient(135deg, #fffdf8 0%, #f7f1e6 100%);
  box-shadow: 0 18px 50px rgba(55, 45, 35, 0.06);
}
.zj-value__promise {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 42px 44px 38px;
}
.zj-value__kicker {
  margin: 0 0 14px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.18em;
  color: var(--ws-primary-color, #a6452e);
}
.zj-value__title {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: clamp(30px, 3.4vw, 44px);
  font-weight: 600;
  letter-spacing: -0.025em;
  line-height: 1.2;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-value__title span {
  color: var(--ws-primary-color, #a6452e);
}
.zj-value__copy {
  max-width: 590px;
  margin: 18px 0 0;
  font-size: 15px;
  line-height: 1.85;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-value__loop {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  margin: 18px 0 0;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-value__loop i {
  font-style: normal;
  color: var(--ws-primary-color, #a6452e);
}
.zj-value__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 16px;
  margin-top: auto;
  padding-top: 28px;
}
.zj-value__primary,
.zj-value__secondary,
.zj-value__proof {
  font-family: inherit;
  cursor: pointer;
}
.zj-value__primary {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 11px 16px;
  border: 1px solid var(--ws-primary-color, #a6452e);
  border-radius: 8px;
  background: var(--ws-primary-color, #a6452e);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
}
.zj-value__primary:hover {
  filter: brightness(0.94);
}
.zj-value__secondary {
  padding: 8px 0;
  border: 0;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  font-size: 13px;
}
.zj-value__secondary:hover {
  color: var(--ws-primary-color, #a6452e);
}
.zj-value__evidence {
  display: flex;
  flex-direction: column;
  padding: 20px;
  border-left: 1px solid rgba(166, 69, 46, 0.14);
  background: rgba(255, 255, 255, 0.58);
  backdrop-filter: blur(8px);
}
.zj-value__evidence-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 4px 14px;
  border-bottom: 1px solid var(--ws-border-color-3, #e8e2d7);
  color: var(--ws-text-primary-color, #1d211f);
  font-size: 13px;
  font-weight: 600;
}
.zj-value__evidence-head small {
  color: var(--ws-text-placeholder-color, #92958f);
  font-size: 11px;
  font-weight: 400;
}
.zj-value__proof {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 4px 10px;
  align-items: center;
  width: 100%;
  padding: 17px 4px;
  border: 0;
  border-bottom: 1px solid var(--ws-border-color-3, #e8e2d7);
  background: transparent;
  color: inherit;
  text-align: left;
}
.zj-value__proof:hover .zj-value__proof-label,
.zj-value__proof:hover > svg {
  color: var(--ws-primary-color, #a6452e);
}
.zj-value__proof-main {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.zj-value__proof-label {
  color: var(--ws-text-primary-color, #1d211f);
  font-size: 13px;
  font-weight: 600;
}
.zj-value__proof-main strong {
  flex: none;
  font-family: var(--ws-font-display, serif);
  font-size: 18px;
  color: var(--ws-primary-color, #a6452e);
}
.zj-value__proof-benefit {
  min-width: 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-value__proof > svg {
  grid-column: 2;
  grid-row: 1 / span 2;
  color: var(--ws-text-placeholder-color, #92958f);
}
.zj-value__evidence-foot {
  margin: auto 4px 2px;
  padding-top: 14px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--ws-text-placeholder-color, #92958f);
}
.zj-value__primary:focus-visible,
.zj-value__secondary:focus-visible,
.zj-value__proof:focus-visible {
  outline: 2px solid var(--ws-primary-color, #a6452e);
  outline-offset: 2px;
}

@media (max-width: 840px) {
  .zj-value {
    grid-template-columns: 1fr;
  }
  .zj-value__evidence {
    border-top: 1px solid rgba(166, 69, 46, 0.14);
    border-left: 0;
  }
}
@media (max-width: 560px) {
  .zj-value {
    border-radius: 14px;
  }
  .zj-value__promise {
    padding: 28px 22px 26px;
  }
  .zj-value__title {
    font-size: 30px;
  }
  .zj-value__copy {
    font-size: 14px;
    line-height: 1.75;
  }
  .zj-value__loop {
    align-items: flex-start;
    font-size: 11px;
  }
  .zj-value__actions {
    align-items: flex-start;
    flex-direction: column;
    width: 100%;
  }
  .zj-value__primary {
    justify-content: center;
    width: 100%;
  }
  .zj-value__evidence {
    padding: 16px 18px;
  }
}
</style>
