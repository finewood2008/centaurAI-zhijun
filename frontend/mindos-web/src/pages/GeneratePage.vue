<script setup lang="ts">
// P14-10：基于所选资料 / 知识卡片生成内容草稿。
// 选择至少一个未归档来源 → 生成「学习笔记 / 文章摘要 / 播客脚本」草稿（含「待用户审阅」标记）
// → 审阅可编辑 → 仅由用户主动「另存为知识卡片」（来源 ID 写入 frontmatter）。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type GenerationCitation, type GenerationResult, type GenerationType } from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { useToast } from '@/composables/useToast'

const router = useRouter()
const toast = useToast()

interface SourceOption {
  id: string
  kind: 'material' | 'knowledge'
  title: string
  meta: string
}

const sources = ref<SourceOption[]>([])
const loadingSources = ref(true)
const sourceError = ref('')

const selected = ref<Set<string>>(new Set())
const genType = ref<GenerationType>('study_note')
const instruction = ref('')

const generating = ref(false)
const genError = ref('')
const draft = ref<GenerationResult | null>(null)
const draftTitle = ref('')
const editedContent = ref('')
const saving = ref(false)

const typeOptions: Array<{ value: GenerationType; label: string }> = [
  { value: 'study_note', label: '学习笔记' },
  { value: 'article_summary', label: '文章摘要' },
  { value: 'podcast_script', label: '播客脚本' },
]

const selectedCount = computed(() => selected.value.size)

async function loadSources() {
  loadingSources.value = true
  sourceError.value = ''
  try {
    const [mres, kres] = await Promise.all([
      api.listMaterials(),
      api.listKnowledge(),
    ])
    const items: SourceOption[] = []
    for (const m of mres.items) {
      // 仅可选“可用”的原材料（处理中/失败/删除不计入来源）
      if (m.status !== 'available') continue
      items.push({ id: m.materialId, kind: 'material', title: m.fileName, meta: '资料' })
    }
    for (const k of kres.items) {
      items.push({ id: k.knowledgeId, kind: 'knowledge', title: k.title, meta: '卡片' })
    }
    sources.value = items
  } catch (e) {
    sourceError.value = e instanceof Error ? e.message : '来源加载失败'
  } finally {
    loadingSources.value = false
  }
}

function toggle(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

async function generate() {
  if (!selectedCount.value || generating.value) return
  generating.value = true
  genError.value = ''
  draft.value = null
  try {
    const result = await api.createGeneration(genType.value, [...selected.value], instruction.value)
    draft.value = result
    editedContent.value = result.content
    const label = typeOptions.find((t) => t.value === genType.value)?.label ?? ''
    draftTitle.value = `基于 ${result.citations.length} 项来源的${label}`
  } catch (e) {
    genError.value = e instanceof Error ? e.message : '生成失败'
  } finally {
    generating.value = false
  }
}

async function saveAsCard() {
  if (!draft.value || saving.value) return
  saving.value = true
  try {
    const res = await api.createKnowledgeFromDraft(draft.value.draftId, {
      title: draftTitle.value.trim() || undefined,
      content: editedContent.value,
    })
    toast({ type: 'success', message: '已另存为知识卡片' })
    router.push(`/knowledge/${res.item.knowledgeId}`)
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '另存为知识卡片失败' })
  } finally {
    saving.value = false
  }
}

function citationTitle(c: GenerationCitation) {
  return `${c.sourceType === 'material' ? '资料' : '卡片'}：${c.title}`
}

