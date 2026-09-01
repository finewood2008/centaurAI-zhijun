// 知君成长闭环前端合同回归：统一 API 边界、Today Top-3 与时区安全提交。
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const api = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')
const router = await readFile(new URL('../src/router/index.ts', import.meta.url), 'utf8')
const sidebar = await readFile(new URL('../src/layouts/AppSidebar.vue', import.meta.url), 'utf8')
const home = await readFile(new URL('../src/pages/HomePage.vue', import.meta.url), 'utf8')
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

assert.match(router, /path: '\/growth'/)
assert.match(router, /title: '今日'/)
assert.match(sidebar, /label: '今日'/)
assert.match(sidebar, /label: '成长'/)
assert.match(sidebar, /问知君/)

assert.match(home, /api\.getGrowthToday\(\)/)
assert.match(home, /api\.getHome\(\)/)
assert.match(home, /Promise\.allSettled\(\[loadToday\(\), loadOverview\(\)\]\)/)
assert.match(home, /today\.value\.todayItems\.slice\(0, 3\)/)
assert.match(home, /stats\.overdueDecisions \+ today\.value\.stats\.dueSoonDecisions/)
assert.doesNotMatch(home, /fetch\(/)

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

console.log('growth-frontend: 29 contract checks OK')
