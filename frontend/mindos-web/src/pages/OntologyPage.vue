<script setup lang="ts">
// 我的本体：默认「全景」——一张图看懂知君眼中的我；「列表」保留六个抽屉 + 收件箱 + 裁决。
// 全景里点一个点，右侧打开这条理解的卡片，所有动作（确认 / 修正 / 撤回 / 可带走）都在卡片上。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ApiError,
  createClaim,
  getInbox,
  getOntologyStats,
  listClaims,
  reviewClaim,
  type Claim,
  type Layer,
  type OntologyStats,
  type ReviewAction,
  type Section,
} from '@/services/api'
import { useToast } from '@/composables/useToast'
import { createSessionGate } from '@/composables/sessionGate'
import { LAYER_META, SECTIONS, sectionLabel } from '@/shared/ontology'
import SectionNav, { type NavKey } from '@/components/ontology/SectionNav.vue'
import ClaimCard from '@/components/ontology/ClaimCard.vue'
import ProposalsPanel from '@/components/ontology/ProposalsPanel.vue'
import SelfMap from '@/components/ontology/SelfMap.vue'
import OntologyExplainer from '@/components/ontology/OntologyExplainer.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { X } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const gate = createSessionGate()
const mapGate = createSessionGate()

const stats = ref<OntologyStats | null>(null)
const items = ref<Claim[]>([])
const loading = ref(true)
const error = ref('')
const busy = reactive<Record<string, boolean>>({})

const showCreate = ref(false)
const creating = ref(false)
const newContent = ref('')
const newSection = ref<Section>('who')
const newLayer = ref<'self_declared' | 'aspirational'>('self_declared')

// ---- 全景 / 列表 视图（记住上次选择）
const VIEW_KEY = 'zhijun.me.view'
type ViewMode = 'map' | 'list'
function readView(): ViewMode {
  try {
    return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'map'
  } catch {
    return 'map'
  }
}
const view = ref<ViewMode>(readView())
function setView(v: ViewMode) {
  view.value = v
  try {
    localStorage.setItem(VIEW_KEY, v)
  } catch {
    // 无法持久化时忽略
  }
}

const mapItems = ref<Claim[]>([])
const mapLoading = ref(false)
const mapError = ref('')
const selected = ref<Claim | null>(null)
const layerFilter = ref<Set<Layer> | null>(null)
const focusSection = ref<Section | null>(null)
const LAYER_CHIPS = (Object.keys(LAYER_META) as Layer[]).map((key) => ({ key, label: LAYER_META[key].label }))

const SECTION_KEYS = new Set<string>(SECTIONS.map((s) => s.key))

const current = computed<NavKey>(() => {
  if (route.path.endsWith('/inbox')) return 'inbox'
  const q = route.query.section
  if (q === 'proposals') return 'proposals'
  return typeof q === 'string' && SECTION_KEYS.has(q) ? (q as Section) : 'who'
})

const isSectionView = computed(() => current.value !== 'inbox' && current.value !== 'proposals')
const showMap = computed(() => view.value === 'map' && isSectionView.value)

const heading = computed(() => {
  if (current.value === 'inbox') return '知君最近学到的'
  if (current.value === 'proposals') return '需要你裁决'
  if (showMap.value) return '本体全景'
  return sectionLabel(current.value)
})
const hint = computed(() => {
  if (current.value === 'inbox') return '这些是知君从对话里提出、还没经你确认的理解。确认后才会成为它对你的认识；否定后永远不会再出现。'
  if (current.value === 'proposals') return '知君整理时发现的疑问：两个名字是不是同一个人？两条理解是不是矛盾？它不会自己拍板，只等你定。'
  if (showMap.value) return '离中心越近越可信。朱砂虚线是信任边界：外面的空心点是知君的猜测，你点头它才进来。点一个点看细节，点分区名只看那一片。'
  return SECTIONS.find((s) => s.key === current.value)?.hint ?? ''
})

function friendlyError(err: unknown, fallback: string): string {
  if (err instanceof ApiError && err.status === 409) return '这条理解的状态已经变化，请刷新后再试。'
  return err instanceof Error && err.message ? err.message : fallback
}

async function loadStats() {
  try {
    stats.value = await getOntologyStats()
  } catch {
    // 统计不可用不阻塞列表
  }
}

