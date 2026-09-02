<script setup lang="ts">
// 本体全景：一眼看懂「知君眼中的我」。
// 中心是「我」，六个扇区是六个分区；离中心越近越可信：实线环内是你确认过的，点线环是很久没再提的，
// 朱砂虚线是信任边界——边界外面的空心点是知君的猜测，等你点头才进来。所有含义都同时用文字表达。
import { computed, ref } from 'vue'
import type { Claim, Layer, OntologyStats, Section } from '@/services/api'
import { LAYER_META, SECTIONS, layerMeta, trustMeta } from '@/shared/ontology'
import {
  BANDS,
  CENTER,
  RINGS,
  SECTOR_DEG,
  SECTOR_INNER,
  SECTOR_ORDER,
  SECTOR_OUTER,
  annularSectorPath,
  nodeAngle,
  nodeBand,
  nodeRadius,
  nodeSize,
  polar,
  sectorCenterDeg,
  sectorStartDeg,
  truncateLabel,
  type Band,
} from '@/shared/selfmap'
import StatusBadge from '@/components/ui/StatusBadge.vue'

const props = defineProps<{
  claims: Claim[]
  stats: OntologyStats | null
  selectedId?: string | null
  layerFilter?: Set<Layer> | null
  focusSection?: Section | null
  // 紧凑模式：更小的分区标签、只留信任边界一句说明、不出图例（建档时放在侧栏用）
  compact?: boolean
  // 建档时正在问的那个分区：柔和朱砂底 + 加粗标签
  highlightSection?: Section | null
  // 刚刚记下的理解：朱砂光晕脉冲约 2 秒
  newIds?: Set<string> | null
}>()

const emit = defineEmits<{
  (e: 'select', claim: Claim): void
  (e: 'sectionFocus', section: Section | null): void
}>()

const now = Date.now()

interface Node {
  claim: Claim
  x: number
  y: number
  size: number
  band: Band
  angle: number
  radius: number
  label: string
}

const visibleClaims = computed(() =>
  props.claims.filter((c) => (c.trustState === 'confirmed' || c.trustState === 'working') && (!props.layerFilter || props.layerFilter.has(c.layer))),
)

const nodes = computed<Node[]>(() =>
  visibleClaims.value.map((claim) => {
    const angle = nodeAngle(claim)
    const radius = nodeRadius(claim, now)
    const { x, y } = polar(angle, radius)
    return { claim, x, y, size: nodeSize(claim), band: nodeBand(claim, now), angle, radius, label: truncateLabel(claim.objectName) }
  }),
)

const sectors = computed(() =>
  SECTOR_ORDER.map((key, i) => {
    const meta = SECTIONS.find((s) => s.key === key)
    const start = sectorStartDeg(i)
    const label = polar(sectorCenterDeg(i), SECTOR_OUTER + 24)
    const counts = props.stats?.bySection?.[key]
    return {
      key,
      index: i,
      label: meta?.label ?? key,
      path: annularSectorPath(start, start + SECTOR_DEG, SECTOR_INNER, SECTOR_OUTER),
      divider: polar(start, SECTOR_OUTER + 8),
      lx: label.x,
      ly: label.y,
      confirmed: counts?.confirmed ?? 0,
      working: counts?.working ?? 0,
      anchor: label.x < CENTER - 40 ? 'end' : label.x > CENTER + 40 ? 'start' : 'middle',
    }
  }),
)

const confirmedTotal = computed(() => props.stats?.claims?.confirmed ?? props.claims.filter((c) => c.trustState === 'confirmed').length)
const inboxCount = computed(() => props.stats?.inbox ?? 0)
const isEmpty = computed(() => props.claims.length === 0)

const ariaSummary = computed(() => {
  const working = props.stats?.claims?.working ?? props.claims.filter((c) => c.trustState === 'working').length
  return `本体全景：已确认 ${confirmedTotal.value} 条理解，待确认 ${working} 条。中心是「我」，六个扇区依次是 ${SECTIONS.map((s) => s.label).join('、')}。`
})

function layerClass(layer: Layer): string {
  return `zj-map__node--${layer}`
}

function isDimmed(section: Section): boolean {
  return !!props.focusSection && props.focusSection !== section
}

function isNew(id: string): boolean {
  return !!props.newIds && props.newIds.has(id)
}

