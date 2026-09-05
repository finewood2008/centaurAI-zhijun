<script setup lang="ts">
// P1（§4）：设置页「模型与运行时」。
// 复用双通道划分：材料处理固定本地 Ollama；对话问答可显式配置并授权的外部 OpenAI 兼容 API。
// 契约：/api/system/models/*（require_local + revision 乐观锁）；test 提交表单暂存值不持久化。
import { computed, onMounted, onUnmounted, ref } from 'vue'
import RoutingPanel from '@/components/conversation/RoutingPanel.vue'
import ExternalProvidersPanel from '@/components/conversation/ExternalProvidersPanel.vue'
import {
  Activity,
  Check,
  CircuitBoard,
  Cpu,
  Database,
  Download,
  HardDrive,
  MemoryStick,
  MessageSquare,
  Play,
  Power,
  RefreshCw,
  Server,
  Square,
  TriangleAlert,
  X,
} from 'lucide-vue-next'
import {
  api,
  ApiError,
  getNudgePolicy,
  putNudgePolicy,
  getMemoryPolicy,
  putMemoryPolicy,
  type ChatProviderConfig,
  type ChatProviderTestResult,
  type ListModelJobsResponse,
  type MaterialModelsResponse,
  type MaterialRuntimeConfig,
  type MindosPipelineMonitor,
  type ModelJob,
  type ModelJobType,
  type MonitorResponse,
  type MemoryPolicy,
  type RuntimeTestResult,
} from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { useToast } from '@/composables/useToast'

const toast = useToast()

// ---- 关系设置：提醒开关与每日上限（/api/mindos/nudges/policy）----
const nudgeEnabled = ref(true)
const nudgeMax = ref(3)
const nudgeSaving = ref(false)
const nudgeLoaded = ref(false)

async function loadNudgePolicy() {
  try {
    const policy = await getNudgePolicy()
    nudgeEnabled.value = policy.enabled
    nudgeMax.value = policy.maxPerDay
    nudgeLoaded.value = true
  } catch {
    nudgeLoaded.value = false
  }
}

async function saveNudgePolicy() {
  if (nudgeSaving.value) return
  nudgeSaving.value = true
  try {
    const policy = await putNudgePolicy({ enabled: nudgeEnabled.value, maxPerDay: nudgeMax.value })
    nudgeEnabled.value = policy.enabled
    nudgeMax.value = policy.maxPerDay
    toast({ type: 'success', message: '已记住' })
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '保存失败' })
    await loadNudgePolicy()
  } finally {
    nudgeSaving.value = false
  }
}

// 记忆整理偏好独立于主动回访和外发授权；只影响之后的新理解。
const memoryPolicy = ref<MemoryPolicy | null>(null)
const memoryMode = ref<MemoryPolicy['mode']>('important')
const memorySaving = ref(false)
const memoryError = ref('')

async function loadMemoryPolicy() {
  try {
    memoryPolicy.value = await getMemoryPolicy()
    memoryMode.value = memoryPolicy.value.mode
    memoryError.value = ''
  } catch {
    memoryError.value = '记忆整理偏好暂时无法读取，请重试。'
  }
}

async function saveMemoryPolicy() {
  if (!memoryPolicy.value || memorySaving.value) return
  memorySaving.value = true
  memoryError.value = ''
  try {
    memoryPolicy.value = await putMemoryPolicy({ mode: memoryMode.value, expectedRevision: memoryPolicy.value.revision })
    memoryMode.value = memoryPolicy.value.mode
    toast({ type: 'success', message: '记忆整理偏好已保存，现有记忆不受影响' })
  } catch (err) {
    await loadMemoryPolicy()
    memoryError.value = err instanceof ApiError && err.status === 409
      ? '偏好已在别处更新，已读取最新状态；如需更改，请重新选择。'
      : '偏好未保存，请重试；原设置继续生效。'
  } finally {
    memorySaving.value = false
  }
}

const loading = ref(true)
const loadError = ref('')
const loadingSection = ref<'' | 'material' | 'chat'>('')

// ---- 材料处理（本地 Ollama）----
const mRevision = ref<number | null>(null)
const mSource = ref<'defaults' | 'runtime_settings'>('defaults')
const mBaseUrl = ref('')
const mModel = ref('')
const mTimeout = ref<number>(60)
const mAppliesTo = ref<string[]>([])
const mHealth = ref<MaterialRuntimeConfig['health'] | null>(null)
const mSavedModel = ref('') // 已保存模型，用于「模型变更」判定
const mTesting = ref(false)
const mInferenceTesting = ref(false)
const mSaving = ref(false)
const mTest = ref<RuntimeTestResult | null>(null)
const mInferenceTest = ref<RuntimeTestResult | null>(null)
const mAvailableModels = ref<string[]>([])

// ---- 对话问答（外部 LLM）----
const cRevision = ref<number | null>(null)
const cSource = ref<'defaults' | 'runtime_settings'>('defaults')
const cProvider = ref<'ollama' | 'openai'>('ollama')
const cExternal = ref(false)
const cBaseUrl = ref('')
const cModel = ref('')
const cFallback = ref(false)
const cTimeout = ref<number>(60)
const cBudget = ref<number>(90)
const cEffective = ref<'ollama' | 'openai'>('ollama')
const cTesting = ref(false)
const cSaving = ref(false)
const cTest = ref<ChatProviderTestResult | null>(null)
const cProviderBusy = ref(false)

const healthReachable = computed(() =>
  mHealth.value ? mHealth.value.reachable : false,
)
const healthVersion = computed(() => (mHealth.value?.version ? mHealth.value.version : null))

// 本地问答模型固定使用材料处理模型（§3 本地问答统一规则）
const chatLocalModel = computed(() => mModel.value || '—')

const APPLIES_TO_LABEL: Record<string, string> = {
  summary: '摘要',
  entities: '实体',
  relations: '关系三元组',
  tags: '标签',
  contentDrafts: '内容生成草稿',
  wiki: 'Wiki 自动整理',
}

async function loadAll() {
  loading.value = true
  loadError.value = ''
  try {
    const [m, c] = await Promise.all([
      api.getMaterialRuntime(),
      api.getChatProvider(),
    ])
    applyMaterial(m)
    applyChat(c)
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '配置加载失败'
  } finally {
    loading.value = false
  }
}

