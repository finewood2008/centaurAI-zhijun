// 知君成长闭环前端合同回归：统一 API 边界、Today Top-3 与时区安全提交。
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const api = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')
const router = await readFile(new URL('../src/router/index.ts', import.meta.url), 'utf8')
const sidebar = await readFile(new URL('../src/layouts/AppSidebar.vue', import.meta.url), 'utf8')
const { existsSync } = await import('node:fs')
const growth = await readFile(new URL('../src/pages/GrowthPage.vue', import.meta.url), 'utf8')

for (const endpoint of [
  '/mindos/growth/charter',
  '/mindos/growth/decisions',
  '/mindos/growth/reviews',
  '/mindos/growth/today',
]) {
  assert.match(api, new RegExp(endpoint.replaceAll('/', '\\/')))
}
assert.match(api, /decisions\/\$\{encodeURIComponent\(decisionId\)\}\/outcome/)
assert.match(api, /charterId: string \| null/)
assert.match(api, /charterVersion: number \| null/)
assert.match(api, /review: GrowthReview \| null/)
assert.match(api, /todayItems: GrowthTodayItem\[\]/)

// IA：/ 是「今日」首屏（TodayPage），/chat 是对话空白态，/c/:id 是具体会话；/growth 重定向到 /judgments，由 GrowthPage 承接；
// 旧的 HomePage 与 /today 路径不再存在；侧栏五入口，今日在最上
assert.match(router, /path: '\/growth', redirect: '\/judgments'/)
assert.match(router, /path: '\/judgments', name: 'judgments', component: \(\) => import\('@\/pages\/GrowthPage\.vue'\), meta: \{ title: '判断' \}/)
assert.match(router, /path: '\/', name: 'today', component: \(\) => import\('@\/pages\/TodayPage\.vue'\), meta: \{ title: '今日' \}/)
assert.match(router, /path: '\/chat', name: 'conversation', component: \(\) => import\('@\/pages\/ConversationPage\.vue'\), meta: \{ title: '对话' \}/)
assert.match(router, /path: '\/c\/:conversationId', name: 'conversation-detail', component: \(\) => import\('@\/pages\/ConversationPage\.vue'\)/)
assert.doesNotMatch(router, /\/today'|HomePage/)
assert.equal(existsSync(new URL('../src/pages/HomePage.vue', import.meta.url)), false)
assert.equal(existsSync(new URL('../src/pages/TodayPage.vue', import.meta.url)), true)
assert.ok(sidebar.indexOf("label: '今日'") < sidebar.indexOf("label: '对话'"), '侧栏「今日」应在「对话」上方')
assert.match(sidebar, /to: '\/', label: '今日'/)
assert.match(sidebar, /to: '\/chat', label: '对话'/)
assert.match(sidebar, /label: '对话'/)
assert.match(sidebar, /label: '判断'/)
assert.match(sidebar, /label: '我的本体'/)

// 判断页：判断簿看板在前，趋势（时间线）折在「查看趋势」里，少于 5 个判断默认收起，展开状态记 localStorage
assert.ok(growth.indexOf('class="board-section"') < growth.indexOf('class="growth-trend"'))
assert.match(growth, /<details v-if="decisions\.length" class="growth-trend" :open="trendOpen"/)
assert.match(growth, /<summary>查看趋势<\/summary>/)
assert.match(growth, /decisions\.value\.length >= 5/)
assert.match(growth, /localStorage\.setItem\(TREND_KEY/)

assert.match(growth, /new Date\(decisionReviewAt\.value\)/)
assert.match(growth, /reviewAt = localDate\.toISOString\(\)/)
assert.match(growth, /api\.saveGrowthCharter/)
assert.match(growth, /api\.createGrowthDecision/)
assert.match(growth, /api\.recordGrowthDecisionOutcome/)
assert.match(growth, /api\.createGrowthReview/)
assert.match(growth, /decision\.review\.lessons/)
assert.match(growth, /error instanceof ApiError && error\.status === 409/)
assert.match(growth, /:loading="charterSaving"/)
assert.match(growth, /:loading="decisionSaving"/)
assert.match(growth, /showCharterForm\.value = response\.currentCharter === null[\s\S]*cancelCharterEdit[\s\S]*applyCharter\(charter\.value\)/)
assert.match(growth, /const saved = await api\.saveGrowthCharter[\s\S]*applyCharter\(saved\)[\s\S]*showCharterForm\.value = false/)
assert.match(growth, /const created = await api\.createGrowthDecision[\s\S]*decisions\.value = \[created[\s\S]*const updated = await api\.recordGrowthDecisionOutcome[\s\S]*replaceDecision\(updated\)[\s\S]*const result = await api\.createGrowthReview[\s\S]*replaceDecision\(result\.decision\)/)
assert.doesNotMatch(growth, /fetch\(/)

console.log('growth-frontend: 40 contract checks OK')
