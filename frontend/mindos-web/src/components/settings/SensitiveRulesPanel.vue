<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { onProductScopeReset } from '@/shared/productScope'
import { createSensitiveRuleStatusPoller, type SensitiveRuleStatus } from '@/services/sensitiveRuleStatus'
import {
  api,
  ApiError,
  type SensitiveRule,
  type SensitiveRuleDeliveryMode,
  type SensitiveRuleDraft,
  type SensitiveRulesResponse,
} from '@/services/api'

const props = withDefaults(defineProps<{ enabled?: boolean }>(), { enabled: true })

type EditorMode = 'new' | 'existing' | null
type PendingMutation = {
  kind: 'create' | 'update'
  requestId: string
  ruleId?: string
  expectedRevision?: number
  draft: SensitiveRuleDraft
}

const rules = ref<SensitiveRule[]>([])
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
const similarRule = ref<SensitiveRule | null>(null)
const pendingSimilar = ref<PendingMutation | null>(null)
const pendingCreate = ref<PendingMutation | null>(null)
const similarConfirmationRequestId = ref('')
const draft = reactive({
  name: '',
  description: '',
  examplesText: '',
  counterExamplesText: '',
  enabled: true,
  deliveryMode: 'confirm' as SensitiveRuleDeliveryMode,
  allowOriginalAfterConfirm: false,
})
const baseline = ref('')
let alive = true
let readRevision = 0
let readController: AbortController | undefined
let statusPoller: ReturnType<typeof createSensitiveRuleStatusPoller> | undefined
let unsubscribeScope: (() => void) | undefined

function updateStatusVisibility() {
  statusPoller?.setEnabled(alive && props.enabled !== false
    && typeof document !== 'undefined' && document.visibilityState !== 'hidden')
}

function refreshStatus() {
  applicationStatus.value = null
  statusPoller?.refresh()
}

watch(() => props.enabled, updateStatusVisibility)

const selected = computed(() => rules.value.find(rule => rule.ruleId === selectedId.value) || null)
const customRemaining = computed(() => Math.max(0, (summary.value?.maxCustomRules || 0) - (summary.value?.customCount || 0)))
const canCreate = computed(() => !!summary.value && customRemaining.value > 0)
const draftSnapshot = () => JSON.stringify(draft)
const dirty = computed(() => editorMode.value !== null && draftSnapshot() !== baseline.value)

function lines(value: string): string[] {
  return value.split(/\r?\n/u).map(item => item.trim()).filter(Boolean)
}

function resetDraft(rule?: SensitiveRule) {
  draft.name = rule?.name || ''
  draft.description = rule?.description || ''
  draft.examplesText = rule?.examples.join('\n') || ''
  draft.counterExamplesText = rule?.counterExamples.join('\n') || ''
  draft.enabled = rule?.enabled ?? true
  draft.deliveryMode = rule?.deliveryMode || 'confirm'
  draft.allowOriginalAfterConfirm = rule?.allowOriginalAfterConfirm ?? false
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
  similarRule.value = null
  pendingSimilar.value = null
  similarConfirmationRequestId.value = ''
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
  deleteConfirmId.value = ''
  error.value = ''
  notice.value = ''
  resetDraft()
}

function startEdit() {
  const rule = selected.value
  if (!rule || rule.source !== 'custom' || rule.immutable || busy.value) return
  editorMode.value = 'existing'
  pendingCreate.value = null
  deleteConfirmId.value = ''
  error.value = ''
  notice.value = ''
  resetDraft(rule)
}

function cancelEdit() {
  editorMode.value = null
  pendingCreate.value = null
  similarRule.value = null
  pendingSimilar.value = null
  similarConfirmationRequestId.value = ''
  error.value = ''
  resetDraft()
}

