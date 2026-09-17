// Run the actual page functions with synthetic stores; no network or model call.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { createSessionGate } from '../src/composables/sessionGate.ts'
const source = await readFile(new URL('../src/pages/ConversationPage.vue', import.meta.url), 'utf8')
const script = parse(source).descriptor.scriptSetup.content
const ast = ts.createSourceFile('conversation.ts', script, ts.ScriptTarget.Latest, true)
const fn = name => ast.statements.find(n => ts.isFunctionDeclaration(n) && n.name?.text === name).getText(ast)
const code = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const execute = (text, bindings = {}) => new Function(...Object.keys(bindings), code(text))(...Object.values(bindings))
{
  const currentId = { value: 'selected-conversation' }, calls = []
  const load = execute(`${fn('loadPageSupportingData')}\nreturn loadPageSupportingData`, {
    currentId, loadStatus: async () => calls.push('status'), loadStats: async () => calls.push('stats'),
  })
  await load()
  assert.deepEqual(calls, ['status'], 'opening a conversation does not submit landing-only ontology statistics')
  currentId.value = null; calls.length = 0
  await load()
  assert.deepEqual(calls, ['status', 'stats'], 'the blank page still loads its required statistics')
}
const starterStatement = ast.statements.find(n => ts.isVariableStatement(n) && n.declarationList.declarations.some(d => d.name.getText(ast) === 'STARTERS')).getText(ast)
{
  let input = '手写草稿', deliberate = false, focus = 0, replacements = 0
  const composerRef = { value: { setDeliberate(value) { deliberate = value }, replaceText(value) { input = value; focus++; replacements++ } } }
  const page = execute(`${starterStatement}\n${fn('useStarter')}\nreturn { STARTERS, useStarter }`, { composerRef })
  const expected = ['我在考虑一件事：','我想准备一次重要沟通：','最近发生了一件事，','基于你目前对我的认识，说说你眼中的我，哪些地方你其实不确定？']
  for (const [index, starter] of page.STARTERS.entries()) {
    page.useStarter(starter); assert.equal(input, expected[index]); assert.equal(deliberate, index === 0)
    page.useStarter(starter); assert.equal(input, expected[index], 'repeated card click replaces, never appends')
  }
  assert.equal(focus, 8); assert.equal(replacements, 8)
}
const defer = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
{
  const pending = new Map(), requests = [], auxiliary = []
  const state = Object.fromEntries(['messagesLoading', 'messagesError', 'current', 'messages', 'draft', 'decision',
    'turnOutcomes', 'mapClaims', 'routingMode', 'alignmentLocalOnly', 'draftChanged', 'draftError', 'draftPending',
    'draftTimedOut', 'outcomeError', 'reviewSaveError', 'closingStreaming', 'onboardingStep'].map(key => [key, { value: null }]))
  const load = execute(`let conversationDetailAbort = null; ${fn('loadConversation')}\nreturn loadConversation`, {
    ...state, loadGate: createSessionGate(), draftPollGate: createSessionGate(),
    clearConversationAuxiliary() {}, clearMemoryAttention() {},
    getConversation(id, signal) { const work = defer(); pending.set(id, work); requests.push({ id, signal }); return work.promise },
    rememberConversationMetadata: value => value, toUi: value => value,
    route: { query: {} }, scrollToBottom: async () => {},
    scheduleConversationAuxiliary: id => auxiliary.push(id),
  })
  const first = load('A'), second = load('B')
  assert.equal(requests[0].signal.aborted, true, 'switching aborts the old detail request')
  pending.get('B').resolve({ conversation: { id: 'B', mode: 'chat' }, messages: [{ id: 'B-message' }] })
  await second
  pending.get('A').resolve({ conversation: { id: 'A', mode: 'chat' }, messages: [{ id: 'A-message' }] })
  await first
  assert.equal(state.current.value.id, 'B')
  assert.deepEqual(state.messages.value, [{ id: 'B-message' }])
  assert.deepEqual(auxiliary, ['B'], 'a late result never launches old auxiliary reads')
  assert.equal(state.messagesLoading.value, false)
  assert.deepEqual(requests.map(req => req.id), ['A', 'B'], 'no speculative prefetch or duplicate detail reads')
}
function creationHarness() {
  const pending = defer(), calls = [], current = { value:null }, currentId = { value:null }, conversations = { value:[] }
  const router = { async replace(path) { calls.push(['route', path]); currentId.value = decodeURIComponent(path.split('/').at(-1)) } }
  const page = execute(`let conversationCreation = null, conversationNavigation = 0, skipLoadFor = null;
    ${fn('ensureConversation')}\n${fn('createCurrentConversation')}
    return { ensureConversation, invalidate() { conversationNavigation++ } }`, {
    current, currentId, conversations, router, alive:true,
    createConversation: async data => { calls.push(['create', data]); return pending.promise },
    rememberConversationMetadata: value => value,
    composerRef: { value: { adoptLandingDraft: id => calls.push(['adopt-draft', id]) } },
  })
  return { ...page, calls, current, currentId, conversations, pending }
}
{
  const h = creationHarness(), first = h.ensureConversation('chat'), second = h.ensureConversation('chat')
  assert.equal(h.calls.filter(c => c[0] === 'create').length, 1, 'first send and lazy entry share one creation')
  h.pending.resolve({ id:'new-conversation', mode:'chat' })
  assert.equal((await first).id, (await second).id)
  assert.equal(h.calls.filter(c => c[0] === 'route').length, 1)
  assert.equal(h.conversations.value.length, 1)
  assert.deepEqual(h.calls.filter(c => c[0] === 'adopt-draft'), [['adopt-draft','new-conversation']])
}
{
  const h = creationHarness(), pending = h.ensureConversation('chat')
  h.invalidate(); h.currentId.value = 'other-conversation'
  h.pending.resolve({ id:'late-created', mode:'chat' })
  await assert.rejects(pending, /已切换对话/)
  assert.equal(h.current.value, null, 'late creation does not replace the selected conversation')
  assert.equal(h.calls.filter(c => c[0] === 'route').length, 0)
  assert.equal(h.calls.filter(c => c[0] === 'adopt-draft').length, 0, 'stale creation cannot move the newly selected draft')
}
assert.match(source, /<MatterWorkspace v-if="\(!currentId \|\| loadedConversationId\) && !guidedOnboarding"/)
assert.doesNotMatch(source.match(/<MatterWorkspace[^>]+/)[0], /conversationAuxPhase/, 'blank entry is not gated behind auxiliary reads')
// A failed background task is not a failed answer. Execute the real SSE handler.
let extraction
function walk(node) {
  if (ts.isPropertyAssignment(node) && node.name.getText(ast) === 'extraction') extraction = node.initializer.getText(ast)
  ts.forEachChild(node, walk)
}
walk(ast)
{
  const assistant = { status:'complete', content:'已经完成的正文' }, refreshed = []
  const handler = execute(`const handler = ${extraction}; return handler`, {
    assistant, alive:true, conv:{ id:'current' },
    refreshMemoryAttention: id => refreshed.push(id), routingPanel:{ value:{ refresh() {} } },
  })
  handler({ state:'failed', code:'WORKER_REGISTER_FAILED', jobId:'synthetic-job' })
  assert.equal(assistant.status, 'complete'); assert.equal(assistant.content, '已经完成的正文')
  assert.match(assistant.extractionNote, /个人理解整理未完成/); assert.deepEqual(refreshed, ['current'])
  handler({ state:'failed', taskKind:'charter_draft', jobId:'charter-job' })
  handler({ state:'skipped', reason:'too_short' })
  assert.match(assistant.extractionNote, /个人理解整理未完成/)
  assert.match(assistant.extractionNote, /人生章程草稿整理未完成/)
  handler({ state:'failed', jobId:null })
  assert.match(assistant.extractionNote, /整理任务未能保存/)
  assert.equal(assistant.status, 'complete')
}
console.log('conversation entry: starter replacement, lazy single-flight creation, stale navigation, visible entry and background-failure receipt passed')
