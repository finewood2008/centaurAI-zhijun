<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { hasProductScope, onProductScopeReset } from '@/shared/productScope'
import { createSensitiveRuleStatusPoller, getSensitiveRuleStatus, type SensitiveRuleStatus } from '@/services/sensitiveRuleStatus'
import {
  api,
  ApiError,
  type SensitiveRule,
  type SensitiveRuleCapabilities,
  type SensitiveRuleDeliveryMode,
  type SensitiveRuleDraft,
  type SensitiveRuleEditableField,
  type SensitiveRuleMaskingFloor,
  type SensitiveRulesResponse,
} from '@/services/api'

const props = withDefaults(defineProps<{ enabled?: boolean }>(), { enabled: true })

type EditorMode = 'new' | 'existing' | 'builtin' | null
type PendingMutation = {
  kind: 'create' | 'update' | 'builtin_update' | 'builtin_reset'
  requestId?: string
  ruleId?: string
  expectedEtag?: string
  draft?: SensitiveRuleDraft | Record<string, unknown>
}

const rules = ref<SensitiveRule[]>([])
const capabilities = ref<SensitiveRuleCapabilities | null>(null)
const capabilityError = ref('')
const summary = ref<SensitiveRulesResponse | null>(null)
const selectedId = ref('')
const editorMode = ref<EditorMode>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const applicationStatus = ref<SensitiveRuleStatus | null>(null)
const statusError = ref('')
const deleteConfirmId = ref('')
const resetConfirmId = ref('')
const pendingReset = ref<{ ruleId: string; etag: string; displayName: string; enabled: boolean;
  overridden: boolean; overrideNeedsReview: boolean; deliveryMode: SensitiveRuleDeliveryMode } | null>(null)
const rolloutConfirm = ref<'start' | 'retry' | null>(null)
const pendingRollout = ref<{ kind: 'start' | 'retry'; requestId: string; expectedDetectorRevision: string } | null>(null)
const similarRule = ref<SensitiveRule | null>(null)
const pendingSimilar = ref<PendingMutation | null>(null)
const pendingCreate = ref<PendingMutation | null>(null)
const similarConfirmationRequestId = ref('')
const editingEtag = ref('')
const draft = reactive({
  name: '',
  displayName: '',
  description: '',
  examplesText: '',
  counterExamplesText: '',
  enabled: true,
  deliveryMode: 'confirm' as SensitiveRuleDeliveryMode,
  allowOriginalAfterConfirm: false,
  maskingStrategy: 'full' as 'full' | 'fixed' | 'keep_edges',
  maskingPrefix: 0,
  maskingSuffix: 0,
  maskingReplacement: '*',
})
const baseline = ref('')
let alive = true
let readRevision = 0
let readController: AbortController | undefined
let statusPoller: ReturnType<typeof createSensitiveRuleStatusPoller> | undefined
let unsubscribeScope: (() => void) | undefined

function updateStatusVisibility() {
  statusPoller?.setEnabled(alive && props.enabled !== false && hasProductScope()
    && typeof document !== 'undefined' && document.visibilityState !== 'hidden')
}

function refreshStatus(retryAuthorization = false) {
  applicationStatus.value = null
  statusPoller?.refresh(retryAuthorization)
}

watch(() => props.enabled, updateStatusVisibility)

const selected = computed(() => rules.value.find(rule => rule.ruleId === selectedId.value) || null)
const customRemaining = computed(() => Math.max(0, (summary.value?.maxCustomRules || 0) - (summary.value?.customCount || 0)))
const canCreate = computed(() => !!capabilities.value?.policyWrite && !!summary.value && customRemaining.value > 0)
const canEditSelected = computed(() => {
  const rule = selected.value
  return !!rule && (rule.source === 'custom'
    ? !!capabilities.value?.policyWrite && !rule.immutable && rule.editable !== false
    : !!capabilities.value?.builtinWrite && !!rule.editable && !!rule.editableFields?.length)
})
const canResetSelected = computed(() => !!capabilities.value?.builtinWrite
  && selected.value?.source === 'built_in' && !!selected.value.resettable)
const availableDeliveryModes = computed(() => {
  const minimum = selected.value?.source === 'built_in' ? selected.value.systemConstraints?.minimumDeliveryMode : 'confirm'
  return (['confirm', 'always_mask', 'block'] as SensitiveRuleDeliveryMode[])
    .filter(mode => ({ confirm: 0, always_mask: 1, block: 2 })[mode] >= ({ confirm: 0, always_mask: 1, block: 2 })[minimum || 'confirm'])
})
const availableMaskingStrategies = computed(() => {
  const floor = selected.value?.systemConstraints?.maskingFloor
  const strength = { keep_edges: 0, fixed: 1, full: 2 }
  return (['keep_edges', 'fixed', 'full'] as const).filter(strategy => strength[strategy] >= strength[floor?.strategy || 'keep_edges'])
})
const maskingFloor = computed<SensitiveRuleMaskingFloor | null>(() => selected.value?.systemConstraints?.maskingFloor || null)
const canToggleSelected = computed(() => !!selected.value && (selected.value.source === 'custom'
  ? !!capabilities.value?.policyWrite && !selected.value.immutable && selected.value.editable !== false
  : !!capabilities.value?.builtinWrite && !!selected.value.editable && !!selected.value.editableFields?.includes('enabled')
    && !selected.value.systemConstraints?.requiredEnabled))
const draftSnapshot = () => JSON.stringify(draft)
const dirty = computed(() => editorMode.value !== null && draftSnapshot() !== baseline.value)

function lines(value: string): string[] {
  return value.split(/\r?\n/u).map(item => item.trim()).filter(Boolean)
}

