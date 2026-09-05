// Execute the real editor setup and Vue watchers with isolated in-memory APIs.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import MarkdownIt from 'markdown-it'
import * as helpers from '../src/shared/charterWorkspace.ts'
const source = await readFile(new URL('../src/components/conversation/CharterWorkspaceEditor.vue', import.meta.url), 'utf8')
const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'charter-markdown-test' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const copy = value => JSON.parse(JSON.stringify(value))
const defer = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const tick = async () => { await Vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }
const text = '\n# 我的章程\n\n## 原则\n\n我可以承认不知道。\n\n- 保留 **强调** 和空行。\n\n'
const workspace = (extra = {}) => ({ id: 'synthetic-workspace', conversationId: 'synthetic-conversation', status: 'active', scope: 'synthetic', revision: 1, baseVersion: 0, document: text, documentFormat: 'markdown', sourceText: '', clauses: [], suggestions: [], generation: 0, ...extra })
const storage = new Map()
const originalStorage = globalThis.sessionStorage
Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) } })
function setup(initial = workspace(), { clear = true, updateParent = true } = {}) {
  if (clear) storage.clear()
  const state = { workspace: copy(initial), formal: null, publishFailures: 0, saveWait: null, suggestWait: null, savedControlChanges: null }
  const calls = [], emits = [], cleanup = []
  const props = Vue.reactive({ workspace: copy(initial) })
  const request = async (path, method = 'GET', body = {}) => {
    calls.push({ path, method, body: copy(body) })
    if (method === 'GET') return { workspace: copy(state.workspace) }
    if (body.revision !== state.workspace.revision) throw new Error('工作稿版本冲突，请读取最新版本。')
    if (method === 'PUT') {
      assert.equal(typeof body.document, 'string'); assert.equal('clauses' in body, false); assert.equal('sourceText' in body, false)
      state.workspace = { ...state.workspace, document: body.document, documentFormat: 'markdown', revision: state.workspace.revision + 1,
        ...(state.savedControlChanges ? { controlChanges: copy(state.savedControlChanges) } : {}) }
      const response = { workspace: copy(state.workspace) }
      if (state.saveWait) await state.saveWait.promise
      return response
    }
    if (path.endsWith('/publish')) {
      assert.equal(body.publishDocument, true); assert.equal('selectedClauseIds' in body, false)
      if (state.publishFailures-- > 0) throw new Error('合成发布失败，草稿保留。')
      state.formal = { id: 'synthetic-formal', version: (state.formal?.version ?? 0) + 1, document: state.workspace.document }
      state.workspace = { ...state.workspace, status: 'published', revision: state.workspace.revision + 1 }
      return { workspace: copy(state.workspace), charter: copy(state.formal) }
    }
    if (path.endsWith('/merge')) {
      const selected = state.workspace.suggestions.find(s => s.id === body.suggestionId)
      state.workspace = { ...state.workspace, document: selected.document, revision: state.workspace.revision + 1, suggestions: state.workspace.suggestions.map(s => s.id === selected.id ? { ...s, status: 'merged' } : s) }
      return { workspace: copy(state.workspace) }
    }
    if (path.endsWith('/pause')) { state.workspace = { ...state.workspace, status: 'paused', revision: state.workspace.revision + 1 }; return { workspace: copy(state.workspace) } }
    throw new Error('Unmocked API: ' + path)
  }
  const routed = async (cid, path, body, signal) => {
    calls.push({ path, method: 'TASK', body: copy(body) })
    state.workspace = { ...state.workspace, revision: state.workspace.revision + 1, suggestions: [{ id: 'synthetic-suggestion', sourceRevision: state.workspace.revision, document: '# 建议正文\n\n可以直接改写这一篇。\n', clauses: [], status: 'pending' }] }
    const response = { workspace: copy(state.workspace) }
    if (state.suggestWait) await state.suggestWait.promise
    if (signal?.aborted) throw new DOMException('已取消', 'AbortError')
    return response
  }
  const exports = {}
  new Function('require', 'exports', code)(id => {
    if (id === 'vue') return { ...Vue, onBeforeUnmount: fn => cleanup.push(fn) }
    if (id.includes('taskRouting')) return { routingRequest: request, routedTask: routed }
    if (id.includes('charterWorkspace')) return helpers
    if (id.endsWith('.vue')) return { default: {} }
    throw new Error('Unmocked import: ' + id)
  }, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup(props, { expose() {}, emit: (event, value) => { emits.push([event, value]); if (event === 'updated' && updateParent) props.workspace = value } }))
  return { state, props, calls, emits, ui, close() { cleanup.forEach(fn => fn()); scope.stop() } }
}

