<script setup lang="ts">
// 统一搜索：分类 Tab + 结果计数 + 相关度 + 片段高亮 + 来源 Badge（B3 FE-UI-015）
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Play, RotateCw, Search } from 'lucide-vue-next'
import { api, type UnifiedSearchResult, type VisualMatchMode } from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { materialStatusLabel } from '@/shared/status'
import { useToast } from '@/composables/useToast'
import { createSessionGate } from '@/composables/sessionGate'
import { cardBodyPreview } from '@/shared/cardContent'

// 视觉命中依据类别标签；当前阶段恒为 visual（CLIP 图文语义），
// ocr/caption 为依据类别预留——绝不把 OCR 命中误标为视觉语义。
function visualModeLabel(mode: VisualMatchMode) {
  if (mode === 'visual') return '图片语义'
  if (mode === 'ocr') return 'OCR 文本'
  return '内嵌说明'
}

const route = useRoute()
const router = useRouter()
const toast = useToast()
const query = ref('')
const loading = ref(false)
const error = ref('')
const result = ref<UnifiedSearchResult | null>(null)
const activeTab = ref<'all' | 'knowledge' | 'available' | 'unavailable'>('all')
const actingMaterialId = ref('')
const searchGate = createSessionGate()

function snippet(value: string) {
  return cardBodyPreview(value, 500) || '暂无可显示片段'
}

function escapeHtml(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function escapeRegex(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// 片段高亮：转义 HTML 后，用 <mark> 包裹查询词
function highlightedSnippet(value: string) {
  const clean = snippet(value)
  const escaped = escapeHtml(clean)
  const terms = query.value.trim().split(/\s+/).filter(Boolean)
  if (!terms.length) return escaped
  let out = escaped
  for (const term of terms) {
    out = out.replace(new RegExp(`(${escapeRegex(escapeHtml(term))})`, 'gi'), '<mark>$1</mark>')
  }
  return out
}

const tabCounts = computed(() => ({
  all: result.value?.total ?? 0,
  knowledge: result.value?.knowledge.length ?? 0,
  available: result.value?.materials.length ?? 0,
  unavailable: result.value?.unavailableMaterials.length ?? 0,
  visual: result.value?.visualMaterials.length ?? 0,
}))

// 视觉命中只在「全部 / 原材料」Tab 展示：计数需与实际展示一致。
// 视觉分属独立向量空间，不并入 API 的 total，只在 Tab 文案上追加「· N 视觉」。
function countWithVisual(textCount: number) {
  const visual = tabCounts.value.visual
  return visual > 0 ? `${textCount} · ${visual} 视觉` : `${textCount}`
}

const visibleKnowledge = computed(() =>
  activeTab.value === 'available' || activeTab.value === 'unavailable' ? [] : (result.value?.knowledge ?? []),
)
const visibleMaterials = computed(() =>
  activeTab.value === 'knowledge' || activeTab.value === 'unavailable' ? [] : (result.value?.materials ?? []),
)
const visibleUnavailableMaterials = computed(() =>
  activeTab.value === 'unavailable' ? (result.value?.unavailableMaterials ?? []) : [],
)
// 视觉命中属于图片材料分组：全部/原材料 Tab 展示，知识成品 Tab 隐藏
const visibleVisualMaterials = computed(() =>
  activeTab.value === 'knowledge' || activeTab.value === 'unavailable' ? [] : (result.value?.visualMaterials ?? []),
)

// 缩略图加载失败时隐藏占位（纯图可能因预览端点暂不可用）
function hideThumb(event: Event) {
  const img = event.target as HTMLImageElement
  img.style.display = 'none'
}

async function runSearch() {
  const value = query.value.trim()
  if (!value) return
  const requestSession = searchGate.next()
  loading.value = true
  error.value = ''
  try {
    const response = await api.search(value)
    if (!searchGate.isCurrent(requestSession)) return
    result.value = response
  } catch (e) {
    if (searchGate.isCurrent(requestSession)) error.value = e instanceof Error ? e.message : '搜索失败'
  } finally {
    if (searchGate.isCurrent(requestSession)) loading.value = false
  }
}

async function runUnavailableAction(materialId: string, action: 'resume' | 'retry') {
  if (actingMaterialId.value) return
  actingMaterialId.value = materialId
  try {
    if (action === 'resume') await api.resumeUpload(materialId)
    else await api.retryUpload(materialId)
    toast({ type: 'success', message: action === 'resume' ? '已继续处理资料' : '已重新提交处理任务' })
    await runSearch()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '操作失败' })
  } finally {
    actingMaterialId.value = ''
  }
}

