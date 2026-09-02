<script setup lang="ts">
// 回复出处小图：左边「这条回复」，右边最多四组来源（已确认理解 / 工作理解 / 资料片段 / 避开的旧理解），
// 连线粗细随数量。每个点都是可聚焦按钮，点了跳到对应理解或资料。
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { ProvenanceEvent } from '@/services/api'
import { groups as buildGroups, lineWidth, truncateTitle, type ProvItem } from '@/shared/provenanceGraph'

const props = defineProps<{ provenance: ProvenanceEvent }>()
const router = useRouter()

const W = 560
const H = 170
const REPLY = { x: 16, y: 55, w: 108, h: 60 }
const GROUP_X = 190
const ROW_H = 36
const DOT_GAP = 22

const groups = computed(() => buildGroups(props.provenance))
const rows = computed(() => {
  const n = groups.value.length
  const total = n * ROW_H
  const startY = Math.max(24, (H - total) / 2 + ROW_H / 2)
  return groups.value.map((g, i) => ({ g, y: startY + i * ROW_H }))
})
const replyMid = { x: REPLY.x + REPLY.w, y: REPLY.y + REPLY.h / 2 }

const hover = ref<{ item: ProvItem; x: number; y: number } | null>(null)
const tip = computed(() => {
  if (!hover.value) return null
  return { left: `${(hover.value.x / W) * 100}%`, top: `${(hover.value.y / H) * 100}%`, text: hover.value.item.label }
})

function layerClass(item: ProvItem): string {
  if (item.kind === 'working') return 'is-working'
  if (item.kind === 'material') return 'is-material'
  if (item.kind === 'retracted') return 'is-retracted'
  if (item.layer === 'observed') return 'is-observed'
  if (item.layer === 'aspirational') return 'is-aspirational'
  return 'is-told'
}

function go(item: ProvItem) {
  if (item.kind === 'material' && item.materialId) {
    router.push(`/materials/${encodeURIComponent(item.materialId)}`)
  } else if (item.kind === 'claim' || item.kind === 'working') {
    router.push({ path: '/me', query: { section: item.section ?? 'who', claim: item.id } })
  } else {
    router.push('/me')
  }
}

function onKey(event: KeyboardEvent, item: ProvItem) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    go(item)
  }
}

function itemAria(item: ProvItem, groupLabel: string): string {
  return `${groupLabel}：${item.label}`
}
</script>

<template>
  <div class="zj-pg" data-testid="provenance-graph">
    <svg :viewBox="`0 0 ${W} ${H}`" class="zj-pg__svg" role="group" :aria-label="groups.length ? `这条回复的依据：${groups.map((g) => `${g.label} ${g.count}`).join('，')}` : '这条回复没有依据本体'">
      <g class="zj-pg__reply" aria-hidden="true">
        <rect :x="REPLY.x" :y="REPLY.y" :width="REPLY.w" :height="REPLY.h" rx="10" />
        <text :x="REPLY.x + REPLY.w / 2" :y="REPLY.y + REPLY.h / 2 + 5" text-anchor="middle">这条回复</text>
      </g>
      <template v-if="rows.length">
        <g v-for="{ g, y } in rows" :key="g.key">
          <path :d="`M ${replyMid.x} ${replyMid.y} C ${replyMid.x + 34} ${replyMid.y}, ${GROUP_X - 34} ${y}, ${GROUP_X - 6} ${y}`" class="zj-pg__link" :class="`is-${g.key}`" :style="{ strokeWidth: lineWidth(g.count) }" aria-hidden="true" />
          <text :x="GROUP_X" :y="y - 10" class="zj-pg__label">{{ g.label }} <tspan class="zj-pg__count">{{ g.count }}</tspan><tspan v-if="g.note" class="zj-pg__note"> · {{ g.note }}</tspan></text>
          <g v-for="(item, i) in g.shown" :key="item.id" class="zj-pg__item" :class="layerClass(item)" role="button" tabindex="0" :aria-label="itemAria(item, g.label)" @click="go(item)" @keydown="onKey($event, item)" @mouseenter="hover = { item, x: GROUP_X + 8 + i * DOT_GAP, y: y + 8 }" @mouseleave="hover = null" @focus="hover = { item, x: GROUP_X + 8 + i * DOT_GAP, y: y + 8 }" @blur="hover = null">
            <template v-if="item.kind === 'material'">
              <rect :x="GROUP_X + i * DOT_GAP" :y="y + 1" width="12" height="15" rx="2" class="zj-pg__doc" />
              <line :x1="GROUP_X + 3 + i * DOT_GAP" :x2="GROUP_X + 9 + i * DOT_GAP" :y1="y + 6" :y2="y + 6" class="zj-pg__doc-line" />
              <line :x1="GROUP_X + 3 + i * DOT_GAP" :x2="GROUP_X + 9 + i * DOT_GAP" :y1="y + 10" :y2="y + 10" class="zj-pg__doc-line" />
            </template>
            <template v-else-if="item.kind === 'retracted'">
              <circle :cx="GROUP_X + 6" :cy="y + 8" r="6" class="zj-pg__dot" />
              <line :x1="GROUP_X + 2" :x2="GROUP_X + 10" :y1="y + 4" :y2="y + 12" class="zj-pg__cross" />
              <line :x1="GROUP_X + 10" :x2="GROUP_X + 2" :y1="y + 4" :y2="y + 12" class="zj-pg__cross" />
              <text :x="GROUP_X + 18" :y="y + 12" class="zj-pg__inline">{{ item.label }}</text>
            </template>
            <circle v-else :cx="GROUP_X + 6 + i * DOT_GAP" :cy="y + 8" r="6" class="zj-pg__dot" />
          </g>
          <text v-if="g.key === 'materials'" :x="GROUP_X + g.shown.length * DOT_GAP + 4" :y="y + 12" class="zj-pg__inline">{{ g.shown.map((m) => truncateTitle(m.label)).join(' · ') }}</text>
          <text v-if="g.extra > 0" :x="GROUP_X + g.shown.length * DOT_GAP + 4" :y="y + 12" class="zj-pg__inline">+{{ g.extra }}</text>
        </g>
      </template>
      <g v-else aria-hidden="true">
        <line :x1="replyMid.x" :x2="GROUP_X - 6" :y1="replyMid.y" :y2="replyMid.y" class="zj-pg__link is-empty" />
        <text :x="GROUP_X" :y="replyMid.y + 4" class="zj-pg__label">没有依据本体，只用了这轮的话</text>
      </g>
    </svg>
    <div v-if="tip" class="zj-pg__tip" :style="{ left: tip.left, top: tip.top }" role="status">{{ tip.text }}</div>
  </div>
