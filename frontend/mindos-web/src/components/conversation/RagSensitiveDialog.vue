<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { RagV2MaterialItem, RagV2Prompt } from '@/services/taskRouting'

import BaseButton from '../ui/BaseButton.vue'

type RagSensitiveStatus = RagV2Prompt['status']

interface RagSensitiveLocation {
  page?: number
  paragraph?: number
  section?: string
}

interface RagSensitiveHit {
  category: string
  redactedPreview: string
  location?: string | RagSensitiveLocation
  title?: string
}

interface RagDetectionNotice {
  code?: string
  message?: string
  retrievedCount?: number
  checkedCount?: number
  withheldCount?: number
  retryable?: boolean
  riskEligibleCount?: number
  riskMessage?: string
}

const props = withDefaults(
  defineProps<{
    status: RagSensitiveStatus
    interactionId?: string
    items?: RagV2MaterialItem[]
    query?: string
    scopeLabel?: string
    outcome?: RagV2Prompt['outcome']
    deliveryMode?: string
    hits?: RagSensitiveHit[]
    detectionNotice?: RagDetectionNotice
    canReadOriginal?: boolean
    passedCount?: number
    riskAvailable?: boolean
    busy?: boolean
  }>(),
  {
    hits: () => [],
    items: () => [],
    canReadOriginal: false,
    passedCount: 0,
    riskAvailable: false,
    busy: false,
  },
)

const emit = defineEmits<{
  masked: []
  original: []
  'continue-passed': []
  retry: []
  'risk-release': []
  'use-selected': [selectedPreviewIds: string[]]
  'without-materials': []
  cancel: []
}>()

const riskExpanded = ref(false)
const riskAcknowledged = ref(false)
const selectedPreviewIds = ref<string[]>([])
const materialItems = computed(() => {
  const seen = new Set<string>()
  return (props.items || []).filter(item => {
    if (!item || typeof item.previewId !== 'string' || !item.previewId.trim()
        || typeof item.preview !== 'string' || seen.has(item.previewId)) return false
    seen.add(item.previewId)
    return true
  })
})
const selectedItems = computed(() => materialItems.value.filter(item => selectedPreviewIds.value.includes(item.previewId)))
const deliveryLabel = computed(() => {
  const labels: Record<string, string> = {
    masked: '使用脱敏后片段',
    original: '原文片段',
    'risk-release': '未经验证原文',
    'continue-passed': '仅使用已通过检测的片段',
  }
  return labels[props.deliveryMode || ''] || ''
})
const emptyMessage = computed(() => props.outcome === 'sensitive_content_blocked'
  ? '本次检索的资料受交付策略限制，没有可使用的片段。'
  : props.outcome === 'no_results'
    ? '本次检索没有找到匹配的资料片段。'
    : '本次没有可供确认的资料片段。')

watch([() => props.interactionId, () => props.status, () => props.items, () => props.deliveryMode], () => {
  selectedPreviewIds.value = []
  riskExpanded.value = false
  riskAcknowledged.value = false
}, { flush: 'sync' })

function useSelected() {
  if (!props.busy && props.status === 'materials_confirmation_required' && selectedItems.value.length) {
    emit('use-selected', selectedItems.value.map(item => item.previewId))
  }
}

function withoutMaterials() {
  if (!props.busy) emit('without-materials')
}

function materialStatus(item: RagV2MaterialItem): string {
  const verification = item.verificationStatus === 'verified' ? '已完成检测' : '未完成检测'
  const sensitivity = item.containsSensitive === false ? '无敏感标记' : '含敏感标记'
  return `${verification} · ${sensitivity}`
}

function previewNotice(item: RagV2MaterialItem): string {
  if (item.previewTruncated !== true) return ''
  const shownLength = Array.from(item.preview).length
  const total = typeof item.textLength === 'number' && Number.isInteger(item.textLength) && item.textLength > shownLength
    ? `，完整检索片段共 ${item.textLength} 字` : ''
  return `仅展示前 ${shownLength} 字${total}。选中后允许使用整个检索片段，包括未展示部分。`
}

