<script setup lang="ts">
// 资料与边界：导入资料、模型与隐私、回收站、知识档案、搜索的枢纽；附「知君会带走什么」的投影预览。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { FileText, FolderOpen, Search, Settings, Trash2, ShieldCheck, AlertTriangle, PackageOpen } from 'lucide-vue-next'
import { exportOntology, getContextPackStatus, getProjection, purgeOntology, type ContextPackStatus, type OntologyProjection, type Section } from '@/services/api'
import { sectionLabel } from '@/shared/ontology'
import { formatDate } from '@/shared/format'
import BaseButton from '@/components/ui/BaseButton.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import { useToast } from '@/composables/useToast'
import { SECTIONS } from '@/shared/ontology'
import { PURGE_PHRASE, exportFileName, purgeConfirmed } from '@/shared/proposals'

const toast = useToast()
const router = useRouter()

// ---- 导出（JSON 下载）
const exporting = ref(false)
const exportSections = ref<Section[]>([])
const exportOpen = ref(false)

function downloadJson(data: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

async function doExport(sections?: Section[]) {
  exporting.value = true
  try {
    const data = await exportOntology({ sections })
    downloadJson(data, exportFileName())
    toast({ type: 'success', message: `已导出 ${data.claims.length} 条已确认理解、${data.entities.length} 个实体` })
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '导出失败' })
  } finally {
    exporting.value = false
  }
}

function toggleSection(key: Section) {
  const idx = exportSections.value.indexOf(key)
  if (idx >= 0) exportSections.value.splice(idx, 1)
  else exportSections.value.push(key)
}

// ---- 全量删除（需逐字输入确认词）
const purgeOpen = ref(false)
const purgeInput = ref('')
const purgeConversations = ref(true)
const purging = ref(false)
const purgeReady = computed(() => purgeConfirmed(purgeInput.value))

function openPurge() {
  purgeInput.value = ''
  purgeConversations.value = true
  purgeOpen.value = true
}

async function doPurge() {
  if (!purgeReady.value || purging.value) return
  purging.value = true
  try {
    const r = await purgeOntology({ confirm: purgeInput.value.trim(), includeConversations: purgeConversations.value })
    purgeOpen.value = false
    const conv = r.conversations ? `，${r.conversations.conversations} 段对话` : ''
    toast({ type: 'success', message: `已删除 ${r.ontology.claims} 条理解、${r.ontology.entities} 个实体${conv}` })
    router.push('/')
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '删除失败' })
  } finally {
    purging.value = false
  }
}

const primaryCards = [
  { to: '/materials', icon: FolderOpen, title: '原材料', desc: '导入文档、图片、音频；查看处理状态与原件出处' },
  { to: '/settings', icon: Settings, title: '偏好（模型与隐私）', desc: '用哪个模型、什么能出设备、提醒多不多' },
]
const moreCards = [
  { to: '/knowledge', icon: FileText, title: '知识档案', desc: '由资料整理出的知识卡片' },
  { to: '/recycle-bin', icon: Trash2, title: '回收站', desc: '恢复或永久清除已删除的资料' },
  { to: '/search', icon: Search, title: '搜索', desc: '在本地资料里找回细节' },
]

// ---- 可以带走的认识（Context Pack 状态）
const pack = ref<ContextPackStatus | null>(null)
const packError = ref('')
async function loadPack() {
  try {
    pack.value = await getContextPackStatus()
    packError.value = ''
  } catch (err) {
    packError.value = err instanceof Error ? err.message : '加载失败'
  }
}
onMounted(loadPack)

const projection = ref<OntologyProjection | null>(null)
const projectionLoading = ref(false)
const projectionOpen = ref(false)

async function toggleProjection() {
  if (projectionOpen.value) {
    projectionOpen.value = false
    return
  }
  projectionLoading.value = true
  try {
    projection.value = await getProjection()
    projectionOpen.value = true
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '投影加载失败' })
  } finally {
    projectionLoading.value = false
  }
}
</script>

