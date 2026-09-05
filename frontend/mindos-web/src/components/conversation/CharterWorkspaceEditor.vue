<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { CharterWorkspace, GrowthCharter } from '@/services/api'
import { routedTask, routingRequest } from '@/services/taskRouting'
import { downloadCharterMarkdown, renderCharterClauses, workspaceMarkdown } from '@/shared/charterWorkspace'
import CharterDocument from './CharterDocument.vue'

const props = defineProps<{ workspace: CharterWorkspace; appearance?: 'compact' | 'codex' }>()
const emit = defineEmits<{ updated: [CharterWorkspace]; published: [GrowthCharter]; dirty: [boolean] }>()
const base = ref<CharterWorkspace>(props.workspace)
const markdown = ref(workspaceMarkdown(props.workspace))
const view = ref<'edit' | 'preview'>('edit')
const busy = ref('')
const error = ref('')
const notice = ref('')
const failedAction = ref('')
const legacySource = ref('')
let controller: AbortController | null = null
let retry: { key: string; requestId: string } | null = null
const baseMarkdown = computed(() => workspaceMarkdown(base.value))
const dirty = computed(() => markdown.value !== baseMarkdown.value)
const editable = computed(() => base.value.status === 'active')
const outdated = computed(() => props.workspace.id === base.value.id && props.workspace.revision > base.value.revision)
const pending = computed(() => props.workspace.suggestions.filter(s => s.status === 'pending'))
const controlChanges = computed(() => base.value.controlChanges ?? [])
const oldSource = computed(() => {
  const source = legacySource.value || base.value.sourceText
  const text = source?.trim()
  return text && !markdown.value.includes(text) ? source : ''
})
const path = computed(() => `/mindos/conversations/${encodeURIComponent(base.value.conversationId)}/charter/workspace/${encodeURIComponent(base.value.id)}`)
const bufferKey = (id: string) => `zhijun-charter-markdown:${id}`

