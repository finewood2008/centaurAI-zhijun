import { createApp, defineComponent, h, ref } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import type { CharterClause, CharterWorkspace, GrowthCharter } from '../../src/services/api'
import CharterPage from '../../src/pages/CharterPage.vue'
import CharterWorkspaceEditor from '../../src/components/conversation/CharterWorkspaceEditor.vue'
import CharterConversation from '../../src/components/conversation/CharterConversation.vue'
import { charterDocument } from '../../src/shared/charterWorkspace'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'

// Every fetch is answered in memory. Never forward unknown requests to the live
// backend/model; even the conversation below is explicitly synthetic.
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const sample = '# 我的人生章程\n\n## 我希望守住的事\n\n我重视诚实，也允许自己暂时不知道答案。\n\n## 我想走的方向\n\n- 让产品真正帮助人，而不是只追求更多功能。\n- 每周留出时间陪家人；这是愿望，不是已经做到的事实。\n\n## 我希望知君如何与我合作\n\n先听我说，再问一个具体问题。未写到的边界仍待明确。\n'
const generated = sample + '\n## 面对选择\n\n把事实、愿望和不确定的部分分开，让我自己决定。\n'
const now = '2026-09-05T12:00:00Z'
const log = ref<string[]>([])
const held = ref<Array<() => void>>([])
const viewKey = ref(0)
const visibleWorkspace = ref<CharterWorkspace | null>(null)
let workspace: CharterWorkspace | null = null
let formal: GrowthCharter | null = null
let versions: GrowthCharter[] = []
let nextSuggestDelay = false, nextSaveDelay = false, nextPublishFailure = false
let count = 0
const documentClauses = (document: string): CharterClause[] => document.trim() ? [{ id: 'fixture-clause', section: '我的章程', text: document, kind: 'principle', scope: 'global', control: null, sources: [], origin: 'manual' }] : []
function newWorkspace(document = ''): CharterWorkspace {
  return { id: 'fixture-charter-' + (++count), conversationId: 'fixture-charter-conversation', scope: 'fixture', status: 'active', revision: 1, generation: 0, baseVersion: formal?.version ?? 0, baseClauseIds: formal?.clauses?.map(c => c.id) ?? [], deletedClauseIds: [], sourceText: '', document, clauses: documentClauses(document), suggestions: [], createdAt: now, updatedAt: now }
}
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
const failure = (detail: string, status = 409) => json({ detail: { code: status === 409 ? 'CHARTER_WORKSPACE_CHANGED' : 'FIXTURE_FAILURE', detail } }, status)
const activeWorkspace = () => workspace?.status === 'published' ? null : workspace
function history() { return { currentCharter: formal, versions, workspace: activeWorkspace() } }
function mutateDocument(document: string) {
  if (!workspace) return
  workspace = { ...workspace, document, documentFormat: 'markdown', clauses: documentClauses(document), revision: workspace.revision + 1, updatedAt: now }
}
const replies = new Map<string, unknown>()
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin)
  const method = init?.method || 'GET'
  if (url.origin !== location.origin || !url.pathname.startsWith('/api/mindos/')) return failure('隔离测试拒绝未模拟请求', 400)
  const path = url.pathname.slice('/api/mindos'.length)
  const body = init?.body ? JSON.parse(String(init.body)) : {}
  log.value.unshift(method + ' ' + path + (body.previewOnly ? '（仅预览，不生成）' : ''))
  if (method === 'GET' && path === '/growth/charter') return json(history())
  if (method === 'POST' && path === '/conversations') return json({ id: 'fixture-charter-conversation', title: body.title, mode: 'chat', status: 'active', createdAt: now, updatedAt: now })
  const route = path.match(/^\/conversations\/fixture-charter-conversation\/charter(?:\/workspace(?:\/([^/]+)(?:\/(suggest|merge|publish|pause))?)?)?$/)
  if (!route) return failure('隔离测试拒绝未模拟请求：' + path, 400)
  if (path.endsWith('/charter') && method === 'GET') return json({ ...history(), charter: formal, topics: [], pending: [], generationState: 'complete' })
  if (route[1] === 'start' && method === 'POST') {
    if (!workspace || workspace.status === 'published') workspace = newWorkspace(formal ? charterDocument(formal) : '')
    else if (workspace.status === 'paused') workspace = { ...workspace, status: 'active', revision: workspace.revision + 1 }
    return json({ workspace, conversationId: workspace.conversationId })
  }
  if (!workspace || route[1] !== workspace.id) return failure('工作稿不存在', 404)
  if (route[2] === 'suggest' && body.previewOnly) return json({ routePreview: { revision: 'fixture-preview-' + workspace.revision, conversationId: workspace.conversationId, service: { id: 'fixture-local', name: '合成模型（没有外部调用）', model: 'fixture', external: false }, purpose: 'charter_draft', purposeLabel: '人生章程整理', missing: [], blocked: [], sources: [], excluded: [], request: { system: '', messages: [] } } })
  const dedupKey = [path, body.requestId].join(':')
  if (body.requestId && replies.has(dedupKey)) return json(replies.get(dedupKey))
  if (route[2] !== 'suggest' && body.revision !== workspace.revision) return failure('工作稿已被另一处更新，你的文字仍保留，请核对最新版本。')
  if (method === 'PUT' && !route[2]) {
    if (typeof body.document !== 'string') return failure('本版只保存完整 Markdown 正文', 422)
    mutateDocument(body.document)
  } else if (route[2] === 'suggest') {
    const sourceRevision = workspace.revision
    workspace = { ...workspace, revision: workspace.revision + 1, generation: workspace.generation + 1, suggestions: [...workspace.suggestions, { id: 'fixture-suggestion-' + workspace.generation, sourceRevision, document: generated, clauses: documentClauses(generated), status: 'pending', createdAt: now } as CharterWorkspace['suggestions'][number]] }
  } else if (route[2] === 'merge') {
    const suggestion = workspace.suggestions.find(s => s.id === body.suggestionId)
    if (!suggestion) return failure('整理建议不存在', 404)
    mutateDocument((suggestion as typeof suggestion & { document?: string }).document || generated)
    workspace!.suggestions = workspace!.suggestions.map(s => s.id === suggestion.id ? { ...s, status: 'merged' } : s)
  } else if (route[2] === 'publish') {
    if (nextPublishFailure) { nextPublishFailure = false; return failure('合成发布失败；正文和草稿均保留，可重试。', 503) }
    if (body.publishDocument !== true || !workspace.document.trim()) return failure('需要明确确认非空正文', 422)
    const version = (formal?.version ?? 0) + 1
    formal = { id: 'fixture-formal-' + version, version, createdAt: now, document: workspace.document, clauses: copy(workspace.clauses), vision: '', roles: [], principles: [], boundaries: [], goals: [], challengeStyle: '', quietDomains: [] }
    versions = [formal, ...versions]
    workspace = { ...workspace, status: 'published', revision: workspace.revision + 1 }
  } else if (route[2] === 'pause') workspace = { ...workspace, status: 'paused', revision: workspace.revision + 1 }
  else return failure('隔离测试拒绝未模拟请求', 400)
  const result = copy({ workspace, ...(route[2] === 'publish' ? { charter: formal } : {}) })
  if (body.requestId) replies.set(dedupKey, result)
  if ((route[2] === 'suggest' && nextSuggestDelay) || (method === 'PUT' && nextSaveDelay)) {
    if (route[2] === 'suggest') nextSuggestDelay = false
    else nextSaveDelay = false
    await new Promise<void>(resolve => held.value.push(resolve))
  }
  if (init?.signal?.aborted) throw new DOMException('已取消合成请求', 'AbortError')
  return json(result)
}