function resetDraft(rule?: SensitiveRule) {
  draft.name = rule?.name || ''
  draft.displayName = rule?.displayName || rule?.name || ''
  draft.description = rule?.description || ''
  draft.examplesText = rule?.examples.join('\n') || ''
  draft.counterExamplesText = rule?.counterExamples.join('\n') || ''
  draft.enabled = rule?.enabled ?? true
  draft.deliveryMode = rule?.deliveryMode || 'confirm'
  draft.allowOriginalAfterConfirm = rule?.allowOriginalAfterConfirm ?? false
  const masking = rule?.masking
  draft.maskingStrategy = masking?.strategy === 'fixed' || masking?.strategy === 'keep_edges' ? masking.strategy : 'full'
  draft.maskingPrefix = typeof masking?.prefixCharacters === 'number' ? masking.prefixCharacters : 0
  draft.maskingSuffix = typeof masking?.suffixCharacters === 'number' ? masking.suffixCharacters : 0
  draft.maskingReplacement = typeof masking?.replacement === 'string' ? masking.replacement : '*'
  baseline.value = draftSnapshot()
}

function choose(ruleId: string) {
  if (dirty.value) {
    error.value = '有未保存修改，请先保存或取消编辑。'
    return
  }
  selectedId.value = ruleId
  editorMode.value = null
  pendingCreate.value = null
  deleteConfirmId.value = ''
  resetConfirmId.value = ''
  pendingReset.value = null
  similarRule.value = null
  pendingSimilar.value = null
  similarConfirmationRequestId.value = ''
  editingEtag.value = ''
  error.value = ''
  notice.value = ''
  resetDraft()
}

function startCreate() {
  if (!canCreate.value || busy.value) return
  if (dirty.value) {
    error.value = '有未保存修改，请先保存或取消编辑。'
    return
  }
  selectedId.value = ''
  editorMode.value = 'new'
  pendingCreate.value = null
  editingEtag.value = ''
  deleteConfirmId.value = ''
  resetConfirmId.value = ''
  pendingReset.value = null
  error.value = ''
  notice.value = ''
  resetDraft()
}

async function startEdit() {
  const rule = selected.value
  if (!rule || !canEditSelected.value || busy.value) return
  const fresh = await readRuleForWrite(rule.ruleId)
  if (!fresh || selectedId.value !== rule.ruleId) return
  editorMode.value = fresh.source === 'built_in' ? 'builtin' : 'existing'
  editingEtag.value = fresh.etag || ''
  pendingCreate.value = null
  deleteConfirmId.value = ''
  resetConfirmId.value = ''
  pendingReset.value = null
  error.value = ''
  notice.value = ''
  resetDraft(fresh)
}

function cancelEdit() {
  editorMode.value = null
  editingEtag.value = ''
  pendingReset.value = null
  pendingCreate.value = null
  similarRule.value = null
  pendingSimilar.value = null
  similarConfirmationRequestId.value = ''
  error.value = ''
  resetDraft()
}

async function refresh(preserveNotice = false, retryAuthorization = false) {
  if (!preserveNotice) refreshStatus(retryAuthorization)
  const ticket = ++readRevision
  readController?.abort()
  readController = new AbortController()
  loading.value = true
  error.value = ''
  if (!preserveNotice) notice.value = ''
  try {
    const [ruleResult, capabilityResult] = await Promise.allSettled([
      api.getSensitiveRules(readController.signal),
      api.getSensitiveRuleCapabilities(readController.signal),
    ])
    if (!alive || ticket !== readRevision) return
    if (ruleResult.status === 'fulfilled') {
      rules.value = ruleResult.value.items
      summary.value = ruleResult.value
      if (selectedId.value && !rules.value.some(rule => rule.ruleId === selectedId.value)) selectedId.value = ''
      if (!selectedId.value && editorMode.value !== 'new') selectedId.value = rules.value[0]?.ruleId || ''
    } else {
      rules.value = []
      summary.value = null
      selectedId.value = ''
      editorMode.value = null
      error.value = ruleResult.reason instanceof Error ? ruleResult.reason.message : '敏感规则暂时无法读取'
    }
    if (capabilityResult.status === 'fulfilled') {
      const oldRollout = capabilities.value?.rolloutManage
      capabilities.value = capabilityResult.value
      capabilityError.value = ''
      if (!capabilities.value.policyWrite) {
        rules.value = []
        summary.value = null
        selectedId.value = ''
        editorMode.value = null
      }
      if (oldRollout !== capabilities.value.rolloutManage) refreshStatus()
    } else {
      capabilities.value = null
      capabilityError.value = capabilityResult.reason instanceof Error
        ? capabilityResult.reason.message : '规则管理权限暂时无法读取'
    }
  } catch (cause) {
    if (alive && ticket === readRevision && !readController.signal.aborted) {
      error.value = cause instanceof Error ? cause.message : '敏感规则暂时无法读取'
    }
  } finally {
    if (alive && ticket === readRevision) loading.value = false
  }
}

async function readRuleForWrite(ruleId: string): Promise<SensitiveRule | null> {
  try {
    const fresh = await api.getSensitiveRule(ruleId)
    if (!fresh.etag) {
      error.value = '未取得规则修改令牌，请重新读取后再试。'
      return null
    }
    upsert(fresh)
    return fresh
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '规则详情暂时无法读取'
    return null
  }
}

function canEditField(rule: SensitiveRule, field: SensitiveRuleEditableField): boolean {
  return rule.source === 'built_in' && !!rule.editableFields?.includes(field)
}

