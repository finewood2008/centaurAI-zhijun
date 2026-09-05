<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { ChevronDown, ShieldCheck } from 'lucide-vue-next'
import SideDrawer from '@/components/ui/SideDrawer.vue'
import { askRoute, routePath, routingRequest, type RoutePreview } from '@/services/taskRouting'
const props = defineProps<{ conversationId?: string; disabled?: boolean }>()
const emit = defineEmits<{ (e: 'mode', value: string): void }>()
const state = ref<any>(null)
const error = ref('')
const notice = ref('')
const acknowledge = ref(false)
const open = ref(false)
const busy = ref(false)
const configureDefault = ref(false)
const consentAcknowledge = ref(false)
const includeFiles = ref(false)
const includeCharter = ref(false)
const configureHandling = ref(false)
const handlingAction = ref<'omit' | 'local'>('omit')
const path = computed(() => props.conversationId ? routePath(props.conversationId) : '/mindos/conversations/routing/default')
const actionPath = computed(() => routePath(props.conversationId || 'default'))
const policy = computed(() => state.value?.defaultAuthorization)
const handling = computed(() => state.value?.handlingPreference)
const taskLabels: Record<string, string> = { alignment: '自我校准', extract_turn: '个人理解', draft_turn: '判断草稿', home_brief: '今日来信', summarize_conversation: '会话摘要', first_observation: '初步理解', consolidate: '理解整理', learning: '情境复盘', decision_suggestions: '判断候选', reply_assistance: '回复辅助' }
const taskLabel = (key: string) => taskLabels[key] || (key.startsWith('file_reply:') ? '文件反馈' : '后台整理')
const taskCount = (task: any) => Number.isInteger(task.count) && task.count > 0 ? task.count : null
const pausedMemory = computed(() => state.value?.pending?.find((task: any) => task.task_key === 'extract_turn'))
const failedCount = computed(() => (state.value?.pending ?? []).reduce((total: number, task: any) => total + (task.failedCount || 0), 0))
const attentionLabel = computed(() => {
  if (error.value || state.value?.error || policy.value?.serviceChanged || handling.value?.serviceChanged) return '检查设置'
  if (failedCount.value) return `整理未完成 · ${failedCount.value}`
  if (pausedMemory.value) return `个人理解暂停${taskCount(pausedMemory.value) ? ` · ${taskCount(pausedMemory.value)} 轮` : ''}`
  return `待处理 ${state.value?.pending?.length ?? 0}`
})
let sequence = 0
let mutation = 0
let alive = true
let pendingController: AbortController | null = null
function begin() {
  pendingController?.abort(); pendingController = null
  const ticket = ++mutation, target = path.value
  busy.value = true; sequence++; error.value = ''; notice.value = ''
  return () => alive && ticket === mutation && target === path.value
}
async function refresh() {
  if (busy.value) return
  const target = path.value, ticket = ++sequence
  try {
    const next = await routingRequest(target)
    if (target !== path.value || ticket !== sequence) return
    state.value = next; error.value = ''; emit('mode', next.mode.mode)
  } catch (e) { if (ticket === sequence) error.value = e instanceof Error ? e.message : '设置读取失败' }
}
watch(path, () => {
  pendingController?.abort(); pendingController = null
  mutation++; sequence++; busy.value = false
  open.value = false; state.value = null; error.value = ''; notice.value = ''
  acknowledge.value = false; configureDefault.value = false; consentAcknowledge.value = false
  configureHandling.value = false
  includeFiles.value = false; includeCharter.value = false
  void refresh()
}, { immediate: true })
const timer = setInterval(() => { if (!props.disabled) void refresh() }, 10000)
onBeforeUnmount(() => { alive = false; mutation++; sequence++; pendingController?.abort(); clearInterval(timer) })
function show() { open.value = true; void refresh() }
async function change(mode: string) {
  const valid = begin(), target = path.value
  try {
    const result = await routingRequest(target, 'PUT', { mode, acknowledge: acknowledge.value,
      serviceId: state.value.service?.id || '', expectedRevision: state.value.mode.revision,
      freshContext: mode === 'online' && state.value.mode.mode !== 'online' })
    if (!valid()) return
    state.value = result
    emit('mode', state.value.mode.mode); acknowledge.value = false
    notice.value = mode === 'online' ? '在线理解已启用；资料授权按下方设置执行。' : '已切换为本地处理。'
  } catch (e) { if (valid()) error.value = e instanceof Error ? e.message : '切换未完成' }
  finally { if (valid()) busy.value = false }
}
async function saveDefault(enabled: boolean) {
  const valid = begin(), target = actionPath.value
  try {
    const result = await routingRequest(target + '/default-consent', 'PUT', {
      enabled, includeFiles: includeFiles.value, includeCharter: includeCharter.value, acknowledge: consentAcknowledge.value,
      serviceId: state.value.service?.id || '', expectedRevision: policy.value?.revision || 0,
    })
    if (!valid()) return
    state.value = result
    configureDefault.value = false; consentAcknowledge.value = false
    notice.value = enabled ? '默认授权已开启，适用范围内不再逐次询问。不会自动恢复已暂停的任务。' : '默认授权已关闭。之前逐次批准的授权仍有效，可在下方一并撤销。'
  } catch (e) { if (valid()) error.value = e instanceof Error ? e.message : '默认授权未保存' }
  finally { if (valid()) busy.value = false }
}
function toggleDefault() {
  if (policy.value?.active) void saveDefault(false)
  else { includeFiles.value = false; includeCharter.value = false; consentAcknowledge.value = false; configureDefault.value = true }
}
function editDefault() { includeFiles.value = !!policy.value?.includeFiles; includeCharter.value = !!policy.value?.includeCharter; consentAcknowledge.value = false; configureDefault.value = true }
async function saveHandling(enabled: boolean) {
  const valid = begin(), target = actionPath.value
  try {
    const result = await routingRequest(target + '/handling', 'PUT', {
      enabled, action: enabled ? handlingAction.value : handling.value.action,
      serviceId: state.value.service?.id || '', expectedRevision: handling.value?.revision || 0,
    })
    if (!valid()) return
    state.value = result
    configureHandling.value = false
    notice.value = enabled ? '已记住处理方式，刷新或重启后仍有效。没有增加任何资料授权。' : '已关闭固定处理方式；需要新的资料授权时再询问。'
  } catch (e) { if (valid()) error.value = e instanceof Error ? e.message : '处理方式未保存' }
  finally { if (valid()) busy.value = false }
}
function editHandling() { handlingAction.value = handling.value?.action || 'omit'; configureHandling.value = true }
function toggleHandling() { if (handling.value?.active) void saveHandling(false); else editHandling() }
async function revoke() {
  const valid = begin(), target = actionPath.value
  try {
    await routingRequest(target + '/revoke', 'POST', {})
    if (!valid()) return
    notice.value = '默认授权及本设备已批准的资料用途授权已撤销；已发送内容无法收回。'
  } catch (e) { if (valid()) error.value = e instanceof Error ? e.message : '撤销未完成' }
  finally { if (valid()) busy.value = false }
  if (valid()) await refresh()
}
async function pending(task: any, reprepare = false) {
  const valid = begin(), target = actionPath.value
  const abort = new AbortController(); pendingController = abort
  try {
    let localOnly = false
    if (!reprepare) {
      const preview = await routingRequest<RoutePreview>(target + '/pending/' + task.preview_id, 'GET', undefined, abort.signal)
      if (!valid()) return
      const choice = preview.missing.length ? await askRoute(preview, false, abort.signal) : { action: 'allow' as const, keys: [] }
      if (!valid() || choice.action === 'cancel') return
      if (choice.action === 'allow' && choice.keys?.length) await routingRequest(target + '/grant', 'POST', { revision: preview.revision, keys: choice.keys }, abort.signal)
      if (!valid()) return
      localOnly = choice.action === 'local'
    }
    const result = await routingRequest<{ queuedCount?: number; pendingCount?: number }>(target + '/resume', 'POST', { task: task.task_key, localOnly }, abort.signal)
    if (!valid()) return
    notice.value = reprepare ? '正在重新准备待办，没有增加授权；仍需授权的内容会在这里提示。'
      : result.queuedCount === 0 ? '这些任务已经排队，无需重复恢复。'
      : `${result.queuedCount ? `已恢复 ${result.queuedCount} 项待办` : '任务已排队'}，处理完成后会更新到对话里。${result.pendingCount ? `还有 ${result.pendingCount} 项需要核对。` : ''}`
  } catch (e) { if (valid()) error.value = e instanceof Error ? e.message : '任务未恢复' }
  finally { if (valid()) { busy.value = false; pendingController = null } }
  if (valid()) await refresh()
}
defineExpose({ refresh })
</script>
<template>
  <section class="routing-panel" aria-label="对话处理方式">
    <button class="routing-trigger" :disabled="disabled || busy" :aria-expanded="open" aria-haspopup="dialog" @click="show">
      <span class="routing-dot" :class="{ online: state?.mode.mode === 'online' }" aria-hidden="true" />
      <span>{{ state?.mode.mode === 'online' ? '在线理解' : '本地处理' }}</span>
      <span v-if="policy?.active" class="routing-default">默认授权</span><ChevronDown :size="13" aria-hidden="true" />
    </button>
    <button v-if="state?.pending?.length || error || state?.error || policy?.serviceChanged || handling?.serviceChanged" class="routing-attention" :title="pausedMemory ? '聊天仍可继续，但这些轮次尚未完成个人理解整理；点击查看原因和恢复' : undefined" @click="show">{{ attentionLabel }}</button>
    <SideDrawer :open="open" title="模型与授权" @close="open = false">
      <div class="routing-settings">
        <section v-if="state" class="routing-group">
          <h3>{{ conversationId ? '本段对话的处理方式' : '新对话默认方式' }}</h3>
          <p class="routing-service">{{ state.service?.name || '服务待配置' }} <span>· {{ state.service?.model }}</span></p>
          <p>在线负责对话与理解，本地负责资料解析、检索和权限。在线失败时由你选择重试或改用本地，不自动降级。</p>
          <p v-if="state.mode.cutoff && state.mode.mode === 'online'">不携带受保护旧历史的在线上下文；原记录仍保留。</p>
          <template v-if="state.mode.mode !== 'online' || state.service?.id !== state.mode.service">
            <label><input v-model="acknowledge" type="checkbox" /> 我理解日常消息会发送给上述服务，离开本机后无法收回。开启在线不携带受保护旧历史。</label>
            <button class="routing-primary" :disabled="!acknowledge || !state.service?.external || busy || disabled" @click="change('online')">启用在线理解</button>
          </template>
          <button v-else :disabled="busy || disabled" @click="change('local')">{{ conversationId ? '整段仅本地' : '新对话默认本地' }}</button>
        </section>
        <section v-if="state" class="routing-group">
          <div class="routing-setting-title"><h3><ShieldCheck :size="17" aria-hidden="true" /> 默认授权相关文字</h3><button class="routing-switch" role="switch" aria-label="默认授权相关文字" :aria-checked="!!policy?.active" :disabled="busy || !state.service?.external" @click="toggleDefault"><span /></button></div>
          <p>开启后，本设备各在线对话及后台理解任务，自动使用所需的对话、个人理解、判断和复盘文字。包括今后新增或修改的相关内容；只发送实际需要的部分。</p>
          <p v-if="policy?.active">已开启 · {{ policy.serviceName }} · {{ policy.includeFiles ? '包括引用的文件提取文字' : '文件文字仍单独询问' }} · {{ policy.includeCharter ? '包括人生章程与草稿' : '章程与草稿仍单独询问' }} <button class="routing-link" @click="editDefault">修改范围</button></p>
          <p v-if="policy?.serviceChanged" class="routing-warning">服务已变化。之前对 {{ policy.serviceName }} 的默认授权不适用于当前服务，请重新确认。</p>
          <div v-if="configureDefault" class="routing-consent-form">
            <p><strong>授权给 {{ state.service?.name }}</strong></p>
            <p>用途：日常对话、回复辅助、判断草稿与候选、个人理解与校准、情境推演及复盘、摘要、今日来信和理解整理。此开关不授权上传原文件、通用导出或训练个人模型；外部服务的数据保留规则以该服务说明为准。</p>
            <label><input v-model="includeFiles" type="checkbox" /> 也默认允许引用的文件提取文字及其派生内容（包括今后新增或更新的文件）</label>
            <label><input v-model="includeCharter" type="checkbox" /> 也默认允许人生章程与章程草稿（含必要的历史版本），用于上述对话和理解任务</label>
            <p class="routing-fine">章程默认不包含在旧授权里，需你明确选择。章程引用的文件仍按文件权限核对，不能绕过撤销、删除或失效的来源。</p>
            <label><input v-model="consentAcknowledge" type="checkbox" /> 我同意把上述范围的必要文字发给此服务，不再逐次询问；已发送的内容无法收回。</label>
            <div class="routing-actions"><button class="routing-primary" :disabled="!consentAcknowledge || busy" @click="saveDefault(true)">确认开启默认授权</button><button :disabled="busy" @click="configureDefault = false">暂不开启</button></div>
          </div>
          <p class="routing-fine">仅本地对话不受影响。换服务需重新确认，来源不明或已删除的内容仍被拦截。关闭开关即停止默认授权；逐次批准的权限可另行撤销。</p>
        </section>
        <section v-if="state" class="routing-group">
          <div class="routing-setting-title"><h3>记住资料受限时的处理方式</h3><button class="routing-switch" role="switch" aria-label="记住资料受限时的处理方式" :aria-checked="!!handling?.active" :disabled="busy || !state.service?.external" @click="toggleHandling"><span /></button></div>
          <p>用于本设备各在线对话。遇到未授权或暂不可用的参考资料时，按你选定的方式继续，减少重复询问；不会开启新的授权。</p>
          <p v-if="handling?.active">已开启 · {{ handling.action === 'omit' ? '跳过受限资料，继续在线对话' : '本轮改用本地模型' }} <button class="routing-link" @click="editHandling">修改方式</button></p>
          <p v-if="handling?.serviceChanged" class="routing-warning">在线服务已变化，之前的处理偏好暂不适用；请核对后重新保存。</p>
          <div v-if="configureHandling" class="routing-consent-form">
            <label><input v-model="handlingAction" type="radio" value="omit" name="restricted-handling" /> 跳过受限资料，继续在线对话（保留可用且已授权的资料）</label>
            <label><input v-model="handlingAction" type="radio" value="local" name="restricted-handling" /> 本轮改用本地模型（能力可能较弱；不可用时提示，不自动改用在线）</label>
            <p class="routing-fine">开启后持续生效，直到你关闭或修改。正常、已授权的对话仍使用在线模型。</p>
            <div class="routing-actions"><button class="routing-primary" :disabled="busy" @click="saveHandling(true)">保存并开启</button><button :disabled="busy" @click="configureHandling = false">取消</button></div>
          </div>
          <p class="routing-fine">来源失效的旧记录默认暂停引用，仅轻提示。你明确选用的附件、回访记录或辅助表达无法安全处理时，仍需决定。后台任务缺少权限继续暂停，不自行扩大授权。</p>
        </section>
        <section v-if="state?.pending?.length" class="routing-group">
          <h3>尚未完成的整理 · {{ state.pending.length }} 类</h3><p>聊天仍可继续。这里区分处理失败与等待授权；恢复时重新核验来源，不会把普通回复当成已经记入本体。</p>
          <div v-for="task in state.pending" :key="task.task_key" class="routing-task"><div><strong>{{ taskLabel(task.task_key) }}</strong><span v-if="taskCount(task)"> · {{ taskCount(task) }} {{ task.task_key === 'extract_turn' ? '轮待整理' : '项待处理' }}</span><p v-if="task.detail" class="routing-fine">{{ task.detail }}</p><p v-if="task.previewExpired && !task.failedCount" class="routing-fine">原预览已过期，先重新准备待办，再核对需要的授权。</p></div><button :disabled="busy || disabled" @click="pending(task, !!task.previewExpired || !!task.failedCount)">{{ task.failedCount ? '重新整理' : task.previewExpired ? '重新准备待办' : '核对并继续' }}</button></div>
        </section>
        <details v-if="state" class="routing-group"><summary>撤销已批准的授权</summary><p>停止本设备后续使用资料，关闭默认授权。日常在线消息仍按对话处理方式发送；已经发送的内容无法收回。</p><button :disabled="busy" @click="revoke">撤销本设备资料用途授权</button></details>
        <p v-if="notice" class="routing-notice" role="status">{{ notice }}</p><p v-if="error || state?.error" class="routing-warning" role="alert">{{ error || state.error }}</p>
      </div>
    </SideDrawer>
  </section>
