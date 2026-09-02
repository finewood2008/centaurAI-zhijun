<script setup lang="ts">
import { computed } from 'vue'
import type { HomeMapNode, HomeRing } from '@/services/api'

const props = defineProps<{
  nodes: HomeMapNode[]
  relationshipDays: number
  selectedId?: string | null
  empty?: boolean
}>()

const emit = defineEmits<{ (e: 'select', node: HomeMapNode): void }>()

const RING_META: Record<HomeRing, { label: string; radius: number; angles: number[] }> = {
  remembered: { label: '我记得', radius: 18, angles: [-90, 0, 90, 180] },
  tracking: { label: '我们在跟进', radius: 31, angles: [-60, 60, 180] },
  uncertain: { label: '我还不确定', radius: 43, angles: [-72, 48, 168] },
}

const placed = computed(() => {
  const counters: Record<HomeRing, number> = { remembered: 0, tracking: 0, uncertain: 0 }
  return props.nodes.map((node) => {
    const meta = RING_META[node.ring]
    const index = counters[node.ring]++
    const angle = (meta.angles[index % meta.angles.length] * Math.PI) / 180
    return {
      ...node,
      left: 50 + Math.cos(angle) * meta.radius,
      top: 50 + Math.sin(angle) * meta.radius,
      recent: index === 0,
    }
  })
})

const daysLabel = computed(() => props.relationshipDays > 0 ? `我们认识的第 ${props.relationshipDays} 天` : '从今天开始')
</script>

<template>
  <section class="zj-rmap" aria-label="我与知君的共同地图">
    <header class="zj-rmap__head">
      <div>
        <p>我与你走到这里</p>
        <h2>共同地图</h2>
      </div>
      <span>{{ nodes.length ? `${nodes.length} 个正在发光的位置` : '等待第一盏灯亮起' }}</span>
    </header>

    <div class="zj-rmap__legend" aria-label="地图图例">
      <span class="is-remembered"><i />我记得</span>
      <span class="is-tracking"><i />我们在跟进</span>
      <span class="is-uncertain"><i />我还不确定</span>
    </div>

    <div class="zj-rmap__stage" :class="{ 'is-empty': empty }">
      <svg viewBox="0 0 600 600" aria-hidden="true">
        <circle cx="300" cy="300" r="108" class="zj-rmap__ring is-remembered" />
        <circle cx="300" cy="300" r="186" class="zj-rmap__ring is-tracking" />
        <circle cx="300" cy="300" r="258" class="zj-rmap__ring is-uncertain" />
        <path d="M300 42 A258 258 0 0 1 545 220" class="zj-rmap__trace" />
      </svg>

      <div class="zj-rmap__center">
        <strong>你</strong>
        <span>{{ daysLabel }}</span>
      </div>

      <template v-if="nodes.length">
        <button
          v-for="node in placed"
          :key="node.id"
          type="button"
          class="zj-rmap__node"
          :class="[`is-${node.ring}`, { 'is-selected': selectedId === node.id, 'is-recent': node.recent }]"
          :style="{ left: `${node.left}%`, top: `${node.top}%` }"
          :aria-label="`${RING_META[node.ring].label}：${node.title}`"
          @click="emit('select', node)"
        >
          <i aria-hidden="true" />
          <span>{{ node.title }}</span>
        </button>
      </template>

      <template v-else>
        <span class="zj-rmap__ghost is-remembered">你说过的原则</span>
        <span class="zj-rmap__ghost is-tracking">正在等待的结果</span>
        <span class="zj-rmap__ghost is-uncertain">还没确认的理解</span>
      </template>
    </div>
  </section>
</template>

