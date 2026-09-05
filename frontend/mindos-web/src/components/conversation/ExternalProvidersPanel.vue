<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, ApiError, type ChatProviderConfig, type ExternalProviderProfile } from '@/services/api'

const props = defineProps<{ chatRevision: number | null; disabled?: boolean }>()
const emit = defineEmits<{ activated: [config: ChatProviderConfig]; busy: [value: boolean] }>()
const providers = ref<ExternalProviderProfile[]>([])
const selectedId = ref('')
const activeId = ref<string | null>(null)
const serverChatRevision = ref<number | null>(null)
const selected = computed(() => providers.value.find(p => p.id === selectedId.value))
const active = computed(() => providers.value.find(p => p.id === activeId.value))
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const editing = ref(false)
const pendingSelection = ref<string | null>(null)
const deleteConfirm = ref(false)
const outdatedDraft = ref(false)
const draft = reactive({ name: '', baseUrl: '', apiKey: '' })
const baseline = ref('')
const dirty = computed(() => editing.value && JSON.stringify(draft) !== baseline.value)
const endpointChanged = computed(() => !!selected.value && draft.baseUrl.trim().replace(/\/+$/, '') !== selected.value.baseUrl.replace(/\/+$/, ''))
const model = ref('')
const models = ref<string[]>([])
const modelsLoading = ref(false)
const modelError = ref('')
const manual = ref(false)
const cache = new Map<string, { revision: number; models: string[] }>()
const choices = new Map<string, string>()
let alive = true, selectionRevision = 0, listRevision = 0, modelRevision = 0
let modelEditRevision = 0
let modelController: AbortController | undefined
let listController: AbortController | undefined
watch(busy, value => emit('busy', value))
watch(() => props.chatRevision, value => { if (value != null) serverChatRevision.value = Math.max(serverChatRevision.value ?? 0, value) })
function invalidateList() {
  listRevision += 1
  listController?.abort()
  loading.value = false
}