function materialLocation(locator: RagV2MaterialItem['locator']): string {
  if (!locator || typeof locator === 'string') return locationLabel(locator)
  const time = typeof locator.startMs === 'number' && Number.isFinite(locator.startMs) && locator.startMs >= 0
    ? `${(locator.startMs / 1000).toFixed(1)} 秒${typeof locator.endMs === 'number' && Number.isFinite(locator.endMs) && locator.endMs >= locator.startMs ? `–${(locator.endMs / 1000).toFixed(1)} 秒` : ''}` : ''
  return [locationLabel(normaliseLocation(locator)),
    typeof locator.table === 'string' || typeof locator.table === 'number' ? `表格 ${locator.table}` : '',
    typeof locator.cell === 'string' ? locator.cell : '', time].filter(Boolean).join(' · ')
}

const categoryLabels: Record<string, string> = {
  mobile_phone: '手机号码',
  address: '地址',
  person_name: '姓名',
  company_name: '公司名称',
  credential_secret: '凭据或密钥',
  id_card: '身份证号',
  contract_name: '合同名称',
  email: '邮箱',
  bank_account: '银行账户',
}

const safeHits = computed<RagSensitiveHit[]>(() => {
  const seen = new Set<string>()
  const result: RagSensitiveHit[] = []

  for (const hit of props.hits) {
    if (!hit || typeof hit.category !== 'string' || typeof hit.redactedPreview !== 'string') {
      continue
    }

    const safeHit: RagSensitiveHit = {
      category: hit.category,
      redactedPreview: hit.redactedPreview,
      ...(typeof hit.title === 'string' && hit.title.trim() ? { title: hit.title } : {}),
      ...(normaliseLocation(hit.location) !== undefined
        ? { location: normaliseLocation(hit.location) }
        : {}),
    }
    const key = JSON.stringify(safeHit)
    if (!seen.has(key)) {
      seen.add(key)
      result.push(safeHit)
    }
  }

  return result
})

const checkedCount = computed(() => nonNegativeInteger(props.detectionNotice?.checkedCount))
const withheldCount = computed(() => nonNegativeInteger(props.detectionNotice?.withheldCount))
const retrievedCount = computed(() => nonNegativeInteger(props.detectionNotice?.retrievedCount))
const hasPassedHits = computed(() => nonNegativeInteger(props.passedCount) > 0)
const canRetry = computed(() => props.detectionNotice?.retryable !== false)
const canRiskRelease = computed(
  () =>
    props.status === 'sensitive_check_unavailable' &&
    props.riskAvailable,
)

watch(
  [() => props.status, () => props.detectionNotice?.riskEligibleCount],
  () => {
    riskExpanded.value = false
    riskAcknowledged.value = false
  },
)

function nonNegativeInteger(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? Math.floor(value)
    : 0
}

function normaliseLocation(location: RagSensitiveHit['location']): RagSensitiveHit['location'] {
  if (typeof location === 'string') {
    const trimmed = location.trim()
    return trimmed || undefined
  }
  if (!location || typeof location !== 'object') return undefined

  const safeLocation: RagSensitiveLocation = {}
  if (Number.isInteger(location.page) && Number(location.page) > 0) {
    safeLocation.page = Number(location.page)
  }
  if (Number.isInteger(location.paragraph) && Number(location.paragraph) > 0) {
    safeLocation.paragraph = Number(location.paragraph)
  }
  if (typeof location.section === 'string' && location.section.trim()) {
    safeLocation.section = location.section.trim()
  }
  return Object.keys(safeLocation).length > 0 ? safeLocation : undefined
}

function categoryLabel(category: string): string {
  return categoryLabels[category] ?? '敏感信息'
}

