<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Hourglass, Link2, Repeat, Scale, type LucideIcon } from 'lucide-vue-next'
import { api, type GovernanceItem, type GovernanceKind, type GovernanceStats } from '@/services/api'
import { governanceKindMeta, governanceStatusMeta } from '@/shared/status'
import { formatDate, formatPercent } from '@/shared/format'
import { useToast } from '@/composables/useToast'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import { createSessionGate } from '@/composables/sessionGate'

const router = useRouter()
const toast = useToast()
const items = ref<GovernanceItem[]>([])
const stats = ref<GovernanceStats | null>(null)
const loading = ref(true)
// 仅用于首次加载失败；操作错误不再覆盖此字段，避免隐藏待办列表
const error = ref('')
const rescaming = ref(false)
const activeKind = ref<'all' | GovernanceKind>('all')
const activeStatus = ref<'all' | string>('all')
// 处理中的项目集合（支持并发：多项可同时仲裁）
const resolvingIds = reactive(new Set<string>())
const mergeItem = ref<GovernanceItem | null>(null)
const mergeCards = ref<{ id: string; title: string }[]>([])
const confirmTarget = ref<GovernanceItem | null>(null)
const loadGate = createSessionGate()

function isResolving(id: string): boolean {
  return resolvingIds.has(id)
}

// 类型 Tab 图标（文案与语义色统一来自 shared/status）
const KIND_ICONS: Record<GovernanceKind, LucideIcon> = {
  duplicate: Repeat,
  outdated: Hourglass,
  relation: Link2,
  conflict: Scale,
}

const filtered = computed(() =>
  items.value.filter((item) => {
    if (activeKind.value !== 'all' && item.kind !== activeKind.value) return false
    if (activeStatus.value !== 'all' && item.status !== activeStatus.value) return false
    return true
  }),
)

async function load() {
  const requestSession = loadGate.next()
  loading.value = true
  error.value = ''
  try {
    const [list, stat] = await Promise.all([
      api.listGovernance({ status: activeStatus.value === 'all' ? undefined : activeStatus.value, kind: activeKind.value === 'all' ? undefined : activeKind.value }),
      api.getGovernanceStats(),
    ])
    if (!loadGate.isCurrent(requestSession)) return
    items.value = list.items
    stats.value = stat
  } catch (e) {
    if (loadGate.isCurrent(requestSession)) error.value = e instanceof Error ? e.message : '本体治理加载失败'
  } finally {
    if (loadGate.isCurrent(requestSession)) loading.value = false
  }
}

async function rescan() {
  rescaming.value = true
  try {
    const result = await api.rescanGovernance()
    toast({ type: 'success', message: `扫描完成，新增 ${result.created} 条建议` })
    await load()
  } catch (e) {
    // 操作失败仅 Toast；保留待办列表
    const message = e instanceof Error ? e.message : '重新扫描失败'
    toast({ type: 'error', message })
  } finally {
    rescaming.value = false
  }
}

const ACTION_LABEL: Record<string, string> = { ignore: '已忽略', merge: '已合并' }

async function resolve(item: GovernanceItem, action: 'ignore' | 'merge', keepKnowledgeId?: string) {
  resolvingIds.add(item.id)
  try {
    await api.resolveGovernance(item.id, action, '', keepKnowledgeId)
    toast({ type: 'success', message: `${ACTION_LABEL[action]}建议「${item.title}」` })
    await load()
  } catch (e) {
    // 操作失败仅 Toast；保留该待办并允许重试
    const message = e instanceof Error ? e.message : '仲裁操作失败'
    toast({ type: 'error', message })
  } finally {
    resolvingIds.delete(item.id)
    if (action === 'merge') mergeItem.value = null
  }
}

function requestIgnore(item: GovernanceItem) {
  confirmTarget.value = item
}

function confirmResolve() {
  const target = confirmTarget.value
  if (!target) return
  confirmTarget.value = null
  resolve(target, 'ignore')
}

async function openMerge(item: GovernanceItem) {
  mergeItem.value = item
  mergeCards.value = []
  const ids = [item.sourceKnowledgeId, item.targetKnowledgeId].filter(Boolean) as string[]
  for (const id of ids) {
    try {
      const card = await api.getKnowledge(id)
      mergeCards.value.push({ id, title: card.title })
    } catch {
      mergeCards.value.push({ id, title: id.slice(0, 16) })
    }
  }
}