<template>
  <div class="page zj-hub">
    <div class="page-head">
      <h1>资料与边界</h1>
      <p>资料从这里进来；什么能出设备，也在这里说清楚。</p>
    </div>

    <div class="zj-hub__grid">
      <RouterLink v-for="c in primaryCards" :key="c.to" :to="c.to" class="zj-hub__card">
        <component :is="c.icon" :size="20" aria-hidden="true" />
        <span class="zj-hub__card-title">{{ c.title }}</span>
        <span class="zj-hub__card-desc">{{ c.desc }}</span>
      </RouterLink>
    </div>

    <section class="zj-hub__boundary">
      <h2><ShieldCheck :size="18" aria-hidden="true" />边界</h2>
      <p>原件不出设备。用外部模型时，只发送完成这一轮所必需的问题和片段，每一轮的出处条里都看得到送出了什么。标为敏感或受限的理解永远不外发。</p>
      <p v-if="packError" class="zj-hub__pack-meta">{{ packError }}</p>
      <p v-else-if="pack" class="zj-hub__pack-meta">
        其他 Agent 能拿到的只有你确认过并打开「可带走」的理解，目前 <strong>{{ pack.exportable }}</strong> 条。
        <RouterLink to="/me" class="zj-hub__pack-link">去「我的本体」逐条决定</RouterLink>
      </p>
    </section>

    <details class="zj-hub__adv">
      <summary>高级 · 知识档案、回收站、搜索、导出、删除全部记忆</summary>

      <div class="zj-hub__grid zj-hub__grid--sub">
        <RouterLink v-for="c in moreCards" :key="c.to" :to="c.to" class="zj-hub__card">
          <component :is="c.icon" :size="18" aria-hidden="true" />
          <span class="zj-hub__card-title">{{ c.title }}</span>
          <span class="zj-hub__card-desc">{{ c.desc }}</span>
        </RouterLink>
      </div>

      <section class="zj-hub__pack">
        <h2><PackageOpen :size="18" aria-hidden="true" />可以带走的认识</h2>
        <template v-if="pack">
          <p class="zj-hub__pack-meta">
            <template v-if="pack.receipts.last">
              最近一次：{{ formatDate(pack.receipts.last.generatedAt) }} 由 {{ pack.receipts.last.consumer || '未署名的 Agent' }} 以「{{ pack.receipts.last.purpose }}」取走 {{ pack.receipts.last.included }} 条（累计 {{ pack.receipts.count }} 次）。
            </template>
            <template v-else>还没有其他 Agent 取过。</template>
          </p>
          <ul v-if="pack.items.length" class="zj-hub__pack-list">
            <li v-for="c in pack.items" :key="c.id">
              <span class="zj-hub__pack-section">{{ sectionLabel(c.section) }}</span>
              <span>{{ c.content }}</span>
            </li>
          </ul>
          <p v-else class="zj-hub__pack-meta">还没有打开「可带走」的理解。</p>
        </template>
        <div class="zj-hub__actions">
          <BaseButton size="sm" :loading="projectionLoading" @click="toggleProjection">{{ projectionOpen ? '收起' : '查看可导出的认识' }}</BaseButton>
          <BaseButton size="sm" :loading="exporting" @click="doExport()">导出全部认识（JSON）</BaseButton>
          <BaseButton size="sm" variant="text" @click="exportOpen = !exportOpen">{{ exportOpen ? '收起分区' : '按分区导出' }}</BaseButton>
        </div>
        <div v-if="exportOpen" class="zj-hub__sections">
          <label v-for="s in SECTIONS" :key="s.key" class="zj-hub__check">
            <input type="checkbox" :checked="exportSections.includes(s.key)" @change="toggleSection(s.key)" />
            <span>{{ s.label }}</span>
          </label>
          <BaseButton size="sm" :disabled="!exportSections.length" :loading="exporting" @click="doExport([...exportSections])">导出所选分区</BaseButton>
        </div>
        <div v-if="projectionOpen && projection" class="zj-hub__projection">
          <p class="zj-hub__projection-meta">生成于 {{ projection.generatedAt }}</p>
          <pre>{{ projection.exportableMarkdown || '（还没有可导出的已确认理解）' }}</pre>
        </div>
      </section>

      <section class="zj-hub__danger">
        <h2><AlertTriangle :size="18" aria-hidden="true" />删除全部记忆</h2>
        <p>删除知君对你的全部认识（实体、理解、证据、复核记录），可选同时删除对话记录。资料原件与索引不受影响。此操作不可恢复，建议先导出。</p>
        <BaseButton size="sm" variant="danger" @click="openPurge">删除全部记忆</BaseButton>
      </section>
    </details>

    <ConfirmDialog
      :open="purgeOpen"
      title="真的要删除全部记忆？"
      confirm-text="删除"
      danger
      :loading="purging"
      @confirm="doPurge"
      @cancel="purgeOpen = false"
    >
      <p class="zj-hub__purge-text">这会清空知君对你的全部认识，不可恢复。请逐字输入「{{ PURGE_PHRASE }}」确认。</p>
      <label class="zj-hub__purge-field">
        <span class="zj-hub__purge-label">确认词</span>
        <input v-model="purgeInput" type="text" class="zj-hub__purge-input" :placeholder="PURGE_PHRASE" autocomplete="off" />
      </label>
      <label class="zj-hub__check">
        <input v-model="purgeConversations" type="checkbox" />
        <span>同时删除对话记录</span>
      </label>
      <p v-if="purgeInput && !purgeReady" class="zj-hub__purge-hint">确认词不一致，需要逐字输入。</p>
      <p v-else-if="!purgeReady" class="zj-hub__purge-hint">输入确认词后才能删除。</p>
    </ConfirmDialog>
  </div>