</template>

<style scoped>
.zj-pg {
  position: relative;
  margin: 0 0 8px;
}
.zj-pg__svg {
  display: block;
  width: 100%;
  max-width: 560px;
  height: auto;
  overflow: visible;
}
.zj-pg__reply rect {
  fill: var(--ws-surface-2, #fbf8f1);
  stroke: var(--ws-border-color, #d8d3c8);
}
.zj-pg__reply text {
  font-family: var(--ws-font-display, serif);
  font-size: 14px;
  fill: var(--ws-text-primary-color, #1d211f);
}
.zj-pg__link {
  fill: none;
  stroke: var(--ws-border-color, #d8d3c8);
  stroke-linecap: round;
}
.zj-pg__link.is-confirmed {
  stroke: var(--ws-text-primary-color, #1d211f);
}
.zj-pg__link.is-working {
  stroke: var(--ws-primary-color, #a6452e);
  stroke-dasharray: 4 3;
}
.zj-pg__link.is-materials {
  stroke: #4a7c59;
}
.zj-pg__link.is-retracted,
.zj-pg__link.is-empty {
  stroke: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-pg__label {
  font-size: 12px;
  fill: var(--ws-text-color, #3c403d);
}
.zj-pg__count {
  fill: var(--ws-text-primary-color, #1d211f);
  font-weight: 600;
}
.zj-pg__note,
.zj-pg__inline {
  font-size: 11px;
  fill: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-pg__item {
  cursor: pointer;
  outline: none;
}
.zj-pg__dot {
  fill: var(--ws-text-primary-color, #1d211f);
  stroke: var(--ws-text-primary-color, #1d211f);
  stroke-width: 1.5;
}
.zj-pg__item.is-observed .zj-pg__dot {
  fill: #4a7c59;
  stroke: #4a7c59;
}
.zj-pg__item.is-aspirational .zj-pg__dot {
  fill: var(--ws-primary-color, #a6452e);
  stroke: var(--ws-primary-color, #a6452e);
}
.zj-pg__item.is-working .zj-pg__dot {
  fill: var(--ws-body-bg, #fffcf6);
  stroke: var(--ws-primary-color, #a6452e);
  stroke-dasharray: 2 2;
}
.zj-pg__item.is-retracted .zj-pg__dot {
  fill: var(--ws-body-bg, #fffcf6);
  stroke: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-pg__cross {
  stroke: var(--ws-text-placeholder-color, #a3a69f);
  stroke-width: 1.5;
}
.zj-pg__doc {
  fill: var(--ws-body-bg, #fffcf6);
  stroke: #4a7c59;
  stroke-width: 1.2;
}
.zj-pg__doc-line {
  stroke: #4a7c59;
  stroke-width: 1;
}
.zj-pg__item:hover .zj-pg__dot,
.zj-pg__item:focus-visible .zj-pg__dot {
  stroke-width: 3;
}
.zj-pg__item:focus-visible .zj-pg__doc {
  stroke-width: 2.5;
}
.zj-pg__tip {
  position: absolute;
  z-index: 2;
  transform: translate(-50%, calc(-100% - 12px));
  max-width: 280px;
  padding: 4px 8px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-surface-2, #fbf8f1);
  box-shadow: var(--ws-shadow-sm);
  font-size: 12px;
  color: var(--ws-text-color, #3c403d);
  pointer-events: none;
}
</style>