function parseBuiltInDraft(rule: SensitiveRule): Record<string, unknown> | null {
  if (rule.source !== 'built_in' || !rule.editable) return null
  const changed: Record<string, unknown> = {}
  const add = (field: SensitiveRuleEditableField, value: unknown, previous: unknown) => {
    if (canEditField(rule, field) && JSON.stringify(value) !== JSON.stringify(previous)) changed[field] = value
  }
  const displayName = draft.displayName.trim()
  if (canEditField(rule, 'displayName') && displayName !== (rule.displayName || rule.name)
      && (displayName.length < 2 || displayName.length > 50)) {
    error.value = '展示名称需要 2 到 50 个字。'
    return null
  }
  add('displayName', displayName, rule.displayName || rule.name)
  add('enabled', draft.enabled, rule.enabled)
  const description = draft.description.trim()
  if (canEditField(rule, 'description') && description !== rule.description
      && (description.length < 10 || description.length > 500)) {
    error.value = '保护内容说明需要 10 到 500 个字。'
    return null
  }
  add('description', description, rule.description)
  const examples = lines(draft.examplesText)
  const counterExamples = lines(draft.counterExamplesText)
  if (((canEditField(rule, 'examples') && examples.length > 10)
      || (canEditField(rule, 'counterExamples') && counterExamples.length > 10)
      || (canEditField(rule, 'examples') && examples.some(item => item.length > 200))
      || (canEditField(rule, 'counterExamples') && counterExamples.some(item => item.length > 200)))) {
    error.value = '示例和反例分别最多 10 条，每条最多 200 个字。'
    return null
  }
  add('examples', examples, rule.examples)
  add('counterExamples', counterExamples, rule.counterExamples)
  if (!availableDeliveryModes.value.includes(draft.deliveryMode)) {
    error.value = '交付方式不能低于系统安全底线。'
    return null
  }
  add('deliveryMode', draft.deliveryMode, rule.deliveryMode)
  add('allowOriginalAfterConfirm', draft.deliveryMode === 'confirm' && draft.allowOriginalAfterConfirm, rule.allowOriginalAfterConfirm)
  if (canEditField(rule, 'masking')) {
    const floor = maskingFloor.value
    const strategy = draft.maskingStrategy
    if (!availableMaskingStrategies.value.includes(strategy)) {
      error.value = '遮盖方式不能低于系统安全底线。'
      return null
    }
    const prefixCharacters = strategy === 'keep_edges' ? Number(draft.maskingPrefix) : 0
    const suffixCharacters = strategy === 'keep_edges' ? Number(draft.maskingSuffix) : 0
    const prefixLimit = floor?.strategy === 'keep_edges' ? Math.min(32, floor.prefixCharacters ?? 0) : 32
    const suffixLimit = floor?.strategy === 'keep_edges' ? Math.min(32, floor.suffixCharacters ?? 0) : 32
    if (!Number.isInteger(prefixCharacters) || !Number.isInteger(suffixCharacters)
        || prefixCharacters < 0 || prefixCharacters > prefixLimit || suffixCharacters < 0 || suffixCharacters > suffixLimit
        || !draft.maskingReplacement || draft.maskingReplacement.length > 32) {
      error.value = '遮盖配置不符合系统安全底线。'
      return null
    }
    add('masking', { strategy, prefixCharacters, suffixCharacters, replacement: draft.maskingReplacement }, rule.masking)
  }
  if (!Object.keys(changed).length) {
    error.value = '规则没有需要保存的修改。'
    return null
  }
  return changed
}

function parseDraft(): SensitiveRuleDraft | null {
  const name = draft.name.trim()
  if (name.length < 2 || name.length > 50) {
    error.value = '规则名称需要 2 到 50 个字。'
    return null
  }
  const description = draft.description.trim()
  if (description.length < 10 || description.length > 500) {
    error.value = '保护内容说明需要 10 到 500 个字。'
    return null
  }
  const examples = lines(draft.examplesText)
  if (!examples.length) {
    error.value = '请至少填写一条命中示例。'
    return null
  }
  const counterExamples = lines(draft.counterExamplesText)
  if (examples.length > 10 || counterExamples.length > 10) {
    error.value = '命中示例和反例分别最多填写 10 条。'
    return null
  }
  if ([...examples, ...counterExamples].some(item => item.length > 200)) {
    error.value = '每条示例最多 200 个字。'
    return null
  }
  return {
    name,
    description,
    examples,
    counterExamples,
    enabled: draft.enabled,
    deliveryMode: draft.deliveryMode,
    allowOriginalAfterConfirm: draft.deliveryMode === 'confirm' && draft.allowOriginalAfterConfirm,
  }
}

function upsert(rule: SensitiveRule) {
  const index = rules.value.findIndex(item => item.ruleId === rule.ruleId)
  if (index < 0) rules.value = [...rules.value, rule]
  else rules.value = rules.value.map(item => item.ruleId === rule.ruleId ? rule : item)
}

function newRequestId(): string {
  return globalThis.crypto.randomUUID()
}

async function loadSimilar(cause: ApiError, pending: PendingMutation) {
  if (!cause.similarRuleId) return false
  similarRule.value = null
  pendingSimilar.value = null
  similarConfirmationRequestId.value = ''
  try {
    const rule = await api.getSensitiveRule(cause.similarRuleId)
    if (!alive) return true
    similarRule.value = rule
    pendingSimilar.value = pending
    similarConfirmationRequestId.value = ''
    error.value = ''
    return true
  } catch (readError) {
    if (alive) error.value = readError instanceof Error
      ? `发现相似规则，但无法读取详情：${readError.message}`
      : '发现相似规则，但无法读取详情，请重新读取后再试。'
    return true
  }
}