function resetDraft(profile = selected.value) {
  draft.name = profile?.name || ''
  draft.baseUrl = profile?.baseUrl || ''
  draft.apiKey = ''
  baseline.value = JSON.stringify(draft)
}
function choose(id: string) {
  if (selectedId.value) choices.set(selectedId.value, model.value)
  selectedId.value = id
  selectionRevision += 1
  modelRevision += 1
  modelController?.abort()
  modelsLoading.value = false
  error.value = ''; notice.value = ''; modelError.value = ''; deleteConfirm.value = false
  outdatedDraft.value = false
  editing.value = !id
  resetDraft()
  model.value = choices.get(id) ?? selected.value?.model ?? ''
  const saved = cache.get(id)
  models.value = saved && saved.revision === selected.value?.revision ? saved.models : []
  manual.value = !models.value.length || (!!model.value && !models.value.includes(model.value))
  if (id && selected.value?.apiKeyConfigured && !models.value.length) void fetchModels()
}
function selectProvider(id: string) {
  if (id === selectedId.value && !editing.value) return
  if (dirty.value) { pendingSelection.value = id; return }
  choose(id)
}
function discardAndSelect() {
  if (pendingSelection.value === null) return
  const id = pendingSelection.value
  pendingSelection.value = null
  choose(id)
}
function edit() { resetDraft(); editing.value = true; outdatedDraft.value = false; error.value = ''; notice.value = '' }
function cancelEdit() { resetDraft(); editing.value = false; outdatedDraft.value = false; error.value = ''; pendingSelection.value = null }
async function refresh() {
  const ticket = ++listRevision
  const selection = selectionRevision
  listController?.abort()
  listController = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const result = await api.getExternalProviders(listController.signal)
    if (!alive || ticket !== listRevision) return
    const prior = selected.value
    const hadEdits = dirty.value
    providers.value = result.providers
    activeId.value = result.activeProviderId
    serverChatRevision.value = Math.max(serverChatRevision.value ?? 0, result.chatRevision)
    if (prior && prior.revision !== selected.value?.revision) {
      cache.delete(prior.id)
      modelRevision += 1; modelController?.abort(); modelsLoading.value = false
      models.value = []; manual.value = true
      modelError.value = '供应商设置已有更新，原模型输入保留；请重新获取列表并核对。'
      if (hadEdits) outdatedDraft.value = true
    }
    if (selection !== selectionRevision || dirty.value) return
    // Initial read does not contact a supplier. Model discovery is user-triggered.
    if (!selectedId.value && !editing.value) {
      selectedId.value = result.activeProviderId || result.providers[0]?.id || ''
      model.value = selected.value?.model || ''
      manual.value = true
      resetDraft()
      if (!result.providers.length) editing.value = true
    }
  } catch (e) {
    if (alive && ticket === listRevision && !listController.signal.aborted) error.value = e instanceof Error ? e.message : '供应商暂时无法读取'
  } finally { if (alive && ticket === listRevision) loading.value = false }
}
async function fetchModels() {
  const profile = selected.value
  if (!profile) return
  const ticket = ++modelRevision
  const selection = selectionRevision
  const modelEdit = modelEditRevision
  modelController?.abort()
  modelController = new AbortController()
  const signal = modelController.signal
  modelsLoading.value = true
  modelError.value = ''
  try {
    const result = await api.getExternalProviderModels(profile.id, profile.revision, signal)
    if (!alive || ticket !== modelRevision || selection !== selectionRevision || selected.value?.revision !== result.revision || result.providerId !== profile.id) return
    models.value = [...new Set(result.models)]
    cache.set(profile.id, { revision: result.revision, models: models.value })
    if (modelEdit === modelEditRevision) manual.value = !models.value.length || (!!model.value && !models.value.includes(model.value))
    // Never replace a selected or manually typed model with a late response.
    if (!models.value.length) modelError.value = '这个服务没有返回可选模型，可以手动填写模型名。'
  } catch (e) {
    if (!alive || ticket !== modelRevision || signal.aborted) return
    modelError.value = e instanceof ApiError && e.status === 409 ? '供应商已在别处修改，请重新读取后获取模型。' : '暂时无法获取模型列表，可以重试或手动填写模型名。'
    if (!models.value.length) manual.value = true
  } finally { if (alive && ticket === modelRevision) modelsLoading.value = false }
}
async function save() {
  if (busy.value || props.disabled) return
  if (outdatedDraft.value) { error.value = '请先核对新版供应商设置，再决定保留哪份修改。'; return }
  const profile = selected.value
  if (!draft.name.trim() || !draft.baseUrl.trim()) { error.value = '请填写供应商名称和服务地址。'; return }
  if ((!profile?.apiKeyConfigured || endpointChanged.value) && !draft.apiKey.trim()) { error.value = endpointChanged.value ? '服务地址已变更，请重新输入对应的 Token。' : '请填写 API Token。'; return }
  const selection = selectionRevision
  const snapshot = JSON.stringify(draft)
  invalidateList()
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const payload = { name: draft.name.trim(), baseUrl: draft.baseUrl.trim(), ...(draft.apiKey.trim() ? { apiKey: draft.apiKey.trim() } : {}) }
    const saved = profile ? await api.updateExternalProvider(profile.id, { ...payload, revision: profile.revision }) : await api.createExternalProvider(payload)
    if (!alive) return
    providers.value = [saved, ...providers.value.filter(p => p.id !== saved.id)]
    cache.delete(saved.id)
    if (selection !== selectionRevision) return
    selectedId.value = saved.id
    // Editing during save stays intact. Only the exact submitted draft is cleared.
    if (JSON.stringify(draft) === snapshot) { resetDraft(saved); editing.value = false }
    else { error.value = '供应商已保存；你继续输入的修改尚未保存。'; baseline.value = JSON.stringify({ name: saved.name, baseUrl: saved.baseUrl, apiKey: '' }) }
    model.value ||= saved.model || ''
    notice.value = '供应商已保存。选择模型并设为默认后才会用于对话。'
    await fetchModels()
  } catch (e) {
    if (alive && selection === selectionRevision) error.value = e instanceof ApiError && e.status === 409 ? '供应商已在别处修改，未覆盖你的输入。请先重新读取，再核对保存。' : e instanceof Error ? e.message : '保存失败，输入仍保留'
  } finally { if (alive) busy.value = false }
}
async function activate() {
  const profile = selected.value
  if (!profile || busy.value || props.disabled) return
  if (editing.value || !model.value.trim()) { error.value = editing.value ? '请先保存供应商修改，再设置默认模型。' : '请先选择或填写模型。'; return }
  const selection = selectionRevision
  const revision = serverChatRevision.value ?? props.chatRevision
  if (revision == null) { error.value = '请先重新读取配置。'; return }
  invalidateList()
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await api.activateExternalProvider(profile.id, { revision: profile.revision, model: model.value.trim(), chatRevision: revision })
    if (!alive) return
    providers.value = providers.value.map(p => p.id === profile.id ? result.provider : { ...p, active: false })
    activeId.value = result.provider.id
    serverChatRevision.value = result.chat.revision
    const savedModels = cache.get(profile.id)
    if (savedModels?.revision === profile.revision) cache.set(profile.id, { ...savedModels, revision: result.provider.revision })
    emit('activated', result.chat)
    if (selection === selectionRevision) notice.value = `已设为默认：${result.provider.name} · ${result.provider.model}`
  } catch (e) {
    if (alive && selection === selectionRevision) error.value = e instanceof ApiError && e.status === 409 ? '默认配置已在别处更新，未覆盖。请重新读取后再确认。' : e instanceof Error ? e.message : '默认模型未更改，请重试'
  } finally { if (alive) busy.value = false }
}
async function remove() {
  const profile = selected.value
  if (!profile || profile.active || busy.value) return
  const selection = selectionRevision
  invalidateList()
  busy.value = true; error.value = ''
  try {
    await api.deleteExternalProvider(profile.id, profile.revision)
    if (!alive) return
    providers.value = providers.value.filter(p => p.id !== profile.id)
    cache.delete(profile.id); choices.delete(profile.id)
    if (selection === selectionRevision) choose(activeId.value || providers.value[0]?.id || '')
  } catch (e) { if (alive && selection === selectionRevision) error.value = e instanceof Error ? e.message : '未删除，请重试' }
  finally { if (alive) busy.value = false }
}
onMounted(refresh)
onUnmounted(() => { alive = false; modelRevision += 1; listRevision += 1; modelController?.abort(); listController?.abort(); draft.apiKey = '' })
</script>

