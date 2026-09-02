// 知君 P1 信息架构回归：路由、侧栏四入口、API 边界与 SSE 客户端接线（源码文本断言）。
// 运行：node --experimental-strip-types tests/zhijun-routes.test.mjs
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const router = await readFile(new URL('../src/router/index.ts', import.meta.url), 'utf8')
const sidebar = await readFile(new URL('../src/layouts/AppSidebar.vue', import.meta.url), 'utf8')
const api = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')
const sse = await readFile(new URL('../src/services/sse.ts', import.meta.url), 'utf8')
const conversation = await readFile(new URL('../src/pages/ConversationPage.vue', import.meta.url), 'utf8')
const relationshipMap = await readFile(new URL('../src/components/today/RelationshipMap.vue', import.meta.url), 'utf8')
const relationshipTimeline = await readFile(new URL('../src/components/today/RelationshipTimeline.vue', import.meta.url), 'utf8')
const homeNodePanel = await readFile(new URL('../src/components/today/HomeNodePanel.vue', import.meta.url), 'utf8')
const selfMap = await readFile(new URL('../src/components/ontology/SelfMap.vue', import.meta.url), 'utf8')
const ontologyPage = await readFile(new URL('../src/pages/OntologyPage.vue', import.meta.url), 'utf8')
const ontologyLabels = await readFile(new URL('../src/shared/ontology.ts', import.meta.url), 'utf8')
const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))

// 路由：今日首屏 + 四入口 + 会话详情；旧问答/生成/治理/纠错页不再存在
for (const path of ["path: '/'", "path: '/chat'", "path: '/c/:conversationId'", "path: '/me'", "path: '/me/inbox'", "path: '/judgments'", "path: '/data'"]) {
  assert.ok(router.includes(path), `router 缺少 ${path}`)
}
assert.match(router, /path: '\/', name: 'today', component: \(\) => import\('@\/pages\/TodayPage\.vue'\), meta: \{ title: '今日' \}/)
assert.match(router, /path: '\/chat', name: 'conversation', component: \(\) => import\('@\/pages\/ConversationPage\.vue'\), meta: \{ title: '对话' \}/)
assert.match(router, /path: '\/c\/:conversationId', name: 'conversation-detail'/)

