// 记忆 V3 · 核心画像视图：PersonalSummary 有画像时按分区渲染原文与来源方印，逐行可重申 / 撤回 / 看详情；
// 画像为空或读不到时回退到前端分组。编译真实 SFC，脚本用 mock 依赖跑 setup，模板用 SSR 渲染成 HTML 断言。
// 运行：node --experimental-strip-types tests/core-profile.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as SSR from 'vue/server-renderer'
import * as summary from '../src/components/ontology/summary.ts'
import * as ontology from '../src/shared/ontology.ts'
import * as alignment from '../src/shared/alignment.ts'

const read = (path) => readFile(new URL('../' + path, import.meta.url), 'utf8')
const cjs = (code) => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const source = await read('src/components/ontology/PersonalSummary.vue')
const { descriptor, errors } = parse(source, { filename: 'PersonalSummary.vue' })
assert.deepEqual(errors, [])
const compiledScript = compileScript(descriptor, { id: 'core-profile', fs: { fileExists: () => false, readFile: () => undefined } })
const compiledTemplate = compileTemplate({ source: descriptor.template.content, filename: 'PersonalSummary.vue', id: 'core-profile', ssr: true, ssrCssVars: [], compilerOptions: { bindingMetadata: compiledScript.bindings } })
assert.deepEqual(compiledTemplate.errors, [])
const scriptCode = cjs(compiledScript.content)
const templateCode = cjs(compiledTemplate.code)

const claim = (id, extras = {}) => ({ id, content: id, section: 'who', trustState: 'confirmed', layer: 'self_declared', scope: 'long_term', firstSeen: '2026-09-01T00:00:00Z', lastReaffirmed: '2026-09-01T00:00:00Z', evidence: [], ...extras })
const line = (id, section, kind, label, text, extras = {}) => ({ id, section, kind, label, text, ref: `${kind}:${id}`, ...extras })
const profile = () => ({
  scope: 'global', sourceHash: 'h1', generatedAt: '2026-09-17T00:00:00Z', budget: { external: 1200, local: 600 },
  text: '- [你告诉我的·2026-08-30] 我负责公司的产品研发\n- [你告诉我的·2026-09-01] 合伙人老林负责销售',
  lines: [
    line('l-who', 'who', 'claim', '你告诉我的', '我负责公司的产品研发', { claimId: 'c-role', date: '2026-08-30' }),
    line('l-people', 'people', 'claim', '你告诉我的', '合伙人老林负责销售', { claimId: 'c-partner' }),
    line('l-matter', 'matters', 'matter', '事项', '准备下周的合伙人沟通', { matterId: 'm-1' }),
    line('l-principle', 'principles', 'claim', '资料里看到的', '重大决定先给团队留协商空间', { claimId: 'c-principle' }),
    line('l-decision', 'recent', 'decision', '判断簿', '先谈职责再谈授权', { decisionId: 'd-9', date: '2026-09-10' }),
    line('l-summary', 'recent', 'summary', '对话摘要', '上次聊到如何把复杂约束说清楚', { summaryRef: 'conv-1' }),
  ],
})

function load(options = {}) {
  const calls = [], toasts = [], emits = []
  const api = {
    async reviewClaim(id, payload) {
      calls.push([id, payload])
      if (options.fail) throw new Error('合成失败')
      return { claim: claim(id, { trustState: payload.action === 'retract' ? 'retracted' : 'confirmed' }) }
    },
  }
  const resolve = (id) => {
    if (id === 'vue') return Vue
    if (id === 'vue/server-renderer') return SSR
    if (id.includes('services/api')) return api
    if (id.endsWith('/summary')) return summary
    if (id.includes('shared/ontology')) return ontology
    if (id.includes('shared/alignment')) return alignment
    if (id.includes('useToast')) return { useToast: () => (t) => toasts.push(t) }
    if (id.endsWith('.vue')) return { default: { name: id.split('/').pop() } }
    throw new Error('Unmocked import: ' + id)
  }
  const script = {}, template = {}
  new Function('require', 'exports', scriptCode)(resolve, script)
  new Function('require', 'exports', templateCode)(resolve, template)
  return { component: { ...script.default, ssrRender: template.ssrRender }, script, calls, toasts, emits }
}