const DirectEditor = defineComponent({ setup() { return () => visibleWorkspace.value ? h(CharterWorkspaceEditor, { workspace: visibleWorkspace.value, onUpdated: (value: CharterWorkspace) => { visibleWorkspace.value = value } }) : h('p', '尚未主动开始工作稿。') } })
const SyntheticChat = defineComponent({ setup() {
  const message = ref(''), messageId = ref('fixture-message-1')
  return () => h('section', { class: 'fixture-chat' }, [
    h('h2', '对话入口 · 合成测试'),
    h('p', '现在最希望知君与你合作时注意什么？这里只演示工作稿衔接，不调用模型。'),
    h('textarea', { 'aria-label': '合成对话输入', value: message.value, rows: 3, onInput: (event: Event) => { message.value = (event.target as HTMLTextAreaElement).value } }),
    h('button', { onClick: () => { mutateDocument(generated); messageId.value = 'fixture-message-' + Date.now(); message.value = '' } }, '模拟对话整理完成'),
    h(CharterConversation, { conversationId: 'fixture-charter-conversation', onboarding: false, requested: true, claims: [], messageId: messageId.value }),
    h('button', { onClick: () => router.push('/me/charter') }, '回到人生章程'),
  ])
} })
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/me/charter', component: CharterPage }, { path: '/me', redirect: '/me/charter' }, { path: '/editor', component: DirectEditor }, { path: '/c/:id', component: SyntheticChat }] })
async function reset(kind: 'empty' | 'editor' | 'formal' | 'legacy') {
  held.value.splice(0).forEach(release => release())
  nextSuggestDelay = false; nextSaveDelay = false; nextPublishFailure = false
  workspace = null; formal = null; versions = []; replies.clear(); visibleWorkspace.value = null; log.value = []
  if (kind === 'formal' || kind === 'legacy') {
    formal = { id: 'fixture-original', version: 1, createdAt: now, document: kind === 'formal' ? sample : '', clauses: [], vision: '希望更从容\n保留这一行换行', roles: ['家长', '产品负责人'], principles: ['诚实', '不把不确定说成确定'], boundaries: ['没有授权时不发送资料'], goals: ['做有用的产品'], challengeStyle: '一次只问一个具体问题', quietDomains: ['暂不讨论身体健康'] }
    versions = [copy(formal)]
  }
  if (kind === 'editor') { workspace = newWorkspace(sample); visibleWorkspace.value = copy(workspace) }
  viewKey.value += 1
  await router.replace(kind === 'editor' ? '/editor' : '/me/charter')
}
const Root = defineComponent({ setup() { return () => h('main', { class: 'fixture' }, [
  h('p', { class: 'fixture-note' }, '隔离测试 · 全部接口在当前页面模拟，不修改真实章程，不联系模型。'),
  h(RouterView, { key: viewKey.value }),
  h('details', { class: 'fixture-controls' }, [h('summary', '测试场景与请求记录'), h('div', { class: 'fixture-actions' }, [
    ...([['empty', '未开始'], ['editor', '工作稿编辑'], ['formal', '已有 Markdown'], ['legacy', '旧版内容']] as const).map(([kind, label]) => h('button', { onClick: () => reset(kind) }, label)),
    h('button', { onClick: () => { nextSuggestDelay = true } }, '下一次整理延迟'),
    h('button', { onClick: () => { nextSaveDelay = true } }, '下一次保存延迟'),
    h('button', { onClick: () => { nextPublishFailure = true } }, '下一次发布失败'),
    h('button', { onClick: () => { mutateDocument(sample + '\n其他窗口增加的新内容。\n'); if (workspace) visibleWorkspace.value = copy(workspace); log.value.unshift('模拟另一窗口修订（正文不应覆盖正在输入的文字）') } }, '模拟另一窗口修改'),
    h('button', { onClick: () => held.value.splice(0).forEach(resolve => resolve()) }, `释放响应（${held.value.length}）`),
  ]), h('p', `正式版本：${formal?.version ?? 0} · 只有“确认并生效”才应增加版本。`), h('pre', log.value.join('\n'))]),
]) } })
const style = document.createElement('style')
style.textContent = '.fixture{max-width:1000px;margin:24px auto;padding:0 20px;font:14px/1.7 sans-serif;box-sizing:border-box}.fixture-note{font-size:12px;color:#777}.fixture-controls{border-top:1px solid #ddd;margin-top:26px;padding:16px 0;font-size:12px}.fixture-actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.fixture pre{white-space:pre-wrap;overflow-wrap:anywhere}.fixture button{font:inherit;cursor:pointer}.fixture-chat textarea{box-sizing:border-box;width:100%;font:inherit}.fixture-chat button{margin:8px}.fixture .page{padding:0}@media(max-width:520px){.fixture{margin:14px auto;padding:0 12px}}'
document.head.append(style)
await router.push('/me/charter')
createApp(Root).use(router).mount('#app')
