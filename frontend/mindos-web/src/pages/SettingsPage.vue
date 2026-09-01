<script setup lang="ts">
// P1（§4）：设置页「模型与运行时」。
// 复用双通道划分：材料处理固定本地 Ollama；对话问答可显式配置并授权的外部 OpenAI 兼容 API。
// 契约：/api/system/models/*（require_local + revision 乐观锁）；test 提交表单暂存值不持久化。
import { computed, onMounted, onUnmounted, ref } from 'vue'
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
  type ChatProviderConfig,
  type ChatProviderTestResult,
  type ListModelJobsResponse,
  type MaterialModelsResponse,
  type MaterialRuntimeConfig,
  type MindosPipelineMonitor,
  type ModelJob,
  type ModelJobType,
  type MonitorResponse,
  type RuntimeTestResult,
} from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { useToast } from '@/composables/useToast'

const toast = useToast()

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
const cApiKey = ref('') // 留空 = 保持原密钥
const cApiKeyConfigured = ref(false)
const cApiKeyHint = ref<string | null>(null)
// 清除已配置密钥的受控意图：勾选后保存时发送 clearApiKey=true，撤销后端保存的密钥。
const cClearApiKey = ref(false)
const cFallback = ref(true)
const cTimeout = ref<number>(60)
const cBudget = ref<number>(90)
const cEffective = ref<'ollama' | 'openai'>('ollama')
const cTesting = ref(false)
const cSaving = ref(false)
const cTest = ref<ChatProviderTestResult | null>(null)

// 外部通道开启且 provider=openai 时，外部字段才可编辑
const cExternalEditable = computed(() => cProvider.value === 'openai' && cExternal.value)

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