function restoreBuffer() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(bufferKey(base.value.id)) || 'null')
    if (saved && typeof saved.markdown === 'string') {
      legacySource.value = typeof saved.legacySource === 'string' ? saved.legacySource : ''
      if (typeof saved.baseRevision === 'number' && typeof saved.baseMarkdown === 'string') {
        // A remote publish/pause does not confirm or discard this browser's
        // unsaved text. Restore its active baseline, then show the conflict.
        base.value = { ...base.value, status: 'active', revision: saved.baseRevision, document: saved.baseMarkdown, documentFormat: 'markdown' }
      }
      markdown.value = saved.markdown
    } else {
      // Preserve unsaved edits from the former multi-field editor, without
      // automatically publishing its separate original-thoughts field.
      const legacy = JSON.parse(sessionStorage.getItem(`zhijun-charter-buffer:${base.value.id}`) || 'null')
      if (legacy && Array.isArray(legacy.clauses) && typeof legacy.sourceText === 'string') {
        legacySource.value = legacy.sourceText
        const text = renderCharterClauses(legacy.clauses) || legacy.sourceText
        if (legacy.baseRevision !== base.value.revision && Array.isArray(legacy.baseClauses)) {
          base.value = { ...base.value, status: 'active', revision: legacy.baseRevision, sourceText: legacy.sourceText,
            document: renderCharterClauses(legacy.baseClauses) || legacy.baseSourceText || '', documentFormat: 'markdown' }
        } else base.value = { ...base.value, sourceText: legacy.sourceText }
        markdown.value = text
      }
    }
  } catch { /* Browser storage is optional; the server draft remains authoritative. */ }
}
restoreBuffer()
function persistBuffer() {
  try {
    if ((dirty.value || legacySource.value) && editable.value) sessionStorage.setItem(bufferKey(base.value.id), JSON.stringify({
      baseRevision: base.value.revision, baseMarkdown: baseMarkdown.value, markdown: markdown.value, legacySource: legacySource.value,
    }))
    else sessionStorage.removeItem(bufferKey(base.value.id))
    // Only retire the former buffer after its replacement is safely stored.
    if (sessionStorage.getItem(bufferKey(base.value.id)) || !dirty.value) sessionStorage.removeItem(`zhijun-charter-buffer:${base.value.id}`)
  } catch { /* Never block typing if local draft storage is unavailable. */ }
}
watch([markdown, base, legacySource], persistBuffer, { deep: true })
watch(dirty, value => emit('dirty', value), { immediate: true })
watch(() => props.workspace, value => {
  if (value.id !== base.value.id || (!dirty.value && value.revision >= base.value.revision)) apply(value)
  else if (value.status === 'active' && base.value.status === 'active' && value.revision > base.value.revision
    && workspaceMarkdown(value) === baseMarkdown.value) base.value = value
})
function apply(value: CharterWorkspace) { base.value = value; markdown.value = workspaceMarkdown(value) }
function requestId(key: string) {
  if (retry?.key !== key) retry = { key, requestId: crypto.randomUUID() }
  return retry.requestId
}
async function mutate(action: string, data: Record<string, unknown>, method = 'POST') {
  const key = JSON.stringify([base.value.id, action, data])
  const sent = markdown.value
  const result = await routingRequest<{ workspace: CharterWorkspace; charter?: GrowthCharter }>(path.value + action, method, {
    ...data, requestId: requestId(key),
  })
  retry = null
  if (result.workspace.revision < Math.max(base.value.revision, props.workspace.revision)) return result
  if (markdown.value === sent) apply(result.workspace)
  else base.value = result.workspace
  emit('updated', result.workspace)
  return result
}
async function saveBuffer() {
  if (outdated.value) throw new Error('另一处已更新工作稿。你的正文仍保留；请先核对新版本。')
  if (!dirty.value && base.value.documentFormat === 'markdown' && !legacySource.value) return
  await mutate('', { revision: base.value.revision, document: markdown.value,
    ...(legacySource.value ? { sourceText: legacySource.value } : {}),
  }, 'PUT')
  legacySource.value = ''
}
async function run(action: string, work: () => Promise<void>) {
  if (busy.value) return
  busy.value = action; error.value = ''; notice.value = ''; failedAction.value = ''
  try { await work() }
  catch (e) { failedAction.value = action; error.value = e instanceof Error ? e.message : '操作未完成，你的正文仍保留。' }
  finally { busy.value = '' }
}
function save() { return run('save', async () => { await saveBuffer(); notice.value = '草稿已保存，尚未生效。' }) }
function generate(localOnly = false) { return run('suggest', async () => {
  await saveBuffer()
  controller?.abort(); controller = new AbortController()
  const key = JSON.stringify(['suggest', base.value.id, base.value.revision, localOnly])
  const result = await routedTask<{ workspace: CharterWorkspace }>(base.value.conversationId, path.value + '/suggest', {
    requestId: requestId(key), localOnly,
  }, controller.signal)
  retry = null
  if (result.workspace.revision < Math.max(base.value.revision, props.workspace.revision)) {
    notice.value = '已有较新的草稿，这次返回没有覆盖正文。'; return
  }
  if (!dirty.value) apply(result.workspace)
  emit('updated', result.workspace)
  notice.value = result.workspace.suggestions.some(s => s.status === 'pending')
    ? '知君整理了一份完整正文，请核对后再采用。' : '已整理为一份 Markdown 草稿，尚未生效。'
}) }
function merge(id: string) { return run('merge', async () => {
  if (dirty.value) throw new Error('你还有未保存的正文。请先保存，再决定是否采用知君的版本。')
  await mutate('/merge', { revision: base.value.revision, suggestionId: id })
  view.value = 'preview'; notice.value = '已采用到草稿，尚未生效。请阅读全文后确认。'
}) }
function publish(confirmControlChanges = false) { return run('publish', async () => {
  if (!markdown.value.trim()) throw new Error('章程正文还是空的，不会创建空版本。')
  await saveBuffer()
  // Never confirm an older snapshot while the user is still changing its text.
  if (dirty.value) throw new Error('保存期间正文又有修改。请核对最新文字后再次确认。')
  if (controlChanges.value.length && !confirmControlChanges) {
    notice.value = '正文涉及原有自动执行约定的变化，请核对下方说明后再确认。'; return
  }
  const result = await mutate('/publish', { revision: base.value.revision, publishDocument: true,
    ...(confirmControlChanges ? { confirmControlChanges: true } : {}),
  })
  if (result.charter) emit('published', result.charter)
  notice.value = '人生章程已确认生效。以后只有你主动修改并再次确认才会更新。'
}) }
function pause() { return run('pause', async () => {
  await saveBuffer()
  if (dirty.value) throw new Error('正文还有新修改，请先保存后再结束。')
  await mutate('/pause', { revision: base.value.revision }); notice.value = '修改已结束，草稿仍然保留。'
}) }
function keepLocal() {
  if (props.workspace.status !== 'active') { error.value = '另一处已结束这份草稿。你的正文仍保留，请先下载 .md，再主动开始修改。'; return }
  base.value = props.workspace; error.value = ''; notice.value = '已保留你的正文。请核对后保存为新的草稿修订。'
}
function discardLocal() { apply(props.workspace); notice.value = '已载入最新工作稿。' }
function appendOldSource() {
  if (oldSource.value) markdown.value += `${markdown.value.trim() ? '\n\n' : ''}## 旧稿中的原始想法\n\n${oldSource.value}`
}
function suggestionDocument(suggestion: CharterWorkspace['suggestions'][number]) { return suggestion.document || renderCharterClauses(suggestion.clauses) }
async function refreshLatest() {
  try {
    const state = await routingRequest<{ workspace: CharterWorkspace | null }>(`/mindos/conversations/${encodeURIComponent(base.value.conversationId)}/charter`)
    if (state.workspace?.id === base.value.id) emit('updated', state.workspace)
    else error.value = '这份草稿已结束或被其他修改替代。你的正文仍保留，可先下载 .md，再回到章程页面查看。'
  } catch (e) { error.value = e instanceof Error ? e.message : '暂时无法读取最新草稿。' }
}
onBeforeUnmount(() => { persistBuffer(); controller?.abort() })
</script>