</template>
<style scoped>
.routing-panel { display:flex; flex-wrap:wrap; align-items:center; gap:8px; min-width:0; font-size:12px; color:var(--ws-text-secondary-color,#686b66); }
.routing-trigger,.routing-attention { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; font:inherit; background:transparent; color:inherit; border:1px solid var(--ws-border-color-3,#ebe7de); padding:6px 9px; border-radius:20px; cursor:pointer; }
.routing-attention { color:var(--ws-primary-color,#a6452e); border-color:transparent; }.routing-dot { width:6px; height:6px; border-radius:50%; background:#9b978e; }.routing-dot.online { background:#4a7c59; }.routing-default { border-left:1px solid #8883; padding-left:6px; }
.routing-settings { font-size:14px; line-height:1.75; }.routing-group { padding-bottom:24px; margin-bottom:24px; border-bottom:1px solid var(--ws-border-color-3,#ebe7de); }.routing-group h3 { display:flex; align-items:center; gap:8px; font-size:16px; color:var(--ws-text-primary-color,#1d211f); margin:0 0 10px; }.routing-group p { margin:10px 0; }.routing-service { color:var(--ws-text-primary-color,#1d211f); overflow-wrap:anywhere; }.routing-service span { color:var(--ws-text-secondary-color,#686b66); }.routing-group label { display:flex; align-items:flex-start; gap:9px; margin:14px 0; }.routing-group input { margin-top:6px; flex-shrink:0; accent-color:var(--ws-primary-color,#a6452e); }
.routing-settings button { font:inherit; border:1px solid var(--ws-border-color,#d8d3c8); border-radius:8px; background:transparent; color:inherit; padding:7px 12px; cursor:pointer; }.routing-settings button.routing-primary { color:#fff; background:var(--ws-primary-color,#a6452e); border-color:transparent; }.routing-settings button.routing-link { border:0; text-decoration:underline; padding:0 5px; color:var(--ws-primary-color,#a6452e); }button:disabled { opacity:.45; cursor:not-allowed; }
.routing-setting-title { display:flex; align-items:center; justify-content:space-between; gap:12px; }.routing-setting-title h3 { margin:0; }.routing-settings .routing-switch { width:40px; height:24px; padding:2px; border:0; border-radius:14px; background:#c8c3b9; flex-shrink:0; }.routing-switch span { display:block; width:20px; height:20px; background:#fff; border-radius:50%; }.routing-switch[aria-checked=true] { background:var(--ws-primary-color,#a6452e); }.routing-switch[aria-checked=true] span { transform:translateX(16px); }
.routing-consent-form { padding:14px; border:1px solid var(--ws-border-color,#d8d3c8); border-radius:10px; background:var(--ws-surface-2,#fbf8f1); }.routing-fine { font-size:12px; color:var(--ws-text-secondary-color,#686b66); }.routing-actions { display:flex; flex-wrap:wrap; gap:8px; }.routing-task { display:flex; justify-content:space-between; align-items:center; gap:12px; margin:10px 0; }.routing-warning { color:var(--ws-primary-color,#a6452e); }.routing-notice { padding:12px; background:#4a7c5910; border-radius:8px; }.routing-group summary { cursor:pointer; }
@media(max-width:600px) { .routing-default { display:none; }.routing-task { flex-wrap:wrap; }.routing-task>div { min-width:0; overflow-wrap:anywhere; } }
</style>
