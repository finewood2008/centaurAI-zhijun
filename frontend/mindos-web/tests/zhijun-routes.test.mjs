// 知君 P1 信息架构回归：路由、侧栏四入口、API 边界与 SSE 客户端接线（源码文本断言）。
// 运行：node --experimental-strip-types tests/zhijun-routes.test.mjs
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const router = await readFile(new URL('../src/router/index.ts', import.meta.url), 'utf8')
const sidebar = await readFile(new URL('../src/layouts/AppSidebar.vue', import.meta.url), 'utf8')
const api = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')
const sse = await readFile(new URL('../src/services/sse.ts', import.meta.url), 'utf8')
const conversation = await readFile(new URL('../src/pages/ConversationPage.vue', import.meta.url), 'utf8')
const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))

// 路由：四入口 + 会话详情；旧问答/生成/治理/纠错页不再存在
for (const path of ["path: '/'", "path: '/c/:conversationId'", "path: '/me'", "path: '/me/inbox'", "path: '/judgments'", "path: '/data'"]) {
  assert.ok(router.includes(path), `router 缺少 ${path}`)
}
assert.match(router, /path: '\/growth', redirect: '\/judgments'/)
for (const gone of ["'/qa'", "'/generate'", "'/governance'", "'/corrections'", 'QaPage', 'GovernancePage', 'GeneratePage', 'CorrectionsPage']) {
  assert.ok(!router.includes(gone), `router 不应再引用 ${gone}`)
}

// 侧栏：单组四项
for (const label of ["label: '对话'", "label: '我的本体'", "label: '判断'", "label: '资料与边界'"]) {
  assert.ok(sidebar.includes(label), `sidebar 缺少 ${label}`)
}
assert.doesNotMatch(sidebar, /问知君|本体治理|logo\.jpg/)
assert.match(sidebar, /ws-sidebar__seal/)

// API 边界：对话 / 本体 / 状态
for (const endpoint of [
  '/mindos/conversations',
  '/mindos/ontology/stats',
  '/mindos/ontology/claims',
  '/mindos/ontology/inbox',
  '/mindos/ontology/entities',
  '/mindos/ontology/projection',
  '/mindos/zhijun/status',
]) {
  assert.ok(api.includes(endpoint), `api 缺少 ${endpoint}`)
}
assert.match(api, /export function buildHeaders/)
assert.match(api, /export async function throwApiError/)
assert.match(api, /export function reviewClaim/)

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

console.log('zhijun-routes: 30+ contract checks OK')