async function load() {
  const session = gate.next()
  if (current.value === 'proposals') {
    items.value = []
    loading.value = false
    error.value = ''
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res =
      current.value === 'inbox'
        ? await getInbox(50)
        : await listClaims({ section: current.value, trust: ['confirmed', 'working'], limit: 200 })
    if (!gate.isCurrent(session)) return
    items.value = res.items
  } catch (err) {
    if (!gate.isCurrent(session)) return
    error.value = friendlyError(err, '本体加载失败')
  } finally {
    if (gate.isCurrent(session)) loading.value = false
  }
}

async function loadMap() {
  const session = mapGate.next()
  mapLoading.value = true
  mapError.value = ''
  try {
    const res = await listClaims({ trust: ['confirmed', 'working'], limit: 1000 })
    if (!mapGate.isCurrent(session)) return
    mapItems.value = res.items
    if (selected.value) selected.value = res.items.find((c) => c.id === selected.value?.id) ?? null
  } catch (err) {
    if (!mapGate.isCurrent(session)) return
    mapError.value = friendlyError(err, '本体全景加载失败')
  } finally {
    if (mapGate.isCurrent(session)) mapLoading.value = false
  }
}

function select(key: NavKey) {
  if (key === 'inbox') router.push('/me/inbox')
  else router.push({ path: '/me', query: { section: key } })
}

function onSectionFocus(section: Section | null) {
  focusSection.value = section
  if (section && current.value !== section) router.push({ path: '/me', query: { section } })
}

function toggleLayer(key: Layer | null) {
  if (key === null) {
    layerFilter.value = null
    return
  }
  const next = new Set(layerFilter.value ?? [])
  if (next.has(key)) next.delete(key)
  else next.add(key)
  layerFilter.value = next.size ? next : null
}

function onProposalsChanged() {
  void loadStats()
}

watch(current, () => {
  void load()
  if (isSectionView.value && current.value !== focusSection.value) focusSection.value = null
})

watch(showMap, (on) => {
  if (on && !mapItems.value.length) void loadMap()
})

function applyReviewResult(claim: Claim, action: ReviewAction, finalClaim: Claim) {
  const keepInList = current.value === 'inbox' ? finalClaim.trustState === 'working' : finalClaim.trustState === 'working' || finalClaim.trustState === 'confirmed'
  const idx = items.value.findIndex((c) => c.id === claim.id)
  if (idx >= 0) {
    if (keepInList) items.value.splice(idx, 1, finalClaim)
    else items.value.splice(idx, 1)
  }
  const keepOnMap = finalClaim.trustState === 'working' || finalClaim.trustState === 'confirmed'
  const midx = mapItems.value.findIndex((c) => c.id === claim.id)
  if (midx >= 0) {
    if (keepOnMap) mapItems.value.splice(midx, 1, finalClaim)
    else mapItems.value.splice(midx, 1)
  } else if (keepOnMap && finalClaim.id !== claim.id) {
    mapItems.value.push(finalClaim)
  }
  if (selected.value?.id === claim.id) selected.value = keepOnMap ? finalClaim : null
  void action
}

async function onReview(claim: Claim, action: ReviewAction, editedContent?: string) {
  busy[claim.id] = true
  try {
    const result = await reviewClaim(claim.id, { action, editedContent, surface: 'ontology_page' })
    const finalClaim = result.replacedBy ?? result.claim
    applyReviewResult(claim, action, finalClaim)
    const label: Record<ReviewAction, string> = {
      confirm: '已确认',
      partial: '已保存修正',
      context_only: '已限定为只适用于那件事',
      reject: '已否定，知君不会再提',
      defer: '先不保存',
      retract: '已撤回',
      reaffirm: '已重申',
    }
    toast({ type: 'success', message: label[action] })
    void loadStats()
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '操作失败') })
  } finally {
    delete busy[claim.id]
  }
}

async function submitCreate() {
  const content = newContent.value.trim()
  if (!content) return
  creating.value = true
  try {
    const created = await createClaim({ content, section: newSection.value, layer: newLayer.value })
    if (current.value === newSection.value) items.value = [created, ...items.value]
    mapItems.value = [created, ...mapItems.value]
    newContent.value = ''
    showCreate.value = false
    toast({ type: 'success', message: `已记入「${sectionLabel(created.section)}」` })
    void loadStats()
  } catch (err) {
    toast({ type: 'error', message: friendlyError(err, '保存失败') })
  } finally {
    creating.value = false
  }
}

onMounted(() => {
  void loadStats()
  void load()
  if (showMap.value) void loadMap()
})