// 从 URL query 同步输入并搜索：顶栏与页面提交都以 query 参数为准，刷新页面不丢失关键词
function syncFromQuery() {
  const q = typeof route.query.query === 'string' ? route.query.query : ''
  query.value = q
  if (q) runSearch()
  else result.value = null
}

function submit() {
  const value = query.value.trim()
  if (!value) return
  if ((route.query.query ?? '') !== value) {
    router.push({ path: '/search', query: { query: value } })
  } else {
    runSearch()
  }
}

watch(() => route.query.query, syncFromQuery)
onMounted(syncFromQuery)
onBeforeUnmount(() => searchGate.invalidate())
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1>搜索记忆</h1>
      <p>关键词与自然语言检索，优先显示知识档案，再显示原材料证据。</p>
    </div>

    <form class="ws-search" role="search" @submit.prevent="submit">
      <Search class="ws-search__icon" :size="18" aria-hidden="true" />
      <input
        v-model="query"
        class="ws-search__input"
        type="search"
        placeholder="输入关键词或自然语言问题"
        autofocus
      >
      <BaseButton variant="primary" type="submit" :loading="loading" :disabled="!query.trim()">
        {{ loading ? '搜索中…' : '搜索' }}
      </BaseButton>
    </form>

    <ErrorState v-if="error" :message="error" retry-label="重试" @retry="runSearch" />
    <div v-else-if="loading" class="loading-state">正在搜索…</div>

    <template v-else-if="result">
      <div v-if="!result.total && !result.visualMaterials.length && !result.unavailableMaterials.length" class="ws-result">
        <EmptyState
          title="未找到相关内容"
          description="可尝试更简短的关键词，或确认资料已经处理完成。"
        />
      </div>

      <div v-else class="ws-result">
        <!-- 分类 Tab -->
        <div class="ws-tabs" role="group" aria-label="结果分类">
          <button class="ws-tab" :class="{ 'is-active': activeTab === 'all' }" type="button" @click="activeTab = 'all'">
            全部（{{ countWithVisual(tabCounts.all) }}）
          </button>
          <button class="ws-tab" :class="{ 'is-active': activeTab === 'knowledge' }" type="button" @click="activeTab = 'knowledge'">
            知识档案（{{ tabCounts.knowledge }}）
          </button>
          <button class="ws-tab" :class="{ 'is-active': activeTab === 'available' }" type="button" @click="activeTab = 'available'">
            可用材料（{{ countWithVisual(tabCounts.available) }}）
          </button>
          <button class="ws-tab" :class="{ 'is-active': activeTab === 'unavailable' }" type="button" @click="activeTab = 'unavailable'">
            不可用材料（{{ tabCounts.unavailable }}）
          </button>
        </div>

        <div class="ws-total">“{{ result.query }}”共找到 {{ result.total }} 项文本结果</div>

        <div v-if="!result.capabilities.visualSearch" class="ws-visual-hint">
          视觉检索暂不可用，当前仅展示文本检索结果。
        </div>

        <section v-if="visibleKnowledge.length" class="ws-section">
          <h2>知识档案</h2>
          <div class="ws-results">
            <button
              v-for="item in visibleKnowledge"
              :key="item.knowledgeId"
              class="ws-result-card"
              type="button"
              @click="router.push(`/knowledge/${item.knowledgeId}`)"
            >
              <span class="ws-result-card__head">
                <span class="ws-kind ws-kind--knowledge">知识卡片</span>
                <span class="ws-result-card__score">相关度 {{ Math.round(item.score * 100) }}%</span>
              </span>
              <strong class="ws-result-card__title">{{ item.title }}</strong>
              <span class="ws-result-card__snippet" v-html="highlightedSnippet(item.snippet)" />
            </button>
          </div>
        </section>

        <section v-if="visibleMaterials.length" class="ws-section">
          <h2>可用材料</h2>
          <div class="ws-results">
            <button
              v-for="item in visibleMaterials"
              :key="item.materialId"
              class="ws-result-card"
              type="button"
              @click="router.push(`/materials/${item.materialId}`)"
            >
              <span class="ws-result-card__head">
                <span class="ws-kind ws-kind--material">{{ item.fileType === 'image' ? '图片 OCR' : item.fileType === 'audio' ? '音频' : '文档' }}</span>
                <span class="ws-result-card__score">相关度 {{ Math.round(item.score * 100) }}%</span>
              </span>
              <strong class="ws-result-card__title">{{ item.title }}</strong>
              <span class="ws-result-card__snippet" v-html="highlightedSnippet(item.snippet)" />
            </button>
          </div>
        </section>

        <section v-if="visibleUnavailableMaterials.length" class="ws-section">
          <h2>不可用材料</h2>
          <div class="ws-results">
            <article v-for="item in visibleUnavailableMaterials" :key="item.materialId" class="ws-unavailable-card">
              <button class="ws-unavailable-card__main" type="button" @click="router.push(`/materials/${item.materialId}`)">
                <span class="ws-result-card__head">
                  <span class="ws-kind ws-kind--unavailable">{{ materialStatusLabel(item.status) }}</span>
                  <span class="ws-result-card__score">暂不参与检索</span>
                </span>
                <strong class="ws-result-card__title">{{ item.title }}</strong>
                <span class="ws-unavailable-card__reason">{{ item.reason }}</span>
              </button>
              <div v-if="item.actions.length" class="ws-unavailable-card__actions">
                <BaseButton v-for="action in item.actions" :key="action" variant="secondary" size="sm" :loading="actingMaterialId === item.materialId" @click="runUnavailableAction(item.materialId, action)">
                  <Play v-if="action === 'resume'" :size="14" aria-hidden="true" />
                  <RotateCw v-else :size="14" aria-hidden="true" />
                  {{ action === 'resume' ? '继续处理' : '重试处理' }}
                </BaseButton>
              </div>
            </article>
          </div>
        </section>

        <!-- P14-08：图片语义命中（Chinese-CLIP 以文搜图），与文本命中分开分组展示，
             分数为 CLIP 图文相似度，不可与 BGE 文本分混合排序。 -->
        <section v-if="visibleVisualMaterials.length" class="ws-section">
          <h2>视觉命中（图片语义）</h2>
          <div class="ws-visual-grid">
            <button
              v-for="item in visibleVisualMaterials"
              :key="item.materialId"
              class="ws-visual-card"
              type="button"
              @click="router.push(`/materials/${item.materialId}`)"
            >
              <img
                class="ws-visual-card__thumb"
                :src="item.previewUrl"
                :alt="`${item.title} 缩略图`"
                loading="lazy"
                @error="hideThumb"
              >
              <span class="ws-visual-card__body">
                <span class="ws-visual-card__head">
                  <span class="ws-kind ws-kind--visual">图片语义</span>
                  <span class="ws-result-card__score">相似度 {{ Math.round(item.score * 100) }}%</span>
                </span>
                <strong class="ws-visual-card__title">{{ item.title }}</strong>
                <span class="ws-visual-card__basis">
                  <span class="ws-kind ws-kind--visual-mode">{{ visualModeLabel(item.matchMode) }}</span>
                  <span v-if="item.snippet" class="ws-visual-card__snippet">{{ item.snippet }}</span>
                  <span v-else class="ws-visual-card__snippet ws-visual-card__snippet--empty">纯图无文字，依据为图片内容语义</span>
                </span>
              </span>
            </button>
          </div>
        </section>

        <EmptyState
          v-if="activeTab !== 'all' && !visibleKnowledge.length && !visibleMaterials.length && !visibleUnavailableMaterials.length && !visibleVisualMaterials.length"
          title="该分类下暂无结果"
          description="可尝试切换分类或更换关键词。"
        />
      </div>
    </template>

    <EmptyState
      v-else
      title="搜索知识与资料"
      description="输入文件名、正文关键词或自然语言问题开始搜索。"
    />
  </div>
