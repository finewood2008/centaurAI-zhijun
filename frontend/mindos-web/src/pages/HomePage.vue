<script setup lang="ts">
// 知君「今日」：优先展示成长闭环，资料与知识概览独立加载、独立降级。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowRight,
  CalendarClock,
  FileText,
  FolderOpen,
  MessageCircle,
  Network,
  Scale,
  Search,
  Sparkles,
  Sprout,
  Target,
  Upload,
  type LucideIcon,
} from 'lucide-vue-next'
import {
  api,
  type GrowthDecision,
  type GrowthToday,
  type HomeOverview,
  type KnowledgeCard,
  type UploadResult,
} from '@/services/api'
import { materialStatusMeta } from '@/shared/status'
import { formatDate } from '@/shared/format'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'

const router = useRouter()
const today = ref<GrowthToday | null>(null)
const todayLoading = ref(true)
const todayError = ref('')
const overview = ref<HomeOverview | null>(null)
const overviewLoading = ref(true)
const overviewError = ref('')

interface QuickLink {
  path: string
  icon: LucideIcon
  title: string
  desc: string
}

interface FocusItem {
  kind: 'outcome' | 'review'
  decision: GrowthDecision
  dueState?: 'overdue' | 'due_soon'
}

const QUICK_LINKS: QuickLink[] = [
  { path: '/materials', icon: FolderOpen, title: '原材料', desc: '导入与查看人生资料' },
  { path: '/knowledge', icon: FileText, title: '知识档案', desc: '整理已确认的知识' },
  { path: '/search', icon: Search, title: '搜索记忆', desc: '在本地资料中找回细节' },
  { path: '/qa', icon: MessageCircle, title: '问知君', desc: '基于你的资料提问' },
  { path: '/graph', icon: Network, title: '关系图谱', desc: '查看人物与事件关联' },
  { path: '/governance', icon: Scale, title: '本体治理', desc: '确认或纠正知君的理解' },
]

// 今日严格只展示 3 个主要事项：逾期 → 待复盘 → 即将到期。
const focusItems = computed<FocusItem[]>(() => {
  if (!today.value) return []
  return today.value.todayItems.slice(0, 3).map((item) => ({
    kind: item.type === 'pending_review' ? 'review' : 'outcome',
    decision: item.decision,
    dueState: item.urgency === 'pending_review' ? undefined : item.urgency,
  }))
})

const outcomeItems = computed(() => focusItems.value.filter((item) => item.kind === 'outcome'))
const reviewItems = computed(() => focusItems.value.filter((item) => item.kind === 'review'))
const totalOutcomeCount = computed(() => today.value ? today.value.stats.overdueDecisions + today.value.stats.dueSoonDecisions : 0)
const totalReviewCount = computed(() => today.value?.stats.pendingReviews ?? 0)
const hiddenFocusCount = computed(() => {
  if (!today.value) return 0
  return Math.max(0, totalOutcomeCount.value + totalReviewCount.value - focusItems.value.length)
})

function openMaterial(item: UploadResult) {
  router.push({ path: `/materials/${item.materialId}`, query: { name: item.fileName } })
}

function openKnowledge(item: KnowledgeCard) {
  router.push(`/knowledge/${item.knowledgeId}`)
}

function openGrowthAction(decision: GrowthDecision, action: 'outcome' | 'review') {
  router.push({ path: '/growth', query: { decisionId: decision.id, action } })
}

async function loadToday() {
  todayLoading.value = true
  todayError.value = ''
  try {
    today.value = await api.getGrowthToday()
  } catch (e) {
    todayError.value = e instanceof Error ? e.message : '今日事项加载失败'
  } finally {
    todayLoading.value = false
  }
}

async function loadOverview() {
  overviewLoading.value = true
  overviewError.value = ''
  try {
    overview.value = await api.getHome()
  } catch (e) {
    overviewError.value = e instanceof Error ? e.message : '资料概览加载失败'
  } finally {
    overviewLoading.value = false
  }
}