onBeforeUnmount(() => {
  gate.invalidate()
  mapGate.invalidate()
})
</script>

<template>
  <div class="page zj-me">
    <div class="page-head">
      <h1>我的本体</h1>
      <p>知君目前对你的认识，全部可核对、可修正、可撤回。标签说明每条理解从哪里来。</p>
    </div>

    <div class="zj-me__grid">
      <SectionNav :stats="stats" :current="current" @select="select" />

      <section class="zj-me__main">
        <header class="zj-me__head">
          <div>
            <h2>{{ heading }}</h2>
            <p>{{ hint }}</p>
          </div>
          <div class="zj-me__head-actions">
            <div v-if="isSectionView" class="zj-me__viewtoggle" role="group" aria-label="视图">
              <button type="button" :class="{ 'is-on': view === 'map' }" :aria-pressed="view === 'map'" @click="setView('map')">全景</button>
              <button type="button" :class="{ 'is-on': view === 'list' }" :aria-pressed="view === 'list'" @click="setView('list')">列表</button>
            </div>
            <BaseButton v-if="current !== 'proposals'" size="sm" @click="showCreate = !showCreate">{{ showCreate ? '收起' : '补一条' }}</BaseButton>
          </div>
        </header>

        <ProposalsPanel v-if="current === 'proposals'" @changed="onProposalsChanged" />

        <form v-if="showCreate && current !== 'proposals'" class="zj-me__create" @submit.prevent="submitCreate">
          <label class="zj-me__field">
            <span>这条理解属于</span>
            <select v-model="newSection">
              <option v-for="s in SECTIONS" :key="s.key" :value="s.key">{{ s.label }}</option>
            </select>
          </label>
          <label class="zj-me__field">
            <span>它是</span>
            <select v-model="newLayer">
              <option value="self_declared">我现在就是这样（你告诉我的）</option>
              <option value="aspirational">我想成为这样（你想成为的）</option>
            </select>
          </label>
          <label class="zj-me__field zj-me__field--wide">
            <span>一句话，120 字以内</span>
            <textarea v-model="newContent" rows="2" maxlength="120" placeholder="例如：我做决定前习惯先问清楚最坏情况" />
          </label>
          <div class="zj-me__create-actions">
            <BaseButton type="submit" variant="primary" size="sm" :loading="creating" :disabled="!newContent.trim()">记入本体</BaseButton>
          </div>
        </form>

        <!-- 全景 -->
        <div v-if="showMap" class="zj-me__map" :class="{ 'has-panel': !!selected }">
          <div class="zj-me__map-main">
            <div class="zj-me__chips" role="group" aria-label="按来源筛选">
              <button type="button" class="zj-me__chip" :class="{ 'is-on': !layerFilter }" :aria-pressed="!layerFilter" @click="toggleLayer(null)">全部</button>
              <button
                v-for="c in LAYER_CHIPS"
                :key="c.key"
                type="button"
                class="zj-me__chip"
                :class="{ 'is-on': !!layerFilter?.has(c.key) }"
                :aria-pressed="!!layerFilter?.has(c.key)"
                @click="toggleLayer(c.key)"
              >
                {{ c.label }}
              </button>
              <span v-if="focusSection" class="zj-me__focus">
                只看「{{ sectionLabel(focusSection) }}」
                <button type="button" class="zj-me__focus-clear" @click="onSectionFocus(null)">看全部</button>
              </span>
            </div>
            <ErrorState v-if="mapError" :message="mapError" @retry="loadMap" />
            <div v-else-if="mapLoading && !mapItems.length" class="loading-state">正在读取…</div>
            <template v-else>
              <SelfMap
                :claims="mapItems"
                :stats="stats"
                :selected-id="selected?.id ?? null"
                :layer-filter="layerFilter"
                :focus-section="focusSection"
                @select="(c) => (selected = c)"
                @section-focus="onSectionFocus"
              />
              <div v-if="!mapItems.length" class="zj-me__explainer">
                <OntologyExplainer compact />
              </div>
            </template>
          </div>
          <aside v-if="selected" class="zj-me__panel" data-testid="selfmap-panel" aria-label="这条理解">
            <div class="zj-me__panel-head">
              <span>{{ sectionLabel(selected.section) }}</span>
              <button type="button" class="zj-me__panel-close" aria-label="关闭" @click="selected = null"><X :size="16" aria-hidden="true" /></button>
            </div>
            <ClaimCard :claim="selected" :busy="!!busy[selected.id]" show-section @review="(action, edited) => onReview(selected!, action, edited)" />
          </aside>
        </div>

        <!-- 列表 -->
        <template v-else>
          <div v-if="current === 'proposals'" />
          <div v-else-if="loading" class="loading-state">正在读取…</div>
          <ErrorState v-else-if="error" :message="error" @retry="load" />
          <EmptyState
            v-else-if="!items.length"
            :title="current === 'inbox' ? '暂时没有待确认的理解' : '知君还不够了解你'"
            :description="current === 'inbox' ? '聊几句之后，知君提出的新理解会出现在这里。' : '先去聊几句，或者用「补一条」直接告诉它。'"
          >
            <template #action>
              <RouterLink to="/" class="zj-me__link">去对话</RouterLink>
            </template>
          </EmptyState>
          <div v-else class="zj-me__list">
            <ClaimCard
              v-for="c in items"
              :key="c.id"
              :claim="c"
              :busy="!!busy[c.id]"
              :show-section="current === 'inbox'"
              @review="(action, edited) => onReview(c, action, edited)"
            />
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.zj-me__grid {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
.zj-me__main {
  min-width: 0;
}
.zj-me__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.zj-me__head h2 {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: 20px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-me__head p {
  margin: 4px 0 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-me__head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}
.zj-me__viewtoggle {
  display: inline-flex;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  overflow: hidden;
}
.zj-me__viewtoggle button {
  padding: 5px 12px;
  border: none;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-me__viewtoggle button.is-on {
  background: var(--ws-primary-color, #a6452e);
  color: #fffcf6;
}
.zj-me__viewtoggle button:focus-visible {
  outline: 2px solid var(--ws-primary-color, #a6452e);
  outline-offset: -2px;
}
.zj-me__create {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;
  margin-bottom: 16px;
  padding: 14px 16px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
}
.zj-me__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-me__field--wide {
  grid-column: 1 / -1;
}
.zj-me__field select,
.zj-me__field textarea {
  padding: 8px 10px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  font-size: 14px;
}
.zj-me__field textarea {
  resize: vertical;
}
.zj-me__field select:focus,
.zj-me__field textarea:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.zj-me__create-actions {
  grid-column: 1 / -1;
  display: flex;
  justify-content: flex-end;
}
.zj-me__list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.zj-me__link {
  font-weight: 600;
}

/* ---- 全景 ---- */
.zj-me__map {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}
.zj-me__map.has-panel {
  grid-template-columns: minmax(0, 1fr) minmax(280px, 34%);
}
.zj-me__map-main {
  min-width: 0;
}
.zj-me__chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}
.zj-me__chip {
  padding: 4px 11px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: transparent;
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-me__chip.is-on {
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-primary-color, #a6452e);
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
}
.zj-me__chip:focus-visible {
  outline: 2px solid var(--ws-primary-color, #a6452e);
  outline-offset: 1px;
}
.zj-me__focus {
  margin-left: auto;
  font-size: 12px;
  color: var(--ws-primary-color, #a6452e);
}
.zj-me__focus-clear {
  margin-left: 6px;
  border: none;
  background: transparent;
  color: inherit;
  font-family: inherit;
  font-size: 12px;
  text-decoration: underline;
  cursor: pointer;
}
.zj-me__explainer {
  display: flex;
  justify-content: center;
  margin-top: 12px;
}
.zj-me__panel {
  position: sticky;
  top: 12px;
  padding: 10px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #f3efe6);
}
.zj-me__panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  font-family: var(--ws-font-display, serif);
  font-size: 14px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-me__panel-close {
  display: inline-flex;
  padding: 4px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
}
.zj-me__panel-close:hover {
  background: var(--ws-body-bg, #fffcf6);
}
@media (max-width: 1023px) {
  .zj-me__map.has-panel {
    grid-template-columns: minmax(0, 1fr);
  }
  .zj-me__panel {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    max-height: 70vh;
    overflow: auto;
    border-radius: var(--ws-radius-lg, 8px) var(--ws-radius-lg, 8px) 0 0;
    box-shadow: var(--ws-shadow-lg, 0 16px 48px rgba(0, 0, 0, 0.18));
  }
}
@media (max-width: 767px) {
  .zj-me__grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .zj-me__create {
    grid-template-columns: minmax(0, 1fr);
  }
  .zj-me__head {
    flex-direction: column;
  }
}
</style>