<template>
  <section class="charter-editor" :class="{ 'charter-editor--codex': appearance === 'codex' }" data-testid="charter-workspace">
    <header class="charter-editor__head">
      <div><span v-if="appearance === 'codex'" class="charter-editor__eyebrow">WORKING COPY · 章程工作稿</span><h3>章程正文</h3><p>{{ editable ? 'Markdown 草稿 · 尚未生效' : base.status === 'published' ? '已确认' : '草稿已暂停' }} · {{ dirty ? '有未保存修改' : '草稿已保存' }} · 修订 {{ base.revision }}</p></div>
      <button v-if="markdown" class="charter-editor__quiet" type="button" @click="downloadCharterMarkdown(markdown)">下载 .md</button>
    </header>
    <p>这是一篇完整的文档。你可以直接改标题、段落和列表，不需要逐项填写。</p>
    <template v-if="editable">
      <nav class="charter-editor__tabs" aria-label="正文查看方式">
        <button :aria-current="view === 'edit'" @click="view = 'edit'">编辑正文</button>
        <button :aria-current="view === 'preview'" @click="view = 'preview'">预览</button>
      </nav>
      <textarea v-if="view === 'edit'" v-model="markdown" class="charter-editor__markdown" rows="22" maxlength="30000"
        :readonly="!!busy && busy !== 'suggest'" aria-label="章程正文（Markdown）" placeholder="# 我的人生章程&#10;&#10;写下对你重要的方向、原则、边界，以及希望知君如何与你合作。" />
      <div v-else class="charter-editor__preview"><CharterDocument :document="markdown" :appearance="appearance" /></div>
      <details v-if="oldSource" class="charter-editor__legacy"><summary>旧稿中的原始想法</summary>
        <p>这段旧内容没有自动并入正文，避免替你确认。需要时可以主动加入后再修改。</p><pre>{{ oldSource }}</pre>
        <button :disabled="!!busy" @click="appendOldSource">加入正文</button>
      </details>
      <section v-if="outdated" class="charter-editor__conflict" role="status"><p>{{ workspace.status === 'active' ? '工作稿有了新修订，你正在输入的正文没有被替换。' : '另一处已结束这份草稿。这里的未保存正文仍保留，但尚未生效；你可以先下载 .md。' }}</p>
        <details><summary>查看较新版本</summary><CharterDocument :document="workspaceMarkdown(workspace)" /></details>
        <div class="charter-editor__actions"><button v-if="workspace.status === 'active'" :disabled="!!busy" @click="keepLocal">保留我的正文</button><button :disabled="!!busy" @click="discardLocal">放弃本地修改，载入新版本</button></div>
      </section>
      <section v-if="pending.length" class="charter-editor__suggestions">
        <h3>知君整理的正文</h3><p>这是建议版本，不会自动替换你的草稿。</p>
        <details v-for="suggestion in pending" :key="suggestion.id">
          <summary>查看建议正文 · 基于修订 {{ suggestion.sourceRevision }}</summary>
          <CharterDocument :document="suggestionDocument(suggestion)" />
          <button :disabled="!!busy || dirty" @click="merge(suggestion.id)">采用这份正文</button>
        </details>
      </section>
      <section v-if="controlChanges.length" class="charter-editor__conflict" role="status">
        <p>这次修改改变了以下自动执行约定。确认后，它们不再由程序强制执行；新正文仍用于指导知君。</p>
        <ul><li v-for="change in controlChanges" :key="change.id">{{ change.text }}</li></ul>
        <p>没有确认之前，原章程和原有规则保持不变。</p>
      </section>
      <div class="charter-editor__actions">
        <button :disabled="!!busy || (!dirty && base.documentFormat === 'markdown' && !legacySource)" @click="save">{{ busy === 'save' ? '正在保存…' : '保存草稿' }}</button>
        <button :disabled="!!busy" @click="generate()">{{ busy === 'suggest' ? '正在整理…' : '根据对话整理' }}</button>
        <button v-if="busy === 'suggest'" @click="controller?.abort()">取消整理</button>
        <button class="charter-editor__primary" :disabled="!!busy || !markdown.trim()" @click="publish(controlChanges.length > 0 && !dirty)">{{ controlChanges.length ? '确认正文与规则变更' : '确认并生效' }}</button>
      </div>
      <details class="charter-editor__more"><summary>更多操作</summary><button :disabled="!!busy" @click="pause">结束本次修改，保留草稿</button></details>
      <p class="charter-editor__meta">正文用于指导知君，保存草稿不会生效。确认章程不确认本体，也不代替资料外发授权。</p>
    </template>
    <template v-else><CharterDocument :document="markdown" :appearance="appearance" /><p>需要调整时，请主动开始修改。日常聊天不会自动改动正文。</p></template>
    <p v-if="notice" role="status" class="charter-editor__notice">{{ notice }}</p>
    <p v-if="error" role="alert" class="charter-editor__error">{{ error }} <button v-if="failedAction === 'suggest'" :disabled="!!busy" @click="generate(true)">仅本地整理</button><button v-else :disabled="!!busy" @click="refreshLatest">核对最新草稿</button></p>
  </section>