// ---- 提示与选择
const tip = ref<Node | null>(null)
function showTip(node: Node) {
  tip.value = node
}
function hideTip(node: Node) {
  if (tip.value?.claim.id === node.claim.id) tip.value = null
}
function pick(node: Node) {
  emit('select', node.claim)
}
function toggleSection(section: Section) {
  emit('sectionFocus', props.focusSection === section ? null : section)
}

const tipStyle = computed(() => {
  if (!tip.value) return {}
  const left = (tip.value.x / 720) * 100
  const top = (tip.value.y / 720) * 100
  return { left: `${left}%`, top: `${top}%`, transform: left > 60 ? 'translate(-100%, -120%)' : 'translate(0, -120%)' }
})

const legendLayers = (Object.keys(LAYER_META) as Layer[]).map((key) => ({ key, ...LAYER_META[key] }))
</script>

<template>
  <div class="zj-map" :class="{ 'is-empty': isEmpty, 'is-compact': compact }">
    <svg class="zj-map__svg" viewBox="0 0 720 720" role="group" :aria-label="ariaSummary">
      <title>本体全景</title>
      <!-- 扇区底色 + 分隔线 + 标签 -->
      <g class="zj-map__sectors">
        <g
          v-for="s in sectors"
          :key="s.key"
          class="zj-map__sector"
          :class="{ 'is-dimmed': isDimmed(s.key), 'is-focus': focusSection === s.key, 'is-highlight': highlightSection === s.key }"
        >
          <path :d="s.path" :fill="s.index % 2 === 0 ? '#F7F3EA' : '#FBF8F1'" />
          <path v-if="highlightSection === s.key" :d="s.path" fill="#A6452E" fill-opacity="0.09" class="zj-map__sector-tint" />
          <line :x1="polar(sectorStartDeg(s.index), SECTOR_INNER).x" :y1="polar(sectorStartDeg(s.index), SECTOR_INNER).y" :x2="s.divider.x" :y2="s.divider.y" stroke="#E2DED4" stroke-width="1" />
          <g
            class="zj-map__sector-label"
            role="button"
            tabindex="0"
            :aria-pressed="focusSection === s.key"
            :aria-label="`${s.label}：已确认 ${s.confirmed} 条，待确认 ${s.working} 条。点击只看这个分区`"
            @click="toggleSection(s.key)"
            @keydown.enter.prevent="toggleSection(s.key)"
            @keydown.space.prevent="toggleSection(s.key)"
          >
            <text :x="s.lx" :y="s.ly" :text-anchor="s.anchor" class="zj-map__sector-title">{{ s.label }}</text>
            <text :x="s.lx" :y="s.ly + 15" :text-anchor="s.anchor" class="zj-map__sector-count">{{ s.confirmed }} / {{ s.working }}</text>
          </g>
        </g>
      </g>

      <!-- 三个环：已确认 / 需重申 / 信任边界 -->
      <g class="zj-map__rings" aria-hidden="true">
        <circle :cx="CENTER" :cy="CENTER" :r="RINGS.core" fill="none" stroke="#1D211F" stroke-width="1.2" />
        <circle :cx="CENTER" :cy="CENTER" :r="RINGS.reaffirm" fill="none" stroke="#8B8E88" stroke-width="1.2" stroke-dasharray="1.5 5" stroke-linecap="round" />
        <circle :cx="CENTER" :cy="CENTER" :r="RINGS.boundary" fill="none" stroke="#A6452E" stroke-width="1.5" stroke-dasharray="7 5" />
        <text v-if="!compact" :x="CENTER" :y="CENTER - RINGS.core - 6" text-anchor="middle" class="zj-map__ring-label">已确认 · 进入本体</text>
        <text v-if="!compact" :x="CENTER" :y="CENTER - RINGS.reaffirm - 6" text-anchor="middle" class="zj-map__ring-label">需重申 · 超过 60 天没再提</text>
        <text :x="CENTER" :y="CENTER - RINGS.boundary - 6" text-anchor="middle" class="zj-map__ring-label zj-map__ring-label--boundary">
          {{ compact ? '朱砂线外：知君的猜测，等你点头' : '信任边界 · 外面是知君的猜测，等你点头才进来' }}<tspan v-if="inboxCount > 0" class="zj-map__ring-inbox">　● {{ inboxCount }} 条等你点头</tspan>
        </text>
      </g>

      <!-- 节点 -->
      <g class="zj-map__nodes">
        <g
          v-for="(n, i) in nodes"
          :key="n.claim.id"
          class="zj-map__node"
          data-testid="selfmap-node"
          :class="[layerClass(n.claim.layer), `zj-map__node--${n.band}`, { 'is-selected': selectedId === n.claim.id, 'is-dimmed': isDimmed(n.claim.section), 'is-new': isNew(n.claim.id) }]"
          :style="{ '--i': i }"
          role="button"
          tabindex="0"
          :aria-label="`${n.claim.content} · ${layerMeta(n.claim.layer).label} · ${trustMeta(n.claim.trustState).label}`"
          @mouseenter="showTip(n)"
          @mouseleave="hideTip(n)"
          @focus="showTip(n)"
          @blur="hideTip(n)"
          @click="pick(n)"
          @keydown.enter.prevent="pick(n)"
          @keydown.space.prevent="pick(n)"
        >
          <circle v-if="isNew(n.claim.id)" :cx="n.x" :cy="n.y" :r="n.size + 6" class="zj-map__glow" />
          <circle v-if="n.claim.promotionReady" :cx="n.x" :cy="n.y" :r="n.size + 5" class="zj-map__halo" />
          <circle v-if="selectedId === n.claim.id" :cx="n.x" :cy="n.y" :r="n.size + 4" class="zj-map__selected" />
          <circle :cx="n.x" :cy="n.y" :r="n.size" class="zj-map__dot" />
          <line
            v-if="n.claim.exportAllowed && n.claim.trustState === 'confirmed'"
            :x1="polar(n.angle, n.radius + n.size + 1).x"
            :y1="polar(n.angle, n.radius + n.size + 1).y"
            :x2="polar(n.angle, n.radius + n.size + 6).x"
            :y2="polar(n.angle, n.radius + n.size + 6).y"
            class="zj-map__tick"
          />
          <text v-if="n.label" :x="n.x + n.size + 3" :y="n.y + 3.5" class="zj-map__obj">{{ n.label }}</text>
          <text v-if="n.band === 'challenged'" :x="n.x" :y="n.y - n.size - 4" text-anchor="middle" class="zj-map__conflict">有矛盾</text>
        </g>
      </g>

      <!-- 中心「我」印 -->
      <g class="zj-map__seal" aria-hidden="true">
        <circle :cx="CENTER" :cy="CENTER" r="34" fill="#FFFCF6" stroke="#A6452E" stroke-width="2" />
        <text :x="CENTER" :y="CENTER + 9" text-anchor="middle" class="zj-map__seal-text">我</text>
        <text :x="CENTER" :y="CENTER + 52" text-anchor="middle" class="zj-map__seal-sub">已确认 {{ confirmedTotal }} 条</text>
      </g>

      <!-- 空状态解释 -->
      <g v-if="isEmpty" class="zj-map__empty" aria-hidden="true">
        <text :x="CENTER" :y="CENTER + 80" text-anchor="middle" class="zj-map__empty-title">本体，就是知君眼中的你。</text>
        <text :x="CENTER" :y="CENTER + RINGS.core - 14" text-anchor="middle" class="zj-map__empty-line">靠近中心：你确认过的</text>
        <text :x="CENTER" :y="CENTER + RINGS.reaffirm - 14" text-anchor="middle" class="zj-map__empty-line">点线环：很久没再提，可能变了</text>
        <text :x="CENTER" :y="CENTER + RINGS.boundary + 22" text-anchor="middle" class="zj-map__empty-line zj-map__empty-line--cinnabar">朱砂线外：知君的猜测，等你点头才进来</text>
      </g>
    </svg>

    <!-- 悬停 / 聚焦提示 -->
    <div v-if="tip" class="zj-map__tip" :style="tipStyle" role="tooltip">
      <p class="zj-map__tip-content">{{ tip.claim.content }}</p>
      <p class="zj-map__tip-badges">
        <StatusBadge :meta="layerMeta(tip.claim.layer)" />
        <StatusBadge :meta="trustMeta(tip.claim.trustState)" />
        <span v-if="tip.band === 'stale'" class="zj-map__tip-note">很久没再提</span>
        <span v-if="tip.band === 'challenged'" class="zj-map__tip-note">与另一条矛盾</span>
      </p>
    </div>

    <!-- 空状态动作 -->
    <div v-if="isEmpty" class="zj-map__empty-cta">
      <RouterLink to="/" class="zj-map__cta">去聊几句</RouterLink>
    </div>

    <!-- 图例（文字承载含义）；紧凑模式由外层说明 -->
    <dl v-if="!compact" class="zj-map__legend">
      <div v-for="l in legendLayers" :key="l.key" class="zj-map__legend-item">
        <dt><span class="zj-map__swatch" :class="`zj-map__swatch--${l.key}`" aria-hidden="true" /></dt>
        <dd>{{ l.label }}</dd>
      </div>
      <div class="zj-map__legend-item zj-map__legend-item--ring">
        <dt><span class="zj-map__swatch-ring zj-map__swatch-ring--core" aria-hidden="true" /></dt>
        <dd>实线环内：你确认过的</dd>
      </div>
      <div class="zj-map__legend-item zj-map__legend-item--ring">
        <dt><span class="zj-map__swatch-ring zj-map__swatch-ring--reaffirm" aria-hidden="true" /></dt>
        <dd>点线环：超过 60 天没再提</dd>
      </div>
      <div class="zj-map__legend-item zj-map__legend-item--ring">
        <dt><span class="zj-map__swatch-ring zj-map__swatch-ring--boundary" aria-hidden="true" /></dt>
        <dd>朱砂虚线外：等你点头的猜测</dd>
      </div>
    </dl>

    <!-- 读屏器：逐条列出 -->
    <ul class="zj-map__sr">
      <li v-for="n in nodes" :key="`sr-${n.claim.id}`">{{ n.claim.content }} · {{ layerMeta(n.claim.layer).label }} · {{ trustMeta(n.claim.trustState).label }}</li>
    </ul>
  </div>
