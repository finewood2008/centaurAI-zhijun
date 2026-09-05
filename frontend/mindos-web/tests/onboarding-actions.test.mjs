import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
const compile = async path => {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  return { source, code: ts.transpileModule(compileScript(parse(source).descriptor, { id: path }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText }
}
const page = await compile('../src/pages/OnboardingPage.vue')
const chip = await compile('../src/components/conversation/ClaimCandidateChip.vue')
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
function setup(overrides = {}) {
  const calls = [], cleanups = [], exports = {}
  const api = {
    getOnboardingProgress: async () => ({ state: 'ready', conversationId: null }),
    createConversation: async body => { calls.push(['create', body]); return { id: 'synthetic' } },
    updateOnboarding: async action => { calls.push(['update', action]); return { state: 'interviewing', conversationId: 'started' } },
    push: async path => calls.push(['navigate', path]),
    ...overrides,
  }
  new Function('require','exports',page.code)(id => id === 'vue' ? { ...Vue, onMounted() {}, onBeforeUnmount: fn => cleanups.push(fn) } : id === 'vue-router' ? { useRouter: () => ({ push: (...args) => api.push(...args) }) } : api, exports)
  const ui = exports.default.setup({}, { expose() {} })
  return { ui, api, calls, close() { cleanups.forEach(fn => fn()) } }
}

// Loading errors cannot accidentally create a new onboarding conversation.
{
  const h = setup({ getOnboardingProgress: async () => { throw Error('离线') } })
  await h.ui.load(); await h.ui.continueChat(); await h.ui.finish()
  assert.equal(h.calls.length, 0); assert.match(h.ui.error.value, /离线/)
  h.api.getOnboardingProgress = async () => ({ state: 'interviewing', conversationId: 'existing' })
  await h.ui.load(); await h.ui.continueChat()
  assert.deepEqual(h.calls, [['navigate','/onboarding/c/existing?charter=1']]); assert.equal(h.ui.error.value, '')
  h.close()
}
// Rapid clicks serialize actions; a navigation failure reuses the created conversation.
{
  const waiting = deferred(), h = setup()
  let creates = 0
  h.api.createConversation = async () => { creates++; return waiting.promise }
  await h.ui.load()
  const first = h.ui.continueChat(); await h.ui.continueChat(); await h.ui.finish()
  assert.equal(creates, 1); assert.equal(h.calls.length, 0)
  h.api.push = async () => { throw Error('导航失败') }
  waiting.resolve({ id: 'created-once' }); await first
  assert.match(h.ui.error.value, /导航失败/)
  h.api.push = async path => h.calls.push(['navigate', path])
  await h.ui.continueChat()
  assert.equal(creates, 1); assert.deepEqual(h.calls, [['navigate', '/c/created-once?charter=1']])
  h.close()
}
// Finishing does not require a charter/model/source and cannot double-submit.
{
  const wait = deferred(), h = setup({ updateOnboarding: async () => wait.promise })
  await h.ui.load(); const run = h.ui.finish(); await h.ui.finish()
  assert.equal(h.ui.busy.value, true); wait.resolve({ state:'ready' }); await run
  assert.deepEqual(h.calls, [['navigate', '/chat']]); h.close()
}
// Leaving while creation is in flight never navigates the user back later.
{
  const wait = deferred(), h = setup({ createConversation: () => wait.promise })
  await h.ui.load(); const run = h.ui.continueChat(); h.close()
  wait.resolve({ id: 'late' }); await run; assert.equal(h.calls.length, 0)
}

// A candidate edit must remain visible for retry if its parent save fails.
{
  const exports = {}, emits = [], props = Vue.reactive({ claim: { id: 'c', content: '原理解' }, busy: false })
  new Function('require','exports',chip.code)(id => id === 'vue' ? Vue : {}, exports)
  const ui = exports.default.setup(props, { expose() {}, emit: (...args) => emits.push(args) })
  ui.onAction('partial'); ui.edited.value = '我的准确说法'; ui.submitPartial()
  assert.equal(ui.editing.value, true); assert.equal(ui.edited.value, '我的准确说法')
  assert.deepEqual(emits, [['review', 'partial', '我的准确说法']])
  props.busy = true; ui.submitPartial(); assert.equal(emits.length, 1)
}
assert.match(page.source, /:disabled="busy \|\| loading \|\| !progress"/)
assert.match(chip.source, /v-model="edited" :disabled="busy"/)
const list = await readFile(new URL('../src/components/conversation/ConversationList.vue', import.meta.url), 'utf8')
assert.doesNotMatch(list, /ONBOARDING_TOTAL_TURNS|onboardingUserTurns|建档 · 第/, 'topic-based onboarding must not show inferred question counts')
console.log('onboarding/actions: failed reads, double-clicks, safe retries, skip path, stale navigation and candidate edit preservation passed')
