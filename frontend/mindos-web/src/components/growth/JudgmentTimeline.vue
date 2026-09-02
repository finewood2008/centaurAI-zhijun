<script setup lang="ts">
// 判断时间线：横轴是时间，纵轴是「当时的把握」；一个点一个判断，点的填充说状态，朱砂描边说逾期。
// 文字图例与摘要始终存在；点是可聚焦按钮；tooltip 是 HTML 覆盖层（pointer-events: none）。
import { computed, ref } from 'vue'
import type { GrowthDecision } from '@/services/api'
import { formatDate } from '@/shared/format'
import { CHART, domain, dotPositions, formatTick, plotHeight, plotWidth, statusText, summary, ticks, timeScale } from '@/shared/timeline'

const props = defineProps<{
  decisions: GrowthDecision[]
  selectedId?: string | null
  now?: number
}>()
const emit = defineEmits<{ (e: 'select', decisionId: string): void }>()

const nowTs = computed(() => props.now ?? Date.now())
const dom = computed(() => domain(props.decisions, nowTs.value))
const scale = computed(() => timeScale(dom.value.min, dom.value.max, plotWidth()))
const dots = computed(() => dotPositions(props.decisions, nowTs.value))
const xTicks = computed(() => ticks(dom.value.min, dom.value.max, 5).map((t) => ({ t, x: CHART.left + scale.value(t), label: formatTick(t) })))
const todayX = computed(() => CHART.left + scale.value(nowTs.value))
const yLines = [0, 25, 50, 75, 100].map((v) => ({ v, y: CHART.top + ((100 - v) / 100) * plotHeight() }))
const plotBottom = CHART.top + plotHeight()
const plotRight = CHART.left + plotWidth()
const stats = computed(() => summary(props.decisions, nowTs.value))
const byId = computed(() => new Map(props.decisions.map((d) => [d.id, d])))

const hover = ref<string | null>(null)
const tip = computed(() => {
  const id = hover.value
  if (!id) return null
  const dot = dots.value.find((d) => d.id === id)
  const decision = byId.value.get(id)
  if (!dot || !decision) return null
  return {
    left: `${(dot.x / CHART.width) * 100}%`,
    top: `${(dot.y / CHART.height) * 100}%`,
    title: decision.title,
    confidence: decision.confidence,
    status: dot.overdue ? '等结果（已逾期）' : statusText(decision.status),
    reviewAt: decision.reviewAt ? formatDate(decision.reviewAt) : '未定',
  }
})

function ariaLabel(id: string): string {
  const d = byId.value.get(id)
  const dot = dots.value.find((x) => x.id === id)
  if (!d) return ''
  return `${d.title}，把握 ${d.confidence}%，${dot?.overdue ? '等结果，已逾期' : statusText(d.status)}，回访日 ${d.reviewAt ? formatDate(d.reviewAt) : '未定'}`
}

function onKey(event: KeyboardEvent, id: string) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    emit('select', id)
  }
}
</script>