function applyMaterial(cfg: MaterialRuntimeConfig) {
  mRevision.value = cfg.revision
  mSource.value = cfg.source
  mBaseUrl.value = cfg.baseUrl
  mModel.value = cfg.model
  mTimeout.value = cfg.timeoutSeconds
  mAppliesTo.value = cfg.appliesTo
  mHealth.value = cfg.health
  mSavedModel.value = cfg.model
  mTest.value = null
  mInferenceTest.value = null
  mAvailableModels.value = []
}

function applyChat(cfg: ChatProviderConfig, preserveTiming = false) {
  cRevision.value = cfg.revision
  cSource.value = cfg.source
  cProvider.value = cfg.provider
  cExternal.value = cfg.externalEnabled
  cBaseUrl.value = cfg.baseUrl ?? ''
  cModel.value = cfg.model ?? ''
  cFallback.value = false // Interactive routing never silently downgrades.
  if (!preserveTiming) {
    cTimeout.value = cfg.timeoutSeconds
    cBudget.value = cfg.totalBudgetSeconds
  }
  cEffective.value = cfg.effectiveProvider
  cTest.value = null
}

// ---- 乐观锁冲突保留草稿交互：冲突时不静默重置两表，而是提示用户再决定 ----
const conflictPrompt = ref(false)
const conflictFor = ref<'material' | 'chat'>('material')

function onConflictKeepEditing() {
  conflictPrompt.value = false
}

function onConflictLoadLatest() {
  conflictPrompt.value = false
  loadAll() // 用户明确选择加载最新，才重置两表
}

// 基于统一错误体 code === 'conflict'（409）判定乐观锁冲突，不依赖中文本地化文案。
function isConflictError(e: unknown): boolean {
  return e instanceof ApiError && e.code === 'conflict'
}

// ---- 材料处理 ----
async function saveMaterial() {
  if (!mBaseUrl.value.trim() || !mModel.value.trim()) {
    toast({ type: 'error', message: '请填写服务地址与模型名' })
    return
  }
  if (!mTimeout.value || mTimeout.value < 10 || mTimeout.value > 600) {
    toast({ type: 'error', message: '超时需在 10–600 秒之间' })
    return
  }
  mSaving.value = true
  try {
    const modelChanged = mSavedModel.value && mSavedModel.value !== mModel.value.trim()
    // PUT 响应仅含已保存字段，缺 health/appliesTo 等只读展示项；保存后重拉完整 GET 状态。
    await api.putMaterialRuntime({
      baseUrl: mBaseUrl.value.trim(),
      model: mModel.value.trim(),
      timeoutSeconds: mTimeout.value,
      revision: mRevision.value,
    })
    applyMaterial(await api.getMaterialRuntime())
    toast({ type: 'success', message: '材料处理配置已保存' })
    if (modelChanged) {
      toast({ type: 'info', message: '仅新材料使用新模型；历史派生数据需通过既有重处理入口更新' })
    }
  } catch (e) {
    if (isConflictError(e)) {
      conflictFor.value = 'material'
      conflictPrompt.value = true
      return // 保留当前草稿，交由确认对话框决定是否加载最新
    }
    toast({ type: 'error', message: e instanceof Error ? e.message : '保存失败' })
  } finally {
    mSaving.value = false
  }
}

async function testMaterial() {
  mTesting.value = true
  mTest.value = null
  mAvailableModels.value = []
  try {
    const res = await api.testMaterialRuntime({
      baseUrl: mBaseUrl.value.trim() || undefined,
      model: mModel.value.trim() || undefined,
      timeoutSeconds: mTimeout.value || undefined,
    })
    mTest.value = res
    mAvailableModels.value = res.models ?? []
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '测试失败' })
  } finally {
    mTesting.value = false
  }
}

async function testMaterialInference() {
  mInferenceTesting.value = true
  mInferenceTest.value = null
  try {
    mInferenceTest.value = await api.testMaterialRuntimeInference({
      baseUrl: mBaseUrl.value.trim() || undefined,
      model: mModel.value.trim() || undefined,
      timeoutSeconds: mTimeout.value || undefined,
    })
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '试运行失败' })
  } finally {
    mInferenceTesting.value = false
  }
}

function refreshMaterial() {
  api
    .getMaterialRuntime()
    .then(applyMaterial)
    .catch((e) => toast({ type: 'error', message: e instanceof Error ? e.message : '刷新失败' }))
}

// ---- 对话问答 ----
async function disableExternalChatImmediately() {
  // 关闭外发是安全动作，不能要求用户再点击一次“保存”。先读取服务端当前值，
  // 仅变更 externalEnabled，避免把页面里尚未确认的 URL/模型草稿一并持久化。
  if (cSaving.value) return
  cSaving.value = true
  try {
    const current = await api.getChatProvider()
    await api.putChatProvider({
      provider: current.provider,
      externalEnabled: false,
      baseUrl: current.baseUrl,
      model: current.model,
      timeoutSeconds: current.timeoutSeconds,
      totalBudgetSeconds: current.totalBudgetSeconds,
      fallbackOllama: current.fallbackOllama,
      revision: current.revision,
    })
    applyChat(await api.getChatProvider())
    toast({ type: 'success', message: '已关闭外部问答，后续请求将使用本地 Ollama' })
  } catch (e) {
    // 失败后重新读取权威状态，不能让 UI 显示“已关闭”而后端实际仍在外发。
    try {
      applyChat(await api.getChatProvider())
    } catch {
      // 保留原始错误提示；后端不可达时无法安全推断当前状态。
    }
    toast({ type: 'error', message: e instanceof Error ? e.message : '关闭外部问答失败' })
  } finally {
    cSaving.value = false
  }
}