// 今日页：来信、共同地图、节点详情与轨迹统一由专用首页接口驱动；刷新保留当前内容。
const today = await readFile(new URL('../src/pages/TodayPage.vue', import.meta.url), 'utf8')
for (const part of ['RelationshipMap', 'RelationshipTimeline', 'HomeNodePanel']) {
  assert.ok(today.includes(`import ${part} from`), `TodayPage 缺少 ${part}`)
}
assert.match(today, /getZhijunHome\(\)/)
assert.match(today, /1500/)
assert.match(today, /brief\.status !== 'refreshing'/)
assert.match(today, /path: '\/chat', query: \{ onboarding: '1' \}/)
assert.match(today, /createConversation\(\{ mode: 'review', decisionId: action\.targetId \}\)/)
assert.match(today, /这封来信的依据/)
assert.match(today, /现在最值得做的一件事/)
assert.match(today, /知君 · 今日来信/)
assert.match(today, /grid-template-areas: "letter" "map" "panel"/)
assert.doesNotMatch(today, /fetch\(/)
for (const label of ['知君记得', '一起跟进', '等你确认']) assert.match(relationshipMap, new RegExp(label))
assert.match(relationshipMap, /aspect-ratio: 1/)
assert.match(relationshipMap, /prefers-reduced-motion/)
assert.match(relationshipTimeline, /我们一起走过/)
assert.match(homeNodePanel, /surface: 'today'/)
assert.match(homeNodePanel, /createConversation\(\{ mode: 'review', decisionId: item\.id \}\)/)
// 对话页与今日页的接缝：?say= / ?deliberate=1 / ?onboarding=1 三个 query 都被读取；空白态跳转走 /chat，不再 import 提醒条与下一步面板
assert.match(conversation, /route\.query\.say/)
assert.match(conversation, /route\.query\.deliberate/)
assert.match(conversation, /route\.query\.onboarding/)
assert.doesNotMatch(conversation, /import (NudgeStrip|NextStepsPanel) from/)
assert.doesNotMatch(conversation, /router\.(push|replace)\('\/'\)/)
// 成果回执能跨页面重开；本体边界说明移出图内，避免顶端碰撞。
assert.match(conversation, /turnOutcomes\.value = null[\s\S]*refreshOutcomes\(id, true\)/)
assert.match(selfMap, /v-if="compact"[\s\S]*zj-map__ring-label--boundary/)
assert.match(selfMap, /class="zj-map__boundary-note"/)
assert.match(selfMap, /我确认过的/)
assert.match(selfMap, /知君的推测/)
assert.match(ontologyPage, /属于你的自我理解档案。知君可以提出理解，由你决定什么留下。/)
for (const label of ['亲口陈述', '资料观察', '知君推测', '理想方向']) assert.match(ontologyLabels, new RegExp(label))
// 其它页「去对话」类跳转都指向 /chat，不再落到今日页
for (const rel of ['../src/components/ontology/SelfMap.vue', '../src/pages/OntologyPage.vue', '../src/pages/DataHubPage.vue']) {
  const src = await readFile(new URL(rel, import.meta.url), 'utf8')
  assert.doesNotMatch(src, /to="\/"|router\.push\('\/'\)/, `${rel} 仍有指向旧首页的跳转`)
}
const nudgeStrip = await readFile(new URL('../src/components/conversation/NudgeStrip.vue', import.meta.url), 'utf8')
assert.match(nudgeStrip, /path: '\/chat', query: \{ say: text \}/)
assert.match(nudgeStrip, /showAll\?: boolean/)
assert.match(router, /path: '\/growth', redirect: '\/judgments'/)
for (const gone of ["'/qa'", "'/generate'", "'/governance'", "'/corrections'", 'QaPage', 'GovernancePage', 'GeneratePage', 'CorrectionsPage']) {
  assert.ok(!router.includes(gone), `router 不应再引用 ${gone}`)
}

// 侧栏：单组五项，今日在最上
for (const label of ["label: '今日'", "label: '对话'", "label: '我的本体'", "label: '判断'", "label: '资料与边界'"]) {
  assert.ok(sidebar.includes(label), `sidebar 缺少 ${label}`)
}
assert.doesNotMatch(sidebar, /问知君|本体治理|logo\.jpg/)
assert.match(sidebar, /ws-sidebar__seal/)
assert.ok(sidebar.indexOf("label: '今日'") < sidebar.indexOf("label: '对话'"), '侧栏「今日」应在「对话」上方')

// 今日页的纯函数：问候行 / 汇总句 / 称呼 / 最近留下的 / 相对时间
const labels = await import('../src/shared/labels.ts')
{
  const wed = new Date(2026, 8, 2, 10, 0, 0)
  assert.equal(labels.greetingLine('阿远', wed), '阿远，9月2日 周三。')
  assert.equal(labels.greetingLine('', wed), '9月2日 周三。')
  assert.equal(labels.todaySummaryLine({ dueReview: 2, inbox: 3 }), '有 2 件事到了回访的时候 · 3 条理解等你点头')
  assert.equal(labels.todaySummaryLine({ dueCommitments: 1 }), '1 个承诺到期了')
  assert.equal(labels.todaySummaryLine({}), '今天没有要催你的事。')
  assert.equal(labels.todaySummaryLine({ pendingReviews: 1, nudges: 1 }), '1 个判断记了结果，可以复盘')
  assert.equal(labels.todaySummaryLine({ nudges: 2 }), '有 2 件事想和你聊聊')
  assert.equal(labels.nicknameFromClaims([{ section: 'who', content: '叫我阿远就行', trustState: 'confirmed' }]), '阿远')
  assert.equal(labels.nicknameFromClaims([{ section: 'who', content: '偏好被称呼为「小周」', trustState: 'confirmed' }]), '小周')
  assert.equal(labels.nicknameFromClaims([{ section: 'who', content: '叫我阿远', trustState: 'working' }]), '')
  assert.equal(labels.nicknameFromClaims([{ section: 'matters', content: '叫我阿远', trustState: 'confirmed' }]), '')
  assert.equal(labels.nicknameFromClaims([{ section: 'who', content: '是一家小公司的创始人', trustState: 'confirmed' }]), '')
  const convs = [
    { id: 'a', updatedAt: '2026-09-01T00:00:00Z', outcomes: { confirmed: 0, working: 0, decision: false, commitments: 0 } },
    { id: 'b', updatedAt: '2026-09-02T00:00:00Z', outcomes: { confirmed: 2 } },
    { id: 'c', updatedAt: '2026-08-30T00:00:00Z', lastMessageAt: '2026-09-03T00:00:00Z', outcomes: { decision: true } },
    { id: 'd', updatedAt: '2026-08-01T00:00:00Z', outcomes: null },
    { id: 'e', updatedAt: '2026-08-20T00:00:00Z', outcomes: { commitments: 1 } },
    { id: 'f', updatedAt: '2026-08-10T00:00:00Z', outcomes: { working: 1 } },
  ]
  assert.deepEqual(labels.recentOutcomeConversations(convs, 3).map((c) => c.id), ['c', 'b', 'e'])
  const now = new Date(2026, 8, 3, 12, 0, 0)
  assert.equal(labels.relativeTime(new Date(2026, 8, 3, 11, 59, 30).toISOString(), now), '刚刚')
  assert.equal(labels.relativeTime(new Date(2026, 8, 3, 11, 30, 0).toISOString(), now), '30 分钟前')
  assert.equal(labels.relativeTime(new Date(2026, 8, 3, 8, 0, 0).toISOString(), now), '4 小时前')
  assert.equal(labels.relativeTime(new Date(2026, 8, 2, 23, 0, 0).toISOString(), now), '昨天')
  assert.equal(labels.relativeTime(new Date(2026, 7, 30, 12, 0, 0).toISOString(), now), '4 天前')
  assert.equal(labels.relativeTime(new Date(2026, 6, 1, 12, 0, 0).toISOString(), now), '7月1日')
  assert.equal(labels.relativeTime(new Date(2025, 6, 1, 12, 0, 0).toISOString(), now), '2025年7月1日')
  assert.equal(labels.relativeTime('not-a-date', now), '')
}

// API 边界：对话 / 本体 / 状态
for (const endpoint of [
  '/mindos/conversations',
  '/mindos/ontology/stats',
  '/mindos/ontology/claims',
  '/mindos/ontology/inbox',
  '/mindos/ontology/entities',
  '/mindos/ontology/projection',
  '/mindos/zhijun/status',
  '/mindos/zhijun/home',
]) {
  assert.ok(api.includes(endpoint), `api 缺少 ${endpoint}`)
}
assert.match(api, /export function buildHeaders/)
assert.match(api, /export async function throwApiError/)
assert.match(api, /export function reviewClaim/)
assert.match(api, /export type ReviewSurface = 'conversation' \| 'ontology_page' \| 'onboarding' \| 'today'/)

// SSE 客户端：fetch + ReadableStream，复用统一头部；不用 EventSource
assert.match(sse, /export async function streamPost/)
assert.match(sse, /buildHeaders\(\)/)
assert.match(sse, /getReader\(\)/)
assert.doesNotMatch(sse, /new EventSource/)
assert.match(conversation, /streamPost\(/)
assert.match(conversation, /surface: current\.value\.mode === 'onboarding' \? 'onboarding' : 'conversation'/)
assert.doesNotMatch(conversation, /fetch\(/)

// markdown-it 已声明为依赖
assert.ok(pkg.dependencies['markdown-it'], 'package.json 缺少 markdown-it')

console.log('zhijun-routes: 60+ contract checks OK')
