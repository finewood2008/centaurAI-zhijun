<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, ApiError } from '@/services/api'
import { editableGrant, grantLabel, PERSONAL_SECTIONS, type AccessAudit, type AccessPreview, type AgentGrant, type ExternalAgentStatus, type GrantUpdate } from '@/services/externalAgents'

const status = ref<ExternalAgentStatus | null>(null)
const error = ref(''), notice = ref(''), busy = ref(false), loading = ref(true)
const preview = ref<AccessPreview | null>(null), editing = ref<AgentGrant | null>(null), form = ref<GrantUpdate | null>(null)
const audit = ref<AccessAudit[] | null>(null), legacyConsent = ref(false), disclosure = ref(false)
const controller = new AbortController()
let alive = true
const selectedPersonal = computed(() => preview.value?.personal.filter(c => form.value?.sections.includes(c.section)) ?? [])
const date = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false })

async function load() {
  error.value = ''
  try { const value = await api.externalAgents(controller.signal); if (alive) status.value = value }
  catch (err) {
    if (alive && err instanceof ApiError && (err.status === 404 || err.status === 501 || ['WORKER_OPERATION_INVALID', 'PRODUCT_OPERATION_UNSUPPORTED'].includes(err.code ?? ''))) {
      status.value = { available: false, enabled: false, endpoint: null, grants: [] }
    } else if (alive) error.value = '外部 Agent 设置暂时无法读取，请重试。'
  }
  finally { if (alive) loading.value = false }
}
async function act(operation: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try { await operation(); if (alive) await load() }
  catch (err) { if (alive) error.value = err instanceof ApiError && err.status === 409 ? '授权或资料已变化，请刷新后重新选择。' : '操作未完成，请重试；请勿重复确认敏感资料交付。' }
  finally { if (alive) busy.value = false }
}
async function edit(grant: AgentGrant) {
  await act(async () => {
    const value = await api.externalAgentPreview(controller.signal)
    if (!alive) return
    preview.value = value; editing.value = grant; form.value = editableGrant(grant)
    legacyConsent.value = false; disclosure.value = false
  })
}
async function save() {
  if (!editing.value || !form.value || !disclosure.value) return
  const body = { ...form.value }
  if (legacyConsent.value) body.acknowledgedLegacyIds = Array.from(new Set([
    ...body.acknowledgedLegacyIds,
    ...selectedPersonal.value.filter(c => c.requiresLegacyConfirmation && !body.excludedClaimIds.includes(c.id)).map(c => c.id),
  ]))
  // Removed categories cannot carry their old historical acknowledgements back in implicitly.
  const selected = new Set(selectedPersonal.value.map(c => c.id))
  body.acknowledgedLegacyIds = body.acknowledgedLegacyIds.filter(id => selected.has(id))
  const id = editing.value.id
  await act(async () => { await api.updateExternalGrant(id, body); editing.value = null; form.value = null; notice.value = '授权范围已更新，期限不会自动延长。' })
}
async function copyEndpoint() {
  if (!status.value?.endpoint) return
  try { await navigator.clipboard.writeText(status.value.endpoint); notice.value = 'MCP 地址已复制。在 Agent 中连接后，浏览器会打开授权页面。' }
  catch { error.value = '无法复制，请选中下方地址复制。' }
}
function canManage(grant: AgentGrant) { return grant.state !== 'revoked' && grant.expiresAt * 1000 > Date.now() }
onMounted(load)
onUnmounted(() => { alive = false; controller.abort(); status.value = null; preview.value = null; audit.value = null })
</script>

