<script setup lang="ts">
// P14-12：纠错本管理页。登记「错误观点 → 已纠正观点」，绑定本地来源，可归档（不物理删除）。
// 纠错仅由用户创建 / 编辑 / 归档；AI 不得自动生成或自动归档。
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Archive, PencilLine, Plus } from 'lucide-vue-next'
import { api, type Correction, type CorrectionStatus } from '@/services/api'
import { sourceRoute } from '@/shared/routes'
import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const router = useRouter()
const toast = useToast()

const items = ref<Correction[]>([])
const loading = ref(true)
const error = ref('')
const filter = ref<'' | CorrectionStatus>('active')
// 问答提醒跳转定位目标（/corrections?correctionId={id}）
const focusCorrectionId = ref('')

const showForm = ref(false)
const editingId = ref('')
const formTitle = ref('')
const formIncorrect = ref('')
const formCorrected = ref('')
const selectedSources = ref<Set<string>>(new Set())
const saving = ref(false)

interface SourceOption {
  id: string
  kind: 'material' | 'knowledge'
  title: string
}
const sourceOptions = ref<SourceOption[]>([])
const loadingSources = ref(true)

const visibleItems = computed(() =>
  filter.value ? items.value.filter((it) => it.status === filter.value) : items.value,
)

async function loadCorrections() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.listCorrections()
    items.value = res.items
  } catch (e) {
    error.value = e instanceof Error ? e.message : '纠错记录加载失败'
  } finally {
    loading.value = false
  }
}

async function loadSources() {
  loadingSources.value = true
  try {
    const [mres, kres] = await Promise.all([
      api.listMaterials(),
      api.listKnowledge(),
    ])
    const opts: SourceOption[] = []
    for (const m of mres.items) {
      if (m.status !== 'available') continue
      opts.push({ id: m.materialId, kind: 'material', title: m.fileName })
    }
    for (const k of kres.items) {
      opts.push({ id: k.knowledgeId, kind: 'knowledge', title: k.title })
    }
    sourceOptions.value = opts
  } catch {
    sourceOptions.value = []
  } finally {
    loadingSources.value = false
  }
}

function resetForm() {
  editingId.value = ''
  formTitle.value = ''
  formIncorrect.value = ''
  formCorrected.value = ''
  selectedSources.value = new Set()
}

function startCreate() {
  resetForm()
  showForm.value = true
}

function startEdit(item: Correction) {
  editingId.value = item.id
  formTitle.value = item.title
  formIncorrect.value = item.incorrectClaim
  formCorrected.value = item.correctedClaim
  selectedSources.value = new Set(item.sourceIds)
  showForm.value = true
}

function cancelForm() {
  showForm.value = false
  resetForm()
}

function toggleSource(id: string) {
  const next = new Set(selectedSources.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedSources.value = next
}

async function save() {
  const title = formTitle.value.trim()
  const incorrect = formIncorrect.value.trim()
  const corrected = formCorrected.value.trim()
  if (!title || !incorrect || !corrected) {
    toast({ type: 'error', message: '标题、错误观点与正确观点均不能为空' })
    return
  }
  if (!selectedSources.value.size) {
    toast({ type: 'error', message: '请至少绑定一个来源' })
    return
  }
  saving.value = true
  try {
    const payload = {
      title,
      incorrectClaim: incorrect,
      correctedClaim: corrected,
      sourceIds: [...selectedSources.value],
    }
    if (editingId.value) {
      await api.updateCorrection(editingId.value, payload)
      toast({ type: 'success', message: '已更新纠错记录' })
    } else {
      await api.createCorrection(payload)
      toast({ type: 'success', message: '已创建纠错记录' })
    }
    showForm.value = false
    resetForm()
    await loadCorrections()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '保存失败' })
  } finally {
    saving.value = false
  }
}

async function archive(item: Correction) {
  try {
    await api.archiveCorrection(item.id)
    toast({ type: 'success', message: '已归档（不会删除来源资料）' })
    await loadCorrections()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '归档失败' })
  }
}

function openSource(sourceId: string) {
  router.push(sourceRoute(sourceId))
}