// ---- 合并弹窗无障碍：role=dialog、Escape、焦点陷阱与焦点进入/恢复 ----
const mergeModalRef = ref<HTMLElement | null>(null)
let mergeLastFocused: HTMLElement | null = null

function onMergeKeydown(e: KeyboardEvent) {
  if (!mergeItem.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    mergeItem.value = null
    return
  }
  if (e.key === 'Tab' && mergeModalRef.value) {
    const focusables = Array.from(
      mergeModalRef.value.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),
    ).filter((el) => !el.hasAttribute('disabled'))
    if (!focusables.length) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }
}

watch(mergeItem, async (item) => {
  if (item) {
    mergeLastFocused = document.activeElement as HTMLElement | null
    document.addEventListener('keydown', onMergeKeydown)
    await nextTick()
    mergeModalRef.value?.querySelector<HTMLElement>('button')?.focus()
  } else {
    document.removeEventListener('keydown', onMergeKeydown)
    mergeLastFocused?.focus?.()
    mergeLastFocused = null
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onMergeKeydown)
  loadGate.invalidate()
})

function switchKind(kind: 'all' | GovernanceKind) {
  activeKind.value = kind
  load()
}

function switchStatus(status: string) {
  activeStatus.value = status
  load()
}

function openTarget(item: GovernanceItem, id: string | null, type: 'card' | 'material') {
  if (!id) return
  if (type === 'card') router.push(`/knowledge/${id}`)
  else router.push(`/materials/${id}`)
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head governance-head">
      <div>
        <h1>本体治理</h1>
        <p>识别重复、可能过时、待确认关联候选，人工确认后处理。仲裁前不修改任何内容。</p>
      </div>
      <button class="primary-btn" type="button" :disabled="rescaming" @click="rescan">{{ rescaming ? '扫描中…' : '重新扫描' }}</button>
    </div>

    <div v-if="stats" class="graph-stats">
      <div class="stat-card"><span class="stat-value">{{ stats.pending }}</span><span class="stat-label">{{ governanceStatusMeta('pending').label }}</span></div>
      <div class="stat-card"><span class="stat-value">{{ stats.duplicate }}</span><span class="stat-label">{{ governanceKindMeta('duplicate').label }}</span></div>
      <div class="stat-card"><span class="stat-value">{{ stats.outdated }}</span><span class="stat-label">{{ governanceKindMeta('outdated').label }}</span></div>
      <div class="stat-card"><span class="stat-value">{{ stats.relation }}</span><span class="stat-label">{{ governanceKindMeta('relation').label }}</span></div>
      <div class="stat-card"><span class="stat-value">{{ stats.conflict }}</span><span class="stat-label">{{ governanceKindMeta('conflict').label }}</span></div>
      <div class="stat-card"><span class="stat-value">{{ stats.ignored + stats.merged + stats.archived }}</span><span class="stat-label">已处理</span></div>
      <div class="stat-card"><span class="stat-value">{{ stats.total }}</span><span class="stat-label">总计</span></div>
    </div>

    <div class="governance-toolbar">
      <div class="governance-tabs">
        <button class="tab-btn" :class="{ active: activeKind === 'all' }" type="button" @click="switchKind('all')">全部</button>
        <button v-for="(icon, kind) in KIND_ICONS" :key="kind" class="tab-btn" :class="{ active: activeKind === kind }" type="button" @click="switchKind(kind)">
          <component :is="icon" :size="14" aria-hidden="true" />
          {{ governanceKindMeta(kind).label }}
        </button>
      </div>
      <select v-model="activeStatus" class="filter-select" aria-label="状态筛选" @change="switchStatus(activeStatus)">
        <option value="all">全部状态</option>
        <option value="pending">待处理</option>
        <option value="processing">处理中</option>
        <option value="ignored">已忽略</option>
        <option value="merged">已合并</option>
        <option value="archived">已归档</option>
      </select>
    </div>

    <div v-if="loading" class="loading-state">正在加载本体治理…</div>
    <ErrorState v-else-if="error" :message="error" retry-label="重试" @retry="load" />
    <EmptyState
      v-else-if="!filtered.length"
      title="暂无本体治理待办"
      description="当前没有匹配的治理候选；可点击“重新扫描”生成。"
    />
    <div v-else class="governance-list">
      <article v-for="item in filtered" :key="item.id" class="governance-card" :class="[`kind-${item.kind}`, `status-${item.status}`]">
        <div class="gov-card-head">
          <StatusBadge :meta="governanceKindMeta(item.kind)" />
          <StatusBadge :meta="governanceStatusMeta(item.status)" />
          <span class="gov-score">置信度 {{ formatPercent(item.score) }}</span>
          <span class="gov-time">{{ formatDate(item.createdAt) }}</span>
        </div>
        <h3 class="gov-title">{{ item.title }}</h3>
        <p class="gov-reason">{{ item.reason }}</p>

        <div class="gov-objects">
          <button v-if="item.sourceKnowledgeId" class="gov-object" type="button" @click="openTarget(item, item.sourceKnowledgeId, 'card')">
            <span class="gov-object-label">{{ item.kind === 'duplicate' ? '卡片 A' : '知识卡片' }}</span><strong>{{ item.sourceKnowledgeId.slice(0, 14) }}</strong>
          </button>
          <button v-if="item.targetKnowledgeId" class="gov-object" type="button" @click="openTarget(item, item.targetKnowledgeId, 'card')">
            <span class="gov-object-label">卡片 B</span><strong>{{ item.targetKnowledgeId.slice(0, 14) }}</strong>
          </button>
          <button v-if="item.materialId" class="gov-object" type="button" @click="openTarget(item, item.materialId, 'material')">
            <span class="gov-object-label">原材料</span><strong>{{ item.materialId.slice(0, 14) }}</strong>
          </button>
        </div>

        <details class="gov-preview">
          <summary>影响预览</summary>
          <div class="gov-preview-body">
            <p v-if="item.reason">{{ item.reason }}</p>
            <pre v-if="item.snippet" class="gov-snippet">{{ item.snippet }}</pre>
            <p v-else class="gov-snippet-empty">无证据片段。已确认来源关系（source）或共享标签候选，人工确认后才成为正式关系。</p>
          </div>
        </details>

        <div class="gov-actions">
          <template v-if="item.status === 'pending'">
            <button class="secondary-btn sm" type="button" :disabled="isResolving(item.id)" @click="requestIgnore(item)">忽略</button>
            <button v-if="item.kind === 'duplicate'" class="primary-btn sm" type="button" :disabled="isResolving(item.id)" @click="openMerge(item)">合并</button>
          </template>
          <span v-else-if="item.status === 'processing'" class="gov-resolved-note processing-note">处理中，请稍候…</span>
          <span v-else class="gov-resolved-note">{{ item.note || '已处理' }}</span>
        </div>
      </article>
    </div>

    <div v-if="mergeItem" class="gov-modal-mask" @click.self="mergeItem = null">
      <div
        ref="mergeModalRef"
        class="gov-modal"
        role="dialog"
        aria-modal="true"
        aria-label="合并重复卡片"
      >
        <h3>合并重复卡片</h3>
        <p class="gov-modal-hint">请选择要保留的主卡片；另一张的正文将并入主卡片并停用。</p>
        <button
          v-for="card in mergeCards"
          :key="card.id"
          class="gov-keep-btn"
          type="button"
          :disabled="mergeItem ? isResolving(mergeItem.id) : false"
          @click="resolve(mergeItem, 'merge', card.id)"
        >
          <span class="gov-keep-main"><strong>保留：{{ card.title }}</strong><span>{{ card.id }}</span></span>
          <span class="gov-keep-arrow">以该卡片为主合并</span>
        </button>
        <div class="gov-modal-actions">
          <button class="secondary-btn sm" type="button" :disabled="mergeItem ? isResolving(mergeItem.id) : false" @click="mergeItem = null">取消</button>
        </div>
      </div>
    </div>

    <ConfirmDialog
      :open="!!confirmTarget"
      title="忽略建议"
      :message="`确认忽略「${confirmTarget?.title || '该建议'}」？该候选将被标记为已忽略，不再提示。`"
      confirm-text="确认忽略"
      :loading="confirmTarget ? isResolving(confirmTarget.id) : false"
      danger
      @confirm="confirmResolve"
      @cancel="confirmTarget = null"
    />
  </div>
</template>