</template>

<style scoped>
/* 搜索栏 */
.ws-search {
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 720px;
  margin-bottom: 20px;
  padding: 0 14px;
  height: 42px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  transition: border-color 0.15s;
}
.ws-search:focus-within {
  border-color: var(--ws-input-focus-border-color, #a6452e);
}

.ws-search__icon {
  flex-shrink: 0;
  color: var(--ws-text-secondary-color, #686b66);
}

.ws-search__input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  font-size: 14px;
}
.ws-search__input::placeholder {
  color: var(--ws-text-placeholder-color, #a3a69f);
}

.ws-result {
  max-width: 760px;
}

/* 分类 Tab */
.ws-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}

.ws-tab {
  padding: 6px 14px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: var(--ws-body-bg, #fff);
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s,
    background 0.15s;
}
.ws-tab:hover {
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-text-primary-color, #1d211f);
}
.ws-tab.is-active {
  border-color: var(--ws-primary-color, #a6452e);
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}

.ws-total {
  margin-bottom: 14px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}

/* CLIP 未就绪时的显式降级提示（区别于“未命中”） */
.ws-visual-hint {
  margin: -6px 0 14px;
  padding: 8px 12px;
  border: 1px dashed var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.03));
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}

/* 视觉命中（图片语义） */
.ws-visual-grid {
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.ws-visual-card {
  display: flex;
  align-items: stretch;
  gap: 14px;
  width: 100%;
  padding: 10px 16px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    border-color 0.15s,
    background 0.15s;
}
.ws-visual-card:hover {
  border-color: var(--ws-primary-color, #a6452e);
  background: var(--ws-edit-color, rgba(0, 119, 255, 0.04));
}

.ws-visual-card__thumb {
  flex-shrink: 0;
  width: 96px;
  height: 96px;
  align-self: center;
  border-radius: var(--ws-radius-md, 6px);
  object-fit: cover;
  background: var(--ws-edit-color, rgba(0, 119, 255, 0.05));
}

.ws-visual-card__body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}

.ws-visual-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.ws-visual-card__title {
  font-size: 14px;
}

.ws-visual-card__basis {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.ws-visual-card__snippet {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-color, #3c403d);
  overflow-wrap: anywhere;
}
.ws-visual-card__snippet--empty {
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-style: normal;
}

.ws-kind--visual {
  background: var(--ws-warn-color-bd, rgba(247, 160, 10, 0.08));
  color: var(--ws-warn-color, #f7a00a);
}
.ws-kind--visual-mode {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  color: var(--ws-main-color, #a6452e);
}

/* 结果区 */
.ws-section {
  margin-bottom: 24px;
}
.ws-section h2 {
  margin: 0 0 10px;
  font-size: 14px;
  color: var(--ws-text-primary-color, #1d211f);
}

.ws-results {
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.ws-result-card {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  width: 100%;
  padding: 14px 16px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    border-color 0.15s,
    background 0.15s;
}
.ws-result-card:hover {
  border-color: var(--ws-primary-color, #a6452e);
  background: var(--ws-edit-color, rgba(0, 119, 255, 0.04));
}

.ws-unavailable-card {
  display: flex;
  align-items: stretch;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
}
.ws-unavailable-card__main {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}
.ws-unavailable-card__reason {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
  overflow-wrap: anywhere;
}
.ws-unavailable-card__actions {
  display: flex;
  align-items: center;
  flex: none;
}
.ws-kind--unavailable {
  background: var(--ws-warn-color-bd, rgba(247, 160, 10, 0.08));
  color: var(--ws-warn-color, #f7a00a);
}

.ws-result-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.ws-kind {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
}
.ws-kind--knowledge {
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  color: var(--ws-main-color, #a6452e);
}
.ws-kind--material {
  background: var(--ws-success-color-bd, rgba(18, 205, 61, 0.06));
  color: var(--ws-success-color, #4a7c59);
}

.ws-result-card__score {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}

.ws-result-card__title {
  font-size: 14px;
}

.ws-result-card__snippet {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-color, #3c403d);
  overflow-wrap: anywhere;
}
.ws-result-card__snippet :deep(mark) {
  background: rgba(255, 213, 79, 0.45);
  color: inherit;
  border-radius: 2px;
}
</style>