<template>
  <section class="external-providers" aria-label="在线供应商与默认模型" data-testid="external-providers">
    <header><strong>在线供应商与默认模型</strong><button type="button" :disabled="busy" @click="refresh">重新读取</button></header>
    <p class="external-providers__hint">保存多个 OpenAI 兼容服务，选择一个默认模型。切换供应商不会转移文件、画像或历史的授权。</p>
    <p v-if="active">已选默认：{{ active.name }} · {{ active.model || '尚未选择模型' }}<span v-if="active.pendingActivation">（修改待启用）</span></p>
    <p v-if="loading" role="status">正在读取供应商…</p>
    <div class="external-providers__list" aria-label="已保存供应商">
      <button v-for="profile in providers" :key="profile.id" type="button" :aria-pressed="selectedId === profile.id" @click="selectProvider(profile.id)">{{ profile.name }}<small v-if="profile.active">默认</small></button>
      <button type="button" @click="selectProvider('')">＋ 添加供应商</button>
    </div>
    <div v-if="pendingSelection !== null" class="external-providers__notice" role="status">有未保存修改。<button type="button" @click="discardAndSelect">放弃修改并切换</button><button type="button" @click="pendingSelection = null">继续编辑</button></div>
    <div v-if="outdatedDraft" class="external-providers__notice" role="status">另一处更新了供应商：{{ selected?.name || '已删除' }} · {{ selected?.baseUrl || '原记录不再存在' }}。你的输入仍保留。
      <button type="button" @click="outdatedDraft = false; error = ''">保留我的输入，基于新版保存</button><button type="button" @click="resetDraft(); outdatedDraft = false; error = ''">放弃我的修改，读取新版</button>
    </div>
    <form v-if="editing" class="external-providers__editor" @submit.prevent="save">
      <label>供应商名称<input v-model="draft.name" maxlength="80" placeholder="例如：我的 DeepSeek" autocomplete="off" /></label>
      <label>服务地址<input v-model="draft.baseUrl" type="url" maxlength="2048" placeholder="https://api.example.com/v1" autocomplete="off" /></label>
      <label>API Token<input v-model="draft.apiKey" type="password" maxlength="8192" autocomplete="new-password" :placeholder="selected?.apiKeyConfigured ? '留空保留已保存的 Token' : '输入供应商提供的 Token'" />
        <small>{{ selected?.apiKeyConfigured ? '已保存 Token，不回显。' : 'Token 只交给本机后端保存，不存入浏览器。' }}<template v-if="endpointChanged"> 地址已变更，需要重新输入 Token。</template></small>
      </label>
      <div class="external-providers__actions"><button type="submit" :disabled="busy || disabled">{{ busy ? '正在保存…' : '保存并获取模型' }}</button><button v-if="selected" type="button" :disabled="busy" @click="cancelEdit">取消编辑</button></div>
    </form>
    <template v-else-if="selected">
      <p class="external-providers__endpoint">{{ selected.baseUrl }} · {{ selected.apiKeyConfigured ? 'Token 已保存' : '未配置 Token' }} <button type="button" @click="edit">编辑供应商</button></p>
      <label class="external-providers__model">选择模型
        <select v-if="!manual && models.length" v-model="model" @change="modelEditRevision += 1"><option value="" disabled>请选择一个模型</option><option v-if="model && !models.includes(model)" :value="model">{{ model }}（已保存）</option><option v-for="name in models" :key="name" :value="name">{{ name }}</option></select>
        <input v-else v-model="model" maxlength="128" placeholder="输入完整模型名称" autocomplete="off" @input="modelEditRevision += 1" />
      </label>
      <div class="external-providers__actions"><button type="button" :disabled="modelsLoading || busy" @click="fetchModels">{{ modelsLoading ? '正在获取模型…' : '获取模型列表' }}</button><button v-if="models.length" type="button" @click="manual = !manual; modelEditRevision += 1">{{ manual ? '从列表选择' : '手动填写模型' }}</button></div>
      <p v-if="modelError" class="external-providers__hint" role="status">{{ modelError }}</p>
      <p class="external-providers__hint">保存后，已启用在线理解的对话默认使用它；仅本地的对话保持不变。换服务需重新核对在线模式与资料授权，失败不会自动换供应商。</p>
      <div class="external-providers__actions"><button type="button" class="external-providers__primary" :disabled="busy || disabled || !model.trim() || !selected.apiKeyConfigured" @click="activate">设为默认并启用</button><button v-if="!selected.active" type="button" :disabled="busy" @click="deleteConfirm = true">删除供应商</button></div>
      <p v-if="deleteConfirm" class="external-providers__notice">删除这项供应商及保存的 Token？<button type="button" :disabled="busy" @click="remove">确认删除</button><button type="button" @click="deleteConfirm = false">取消</button></p>
    </template>
    <p v-if="error" role="alert" class="external-providers__error">{{ error }}</p>
    <p v-if="notice" role="status" class="external-providers__hint">{{ notice }}</p>
  </section>