async function refresh(preserveNotice = false) {
  if (!preserveNotice) refreshStatus()
  const ticket = ++readRevision
  readController?.abort()
  readController = new AbortController()
  loading.value = true
  error.value = ''
  if (!preserveNotice) notice.value = ''
  try {
    const result = await api.getSensitiveRules(readController.signal)
    if (!alive || ticket !== readRevision) return
    rules.value = result.items
    summary.value = result
    if (selectedId.value && !rules.value.some(rule => rule.ruleId === selectedId.value)) selectedId.value = ''
    if (!selectedId.value && editorMode.value !== 'new') selectedId.value = rules.value[0]?.ruleId || ''
  } catch (cause) {
    if (alive && ticket === readRevision && !readController.signal.aborted) {
      error.value = cause instanceof Error ? cause.message : '敏感规则暂时无法读取'
    }
  } finally {
    if (alive && ticket === readRevision) loading.value = false
  }
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
    const requestId = confirmedRequestId || pending.requestId
    const saved = pending.kind === 'create'
      ? await api.createSensitiveRule({ ...pending.draft, requestId, ...(acknowledgeSimilarRuleId ? { acknowledgeSimilarRuleId } : {}) })
      : await api.updateSensitiveRule(pending.ruleId!, {
          ...pending.draft,
          requestId,
          expectedRevision: pending.expectedRevision!,
          ...(acknowledgeSimilarRuleId ? { acknowledgeSimilarRuleId } : {}),
        })
    if (!alive) return
    upsert(saved)
    selectedId.value = saved.ruleId
    editorMode.value = null
    pendingCreate.value = null
    similarRule.value = null
    pendingSimilar.value = null
    similarConfirmationRequestId.value = ''
    resetDraft()
    notice.value = `“${saved.name}”已保存${saved.enabled ? '' : '为停用状态'}。交付方式变更立即生效；识别语义变更会在后台应用，期间继续使用当前活动规则。`
    refreshStatus()
    await refresh(true)
  } catch (cause) {
    if (!alive) return
    if (!acknowledgeSimilarRuleId && cause instanceof ApiError && cause.similarRuleId) {
      await loadSimilar(cause, pending)
    } else if (cause instanceof ApiError && cause.status === 409) {
      error.value = '规则已在别处更新，未覆盖当前设置。请重新读取后再修改。'
    } else {
      error.value = cause instanceof Error ? cause.message : '规则未保存，请重试。'
    }
  } finally {
    if (alive) busy.value = false
  }
}

async function save() {
  const value = parseDraft()
  if (!value) return
  const rule = selected.value
  if (editorMode.value === 'new') {
    const current = pendingCreate.value
    const pending = current && JSON.stringify(current.draft) === JSON.stringify(value)
      ? current
      : { kind: 'create' as const, requestId: newRequestId(), draft: value }
    pendingCreate.value = pending
    await commit(pending)
  } else if (editorMode.value === 'existing' && rule?.source === 'custom' && !rule.immutable) {
    await commit({ kind: 'update', requestId: newRequestId(), ruleId: rule.ruleId, expectedRevision: rule.revision, draft: value })
  }
}

async function confirmSimilar() {
  const pending = pendingSimilar.value
  const similar = similarRule.value
  if (!pending || !similar) return
  similarConfirmationRequestId.value ||= newRequestId()
  await commit(pending, similar.ruleId, similarConfirmationRequestId.value)
}

function dismissSimilar() {
  similarRule.value = null
  pendingSimilar.value = null
  similarConfirmationRequestId.value = ''
  error.value = ''
}

async function toggle(rule: SensitiveRule) {
  if (rule.source !== 'custom' || rule.immutable || busy.value) return
  await commit({
    kind: 'update',
    requestId: newRequestId(),
    ruleId: rule.ruleId,
    expectedRevision: rule.revision,
    draft: {
      name: rule.name,
      description: rule.description,
      examples: [...rule.examples],
      counterExamples: [...rule.counterExamples],
      enabled: !rule.enabled,
      deliveryMode: rule.deliveryMode,
      allowOriginalAfterConfirm: rule.deliveryMode === 'confirm' && rule.allowOriginalAfterConfirm,
    },
  })
}

