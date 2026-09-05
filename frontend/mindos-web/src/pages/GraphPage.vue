<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type GraphData, type GraphNode } from '@/services/api'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'

const router = useRouter()
const graph = ref<GraphData | null>(null)
const loading = ref(true)
const error = ref('')
const selectedId = ref<string | null>(null)

// 视图变换（缩放 / 平移）
const scale = ref(1)
const panX = ref(0)
const panY = ref(0)

// 节点类型与边类型元信息
const NODE_COLOR: Record<string, string> = { material: '#3b82f6', knowledge: '#8b5cf6' }
const RELATION_META: Record<string, { label: string; color: string; dash: string; kind: string }> = {
  source: { label: '来源', color: '#2f9e6e', dash: '', kind: '已确认' },
  'shared-tag': { label: '共享标签', color: '#e07b2f', dash: '6 4', kind: '候选' },
  similar: { label: '内容相似', color: '#3b82f6', dash: '3 4', kind: '候选' },
  semantic: { label: '语义关系', color: '#a855f7', dash: '', kind: 'AI 抽取' },
}
// 未知 relation 的兜底，避免后端新增类型导致页面直接崩溃
const FALLBACK_META: { label: string; color: string; dash: string; kind: string } = { label: '未知关系', color: '#9ca3af', dash: '', kind: '候选' }
function edgeMeta(relation: string): { label: string; color: string; dash: string; kind: string } {
  return RELATION_META[relation] ?? FALLBACK_META
}
// 箭头规则：source 恒走绿色箭头；semantic 仅 directed===true（两端均命中资源，方向 S→O）走紫色箭头；
// directed 缺失（旧接口）或 false（单端命中，"资料涉及该实体"无方向）不渲染箭头
function markerFor(edge: { relation: string; directed?: boolean }): string | undefined {
  if (edge.relation === 'source') return 'url(#arrow-confirmed)'
  if (edge.relation === 'semantic' && edge.directed === true) return 'url(#arrow-semantic)'
  return undefined
}

interface LayoutNode extends GraphNode {
  x: number
  y: number
  vx: number
  vy: number
  degree: number
}

const layoutNodes = ref<LayoutNode[]>([])

const nodeMap = computed(() => new Map(layoutNodes.value.map((n) => [n.id, n])))
const selectedNode = computed(() => (selectedId.value ? nodeMap.value.get(selectedId.value) ?? null : null))

const selectedEdges = computed(() => {
  if (!selectedId.value || !graph.value) return []
  const id = selectedId.value
  return graph.value.edges
    .filter((e) => e.source === id || e.target === id)
    .map((e) => {
      const otherId = e.source === id ? e.target : e.source
      const other = nodeMap.value.get(otherId)
      return { ...e, otherId, otherLabel: other?.label ?? otherId, otherType: other?.type ?? 'material' }
    })
})

function runLayout() {
  const nodes = layoutNodes.value
  const edges = graph.value?.edges ?? []
  const n = nodes.length
  if (!n) return
  const W = 800
  const H = 600

  // 环形初始化
  nodes.forEach((node, i) => {
    const angle = (i / n) * Math.PI * 2
    node.x = W / 2 + Math.cos(angle) * (Math.min(W, H) / 2 - 70)
    node.y = H / 2 + Math.sin(angle) * (Math.min(W, H) / 2 - 70)
    node.vx = 0
    node.vy = 0
  })

  const byId = new Map(nodes.map((node) => [node.id, node]))

  for (let iter = 0; iter < 200; iter++) {
    const cooling = 1 - iter / 200
    // 斥力（两两）
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = nodes[i].x - nodes[j].x
        const dy = nodes[i].y - nodes[j].y
        const dist2 = Math.max(dx * dx + dy * dy, 4)
        const dist = Math.sqrt(dist2)
        const f = (3200 / dist2) * cooling
        const fx = (dx / dist) * f
        const fy = (dy / dist) * f
        nodes[i].vx += fx
        nodes[i].vy += fy
        nodes[j].vx -= fx
        nodes[j].vy -= fy
      }
    }
    // 弹簧力（有边节点）
    for (const edge of edges) {
      const a = byId.get(edge.source)
      const b = byId.get(edge.target)
      if (!a || !b) continue
      const dx = b.x - a.x
      const dy = b.y - a.y
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 0.01)
      const f = 0.06 * (dist - 100) * cooling
      const fx = (dx / dist) * f
      const fy = (dy / dist) * f
      a.vx += fx
      a.vy += fy
      b.vx -= fx
      b.vy -= fy
    }
    // 中心引力
    for (const node of nodes) {
      node.vx += (W / 2 - node.x) * 0.01 * cooling
      node.vy += (H / 2 - node.y) * 0.01 * cooling
    }
    // 积分 + 边界
    for (const node of nodes) {
      node.vx *= 0.85
      node.vy *= 0.85
      node.x += node.vx
      node.y += node.vy
      node.x = Math.max(24, Math.min(W - 24, node.x))
      node.y = Math.max(24, Math.min(H - 24, node.y))
    }
  }
}