async function commit(pending: PendingMutation, acknowledgeSimilarRuleId?: string, confirmedRequestId?: string) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    let saved: SensitiveRule
    if (pending.kind === 'create') {
      saved = await api.createSensitiveRule({ ...(pending.draft as SensitiveRuleDraft),
        requestId: confirmedRequestId || pending.requestId!,
        ...(acknowledgeSimilarRuleId ? { acknowledgeSimilarRuleId } : {}) })
    } else if (pending.kind === 'update') {
      saved = await api.updateSensitiveRule(pending.ruleId!, { ...(pending.draft as SensitiveRuleDraft),
        expectedEtag: pending.expectedEtag!,
        ...(acknowledgeSimilarRuleId ? { acknowledgeSimilarRuleId } : {}) })
    } else if (pending.kind === 'builtin_update') {
      saved = await api.updateBuiltInSensitiveRule(pending.ruleId!, pending.expectedEtag!, {
        ...pending.draft,
        ...(acknowledgeSimilarRuleId ? { acknowledgeSimilarRuleId } : {}),
      })
    } else {
      saved = await api.resetBuiltInSensitiveRule(pending.ruleId!, pending.expectedEtag!, acknowledgeSimilarRuleId)
    }
    if (!alive) return
    upsert(saved)
    selectedId.value = saved.ruleId
    editorMode.value = null
    pendingCreate.value = null
    similarRule.value = null
    pendingSimilar.value = null
    similarConfirmationRequestId.value = ''
    editingEtag.value = ''
    resetConfirmId.value = ''
    pendingReset.value = null
    resetDraft()
    notice.value = saved.historicalScanRequired || saved.changeImpact === 'semantic_detection'
      ? `“${saved.displayName || saved.name}”已保存。识别语义变更需确认后扫描历史材料；在新规则激活前，相关类别会保守阻断。`
      : `“${saved.displayName || saved.name}”已保存${saved.enabled ? '' : '为停用状态'}。交付方式变更立即生效。`
    refreshStatus()
    await refresh(true)
  } catch (cause) {
    if (!alive) return
    if (cause instanceof ApiError && cause.code === 'CUSTOM_RULE_SIMILAR' && cause.similarRuleId
        && cause.similarRuleId !== acknowledgeSimilarRuleId) {
      await loadSimilar(cause, pending)
    } else if (cause instanceof ApiError && cause.code === 'CUSTOM_RULE_SIMILAR') {
      error.value = '相似规则状态已变化，请重新读取并核对后再操作。'
      similarRule.value = null
      pendingSimilar.value = null
      similarConfirmationRequestId.value = ''
    } else if (cause instanceof ApiError && cause.code === 'CUSTOM_RULE_DUPLICATE') {
      error.value = '已有完全重复的规则。请调整规则内容，不能通过相似确认覆盖。'
    } else if (cause instanceof ApiError && cause.status === 409) {
      error.value = '规则或目标修订已更新，未覆盖当前设置。请重新读取详情后再修改。'
    } else {
      error.value = cause instanceof Error ? cause.message : '规则未保存，请重试。'
    }
    if (pending.kind === 'builtin_reset' && !(cause instanceof ApiError
        && cause.code === 'CUSTOM_RULE_SIMILAR' && !!cause.similarRuleId
        && cause.similarRuleId !== acknowledgeSimilarRuleId)) {
      resetConfirmId.value = ''
      pendingReset.value = null
      pendingSimilar.value = null
      similarRule.value = null
    }
  } finally {
    if (alive) busy.value = false
  }
}

async function save() {
  const rule = selected.value
  if (editorMode.value === 'new') {
    const value = parseDraft()
    if (!value) return
    const current = pendingCreate.value
    const pending = current && JSON.stringify(current.draft) === JSON.stringify(value)
      ? current
      : { kind: 'create' as const, requestId: newRequestId(), draft: value }
    pendingCreate.value = pending
    await commit(pending)
  } else if (editorMode.value === 'existing' && rule?.source === 'custom' && !rule.immutable && editingEtag.value) {
    const value = parseDraft()
    if (!value) return
    await commit({ kind: 'update', ruleId: rule.ruleId, expectedEtag: editingEtag.value, draft: value })
  } else if (editorMode.value === 'builtin' && rule?.source === 'built_in' && editingEtag.value) {
    const value = parseBuiltInDraft(rule)
    if (!value) return
    await commit({ kind: 'builtin_update', ruleId: rule.ruleId, expectedEtag: editingEtag.value, draft: value })
  }
}

async function confirmSimilar() {
  const pending = pendingSimilar.value
  const similar = similarRule.value
  if (!pending || !similar) return
  if (pending.kind === 'create') similarConfirmationRequestId.value ||= newRequestId()
  await commit(pending, similar.ruleId, similarConfirmationRequestId.value || undefined)
}

function dismissSimilar() {
  similarRule.value = null
  pendingSimilar.value = null
  similarConfirmationRequestId.value = ''
  error.value = ''
}

async function toggle(rule: SensitiveRule) {
  if (!canToggleSelected.value || busy.value) return
  const fresh = await readRuleForWrite(rule.ruleId)
  if (!fresh || fresh.enabled !== rule.enabled) {
    if (fresh) error.value = '规则已在别处变化，请核对最新状态后再操作。'
    return
  }
  if (fresh.source === 'built_in') {
    await commit({ kind: 'builtin_update', ruleId: fresh.ruleId, expectedEtag: fresh.etag,
      draft: { enabled: !fresh.enabled } })
    return
  }
  await commit({
    kind: 'update', ruleId: fresh.ruleId, expectedEtag: fresh.etag,
    draft: {
      name: fresh.name,
      description: fresh.description,
      examples: [...fresh.examples],
      counterExamples: [...fresh.counterExamples],
      enabled: !fresh.enabled,
      deliveryMode: fresh.deliveryMode,
      allowOriginalAfterConfirm: fresh.deliveryMode === 'confirm' && fresh.allowOriginalAfterConfirm,
    },
  })
}

async function remove(rule: SensitiveRule) {
  if (rule.source !== 'custom' || !capabilities.value?.policyWrite || rule.deletable === false
      || busy.value || deleteConfirmId.value !== rule.ruleId) return
  const fresh = await readRuleForWrite(rule.ruleId)
  if (!fresh || fresh.revision !== rule.revision || fresh.deletable === false) {
    if (fresh) error.value = '规则已在别处变化，请核对最新内容后重新确认删除。'
    deleteConfirmId.value = ''
    return
  }
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await api.deleteSensitiveRule(rule.ruleId, fresh.etag!)
    if (!alive) return
    rules.value = rules.value.filter(item => item.ruleId !== rule.ruleId)
    selectedId.value = rules.value[0]?.ruleId || ''
    deleteConfirmId.value = ''
    notice.value = `“${rule.displayName || rule.name}”已删除。识别语义变更需确认后扫描历史材料；在新规则激活前，相关类别会保守阻断。`
    refreshStatus()
    await refresh(true)
  } catch (cause) {
    if (alive) error.value = cause instanceof ApiError && cause.status === 409
      ? '规则已在别处更新，未执行删除。请重新读取后再确认。'
      : cause instanceof Error ? cause.message : '规则未删除，请重试。'
  } finally {
    if (alive) busy.value = false
  }
}