async function render(props, options) {
  const h = load(options)
  const app = Vue.createSSRApp(h.component, props)
  app.config.warnHandler = () => {}
  app.component('RouterLink', { props: ['to'], setup(p, { slots }) { return () => Vue.h('a', { href: typeof p.to === 'string' ? p.to : p.to.path + (p.to.query?.decisionId ? '?decisionId=' + p.to.query.decisionId : '') }, slots.default?.()) } })
  app.component('ConfirmDialog', { props: ['open', 'message'], setup(p) { return () => (p.open ? Vue.h('div', { 'data-dialog': '' }, p.message) : null) } })
  return SSR.renderToString(app)
}

function setup(props, options) {
  const h = load(options)
  const scope = Vue.effectScope()
  const ui = scope.run(() => h.script.default.setup(Vue.reactive(props), { expose() {}, emit: (...args) => h.emits.push(args) }))
  return { ...h, ui, stop: () => scope.stop() }
}
const tick = async () => { await Vue.nextTick(); await new Promise((resolve) => setImmediate(resolve)) }

test('profile lines render by section in order with source seals, per-line actions and review links', async () => {
  const html = await render({ claims: [claim('current-role')], profile: profile() })
  assert.match(html, /知君每次回答都带着这一页（约 \d+ 字），这里只列已确认且不受限的内容/)
  const order = ['我是谁', '重要的人', '正在做的事与承诺', '原则', '近期脉络'].map((title) => html.indexOf(`>${title}<`))
  assert.ok(order.every((i) => i >= 0), '每个非空分区都有标题')
  assert.deepEqual(order, [...order].sort((a, b) => a - b), '分区顺序固定')
  assert.doesNotMatch(html, />做法<|>方向</, '空分区不占位')
  for (const text of ['我负责公司的产品研发', '合伙人老林负责销售', '准备下周的合伙人沟通', '先谈职责再谈授权', '上次聊到如何把复杂约束说清楚']) assert.ok(html.includes(text), text)
  assert.match(html, /class="(zj-seal zj-seal--ink|zj-seal--ink zj-seal)">你告诉我的</)
  assert.match(html, /class="(zj-seal zj-seal--green|zj-seal--green zj-seal)">资料里看到的</)
  assert.match(html, /class="zj-seal">判断簿</)
  assert.equal((html.match(/>还是这样</g) || []).length, 3, '只有理解行有重申')
  assert.equal((html.match(/>不再这样了</g) || []).length, 3)
  assert.equal((html.match(/>详情</g) || []).length, 3)
  assert.equal((html.match(/>去回看</g) || []).length, 3, '派生行（事项 / 判断 / 摘要）只链到回看')
  assert.match(html, /href="\/review\?decisionId=d-9"/)
  assert.doesNotMatch(html, /这是你留下的原话与已核对的理解|身份与角色/, '有画像时不再显示前端分组')
  assert.doesNotMatch(html, /data-dialog/, '撤回确认框默认关闭')
  assert.doesNotMatch(html, /—/, '不用长破折号')
})

test('falls back to the local grouping when the profile is missing or empty', async () => {
  for (const missing of [null, undefined, { ...profile(), lines: [] }]) {
    const html = await render({ claims: [claim('current-role'), claim('uncertain', { trustState: 'working' })], profile: missing })
    assert.match(html, /这是你留下的原话与已核对的理解/)
    assert.match(html, /身份与角色/)
    assert.ok(html.includes('current-role'))
    assert.doesNotMatch(html, /知君每次回答都带着这一页/)
  }
})

test('reaffirm calls reviewClaim on the ontology page surface and reports the change', async () => {
  const h = setup({ claims: [], profile: profile() })
  assert.equal(h.ui.profileMode.value, true)
  const who = h.ui.sections.value.find((s) => s.key === 'who')
  h.ui.act(who.lines[0], 'reaffirm')
  await tick()
  assert.deepEqual(h.calls, [['c-role', { action: 'reaffirm', surface: 'ontology_page' }]])
  assert.equal(h.emits.length, 1)
  assert.equal(h.emits[0][0], 'changed')
  assert.equal(h.emits[0][1].id, 'c-role')
  assert.equal(h.emits[0][2], 'reaffirm')
  assert.deepEqual(h.toasts, [{ type: 'success', message: '已重申' }])
  assert.equal(h.ui.sections.value.find((s) => s.key === 'who').lines.length, 1, '重申后这一行还在')
  h.stop()
})

