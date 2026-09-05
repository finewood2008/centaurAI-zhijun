<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, createConversation, type CharterWorkspace, type GrowthCharter } from '@/services/api'
import { routingRequest } from '@/services/taskRouting'
import { charterDocument, downloadCharterMarkdown, workspaceMarkdown } from '@/shared/charterWorkspace'
import { formatDate } from '@/shared/format'
import SelfSections from '@/components/ontology/SelfSections.vue'
import CharterDocument from '@/components/conversation/CharterDocument.vue'
import CharterWorkspaceEditor from '@/components/conversation/CharterWorkspaceEditor.vue'
const route = useRoute()
const router = useRouter()
const charter = ref<GrowthCharter | null>(null)
const history = ref<GrowthCharter[]>([])
const workspace = ref<CharterWorkspace | null>(null)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const editing = ref(false)
const editorDirty = ref(false)
const versionMode = computed(() => route.query.version !== undefined)
const displayCharter = computed(() => versionMode.value ? history.value.find(v => String(v.version) === route.query.version) ?? null : charter.value)
const hasDraft = computed(() => !versionMode.value && workspace.value && workspace.value.status !== 'published')
const readingDocument = computed(() => hasDraft.value && workspace.value ? workspaceMarkdown(workspace.value) : displayCharter.value ? charterDocument(displayCharter.value) : '')
const documentState = computed(() => {
  if (versionMode.value) return displayCharter.value ? `历史存卷 · 第 ${displayCharter.value.version} 版` : '历史存卷 · 未找到'
  if (hasDraft.value && workspace.value) return workspace.value.baseVersion > 0 ? `工作稿 · 基于第 ${workspace.value.baseVersion} 版` : '工作稿 · 尚未生效'
  if (displayCharter.value) return `已确认 · 第 ${displayCharter.value.version} 版`
  return '尚未建立'
})
let startRequest: { conversationId: string; requestId: string } | null = null
watch(() => route.query.version, () => { editing.value = false })
async function load() {
  loading.value = true; error.value = ''
  try {
    const result = await api.getGrowthCharter()
    charter.value = result.currentCharter; history.value = result.versions; workspace.value = result.workspace ?? null
  } catch (e) { error.value = e instanceof Error ? e.message : '暂时无法读取章程' }
  finally { loading.value = false }
}
async function start(mode: 'chat' | 'manual') {
  if (busy.value) return
  if (mode === 'chat' && editorDirty.value) { error.value = '请先保存工作稿，再切换到对话，让知君能读到你刚刚填写的内容。'; return }
  busy.value = true; error.value = ''
  try {
    if (!startRequest) {
      const conversationId = workspace.value?.conversationId ?? (await createConversation({ mode: 'chat', taskContext: 'charter', title: charter.value ? '修改人生章程' : '聊聊人生章程' })).id
      startRequest = { conversationId, requestId: crypto.randomUUID() }
    }
    // A read or historical route must never implicitly start a session.
    const result = await routingRequest<{ workspace: CharterWorkspace; conversationId: string }>(`/mindos/conversations/${encodeURIComponent(startRequest.conversationId)}/charter/workspace/start`, 'POST', { requestId: startRequest.requestId })
    workspace.value = result.workspace; startRequest = null
    if (mode === 'manual') { editing.value = true; return }
    const say = workspaceMarkdown(result.workspace)
      ? '我想接着完善我的人生章程。请通过对话帮我整理为一篇完整的 Markdown 正文，保留我自己写的部分，一次最多问一个必要问题，等我确认后再生效。'
      : '我想通过对话生成人生章程。请先和我聊聊现在最在意的事，再整理为一篇完整的 Markdown 正文，不要求我填写固定栏目，也不需要一次想清楚全部。'
    await router.push({ path: `/c/${result.conversationId}`, query: { charter: '1', say } })
  } catch (e) { error.value = e instanceof Error ? e.message : '暂时无法开始，现有内容没有改变' }
  finally { busy.value = false }
}
function published(value: GrowthCharter) {
  if (charter.value && value.version < charter.value.version) return
  if (!charter.value || value.version >= charter.value.version) charter.value = value
  history.value = [value, ...history.value.filter(v => v.id !== value.id)]
  editing.value = false; editorDirty.value = false
}
function updated(value: CharterWorkspace) {
  if (!workspace.value || workspace.value.id !== value.id || value.revision >= workspace.value.revision) workspace.value = value
}
function editDocument() {
  if (workspace.value?.status === 'active') editing.value = true
  else void start('manual')
}
function draftChanged(value: boolean) {
  editorDirty.value = value
  if (!value && error.value.startsWith('请先保存工作稿')) error.value = ''
}
onMounted(load)
</script>
<template>
  <main class="page charter-page">
    <div class="page-head"><h1>我的本体</h1><p>本体记录知君对你的理解，章程写下你自己的方向与约定。</p></div>
    <SelfSections />
    <p v-if="versionMode" class="charter-page__notice">正在查看历史版本，只读，不影响当前章程。<RouterLink to="/me/charter">查看当前章程</RouterLink></p>
    <section class="charter-page__panel" aria-labelledby="charter-heading">
      <header class="charter-page__masthead">
        <span class="charter-page__seal" aria-hidden="true">章</span>
        <div class="charter-page__title">
          <span class="charter-page__eyebrow">PERSONAL CHARTER · 私人法典</span>
          <h2 id="charter-heading">人生章程</h2>
          <p>由你亲自确认的长期方向与合作约定。知君遵循，但不会替你改写。</p>
        </div>
        <div class="charter-page__edition" aria-label="章程状态">
          <span class="charter-page__state" :class="{ 'is-draft': hasDraft, 'is-history': versionMode }">{{ documentState }}</span>
          <span v-if="displayCharter" class="charter-page__version">{{ formatDate(displayCharter.createdAt) }}</span>
        </div>
      </header>
      <div class="charter-page__rule" aria-hidden="true"><span></span><i>◆</i><span></span></div>
      <p v-if="error" class="charter-page__error" role="alert">{{ error }} <button :disabled="busy" @click="load">重新读取</button></p>
      <p v-if="loading" class="charter-page__loading" role="status">正在展开人生章程…</p>
      <template v-else>
        <div v-if="!versionMode" class="charter-page__actions">
          <button class="charter-page__primary" :disabled="busy" @click="start('chat')">{{ hasDraft ? '继续对话' : charter ? '通过对话修改' : '通过对话生成' }}</button>
          <button v-if="!editing" :disabled="busy" @click="editDocument">编辑正文</button>
          <button v-if="readingDocument && !editing" class="charter-page__download" @click="downloadCharterMarkdown(readingDocument)">下载 .md</button>
        </div>
        <p v-if="hasDraft && !editing" class="charter-page__draft-note"><strong>这是一份工作稿。</strong> 对话与编辑共用这一篇；确认生效以前，当前正式章程保持不变。</p>
        <CharterWorkspaceEditor v-if="!versionMode && editing && workspace" :key="workspace.id" :workspace="workspace" @updated="updated" @published="published" @dirty="draftChanged" appearance="codex" />
        <div v-else-if="readingDocument" class="charter-page__folio">
          <aside class="charter-page__margin" aria-label="章程卷宗信息">
            <div><span>STATUS</span><strong>{{ hasDraft ? '工作稿' : versionMode ? '历史存卷' : '正式生效' }}</strong></div>
            <div v-if="displayCharter"><span>EDITION</span><strong>第 {{ displayCharter.version }} 版</strong></div>
            <div v-if="displayCharter"><span>DATED</span><strong>{{ formatDate(displayCharter.createdAt) }}</strong></div>
            <p>{{ hasDraft ? '正文尚未生效，你可以继续修改。' : versionMode ? '只读存卷，不改变当前章程。' : '只有你主动修改并确认，正式章程才会更新。' }}</p>
          </aside>
          <article class="charter-page__leaf" aria-label="人生章程正文">
            <div class="charter-page__leaf-head"><span>知君 · 人生章程</span><span>{{ hasDraft ? 'DRAFT' : versionMode ? 'ARCHIVE' : 'IN FORCE' }}</span></div>
            <CharterDocument :document="readingDocument" appearance="codex" />
            <footer class="charter-page__leaf-foot" aria-label="章程确认说明"><span></span><small>{{ hasDraft ? '待你确认' : versionMode ? '历史版本' : '由你确认并生效' }}</small><span></span></footer>
          </article>
        </div>
        <p v-else-if="versionMode">未找到这个历史版本，不会用当前章程替代当时的依据。</p>
        <div v-else class="charter-page__empty">
          <span aria-hidden="true">章</span><h3>写下属于你的第一条约定</h3>
          <p>可以从一件正在意的事聊起，也可以直接写正文。没有固定框架，不用一次想清楚全部。</p>
        </div>
        <details v-if="history.length" class="charter-page__history"><summary><span>版本存卷</span><small>共 {{ history.length }} 版</small></summary><div><p v-for="version in history" :key="version.id"><RouterLink :to="{ path: '/me/charter', query: { version: version.version } }"><strong>第 {{ version.version }} 版</strong><span>{{ formatDate(version.createdAt) }}</span></RouterLink></p></div></details>
      </template>
    </section>
  </main>