async function prepareResetBuiltIn(rule: SensitiveRule) {
  if (!capabilities.value?.builtinWrite || rule.source !== 'built_in' || !rule.resettable || busy.value) return
  busy.value = true
  resetConfirmId.value = ''
  pendingReset.value = null
  try {
    const fresh = await readRuleForWrite(rule.ruleId)
    if (!fresh || fresh.source !== 'built_in' || !fresh.resettable || selectedId.value !== rule.ruleId) return
    pendingReset.value = {
      ruleId: fresh.ruleId, etag: fresh.etag!, displayName: fresh.displayName || fresh.name,
      enabled: fresh.enabled, overridden: !!fresh.overridden,
      overrideNeedsReview: !!fresh.overrideNeedsReview, deliveryMode: fresh.deliveryMode,
    }
    resetConfirmId.value = fresh.ruleId
    error.value = ''
  } finally {
    busy.value = false
  }
}

async function resetBuiltIn(rule: SensitiveRule) {
  const pending = pendingReset.value
  if (!capabilities.value?.builtinWrite || !pending || pending.ruleId !== rule.ruleId
      || resetConfirmId.value !== rule.ruleId || busy.value || !!similarRule.value) return
  let currentEtag: string | null = null
  busy.value = true
  try {
    const fresh = await readRuleForWrite(rule.ruleId)
    if (fresh?.source === 'built_in' && fresh.resettable && selectedId.value === rule.ruleId
        && fresh.etag === pending.etag) currentEtag = pending.etag
    else if (fresh) error.value = '规则已在别处变化，请核对最新内容后重新确认恢复默认。'
  } finally {
    busy.value = false
  }
  if (!currentEtag) {
    resetConfirmId.value = ''
    pendingReset.value = null
    return
  }
  await commit({ kind: 'builtin_reset', ruleId: pending.ruleId, expectedEtag: currentEtag })
}

async function prepareRollout(kind: 'start' | 'retry') {
  if (!capabilities.value?.rolloutManage || busy.value) return
  try {
    const current = await api.getSensitiveRuleRolloutStatus()
    applicationStatus.value = { ...current, rolloutManage: true }
    if (!current.scanEnabled || !current.targetDetectorRevision
        || (kind === 'start' && !current.historicalScanRequired)
        || (kind === 'retry' && !(current.state === 'failed' && current.retryAvailable))) {
      error.value = '历史扫描状态已变化，请核对最新状态。'
      return
    }
    pendingRollout.value = { kind, requestId: newRequestId(), expectedDetectorRevision: current.targetDetectorRevision }
    rolloutConfirm.value = kind
    error.value = ''
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '历史扫描状态暂时无法读取'
  }
}

async function confirmRollout() {
  const pending = pendingRollout.value
  if (!pending || rolloutConfirm.value !== pending.kind || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = pending.kind === 'start'
      ? await api.startSensitiveRuleRollout(pending.requestId, pending.expectedDetectorRevision)
      : await api.retrySensitiveRuleRollout(pending.requestId, pending.expectedDetectorRevision)
    applicationStatus.value = { ...result, rolloutManage: true }
    notice.value = pending.kind === 'start' ? '历史材料扫描已启动；当前活动规则继续服务。' : '失败的历史扫描任务已重新安排。'
    rolloutConfirm.value = null
    pendingRollout.value = null
    resetConfirmId.value = ''
    pendingReset.value = null
    refreshStatus()
  } catch (cause) {
    if (cause instanceof ApiError && (cause.code === 'SENSITIVE_RULE_REVISION_CHANGED'
        || cause.code === 'SENSITIVE_ROLLOUT_RETRY_NOT_ALLOWED' || cause.code === 'IDEMPOTENCY_KEY_REUSED')) {
      rolloutConfirm.value = null
      pendingRollout.value = null
      refreshStatus()
      error.value = '规则或扫描目标已变化，请核对最新状态并重新确认。'
    } else {
      // An uncertain response keeps the exact request identity for an explicit retry.
      error.value = cause instanceof Error ? cause.message : '提交结果暂时无法确认，请稍后重试。'
    }
  } finally {
    busy.value = false
  }
}

function makeStatusPoller() {
  return createSensitiveRuleStatusPoller({
    apply: value => { applicationStatus.value = value },
    read: signal => getSensitiveRuleStatus(signal, !!capabilities.value?.rolloutManage),
    onError: cause => {
      statusError.value = cause
        ? cause instanceof ApiError && (cause.status === 401 || cause.status === 403)
          ? '当前账号无法查看规则应用状态。此状态查询不影响问答；规则管理需要相应权限。'
          : '规则应用状态暂时无法读取，当前活动规则仍继续服务；可继续管理规则和问答。'
        : ''
    },
  })
}

onMounted(() => {
  statusPoller = makeStatusPoller()
  document.addEventListener('visibilitychange', updateStatusVisibility)
  unsubscribeScope = onProductScopeReset(() => {
    readRevision++
    readController?.abort()
    statusPoller?.dispose()
    statusPoller = makeStatusPoller()
    rules.value = []
    summary.value = null
    selectedId.value = ''
    editorMode.value = null
    resetConfirmId.value = ''
    pendingReset.value = null
    resetDraft()
    applicationStatus.value = null
    statusError.value = ''
    capabilities.value = null
    capabilityError.value = ''
    rolloutConfirm.value = null
    pendingRollout.value = null
    if (hasProductScope()) void refresh()
    updateStatusVisibility()
  })
  // The ordinary rule refresh requests status independently; neither read
  // depends on the other's success.
  void refresh()
  updateStatusVisibility()
})
onUnmounted(() => {
  alive = false
  readRevision += 1
  readController?.abort()
  statusPoller?.dispose()
  unsubscribeScope?.()
  if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', updateStatusVisibility)
})