</template>

<style scoped>
.charter-editor { font-size:14px; line-height:1.75; overflow-wrap:anywhere; }
.charter-editor__head { display:flex; align-items:start; justify-content:space-between; gap:16px; margin-bottom:16px; }
.charter-editor__head h3 { margin:0; font:600 20px var(--ws-font-display); }
.charter-editor__head p,.charter-editor__meta { margin:3px 0 0; color:var(--ws-text-secondary-color); font-size:12px; }
.charter-editor button { font:inherit; color:inherit; background:transparent; border:1px solid var(--ws-border-color); border-radius:9px; padding:7px 11px; cursor:pointer; }
.charter-editor button:disabled { opacity:.5; cursor:default; }
.charter-editor__tabs { display:flex; gap:4px; margin:18px 0 8px; }
.charter-editor__tabs button { border:0; border-radius:0; color:var(--ws-text-secondary-color); border-bottom:2px solid transparent; }
.charter-editor__tabs button[aria-current=true] { color:var(--ws-text-color); border-color:var(--ws-primary-color); }
.charter-editor__markdown { display:block; width:100%; min-height:420px; box-sizing:border-box; resize:vertical; padding:18px; color:inherit; background:var(--ws-card-bg); border:1px solid var(--ws-border-color); border-radius:10px; font:14px/1.75 ui-monospace,SFMono-Regular,Menlo,monospace; }
.charter-editor__preview { min-height:420px; padding:18px; border:1px solid var(--ws-border-color); border-radius:10px; background:var(--ws-card-bg); }
.charter-editor__actions { display:flex; flex-wrap:wrap; gap:8px; margin:16px 0; }
.charter-editor .charter-editor__primary { background:var(--ws-primary-color); border-color:var(--ws-primary-color); color:#fff; }
.charter-editor .charter-editor__quiet { border:0; color:var(--ws-text-secondary-color); white-space:nowrap; }
.charter-editor__legacy,.charter-editor__more { margin:16px 0; color:var(--ws-text-secondary-color); }
.charter-editor summary { cursor:pointer; }
.charter-editor__legacy pre { white-space:pre-wrap; padding:12px; background:var(--ws-surface-2); color:var(--ws-text-color); }
.charter-editor__suggestions,.charter-editor__conflict { margin:18px 0; padding:14px; border:1px solid var(--ws-border-color); border-radius:10px; }
.charter-editor__suggestions h3 { margin:0; font-size:17px; }.charter-editor__suggestions details { margin:12px 0; }
.charter-editor__notice { color:var(--ws-success-color,#4c7d61); }.charter-editor__error { color:var(--ws-primary-color); }
.charter-editor--codex { position:relative; z-index:1; padding:24px clamp(20px,4vw,38px) 20px; border:1px solid #d6cab9; outline:1px solid rgba(166,69,46,.12); outline-offset:-8px; background:#fffdf8; box-shadow:0 12px 34px rgba(58,44,29,.1); }
.charter-editor--codex::before { content:""; position:absolute; inset:14px auto 14px 15px; width:2px; border-left:1px solid rgba(166,69,46,.22); border-right:1px solid rgba(166,69,46,.09); pointer-events:none; }
.charter-editor--codex .charter-editor__head { max-width:820px; margin:0 auto 20px; padding-bottom:14px; border-bottom:1px solid #d9d0c3; }
.charter-editor__eyebrow { display:block; margin-bottom:5px; color:var(--ws-primary-color); font-size:9px; font-weight:700; letter-spacing:.16em; }
.charter-editor--codex .charter-editor__head h3 { font-size:25px; letter-spacing:.06em; }
.charter-editor--codex > p:not([role]) { max-width:820px; margin:0 auto; color:var(--ws-text-secondary-color); }
.charter-editor--codex .charter-editor__tabs { width:min(820px,100%); margin:22px auto 0; border-bottom:1px solid #ddd4c7; }
.charter-editor--codex .charter-editor__tabs button { min-height:40px; padding:8px 16px; }
.charter-editor--codex .charter-editor__markdown,.charter-editor--codex .charter-editor__preview { width:min(820px,100%); min-height:560px; margin:0 auto; border:0; border-radius:0; background:transparent; box-shadow:none; }
.charter-editor--codex .charter-editor__markdown { padding:38px clamp(10px,4vw,52px) 48px; color:#292b28; font:16px/2 ui-monospace,SFMono-Regular,Menlo,monospace; caret-color:var(--ws-primary-color); }
.charter-editor--codex .charter-editor__markdown:focus { outline:0; background:rgba(248,243,234,.45); box-shadow:inset 0 0 0 1px rgba(166,69,46,.2); }
.charter-editor--codex .charter-editor__preview { padding:38px clamp(10px,4vw,52px) 48px; }
.charter-editor--codex .charter-editor__legacy,.charter-editor--codex .charter-editor__suggestions,.charter-editor--codex .charter-editor__conflict,.charter-editor--codex .charter-editor__actions,.charter-editor--codex .charter-editor__more,.charter-editor--codex .charter-editor__meta,.charter-editor--codex .charter-editor__notice,.charter-editor--codex .charter-editor__error { width:min(820px,100%); margin-left:auto; margin-right:auto; }
.charter-editor--codex .charter-editor__actions { align-items:center; padding-top:16px; border-top:1px solid #ddd4c7; }
.charter-editor--codex button { border-radius:4px; background:#fffaf2; }
.charter-editor--codex .charter-editor__tabs button { background:transparent; }
.charter-editor--codex .charter-editor__primary { margin-left:auto; }
@media(max-width:600px) { .charter-editor--codex { padding:20px 18px 16px; outline-offset:-5px; }.charter-editor--codex::before { display:none; }.charter-editor--codex .charter-editor__markdown,.charter-editor--codex .charter-editor__preview { min-height:60dvh; padding:26px 4px 34px; }.charter-editor--codex .charter-editor__primary { width:100%; margin-left:0; text-align:center; }.charter-editor--codex .charter-editor__head { gap:8px; } }
@media(max-width:600px) { .charter-editor__markdown,.charter-editor__preview { min-height:360px; padding:12px; }.charter-editor__head { gap:8px; } }
</style>