function locationLabel(location: RagSensitiveHit['location']): string {
  if (typeof location === 'string') return location
  if (!location) return ''

  return [
    location.section ? location.section : '',
    location.page ? `第 ${location.page} 页` : '',
    location.paragraph ? `第 ${location.paragraph} 段` : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

function chooseMasked() {
  if (!props.busy && props.status === 'sensitive_confirmation_required') emit('masked')
}

function chooseOriginal() {
  if (
    !props.busy &&
    props.status === 'sensitive_confirmation_required' &&
    props.canReadOriginal
  ) {
    emit('original')
  }
}

function continueWithPassed() {
  if (!props.busy && props.status === 'sensitive_check_unavailable' && hasPassedHits.value) {
    emit('continue-passed')
  }
}

function retryDetection() {
  if (!props.busy && props.status === 'sensitive_check_unavailable' && canRetry.value) {
    emit('retry')
  }
}

function requestRiskRelease() {
  if (!props.busy && canRiskRelease.value) {
    riskExpanded.value = true
    riskAcknowledged.value = false
  }
}

function releaseRisk() {
  if (!props.busy && canRiskRelease.value && riskExpanded.value && riskAcknowledged.value) {
    emit('risk-release')
  }
}

function closeRiskRelease() {
  riskExpanded.value = false
  riskAcknowledged.value = false
}

function cancel() {
  if (!props.busy) emit('cancel')
}
</script>

<template>
  <section
    class="rag-sensitive"
    role="alertdialog"
    aria-modal="true"
    :aria-label="status === 'materials_confirmation_required' ? '资料片段确认' : '敏感资料确认'"
    :aria-busy="busy"
  >
    <dl v-if="query || scopeLabel" class="rag-sensitive__search" aria-label="本次资料检索">
      <div v-if="query"><dt>检索问题</dt><dd>{{ query }}</dd></div>
      <div v-if="scopeLabel"><dt>检索范围</dt><dd>{{ scopeLabel }}</dd></div>
    </dl>

    <template v-if="status === 'materials_confirmation_required'">
      <div class="rag-sensitive__eyebrow">资料片段确认</div>
      <h3>{{ materialItems.length ? '选择本次回答可使用的片段' : '本次没有可用的资料片段' }}</h3>
      <p v-if="materialItems.length" class="rag-sensitive__lead">
        核对以下预览并勾选需要使用的片段。默认不选；确认后允许将选中的完整检索片段用于本次回答。较长片段这里只展示前 500 字。
      </p>
      <p v-if="materialItems.length && deliveryLabel" class="rag-sensitive__delivery" aria-label="资料交付方式">{{ deliveryLabel }}</p>
      <ul v-if="materialItems.length" class="rag-sensitive__hits" aria-label="可选择的资料片段">
        <li v-for="item in materialItems" :key="item.previewId">
          <label class="rag-sensitive__selection">
            <input v-model="selectedPreviewIds" type="checkbox" :value="item.previewId" :disabled="busy" />
            <span>
              <strong>{{ item.title || '未命名资料' }}</strong>
              <span v-if="item.materialVersion" class="rag-sensitive__location"> · 版本 {{ item.materialVersion }}</span>
              <span class="rag-sensitive__material-status">{{ materialStatus(item) }}</span>
              <span v-if="materialLocation(item.locator)" class="rag-sensitive__location">{{ materialLocation(item.locator) }}</span>
              <span class="rag-sensitive__preview">{{ item.preview }}</span>
              <span v-if="previewNotice(item)" class="rag-sensitive__preview-notice">{{ previewNotice(item) }}</span>
            </span>
          </label>
        </li>
      </ul>
      <p v-else class="rag-sensitive__empty">{{ emptyMessage }}</p>
      <div class="rag-sensitive__actions">
        <BaseButton v-if="materialItems.length" variant="primary" :disabled="busy || !selectedItems.length" @click="useSelected">
          使用选中的 {{ selectedItems.length }} 个片段继续
        </BaseButton>
        <BaseButton variant="secondary" :disabled="busy" @click="withoutMaterials">不用材料继续</BaseButton>
        <BaseButton variant="text" :disabled="busy" @click="cancel">取消</BaseButton>
      </div>
    </template>

    <template v-else-if="status === 'sensitive_confirmation_required'">
      <div class="rag-sensitive__eyebrow">资料安全确认</div>
      <h3>发现可能包含敏感信息的资料</h3>
      <p class="rag-sensitive__lead">
        请选择发送脱敏内容，或在有权限时领取原文。未经选择，资料不会发送给模型。
      </p>
      <p v-if="detectionNotice?.withheldCount" class="rag-sensitive__lead">
        另有 {{ withheldCount }} 个片段尚未完成检测；处理当前敏感项后，会再请你决定是否仅用已通过片段、重试或明确承担风险。
      </p>

      <ul v-if="safeHits.length" class="rag-sensitive__hits" aria-label="敏感信息摘要">
        <li v-for="(hit, index) in safeHits" :key="`${hit.category}-${index}`">
          <div class="rag-sensitive__hit-heading">
            <span class="rag-sensitive__tag">{{ categoryLabel(hit.category) }}</span>
            <strong v-if="hit.title">{{ hit.title }}</strong>
            <span v-if="locationLabel(hit.location)" class="rag-sensitive__location">
              {{ locationLabel(hit.location) }}
            </span>
          </div>
          <p>{{ hit.redactedPreview }}</p>
        </li>
      </ul>
      <p v-else class="rag-sensitive__empty">未返回可展示的脱敏预览。</p>

      <div class="rag-sensitive__actions">
        <BaseButton variant="primary" :disabled="busy" @click="chooseMasked">
          脱敏后继续
        </BaseButton>
        <BaseButton v-if="canReadOriginal" variant="secondary" :disabled="busy" @click="chooseOriginal">
          领取原文
        </BaseButton>
        <BaseButton variant="secondary" :disabled="busy" @click="withoutMaterials">不用材料继续</BaseButton>
        <BaseButton variant="text" :disabled="busy" @click="cancel">取消</BaseButton>
      </div>
    </template>

    <template v-else>
      <div class="rag-sensitive__eyebrow rag-sensitive__eyebrow--warning">敏感检测暂不可用</div>
      <h3>部分资料已暂缓使用</h3>
      <p class="rag-sensitive__lead">
        {{ detectionNotice?.message || '敏感检测服务暂时不可用。未完成检测的资料不会展示，也不会发送给模型。' }}
      </p>

      <dl class="rag-sensitive__counts" aria-label="资料检测结果">
        <div>
          <dt>本次检索</dt>
          <dd>{{ retrievedCount }}</dd>
        </div>
        <div>
          <dt>已通过</dt>
          <dd>{{ checkedCount }}</dd>
        </div>
        <div>
          <dt>暂缓</dt>
          <dd>{{ withheldCount }}</dd>
        </div>
      </dl>

      <div v-if="!riskExpanded" class="rag-sensitive__actions">
        <BaseButton
          v-if="hasPassedHits"
          variant="primary"
          :disabled="busy"
          @click="continueWithPassed"
        >
          仅使用已通过资料继续
        </BaseButton>
        <BaseButton
          variant="secondary"
          :disabled="busy || !canRetry"
          @click="retryDetection"
        >
          重试检测
        </BaseButton>
        <BaseButton
          v-if="canRiskRelease"
          variant="text"
          :disabled="busy"
          @click="requestRiskRelease"
        >
          了解风险后放行
        </BaseButton>
        <BaseButton variant="secondary" :disabled="busy" @click="withoutMaterials">不用材料继续</BaseButton>
        <BaseButton variant="text" :disabled="busy" @click="cancel">取消</BaseButton>
      </div>

      <div v-else class="rag-sensitive__risk" role="alert" aria-label="风险放行确认">
        <strong>确认承担风险后放行</strong>
        <p>
          {{ detectionNotice?.riskMessage || '这些资料未完成敏感检测，放行后可能把敏感信息发送给模型。' }}
        </p>
        <label class="rag-sensitive__acknowledgement">
          <input v-model="riskAcknowledged" type="checkbox" :disabled="busy" />
          <span>我已了解未检测资料可能包含敏感信息，并确认继续。</span>
        </label>
        <div class="rag-sensitive__actions">
          <BaseButton
            variant="danger"
            :disabled="busy || !riskAcknowledged"
            @click="releaseRisk"
          >
            确认风险并放行
          </BaseButton>
          <BaseButton variant="text" :disabled="busy" @click="closeRiskRelease">返回</BaseButton>
          <BaseButton variant="secondary" :disabled="busy" @click="withoutMaterials">不用材料继续</BaseButton>
          <BaseButton variant="text" :disabled="busy" @click="cancel">取消</BaseButton>
        </div>
      </div>
    </template>

    <p v-if="busy" class="rag-sensitive__busy" role="status">正在处理，请稍候…</p>
  </section>
</template>

<style scoped>
.rag-sensitive {
  width: min(100%, 680px);
  padding: 24px;
  border: 1px solid color-mix(in srgb, var(--ws-accent, #b64b32) 32%, transparent);
  border-radius: 20px;
  background: var(--ws-surface-raised, #fffdf8);
  color: var(--ws-text-primary, #2c2925);
  box-shadow: 0 14px 40px rgb(53 42 31 / 8%);
}

.rag-sensitive h3 {
  margin: 8px 0 10px;
  font: 600 22px/1.35 var(--ws-font-serif, serif);
}

.rag-sensitive__eyebrow {
  color: var(--ws-accent, #b64b32);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.rag-sensitive__eyebrow--warning {
  color: var(--ws-warning, #9b5d20);
}

.rag-sensitive__lead,
.rag-sensitive__empty,
.rag-sensitive__busy,
.rag-sensitive__risk p {
  margin: 0;
  color: var(--ws-text-secondary, #6d6861);
  font-size: 14px;
  line-height: 1.7;
}

.rag-sensitive__hits {
  display: grid;
  gap: 10px;
  max-height: 260px;
  margin: 18px 0 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}

.rag-sensitive__hits li {
  padding: 12px 14px;
  border: 1px solid var(--ws-border-subtle, #e8e0d6);
  border-radius: 12px;
  background: var(--ws-surface-soft, #faf6ef);
}

.rag-sensitive__hit-heading {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.rag-sensitive__hit-heading strong {
  font-size: 14px;
}

.rag-sensitive__tag {
  padding: 2px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--ws-accent, #b64b32) 12%, transparent);
  color: var(--ws-accent, #b64b32);
  font-size: 12px;
  font-weight: 650;
}

.rag-sensitive__location {
  color: var(--ws-text-tertiary, #8a847c);
  font-size: 12px;
}

.rag-sensitive__hits p {
  margin: 8px 0 0;
  color: var(--ws-text-secondary, #5f5a54);
  font-size: 14px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.rag-sensitive__search {
  margin: 0 0 18px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--ws-border-subtle, #e8e0d6);
  font-size: 14px;
  line-height: 1.6;
}

.rag-sensitive__search dt {
  color: var(--ws-text-tertiary, #8a847c);
}

.rag-sensitive__search dd {
  margin: 0 0 6px;
  overflow-wrap: anywhere;
}

.rag-sensitive__selection {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  cursor: pointer;
  font-size: 14px;
}

.rag-sensitive__selection input {
  flex-shrink: 0;
  margin-top: 3px;
  accent-color: var(--ws-accent, #b64b32);
}

.rag-sensitive__material-status,
.rag-sensitive__preview {
  display: block;
  margin-top: 6px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--ws-text-secondary, #5f5a54);
  line-height: 1.6;
}

.rag-sensitive__delivery,
.rag-sensitive__preview-notice {
  display: block;
  margin: 8px 0 0;
  color: var(--ws-warning, #9b5d20);
  font-size: 13px;
  line-height: 1.6;
}

.rag-sensitive__counts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 18px 0 0;
}

.rag-sensitive__counts div {
  padding: 12px;
  border-radius: 12px;
  background: var(--ws-surface-soft, #faf6ef);
}

.rag-sensitive__counts dt {
  color: var(--ws-text-tertiary, #8a847c);
  font-size: 12px;
}

.rag-sensitive__counts dd {
  margin: 5px 0 0;
  font-size: 20px;
  font-weight: 650;
}

.rag-sensitive__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 20px;
}

.rag-sensitive__risk {
  margin-top: 18px;
  padding: 16px;
  border: 1px solid color-mix(in srgb, var(--ws-danger, #a84232) 35%, transparent);
  border-radius: 14px;
  background: color-mix(in srgb, var(--ws-danger, #a84232) 6%, transparent);
}

.rag-sensitive__risk strong {
  display: block;
  margin-bottom: 6px;
  color: var(--ws-danger, #a84232);
}

.rag-sensitive__acknowledgement {
  display: flex;
  gap: 9px;
  align-items: flex-start;
  margin-top: 14px;
  color: var(--ws-text-primary, #2c2925);
  font-size: 14px;
  line-height: 1.55;
  cursor: pointer;
}

.rag-sensitive__acknowledgement input {
  width: 17px;
  height: 17px;
  margin-top: 2px;
  accent-color: var(--ws-accent, #b64b32);
}

.rag-sensitive__busy {
  margin-top: 12px;
}

@media (max-width: 640px) {
  .rag-sensitive {
    padding: 18px;
    border-radius: 16px;
  }

  .rag-sensitive__counts {
    grid-template-columns: 1fr;
  }
}
</style>