function shortLabel(label: string) {
  return label.length > 9 ? `${label.slice(0, 9)}…` : label
}

function selectNode(id: string) {
  selectedId.value = id
}

function openDetail(node: LayoutNode) {
  if (node.type === 'material') router.push(`/materials/${node.id}`)
  else router.push(`/knowledge/${node.id}`)
}

function openRelated(item: { otherId: string; otherType: string }) {
  if (item.otherType === 'material') router.push(`/materials/${item.otherId}`)
  else router.push(`/knowledge/${item.otherId}`)
}

// 缩放 / 平移
function onWheel(e: WheelEvent) {
  const factor = e.deltaY > 0 ? 0.9 : 1.1
  scale.value = Math.min(2.5, Math.max(0.5, scale.value * factor))
}

let panning = false
let startX = 0
let startY = 0
function onPanStart(e: MouseEvent) {
  panning = true
  startX = e.clientX - panX.value
  startY = e.clientY - panY.value
}
function onPanMove(e: MouseEvent) {
  if (!panning) return
  panX.value = e.clientX - startX
  panY.value = e.clientY - startY
}
function onPanEnd() {
  panning = false
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    graph.value = await api.getGraph()
    layoutNodes.value = graph.value.nodes.map((node) => ({ ...node, x: 0, y: 0, vx: 0, vy: 0, degree: 0 }))
    runLayout()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '关系图谱加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1>关系图谱</h1>
      <p>浏览知君资料与知识卡片的关联关系。</p>
    </div>

    <div v-if="loading" class="loading-state">正在生成关系图谱…</div>
    <ErrorState v-else-if="error" :message="error" retry-label="重试" @retry="load" />
    <EmptyState
      v-else-if="!graph || !graph.nodes.length"
      title="暂无图谱数据"
      description="导入资料或创建知识卡片后，图谱会自动生成。"
    />
    <template v-else>
      <div class="graph-stats">
        <div class="stat-card"><span class="stat-value">{{ graph.stats.totalNodes }}</span><span class="stat-label">节点总数</span></div>
        <div class="stat-card"><span class="stat-value">{{ graph.stats.materials }}</span><span class="stat-label">原材料</span></div>
        <div class="stat-card"><span class="stat-value">{{ graph.stats.knowledge }}</span><span class="stat-label">知识卡片</span></div>
        <div class="stat-card"><span class="stat-value">{{ graph.stats.totalEdges }}</span><span class="stat-label">关系总数</span></div>
        <div class="stat-card"><span class="stat-value">{{ graph.stats.sourceEdges }}</span><span class="stat-label">来源关系</span></div>
        <div class="stat-card"><span class="stat-value">{{ graph.stats.sharedTagEdges }}</span><span class="stat-label">共享标签</span></div>
        <div class="stat-card"><span class="stat-value">{{ graph.stats.similarEdges }}</span><span class="stat-label">内容相似</span></div>
        <div class="stat-card"><span class="stat-value">{{ graph.stats.semanticEdges ?? 0 }}</span><span class="stat-label">语义关系</span></div>
        <div class="stat-card"><span class="stat-value">{{ graph.stats.isolatedNodes }}</span><span class="stat-label">孤立节点</span></div>
      </div>

      <div class="graph-legend">
        <span class="legend-item"><span class="legend-node" style="background: #3b82f6"></span>原材料</span>
        <span class="legend-item"><span class="legend-node" style="background: #8b5cf6"></span>知识卡片</span>
        <span class="legend-item"><span class="legend-line" style="background: #2f9e6e"></span>来源（已确认）</span>
        <span class="legend-item"><span class="legend-line dashed" style="background: #e07b2f"></span>共享标签（候选）</span>
        <span class="legend-item"><span class="legend-line dashed-short" style="background: #3b82f6"></span>内容相似（候选）</span>
        <span class="legend-item"><span class="legend-line" style="background: #a855f7"></span>语义关系 · AI 抽取</span>
        <span class="legend-hint">语义边：箭头表示有向关系（X 谓词 Y），无箭头表示"资料涉及该实体"的关联</span>
        <span class="legend-hint">滚轮缩放 · 拖拽平移 · 点击节点查看详情</span>
      </div>

      <div class="graph-layout">
        <div class="graph-canvas" @wheel.prevent="onWheel">
          <div
            class="graph-svg-wrap"
            :style="{ transform: `translate(${panX}px, ${panY}px) scale(${scale})` }"
            @mousedown="onPanStart"
            @mousemove="onPanMove"
            @mouseup="onPanEnd"
            @mouseleave="onPanEnd"
          >
            <svg class="graph-svg" viewBox="0 0 800 600" preserveAspectRatio="xMidYMid meet">
              <defs>
                <marker id="arrow-confirmed" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
                  <path d="M0,0 L7,3 L0,6 Z" fill="#2f9e6e"></path>
                </marker>
                <marker id="arrow-semantic" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
                  <path d="M0,0 L7,3 L0,6 Z" fill="#a855f7"></path>
                </marker>
              </defs>
              <line
                v-for="(edge, i) in graph.edges"
                :key="`e${i}`"
                :x1="nodeMap.get(edge.source)?.x ?? 0"
                :y1="nodeMap.get(edge.source)?.y ?? 0"
                :x2="nodeMap.get(edge.target)?.x ?? 0"
                :y2="nodeMap.get(edge.target)?.y ?? 0"
                :stroke="edgeMeta(edge.relation).color"
                :stroke-width="edge.relation === 'source' || edge.relation === 'semantic' ? 2 : 1.5"
                :stroke-dasharray="edgeMeta(edge.relation).dash || undefined"
                :marker-end="markerFor(edge)"
                class="graph-edge"
              ></line>
              <g
                v-for="node in layoutNodes"
                :key="node.id"
                class="graph-node"
                :class="{ selected: node.id === selectedId }"
                role="button"
                tabindex="0"
                :aria-label="`查看节点 ${node.label}`"
                :aria-pressed="node.id === selectedId"
                @mousedown.stop
                @click.stop="selectNode(node.id)"
                @keydown.enter.prevent="selectNode(node.id)"
                @keydown.space.prevent="selectNode(node.id)"
              >
                <circle
                  :cx="node.x"
                  :cy="node.y"
                  :r="node.type === 'knowledge' ? 14 : 11"
                  :fill="NODE_COLOR[node.type]"
                  stroke="#fff"
                  stroke-width="2"
                ></circle>
                <text :x="node.x" :y="node.y + 26" class="graph-node-label" text-anchor="middle">{{ shortLabel(node.label) }}</text>
              </g>
            </svg>
          </div>
        </div>

        <aside class="graph-sidebar" :class="{ empty: !selectedNode }">
          <template v-if="selectedNode">
            <div class="sidebar-head">
              <span class="badge" :class="selectedNode.type === 'material' ? 'material-badge' : 'knowledge-badge'">
                {{ selectedNode.type === 'material' ? '原材料' : '知识卡片' }}
              </span>
              <h3>{{ selectedNode.label }}</h3>
            </div>
            <div class="sidebar-meta-row">
              <span class="sidebar-meta-item"><strong>{{ selectedNode.referenceCount }}</strong> 引用次数</span>
              <span class="sidebar-meta-item"><strong>{{ selectedEdges.length }}</strong> 关联关系</span>
            </div>
            <div v-if="selectedNode.tags.length" class="sidebar-tags">
              <span v-for="tag in selectedNode.tags" :key="tag" class="tag-chip">{{ tag }}</span>
            </div>
            <div class="sidebar-section">
              <div class="sidebar-section-title">关联关系（{{ selectedEdges.length }}）</div>
              <div v-if="!selectedEdges.length" class="sidebar-muted">此节点暂无关联</div>
              <div v-else class="sidebar-edges">
                <button v-for="(item, i) in selectedEdges" :key="i" class="sidebar-edge" type="button" @click="openRelated(item)">
                  <span class="sidebar-edge-line" :style="{ background: edgeMeta(item.relation).color }"></span>
                  <span class="sidebar-edge-main">
                    <strong>{{ item.otherLabel }}</strong>
                    <span class="sidebar-edge-meta">{{ item.otherType === 'material' ? '原材料' : '知识卡片' }} · {{ edgeMeta(item.relation).label }}</span>
                    <span v-if="item.relation === 'semantic' && item.reason" class="sidebar-edge-reason">{{ item.reason }}</span>
                  </span>
                  <span class="badge soon" :class="edgeMeta(item.relation).kind === '已确认' ? 'confirmed-badge' : 'candidate-badge'">
                    {{ edgeMeta(item.relation).kind }}
                  </span>
                </button>
              </div>
            </div>
            <div class="sidebar-actions">
              <button class="primary-btn" type="button" @click="openDetail(selectedNode)">查看详情</button>
              <button class="secondary-btn" type="button" @click="selectedId = null">关闭</button>
            </div>
          </template>
          <template v-else>
            <div class="sidebar-placeholder">点击图谱中的节点查看详情</div>
          </template>
        </aside>
      </div>
    </template>
  </div>
</template>
