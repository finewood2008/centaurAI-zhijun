<script setup lang="ts">
// 我的本体：六个抽屉 + 「知君最近学到的」收件箱。每条理解带层标签、信任状态、来源深链与动作。
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
  type OntologyStats,
  type ReviewAction,
  type Section,
} from '@/services/api'
import { useToast } from '@/composables/useToast'
import { createSessionGate } from '@/composables/sessionGate'
import { SECTIONS, sectionLabel } from '@/shared/ontology'
import SectionNav, { type NavKey } from '@/components/ontology/SectionNav.vue'
import ClaimCard from '@/components/ontology/ClaimCard.vue'
import ProposalsPanel from '@/components/ontology/ProposalsPanel.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const gate = createSessionGate()

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

const SECTION_KEYS = new Set<string>(SECTIONS.map((s) => s.key))

const current = computed<NavKey>(() => {
  if (route.path.endsWith('/inbox')) return 'inbox'
  const q = route.query.section
  if (q === 'proposals') return 'proposals'
  return typeof q === 'string' && SECTION_KEYS.has(q) ? (q as Section) : 'who'
})

const heading = computed(() => {
  if (current.value === 'inbox') return '知君最近学到的'
  if (current.value === 'proposals') return '需要你裁决'
  return sectionLabel(current.value)
})
const hint = computed(() => {
  if (current.value === 'inbox') return '这些是知君从对话里提出、还没经你确认的理解。确认后才会成为它对你的认识；否定后永远不会再出现。'
  if (current.value === 'proposals') return '知君整理时发现的疑问：两个名字是不是同一个人？两条理解是不是矛盾？它不会自己拍板，只等你定。'
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

function select(key: NavKey) {
  if (key === 'inbox') router.push('/me/inbox')
  else router.push({ path: '/me', query: { section: key } })
}

function onProposalsChanged() {
  void loadStats()
}

watch(current, () => {
  void load()
})

async function onReview(claim: Claim, action: ReviewAction, editedContent?: string) {
  busy[claim.id] = true
  try {
    const result = await reviewClaim(claim.id, { action, editedContent, surface: 'ontology_page' })
    const finalClaim = result.replacedBy ?? result.claim
    const idx = items.value.findIndex((c) => c.id === claim.id)
    const keep = current.value === 'inbox' ? finalClaim.trustState === 'working' : finalClaim.trustState === 'working' || finalClaim.trustState === 'confirmed'
    if (idx >= 0) {
      if (keep) items.value.splice(idx, 1, finalClaim)
      else items.value.splice(idx, 1)
    }
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
})

onBeforeUnmount(() => gate.invalidate())
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
          <BaseButton v-if="current !== 'proposals'" size="sm" @click="showCreate = !showCreate">{{ showCreate ? '收起' : '补一条' }}</BaseButton>
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
@media (max-width: 767px) {
  .zj-me__grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .zj-me__create {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