<style scoped>
.zj-rmap {
  min-width: 0;
  padding: 24px 26px 20px;
  border: 1px solid var(--ws-border-color-3, #e8e2d7);
  border-radius: 18px;
  background: linear-gradient(145deg, rgba(255, 253, 248, 0.96), rgba(247, 242, 232, 0.9));
  box-shadow: 0 20px 60px rgba(55, 45, 35, 0.05);
}
.zj-rmap__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
}
.zj-rmap__head p {
  margin: 0 0 4px;
  color: var(--ws-primary-color, #a6452e);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.16em;
}
.zj-rmap__head h2 {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: 25px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-rmap__head > span {
  color: var(--ws-text-placeholder-color, #92958f);
  font-size: 11px;
}
.zj-rmap__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  margin-top: 15px;
  color: var(--ws-text-secondary-color, #686b66);
  font-size: 11px;
}
.zj-rmap__legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.zj-rmap__legend i {
  width: 16px;
  border-top: 1px solid currentColor;
}
.zj-rmap__legend .is-tracking i { border-top-style: dashed; color: #50735a; }
.zj-rmap__legend .is-uncertain i { border-top-style: dotted; color: var(--ws-primary-color, #a6452e); }
.zj-rmap__stage {
  position: relative;
  width: min(100%, 590px);
  aspect-ratio: 1;
  margin: 4px auto 0;
}
.zj-rmap__stage svg {
  display: block;
  width: 100%;
  height: 100%;
  overflow: visible;
}
.zj-rmap__ring {
  fill: none;
  stroke-width: 1.2;
}
.zj-rmap__ring.is-remembered { stroke: rgba(29, 33, 31, 0.38); }
.zj-rmap__ring.is-tracking { stroke: rgba(80, 115, 90, 0.44); stroke-dasharray: 7 6; }
.zj-rmap__ring.is-uncertain { stroke: rgba(166, 69, 46, 0.42); stroke-dasharray: 2 7; stroke-linecap: round; }
.zj-rmap__trace {
  fill: none;
  stroke: rgba(166, 69, 46, 0.48);
  stroke-width: 2;
  stroke-linecap: round;
}
.zj-rmap__center {
  position: absolute;
  left: 50%;
  top: 50%;
  display: grid;
  place-items: center;
  width: 92px;
  height: 92px;
  padding: 8px;
  transform: translate(-50%, -50%);
  border: 1.5px solid var(--ws-primary-color, #a6452e);
  border-radius: 50%;
  background: #fffdf8;
  box-shadow: 0 0 0 9px rgba(166, 69, 46, 0.05);
  text-align: center;
}
.zj-rmap__center strong {
  align-self: end;
  font-family: var(--ws-font-display, serif);
  font-size: 27px;
  font-weight: 600;
  line-height: 1;
  color: var(--ws-primary-color, #a6452e);
}
.zj-rmap__center span {
  align-self: start;
  margin-top: 3px;
  color: var(--ws-text-secondary-color, #686b66);
  font-size: 9px;
  white-space: nowrap;
}
.zj-rmap__node {
  position: absolute;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 6px;
  width: max-content;
  max-width: 138px;
  padding: 6px 8px;
  transform: translate(-50%, -50%);
  border: 1px solid rgba(29, 33, 31, 0.14);
  border-radius: 999px;
  background: rgba(255, 253, 248, 0.94);
  box-shadow: 0 5px 18px rgba(55, 45, 35, 0.07);
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  font-size: 10px;
  line-height: 1.35;
  text-align: left;
  cursor: pointer;
  animation: zj-rmap-enter 0.32s ease both;
}
.zj-rmap__node > i {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #1d211f;
}
.zj-rmap__node > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zj-rmap__node.is-tracking > i { background: #50735a; }
.zj-rmap__node.is-uncertain { border-style: dashed; }
.zj-rmap__node.is-uncertain > i { border: 1px solid var(--ws-primary-color, #a6452e); background: transparent; }
.zj-rmap__node:hover,
.zj-rmap__node.is-selected {
  z-index: 3;
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-primary-color, #a6452e);
}
.zj-rmap__node.is-recent > i { box-shadow: 0 0 0 6px rgba(166, 69, 46, 0.1); }
.zj-rmap__node:focus-visible { outline: 2px solid var(--ws-primary-color, #a6452e); outline-offset: 2px; }
.zj-rmap__ghost {
  position: absolute;
  padding: 5px 8px;
  transform: translate(-50%, -50%);
  border: 1px dashed rgba(104, 107, 102, 0.24);
  border-radius: 999px;
  background: rgba(255, 253, 248, 0.68);
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-size: 10px;
}
.zj-rmap__ghost.is-remembered { left: 50%; top: 32%; }
.zj-rmap__ghost.is-tracking { left: 77%; top: 55%; }
.zj-rmap__ghost.is-uncertain { left: 27%; top: 79%; }
.zj-rmap__stage.is-empty .zj-rmap__ring { opacity: 0.55; }
@keyframes zj-rmap-enter {
  from { opacity: 0; transform: translate(-50%, -44%) scale(0.96); }
  to { opacity: 1; transform: translate(-50%, -50%) scale(1); }
}
@media (prefers-reduced-motion: reduce) {
  .zj-rmap__node { animation: none; }
}
@media (max-width: 600px) {
  .zj-rmap { padding: 20px 14px 14px; border-radius: 14px; }
  .zj-rmap__head { padding: 0 4px; }
  .zj-rmap__head > span { max-width: 120px; text-align: right; }
  .zj-rmap__legend { padding: 0 4px; }
  .zj-rmap__node { max-width: 104px; padding: 5px 7px; font-size: 9px; }
  .zj-rmap__center { width: 76px; height: 76px; }
  .zj-rmap__center strong { font-size: 23px; }
  .zj-rmap__center span { font-size: 8px; }
}
</style>