onMounted(() => {
  void Promise.allSettled([loadToday(), loadOverview()])
})
</script>

<template>
  <div class="page">
    <div class="page-head home-head">
      <div>
        <h1>今日</h1>
        <p>知君陪你跟踪重要判断、理解真实结果，把经历变成可复用的成长。</p>
      </div>
      <div class="home-actions">
        <BaseButton variant="secondary" @click="router.push('/materials')">
          <Upload :size="15" aria-hidden="true" />导入资料
        </BaseButton>
        <BaseButton variant="primary" @click="router.push({ path: '/growth', query: { create: 'decision' } })">
          <Target :size="15" aria-hidden="true" />记录判断
        </BaseButton>
      </div>
    </div>

    <section class="today-section" aria-labelledby="today-growth-heading">
      <div class="section-heading">
        <div>
          <h2 id="today-growth-heading">成长闭环</h2>
          <p>只展示当下最需要你回应的事情。</p>
        </div>
        <BaseButton variant="text" size="sm" @click="router.push('/growth')">
          进入成长<ArrowRight :size="14" aria-hidden="true" />
        </BaseButton>
      </div>

      <div v-if="todayLoading" class="loading-state" aria-live="polite">正在整理今日事项…</div>
      <ErrorState v-else-if="todayError" :message="todayError" retry-label="重试成长概览" @retry="loadToday" />

      <template v-else-if="today">
        <article class="charter-card" :class="{ 'is-empty': !today.currentCharter }">
          <span class="charter-card__icon" aria-hidden="true"><Sprout :size="20" /></span>
          <div class="charter-card__body">
            <div class="charter-card__eyebrow">人生章程</div>
            <template v-if="today.currentCharter">
              <h3>{{ today.currentCharter.vision }}</h3>
              <p>第 {{ today.currentCharter.version }} 版 · {{ today.currentCharter.goals.length }} 个当前目标</p>
            </template>
            <template v-else>
              <h3>先告诉知君，你想成为怎样的人</h3>
              <p>章程是成长建议的用户授权依据，它不会由 AI 替你决定。</p>
            </template>
          </div>
          <BaseButton variant="secondary" size="sm" @click="router.push('/growth')">
            {{ today.currentCharter ? '查看与更新' : '创建章程' }}
          </BaseButton>
        </article>

        <div class="growth-stats" aria-label="成长统计">
          <button type="button" class="growth-stat" @click="router.push('/growth')"><span>{{ today.stats.openDecisions }}</span><small>进行中判断</small></button>
          <button type="button" class="growth-stat" @click="router.push('/growth')"><span>{{ today.stats.overdueDecisions + today.stats.dueSoonDecisions }}</span><small>待跟踪结果</small></button>
          <button type="button" class="growth-stat" @click="router.push('/growth')"><span>{{ today.stats.pendingReviews }}</span><small>待完成复盘</small></button>
          <button type="button" class="growth-stat" @click="router.push('/growth')"><span>{{ today.stats.totalReviews }}</span><small>已沉淀复盘</small></button>
        </div>

        <div class="focus-grid">
          <section class="focus-panel">
            <div class="focus-panel__head">
              <h3><CalendarClock :size="17" aria-hidden="true" />待跟踪判断</h3><span>{{ totalOutcomeCount }}</span>
            </div>
            <div v-if="outcomeItems.length" class="focus-list">
              <button v-for="item in outcomeItems" :key="item.decision.id" type="button" class="focus-item" @click="openGrowthAction(item.decision, 'outcome')">
                <span class="focus-item__main"><strong>{{ item.decision.title }}</strong><small>观察时间：{{ formatDate(item.decision.reviewAt) }}</small></span>
                <span class="focus-item__state" :class="{ 'is-overdue': item.dueState === 'overdue' }">{{ item.dueState === 'overdue' ? '已到期' : '即将到期' }}</span>
                <ArrowRight :size="15" aria-hidden="true" />
              </button>
            </div>
            <div v-else-if="totalOutcomeCount" class="collapsed-state">本轮有更高优先级的事项；其余 {{ totalOutcomeCount }} 个判断可在成长页查看。</div>
            <EmptyState v-else title="暂无到期判断" description="记录选择和观察时间，知君会在合适的时候请你回看结果。" />
          </section>

          <section class="focus-panel">
            <div class="focus-panel__head">
              <h3><Sparkles :size="17" aria-hidden="true" />待复盘结果</h3><span>{{ totalReviewCount }}</span>
            </div>
            <div v-if="reviewItems.length" class="focus-list">
              <button v-for="item in reviewItems" :key="item.decision.id" type="button" class="focus-item" @click="openGrowthAction(item.decision, 'review')">
                <span class="focus-item__main"><strong>{{ item.decision.title }}</strong><small>{{ item.decision.outcome?.result || '已记录结果' }}</small></span>
                <span class="focus-item__state is-review">去复盘</span><ArrowRight :size="15" aria-hidden="true" />
              </button>
            </div>
            <div v-else-if="totalReviewCount" class="collapsed-state">今日优先展示逾期判断；其余 {{ totalReviewCount }} 个待复盘结果可在成长页查看。</div>
            <EmptyState v-else title="暂无待复盘结果" description="记录真实结果后，可以在这里把经验变成下一次行动。" />
          </section>
        </div>

        <p v-if="hiddenFocusCount" class="focus-more">今日只展示 3 个主要事项，其余 {{ hiddenFocusCount }} 项可在 <button type="button" @click="router.push('/growth')">成长</button> 中查看。</p>

        <section class="latest-review">
          <div class="latest-review__icon" aria-hidden="true"><Sparkles :size="19" /></div>
          <div>
            <div class="latest-review__label">最近复盘</div>
            <template v-if="today.latestReview">
              <h3>{{ today.latestReview.reflection }}</h3><p>下一步：{{ today.latestReview.nextAction }}</p><small>{{ formatDate(today.latestReview.createdAt) }}</small>
            </template>
            <template v-else>
              <h3>还没有完成过复盘</h3><p>真实结果与当初判断的差异，是最值得留下的成长资料。</p>
            </template>
          </div>
        </section>
      </template>
    </section>

    <section class="memory-section" aria-labelledby="memory-heading">
      <div class="section-heading"><div><h2 id="memory-heading">资料与知识</h2><p>这些是知君理解你的证据底座。</p></div></div>
      <div v-if="overviewLoading" class="loading-state" aria-live="polite">正在加载资料概览…</div>
      <ErrorState v-else-if="overviewError" :message="overviewError" retry-label="重试资料概览" @retry="loadOverview" />

      <template v-else-if="overview">
        <div class="memory-stats">
          <button type="button" @click="router.push('/materials')"><strong>{{ overview.recentMaterials.length }}</strong><span>最近导入</span></button>
          <button type="button" @click="router.push('/knowledge')"><strong>{{ overview.recentKnowledge.length }}</strong><span>最近编辑</span></button>
          <button type="button" :class="{ 'is-warn': overview.failedCount > 0 }" @click="router.push({ path: '/materials', query: { status: 'failed' } })"><strong>{{ overview.failedCount }}</strong><span>处理失败</span></button>
          <button type="button" :class="{ 'is-warn': overview.pendingGovernance > 0 }" @click="router.push('/governance')"><strong>{{ overview.pendingGovernance }}</strong><span>本体待治理</span></button>
        </div>

        <div class="memory-grid">
          <section class="memory-panel">
            <div class="memory-panel__head"><h3>最近导入资料</h3><BaseButton variant="text" size="sm" @click="router.push('/materials')">全部<ArrowRight :size="14" aria-hidden="true" /></BaseButton></div>
            <EmptyState v-if="!overview.recentMaterials.length" title="暂无导入资料" description="上传文档、图片或音频后，将在这里显示最近处理结果。" />
            <ul v-else class="memory-list">
              <li v-for="item in overview.recentMaterials" :key="item.materialId"><button type="button" @click="openMaterial(item)"><span class="memory-list__title">{{ item.fileName }}</span><StatusBadge :meta="materialStatusMeta(item.status)" /><span class="memory-list__time">{{ formatDate(item.createdAt) }}</span></button></li>
            </ul>
          </section>

          <section class="memory-panel">
            <div class="memory-panel__head"><h3>最近编辑知识</h3><BaseButton variant="text" size="sm" @click="router.push('/knowledge')">全部<ArrowRight :size="14" aria-hidden="true" /></BaseButton></div>
            <EmptyState v-if="!overview.recentKnowledge.length" title="暂无知识档案" description="从原材料生成或新建知识后，会在这里显示最近编辑。" />
            <ul v-else class="memory-list">
              <li v-for="item in overview.recentKnowledge" :key="item.knowledgeId"><button type="button" @click="openKnowledge(item)"><span class="memory-list__title">{{ item.title }}</span><span class="memory-list__time">{{ formatDate(item.updatedAt) }}</span></button></li>
            </ul>
          </section>
        </div>
      </template>
    </section>

    <section class="quick-section">
      <h2>继续探索</h2>
      <div class="quick-grid"><button v-for="link in QUICK_LINKS" :key="link.path" type="button" @click="router.push(link.path)"><component :is="link.icon" :size="20" aria-hidden="true" /><strong>{{ link.title }}</strong><span>{{ link.desc }}</span></button></div>
    </section>
  </div>