</template>
<style scoped>
.external-providers{display:grid;gap:12px;min-width:0;font-size:13px}.external-providers header,.external-providers__actions,.external-providers__list{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.external-providers header{justify-content:space-between}.external-providers p{margin:0;line-height:1.6;overflow-wrap:anywhere}.external-providers button{font:inherit;color:inherit;background:transparent;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:8px;padding:7px 10px;cursor:pointer}.external-providers button:disabled{opacity:.5;cursor:default}.external-providers button[aria-pressed=true]{border-color:var(--ws-primary-color,#a6452e);color:var(--ws-primary-color,#a6452e);background:var(--ws-primary-soft,#fbf1eb)}.external-providers button small{margin-left:6px;font-size:11px}.external-providers__editor{display:grid;gap:12px;padding:14px;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:10px}.external-providers label{display:grid;gap:6px;min-width:0}.external-providers input,.external-providers select{box-sizing:border-box;width:100%;min-width:0;padding:9px 10px;border:1px solid var(--ws-border-color,#ddd4c7);border-radius:7px;background:var(--ws-card-bg,#fff);color:inherit;font:inherit}.external-providers__hint,.external-providers small{font-size:12px;color:var(--ws-text-secondary-color,#686b66)}.external-providers .external-providers__primary{background:var(--ws-primary-color,#a6452e);border-color:transparent;color:white}.external-providers__error{color:var(--ws-primary-color,#a6452e)}.external-providers__notice{padding:10px;background:var(--ws-surface-2,#fbf8f1);border-radius:8px}.external-providers__notice button{margin-left:6px}.external-providers__endpoint{font-size:12px}
</style>