<template>
  <figure class="zj-tl" data-testid="judgment-timeline">
    <figcaption class="zj-tl__caption">
      <span class="zj-tl__title">判断时间线</span>
      <span class="zj-tl__summary">{{ stats.count }} 个判断 · 平均把握 {{ stats.avgConfidence }}% · 逾期 {{ stats.overdue }}</span>
    </figcaption>
    <div class="zj-tl__stage">
      <svg :viewBox="`0 0 ${CHART.width} ${CHART.height}`" class="zj-tl__svg" role="group" :aria-label="`判断时间线，${stats.count} 个判断，平均把握 ${stats.avgConfidence}%，逾期 ${stats.overdue}`">
        <!-- 纵向网格：把握 -->
        <g class="zj-tl__grid" aria-hidden="true">
          <line v-for="l in yLines" :key="l.v" :x1="CHART.left" :x2="plotRight" :y1="l.y" :y2="l.y" />
          <text v-for="l in yLines" :key="`t${l.v}`" :x="CHART.left - 8" :y="l.y + 4" text-anchor="end" class="zj-tl__axis">{{ l.v }}</text>
          <text :x="CHART.left - 8" :y="CHART.top - 8" text-anchor="end" class="zj-tl__axis zj-tl__axis--label">当时的把握</text>
        </g>
        <!-- 横轴：时间刻度 -->
        <g class="zj-tl__grid" aria-hidden="true">
          <line :x1="CHART.left" :x2="plotRight" :y1="plotBottom" :y2="plotBottom" class="zj-tl__base" />
          <g v-for="t in xTicks" :key="t.t">
            <line :x1="t.x" :x2="t.x" :y1="plotBottom" :y2="plotBottom + 5" />
            <text :x="t.x" :y="plotBottom + 18" text-anchor="middle" class="zj-tl__axis">{{ t.label }}</text>
          </g>
        </g>
        <!-- 今天 -->
        <g aria-hidden="true">
          <line :x1="todayX" :x2="todayX" :y1="CHART.top" :y2="plotBottom" class="zj-tl__today" />
          <text :x="todayX + 4" :y="CHART.top + 12" class="zj-tl__axis zj-tl__axis--today">今天</text>
        </g>
        <!-- 回访引导线 -->
        <g aria-hidden="true">
          <template v-for="d in dots" :key="`g-${d.id}`">
            <g v-if="d.reviewX !== null" :class="['zj-tl__guide', { 'is-overdue': d.overdue }]">
              <line :x1="d.x" :x2="d.reviewX" :y1="d.y" :y2="d.y" />
              <line :x1="d.reviewX" :x2="d.reviewX" :y1="d.y - 5" :y2="d.y + 5" />
            </g>
          </template>
        </g>
        <!-- 点 -->
        <g v-for="d in dots" :key="d.id" :class="['zj-tl__dot', `is-${d.status}`, { 'is-overdue': d.overdue, 'is-selected': selectedId === d.id }]" role="button" tabindex="0" :aria-label="ariaLabel(d.id)" :data-decision-id="d.id" @click="emit('select', d.id)" @keydown="onKey($event, d.id)" @mouseenter="hover = d.id" @mouseleave="hover = null" @focus="hover = d.id" @blur="hover = null">
          <clipPath :id="`half-${d.id}`"><rect :x="d.x - 8" :y="d.y - 8" width="8" height="16" /></clipPath>
          <circle v-if="selectedId === d.id" :cx="d.x" :cy="d.y" r="12" class="zj-tl__halo" />
          <circle v-if="d.status === 'outcome_recorded'" :cx="d.x" :cy="d.y" r="7" class="zj-tl__half" :clip-path="`url(#half-${d.id})`" />
          <circle :cx="d.x" :cy="d.y" r="7" class="zj-tl__circle" />
          <text v-if="d.overdue" :x="d.x + 10" :y="d.y - 8" class="zj-tl__overdue">逾期</text>
        </g>
      </svg>
      <div v-if="tip" class="zj-tl__tip" :style="{ left: tip.left, top: tip.top }" role="status">
        <strong>{{ tip.title }}</strong>
        <span>把握 {{ tip.confidence }}% · {{ tip.status }}</span>
        <span>回访日 {{ tip.reviewAt }}</span>
      </div>
    </div>
    <p class="zj-tl__legend">
      <span><i class="zj-tl__key is-open" aria-hidden="true" />空心 = 等结果</span>
      <span><i class="zj-tl__key is-half" aria-hidden="true" />半实 = 已记结果</span>
      <span><i class="zj-tl__key is-done" aria-hidden="true" />实心 = 已复盘</span>
      <span><i class="zj-tl__key is-overdue" aria-hidden="true" />朱砂描边 = 逾期</span>
      <span><i class="zj-tl__key is-today" aria-hidden="true" />虚线 = 今天</span>
    </p>
  </figure>