<template>
  <section class="external-agents" aria-labelledby="external-agents-heading">
    <header><div><h2 id="external-agents-heading">外部 Agent</h2><p>让你选择的助手，在授权范围内理解你、查阅工作资料。</p></div>
      <span class="badge">只读</span></header>
    <p v-if="loading" role="status">正在读取…</p>
    <p v-if="error" class="error" role="alert">{{ error }} <button :disabled="busy" @click="load">重试</button></p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="status">
      <p v-if="!status.available" class="muted">这台盒子尚未开通外部 Agent 连接。盒子服务更新完成后，可在这里启用和管理授权。</p>
      <template v-else>
        <div class="connection-row"><p>{{ status.enabled ? '已启用 · 每个 Agent 仍需单独授权' : '未启用 · 你的资料不会因此开放' }}</p>
          <button :disabled="busy" @click="act(() => api.setExternalAgentsEnabled(!status!.enabled))">{{ status.enabled ? '关闭外部访问' : '启用外部访问' }}</button></div>
        <div v-if="status.enabled && status.endpoint" class="connect-box">
          <button :disabled="busy" @click="copyEndpoint">连接 Agent · 复制 MCP 地址</button><code>{{ status.endpoint }}</code>
          <p>在 WorkBuddy 等支持 MCP 的助手中添加地址，登录后选择资料范围与期限。知君关闭后，在线的盒子仍可提供获准资料。</p>
        </div>
        <div v-if="!status.grants.length" class="empty">还没有授权任何 Agent。</div>
        <article v-for="grant in status.grants" :key="grant.id" class="grant">
          <div class="grant-head"><strong>{{ grant.agentName }}</strong><span>{{ grantLabel(grant) }}</span></div>
          <small>{{ grant.agentId }}</small>
          <p>{{ grant.sections.map(s => PERSONAL_SECTIONS.find(([id]) => id === s)?.[1]).join('、') || '未授权个人信息' }} · {{ grant.materialIds.length }} 份工作资料</p>
          <p class="muted">{{ date(grant.expiresAt) }} 到期</p>
          <div v-if="canManage(grant)" class="actions">
            <button :disabled="busy" @click="edit(grant)">调整范围</button>
            <button :disabled="busy" @click="act(() => api.setExternalGrantState(grant.id, grant.revision, grant.state === 'paused' ? 'active' : 'paused'))">{{ grant.state === 'paused' ? '恢复' : '暂停' }}</button>
            <button :disabled="busy" @click="act(() => api.setExternalGrantState(grant.id, grant.revision, 'revoked'))">撤销授权</button>
          </div>
          <p v-else class="muted">如需重新授权，请在 Agent 中重新连接。</p>
        </article>
        <form v-if="editing && form && preview" class="editor" @submit.prevent="save">
          <h3>调整 {{ editing.agentName }} 的范围</h3>
          <fieldset><legend>个人信息类别</legend><label v-for="[id, title] in PERSONAL_SECTIONS" :key="id"><input v-model="form.sections" type="checkbox" :value="id">{{ title }}</label>
            <p>授权期内，所选类别以后新增的合格信息也会提供。</p></fieldset>
          <details v-if="selectedPersonal.length"><summary>查看内容与排除项</summary>
            <label v-for="claim in selectedPersonal" :key="claim.id"><input v-model="form.excludedClaimIds" type="checkbox" :value="claim.id">排除：{{ claim.content }}</label>
          </details>
          <label><input v-model="legacyConsent" type="checkbox">将当前预览中尚未授权的历史条目纳入所选类别。明确禁止外发的条目始终排除。</label>
          <fieldset><legend>工作资料</legend><label v-for="material in preview.materials" :key="material.id"><input v-model="form.materialIds" type="checkbox" :value="material.id">{{ material.title }}</label><p>新增资料不会自动加入授权。</p></fieldset>
          <label><input v-model="disclosure" type="checkbox" required>我理解内容会交给该 Agent 及其处理服务；撤销不能收回已交付内容。</label>
          <div class="actions"><button type="submit" :disabled="busy || !disclosure">保存范围</button><button type="button" :disabled="busy" @click="editing = null; form = null">取消</button></div>
        </form>
        <button :disabled="busy" @click="act(async () => { const value = await api.externalAgentAudit(controller.signal); if (alive) audit = value.items })">查看调用记录</button>
        <div v-if="audit" class="audit"><p v-if="!audit.length">还没有调用记录。</p><article v-for="record in audit" :key="record.id"><strong>{{ record.agent }}</strong><p>{{ record.operation }} · {{ record.result }} · {{ date(record.created) }}</p><small>回执 {{ record.id }}</small></article></div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.external-agents { border: 1px solid var(--ws-border-color, #dfe3db); border-radius: 16px; padding: 24px; margin: 24px 0; background: var(--ws-surface-color, #fff); }
header, .grant-head, .connection-row { display: flex; justify-content: space-between; gap: 16px; align-items: center; }
h2 { font-size: 20px; margin: 0; } p { line-height: 1.7; margin: 8px 0; } header p, .muted, small { color: var(--ws-text-secondary-color, #657061); }
.badge { background: #eaf0e8; color: #3d6144; border-radius: 20px; padding: 4px 12px; white-space: nowrap; font-size: 12px; }
button { font: inherit; font-size: 13px; padding: 8px 12px; border: 1px solid var(--ws-border-color, #dfe3db); border-radius: 8px; background: transparent; color: inherit; cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
.connect-box, .editor { background: #f4f6f1; padding: 18px; border-radius: 12px; margin: 16px 0; } code { display: block; overflow-wrap: anywhere; user-select: all; margin-top: 12px; font-size: 13px; }
.empty { padding: 24px 0; color: var(--ws-text-secondary-color, #657061); }
.grant, .audit article { padding: 18px 0; border-top: 1px solid var(--ws-border-color, #dfe3db); } .actions { display: flex; gap: 8px; flex-wrap: wrap; }
fieldset { border: 1px solid #d1d9cc; margin: 16px 0; padding: 16px; border-radius: 10px; } label { display: block; line-height: 1.7; margin: 8px 0; } input { margin-right: 8px; } .error { color: #a34632; } small { overflow-wrap: anywhere; }
@media (max-width: 767px) { .external-agents { padding: 18px 14px; } .connection-row { align-items: flex-start; flex-direction: column; } }
</style>