test('retract asks first, then calls reviewClaim and drops the line locally', async () => {
  const h = setup({ claims: [], profile: profile() })
  const people = h.ui.sections.value.find((s) => s.key === 'people')
  h.ui.act(people.lines[0], 'retract')
  await tick()
  assert.deepEqual(h.calls, [], '点「不再这样了」先弹确认，不直接撤回')
  assert.equal(h.ui.retractTarget.value?.claimId, 'c-partner')
  h.ui.confirmRetract()
  await tick()
  assert.equal(h.ui.retractTarget.value, null)
  assert.deepEqual(h.calls, [['c-partner', { action: 'retract', surface: 'ontology_page' }]])
  assert.equal(h.ui.sections.value.some((s) => s.key === 'people'), false, '撤回后这一行立刻消失，空分区不再占位')
  assert.deepEqual(h.toasts, [{ type: 'success', message: '已撤回，知君不会再当作对你的认识' }])
  assert.equal(h.emits[0][0], 'changed')
  assert.equal(h.emits[0][2], 'retract')
  h.stop()
})

test('a failed review keeps the line, reports the error and emits nothing', async () => {
  const h = setup({ claims: [], profile: profile() }, { fail: true })
  h.ui.act(h.ui.sections.value[0].lines[0], 'reaffirm')
  await tick()
  assert.equal(h.calls.length, 1)
  assert.deepEqual(h.emits, [])
  assert.deepEqual(h.toasts, [{ type: 'error', message: '合成失败' }])
  assert.equal(h.ui.sections.value[0].lines.length, 1)
  h.stop()
})

test('derived lines and lines without a claim never call reviewClaim', async () => {
  const h = setup({ claims: [], profile: profile() })
  const recent = h.ui.sections.value.find((s) => s.key === 'recent')
  h.ui.act(recent.lines[0], 'reaffirm')
  h.ui.act(recent.lines[1], 'retract')
  await tick()
  assert.deepEqual(h.calls, [])
  assert.equal(h.ui.retractTarget.value, null)
  assert.deepEqual(h.ui.derivedLink(recent.lines[0]), { path: '/review', query: { decisionId: 'd-9' } })
  assert.deepEqual(h.ui.derivedLink(recent.lines[1]), { path: '/review' })
  h.stop()
})

test('page, loader, api and catalog are wired to the core profile endpoint', async () => {
  const page = await read('src/pages/OntologyPage.vue')
  const api = await read('src/services/api.ts')
  const loading = await read('src/pages/ontologyLoading.ts')
  const catalog = JSON.parse(await read('../shared/product-operations.json'))
  assert.match(source, /emit\('select-claim', line\.claimId\)/)
  assert.match(source, /<ConfirmDialog/)
  assert.match(page, /:profile="profile"/)
  assert.match(page, /@select-claim="selectClaimId"/)
  assert.match(page, /@changed="onProfileChanged"/)
  assert.match(page, /plan\.profile && !profileLoaded\.value && !profileLoading\.value/)
  assert.match(page, /invalidateProfile\(\)/)
  assert.match(loading, /profile: true/)
  assert.match(api, /export function getCoreProfile/)
  assert.match(api, /'\/mindos\/ontology\/core-profile'/)
  assert.match(api, /export interface CoreProfileLine/)
  const op = catalog.operations.find((o) => o.path === '/api/mindos/ontology/core-profile')
  assert.deepEqual(op, { id: 'get_api_mindos_ontology_core_profile', method: 'GET', path: '/api/mindos/ontology/core-profile', pathParams: [], query: [], body: 'none', response: 'json', maxRequestBytes: 0, maxResponseBytes: 524288, capability: 'domain', mutating: false })
  for (const [path, method, body] of [['/api/mindos/nudges/today', 'GET', 'none'], ['/api/mindos/nudges/scan', 'POST', 'json'], ['/api/mindos/nudges/{nudgeId}/dismiss', 'POST', 'json'], ['/api/mindos/nudges/{nudgeId}/silence', 'POST', 'json']]) {
    const entry = catalog.operations.find((o) => o.path === path && o.method === method)
    assert.ok(entry, path)
    assert.equal(entry.body, body)
    assert.equal(entry.capability, 'domain')
    assert.equal(entry.mutating, method !== 'GET')
  }
  assert.match(api, /kind: 'onboarding' \| 'resume_onboarding' \| 'review' \| 'reflect' \| 'commitment' \| 'confirm' \| 'nudge' \| 'chat' \| 'inquiry'/)
  assert.match(api, /coreProfile\?: \{ lineCount: number; claimIds: string\[\]; sourceHash: string; excludedCount: number \}/)
  assert.match(api, /inquiry\?: \{ kind: string; targetId: string \| null \}/)
})