function applyChat(cfg: ChatProviderConfig) {
  cRevision.value = cfg.revision
  cSource.value = cfg.source
  cProvider.value = cfg.provider
  cExternal.value = cfg.externalEnabled
  cBaseUrl.value = cfg.baseUrl ?? ''
  cModel.value = cfg.model ?? ''
  cApiKey.value = ''
  cApiKeyConfigured.value = cfg.apiKeyConfigured
  cApiKeyHint.value = cfg.apiKeyHint
  cClearApiKey.value = false // 载入最新配置后复位“清除”意图
  cFallback.value = cfg.fallbackOllama
  cTimeout.value = cfg.timeoutSeconds
  cBudget.value = cfg.totalBudgetSeconds
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
  if (cExternal.value || cSaving.value) return
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
  if (cProvider.value === 'ollama') {
    // 归一化：provider=ollama → 强制 externalEnabled=false，不外发外部字段
    cSaving.value = true
    try {
      // ollama 分支：PUT 响应仅含已保存字段，重拉完整 GET（含 source/effectiveProvider 等）。
      await api.putChatProvider({
        provider: 'ollama',
        externalEnabled: false,
        timeoutSeconds: cTimeout.value,
        totalBudgetSeconds: cBudget.value,
        fallbackOllama: cFallback.value,
        revision: cRevision.value,
      })
      applyChat(await api.getChatProvider())
      toast({ type: 'success', message: '对话问答配置已保存（本地 Ollama）' })
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
    return
  }

  // provider=openai
  if (cExternal.value && (!cBaseUrl.value.trim() || !cModel.value.trim())) {
    toast({ type: 'error', message: '开启外部问答需填写 API Base URL 与模型名' })
    return
  }
  // 已标记清除时无需密钥输入；否则外发必须提供密钥（新输入或既有已配置）
  if (cExternal.value && !cClearApiKey.value && !cApiKey.value.trim() && !cApiKeyConfigured.value) {
    toast({ type: 'error', message: '开启外部问答需提供 API Key' })
    return
  }
  cSaving.value = true
  try {
    // PUT 响应缺 apiKeyHint/effectiveProvider 等只读展示项；保存后重拉完整 GET 状态。
    await api.putChatProvider({
      provider: 'openai',
      externalEnabled: cExternal.value,
      baseUrl: cBaseUrl.value.trim() || null,
      model: cModel.value.trim() || null,
      timeoutSeconds: cTimeout.value,
      totalBudgetSeconds: cBudget.value,
      fallbackOllama: cFallback.value,
      // 清除密钥意图：不发 apiKey，发送 clearApiKey=true 撤销已保存密钥。
      apiKey: cClearApiKey.value ? undefined : (cApiKey.value.trim() || undefined),
      clearApiKey: cClearApiKey.value ? true : undefined,
      revision: cRevision.value,
    })
    applyChat(await api.getChatProvider())
    toast({ type: 'success', message: cExternal.value ? '对话问答配置已保存（外部 API）' : '对话问答配置已保存（本地 Ollama）' })
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
      cProvider.value === 'ollama'
        ? { provider: 'ollama', externalEnabled: false }
        : {
            provider: 'openai',
            externalEnabled: cExternal.value,
            baseUrl: cBaseUrl.value.trim() || undefined,
            model: cModel.value.trim() || undefined,
            apiKey: cApiKey.value.trim() || undefined,
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
      <h1>设置</h1>
      <p>模型与运行时。修改保存后仅新请求生效；历史派生/问答使用各自任务开始时的配置快照。</p>
    </div>

    <ErrorState v-if="loadError" :message="loadError" retry-label="重试" @retry="loadAll" />
    <div v-else-if="loading" class="loading-state">正在加载模型与运行时配置…</div>

    <template v-else>
      <!-- ============ 材料处理（本地 Ollama） ============ -->
      <section class="rt-section">
        <header class="rt-section__head">
          <div class="rt-section__title">
            <span class="rt-section__icon is-local"><Server :size="16" aria-hidden="true" /></span>
            <h2>材料处理（本地 Ollama）</h2>
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

      <!-- ============ 对话问答（外部 LLM） ============ -->
      <section class="rt-section">
        <header class="rt-section__head">
          <div class="rt-section__title">
            <span class="rt-section__icon"><MessageSquare :size="16" aria-hidden="true" /></span>
            <h2>对话问答（外部 LLM）</h2>
          </div>
          <span class="rt-section__source" :class="cSource === 'defaults' ? 'is-default' : 'is-custom'">
            {{ cSource === 'defaults' ? '部署默认值' : '运行时设置' }}
          </span>
        </header>

        <div class="rt-form">
          <label class="rt-field is-switch">
            <span class="rt-field__line">
              <span>使用外部问答</span>
              <span class="rt-toggle">
                <input v-model="cExternal" type="checkbox" role="switch" :disabled="cSaving" @change="disableExternalChatImmediately" />
                <span class="rt-toggle__track" aria-hidden="true" />
              </span>
            </span>
            <span v-if="cExternal" class="rt-hint">
              开启后，问题和检索证据将发送至所配置的外部 API；本期不对材料或知识卡片按授权过滤。
            </span>
          </label>

          <label class="rt-field">
            提供商
            <select v-model="cProvider">
              <option value="ollama">ollama（本地）</option>
              <option value="openai">openai（兼容协议）</option>
            </select>
          </label>

          <template v-if="cProvider === 'openai'">
            <label class="rt-field" :class="{ 'is-disabled': !cExternalEditable }">
              API Base URL
              <input v-model="cBaseUrl" type="url" placeholder="https://api.example.com/v1" :disabled="!cExternalEditable" />
            </label>
            <label class="rt-field" :class="{ 'is-disabled': !cExternalEditable }">
              外部模型名
              <input v-model="cModel" type="text" placeholder="deepseek-chat" :disabled="!cExternalEditable" />
            </label>
            <label class="rt-field" :class="{ 'is-disabled': !cExternalEditable && !(cApiKeyConfigured && !cExternal) }">
              API Key
              <input v-model="cApiKey" type="password" placeholder="留空表示保持原密钥" :disabled="!cExternalEditable" autocomplete="off" />
              <span class="rt-hint">
                {{ cApiKeyConfigured ? `已配置${cApiKeyHint ? `（${cApiKeyHint}）` : ''}` : '未配置'
                }}；密钥仅下发脱敏提示，不会回显。
              </span>
              <span v-if="cProvider === 'openai' && !cExternal && cApiKeyConfigured" class="rt-clear-key">
                <label :class="{ 'is-marked': cClearApiKey }">
                  <input v-model="cClearApiKey" type="checkbox" />
                  <span>清除已配置密钥{{ cClearApiKey ? '（保存后生效）' : '（请保持外部问答关闭）' }}</span>
                </label>
              </span>
            </label>
          </template>
          <div class="rt-field is-none-label">
            <span class="rt-hint">外部关闭时，问答固定使用材料处理模型 <code>{{ chatLocalModel }}</code>。</span>
          </div>

          <label class="rt-field is-switch">
            <span class="rt-field__line">
              <span>外部失败时回退本地 Ollama</span>
              <span class="rt-toggle">
                <input v-model="cFallback" type="checkbox" role="switch" />
                <span class="rt-toggle__track" aria-hidden="true" />
              </span>
            </span>
          </label>

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
        </div>

        <div class="rt-actions">
          <BaseButton variant="secondary" size="sm" @click="testChat" :loading="cTesting">
            <template v-if="!cTesting">测试连通性</template>
            <template v-else>测试中…</template>
          </BaseButton>
          <BaseButton variant="primary" @click="saveChat" :loading="cSaving" :disabled="cTesting">
            保存
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
.rt-section {
  margin-bottom: 16px;
  padding: 16px;
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
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
  color: var(--ws-text-primary-color, #303133);
}
.rt-section__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 6px;
  background: var(--ws-edit-color, rgba(0, 119, 255, 0.08));
  color: var(--ws-primary-color, #0077ff);
}
.rt-section__icon.is-local {
  background: var(--ws-success-soft, rgba(18, 205, 61, 0.1));
  color: var(--ws-success, #12cd3d);
}
.rt-section__source {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
}
.rt-section__source.is-custom {
  background: var(--ws-accent-soft, rgba(0, 119, 255, 0.1));
  color: var(--ws-primary-color, #0077ff);
}
.rt-section__source.is-default {
  background: var(--ws-muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--ws-text-secondary-color, #909399);
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
  color: var(--ws-success, #12cd3d);
}
.rt-health__item.is-bad {
  background: var(--ws-danger-soft, rgba(255, 73, 24, 0.1));
  color: var(--ws-danger, #ff4918);
}
.rt-health__item.is-warn {
  background: var(--ws-warning-soft, rgba(230, 162, 60, 0.12));
  color: #e6a23c;
}
.rt-health__item.is-muted {
  background: var(--ws-muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--ws-text-secondary-color, #909399);
}
.rt-health__version {
  margin-left: auto;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #909399);
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
  color: var(--ws-text-primary-color, #303133);
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
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
  border-radius: var(--ws-radius, 6px);
  font-family: inherit;
  font-size: 13px;
  color: var(--ws-text-primary-color, #303133);
  background: var(--ws-body-bg, #fff);
}
.rt-field input:disabled,
.rt-field select:disabled {
  background: var(--ws-card-bg, #f5f7fa);
  color: var(--ws-text-secondary-color, #909399);
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
  color: var(--ws-text-secondary-color, #909399);
}
.rt-hint {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #909399);
}
.rt-note.is-security-warning {
  color: var(--ws-warning-color, #e6a23c);
}
.rt-hint code {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--ws-card-bg, #f5f7fa);
  color: var(--ws-primary-color, #0077ff);
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
  background: var(--ws-border-color-2, #e4e7ed);
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
  background: var(--ws-primary-color, #0077ff);
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
  color: var(--ws-text-secondary-color, #909399);
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
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
  border-radius: 6px;
  background: transparent;
  color: var(--ws-text-secondary-color, #909399);
  cursor: pointer;
}
.rt-refresh:hover {
  color: var(--ws-primary-color, #0077ff);
  border-color: var(--ws-primary-color, #0077ff);
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
  color: var(--ws-success, #12cd3d);
}
.rt-test.is-bad {
  background: var(--ws-danger-soft, rgba(255, 73, 24, 0.1));
  color: var(--ws-danger, #ff4918);
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
  color: var(--ws-success, #12cd3d);
}
.m2-worker.is-muted {
  background: var(--ws-muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--ws-text-secondary-color, #909399);
}

.m2-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.m2-cell {
  padding: 10px 12px;
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
  border-radius: var(--ws-radius, 6px);
}
.m2-cell__head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #909399);
}
.m2-cell__value {
  font-size: 18px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #303133);
}
.m2-cell__value.is-muted {
  color: var(--ws-text-secondary-color, #909399);
  font-weight: 500;
}
.m2-cell__gpu {
  margin-left: 6px;
  font-size: 12px;
  font-weight: 400;
  color: var(--ws-text-secondary-color, #909399);
}
.m2-cell__sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #909399);
}

.m2-block {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--ws-border-color-2, #e4e7ed);
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
  color: var(--ws-text-primary-color, #303133);
}
.m2-block__hint {
  font-size: 12px;
  color: var(--ws-danger, #ff4918);
}
.m2-refresh-text {
  margin-left: auto;
  border: 0;
  background: transparent;
  color: var(--ws-primary-color, #0077ff);
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
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
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
  color: var(--ws-text-primary-color, #303133);
}
.m2-model__meta {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #909399);
}
.m2-model__actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.m2-model__run-badge {
  font-size: 11px;
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
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
  border-radius: var(--ws-radius, 6px);
  font-family: inherit;
  font-size: 13px;
  color: var(--ws-text-primary-color, #303133);
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
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
  border-radius: var(--ws-radius, 6px);
}
.m2-job__state {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
}
.m2-job__state.is-ok {
  background: var(--ws-success-soft, rgba(18, 205, 61, 0.1));
  color: var(--ws-success, #12cd3d);
}
.m2-job__state.is-bad {
  background: var(--ws-danger-soft, rgba(255, 73, 24, 0.1));
  color: var(--ws-danger, #ff4918);
}
.m2-job__state.is-muted,
.m2-job__state.is-cancelled {
  background: var(--ws-muted-soft, rgba(144, 147, 153, 0.12));
  color: var(--ws-text-secondary-color, #909399);
}
.m2-job__state.is-queued {
  background: var(--ws-accent-soft, rgba(0, 119, 255, 0.1));
  color: var(--ws-primary-color, #0077ff);
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
  color: var(--ws-text-primary-color, #303133);
}
.m2-job__err {
  font-size: 12px;
  color: var(--ws-danger, #ff4918);
}
.m2-job__prog {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #909399);
}
.m2-job__cancel {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  margin-left: auto;
  border: 1px solid var(--ws-border-color-2, #e4e7ed);
  border-radius: 6px;
  background: transparent;
  color: var(--ws-text-secondary-color, #909399);
  cursor: pointer;
  flex-shrink: 0;
}
.m2-job__cancel:hover {
  color: var(--ws-danger, #ff4918);
  border-color: var(--ws-danger, #ff4918);
}


.rt-clear-key {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #909399);
}
.rt-clear-key label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}
.rt-clear-key label.is-marked {
  color: var(--ws-danger, #ff4918);
  font-weight: 600;
}
.rt-clear-key input[type='checkbox'] {
  accent-color: var(--ws-danger, #ff4918);
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
  color: var(--ws-text-primary-color, #303133);
}
.rt-conflict__body {
  margin: 0 0 18px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--ws-text-secondary-color, #606266);
}
.rt-conflict__actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