defineExpose({ refresh })
</script>

<template>
  <section class="sensitive-rules" aria-label="知君敏感规则" data-testid="sensitive-rules">
    <header class="sensitive-rules__header">
      <div>
        <strong>知君敏感规则</strong>
        <p>交付方式变更立即生效；识别语义变更需确认后扫描历史材料。</p>
      </div>
      <button type="button" :disabled="loading || busy" @click="refresh(false, true)">{{ loading ? '正在读取…' : '重新读取' }}</button>
    </header>

    <p v-if="applicationStatus" class="sensitive-rules__muted" role="status" data-testid="sensitive-rule-application-status">
      {{ applicationStatus.state === 'active' ? '已应用：当前保存的规则已用于交付。'
        : applicationStatus.state === 'failed' ? '历史扫描失败；当前活动规则继续服务，变化的类别暂时保守阻断。'
        : applicationStatus.historicalScanRequired ? '待扫描：识别规则已保存，需确认后扫描历史材料；变化的类别暂时保守阻断。'
        : applicationStatus.state === 'disabled' ? '历史扫描服务未启用，请联系管理员。'
        : '应用中：历史材料正在扫描，当前活动规则继续服务。' }}
    </p>
    <p v-if="statusError" class="sensitive-rules__warning" role="status" data-testid="sensitive-rule-status-error">{{ statusError }}</p>
    <p v-if="capabilityError" class="sensitive-rules__warning" role="status">规则权限暂时无法读取：{{ capabilityError }}</p>
    <p v-else-if="capabilities && !capabilities.policyWrite" class="sensitive-rules__muted" role="status">当前知君 App 未获敏感规则管理权限，无法查看或修改规则。请管理员为现有 App 追加能力。</p>
    <div v-if="applicationStatus?.rolloutManage && applicationStatus.scanEnabled" class="sensitive-rules__actions">
      <button v-if="applicationStatus.historicalScanRequired" type="button" :disabled="busy" @click="prepareRollout('start')">扫描历史材料</button>
      <button v-if="applicationStatus.state === 'failed' && applicationStatus.retryAvailable" type="button" :disabled="busy" @click="prepareRollout('retry')">重试失败扫描</button>
    </div>
    <p v-else-if="applicationStatus?.historicalScanRequired && !capabilities?.rolloutManage" class="sensitive-rules__muted">需扫描历史材料，请管理员为现有 App 追加历史扫描管理能力。</p>
    <div v-if="rolloutConfirm && pendingRollout" class="sensitive-rules__confirm" role="alertdialog" aria-label="确认历史材料扫描">
      <p>{{ rolloutConfirm === 'start' ? '扫描历史材料可能耗时并占用盒子资源。确认按当前规则启动扫描？' : '确认重试当前规则修订的失败扫描？' }}</p>
      <button type="button" class="sensitive-rules__primary" :disabled="busy" @click="confirmRollout">{{ rolloutConfirm === 'start' ? '确认启动扫描' : '确认重试' }}</button>
      <button type="button" :disabled="busy" @click="rolloutConfirm = null; pendingRollout = null">取消</button>
    </div>

    <div v-if="summary" class="sensitive-rules__capacity" aria-label="规则容量">
      <span>自定义 {{ summary.customCount }} / {{ summary.maxCustomRules }}</span>
      <span>内置 {{ summary.builtinCount }}</span>
      <span>已启用 {{ summary.enabledRuleCount }}</span>
      <span>检测提示 {{ summary.detectorPromptTokens }} / {{ summary.detectorPromptTokenLimit }} tokens</span>
      <span>剩余 {{ summary.detectorPromptTokensRemaining }} tokens</span>
    </div>
    <p v-if="summary && !summary.detectorPromptWithinLimit" class="sensitive-rules__warning" role="alert">
      当前规则已超过检测提示容量，请先停用或精简自定义规则。
    </p>

    <div class="sensitive-rules__toolbar">
      <button type="button" class="sensitive-rules__primary" :disabled="!canCreate || busy" @click="startCreate">
        添加自定义规则
      </button>
      <small v-if="summary && capabilities?.policyWrite && !canCreate">自定义规则容量已满，请先删除不再使用的规则。</small>
      <small v-else-if="summary">还可添加 {{ customRemaining }} 条自定义规则。</small>
    </div>

    <div class="sensitive-rules__layout">
      <nav class="sensitive-rules__list" aria-label="规则列表">
        <button
          v-for="rule in rules"
          :key="rule.ruleId"
          type="button"
          :aria-pressed="selectedId === rule.ruleId"
          @click="choose(rule.ruleId)"
        >
          <span>{{ rule.displayName || rule.name }}</span>
          <small>{{ rule.source === 'built_in' ? '内置' : '自定义' }} · {{ rule.enabled ? '已启用' : '已停用' }}<template v-if="rule.overrideNeedsReview"> · 覆盖待复核</template><template v-else-if="rule.overridden"> · 已覆盖默认</template></small>
        </button>
        <p v-if="!loading && !rules.length">暂时没有可显示的规则。</p>
      </nav>

      <form v-if="editorMode" class="sensitive-rules__editor" @submit.prevent="save">
        <fieldset :disabled="busy || !!similarRule">
          <legend>{{ editorMode === 'new' ? '添加自定义规则' : editorMode === 'builtin' ? '编辑内置规则' : '编辑自定义规则' }}</legend>
          <label v-if="editorMode !== 'builtin'">规则名称<input v-model="draft.name" minlength="2" maxlength="50" autocomplete="off" /></label>
          <label v-if="editorMode === 'builtin' && selected?.editableFields?.includes('displayName')">展示名称<input v-model="draft.displayName" maxlength="50" autocomplete="off" /></label>
          <label v-if="editorMode !== 'builtin' || selected?.editableFields?.includes('description')">保护内容说明<textarea v-model="draft.description" :minlength="editorMode === 'builtin' ? undefined : 10" maxlength="500" rows="3" /></label>
          <label v-if="editorMode !== 'builtin' || selected?.editableFields?.includes('examples')">命中示例<textarea v-model="draft.examplesText" rows="5" placeholder="每行一条；请使用虚构示例，避免填入真实隐私" /><small>最多 10 条，每行一条最多 200 个字。</small></label>
          <label v-if="editorMode !== 'builtin' || selected?.editableFields?.includes('counterExamples')">反例<textarea v-model="draft.counterExamplesText" rows="4" placeholder="每行一条不会命中的例子" /><small>最多 10 条，每行一条最多 200 个字。</small></label>
          <label v-if="editorMode !== 'builtin' || selected?.editableFields?.includes('deliveryMode')">交付方式
            <select v-model="draft.deliveryMode">
              <option v-if="editorMode !== 'builtin' || availableDeliveryModes.includes('confirm')" value="confirm">每次确认</option>
              <option v-if="editorMode !== 'builtin' || availableDeliveryModes.includes('always_mask')" value="always_mask">始终脱敏</option>
              <option v-if="editorMode !== 'builtin' || availableDeliveryModes.includes('block')" value="block">阻止交付</option>
            </select>
          </label>
          <label v-if="editorMode !== 'builtin' || (selected?.editableFields?.includes('enabled') && !selected.systemConstraints?.requiredEnabled)" class="sensitive-rules__check"><input v-model="draft.enabled" type="checkbox" /> 保存后启用</label>
          <label v-if="editorMode !== 'builtin' || selected?.editableFields?.includes('allowOriginalAfterConfirm')" class="sensitive-rules__check"><input v-model="draft.allowOriginalAfterConfirm" type="checkbox" :disabled="draft.deliveryMode !== 'confirm'" /> 确认后允许交付原文</label>
          <template v-if="editorMode === 'builtin' && selected?.editableFields?.includes('masking')">
            <label>遮盖方式<select v-model="draft.maskingStrategy">
              <option v-if="availableMaskingStrategies.includes('keep_edges')" value="keep_edges">保留边缘字符</option>
              <option v-if="availableMaskingStrategies.includes('fixed')" value="fixed">固定遮盖</option>
              <option v-if="availableMaskingStrategies.includes('full')" value="full">完整遮盖</option>
            </select></label>
            <label v-if="draft.maskingStrategy === 'keep_edges'">保留开头字符<input v-model.number="draft.maskingPrefix" type="number" min="0" :max="maskingFloor?.strategy === 'keep_edges' ? maskingFloor.prefixCharacters : 32" /></label>
            <label v-if="draft.maskingStrategy === 'keep_edges'">保留末尾字符<input v-model.number="draft.maskingSuffix" type="number" min="0" :max="maskingFloor?.strategy === 'keep_edges' ? maskingFloor.suffixCharacters : 32" /></label>
            <label>替代字符<input v-model="draft.maskingReplacement" maxlength="32" /></label>
          </template>
          <div class="sensitive-rules__actions">
            <button type="submit" class="sensitive-rules__primary" :disabled="busy">{{ busy ? '正在保存…' : '保存规则' }}</button>
            <button type="button" :disabled="busy" @click="cancelEdit">取消</button>
          </div>
        </fieldset>
      </form>

      <article v-else-if="selected" class="sensitive-rules__detail">
        <div class="sensitive-rules__title">
          <div><strong>{{ selected.displayName || selected.name }}</strong><small>{{ selected.source === 'built_in' ? '内置规则' : '自定义规则' }}</small></div>
          <span :class="{ off: !selected.enabled }">{{ selected.enabled ? '已启用' : '已停用' }}</span>
        </div>
        <p>{{ selected.description }}</p>
        <p v-if="selected.overrideNeedsReview" class="sensitive-rules__warning">覆盖待复核：服务端已安全回退到系统默认。请重新保存覆盖或恢复默认。</p>
        <dl>
          <div><dt>交付方式</dt><dd>{{ selected.deliveryMode === 'confirm' ? '每次确认' : selected.deliveryMode === 'always_mask' ? '始终脱敏' : '阻止交付' }}</dd></div>
          <div><dt>确认后原文</dt><dd>{{ selected.allowOriginalAfterConfirm ? '允许' : '不允许' }}</dd></div>
        </dl>
        <section><strong>命中示例</strong><ul><li v-for="item in selected.examples" :key="item">{{ item }}</li></ul></section>
        <section v-if="selected.counterExamples.length"><strong>反例</strong><ul><li v-for="item in selected.counterExamples" :key="item">{{ item }}</li></ul></section>
        <p v-if="selected.source === 'built_in' && !canEditSelected && !canResetSelected" class="sensitive-rules__muted">内置规则当前不可修改；如需受控覆盖，请管理员授权。</p>
        <div class="sensitive-rules__actions">
          <button v-if="canEditSelected" type="button" :disabled="busy" @click="startEdit">编辑</button>
          <button v-if="canToggleSelected" type="button" :disabled="busy" @click="toggle(selected)">{{ selected.enabled ? '停用' : '启用' }}</button>
          <button v-if="selected.source === 'custom' && capabilities?.policyWrite && selected.deletable !== false" type="button" :disabled="busy" @click="deleteConfirmId = selected.ruleId">删除</button>
          <button v-if="selected.source === 'built_in' && canResetSelected" type="button" :disabled="busy" @click="prepareResetBuiltIn(selected)">恢复系统默认</button>
        </div>
        <div v-if="deleteConfirmId === selected.ruleId" class="sensitive-rules__confirm" role="alertdialog" aria-label="确认删除规则">
          <p>确定删除“{{ selected.displayName || selected.name }}”？识别语义变更后需确认是否扫描历史材料。</p>
          <button type="button" :disabled="busy" @click="remove(selected)">确认删除</button>
          <button type="button" :disabled="busy" @click="deleteConfirmId = ''">取消</button>
        </div>
        <div v-if="resetConfirmId === selected.ruleId && pendingReset" class="sensitive-rules__confirm" role="alertdialog" aria-label="确认恢复系统默认">
          <p>恢复“{{ pendingReset.displayName }}”的系统默认设置？当前{{ pendingReset.enabled ? '已启用' : '已停用' }}，交付方式为{{ pendingReset.deliveryMode === 'confirm' ? '每次确认' : pendingReset.deliveryMode === 'always_mask' ? '始终脱敏' : '阻止交付' }}{{ pendingReset.overrideNeedsReview ? '，覆盖待复核' : pendingReset.overridden ? '，已有覆盖' : '，暂无覆盖' }}。恢复可能重新启用规则；识别语义变化后需确认扫描历史材料。</p>
          <button type="button" :disabled="busy || !!similarRule" @click="resetBuiltIn(selected)">确认恢复</button>
          <button type="button" :disabled="busy" @click="resetConfirmId = ''; pendingReset = null">取消</button>
        </div>
      </article>
    </div>

    <aside v-if="similarRule" class="sensitive-rules__confirm" role="alertdialog" aria-label="确认相似规则">
      <strong>发现相似规则，请再次确认</strong>
      <p>“{{ similarRule.displayName || similarRule.name }}”已经覆盖相近内容：{{ similarRule.description }}</p>
      <ul><li v-for="item in similarRule.examples" :key="item">{{ item }}</li></ul>
      <p>确认两条规则用途不同后继续当前操作。</p>
      <button type="button" class="sensitive-rules__primary" :disabled="busy" @click="confirmSimilar">确认后继续</button>
      <button type="button" :disabled="busy" @click="dismissSimilar">返回修改</button>
    </aside>

    <p v-if="error" class="sensitive-rules__error" role="alert">{{ error }}</p>
    <p v-if="notice" class="sensitive-rules__notice" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.sensitive-rules{display:grid;gap:14px;min-width:0;font-size:13px}.sensitive-rules p{margin:0;line-height:1.6;overflow-wrap:anywhere}.sensitive-rules button,.sensitive-rules input,.sensitive-rules textarea,.sensitive-rules select{font:inherit;color:inherit}.sensitive-rules button{padding:7px 10px;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:8px;background:transparent;cursor:pointer}.sensitive-rules button:disabled{opacity:.5;cursor:default}.sensitive-rules__header,.sensitive-rules__toolbar,.sensitive-rules__actions,.sensitive-rules__title{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.sensitive-rules__header p,.sensitive-rules__muted,.sensitive-rules small{font-size:12px;color:var(--ws-text-secondary-color,#686b66)}.sensitive-rules__capacity{display:flex;gap:8px;flex-wrap:wrap}.sensitive-rules__capacity span{padding:6px 9px;border-radius:999px;background:var(--ws-surface-2,#fbf8f1)}.sensitive-rules__layout{display:grid;grid-template-columns:minmax(180px,.7fr) minmax(0,1.3fr);gap:14px;align-items:start}.sensitive-rules__list{display:grid;gap:7px}.sensitive-rules__list button{display:grid;text-align:left;gap:3px}.sensitive-rules__list button[aria-pressed=true]{border-color:var(--ws-primary-color,#a6452e);background:var(--ws-primary-soft,#fbf1eb)}.sensitive-rules__editor,.sensitive-rules__detail,.sensitive-rules__confirm{padding:14px;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:10px;background:var(--ws-card-bg,#fff)}.sensitive-rules fieldset{display:grid;gap:12px;margin:0;padding:0;border:0}.sensitive-rules legend{margin-bottom:12px;font-weight:600}.sensitive-rules label{display:grid;gap:6px}.sensitive-rules input:not([type=checkbox]),.sensitive-rules textarea,.sensitive-rules select{box-sizing:border-box;width:100%;min-width:0;padding:9px 10px;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:7px;background:var(--ws-card-bg,#fff)}.sensitive-rules textarea{resize:vertical}.sensitive-rules .sensitive-rules__check{display:flex;align-items:center;gap:7px}.sensitive-rules__detail{display:grid;gap:12px}.sensitive-rules__title>div{display:grid;gap:3px}.sensitive-rules__title>span{color:#2f7852}.sensitive-rules__title>span.off{color:var(--ws-text-secondary-color,#686b66)}.sensitive-rules dl{display:grid;gap:7px;margin:0}.sensitive-rules dl div{display:grid;grid-template-columns:100px 1fr;gap:8px}.sensitive-rules dt{color:var(--ws-text-secondary-color,#686b66)}.sensitive-rules dd{margin:0}.sensitive-rules ul{margin:6px 0 0;padding-left:20px}.sensitive-rules__confirm{display:flex;align-items:center;gap:9px;flex-wrap:wrap;background:var(--ws-surface-2,#fbf8f1)}.sensitive-rules__confirm p,.sensitive-rules__confirm ul{flex-basis:100%}.sensitive-rules .sensitive-rules__primary{color:#fff;background:var(--ws-primary-color,#a6452e);border-color:transparent}.sensitive-rules__warning,.sensitive-rules__error{color:var(--ws-primary-color,#a6452e)}.sensitive-rules__notice{padding:10px;border-radius:8px;background:#eef7f1;color:#2f6848}@media(max-width:760px){.sensitive-rules__layout{grid-template-columns:1fr}}
</style>