</template>

<style scoped>
.zj-map {
  position: relative;
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
}
.zj-map__svg {
  display: block;
  width: 100%;
  height: auto;
  font-family: var(--ws-font-display, serif);
  overflow: visible;
}
.zj-map__sector {
  transition: opacity 0.2s ease;
}
.zj-map__sector.is-dimmed {
  opacity: 0.4;
}
.zj-map__sector-label {
  cursor: pointer;
}
.zj-map__sector-label:focus-visible .zj-map__sector-title {
  fill: var(--ws-primary-color, #a6452e);
  text-decoration: underline;
}
.zj-map__sector-title {
  font-size: 15px;
  fill: #1d211f;
}
.zj-map__sector.is-focus .zj-map__sector-title {
  fill: #a6452e;
}
.zj-map__sector.is-highlight .zj-map__sector-title {
  fill: #a6452e;
  font-weight: 700;
}
.zj-map__sector-tint {
  transition: fill-opacity 0.3s ease;
}
.zj-map.is-compact .zj-map__sector-title {
  font-size: 12px;
}
.zj-map.is-compact .zj-map__sector-count {
  font-size: 9px;
}
.zj-map.is-compact .zj-map__ring-label {
  font-size: 10px;
}
.zj-map.is-compact .zj-map__seal-sub {
  font-size: 10px;
}
.zj-map__glow {
  fill: none;
  stroke: #a6452e;
  stroke-width: 2;
  animation: zj-map-glow 2s ease-out both;
}
.zj-map__sector-count {
  font-family: -apple-system, 'PingFang SC', sans-serif;
  font-size: 11px;
  fill: #8b8e88;
}
.zj-map__ring-label {
  font-family: -apple-system, 'PingFang SC', sans-serif;
  font-size: 11px;
  fill: #686b66;
  paint-order: stroke;
  stroke: #fffcf6;
  stroke-width: 4px;
  stroke-linejoin: round;
}
.zj-map__ring-label--boundary {
  fill: #a6452e;
}
.zj-map__ring-inbox {
  font-weight: 700;
}
.zj-map__seal-text {
  font-size: 26px;
  fill: #a6452e;
}
.zj-map__seal-sub {
  font-family: -apple-system, 'PingFang SC', sans-serif;
  font-size: 11px;
  fill: #686b66;
}
.zj-map__node {
  cursor: pointer;
  outline: none;
  transform-origin: center;
  transform-box: fill-box;
  animation: zj-map-pop 0.3s ease both;
  animation-delay: calc(var(--i, 0) * 20ms);
  transition: opacity 0.2s ease;
}
.zj-map__node.is-dimmed {
  opacity: 0.3;
}
.zj-map__node:focus-visible .zj-map__dot {
  stroke: #a6452e;
  stroke-width: 2.5;
}
.zj-map__dot {
  stroke-width: 1.5;
}
.zj-map__node--self_declared .zj-map__dot {
  fill: #1d211f;
  stroke: #1d211f;
}
.zj-map__node--observed .zj-map__dot {
  fill: #4a7c59;
  stroke: #4a7c59;
}
.zj-map__node--aspirational .zj-map__dot {
  fill: #a6452e;
  stroke: #a6452e;
}
.zj-map__node--hypothesis .zj-map__dot {
  fill: #fffcf6;
  stroke: #a6452e;
  stroke-dasharray: 2 2;
}
.zj-map__halo {
  fill: none;
  stroke: #a6452e;
  stroke-opacity: 0.35;
  stroke-width: 1.5;
}
.zj-map__selected {
  fill: none;
  stroke: #a6452e;
  stroke-width: 2;
}
.zj-map__tick {
  stroke: #4a7c59;
  stroke-width: 1.5;
  stroke-linecap: round;
}
.zj-map__obj,
.zj-map__conflict {
  font-family: -apple-system, 'PingFang SC', sans-serif;
  font-size: 10px;
  fill: #686b66;
  pointer-events: none;
}
.zj-map__conflict {
  fill: #a6452e;
}
.zj-map__empty-title {
  font-size: 15px;
  fill: #1d211f;
}
.zj-map__empty-line {
  font-family: -apple-system, 'PingFang SC', sans-serif;
  font-size: 12px;
  fill: #686b66;
  paint-order: stroke;
  stroke: #fffcf6;
  stroke-width: 4px;
}
.zj-map__empty-line--cinnabar {
  fill: #a6452e;
}
.zj-map__empty-cta {
  text-align: center;
  margin: 8px 0 4px;
}
.zj-map__cta {
  display: inline-block;
  padding: 8px 18px;
  border: 1px solid var(--ws-primary-color, #a6452e);
  border-radius: 999px;
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
  text-decoration: none;
}
.zj-map__tip {
  position: absolute;
  z-index: 2;
  max-width: 260px;
  padding: 8px 10px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
  box-shadow: var(--ws-shadow-md, 0 6px 20px rgba(0, 0, 0, 0.1));
  pointer-events: none;
}
.zj-map__tip-content {
  margin: 0 0 6px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-map__tip-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  align-items: center;
}
.zj-map__tip-note {
  font-size: 11px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-map__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin: 12px 0 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-map__legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.zj-map__legend-item dt,
.zj-map__legend-item dd {
  margin: 0;
}
.zj-map__swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 1.5px solid #1d211f;
  background: #1d211f;
}
.zj-map__swatch--observed {
  border-color: #4a7c59;
  background: #4a7c59;
}
.zj-map__swatch--aspirational {
  border-color: #a6452e;
  background: #a6452e;
}
.zj-map__swatch--hypothesis {
  border-color: #a6452e;
  border-style: dashed;
  background: #fffcf6;
}
.zj-map__swatch-ring {
  display: inline-block;
  width: 22px;
  height: 0;
  border-top: 1.5px solid #1d211f;
}
.zj-map__swatch-ring--reaffirm {
  border-top-style: dotted;
  border-top-color: #8b8e88;
}
.zj-map__swatch-ring--boundary {
  border-top-style: dashed;
  border-top-color: #a6452e;
}
.zj-map__sr {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  margin: -1px;
}
@keyframes zj-map-pop {
  from {
    opacity: 0;
    transform: scale(0.6);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
}
@keyframes zj-map-glow {
  0% {
    stroke-opacity: 0.9;
    transform: scale(0.6);
  }
  60% {
    stroke-opacity: 0.5;
    transform: scale(1.6);
  }
  100% {
    stroke-opacity: 0;
    transform: scale(2);
  }
}
.zj-map__glow {
  transform-origin: center;
  transform-box: fill-box;
}
@media (prefers-reduced-motion: reduce) {
  .zj-map__node {
    animation: none;
  }
  .zj-map__glow {
    animation: none;
    stroke-opacity: 0.6;
  }
}
</style>
