// 记忆 V3 · M8 知君的主动性（前端）：知君主动发起、你还没回的会话在列表里标「知君发起」；那条知君消息下写明为何现在，
// 并可「先别找我三天」；今日页在来信之后、地图之前有一段「知君想和你聊」；设置页「知君主动找我」折叠区即改即存。
// 编译真实 SFC，脚本用 mock 依赖跑 setup，模板用 SSR 渲染成 HTML 断言（同 core-profile / outcomes-card）。
// 运行：node --experimental-strip-types tests/proactive-ui.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as SSR from 'vue/server-renderer'
import MarkdownIt from 'markdown-it'
import * as labels from '../src/shared/labels.ts'
import * as format from '../src/shared/format.ts'
import * as conversationManagement from '../src/shared/conversationManagement.ts'
import * as visiblePolling from '../src/composables/visiblePolling.ts'

const read = (path) => readFile(new URL('../' + path, import.meta.url), 'utf8')
const cjs = (code) => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

async function compile(path, id) {
  const source = await read(path)
  const { descriptor, errors } = parse(source, { filename: path })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id, fs: { fileExists: () => false, readFile: () => undefined } })
  const template = compileTemplate({ source: descriptor.template.content, filename: path, id, ssr: true, ssrCssVars: [], compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  return { source, scriptCode: cjs(script.content), templateCode: cjs(template.code) }
}

// 子组件与图标都换成带名字的空壳：单根 div 让透传的 class 留在 HTML 里，方便断言先后顺序
const stub = (name) => ({ name, render: () => Vue.h('div', { 'data-stub': name }) })
const icons = new Proxy({}, { get: (_target, name) => (name === '__esModule' ? true : stub(String(name))) })

function load(compiled, mocks) {
  const resolve = (id) => {
    if (id === 'vue') return Vue
    if (id === 'vue/server-renderer') return SSR
    for (const [key, value] of Object.entries(mocks)) if (id === key || id.includes(key)) return value
    if (id.endsWith('.vue')) return { __esModule: true, default: stub(id.split('/').pop()) }
    throw new Error('Unmocked import: ' + id)
  }
  const script = {}, template = {}
  new Function('require', 'exports', compiled.scriptCode)(resolve, script)
  new Function('require', 'exports', compiled.templateCode)(resolve, template)
  return { component: { ...script.default, ssrRender: template.ssrRender }, script }
}

// seed：在 setup 返回之前改写状态（onMounted 在 SSR 里不会跑，页面级组件靠它把 overview 灌进去）
async function render(component, props, { seed } = {}) {
  const wrapped = seed ? { ...component, setup(p, ctx) { const bindings = component.setup(p, ctx); seed(bindings); return bindings } } : component
  const app = Vue.createSSRApp(wrapped, props)
  app.config.warnHandler = () => {}
  app.component('RouterLink', { props: ['to'], setup(p, { slots }) { return () => Vue.h('a', { href: typeof p.to === 'string' ? p.to : p.to.path }, slots.default?.()) } })
  return SSR.renderToString(app)
}

function setup(script, props) {
  const scope = Vue.effectScope()
  const warn = console.warn
  console.warn = () => {}
  try {
    const ui = scope.run(() => script.default.setup(Vue.reactive(props), { expose() {}, emit() {} }))
    return { ui, stop: () => scope.stop() }
  } finally {
    console.warn = warn
  }
}
const tick = async () => { await Vue.nextTick(); await new Promise((resolve) => setImmediate(resolve)) }

// ---- 纯函数

test('initiated kind labels, the why-now caption and the snooze line are pure and quiet', () => {
  const expected = { review_due: '回访', commitment_due: '承诺', principle_tension: '原则与做法', weekly_review: '每周回顾', open_loop: '上次的事', nod: '想核对', stale: '好久没提', gap: '还不了解', milestone: '纪念日', greeting: '问候' }
  for (const [kind, text] of Object.entries(expected)) assert.equal(labels.initiatedKindLabel(kind), text)
  assert.equal(labels.initiatedKindLabel('something_new'), '知君发起')
  assert.equal(labels.initiatedKindLabel(undefined), '知君发起')
  assert.equal(labels.initiatedCaption('「合伙人沟通」到了回访的日子'), '知君主动找你 · 「合伙人沟通」到了回访的日子')
  assert.equal(labels.initiatedCaption('  '), '知君主动找你')
  assert.equal(labels.initiatedCaption(null), '知君主动找你')
  const now = new Date(2026, 8, 17, 10, 0, 0)
  assert.equal(labels.proactiveSnoozeLine(new Date(2026, 8, 20, 10, 0, 0).toISOString(), now), '已请知君到 9月20日 前别找你')
  assert.equal(labels.proactiveSnoozeLine(new Date(2026, 8, 17, 9, 0, 0).toISOString(), now), '', '已过期不显示')
  assert.equal(labels.proactiveSnoozeLine(null, now), '')
  assert.equal(labels.proactiveSnoozeLine('not-a-date', now), '')
  for (const text of Object.values(expected)) assert.doesNotMatch(text, /—/)
})

// ---- 会话列表

const list = await compile('src/components/conversation/ConversationList.vue', 'proactive-list')

test('conversation list seals unanswered zhijun-initiated conversations 「知君发起」 and everything else by mode', async () => {
  const { component } = load(list, { 'lucide-vue-next': icons, 'shared/conversationManagement': conversationManagement, 'shared/format': format, 'shared/labels': labels })
  const conv = (id, extras = {}) => ({ id, title: `会话 ${id}`, mode: 'chat', status: 'active', pinnedAt: null, metadataRevision: 1, decisionId: null, messageCount: 1, createdAt: '2026-09-17T00:00:00Z', updatedAt: '2026-09-17T00:00:00Z', lastMessageAt: null, ...extras })
  const items = [
    conv('a', { initiated: { by: 'zhijun', kind: 'review_due', whyNow: '到了回访的日子', createdAt: '2026-09-17T00:00:00Z', answeredAt: null } }),
    conv('b', { mode: 'review', initiated: { by: 'zhijun', kind: 'review_due', whyNow: '', createdAt: '2026-09-16T00:00:00Z', answeredAt: '2026-09-16T01:00:00Z' } }),
    conv('c'),
    conv('d', { initiated: null }),
  ]
  const html = await render(component, { items, currentId: null })
  const seals = [...html.matchAll(/class="zj-seal zj-seal--muted zj-convs__seal">([^<]+)</g)].map((m) => m[1])
  assert.deepEqual(seals, ['知君发起', '回访', '对话', '对话'], '只有没回的主动会话换印；回过的按模式；旧盒端没有字段照旧')
  assert.equal((html.match(/知君发起/g) || []).length, 1)
  assert.doesNotMatch(html, /—/)
})

// ---- 知君消息下方的说明与「先别找我三天」

const bubble = await compile('src/components/conversation/MessageBubble.vue', 'proactive-bubble')

function bubbleHarness(options = {}) {
  const calls = [], toasts = []
  const api = {
    async snoozeProactive(days) {
      calls.push(days)
      if (options.fail) throw new Error('合成失败')
      return { enabled: true, maxPerDay: 3, silencedRefs: [], proactive: { snoozeUntil: '2026-09-20T00:00:00Z' } }
    },
  }
  const h = load(bubble, { 'markdown-it': { __esModule: true, default: MarkdownIt }, 'services/api': api, useToast: { useToast: () => (t) => toasts.push(t) }, 'shared/labels': labels })
  return { ...h, calls, toasts }
}

test('an initiated assistant message shows the muted why-now caption with the snooze text link; other messages are untouched', async () => {
  const h = bubbleHarness()
  const html = await render(h.component, { role: 'assistant', content: '你上次说要和合伙人谈职责。', status: 'complete', initiated: { whyNow: '「合伙人沟通」到了回访的日子' } })
  assert.match(html, /data-testid="initiated-caption"/)
  assert.ok(html.includes('知君主动找你 · 「合伙人沟通」到了回访的日子'))
  assert.match(html, /<button type="button" class="zj-msg__initiated-snooze">先别找我三天<\/button>/)
  assert.ok(html.indexOf('initiated-caption') > html.indexOf('zj-msg__body'), '说明在正文之下')
  assert.equal((html.match(/<button/g) || []).length, 1, '不带 allowSave 时只有这一个文字按钮')
  assert.doesNotMatch(html, /—/)
  const bare = await render(h.component, { role: 'assistant', content: '开场', status: 'complete', initiated: { whyNow: '' } })
  assert.ok(bare.includes('<span>知君主动找你</span>'), '没有理由时只写前半句')
  const plain = await render(h.component, { role: 'assistant', content: '普通回复', status: 'complete' })
  assert.doesNotMatch(plain, /知君主动找你|先别找我三天|initiated-caption/)
  const user = await render(h.component, { role: 'user', content: '好的', status: 'complete', initiated: { whyNow: 'x' } })
  assert.doesNotMatch(user, /知君主动找你/)
})

test('snooze asks for three days once, then reports; a failure reports the error and nothing else', async () => {
  const h = bubbleHarness()
  const s = setup(h.script, { role: 'assistant', content: 'x', initiated: { whyNow: 'y' } })
  const pending = s.ui.snooze()
  s.ui.snooze()
  await pending
  await tick()
  assert.deepEqual(h.calls, [3], '连点只发一次')
  assert.deepEqual(h.toasts, [{ type: 'success', message: '好，三天内我不会主动找你' }])
  assert.equal(s.ui.snoozing.value, false)
  s.stop()
  const f = bubbleHarness({ fail: true })
  const fs = setup(f.script, { role: 'assistant', content: 'x', initiated: { whyNow: 'y' } })
  await fs.ui.snooze()
  await tick()
  assert.deepEqual(f.calls, [3])
  assert.deepEqual(f.toasts, [{ type: 'error', message: '合成失败' }])
  fs.stop()
})

test('conversation page hands zhijun_initiated meta to the bubble, leaves chat_open alone, and api carries the contracts', async () => {
  const page = await read('src/pages/ConversationPage.vue')
  assert.match(page, /function initiatedMeta\(m: UiMessage\)[\s\S]*m\.meta\?\.kind !== 'zhijun_initiated'/)
  assert.match(page, /:initiated="initiatedMeta\(m\)"/)
  assert.match(page, /m\.meta\?\.kind === 'chat_open'/)
  const api = await read('src/services/api.ts')
  assert.match(api, /export async function snoozeProactive\(days: number/)
  assert.match(api, /proactive: \{ \.\.\.\(policy\.proactive \?\? \{\}\), snoozeUntil \}/, '读改写：只动 snoozeUntil')
  assert.match(api, /initiated\?: ConversationInitiated \| null/)
  assert.match(api, /initiated\?: HomeInitiated\[\]/)
  assert.match(api, /proactive\?: ProactivePolicy \| null/)
  assert.match(api, /'review_due'[\s\S]*'greeting'/)
  const catalog = JSON.parse(await read('../shared/product-operations.json'))
  for (const method of ['GET', 'PUT']) assert.ok(catalog.operations.find((o) => o.path === '/api/mindos/nudges/policy' && o.method === method), `${method} policy 已在目录里，不需要新条目`)
})

// ---- 今日页「知君想和你聊」

const today = await compile('src/pages/TodayPage.vue', 'proactive-today')
const overview = (initiated) => ({
  state: 'active',
  brief: { status: 'ready', headline: '合成标题', message: '合成正文', sourceRefs: [], generatedBy: 'template' },
  map: { relationshipDays: 3, nodes: [] },
  nextAction: { kind: 'chat', title: '接着聊', description: '', targetId: null, say: null },
  timeline: [], generatedAt: '2026-09-17T00:00:00Z', sourceHash: 'h',
  ...(initiated === undefined ? {} : { initiated }),
})
function todayHarness() {
  const reads = []
  return { reads, ...load(today, {
    'vue-router': { useRouter: () => ({ push: async () => {} }) },
    'lucide-vue-next': icons,
    'services/api': { getZhijunHome: async () => { reads.push(1); return overview([]) }, createConversation: async () => ({ id: 'x' }), updateOnboarding: async () => {} },
    useToast: { useToast: () => () => {} },
    'shared/labels': labels,
  }) }
}

test('today page lists at most three initiated conversations between the letter and the map, linking to /c/{id}', async () => {
  const h = todayHarness()
  const items = [1, 2, 3, 4].map((n) => ({ conversationId: `conv-${n}`, title: `想聊 ${n}`, whyNow: `理由 ${n}`, kind: ['review_due', 'nod', 'milestone', 'greeting'][n - 1], createdAt: '2026-09-17T00:00:00Z' }))
  const html = await render(h.component, {}, { seed: (b) => { b.overview.value = overview(items); b.loading.value = false } })
  assert.match(html, /aria-label="知君想和你聊"/)
  assert.ok(html.includes('>知君想和你聊</p>'))
  assert.equal((html.match(/class="zj-initiated__row"/g) || []).length, 3, '最多三条')
  for (const n of [1, 2, 3]) {
    assert.match(html, new RegExp(`href="/c/conv-${n}"`))
    assert.ok(html.includes(`想聊 ${n}`) && html.includes(`理由 ${n}`))
  }
  assert.doesNotMatch(html, /conv-4/)
  const seals = [...html.matchAll(/class="zj-seal zj-seal--muted">([^<]+)</g)].map((m) => m[1])
  assert.deepEqual(seals, ['回访', '想核对', '纪念日'])
  const at = (needle) => { const i = html.indexOf(needle); assert.ok(i >= 0, needle); return i }
  assert.ok(at('data-testid="initiated-section"') > at('zj-letter__action'), '在来信之后')
  assert.ok(at('data-testid="initiated-section"') < at('data-stub="RelationshipMap.vue"'), '在共同地图之前')
  assert.doesNotMatch(html, /—/)
  assert.deepEqual(h.reads, [], 'SSR 里没有挂载读取；这段只读 overview')
})

test('today page hides the section when the backend gives no initiated conversations, and adds no request of its own', async () => {
  for (const initiated of [undefined, []]) {
    const h = todayHarness()
    const html = await render(h.component, {}, { seed: (b) => { b.overview.value = overview(initiated); b.loading.value = false } })
    assert.doesNotMatch(html, /知君想和你聊|zj-initiated|initiated-section/)
    assert.match(html, /zj-letter__action/)
  }
  assert.match(today.source, /overview\.value\?\.initiated \?\? \[\]/)
  assert.doesNotMatch(today.source, /getNudgePolicy|snoozeProactive|fetch\(/)
  assert.match(today.source, /grid-template-areas: "letter map" "panel map"/, '版式区域名不变')
})

// ---- 设置页「知君主动找我」

const settings = await compile('src/components/settings/WorkspaceSettings.vue', 'proactive-settings')

function settingsHarness(options = {}) {
  const gets = [], puts = [], toasts = []
  const policy = () => ({ enabled: true, maxPerDay: 3, silencedRefs: [], ...(options.policy ?? {}) })
  const api = {
    api: {},
    ApiError: class ApiError extends Error {},
    async getNudgePolicy() { gets.push(1); return policy() },
    async putNudgePolicy(payload) {
      puts.push(structuredClone(payload))
      if (options.fail) throw new Error('合成失败')
      return { ...policy(), ...payload }
    },
    async getMemoryPolicy() { return { mode: 'important', revision: 1 } },
    async putMemoryPolicy() { return { mode: 'important', revision: 2 } },
  }
  const h = load(settings, { 'lucide-vue-next': icons, 'services/api': api, useToast: { useToast: () => (t) => toasts.push(t) }, 'composables/visiblePolling': visiblePolling, 'shared/labels': labels })
  return { ...h, gets, puts, toasts }
}

test('settings: the folded block saves the whole proactive object at once and shows off when an old backend has none', async () => {
  const h = settingsHarness()
  const s = setup(h.script, {})
  assert.equal(h.gets.length, 0, 'setup 本身不读策略；挂载时那次读取是原有的')
  assert.equal(s.ui.proactiveOpen.value, false, '默认折叠')
  await s.ui.loadNudgePolicy()
  assert.equal(h.gets.length, 1)
  assert.equal(s.ui.proactive.enabled, false, '旧盒端没有 proactive 时按关闭显示')
  assert.equal(s.ui.snoozeLine.value, '')
  s.ui.proactive.enabled = true
  await s.ui.saveProactive()
  assert.deepEqual(h.puts, [{ proactive: { enabled: true, maxPerDay: 1, minGapHours: 4, quietHours: { start: '22:00', end: '08:00' }, snoozeUntil: null, greetAfterDays: 0, backoffUntil: null } }])
  assert.deepEqual(h.toasts, [{ type: 'success', message: '已记住' }])
  assert.equal(h.gets.length, 1, '保存成功不再重读')
  s.ui.onProactiveToggle({ target: { open: true } })
  assert.equal(s.ui.proactiveOpen.value, true)
  assert.equal(h.gets.length, 1, '已读到过策略时，展开不发请求')
  s.stop()
})

test('settings: a loaded proactive policy fills the controls, unusual values stay selectable, and the snooze line can be cancelled', async () => {
  const future = new Date(Date.now() + 2 * 86_400_000)
  const h = settingsHarness({ policy: { proactive: { enabled: true, maxPerDay: 5, minGapHours: 6, quietHours: { start: '23:00', end: '07:30' }, snoozeUntil: future.toISOString(), greetAfterDays: 7, backoffUntil: '2026-09-19T00:00:00Z' } } })
  const s = setup(h.script, {})
  await s.ui.loadNudgePolicy()
  assert.deepEqual({ ...s.ui.proactive }, { enabled: true, maxPerDay: 5, minGapHours: 6, quietStart: '23:00', quietEnd: '07:30', snoozeUntil: future.toISOString(), greetAfterDays: 7, backoffUntil: '2026-09-19T00:00:00Z' })
  assert.deepEqual(s.ui.maxPerDayOptions.value, [0, 1, 2, 3, 5])
  assert.deepEqual(s.ui.minGapOptions.value, [2, 4, 6, 8, 12])
  assert.deepEqual(s.ui.greetOptions.value, [0, 3, 7, 14])
  assert.equal(s.ui.snoozeLine.value, `已请知君到 ${future.getMonth() + 1}月${future.getDate()}日 前别找你`)
  s.ui.cancelSnooze()
  await tick()
  assert.equal(h.puts.length, 1)
  assert.equal(h.puts[0].proactive.snoozeUntil, null)
  assert.equal(h.puts[0].proactive.maxPerDay, 5, '取消只动 snoozeUntil，其它照旧写回')
  assert.equal(h.puts[0].proactive.backoffUntil, '2026-09-19T00:00:00Z')
  assert.equal(s.ui.snoozeLine.value, '')
  s.stop()
})

test('settings: a failed save reports and re-reads; an incomplete quiet-hours pair is not sent', async () => {
  const h = settingsHarness({ fail: true })
  const s = setup(h.script, {})
  await s.ui.loadNudgePolicy()
  s.ui.proactive.enabled = true
  await s.ui.saveProactive()
  assert.equal(h.puts.length, 1)
  assert.deepEqual(h.toasts, [{ type: 'error', message: '合成失败' }])
  assert.equal(h.gets.length, 2, '失败后重读一次')
  assert.equal(s.ui.proactive.enabled, false, '重读后回到服务端的值')
  s.ui.proactive.quietStart = ''
  await s.ui.saveProactive()
  assert.equal(h.puts.length, 1, '时段没填完整不发请求')
  s.stop()
})

test('settings: the block is a folded <details> whose body (and its switch) only exists once opened; copy is quiet', async () => {
  const src = settings.source
  assert.match(src, /<details class="rt-more rt-proactive" data-testid="proactive-settings" @toggle="onProactiveToggle">\s*<summary>知君主动找我<\/summary>\s*<div v-if="proactiveOpen" class="rt-form rt-proactive__form">/)
  assert.equal((src.match(/role="switch"/g) || []).length, 2, '原有提醒开关 + 折叠区里的一个')
  assert.equal((src.match(/getNudgePolicy\(/g) || []).length, 1, '策略只在 loadNudgePolicy 里读')
  assert.match(src, /onMounted\(\(\) => \{\n  void loadNudgePolicy\(\)\n  void loadMemoryPolicy\(\)/, '挂载时的读取没有增加')
  for (const copy of ['让知君主动发起对话', '只在有理由的时候：到期的回访、你说过要做的事、多处提到等你点头的理解、认识的纪念日。每次都会说明为何现在，你随时可以让它先别找你。', '每天最多几次', '两次之间至少', '安静时段', '多久没聊主动问候', '取消']) {
    assert.ok(src.includes(copy), copy)
  }
  const block = src.slice(src.indexOf('<details class="rt-more rt-proactive"'), src.indexOf('</details>', src.indexOf('<details class="rt-more rt-proactive"')))
  assert.doesNotMatch(block, /—|badge|dot/)
  assert.match(block, /aria-label="安静时段开始"/)
  assert.match(block, /aria-label="安静时段结束"/)
})