</template>
<style scoped>
.charter-page { max-width:1080px; padding-bottom:56px; }
.charter-page__panel { position:relative; overflow:hidden; padding:34px clamp(20px,4vw,46px) 28px; border:1px solid #d7cdbd; border-radius:4px; background:linear-gradient(145deg,#fbf7ee 0%,#f5efe3 100%); box-shadow:0 18px 52px rgba(58,44,29,.09); line-height:1.8; }
.charter-page__panel::before,.charter-page__panel::after { content:""; position:absolute; pointer-events:none; }
.charter-page__panel::before { inset:7px; border:1px solid rgba(166,69,46,.14); }
.charter-page__panel::after { top:0; right:36px; width:1px; height:100%; background:rgba(166,69,46,.08); }
.charter-page__masthead { position:relative; z-index:1; display:grid; grid-template-columns:auto minmax(0,1fr) auto; gap:20px; align-items:center; }
.charter-page__seal { display:grid; width:58px; height:58px; place-items:center; border:2px solid var(--ws-primary-color); outline:1px solid rgba(166,69,46,.36); outline-offset:-6px; color:var(--ws-primary-color); background:rgba(255,252,246,.55); font:600 27px/1 var(--ws-font-display); }
.charter-page__title { min-width:0; }
.charter-page__eyebrow { display:block; margin-bottom:5px; color:var(--ws-primary-color); font-size:10px; font-weight:700; letter-spacing:.18em; }
.charter-page h2 { margin:0 0 6px; font:600 clamp(29px,3.3vw,38px)/1.2 var(--ws-font-display); letter-spacing:.08em; }
.charter-page__title p { margin:0; color:var(--ws-text-secondary-color); font-size:13px; }
.charter-page__edition { display:grid; justify-items:end; gap:6px; text-align:right; }
.charter-page__state { display:inline-flex; align-items:center; gap:7px; padding:4px 8px; border:1px solid rgba(74,124,89,.45); color:var(--ws-success-color); background:rgba(255,255,255,.36); font-size:11px; font-weight:600; letter-spacing:.04em; }
.charter-page__state::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor; }
.charter-page__state.is-draft { border-style:dashed; border-color:rgba(184,134,43,.55); color:var(--ws-warning-color); }
.charter-page__state.is-history { border-color:var(--ws-border-color); color:var(--ws-text-secondary-color); }
.charter-page__version { color:var(--ws-text-secondary-color); font-size:11px; font-variant-numeric:tabular-nums; }
.charter-page__rule { position:relative; z-index:1; display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:10px; margin:26px 0 20px; color:rgba(166,69,46,.48); }
.charter-page__rule span { height:1px; background:linear-gradient(90deg,transparent,rgba(166,69,46,.34)); }.charter-page__rule span:last-child { transform:scaleX(-1); }.charter-page__rule i { font-style:normal; font-size:7px; }
.charter-page__actions { position:relative; z-index:1; display:flex; flex-wrap:wrap; gap:9px; margin:0 0 22px; }
.charter-page button { min-height:38px; padding:8px 14px; border:1px solid #cfc4b4; border-radius:4px; color:inherit; background:rgba(255,252,246,.7); font:inherit; cursor:pointer; transition:border-color .15s,background .15s,transform .15s; }
.charter-page button:hover:not(:disabled) { border-color:rgba(166,69,46,.6); background:#fffaf2; }.charter-page button:active:not(:disabled) { transform:translateY(1px); }.charter-page button:disabled { opacity:.5; cursor:default; }
.charter-page .charter-page__primary { border-color:var(--ws-primary-color); color:white; background:var(--ws-primary-color); }.charter-page .charter-page__primary:hover:not(:disabled) { background:var(--ws-primary-color-hover); }
.charter-page__notice,.charter-page__draft-note,.charter-page__error,.charter-page__loading { position:relative; z-index:1; padding:11px 13px; border-left:3px solid var(--ws-primary-color); background:rgba(255,252,246,.75); font-size:13px; line-height:1.7; }
.charter-page__notice { margin-bottom:16px; }.charter-page__notice a { margin-left:10px; }.charter-page__draft-note { margin:-4px 0 20px; border-left-color:var(--ws-warning-color); color:var(--ws-text-secondary-color); }.charter-page__draft-note strong { color:var(--ws-text-color); }
.charter-page__error { color:var(--ws-primary-color); }.charter-page__loading { color:var(--ws-text-secondary-color); }
.charter-page__folio { position:relative; z-index:1; display:grid; grid-template-columns:150px minmax(0,1fr); gap:22px; align-items:start; }
.charter-page__margin { display:grid; gap:22px; padding:19px 0; }
.charter-page__margin div { display:grid; gap:2px; padding-bottom:12px; border-bottom:1px solid rgba(104,107,102,.2); }
.charter-page__margin span { color:var(--ws-primary-color); font-size:9px; font-weight:700; letter-spacing:.18em; }.charter-page__margin strong { font:600 14px/1.5 var(--ws-font-display); }.charter-page__margin p { margin:0; color:var(--ws-text-secondary-color); font-size:11px; line-height:1.75; }
.charter-page__leaf { position:relative; min-width:0; min-height:560px; padding:25px clamp(28px,6vw,72px) 38px; border:1px solid #d6cab9; outline:1px solid rgba(166,69,46,.12); outline-offset:-8px; background:#fffdf8; box-shadow:0 12px 34px rgba(58,44,29,.12); }
.charter-page__leaf::before { content:""; position:absolute; inset:13px auto 13px 15px; width:2px; border-left:1px solid rgba(166,69,46,.22); border-right:1px solid rgba(166,69,46,.09); }
.charter-page__leaf-head { display:flex; justify-content:space-between; gap:16px; margin:0 0 36px; padding-bottom:10px; border-bottom:1px solid #d9d0c3; color:var(--ws-text-secondary-color); font-size:9px; font-weight:700; letter-spacing:.16em; }
.charter-page__leaf-head span:last-child { color:var(--ws-primary-color); }
.charter-page__leaf-foot { display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:12px; margin-top:48px; color:var(--ws-text-secondary-color); text-align:center; }.charter-page__leaf-foot span { height:1px; background:#ded5c8; }.charter-page__leaf-foot small { font-family:var(--ws-font-display); letter-spacing:.12em; }
.charter-page__empty { position:relative; z-index:1; display:grid; justify-items:center; gap:10px; min-height:390px; align-content:center; padding:44px 20px; border:1px solid #d6cab9; outline:1px solid rgba(166,69,46,.12); outline-offset:-8px; background:#fffdf8; text-align:center; }
.charter-page__empty > span { display:grid; width:56px; height:56px; place-items:center; margin-bottom:10px; border:1px solid var(--ws-primary-color); color:var(--ws-primary-color); font:600 25px var(--ws-font-display); }.charter-page__empty h3 { margin:0; font:600 22px var(--ws-font-display); letter-spacing:.04em; }.charter-page__empty p { max-width:460px; margin:0; color:var(--ws-text-secondary-color); font-size:13px; line-height:1.9; }
.charter-page a { color:var(--ws-primary-color); }
.charter-page__history { position:relative; z-index:1; margin-top:25px; border-top:1px solid rgba(104,107,102,.22); padding-top:17px; }.charter-page__history > summary { display:flex; align-items:center; justify-content:space-between; gap:12px; cursor:pointer; list-style:none; font-family:var(--ws-font-display); }.charter-page__history > summary::-webkit-details-marker { display:none; }.charter-page__history > summary::after { content:"＋"; color:var(--ws-primary-color); font-family:sans-serif; }.charter-page__history[open] > summary::after { content:"－"; }.charter-page__history summary small { color:var(--ws-text-secondary-color); font-family:inherit; font-size:11px; }.charter-page__history > div { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:8px; margin-top:14px; }.charter-page__history p { margin:0; }.charter-page__history a { display:grid; gap:1px; padding:10px 12px; border:1px solid rgba(104,107,102,.2); background:rgba(255,252,246,.55); }.charter-page__history a strong { color:var(--ws-text-color); font-size:12px; }.charter-page__history a span { color:var(--ws-text-secondary-color); font-size:10px; }
@media(max-width:760px) { .charter-page__panel { padding:24px 18px 22px; }.charter-page__panel::after { display:none; }.charter-page__masthead { grid-template-columns:auto minmax(0,1fr); }.charter-page__edition { grid-column:1/-1; justify-items:start; text-align:left; }.charter-page__folio { grid-template-columns:1fr; gap:12px; }.charter-page__margin { grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; padding:0; }.charter-page__margin div { padding:9px 0; }.charter-page__margin p { grid-column:1/-1; }.charter-page__leaf { min-height:480px; padding:22px 30px 32px; } }
@media(max-width:480px) { .charter-page { padding-bottom:24px; }.charter-page__panel { margin:0 -7px; padding:20px 14px; }.charter-page__masthead { gap:12px; }.charter-page__seal { width:46px; height:46px; font-size:22px; }.charter-page__eyebrow { font-size:8px; letter-spacing:.12em; }.charter-page h2 { font-size:28px; }.charter-page__title p { grid-column:1/-1; }.charter-page__actions > button { flex:1 1 auto; }.charter-page__margin { grid-template-columns:repeat(2,minmax(0,1fr)); }.charter-page__leaf { padding:19px 22px 27px; outline-offset:-5px; }.charter-page__leaf::before { display:none; }.charter-page__leaf-head { margin-bottom:27px; font-size:8px; }.charter-page__history > div { grid-template-columns:1fr; } }
</style>