onMounted(loadSources)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1>内容生成</h1>
      <p>基于已选择的资料 / 知识卡片生成草稿，审阅后可另存为知识卡片。</p>
    </div>

    <ErrorState v-if="sourceError" :message="sourceError" retry-label="重试" @retry="loadSources" />
    <div v-else-if="loadingSources" class="loading-state">正在加载来源…</div>

    <template v-else>
      <section class="gen-panel">
        <div class="gen-panel__title">1. 选择来源（已选 {{ selectedCount }}）</div>
        <div v-if="sources.length" class="gen-source-grid">
          <button
            v-for="s in sources"
            :key="`${s.kind}:${s.id}`"
            type="button"
            class="gen-source"
            :class="{ 'is-selected': selected.has(s.id) }"
            @click="toggle(s.id)"
          >
            <span class="gen-source__title">{{ s.title }}</span>
            <span class="gen-source__meta">{{ s.meta }}</span>
          </button>
        </div>
        <div v-else class="empty-sub">暂无可选来源（无可用资料或知识卡片）</div>
      </section>

      <section class="gen-panel">
        <div class="gen-panel__title">2. 草稿类型与偏好</div>
        <div class="gen-type-row">
          <label v-for="opt in typeOptions" :key="opt.value" class="gen-type">
            <input v-model="genType" type="radio" :value="opt.value">
            <span>{{ opt.label }}</span>
          </label>
        </div>
        <textarea
          v-model="instruction"
          class="gen-instruction"
          maxlength="500"
          rows="2"
          placeholder="可选：补充格式 / 侧重偏好（仅作参考，不会覆盖来源内容）"
        />
        <div class="gen-actions">
          <BaseButton variant="primary" :loading="generating" :disabled="!selectedCount" @click="generate">
            {{ generating ? '生成中…' : '生成草稿' }}
          </BaseButton>
        </div>
      </section>

      <ErrorState v-if="genError" :message="genError" retry-label="重试" @retry="generate" />

      <section v-if="draft" class="gen-panel gen-draft">
        <div class="gen-panel__title">3. 草稿（待用户审阅）</div>
        <div v-if="draft.citations.length" class="gen-citations">
          <span v-for="c in draft.citations" :key="`${c.sourceType}:${c.id}`" class="gen-citation">
            {{ citationTitle(c) }}
          </span>
        </div>
        <textarea v-model="editedContent" class="gen-draft__text" rows="12" />
        <label class="gen-draft__title-row">
          卡片标题
          <input v-model="draftTitle" maxlength="200" class="gen-draft__title">
        </label>
        <div class="gen-actions">
          <BaseButton variant="primary" :loading="saving" :disabled="!editedContent.trim()" @click="saveAsCard">
            {{ saving ? '保存中…' : '另存为知识卡片' }}
          </BaseButton>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.gen-panel {
  margin-bottom: 18px;
  padding: 16px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--surface, #fff);
}
.gen-panel__title {
  margin-bottom: 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text, #303133);
}

.gen-source-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.gen-source {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: 999px;
  background: var(--surface, #fff);
  color: var(--text, #303133);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.gen-source:hover {
  border-color: var(--accent, #1b99ff);
}
.gen-source.is-selected {
  border-color: var(--accent, #1b99ff);
  background: var(--accent-soft, rgba(0, 119, 255, 0.06));
  color: var(--accent, #1b99ff);
}
.gen-source__title {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gen-source__meta {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-muted, #909399);
}

.gen-type-row {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
.gen-type {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text, #303133);
  cursor: pointer;
}
.gen-instruction {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 10px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: var(--ws-radius-md, 6px);
  font-family: inherit;
  font-size: 13px;
  color: var(--text, #303133);
  resize: vertical;
}
.gen-actions {
  margin-top: 12px;
}

.gen-citations {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.gen-citation {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  background: var(--accent-soft, rgba(0, 119, 255, 0.06));
  color: var(--accent, #1b99ff);
}
.gen-draft__text {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 12px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: var(--ws-radius-md, 6px);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text, #303133);
  resize: vertical;
}
.gen-draft__title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  font-size: 13px;
  color: var(--text, #303133);
}
.gen-draft__title {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  border: 1px solid var(--border, #dcdfe6);
  border-radius: var(--ws-radius-md, 6px);
  font-family: inherit;
  font-size: 13px;
  color: var(--text, #303133);
}
</style>
