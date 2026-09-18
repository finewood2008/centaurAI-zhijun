// 一条流的纯函数：按本地日分节（跨天 / 置顶乱序 / 去重）、日头文字、当前块判定（路由优先 / 今日最新 / 排除回访与归档 / 无则 null）。
// 运行：node --experimental-strip-types --test tests/immersive-day-stream.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { activityAt, dayKey, dayLabel, groupByDay, hasOutcomesBrief, mergePage, outcomesLine, pickActiveConversation, withDay } from '../src/immersive/dayStream.ts'

// 2026-09-18 是周五；用本地时间构造，避免时区把「今天」挪到别的日子
const now = new Date(2026, 8, 18, 15, 30)
const iso = (y, m, d, h = 9, min = 0) => new Date(y, m - 1, d, h, min).toISOString()
const conv = (id, createdAt, extra = {}) => ({
  id, title: id, mode: 'chat', status: 'active', pinnedAt: null, metadataRevision: 1, decisionId: null,
  messageCount: 2, createdAt, updatedAt: createdAt, lastMessageAt: createdAt, outcomes: null, ...extra,
})

test('groupByDay: across days, pinned / out-of-order input, dedupe by id, oldest first and today last', () => {
  const items = [
    conv('today-late', iso(2026, 9, 18, 14)),
    conv('lastweek', iso(2026, 9, 11, 10)),
    conv('today-early', iso(2026, 9, 18, 8), { pinnedAt: iso(2026, 9, 18, 9) }),
    conv('yesterday', iso(2026, 9, 17, 22)),
    conv('today-early', iso(2026, 9, 18, 8), { title: 'renamed' }),
  ]
  const groups = groupByDay(items, now)
  assert.deepEqual(groups.map(g => g.key), ['2026-09-11', '2026-09-17', '2026-09-18'])
  assert.deepEqual(groups.at(-1).items.map(i => i.id), ['today-early', 'today-late'], '同一天内按创建时间从早到晚，置顶不影响顺序')
  assert.equal(groups.at(-1).items[0].title, 'renamed', '同 id 后来的覆盖先前的')
  assert.equal(groups.at(-1).isToday, true)
  assert.equal(groups[0].isToday, false)
  assert.deepEqual(groups.map(g => g.label), ['9月11日 · 周五', '昨天 · 9月17日 周四', '今天 · 9月18日 周五'])
  assert.deepEqual(groupByDay([], now), [])
})

test('groupByDay: unparsable or future createdAt lands on today', () => {
  const groups = groupByDay([conv('future', iso(2026, 9, 25)), conv('broken', 'not-a-date')], now)
  assert.equal(groups.length, 1)
  assert.equal(groups[0].key, '2026-09-18')
  assert.equal(groups[0].isToday, true)
})

test('dayLabel: today / yesterday / same year / other year', () => {
  assert.equal(dayLabel(new Date(2026, 8, 18, 1), now), '今天 · 9月18日 周五')
  assert.equal(dayLabel(new Date(2026, 8, 17, 23, 59), now), '昨天 · 9月17日 周四')
  assert.equal(dayLabel(new Date(2026, 8, 11), now), '9月11日 · 周五')
  assert.equal(dayLabel(new Date(2025, 11, 31), now), '2025年12月31日 · 周三')
  assert.equal(dayLabel(new Date(2025, 11, 31), new Date(2026, 0, 1, 8)), '昨天 · 2025年12月31日 周三', '跨年时的昨天带年份')
  assert.equal(dayKey(new Date(2026, 0, 5)), '2026-01-05')
})

test('pickActiveConversation: route first, then the latest active chat created today, else null', () => {
  const items = [
    conv('yesterday-chat', iso(2026, 9, 17, 20), { lastMessageAt: iso(2026, 9, 18, 13) }),
    conv('today-review', iso(2026, 9, 18, 10), { mode: 'review' }),
    conv('today-onboarding', iso(2026, 9, 18, 10), { mode: 'onboarding' }),
    conv('today-archived', iso(2026, 9, 18, 11), { status: 'archived', lastMessageAt: iso(2026, 9, 18, 15) }),
    conv('today-a', iso(2026, 9, 18, 8), { lastMessageAt: iso(2026, 9, 18, 12) }),
    conv('today-b', iso(2026, 9, 18, 9), { lastMessageAt: iso(2026, 9, 18, 9, 30) }),
  ]
  assert.equal(pickActiveConversation(items, now, 'today-review').id, 'today-review', '路由指向的会话优先，哪怕是回访')
  assert.equal(pickActiveConversation(items, now, 'missing'), null, '路由指向的会话不在列表里 → null，由对话页自己加载')
  assert.equal(pickActiveConversation(items, now, null).id, 'today-a', '今天创建的 chat 里最近活动的一条')
  assert.equal(pickActiveConversation(items.filter(i => !i.id.startsWith('today-a') && i.id !== 'today-b'), now, null), null, '回访 / 建档 / 归档 / 昨天的都不算')
  assert.equal(pickActiveConversation([], now, null), null)
})

test('activityAt, mergePage, withDay and the outcomes line', () => {
  assert.equal(activityAt(conv('x', iso(2026, 9, 1), { updatedAt: iso(2026, 9, 2), lastMessageAt: iso(2026, 9, 3) })), new Date(2026, 8, 3, 9).valueOf())
  assert.equal(activityAt(conv('x', iso(2026, 9, 1), { updatedAt: null, lastMessageAt: null })), new Date(2026, 8, 1, 9).valueOf())
  assert.equal(activityAt({ createdAt: 'bad', updatedAt: 'bad', lastMessageAt: null }), 0)

  const merged = mergePage([conv('a', iso(2026, 9, 1)), conv('b', iso(2026, 9, 2))], [conv('b', iso(2026, 9, 2), { title: 'b2' }), conv('c', iso(2026, 9, 3))])
  assert.deepEqual(merged.map(i => i.id), ['a', 'b', 'c'])
  assert.equal(merged[1].title, 'b2')

  const groups = withDay(groupByDay([conv('old', iso(2026, 9, 11))], now), now, now)
  assert.deepEqual(groups.map(g => g.key), ['2026-09-11', '2026-09-18'])
  assert.equal(groups[1].items.length, 0)
  assert.equal(withDay(groups, now, now), groups, '已有那一天就原样返回')
  assert.deepEqual(withDay(groups, new Date(2026, 8, 15), now).map(g => g.key), ['2026-09-11', '2026-09-15', '2026-09-18'])

  assert.equal(outcomesLine({ confirmed: 1, working: 1, decision: true, commitments: 2 }), '记下 2 条 · 1 个判断 · 2 个承诺')
  assert.equal(outcomesLine({ confirmed: 0, working: 0, decision: false, commitments: 0 }), '')
  assert.equal(outcomesLine(null), '')
  assert.equal(hasOutcomesBrief({ confirmed: 0, working: 0, decision: true, commitments: 0 }), true)
  assert.equal(hasOutcomesBrief({ confirmed: 0, working: 0, decision: false, commitments: 0 }), false)
  assert.equal(hasOutcomesBrief(undefined), false)
})
