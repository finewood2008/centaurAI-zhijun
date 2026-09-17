// 记忆 V3 · 「这段对话留下的」撤回：亲口说的、直接记下的理解（undoable）显示一句安静的文字按钮，
// 点「撤回」走 reviewClaim(retract, surface=conversation) 并让父组件重新读取成果。卡片上没有别的按钮。
// 运行：node --experimental-strip-types tests/outcomes-card.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as SSR from 'vue/server-renderer'
import * as ontology from '../src/shared/ontology.ts'

const read = (path) => readFile(new URL('../' + path, import.meta.url), 'utf8')
const cjs = (code) => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const source = await read('src/components/conversation/OutcomesCard.vue')
const { descriptor, errors } = parse(source, { filename: 'OutcomesCard.vue' })
assert.deepEqual(errors, [])
const compiledScript = compileScript(descriptor, { id: 'outcomes-card', fs: { fileExists: () => false, readFile: () => undefined } })
const compiledTemplate = compileTemplate({ source: descriptor.template.content, filename: 'OutcomesCard.vue', id: 'outcomes-card', ssr: true, ssrCssVars: [], compilerOptions: { bindingMetadata: compiledScript.bindings } })
assert.deepEqual(compiledTemplate.errors, [])
const scriptCode = cjs(compiledScript.content)
const templateCode = cjs(compiledTemplate.code)

const brief = (id, extras = {}) => ({ id, content: `理解 ${id}`, section: 'who', layer: 'self_declared', ...extras })
const outcomes = (confirmedClaims) => ({ conversationId: 'conv-1', confirmedClaims, workingClaims: [], decision: null, commitments: [], pendingJobs: 0, retracted: 0 })

function load(options = {}) {
  const calls = [], toasts = [], emits = []
  const api = {
    async reviewClaim(id, payload) {
      calls.push([id, payload])
      if (options.fail) throw new Error('合成失败')
      return { claim: { id, trustState: 'retracted' } }
    },
  }
  const resolve = (id) => {
    if (id === 'vue') return Vue
    if (id === 'vue/server-renderer') return SSR
    if (id.includes('services/api')) return api
    if (id.includes('shared/ontology')) return ontology
    if (id.includes('useToast')) return { useToast: () => (t) => toasts.push(t) }
    throw new Error('Unmocked import: ' + id)
  }
  const script = {}, template = {}
  new Function('require', 'exports', scriptCode)(resolve, script)
  new Function('require', 'exports', templateCode)(resolve, template)
  return { component: { ...script.default, ssrRender: template.ssrRender }, script, calls, toasts, emits }
}

async function render(props) {
  const h = load()
  const app = Vue.createSSRApp(h.component, props)
  app.config.warnHandler = () => {}
  app.component('RouterLink', { props: ['to'], setup(p, { slots }) { return () => Vue.h('a', { href: typeof p.to === 'string' ? p.to : p.to.path }, slots.default?.()) } })
  return SSR.renderToString(app)
}

function setup(props, options) {
  const h = load(options)
  const scope = Vue.effectScope()
  const ui = scope.run(() => h.script.default.setup(Vue.reactive(props), { expose() {}, emit: (...args) => h.emits.push(args) }))
  return { ...h, ui, stop: () => scope.stop() }
}
const tick = async () => { await Vue.nextTick(); await new Promise((resolve) => setImmediate(resolve)) }

test('only undoable confirmed claims get the quiet undo text, and it is the only button on the card', async () => {
  const html = await render({ outcomes: outcomes([brief('a', { undoable: true, trustOrigin: 'utterance', createdAt: '2026-09-17T00:00:00Z' }), brief('b', { undoable: false }), brief('c')]), conversationId: 'conv-1' })
  assert.equal((html.match(/你亲口说的，已直接记下 · /g) || []).length, 1)
  assert.equal((html.match(/<button/g) || []).length, 1)
  assert.match(html, /<button type="button" class="zj-outcomes__undo-btn">撤回<\/button>/)
  assert.ok(html.includes('理解 a') && html.includes('理解 b') && html.includes('理解 c'))
  assert.doesNotMatch(html, /—/)
  const quiet = await render({ outcomes: outcomes([brief('a'), brief('b')]) })
  assert.doesNotMatch(quiet, /<button|撤回|已直接记下/, '旧盒端没有 undoable 字段时卡片和从前一样安静')
})

test('undo retracts on the conversation surface, toasts and asks the parent to refresh', async () => {
  const h = setup({ outcomes: outcomes([brief('a', { undoable: true })]), conversationId: 'conv-1' })
  const claim = h.ui.shownConfirmed.value[0]
  const pending = h.ui.undo(claim)
  h.ui.undo(claim)
  await pending
  await tick()
  assert.deepEqual(h.calls, [['a', { action: 'retract', surface: 'conversation', conversationId: 'conv-1' }]], '连点只发一次')
  assert.deepEqual(h.emits, [['refresh']])
  assert.deepEqual(h.toasts, [{ type: 'success', message: '已撤回，知君不会再当作对你的认识' }])
  h.stop()
})

test('a failed undo reports the error and does not refresh', async () => {
  const h = setup({ outcomes: outcomes([brief('a', { undoable: true })]), conversationId: 'conv-1' }, { fail: true })
  await h.ui.undo(h.ui.shownConfirmed.value[0])
  await tick()
  assert.equal(h.calls.length, 1)
  assert.deepEqual(h.emits, [])
  assert.deepEqual(h.toasts, [{ type: 'error', message: '合成失败' }])
  h.stop()
})

test('conversation page passes the conversation id and re-reads outcomes after an undo', async () => {
  const page = await read('src/pages/ConversationPage.vue')
  const api = await read('src/services/api.ts')
  assert.match(page, /<OutcomesCard[^>]*:conversation-id="current\?\.id"[^>]*@refresh="current && refreshOutcomes\(current\.id, true\)"/)
  assert.match(api, /export interface OutcomeClaimBrief extends ClaimBrief \{\s*trustOrigin\?: TrustOrigin\s*createdAt\?: string\s*undoable\?: boolean\s*\}/)
  assert.match(api, /confirmedClaims: OutcomeClaimBrief\[\]/)
})