async function saveChat() {
  // Credentials and selected models are managed only by ExternalProvidersPanel.
  cSaving.value = true
  try {
    await api.putChatProvider({
      provider: cProvider.value,
      externalEnabled: cExternal.value,
      baseUrl: cBaseUrl.value.trim() || null,
      model: cModel.value.trim() || null,
      timeoutSeconds: cTimeout.value,
      totalBudgetSeconds: cBudget.value,
      fallbackOllama: cFallback.value,
      revision: cRevision.value,
    })
    applyChat(await api.getChatProvider())
    toast({ type: 'success', message: '请求超时设置已保存' })
  } catch (e) {
    if (isConflictError(e)) {
      conflictFor.value = 'chat'
      conflictPrompt.value = true
      return // 保留当前草稿，交由确认对话框决定是否加载最新
    }
    toast({ type: 'error', message: e instanceof Error ? e.message : '保存失败' })
  } finally {
    cSaving.value = false
  }
}

async function testChat() {
  cTesting.value = true
  cTest.value = null
  try {
    const payload:
      | { provider: 'ollama'; externalEnabled: false }
      | {
          provider: 'openai'
          externalEnabled: boolean
          baseUrl?: string
          model?: string
          apiKey?: string
          timeoutSeconds?: number
          totalBudgetSeconds?: number
          fallbackOllama?: boolean
        } =
      !cExternal.value || cProvider.value === 'ollama'
        ? { provider: 'ollama', externalEnabled: false }
        : {
            provider: 'openai',
            externalEnabled: cExternal.value,
            baseUrl: cBaseUrl.value.trim() || undefined,
            model: cModel.value.trim() || undefined,
            timeoutSeconds: cTimeout.value,
            totalBudgetSeconds: cBudget.value,
            fallbackOllama: cFallback.value,
          }
    const res = await api.testChatProvider(payload)
    cTest.value = res
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '测试失败' })
  } finally {
    cTesting.value = false
  }
}

function refreshChat() {
  api
    .getChatProvider()
    .then(applyChat)
    .catch((e) => toast({ type: 'error', message: e instanceof Error ? e.message : '刷新失败' }))
}

function errorLabel(code: string | undefined): string {
  return (
    { timeout: '超时', connection: '连接失败', model_not_installed: '指定模型未安装' }[code ?? ''] ?? (code ? `HTTP ${code}` : '失败')
  )
}

// =====================================================================
// P2（§7 / §8）：运行监控与任务
// =====================================================================
// 资源监控 / 模型任务走 require_local 管理路由，采样端缓存最短 2 秒（避免轮询放大）。

const m2Models = ref<MaterialModelsResponse>({ reachable: false, models: [] })
const m2Monitor = ref<MonitorResponse | null>(null)
const m2MindosPipeline = ref<MindosPipelineMonitor | null>(null)
const m2Jobs = ref<ListModelJobsResponse>({ items: [], nextCursor: null })
const m2Busy = ref<{ key: string } | null>(null)
const m2PullModel = ref('') // 拉取输入框：临时模型名，不写入配置
const m2MonitorError = ref('')

const m2RunningModels = computed(() => {
  // 全局指示 Ollama 是否已有加载中的模型（数量>0），具体清单见重载模型健康/加载任务。
  return (m2Monitor.value?.resource.ollama.runningCount ?? 0) > 0
})

const JOB_STATE_LABEL: Record<ModelJobType | string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancel_requested: '取消中',
  cancelled: '已取消',
}
const JOB_STATE_CLASS: Record<ModelJobType | string, string> = {
  queued: 'is-queued',
  running: 'is-running',
  succeeded: 'is-ok',
  failed: 'is-bad',
  cancel_requested: 'is-cancelling',
  cancelled: 'is-cancelled',
}
const MODEL_ACTION_LABEL: Record<ModelJobType, string> = {
  pull: '拉取',
  load: '加载',
  unload: '卸载',
}