// P14-12：从 /corrections?correctionId={id} 定位并高亮对应记录（问答提醒跳转闭环）
async function focusCorrectionFromQuery() {
  const target = typeof route.query.correctionId === 'string' ? route.query.correctionId : ''
  if (!target) return
  // 确保目标可见（可能已归档），清空筛选后滚动到该记录
  filter.value = ''
  focusCorrectionId.value = target
  await nextTick()
  document.getElementById(`corr-${target}`)?.scrollIntoView({ block: 'center' })
}

onMounted(async () => {
  await Promise.all([loadCorrections(), loadSources()])
  await focusCorrectionFromQuery()
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1>纠错本</h1>
      <p>登记已确认的「错误观点 → 已纠正观点」；问答命中时置顶提醒，不篡改资料与回答。</p>
    </div>

    <div class="corr-toolbar">
      <div class="corr-tabs" role="group" aria-label="状态筛选">
        <button type="button" class="corr-tab" :class="{ 'is-active': filter === '' }" @click="filter = ''">全部（{{ items.length }}）</button>
        <button type="button" class="corr-tab" :class="{ 'is-active': filter === 'active' }" @click="filter = 'active'">生效中</button>
        <button type="button" class="corr-tab" :class="{ 'is-active': filter === 'archived' }" @click="filter = 'archived'">已归档</button>
      </div>
      <BaseButton v-if="!showForm" variant="primary" @click="startCreate">
        <Plus :size="15" aria-hidden="true" />登记纠错
      </BaseButton>
    </div>

    <ErrorState v-if="error" :message="error" retry-label="重试" @retry="loadCorrections" />
    <div v-else-if="loading" class="loading-state">正在加载纠错记录…</div>

    <!-- 创建 / 编辑表单 -->
    <section v-else-if="showForm" class="corr-panel">
      <div class="corr-panel__title">{{ editingId ? '编辑纠错记录' : '登记纠错' }}</div>
      <label class="corr-field">
        标题
        <input v-model="formTitle" maxlength="200" placeholder="如：预算交付时间的更正">
      </label>
      <label class="corr-field">
        错误观点（将被问答识别并提醒）
        <textarea v-model="formIncorrect" maxlength="500" rows="2" placeholder="如：项目交付时间是 2026 年 3 月" />
      </label>
      <label class="corr-field">
        正确观点（提醒中展示的正确表述）
        <textarea v-model="formCorrected" maxlength="500" rows="2" placeholder="如：项目交付时间已更改为 2026 年 9 月" />
      </label>
      <div class="corr-field">
        <div class="corr-field__label">绑定来源（已选 {{ selectedSources.size }}）</div>
        <div v-if="sourceOptions.length" class="corr-source-grid">
          <button
            v-for="s in sourceOptions"
            :key="`${s.kind}:${s.id}`"
            type="button"
            class="corr-source"
            :class="{ 'is-selected': selectedSources.has(s.id) }"
            @click="toggleSource(s.id)"
          >
            <span class="corr-source__title">{{ s.title }}</span>
            <span class="corr-source__meta">{{ s.kind === 'knowledge' ? '卡片' : '资料' }}</span>
          </button>
        </div>
        <div v-else class="empty-sub">暂无可绑定来源（无可用资料或知识卡片）</div>
      </div>
      <div class="corr-form-actions">
        <BaseButton variant="primary" :loading="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</BaseButton>
        <BaseButton variant="secondary" :disabled="saving" @click="cancelForm">取消</BaseButton>
      </div>
    </section>

    <!-- 列表 -->
    <template v-else>
      <div v-if="!visibleItems.length" class="empty-sub">
        {{ filter ? '该分类下暂无纠错记录' : '暂无纠错记录，点击右上角「登记纠错」创建。' }}
      </div>
      <div v-else class="corr-list">
        <div
          v-for="item in visibleItems"
          :id="`corr-${item.id}`"
          :key="item.id"
          class="corr-card"
          :class="{
            'is-archived': item.status === 'archived',
            'is-focused': item.id === focusCorrectionId,
          }"
        >
          <div class="corr-card__head">
            <strong class="corr-card__title">{{ item.title }}</strong>
            <span class="corr-card__status" :class="item.status === 'archived' ? 'is-archived' : 'is-active'">
              {{ item.status === 'archived' ? '已归档' : '生效中' }}
            </span>
          </div>
          <div class="corr-card__claims">
            <div class="corr-card__claim is-incorrect">
              <span class="corr-card__claim-label">错误观点</span>
              <span class="corr-card__claim-text">{{ item.incorrectClaim }}</span>
            </div>
            <div class="corr-card__claim is-corrected">
              <span class="corr-card__claim-label">已纠正</span>
              <span class="corr-card__claim-text">{{ item.correctedClaim }}</span>
            </div>
          </div>
          <div v-if="item.sourceIds.length" class="corr-card__sources">
            <span class="corr-card__sources-label">来源：</span>
            <button v-for="sid in item.sourceIds" :key="sid" type="button" class="corr-card__source" @click="openSource(sid)">查看来源</button>
          </div>
          <div v-if="item.status === 'active'" class="corr-card__actions">
            <button type="button" class="corr-card__action" @click="startEdit(item)"><PencilLine :size="13" aria-hidden="true" />编辑</button>
            <button type="button" class="corr-card__action is-archive" @click="archive(item)"><Archive :size="13" aria-hidden="true" />归档</button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.corr-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
.corr-tabs {
  display: flex;
  gap: 6px;
}
.corr-tab {
  padding: 6px 14px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: 999px;
  background: var(--surface, #fff);
  color: var(--text, #606266);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.corr-tab.is-active {
  border-color: var(--accent, #1b99ff);
  background: var(--accent-soft, rgba(0, 119, 255, 0.06));
  color: var(--accent, #1b99ff);
  font-weight: 600;
}

.corr-panel {
  margin-bottom: 18px;
  padding: 16px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--surface, #fff);
}
.corr-panel__title {
  margin-bottom: 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text, #303133);
}
.corr-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 13px;
  color: var(--text, #303133);
}
.corr-field__label {
  font-size: 13px;
  color: var(--text, #303133);
}
.corr-field input,
.corr-field textarea {
  padding: 8px 10px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: var(--ws-radius-md, 6px);
  font-family: inherit;
  font-size: 13px;
  color: var(--text, #303133);
  resize: vertical;
}
.corr-source-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.corr-source {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: 999px;
  background: var(--surface, #fff);
  color: var(--text, #303133);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.corr-source.is-selected {
  border-color: var(--accent, #1b99ff);
  background: var(--accent-soft, rgba(0, 119, 255, 0.06));
  color: var(--accent, #1b99ff);
}
.corr-source__title {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.corr-source__meta {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-muted, #909399);
}
.corr-form-actions {
  display: flex;
  gap: 10px;
}

.corr-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.corr-card {
  padding: 14px 16px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--surface, #fff);
}
.corr-card.is-archived {
  opacity: 0.65;
}
.corr-card.is-focused {
  border-color: var(--accent, #1b99ff);
  box-shadow: 0 0 0 2px var(--accent-soft, rgba(0, 119, 255, 0.18));
}
.corr-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.corr-card__title {
  font-size: 14px;
  color: var(--text, #303133);
}
.corr-card__status {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
}
.corr-card__status.is-active {
  background: var(--success-soft, rgba(18, 205, 61, 0.08));
  color: var(--success, #12cd3d);
}
.corr-card__status.is-archived {
  background: var(--muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--text-muted, #909399);
}
.corr-card__claims {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.corr-card__claim {
  display: flex;
  gap: 10px;
  font-size: 13px;
  line-height: 1.6;
}
.corr-card__claim-label {
  flex-shrink: 0;
  width: 60px;
  font-size: 11px;
  color: var(--text-muted, #909399);
  padding-top: 2px;
}
.corr-card__claim.is-incorrect .corr-card__claim-text {
  color: var(--text-muted, #909399);
  text-decoration: line-through;
}
.corr-card__claim.is-corrected .corr-card__claim-text {
  color: var(--text, #303133);
}
.corr-card__sources {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-muted, #909399);
}
.corr-card__source {
  padding: 2px 10px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: 999px;
  background: transparent;
  color: var(--accent, #1b99ff);
  font-family: inherit;
  font-size: 11px;
  cursor: pointer;
}
.corr-card__actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--border, rgba(220, 223, 230, 0.6));
}
.corr-card__action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: 999px;
  background: transparent;
  color: var(--text, #606266);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.corr-card__action.is-archive {
  color: var(--danger, #ff4918);
  border-color: var(--danger, rgba(255, 73, 24, 0.5));
}
</style>