try {
  // Mount/preview are read-only. Saving and publishing are distinct operations.
  {
    const h = setup(); assert.equal(h.calls.length, 0); assert.equal(h.ui.markdown.value, text)
    h.ui.view.value = 'preview'; h.ui.view.value = 'edit'; assert.equal(h.ui.markdown.value, text)
    const edited = text + '这是我补的一行。\n'
    h.ui.markdown.value = edited; await h.ui.save(); await tick()
    assert.equal(h.state.workspace.document, edited); assert.equal(h.state.formal, null)
    assert.equal(h.calls.length, 1); assert.equal(h.calls[0].method, 'PUT')
    assert.match(h.ui.notice.value, /尚未生效/)
    await h.ui.publish()
    assert.equal(h.state.formal.document, edited); assert.equal(h.state.formal.version, 1)
    assert.equal(h.calls.at(-1).body.revision, 2)
    assert.equal('confirmControlChanges' in h.calls.at(-1).body, false, 'ordinary text has no extra rule confirmation')
    assert.equal(h.emits.filter(([e]) => e === 'published').length, 1)
    h.close()
  }
  // Empty canonical Markdown never revives old fields or produces an empty version.
  {
    const h = setup(workspace({ document: '', sourceText: '不能自动塞回正文' }))
    assert.equal(h.ui.markdown.value, ''); await h.ui.publish()
    assert.match(h.ui.error.value, /空/); assert.equal(h.calls.length, 0)
    assert.equal(h.ui.oldSource.value, '不能自动塞回正文')
    h.ui.appendOldSource(); assert.match(h.ui.markdown.value, /不能自动塞回正文/)
    assert.equal(h.state.formal, null); h.close()
  }
  // Failure retains input, and retry reuses the same publish request ID.
  {
    const h = setup(); h.ui.markdown.value += '修改仍应保留。\n'; h.state.publishFailures = 1
    await h.ui.publish(); const edited = h.ui.markdown.value
    assert.match(h.ui.error.value, /合成发布失败/); assert.equal(h.state.formal, null)
    const first = h.calls.find(c => c.path.endsWith('/publish'))
    await h.ui.publish()
    const second = h.calls.filter(c => c.path.endsWith('/publish'))[1]
    assert.equal(first.body.requestId, second.body.requestId)
    assert.equal(h.state.formal.document, edited); h.close()
  }
  // A remote revision cannot overwrite typing or be silently adopted on save.
  {
    const h = setup(); const mine = text + '我的未保存正文。\n'; h.ui.markdown.value = mine
    h.state.workspace = workspace({ revision: 2, document: '# 另一窗口\n新版正文。\n' })
    h.props.workspace = copy(h.state.workspace); await tick()
    assert.equal(h.ui.markdown.value, mine); assert.equal(h.ui.outdated.value, true)
    await h.ui.save(); assert.equal(h.calls.length, 0); assert.match(h.ui.error.value, /核对新版本/)
    h.ui.keepLocal(); await h.ui.save()
    assert.equal(h.calls[0].body.revision, 2); assert.equal(h.state.workspace.document, mine)
    h.close()
  }
  // Late older prop snapshots cannot regress newer clean content.
  {
    const h = setup(workspace({ revision: 5 })); h.props.workspace = workspace({ revision: 4, document: '过期文字' }); await tick()
    assert.equal(h.ui.markdown.value, text); assert.equal(h.ui.base.value.revision, 5); h.close()
  }
  // A 409 with stale parent props has an explicit read/reconcile path, not an endless retry.
  {
    const h = setup(); const mine = text + '我的修订不丢。\n'; h.ui.markdown.value = mine
    h.state.workspace = workspace({ revision: 2, document: '# 另一窗口刚保存\n' })
    await h.ui.save(); assert.match(h.ui.error.value, /版本冲突/)
    assert.equal(h.ui.outdated.value, false, 'parent has not learned the new revision yet')
    await h.ui.refreshLatest(); await tick()
    assert.equal(h.ui.outdated.value, true); assert.equal(h.ui.markdown.value, mine)
    h.ui.keepLocal(); await h.ui.save()
    assert.equal(h.state.workspace.document, mine); assert.equal(h.calls.at(-1).body.revision, 2); h.close()
  }
  // Typing while generation is pending stays editable; adoption is explicit.
  {
    const h = setup(); const wait = defer(); h.state.suggestWait = wait
    const task = h.ui.generate(); await tick()
    const mine = text + '等待时继续写。\n'; h.ui.markdown.value = mine
    wait.resolve(); await task; await tick()
    assert.equal(h.ui.markdown.value, mine); assert.equal(h.ui.pending.value.length, 1)
    assert.equal(h.ui.base.value.revision, 2, 'metadata-only suggestion can safely advance revision')
    await h.ui.merge('synthetic-suggestion'); assert.match(h.ui.error.value, /先保存/)
    await h.ui.save(); await h.ui.merge('synthetic-suggestion')
    assert.match(h.ui.markdown.value, /# 建议正文/); assert.equal(h.state.formal, null)
    assert.equal(h.ui.view.value, 'preview'); h.close()
  }
  // No stale publication if input changes while the pre-publish save is in flight.
  {
    const h = setup(); const wait = defer(); h.state.saveWait = wait
    h.ui.markdown.value += '准备保存。\n'; const task = h.ui.publish(); await tick()
    const mine = h.ui.markdown.value + '保存尚未返回时又改了。\n'; h.ui.markdown.value = mine
    wait.resolve(); await task
    assert.equal(h.ui.markdown.value, mine); assert.equal(h.state.formal, null)
    assert.equal(h.calls.some(c => c.path.endsWith('/publish')), false); assert.match(h.ui.error.value, /保存期间/)
    h.close()
  }
  // Older save receipts are ignored relative to both local and parent revisions;
  // they must not emit an update that could regress the parent page/drawer.
  for (const remoteText of [text, '# 较新的其他正文\n']) {
    const h = setup(); h.state.saveWait = defer(); const mine = text + '正在保存的旧请求。\n'
    h.ui.markdown.value = mine; const task = h.ui.save(); await tick()
    h.props.workspace = workspace({ revision: 5, document: remoteText }); await tick()
    const baseRevision = h.ui.base.value.revision
    h.state.saveWait.resolve(); await task; await tick()
    assert.equal(h.ui.markdown.value, mine); assert.equal(h.ui.base.value.revision, baseRevision)
    assert.equal(h.props.workspace.revision, 5)
    assert.equal(h.emits.filter(([event]) => event === 'updated').length, 0)
    h.close()
  }
  // An old generated suggestion arriving after newer server text cannot replace
  // that text or revive obsolete suggestions.
  {
    const h = setup(); h.state.suggestWait = defer(); const task = h.ui.generate(); await tick()
    const latest = '# 服务器较新正文\n'; h.props.workspace = workspace({ revision: 7, document: latest }); await tick()
    h.state.suggestWait.resolve(); await task; await tick()
    assert.equal(h.ui.base.value.revision, 7); assert.equal(h.ui.markdown.value, latest)
    assert.equal(h.ui.pending.value.length, 0); assert.equal(h.emits.filter(([event]) => event === 'updated').length, 0)
    assert.match(h.ui.notice.value, /没有覆盖/); h.close()
  }
  // Only changing an existing automatic rule requires an explicit second step.
  // The first click saves the draft and shows consequences, but cannot publish.
  {
    const h = setup(); h.ui.markdown.value += '我重新表述这一条边界。\n'
    h.state.savedControlChanges = [{ id: 'old-rule', text: '默认仅在本机处理', control: 'local_only' }]
    await h.ui.publish(); await tick()
    assert.equal(h.calls.length, 1); assert.equal(h.calls[0].method, 'PUT'); assert.equal(h.state.formal, null)
    assert.equal(h.ui.controlChanges.value.length, 1); assert.match(h.ui.notice.value, /自动执行约定/)
    await h.ui.publish(true)
    assert.equal(h.calls.filter(c => c.path.endsWith('/publish')).length, 1)
    assert.equal(h.calls.at(-1).body.confirmControlChanges, true)
    assert.equal(h.state.formal.document, h.ui.markdown.value)
    h.close()
  }
  // A session draft survives remount, including a concurrent server revision.
  {
    let h = setup(); h.ui.markdown.value += '刷新前未保存。\n'; const mine = h.ui.markdown.value; await tick(); h.close()
    h = setup(workspace({ revision: 3, document: '服务器新版。' }), { clear: false })
    assert.equal(h.ui.markdown.value, mine); assert.equal(h.ui.outdated.value, true)
    h.ui.discardLocal(); assert.equal(h.ui.markdown.value, '服务器新版。'); h.close()
  }
  // Legacy original text is kept separate, not silently concatenated/published.
  {
    const h = setup(workspace({ documentFormat: undefined, sourceText: '原始想法\n不能丢失' }))
    assert.equal(h.ui.markdown.value, text); assert.equal(h.ui.oldSource.value, '原始想法\n不能丢失')
    h.ui.appendOldSource(); assert.ok(h.ui.markdown.value.startsWith(text)); assert.match(h.ui.markdown.value, /原始想法\n不能丢失/)
    assert.equal(h.calls.length, 0); h.close()
  }
  // Another window publishing/pausing A must not relabel unsaved B as confirmed,
  // clear its recovery buffer, or let "keep mine" blindly reopen a closed draft.
  for (const status of ['published', 'paused']) {
    let h = setup(); const mine = text + '这句B从未被确认，必须保留。\n'; h.ui.markdown.value = mine; await tick()
    const terminal = workspace({ status, revision: 2 })
    h.state.workspace = copy(terminal); h.props.workspace = copy(terminal); await tick()
    assert.equal(h.ui.markdown.value, mine)
    assert.equal(h.ui.base.value.status, 'active', 'unsaved B must not appear under the confirmed/paused A heading')
    assert.equal(h.ui.outdated.value, true)
    assert.equal(JSON.parse(storage.get('zhijun-charter-markdown:synthetic-workspace')).markdown, mine)
    h.ui.keepLocal(); await h.ui.save()
    assert.equal(h.calls.length, 0, 'closed server draft cannot silently become writable')
    h.close()
    h = setup(terminal, { clear: false })
    assert.equal(h.ui.markdown.value, mine, 'refresh must still offer the unsubmitted document')
    assert.notEqual(h.ui.base.value.status, 'published', 'recovered text is not the published document')
    assert.equal(JSON.parse(storage.get('zhijun-charter-markdown:synthetic-workspace')).markdown, mine)
    h.close()
  }
  // Former multi-field unsaved edits are recoverable without invoking a model.
  {
    storage.clear(); storage.set('zhijun-charter-buffer:synthetic-workspace', JSON.stringify({ baseRevision: 1, baseClauses: [], baseSourceText: '', sourceText: '独立原始想法', clauses: [{ id: 'old', section: '旧的自由章节', text: '旧编辑器里还没有保存的句子。', scope: 'global', sources: [] }] }))
    const h = setup(workspace({ documentFormat: undefined, document: '' }), { clear: false })
    assert.match(h.ui.markdown.value, /旧编辑器里还没有保存/); assert.equal(h.ui.oldSource.value, '独立原始想法'); assert.equal(h.calls.length, 0); h.close()
  }
  assert.match(source, /:readonly="!!busy && busy !== 'suggest'"/)
  assert.equal((source.match(/<textarea\b/g) ?? []).length, 1)
  assert.doesNotMatch(source, /<input\b|<select\b|selectedClauseIds/)
  assert.match(source, /确认正文与规则变更/)
  assert.match(source, /publish\(controlChanges.length > 0 && !dirty\)/)
  assert.match(source, /正文用于指导知君，保存草稿不会生效。确认章程不确认本体，也不代替资料外发授权。/)
  console.log('charter Markdown: real SFC read-only mount, exact document, save/publish, idempotent retry, revisions, delayed generation/save, remount and legacy recovery passed')
} finally { Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: originalStorage }) }