</template>

<style scoped>
.zj-tl {
  margin: 0 0 20px;
  padding: 14px 16px 10px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
}
.zj-tl__caption {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}
.zj-tl__title {
  font-family: var(--ws-font-display, serif);
  font-size: 16px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-tl__summary {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-tl__stage {
  position: relative;
}
.zj-tl__svg {
  display: block;
  width: 100%;
  height: auto;
  overflow: visible;
}
.zj-tl__grid line {
  stroke: var(--ws-border-color-2, #e2ded4);
  stroke-width: 1;
}
.zj-tl__grid .zj-tl__base {
  stroke: var(--ws-border-color, #d8d3c8);
}
.zj-tl__axis {
  font-size: 11px;
  fill: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-tl__axis--label {
  fill: var(--ws-text-secondary-color, #686b66);
}
.zj-tl__axis--today {
  fill: var(--ws-primary-color, #a6452e);
}
.zj-tl__today {
  stroke: var(--ws-primary-color, #a6452e);
  stroke-width: 1;
  stroke-dasharray: 4 4;
  opacity: 0.7;
}
.zj-tl__guide line {
  stroke: var(--ws-text-placeholder-color, #a3a69f);
  stroke-width: 1;
}
.zj-tl__guide.is-overdue line {
  stroke: var(--ws-primary-color, #a6452e);
}
.zj-tl__dot {
  cursor: pointer;
  outline: none;
}
.zj-tl__circle {
  fill: var(--ws-body-bg, #fffcf6);
  stroke: var(--ws-text-primary-color, #1d211f);
  stroke-width: 1.5;
  transition: r 120ms ease;
}
.zj-tl__dot.is-reviewed .zj-tl__circle {
  fill: var(--ws-text-primary-color, #1d211f);
}
.zj-tl__half {
  fill: var(--ws-text-primary-color, #1d211f);
}
.zj-tl__dot.is-overdue .zj-tl__circle {
  stroke: var(--ws-primary-color, #a6452e);
  stroke-width: 2;
}
.zj-tl__dot:hover .zj-tl__circle,
.zj-tl__dot:focus-visible .zj-tl__circle {
  r: 9;
}
.zj-tl__dot:focus-visible .zj-tl__circle {
  stroke-width: 2.5;
}
.zj-tl__halo {
  fill: none;
  stroke: var(--ws-primary-color, #a6452e);
  stroke-width: 1.5;
  opacity: 0.7;
}
.zj-tl__overdue {
  font-size: 10px;
  fill: var(--ws-primary-color, #a6452e);
}
.zj-tl__tip {
  position: absolute;
  z-index: 2;
  transform: translate(-50%, calc(-100% - 14px));
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-width: 260px;
  padding: 6px 10px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-card-bg, #f3efe6);
  box-shadow: var(--ws-shadow-sm);
  font-size: 12px;
  color: var(--ws-text-color, #3c403d);
  pointer-events: none;
}
.zj-tl__tip strong {
  font-family: var(--ws-font-display, serif);
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-tl__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-tl__legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.zj-tl__key {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 1.5px solid var(--ws-text-primary-color, #1d211f);
  background: transparent;
}
.zj-tl__key.is-half {
  background: linear-gradient(90deg, var(--ws-text-primary-color, #1d211f) 50%, transparent 50%);
}
.zj-tl__key.is-done {
  background: var(--ws-text-primary-color, #1d211f);
}
.zj-tl__key.is-overdue {
  border-color: var(--ws-primary-color, #a6452e);
  border-width: 2px;
}
.zj-tl__key.is-today {
  width: 14px;
  height: 0;
  border: 0;
  border-top: 1px dashed var(--ws-primary-color, #a6452e);
  border-radius: 0;
}
@media (prefers-reduced-motion: reduce) {
  .zj-tl__circle {
    transition: none;
  }
}
</style>