function fmtBytes(n: number | null | undefined): string {
  if (n == null || n < 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)} ${units[i]}`
}

function fmtPct(v: number | null | undefined): string {
  return v == null ? '—' : `${Math.round(v)}%`
}

// CPU 首采返回 stale=true + value=null：区分「尚未采样」与「可用/不可用」三类。
function cpuText(cpu: MonitorResponse['resource']['cpu']): string {
  if (!cpu.available) return '不可用'
  if (cpu.stale || cpu.value == null) return '采样中…'
  return `${Math.round(cpu.value)}%`
}

// 以下辅助函数一律接收「可能为空」的资源对象，配合模板全程可选链使用，
// 避免在模板内依赖 vue-tsc 对 v-if 的可选链收窄而误判非空。

function memText(mem: MonitorResponse['resource']['memory'] | undefined): string {
  if (!mem?.available || mem.totalBytes == null || mem.availableBytes == null) return '—'
  return `${fmtBytes(mem.totalBytes - mem.availableBytes)} / ${fmtBytes(mem.totalBytes)}`
}

function gpuMemText(gpu: MonitorResponse['resource']['gpu'] | undefined): string {
  if (!gpu?.available || gpu.memoryUsedBytes == null || gpu.memoryTotalBytes == null) return '—'
  return `${fmtBytes(gpu.memoryUsedBytes)} / ${fmtBytes(gpu.memoryTotalBytes)}`
}

async function m2LoadModels() {
  try {
    m2Models.value = await api.getMaterialModels()
  } catch (e) {
    m2Models.value = { reachable: false, errorCode: 'query_error', models: [] }
  }
}

async function m2MonitorTick() {
  // 懒加载监控：先保证模型/任务列表，再拉聚合监控；容错不打断页面其余配置。
  try {
    m2Monitor.value = await api.getMonitor()
  } catch {
    m2MonitorError.value = '运行监控不可用'
    return
  }
  m2MonitorError.value = ''
}

async function m2MindosPipelineTick() {
  try {
    m2MindosPipeline.value = await api.getMindosPipelineMonitor()
  } catch {
    m2MindosPipeline.value = null
  }
}

async function m2LoadJobs() {
  try {
    m2Jobs.value = await api.listModelJobs({ limit: 12 })
  } catch {
    m2Jobs.value = { items: [], nextCursor: null }
  }
}

async function m2RefreshAll() {
  await Promise.allSettled([m2LoadModels(), m2MonitorTick(), m2MindosPipelineTick(), m2LoadJobs()])
}

async function m2RunAction(type: ModelJobType, model: string) {
  const key = `${type}:${model}`
  if (m2Busy.value) return // 单任务串行，避免并发提交歧义
  m2Busy.value = { key }
  try {
    const res =
      type === 'pull'
        ? await api.pullModel(model)
        : type === 'load'
          ? await api.loadModel(model)
          : await api.unloadModel(model)
    if (res.deduplicated) {
      toast({ type: 'info', message: `已在排队：${MODEL_ACTION_LABEL[type]} ${model}（去重）` })
    } else {
      toast({ type: 'success', message: `已提交：${MODEL_ACTION_LABEL[type]} ${model}` })
    }
    if (type === 'pull') m2PullModel.value = ''
    await m2RefreshAll()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : `${MODEL_ACTION_LABEL[type]}提交失败` })
  } finally {
    m2Busy.value = null
  }
}

function m2BusyFor(key: string): boolean {
  return m2Busy.value?.key === key
}

async function m2Cancel(job: ModelJob) {
  try {
    await api.cancelModelJob(job.jobId)
    toast({ type: 'info', message: '已请求取消任务' })
    await m2LoadJobs()
    await m2MonitorTick()
  } catch (e) {
    toast({ type: 'error', message: e instanceof Error ? e.message : '取消失败' })
  }
}

// 一次性模型列表在挂载时加载；聚合监控与任务列表开启 6 秒轮询（后端采样缓存 2s）。
let m2TimerHandle: number | null = null

onMounted(() => {
  void loadNudgePolicy()
  void loadMemoryPolicy()
  loadAll()
  void m2LoadModels()
  void m2RefreshAll()
  m2TimerHandle = window.setInterval(() => {
    void m2MonitorTick()
    void m2MindosPipelineTick()
    void m2LoadJobs()
  }, 6000)
})

onUnmounted(() => {
  if (m2TimerHandle != null) {
    window.clearInterval(m2TimerHandle)
    m2TimerHandle = null
  }
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1>偏好</h1>
      <p>关系怎么处、用哪个模型、什么能出设备。改动只对之后的对话生效。</p>
    </div>

    <!-- ============ 关系设置 ============ -->
    <section class="rt-section">
      <header class="rt-section__head">
        <div class="rt-section__title">
          <span class="rt-section__icon"><Activity :size="16" aria-hidden="true" /></span>
          <h2>关系设置</h2>
        </div>
      </header>
      <div class="rt-form">
        <label class="rt-field is-switch">
          <span class="rt-field__line">
            <span>让知君主动提醒我</span>
            <span class="rt-toggle">
              <input v-model="nudgeEnabled" type="checkbox" role="switch" :disabled="nudgeSaving || !nudgeLoaded" @change="saveNudgePolicy" />
              <span class="rt-toggle__track" aria-hidden="true" />
            </span>
          </span>
          <span class="rt-hint">只在有事的时候：判断到了回访日、承诺到期、一周攒了些东西值得回顾。每条都会说明为何现在，都可以划掉或永久静默。</span>
        </label>
        <label class="rt-field is-narrow" :class="{ 'is-disabled': !nudgeEnabled }">
          每天最多几条
          <select v-model.number="nudgeMax" :disabled="nudgeSaving || !nudgeLoaded || !nudgeEnabled" @change="saveNudgePolicy">
            <option :value="1">1 条</option>
            <option :value="2">2 条</option>
            <option :value="3">3 条</option>
            <option :value="5">5 条</option>
          </select>
        </label>
        <p class="rt-note">哪些话题不想让知君主动提、AI 不该替你决定什么，写在<RouterLink to="/me/charter" class="rt-link">「人生章程」</RouterLink>里。</p>
      </div>
    </section>

    <section class="rt-section" data-testid="memory-policy">
      <header class="rt-section__head">
        <div class="rt-section__title">
          <span class="rt-section__icon"><MemoryStick :size="16" aria-hidden="true" /></span>
          <h2>记忆整理</h2>
        </div>
      </header>
      <div class="rt-form">
        <label class="rt-field">
          什么时候提出新的理解
          <select v-model="memoryMode" :disabled="!memoryPolicy || memorySaving" @change="saveMemoryPolicy">
            <option value="important">仅重要内容提醒</option>
            <option value="manual">仅在我要求时整理</option>
          </select>
          <span v-if="memoryMode === 'important'" class="rt-hint">只挑值得长期保留的内容请你核对，每次只提醒一条。继续聊出新的重要信息后才再次提醒；其余候选可随时在对话的「待核对」里查看，事件细节合并到可选小结中。</span>
          <span v-else class="rt-hint">平常只保留对话，不自动整理新的本体理解；说“请记住……”时再整理。</span>
        </label>
        <p class="rt-note">不删除或改写已有记忆，也不改变自我贴合度。这个设置不授予在线模型任何新的资料权限。</p>
        <p v-if="memoryError" role="alert" class="rt-note">{{ memoryError }} <button type="button" class="rt-link" :disabled="memorySaving" @click="loadMemoryPolicy">重新读取</button></p>
      </div>
    </section>

    <ErrorState v-if="loadError" :message="loadError" retry-label="重试" @retry="loadAll" />
    <div v-else-if="loading" class="loading-state">正在加载模型配置…</div>

    <template v-else>
      <!-- ============ 模型与隐私 ============ -->
      <section class="rt-section">
        <header class="rt-section__head">
          <div class="rt-section__title">
            <span class="rt-section__icon"><MessageSquare :size="16" aria-hidden="true" /></span>
            <h2>日常对话与理解</h2>
          </div>
          <span class="rt-section__source" :class="cSource === 'defaults' ? 'is-default' : 'is-custom'">
            {{ cSource === 'defaults' ? '部署默认值' : '运行时设置' }}
          </span>
        </header>

        <div class="rt-form">
          <RoutingPanel />
          <ExternalProvidersPanel :chat-revision="cRevision" :disabled="cSaving || cTesting" @activated="applyChat($event, true)" @busy="cProviderBusy = $event" />
          <p class="rt-note">{{ cExternal ? '在线通道已启用；仅在对话选择在线理解后使用。' : '在线通道已暂停；本地处理仍可用。' }}<button v-if="cExternal" type="button" class="rt-link" :disabled="cSaving || cProviderBusy" @click="disableExternalChatImmediately">暂停在线通道，使用本地</button></p>
          <div class="rt-field is-none-label">
            <span class="rt-hint">外部关闭时，对话用本机模型 <code>{{ chatLocalModel }}</code>。</span>
          </div>

          <details class="rt-more">
            <summary>更多</summary>
            <p>在线失败时保留消息，提供重试在线或改用本地；不自动回退、不切换服务。</p>
            <div class="rt-form__row">
              <label class="rt-field is-narrow">
                请求超时（秒）
                <input v-model.number="cTimeout" type="number" min="1" step="1" />
              </label>
              <label class="rt-field is-narrow">
                总预算（秒）
                <input v-model.number="cBudget" type="number" min="1" step="1" />
              </label>
            </div>
          </details>
        </div>

        <div class="rt-actions">
          <BaseButton variant="secondary" size="sm" @click="testChat" :loading="cTesting" :disabled="cProviderBusy || cSaving">
            <template v-if="!cTesting">测试当前通道</template>
            <template v-else>测试中…</template>
          </BaseButton>
          <BaseButton variant="primary" @click="saveChat" :loading="cSaving" :disabled="cTesting || cProviderBusy">
            保存超时设置
          </BaseButton>
          <button type="button" class="rt-refresh" title="重新读取" aria-label="刷新对话问答配置" @click="refreshChat">
            <RefreshCw :size="14" aria-hidden="true" />
          </button>
        </div>

        <div v-if="cTest" class="rt-test" :class="cTest.ok ? 'is-ok' : 'is-bad'">
          <component :is="cTest.ok ? Check : X" :size="14" aria-hidden="true" />
          <span v-if="cTest.ok">
            连接成功 · {{ cTest.provider }} / {{ cTest.model }} · {{ cTest.latencyMs }}ms
          </span>
          <span v-else>连接失败 · {{ errorLabel(cTest.errorCode) }} · {{ cTest.latencyMs }}ms</span>
        </div>
      </section>


      <!-- ============ 高级 · 运行时管理台 ============ -->
      <details class="rt-adv">
        <summary>高级 · 运行时管理台</summary>
        <p class="rt-note">材料处理用哪个本机模型、机器资源、模型任务。平时不用碰。</p>
      <!-- ============ 材料处理（本地 Ollama） ============ -->
      <section class="rt-section">
        <header class="rt-section__head">
          <div class="rt-section__title">
            <span class="rt-section__icon is-local"><Server :size="16" aria-hidden="true" /></span>
            <h2>本地文件处理（Ollama）</h2>
          </div>
          <span class="rt-section__source" :class="mSource === 'defaults' ? 'is-default' : 'is-custom'">
            {{ mSource === 'defaults' ? '部署默认值' : '运行时设置' }}
          </span>
        </header>

        <!-- 服务状态 -->
        <div class="rt-health" role="group" aria-label="Ollama 服务状态">
          <span class="rt-health__item" :class="healthReachable ? 'is-ok' : 'is-bad'">
            {{ healthReachable ? '可达' : '不可达' }}
          </span>
          <span class="rt-health__item" :class="mHealth?.modelInstalled ? 'is-ok' : 'is-warn'">
            {{ mHealth?.modelInstalled ? '模型已安装' : '模型未安装' }}
          </span>
          <span class="rt-health__item" :class="mHealth?.modelRunning ? 'is-ok' : 'is-muted'">
            {{ mHealth?.modelRunning ? '已加载' : '未加载' }}
          </span>
          <span v-if="healthVersion" class="rt-health__version">Ollama v{{ healthVersion }}</span>
        </div>

        <div class="rt-form">
          <label class="rt-field">
            服务地址
            <input v-model="mBaseUrl" type="url" placeholder="http://127.0.0.1:11434" />
          </label>
          <label class="rt-field">
            材料处理模型
            <input v-model="mModel" list="material-model-options" type="text" placeholder="先测试连通性，再从已安装模型中选择" />
            <datalist id="material-model-options">
              <option v-for="name in mAvailableModels" :key="name" :value="name" />
            </datalist>
            <span class="rt-hint">
              {{ mAvailableModels.length ? `已读取 ${mAvailableModels.length} 个已安装模型，可从下拉列表选择。` : '测试连通性后显示当前地址的已安装模型列表。' }}
            </span>
          </label>
          <label class="rt-field is-narrow">
            超时（秒）
            <input v-model.number="mTimeout" type="number" min="10" max="600" step="1" />
          </label>
        </div>

        <p v-if="mAppliesTo.length" class="rt-note">
          使用范围：{{ mAppliesTo.map((k) => APPLIES_TO_LABEL[k] ?? k).join('、') }}。该地址/模型与 Wiki 自动整理共用，不允许选择外部提供商。
        </p>

        <div class="rt-actions">
          <BaseButton variant="secondary" size="sm" @click="testMaterial" :loading="mTesting">
            <template v-if="!mTesting">测试连通性</template>
            <template v-else>测试中…</template>
          </BaseButton>
          <BaseButton variant="secondary" size="sm" @click="testMaterialInference" :loading="mInferenceTesting" :disabled="mTesting">
            <template v-if="!mInferenceTesting">试运行模型</template>
            <template v-else>运行中…</template>
          </BaseButton>
          <BaseButton variant="primary" @click="saveMaterial" :loading="mSaving" :disabled="mTesting || mInferenceTesting">
            保存
          </BaseButton>
          <button type="button" class="rt-refresh" title="重新读取" aria-label="刷新材料处理配置" @click="refreshMaterial">
            <RefreshCw :size="14" aria-hidden="true" />
          </button>
        </div>

        <div v-if="mTest" class="rt-test" :class="mTest.ok ? 'is-ok' : 'is-bad'">
          <component :is="mTest.ok ? Check : X" :size="14" aria-hidden="true" />
          <span v-if="mTest.ok">服务可达 · 模型已安装：{{ mTest.model }} · {{ mTest.latencyMs }}ms</span>
          <span v-else>连接失败 · {{ errorLabel(mTest.errorCode) }} · {{ mTest.latencyMs }}ms</span>
        </div>
        <div v-if="mInferenceTest" class="rt-test" :class="mInferenceTest.ok ? 'is-ok' : 'is-bad'">
          <component :is="mInferenceTest.ok ? Check : X" :size="14" aria-hidden="true" />
          <span v-if="mInferenceTest.ok">模型试运行成功 · {{ mInferenceTest.model }} · {{ mInferenceTest.latencyMs }}ms</span>
          <span v-else>模型试运行失败 · {{ errorLabel(mInferenceTest.errorCode) }} · {{ mInferenceTest.latencyMs }}ms</span>
        </div>
      </section>

      <!-- ============ 运行监控与任务（P2 §7 / §8） ============ -->
      <section class="rt-section">
        <header class="rt-section__head">
          <div class="rt-section__title">
            <span class="rt-section__icon"><Database :size="16" aria-hidden="true" /></span>
            <h2>运行监控与任务</h2>
          </div>
          <div class="m2-head-side">
            <span class="m2-worker" :class="m2Monitor?.worker.running ? 'is-ok' : 'is-muted'">
              {{ m2Monitor?.worker.running ? '任务执行中' : '执行器未运行' }}
            </span>
            <span class="m2-worker" :class="m2RunningModels ? 'is-ok' : 'is-muted'">
              Ollama {{ m2RunningModels ? '已加载模型' : '无加载模型' }}
            </span>
            <button type="button" class="rt-refresh" title="刷新监控" aria-label="刷新运行监控" @click="m2RefreshAll">
              <RefreshCw :size="14" aria-hidden="true" />
            </button>
          </div>
        </header>

        <p v-if="m2MonitorError" class="rt-note"><TriangleAlert :size="13" aria-hidden="true" /> {{ m2MonitorError }}，可点击右上角重试。</p>

        <!-- 资源快照：CPU / 内存 / GPU / Ollama -->
        <div class="m2-grid" role="group" aria-label="系统资源监控">
          <div class="m2-cell">
            <div class="m2-cell__head"><Cpu :size="14" aria-hidden="true" /><span>CPU</span></div>
            <div class="m2-cell__value" :class="{ 'is-muted': !m2Monitor?.resource.cpu.available }">
              {{ cpuText(m2Monitor?.resource.cpu ?? { available: false }) }}
            </div>
          </div>
          <div class="m2-cell">
            <div class="m2-cell__head"><MemoryStick :size="14" aria-hidden="true" /><span>内存</span></div>
            <template v-if="m2Monitor?.resource.memory.available">
              <div class="m2-cell__value">{{ fmtPct(m2Monitor?.resource.memory.usedPercent) }}</div>
              <div class="m2-cell__sub">{{ memText(m2Monitor?.resource.memory) }}</div>
            </template>
            <div v-else class="m2-cell__value is-muted">不可用</div>
          </div>
          <div class="m2-cell">
            <div class="m2-cell__head"><CircuitBoard :size="14" aria-hidden="true" /><span>GPU</span></div>
            <template v-if="m2Monitor?.resource.gpu.available">
              <div class="m2-cell__value">
                {{ fmtPct(m2Monitor?.resource.gpu.utilizationPercent) }}
                <span v-if="m2Monitor?.resource.gpu.name" class="m2-cell__gpu">{{ m2Monitor?.resource.gpu.name }}</span>
              </div>
              <div class="m2-cell__sub">{{ gpuMemText(m2Monitor?.resource.gpu) }}</div>
            </template>
            <div v-else class="m2-cell__value is-muted">
              {{ m2Monitor?.resource.gpu.errorMessageSafe || '不可用' }}
            </div>
          </div>
          <div class="m2-cell">
            <div class="m2-cell__head"><HardDrive :size="14" aria-hidden="true" /><span>Ollama</span></div>
            <template v-if="m2Monitor?.resource.ollama.available">
              <div class="m2-cell__value">v{{ m2Monitor?.resource.ollama.version ?? '—' }}</div>
              <div class="m2-cell__sub">
                已安装 {{ m2Monitor?.resource.ollama.installedCount ?? 0 }} · 加载 {{ m2Monitor?.resource.ollama.runningCount ?? 0 }}
              </div>
            </template>
            <div v-else class="m2-cell__value is-muted">不可达</div>
          </div>
        </div>

        <!-- 索引队列 -->
        <p v-if="m2Monitor" class="rt-note">
          索引队列：活跃 {{ m2Monitor.indexQueue.active ?? '—' }} / 待处理 {{ m2Monitor.indexQueue.total ?? '—' }}。
        </p>
        <p v-if="m2MindosPipeline" class="rt-note">
          材料队列：等待 {{ m2MindosPipeline.materialQueue.queued ?? 0 }} / 处理中 {{ m2MindosPipeline.materialQueue.processing ?? 0 }} / 已暂停 {{ m2MindosPipeline.materialQueue.paused ?? 0 }}；
          卡片索引任务 {{ m2MindosPipeline.cardIndexQueue.total }}；Ollama {{ m2MindosPipeline.ollamaScheduler.running ? '运行中' : '未运行' }}。
        </p>

        <!-- 本地模型列表与拉取 -->
        <div class="m2-block">
          <div class="m2-block__head">
            <h3>本地模型</h3>
            <span v-if="!m2Models.reachable" class="m2-block__hint">
              {{ m2Models.errorCode ? `不可达（${m2Models.errorCode}）` : '未获取到模型列表' }}
            </span>
          </div>
          <div v-if="m2Models.reachable && !m2Models.models.length" class="rt-note">
            暂未安装模型。可在下方输入模型名（如 qwen3:1.7b）拉取。
          </div>
          <ul v-if="m2Models.models.length" class="m2-models">
            <li v-for="m in m2Models.models" :key="m.name" class="m2-model">
              <div class="m2-model__info">
                <span class="m2-model__name">{{ m.name }}</span>
                <span v-if="m.running" class="m2-model__run-badge" title="该模型已加载运行">运行中</span>
                <span v-if="m.sizeBytes != null" class="m2-model__meta">{{ fmtBytes(m.sizeBytes) }}</span>
                <span v-if="(m.parameterSize || m.quantization)" class="m2-model__meta">{{ [m.parameterSize, m.quantization].filter(Boolean).join(' · ') }}</span>
              </div>
              <div class="m2-model__actions">
                <BaseButton variant="secondary" size="sm" @click="m2RunAction('load', m.name)" :loading="m2BusyFor(`load:${m.name}`)" :disabled="!!m2Busy">加载</BaseButton>
                <BaseButton variant="secondary" size="sm" @click="m2RunAction('unload', m.name)" :loading="m2BusyFor(`unload:${m.name}`)" :disabled="!!m2Busy">卸载</BaseButton>
              </div>
            </li>
          </ul>
          <div class="m2-pull">
            <input v-model="m2PullModel" type="text" placeholder="输入模型名拉取，如 qwen3:1.7b" @keyup.enter="m2PullModel.trim() && m2RunAction('pull', m2PullModel.trim())" />
            <BaseButton variant="primary" size="sm" :loading="m2BusyFor(`pull:${m2PullModel}`)" :disabled="!!m2Busy || !m2PullModel.trim()" @click="m2RunAction('pull', m2PullModel.trim())">
              <template v-if="!m2BusyFor(`pull:${m2PullModel}`)"><Download :size="14" aria-hidden="true" /> 拉取</template>
              <template v-else>拉取中…</template>
            </BaseButton>
          </div>
        </div>

        <!-- 模型任务 -->
        <div class="m2-block">
          <div class="m2-block__head">
            <h3>模型任务</h3>
            <button type="button" class="m2-refresh-text" @click="m2LoadJobs">刷新</button>
          </div>
          <ul v-if="m2Jobs.items.length" class="m2-jobs">
            <li v-for="job in m2Jobs.items" :key="job.jobId" class="m2-job">
              <span class="m2-job__state" :class="JOB_STATE_CLASS[job.state] ?? 'is-muted'">{{ JOB_STATE_LABEL[job.state] ?? job.state }}</span>
              <div class="m2-job__info">
                <span class="m2-job__title">{{ MODEL_ACTION_LABEL[job.type] ?? job.type }} · {{ job.targetModel }}</span>
                <span v-if="job.errorMessageSafe || job.errorCode" class="m2-job__err">{{ job.errorMessageSafe || job.errorCode }}</span>
                <span v-if="(job.progressCurrent != null || job.progressTotal != null)" class="m2-job__prog">进度 {{ job.progressCurrent ?? 0 }}/{{ job.progressTotal ?? '—' }}</span>
              </div>
              <button v-if="job.state === 'queued' || job.state === 'running' || job.state === 'cancel_requested'" type="button" class="m2-job__cancel" title="取消任务" aria-label="取消任务" @click="m2Cancel(job)">
                <Square :size="13" aria-hidden="true" />
              </button>
            </li>
          </ul>
          <p v-else class="rt-note">暂无模型任务。</p>
        </div>

      </section>
      </details>
    </template>
  </div>

  <!-- 乐观锁冲突确认：不静默覆盖未保存草稿的受控图层 -->
  <Teleport to="body">
    <div v-if="conflictPrompt" class="rt-conflict-mask" role="presentation" @click.self="onConflictKeepEditing">
      <div class="rt-conflict" role="alertdialog" aria-modal="true" :aria-label="'配置已被更新'">
        <h3 class="rt-conflict__title">配置已被更新</h3>
        <p class="rt-conflict__body">
          {{ conflictFor === 'material' ? '材料处理' : '对话问答' }} 配置已被其他会话修改。加载最新配置将丢弃本页未保存的改动。
        </p>
        <div class="rt-conflict__actions">
          <BaseButton variant="secondary" @click="onConflictKeepEditing">保留我的编辑</BaseButton>
          <BaseButton variant="primary" @click="onConflictLoadLatest">加载最新配置</BaseButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.rt-adv {
  margin-top: 8px;
}
.rt-adv > summary {
  font-family: var(--ws-font-display, serif);
  font-size: var(--ws-display-3, 16px);
  font-weight: 600;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
}
.rt-adv > summary:hover {
  color: var(--ws-primary-color, #a6452e);
}
.rt-adv[open] > summary {
  margin-bottom: 8px;
}
.rt-more {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.rt-more > summary {
  cursor: pointer;
}
.rt-more[open] > summary {
  margin-bottom: 8px;
}
.rt-link {
  color: var(--ws-primary-color, #a6452e);
}

.rt-section {
  margin-bottom: 16px;
  padding: 16px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.rt-section__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.rt-section__title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rt-section__title h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.rt-section__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 6px;
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.09));
  color: var(--ws-primary-color, #a6452e);
}
.rt-section__icon.is-local {
  background: var(--ws-success-soft, rgba(18, 205, 61, 0.1));
  color: var(--ws-success, #4a7c59);
}
.rt-section__source {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
.rt-section__source.is-custom {
  background: var(--ws-accent-soft, rgba(0, 119, 255, 0.1));
  color: var(--ws-primary-color, #a6452e);
}
.rt-section__source.is-default {
  background: var(--ws-muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--ws-text-secondary-color, #686b66);
}

.rt-health {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
}
.rt-health__item {
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
}
.rt-health__item.is-ok {
  background: var(--ws-success-soft, rgba(18, 205, 61, 0.1));
  color: var(--ws-success, #4a7c59);
}
.rt-health__item.is-bad {
  background: var(--ws-danger-soft, rgba(255, 73, 24, 0.1));
  color: var(--ws-danger, #a6452e);
}
.rt-health__item.is-warn {
  background: var(--ws-warning-soft, rgba(230, 162, 60, 0.12));
  color: #e6a23c;
}
.rt-health__item.is-muted {
  background: var(--ws-muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--ws-text-secondary-color, #686b66);
}
.rt-health__version {
  margin-left: auto;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}

.rt-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.rt-form__row {
  display: flex;
  gap: 12px;
}
.rt-form__row .rt-field {
  flex: 1;
}
.rt-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--ws-text-primary-color, #1d211f);
}
.rt-field.is-narrow {
  max-width: 200px;
}
.rt-field.is-disabled {
  opacity: 0.6;
}
.rt-field input,
.rt-field select {
  padding: 8px 10px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius, 6px);
  font-family: inherit;
  font-size: 13px;
  color: var(--ws-text-primary-color, #1d211f);
  background: var(--ws-body-bg, #fff);
}
.rt-field input:disabled,
.rt-field select:disabled {
  background: var(--ws-surface-2, #fbf8f1);
  color: var(--ws-text-secondary-color, #686b66);
  cursor: not-allowed;
}
.rt-field.is-switch {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.rt-field__line {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rt-field.is-none-label {
  color: var(--ws-text-secondary-color, #686b66);
}
.rt-hint {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.rt-note.is-security-warning {
  color: var(--ws-warning-color, #e6a23c);
}
.rt-hint code {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--ws-surface-2, #fbf8f1);
  color: var(--ws-primary-color, #a6452e);
  font-family: inherit;
}
.rt-field.is-switch .rt-hint {
  flex-basis: 100%;
}

.rt-toggle {
  position: relative;
  flex-shrink: 0;
  width: 38px;
  height: 22px;
}
.rt-toggle input {
  position: absolute;
  inset: 0;
  opacity: 0;
  margin: 0;
  cursor: pointer;
}
.rt-toggle__track {
  position: absolute;
  inset: 0;
  border-radius: 999px;
  background: var(--ws-border-color-2, #e2ded4);
  transition: background 0.15s;
  pointer-events: none;
}
.rt-toggle__track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
  transition: transform 0.15s;
}
.rt-toggle input:checked + .rt-toggle__track {
  background: var(--ws-primary-color, #a6452e);
}
.rt-toggle input:checked + .rt-toggle__track::after {
  transform: translateX(16px);
}

.rt-note {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}

.rt-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
}
.rt-refresh {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  margin-left: auto;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: 6px;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
}
.rt-refresh:hover {
  color: var(--ws-primary-color, #a6452e);
  border-color: var(--ws-primary-color, #a6452e);
}

.rt-test {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
}
.rt-test.is-ok {
  background: var(--ws-success-soft, rgba(18, 205, 61, 0.1));
  color: var(--ws-success, #4a7c59);
}
.rt-test.is-bad {
  background: var(--ws-danger-soft, rgba(255, 73, 24, 0.1));
  color: var(--ws-danger, #a6452e);
}

/* ---- P2 运行监控 ----
   能力探测语义：available=false → 该能力不可用/未采样，均以灰色「不可用/采样中」呈现，
   不伪造 0 值。is-muted 覆盖各资源的降级态。 */
.m2-head-side {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
.m2-worker {
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.m2-worker.is-ok {
  background: var(--ws-success-soft, rgba(18, 205, 61, 0.1));
  color: var(--ws-success, #4a7c59);
}
.m2-worker.is-muted {
  background: var(--ws-muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--ws-text-secondary-color, #686b66);
}

.m2-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.m2-cell {
  padding: 10px 12px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius, 6px);
}
.m2-cell__head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.m2-cell__value {
  font-size: 18px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.m2-cell__value.is-muted {
  color: var(--ws-text-secondary-color, #686b66);
  font-weight: 500;
}
.m2-cell__gpu {
  margin-left: 6px;
  font-size: 12px;
  font-weight: 400;
  color: var(--ws-text-secondary-color, #686b66);
}
.m2-cell__sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}

.m2-block {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--ws-border-color-2, #e2ded4);
}
.m2-block__head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.m2-block__head h3 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.m2-block__hint {
  font-size: 12px;
  color: var(--ws-danger, #a6452e);
}
.m2-refresh-text {
  margin-left: auto;
  border: 0;
  background: transparent;
  color: var(--ws-primary-color, #a6452e);
  font-size: 12px;
  cursor: pointer;
}
.m2-refresh-text:hover {
  text-decoration: underline;
}

.m2-models {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.m2-model {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius, 6px);
}
.m2-model__info {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.m2-model__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.m2-model__meta {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.m2-model__actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.m2-model__run-badge {
  font-size: 12px;
  line-height: 1;
  padding: 3px 6px;
  border-radius: 999px;
  color: var(--ws-color-success, #2ba245);
  background: rgba(43, 162, 69, 0.12);
  border: 1px solid rgba(43, 162, 69, 0.3);
}
.m2-pull {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.m2-pull input {
  flex: 1;
  padding: 8px 10px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius, 6px);
  font-family: inherit;
  font-size: 13px;
  color: var(--ws-text-primary-color, #1d211f);
  background: var(--ws-body-bg, #fff);
}

.m2-jobs {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.m2-job {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius, 6px);
}
.m2-job__state {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
}
.m2-job__state.is-ok {
  background: var(--ws-success-soft, rgba(18, 205, 61, 0.1));
  color: var(--ws-success, #4a7c59);
}
.m2-job__state.is-bad {
  background: var(--ws-danger-soft, rgba(255, 73, 24, 0.1));
  color: var(--ws-danger, #a6452e);
}
.m2-job__state.is-muted,
.m2-job__state.is-cancelled {
  background: var(--ws-muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--ws-text-secondary-color, #686b66);
}
.m2-job__state.is-queued {
  background: var(--ws-accent-soft, rgba(0, 119, 255, 0.1));
  color: var(--ws-primary-color, #a6452e);
}
.m2-job__state.is-cancelling {
  background: var(--ws-warning-soft, rgba(230, 162, 60, 0.12));
  color: #e6a23c;
}
.m2-job__info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.m2-job__title {
  font-size: 13px;
  color: var(--ws-text-primary-color, #1d211f);
}
.m2-job__err {
  font-size: 12px;
  color: var(--ws-danger, #a6452e);
}
.m2-job__prog {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.m2-job__cancel {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  margin-left: auto;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: 6px;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
  flex-shrink: 0;
}
.m2-job__cancel:hover {
  color: var(--ws-danger, #a6452e);
  border-color: var(--ws-danger, #a6452e);
}


.rt-clear-key {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.rt-clear-key label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}
.rt-clear-key label.is-marked {
  color: var(--ws-danger, #a6452e);
  font-weight: 600;
}
.rt-clear-key input[type='checkbox'] {
  accent-color: var(--ws-danger, #a6452e);
  width: auto;
}

.rt-conflict-mask {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.35);
}
.rt-conflict {
  width: min(420px, calc(100vw - 32px));
  padding: 20px;
  border-radius: 10px;
  background: var(--ws-body-bg, #fff);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.2);
}
.rt-conflict__title {
  margin: 0 0 10px;
  font-size: 16px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.rt-conflict__body {
  margin: 0 0 18px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--ws-text-secondary-color, #3c403d);
}
.rt-conflict__actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
