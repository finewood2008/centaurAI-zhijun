// 判断时间线纯逻辑回归：时间域、比例尺、刻度、布点与碰撞下移、摘要。
// 运行：node --experimental-strip-types tests/timeline.test.mjs
import assert from 'node:assert/strict'
import { CHART, COLLISION_STEP, domain, dotPositions, formatTick, isOverdue, plotWidth, statusText, summary, ticks, timeScale } from '../src/shared/timeline.ts'

const NOW = Date.parse('2026-09-02T12:00:00Z')
const day = 24 * 3600 * 1000
const dec = (over = {}) => ({
  id: 'dec_1',
  title: '要不要涨价',
  status: 'open',
  confidence: 60,
  createdAt: new Date(NOW - 10 * day).toISOString(),
  reviewAt: new Date(NOW + 5 * day).toISOString(),
  ...over,
})

// 1) 比例尺：线性、端点落在 0/width
{
  const s = timeScale(0, 100, 500)
  assert.equal(s(0), 0)
  assert.equal(s(100), 500)
  assert.equal(s(50), 250)
}

// 2) 时间域：最早 createdAt → max(reviewAt, now)，带 5% 留白；空列表也至少一天
{
  const d = domain([dec(), dec({ id: 'dec_2', createdAt: new Date(NOW - 30 * day).toISOString(), reviewAt: null })], NOW)
  assert.ok(d.min < NOW - 30 * day)
  assert.ok(d.max > NOW + 5 * day)
  const empty = domain([], NOW)
  assert.ok(empty.max - empty.min >= day)
}

// 3) 刻度：4–8 个整天，落在域内，格式 M/D
{
  const t = ticks(NOW - 20 * day, NOW + 5 * day, 5)
  assert.ok(t.length >= 4 && t.length <= 8, `ticks=${t.length}`)
  assert.ok(t.every((x) => x >= NOW - 20 * day && x <= NOW + 5 * day))
  assert.match(formatTick(t[0]), /^\d{1,2}\/\d{1,2}$/)
}

// 4) 布点：y 随把握线性；100 在顶，0 在底；x 在绘图区内；逾期与回访线
{
  const dots = dotPositions([dec({ confidence: 100 }), dec({ id: 'dec_low', confidence: 0, createdAt: new Date(NOW - 5 * day).toISOString() })], NOW)
  const hi = dots.find((d) => d.id === 'dec_1')
  const lo = dots.find((d) => d.id === 'dec_low')
  assert.equal(hi.y, CHART.top)
  assert.ok(lo.y > hi.y)
  assert.ok(hi.x >= CHART.left && hi.x <= CHART.left + plotWidth())
  assert.equal(hi.overdue, false)
  assert.ok(hi.reviewX > hi.x)
  const over = dotPositions([dec({ reviewAt: new Date(NOW - day).toISOString() })], NOW)[0]
  assert.equal(over.overdue, true)
  assert.equal(isOverdue({ status: 'reviewed', reviewAt: new Date(NOW - day).toISOString() }, NOW), false)
}

// 5) 碰撞：同时间同把握的两个点，后一个下移 8px；结果确定
{
  const a = dec({ id: 'a' })
  const b = dec({ id: 'b' })
  const dots = dotPositions([a, b], NOW)
  const da = dots.find((d) => d.id === 'a')
  const db = dots.find((d) => d.id === 'b')
  assert.equal(da.offset, 0)
  assert.equal(db.offset, COLLISION_STEP)
  assert.equal(db.y - da.y, COLLISION_STEP)
  assert.deepEqual(dotPositions([b, a], NOW).map((d) => [d.id, d.offset]), dots.map((d) => [d.id, d.offset]))
}

// 6) 摘要与状态文案
{
  const s = summary([dec({ confidence: 60 }), dec({ id: 'x', confidence: 80, status: 'reviewed' }), dec({ id: 'y', reviewAt: new Date(NOW - day).toISOString() })], NOW)
  assert.deepEqual(s, { count: 3, avgConfidence: 67, overdue: 1 })
  assert.equal(statusText('open'), '等结果')
  assert.equal(statusText('outcome_recorded'), '已记结果')
  assert.equal(statusText('reviewed'), '已复盘')
}

console.log('timeline: 6 groups OK')