async function remove(rule: SensitiveRule) {
  if (rule.source !== 'custom' || rule.immutable || busy.value || deleteConfirmId.value !== rule.ruleId) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await api.deleteSensitiveRule(rule.ruleId, rule.revision)
    if (!alive) return
    rules.value = rules.value.filter(item => item.ruleId !== rule.ruleId)
    selectedId.value = rules.value[0]?.ruleId || ''
    deleteConfirmId.value = ''
    notice.value = `“${rule.name}”已删除。交付方式变更立即生效；识别语义变更会在后台应用，期间继续使用当前活动规则。`
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

onMounted(() => {
  statusPoller = createSensitiveRuleStatusPoller({
    apply: value => { applicationStatus.value = value },
    onError: cause => {
      statusError.value = cause
        ? cause instanceof ApiError && (cause.status === 401 || cause.status === 403)
          ? '当前账号无法查看规则应用状态。此状态查询不影响问答；规则管理需要相应权限。'
          : '规则应用状态暂时无法读取，当前活动规则仍继续服务；可继续管理规则和问答。'
        : ''
    },
  })
  document.addEventListener('visibilitychange', updateStatusVisibility)
  unsubscribeScope = onProductScopeReset(() => {
    statusPoller?.dispose()
    applicationStatus.value = null
    statusError.value = ''
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
        <p>交付方式变更立即生效；识别语义变更会在后台应用，期间当前活动规则继续服务。</p>
      </div>
      <button type="button" :disabled="loading || busy" @click="refresh()">{{ loading ? '正在读取…' : '重新读取' }}</button>
    </header>

    <p v-if="applicationStatus" class="sensitive-rules__muted" role="status" data-testid="sensitive-rule-application-status">
      {{ applicationStatus.applying ? '应用中：识别规则正在后台更新，当前活动规则仍继续服务。' : '已应用：当前保存的规则已用于交付。' }}
    </p>
    <p v-if="statusError" class="sensitive-rules__warning" role="status" data-testid="sensitive-rule-status-error">{{ statusError }}</p>

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
      <small v-if="summary && !canCreate">自定义规则容量已满，请先删除不再使用的规则。</small>
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
          <span>{{ rule.name }}</span>
          <small>{{ rule.source === 'built_in' ? '内置' : '自定义' }} · {{ rule.enabled ? '已启用' : '已停用' }}</small>
        </button>
        <p v-if="!loading && !rules.length">暂时没有可显示的规则。</p>
      </nav>

      <form v-if="editorMode" class="sensitive-rules__editor" @submit.prevent="save">
        <fieldset :disabled="busy || !!similarRule">
          <legend>{{ editorMode === 'new' ? '添加自定义规则' : '编辑自定义规则' }}</legend>
          <label>规则名称<input v-model="draft.name" minlength="2" maxlength="50" autocomplete="off" /></label>
          <label>保护内容说明<textarea v-model="draft.description" minlength="10" maxlength="500" rows="3" /></label>
          <label>命中示例<textarea v-model="draft.examplesText" rows="5" placeholder="每行一条；请使用虚构示例，避免填入真实隐私" /><small>1 到 10 条，每行一条，每条最多 200 个字。</small></label>
          <label>反例<textarea v-model="draft.counterExamplesText" rows="4" placeholder="每行一条不会命中的例子" /><small>最多 10 条，每行一条，每条最多 200 个字。</small></label>
          <label>交付方式
            <select v-model="draft.deliveryMode">
              <option value="confirm">每次确认</option>
              <option value="always_mask">始终脱敏</option>
              <option value="block">阻止交付</option>
            </select>
          </label>
          <label class="sensitive-rules__check"><input v-model="draft.enabled" type="checkbox" /> 保存后启用</label>
          <label class="sensitive-rules__check"><input v-model="draft.allowOriginalAfterConfirm" type="checkbox" :disabled="draft.deliveryMode !== 'confirm'" /> 确认后允许交付原文</label>
          <div class="sensitive-rules__actions">
            <button type="submit" class="sensitive-rules__primary" :disabled="busy">{{ busy ? '正在保存…' : '保存规则' }}</button>
            <button type="button" :disabled="busy" @click="cancelEdit">取消</button>
          </div>
        </fieldset>
      </form>

      <article v-else-if="selected" class="sensitive-rules__detail">
        <div class="sensitive-rules__title">
          <div><strong>{{ selected.name }}</strong><small>{{ selected.source === 'built_in' ? '内置规则' : '自定义规则' }}</small></div>
          <span :class="{ off: !selected.enabled }">{{ selected.enabled ? '已启用' : '已停用' }}</span>
        </div>
        <p>{{ selected.description }}</p>
        <dl>
          <div><dt>交付方式</dt><dd>{{ selected.deliveryMode === 'confirm' ? '每次确认' : selected.deliveryMode === 'always_mask' ? '始终脱敏' : '阻止交付' }}</dd></div>
          <div><dt>确认后原文</dt><dd>{{ selected.allowOriginalAfterConfirm ? '允许' : '不允许' }}</dd></div>
        </dl>
        <section><strong>命中示例</strong><ul><li v-for="item in selected.examples" :key="item">{{ item }}</li></ul></section>
        <section v-if="selected.counterExamples.length"><strong>反例</strong><ul><li v-for="item in selected.counterExamples" :key="item">{{ item }}</li></ul></section>
        <p v-if="selected.source === 'built_in' || selected.immutable" class="sensitive-rules__muted">内置规则由系统维护，不能在这里修改或删除。</p>
        <div v-else class="sensitive-rules__actions">
          <button type="button" :disabled="busy" @click="startEdit">编辑</button>
          <button type="button" :disabled="busy" @click="toggle(selected)">{{ selected.enabled ? '停用' : '启用' }}</button>
          <button type="button" :disabled="busy" @click="deleteConfirmId = selected.ruleId">删除</button>
        </div>
        <div v-if="deleteConfirmId === selected.ruleId" class="sensitive-rules__confirm" role="alertdialog" aria-label="确认删除规则">
          <p>确定删除“{{ selected.name }}”？识别规则更新会在后台完成，期间当前活动规则继续服务。</p>
          <button type="button" :disabled="busy" @click="remove(selected)">确认删除</button>
          <button type="button" :disabled="busy" @click="deleteConfirmId = ''">取消</button>
        </div>
      </article>
    </div>

    <aside v-if="similarRule" class="sensitive-rules__confirm" role="alertdialog" aria-label="确认相似规则">
      <strong>发现相似规则，请再次确认</strong>
      <p>“{{ similarRule.name }}”已经覆盖相近内容：{{ similarRule.description }}</p>
      <ul><li v-for="item in similarRule.examples" :key="item">{{ item }}</li></ul>
      <p>仍要保存当前规则吗？确认后会以一次新的操作提交，并记录你已核对这条相似规则。</p>
      <button type="button" class="sensitive-rules__primary" :disabled="busy" @click="confirmSimilar">仍要保存</button>
      <button type="button" :disabled="busy" @click="dismissSimilar">返回修改</button>
    </aside>

    <p v-if="error" class="sensitive-rules__error" role="alert">{{ error }}</p>
    <p v-if="notice" class="sensitive-rules__notice" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.sensitive-rules{display:grid;gap:14px;min-width:0;font-size:13px}.sensitive-rules p{margin:0;line-height:1.6;overflow-wrap:anywhere}.sensitive-rules button,.sensitive-rules input,.sensitive-rules textarea,.sensitive-rules select{font:inherit;color:inherit}.sensitive-rules button{padding:7px 10px;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:8px;background:transparent;cursor:pointer}.sensitive-rules button:disabled{opacity:.5;cursor:default}.sensitive-rules__header,.sensitive-rules__toolbar,.sensitive-rules__actions,.sensitive-rules__title{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.sensitive-rules__header p,.sensitive-rules__muted,.sensitive-rules small{font-size:12px;color:var(--ws-text-secondary-color,#686b66)}.sensitive-rules__capacity{display:flex;gap:8px;flex-wrap:wrap}.sensitive-rules__capacity span{padding:6px 9px;border-radius:999px;background:var(--ws-surface-2,#fbf8f1)}.sensitive-rules__layout{display:grid;grid-template-columns:minmax(180px,.7fr) minmax(0,1.3fr);gap:14px;align-items:start}.sensitive-rules__list{display:grid;gap:7px}.sensitive-rules__list button{display:grid;text-align:left;gap:3px}.sensitive-rules__list button[aria-pressed=true]{border-color:var(--ws-primary-color,#a6452e);background:var(--ws-primary-soft,#fbf1eb)}.sensitive-rules__editor,.sensitive-rules__detail,.sensitive-rules__confirm{padding:14px;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:10px;background:var(--ws-card-bg,#fff)}.sensitive-rules fieldset{display:grid;gap:12px;margin:0;padding:0;border:0}.sensitive-rules legend{margin-bottom:12px;font-weight:600}.sensitive-rules label{display:grid;gap:6px}.sensitive-rules input:not([type=checkbox]),.sensitive-rules textarea,.sensitive-rules select{box-sizing:border-box;width:100%;min-width:0;padding:9px 10px;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:7px;background:var(--ws-card-bg,#fff)}.sensitive-rules textarea{resize:vertical}.sensitive-rules .sensitive-rules__check{display:flex;align-items:center;gap:7px}.sensitive-rules__detail{display:grid;gap:12px}.sensitive-rules__title>div{display:grid;gap:3px}.sensitive-rules__title>span{color:#2f7852}.sensitive-rules__title>span.off{color:var(--ws-text-secondary-color,#686b66)}.sensitive-rules dl{display:grid;gap:7px;margin:0}.sensitive-rules dl div{display:grid;grid-template-columns:100px 1fr;gap:8px}.sensitive-rules dt{color:var(--ws-text-secondary-color,#686b66)}.sensitive-rules dd{margin:0}.sensitive-rules ul{margin:6px 0 0;padding-left:20px}.sensitive-rules__confirm{display:flex;align-items:center;gap:9px;flex-wrap:wrap;background:var(--ws-surface-2,#fbf8f1)}.sensitive-rules__confirm p,.sensitive-rules__confirm ul{flex-basis:100%}.sensitive-rules .sensitive-rules__primary{color:#fff;background:var(--ws-primary-color,#a6452e);border-color:transparent}.sensitive-rules__warning,.sensitive-rules__error{color:var(--ws-primary-color,#a6452e)}.sensitive-rules__notice{padding:10px;border-radius:8px;background:#eef7f1;color:#2f6848}@media(max-width:760px){.sensitive-rules__layout{grid-template-columns:1fr}}
</style>