</template>

<style scoped>
.collapsed-state{display:grid;min-height:178px;place-items:center;padding:24px;color:var(--ws-text-secondary-color);font-size:12px;line-height:1.7;text-align:center}
.home-head,.section-heading,.home-actions,.charter-card,.focus-panel__head,.focus-panel__head h3,.focus-item,.latest-review,.memory-panel__head,.memory-list button{display:flex;align-items:center}
.home-head,.section-heading,.charter-card,.focus-panel__head,.memory-panel__head{justify-content:space-between}
.home-head{gap:18px}.home-actions{gap:8px}.today-section,.memory-section{margin-bottom:28px}.section-heading{gap:12px;margin-bottom:12px}.section-heading h2,.quick-section h2{margin:0;font-size:16px}.section-heading p{margin:3px 0 0;color:var(--ws-text-secondary-color);font-size:12px}
.charter-card{gap:14px;margin-bottom:12px;padding:16px;border:1px solid #b9d7ff;border-radius:var(--ws-radius-lg);background:linear-gradient(105deg,#f3f8ff 0%,#fff 78%)}.charter-card.is-empty{border-style:dashed}.charter-card__icon,.latest-review__icon{display:grid;flex:none;width:38px;height:38px;place-items:center;border-radius:10px;background:var(--ws-edit-color);color:var(--ws-primary-color)}.charter-card__body{flex:1;min-width:0}.charter-card__eyebrow{color:var(--ws-primary-color);font-size:11px;font-weight:700}.charter-card h3{margin:4px 0;font-size:15px;overflow-wrap:anywhere}.charter-card p{margin:0;color:var(--ws-text-secondary-color);font-size:12px}
.growth-stats,.memory-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}.growth-stat,.memory-stats button{display:grid;gap:3px;padding:12px 14px;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius-lg);background:var(--ws-body-bg);color:var(--ws-text-primary-color);text-align:left}.growth-stat:hover,.memory-stats button:hover{border-color:var(--ws-primary-color)}.growth-stat span,.memory-stats strong{font-size:21px}.growth-stat small,.memory-stats span{color:var(--ws-text-secondary-color);font-size:11px}.memory-stats .is-warn strong{color:var(--ws-danger-color)}
.focus-grid,.memory-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.focus-panel,.memory-panel{overflow:hidden;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius-lg);background:var(--ws-body-bg)}.focus-panel__head,.memory-panel__head{min-height:48px;padding:11px 14px;border-bottom:1px solid var(--ws-border-color-3)}.focus-panel__head h3{gap:7px;margin:0;font-size:14px}.focus-panel__head>span{color:var(--ws-text-secondary-color);font-size:12px}.focus-list{padding:5px}.focus-item{width:100%;gap:8px;padding:11px 10px;border:0;border-radius:var(--ws-radius);background:transparent;color:var(--ws-text-primary-color);text-align:left}.focus-item:hover{background:var(--ws-card-bg)}.focus-item__main{display:grid;flex:1;min-width:0;gap:4px}.focus-item__main strong,.focus-item__main small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.focus-item__main strong{font-size:13px}.focus-item__main small{color:var(--ws-text-secondary-color);font-size:11px}.focus-item__state{flex:none;padding:3px 7px;border-radius:999px;background:var(--ws-warning-color-bd);color:#9a5b00;font-size:11px}.focus-item__state.is-overdue{background:var(--ws-danger-color-bd);color:var(--ws-danger-color)}.focus-item__state.is-review{background:var(--ws-edit-color);color:var(--ws-primary-color)}.focus-panel :deep(.ws-empty){min-height:178px;padding:24px 16px}.focus-more{margin:9px 0 0;color:var(--ws-text-secondary-color);font-size:11px;text-align:right}.focus-more button{padding:0;border:0;background:transparent;color:var(--ws-primary-color);font:inherit}
.latest-review{align-items:flex-start;gap:12px;margin-top:12px;padding:14px 16px;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius-lg);background:var(--ws-card-bg)}.latest-review__label{color:var(--ws-text-secondary-color);font-size:11px;font-weight:700}.latest-review h3{margin:4px 0;font-size:13px;line-height:1.55}.latest-review p{margin:0;color:var(--ws-text-color);font-size:12px}.latest-review small{display:inline-block;margin-top:5px;color:var(--ws-text-secondary-color)}
.memory-panel__head h3{margin:0;font-size:14px}.memory-list{margin:0;padding:5px;list-style:none}.memory-list button{width:100%;gap:9px;padding:9px 10px;border:0;border-radius:var(--ws-radius);background:transparent;text-align:left}.memory-list button:hover{background:var(--ws-card-bg)}.memory-list__title{flex:1;min-width:0;overflow:hidden;color:var(--ws-text-primary-color);font-size:13px;text-overflow:ellipsis;white-space:nowrap}.memory-list__time{flex:none;color:var(--ws-text-secondary-color);font-size:11px}
.quick-section h2{margin-bottom:12px}.quick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:10px}.quick-grid button{display:grid;justify-items:start;gap:5px;padding:14px;border:1px solid var(--ws-border-color);border-radius:var(--ws-radius-lg);background:var(--ws-body-bg);color:var(--ws-text-primary-color);text-align:left}.quick-grid button:hover{border-color:var(--ws-primary-color);background:var(--ws-edit-color)}.quick-grid svg{color:var(--ws-primary-color)}.quick-grid strong{font-size:13px}.quick-grid span{color:var(--ws-text-secondary-color);font-size:11px;line-height:1.5}
@media(max-width:900px){.growth-stats,.memory-stats{grid-template-columns:repeat(2,1fr)}.focus-grid,.memory-grid{grid-template-columns:1fr}}
@media(max-width:640px){.home-head{align-items:stretch;flex-direction:column}.home-actions{align-items:stretch}.home-actions :deep(.ws-btn){flex:1}.charter-card{align-items:flex-start;flex-wrap:wrap}.charter-card__body{flex-basis:calc(100% - 54px)}.charter-card>:deep(.ws-btn){margin-left:52px}.growth-stats,.memory-stats{gap:8px}.memory-list__time{display:none}}
</style>