</template>

<style scoped>
.zj-hub__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  max-width: 760px;
  margin-bottom: 20px;
}
.zj-hub__grid--sub {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 12px 0 20px;
}
.zj-hub__adv {
  max-width: 760px;
  margin-top: 20px;
}
.zj-hub__adv > summary {
  font-size: 13px;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
}
.zj-hub__adv > summary:hover {
  color: var(--ws-primary-color, #a6452e);
}
@media (max-width: 767px) {
  .zj-hub__grid,
  .zj-hub__grid--sub {
    grid-template-columns: 1fr;
  }
}
.zj-hub__card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 18px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  color: var(--ws-primary-color, #a6452e);
  text-decoration: none;
  transition:
    border-color 0.15s,
    transform 0.15s;
}
.zj-hub__card:hover {
  border-color: var(--ws-primary-color, #a6452e);
  transform: translateY(-1px);
}
.zj-hub__card-title {
  font-family: var(--ws-font-display, serif);
  font-size: 17px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-hub__card-desc {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-hub__boundary {
  max-width: 760px;
  padding: 18px 20px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.zj-hub__boundary h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  font-family: var(--ws-font-display, serif);
  font-size: 18px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-hub__boundary p {
  margin: 0 0 10px;
  font-size: 14px;
  line-height: 1.8;
  color: var(--ws-text-color, #3c403d);
}
.zj-hub__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.zj-hub__sections {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 14px;
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px dashed var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
}
.zj-hub__check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--ws-text-color, #3c403d);
  cursor: pointer;
}
.zj-hub__pack {
  max-width: 760px;
  margin-top: 12px;
  padding: 18px 20px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.zj-hub__pack h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  font-family: var(--ws-font-display, serif);
  font-size: 18px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-hub__pack p {
  margin: 0 0 10px;
  font-size: 14px;
  line-height: 1.8;
  color: var(--ws-text-color, #3c403d);
}
.zj-hub__pack-meta {
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-hub__pack-list {
  margin: 0 0 10px;
  padding: 0;
  list-style: none;
  font-size: 13px;
  line-height: 1.8;
}
.zj-hub__pack-list li {
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.zj-hub__pack-section {
  flex: none;
  padding: 0 8px;
  border-radius: 999px;
  background: var(--ws-surface-2, #fbf8f1);
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-hub__pack-link {
  margin-left: 6px;
  font-size: 13px;
  color: var(--ws-primary-color, #a6452e);
}
.zj-hub__danger {
  max-width: 760px;
  margin-top: 20px;
  padding: 18px 20px;
  border: 1px solid rgba(166, 69, 46, 0.35);
  border-radius: var(--ws-radius-lg, 8px);
  background: rgba(166, 69, 46, 0.04);
}
.zj-hub__danger h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  font-family: var(--ws-font-display, serif);
  font-size: 18px;
  color: var(--ws-danger-color, #a6452e);
}
.zj-hub__danger p {
  margin: 0 0 10px;
  font-size: 14px;
  line-height: 1.8;
  color: var(--ws-text-color, #3c403d);
}
.zj-hub__purge-text,
.zj-hub__purge-hint {
  margin: 0 0 10px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-color, #3c403d);
}
.zj-hub__purge-hint {
  margin: 8px 0 0;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-hub__purge-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
}
.zj-hub__purge-label {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-hub__purge-input {
  padding: 8px 10px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-body-bg, #fffcf6);
  font-family: inherit;
  font-size: 14px;
}
.zj-hub__purge-input:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.zj-hub__projection {
  margin-top: 12px;
}
.zj-hub__projection-meta {
  font-size: 12px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-hub__projection pre {
  max-height: 420px;
  overflow: auto;
  padding: 12px 14px;
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-surface-2, #fbf8f1);
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