// The real page keeps conversation generation primary, but a read/preview never starts it.
const pageSource = await readFile(new URL('../src/pages/CharterPage.vue', import.meta.url), 'utf8')
const pageCode = ts.transpileModule(compileScript(parse(pageSource).descriptor, { id: 'charter-page-test' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function pageSetup(initial = {}) {
  const calls = [], pushed = []
  const state = { currentCharter: null, workspace: null, versions: [], failStart: false, ...initial }
  const route = Vue.reactive({ path: '/me/charter', query: {} })
  const mocks = {
    api: { getGrowthCharter: async () => { calls.push('read'); return copy(state) } },
    createConversation: async payload => { calls.push(['create', copy(payload)]); return { id: 'synthetic-conversation' } },
    routingRequest: async (path, method, body) => {
      calls.push(['start', path, method, copy(body)])
      if (state.failStart) { state.failStart = false; throw new Error('合成暂时失败') }
      state.workspace ||= workspace(); return { workspace: copy(state.workspace), conversationId: state.workspace.conversationId }
    },
  }
  const exports = {}
  new Function('require', 'exports', pageCode)(id => {
    if (id === 'vue') return { ...Vue, onMounted() {} }
    if (id === 'vue-router') return { useRoute: () => route, useRouter: () => ({ push: async target => pushed.push(copy(target)) }) }
    if (id.includes('services/api')) return mocks
    if (id.includes('taskRouting')) return mocks
    if (id.includes('charterWorkspace')) return helpers
    if (id.includes('format')) return { formatDate: value => value }
    if (id.endsWith('.vue')) return { default: {} }
    throw new Error('Unmocked page import: ' + id)
  }, exports)
  const scope = Vue.effectScope(), ui = scope.run(() => exports.default.setup({}, { expose() {} }))
  return { ui, state, calls, pushed, route, close: () => scope.stop() }
}
{
  const h = pageSetup(); await h.ui.load()
  assert.deepEqual(h.calls, ['read']); assert.equal(h.state.workspace, null)
  h.ui.draftChanged(true); await h.ui.start('chat')
  assert.equal(h.calls.length, 1); assert.equal(h.pushed.length, 0); assert.match(h.ui.error.value, /请先保存/)
  h.ui.draftChanged(false); h.state.failStart = true; await h.ui.start('chat'); await h.ui.start('chat')
  assert.equal(h.calls.filter(c => Array.isArray(c) && c[0] === 'create').length, 1, 'retry does not create another conversation')
  const starts = h.calls.filter(c => Array.isArray(c) && c[0] === 'start')
  assert.equal(starts[0][3].requestId, starts[1][3].requestId)
  assert.equal(h.pushed.length, 1); assert.match(h.pushed[0].query.say, /Markdown/); assert.equal(h.pushed[0].path, '/c/synthetic-conversation')
  assert.equal(h.state.currentCharter, null); h.close()
}
{
  const legacy = { id: 'legacy', version: 1, createdAt: '', vision: '从容\n保留换行', roles: ['家长'], goals: ['做有用的东西'], principles: ['诚实'], boundaries: ['先问我'], challengeStyle: '一次只问一个问题', quietDomains: ['暂不触碰'] }
  const h = pageSetup({ currentCharter: legacy, versions: [legacy] }); await h.ui.load()
  assert.equal(h.ui.readingDocument.value, helpers.charterDocument(legacy))
  for (const fragment of ['从容\n保留换行', '家长', '做有用的东西', '诚实', '先问我', '一次只问一个问题', '暂不触碰']) assert.ok(h.ui.readingDocument.value.includes(fragment))
  assert.deepEqual(h.calls, ['read']); assert.deepEqual(h.state.currentCharter, legacy)
  h.route.query = { version: '1' }; await tick()
  assert.equal(h.ui.versionMode.value, true); assert.deepEqual(h.calls, ['read']); h.close()
}
{
  const current = { id: 'formal-5', version: 5, document: text }
  const h = pageSetup({ workspace: workspace({ revision: 8 }), currentCharter: current, versions: [current] }); await h.ui.load()
  h.ui.updated(workspace({ revision: 6, document: '过时回执' }))
  assert.equal(h.ui.workspace.value.revision, 8); assert.equal(h.ui.workspace.value.document, text)
  h.ui.published({ id: 'formal-4', version: 4, document: '旧版正文' })
  assert.equal(h.ui.charter.value.version, 5); assert.equal(h.ui.charter.value.document, text)
  h.close()
}
assert.match(pageSource, /通过对话生成/)
assert.match(pageSource, /通过对话修改/)
assert.match(pageSource, /class="charter-page__primary"[^>]+start\('chat'\)/)
console.log('charter Markdown page: read-only loading/history, legacy fields preserved, explicit conversation entry, dirty protection and retry deduplication passed')

// The conversation drawer applies the same monotonic update rules as the page.
const chatSource = await readFile(new URL('../src/components/conversation/CharterConversation.vue', import.meta.url), 'utf8')
const chatCode = ts.transpileModule(compileScript(parse(chatSource).descriptor, { id: 'charter-conversation-test' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
{
  const cleanup = [], exports = {}
  const current = { id: 'formal-5', version: 5, document: text }
  new Function('require', 'exports', chatCode)(id => {
    if (id === 'vue') return { ...Vue, onBeforeUnmount: fn => cleanup.push(fn) }
    if (id === 'vue-router') return { useRouter: () => ({ push: () => {} }) }
    if (id.includes('taskRouting')) return { routingRequest: async () => ({ workspace: workspace({ revision: 8 }), charter: current, topics: [] }) }
    if (id.includes('services/api')) return { reviewClaim: () => { throw new Error('This read-only test must not confirm ontology') } }
    if (id.endsWith('.vue')) return { default: {} }
    throw new Error('Unmocked conversation import: ' + id)
  }, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup(Vue.reactive({ conversationId: 'synthetic-conversation', onboarding: false, claims: [] }), { expose() {}, emit() {} }))
  await tick()
  assert.equal(ui.open.value, false)
  ui.updated(workspace({ revision: 6, document: '过时工作稿' }))
  ui.published({ id: 'formal-4', version: 4, document: '旧的正式正文' })
  assert.equal(ui.state.value.workspace.revision, 8); assert.equal(ui.state.value.workspace.document, text)
  assert.equal(ui.state.value.charter.version, 5); assert.equal(ui.state.value.charter.document, text)
  cleanup.forEach(fn => fn()); scope.stop()
}
console.log('charter Markdown monotonic updates: old page/drawer saves and publications cannot replace newer versions')

// Preview uses the real renderer: HTML is escaped and Markdown images cannot
// initiate remote fetches merely by opening a private charter document.
const documentSource = await readFile(new URL('../src/components/conversation/CharterDocument.vue', import.meta.url), 'utf8')
const documentCode = ts.transpileModule(compileScript(parse(documentSource).descriptor, { id: 'charter-document-test' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const documentExports = {}
new Function('require', 'exports', documentCode)(id => id === 'vue' ? Vue : id === 'markdown-it' ? { default: MarkdownIt } : helpers, documentExports)
const inputDocument = '# 标题\n\n**强调**\n\n<script>alert(1)</script>\n\n![替代文字](https://example.invalid/private-pixel.png)\n\n[危险链接](javascript:alert(1))'
const rendered = documentExports.default.setup({ document: inputDocument }, { expose() {} }).rendered.value
assert.match(rendered, /<h1>标题<\/h1>/); assert.match(rendered, /<strong>强调<\/strong>/)
assert.doesNotMatch(rendered, /<img\b|<script\b|href="javascript:/)
assert.match(rendered, /&lt;script&gt;/)
console.log('charter Markdown preview: renders headings/emphasis, escapes HTML and never loads embedded remote images')
