// 回复节奏（纯渲染层，注入 schedule / now）：首句先出、停顿在 [300,600]、未完成尾段不出、追赶 ≤900ms、aborted 全量、reduced-motion 直通、卸载清理。
// 运行：node --experimental-strip-types --test tests/paced-reply.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { createPacer, pausePlan, shouldPassthrough, splitPacedUnits } from '../src/immersive/composables/usePacedReply.ts'

function fakeClock() {
  let t = 0
  let seq = 0
  const timers = new Map()
  const delays = []
  const clock = {
    now: () => t,
    schedule: (fn, ms) => { const id = ++seq; delays.push(ms); timers.set(id, { at: t + ms, fn }); return id },
    cancel: id => { timers.delete(id) },
    delays,
    pending: () => timers.size,
    advance(ms) {
      const target = t + ms
      for (;;) {
        const due = [...timers.entries()].filter(([, x]) => x.at <= target).sort((a, b) => a[1].at - b[1].at)[0]
        if (!due) break
        timers.delete(due[0])
        t = due[1].at
        due[1].fn()
      }
      t = target
    },
  }
  return clock
}

function pacer(clock, extra = {}) {
  const changes = []
  const p = createPacer({ schedule: clock.schedule, cancel: clock.cancel, now: clock.now, onChange: text => changes.push(text), ...extra })
  return { p, changes }
}

test('splitPacedUnits: paragraphs, first sentence, fenced code, unfinished tail', () => {
  assert.deepEqual(splitPacedUnits('第一句。第二句还没完', false), [{ text: '第一句。', done: true }, { text: '第二句还没完', done: false }])
  assert.deepEqual(splitPacedUnits('第一句。', false), [{ text: '第一句。', done: true }], '首句标点已到就算边界')
  assert.deepEqual(splitPacedUnits('段一\n\n段二未完', false), [{ text: '段一\n\n', done: true }, { text: '段二未完', done: false }])
  assert.deepEqual(splitPacedUnits('段一\n\n段二', true), [{ text: '段一\n\n', done: true }, { text: '段二', done: true }])
  assert.deepEqual(splitPacedUnits('- a\n- b\n- c', false).map(u => u.text), ['- a\n', '- b\n', '- c'])
  const fenced = splitPacedUnits('说明如下\n\n```js\nconst a = 1\n```\n\n结尾', false)
  assert.deepEqual(fenced, [{ text: '说明如下\n\n', done: true }, { text: '```js\nconst a = 1\n```\n\n', done: true }, { text: '结尾', done: false }])
  assert.deepEqual(splitPacedUnits('```\nconst a = 1\n', false), [{ text: '```\nconst a = 1\n', done: false }], '没闭合的代码块是一个未完成的单位')
  assert.equal(splitPacedUnits('第一句。第二句。\n\n下一段', true).map(u => u.text).join(''), '第一句。第二句。\n\n下一段', '单位拼回去就是原文')
  assert.deepEqual(splitPacedUnits('', true), [])
})

test('first sentence surfaces first; the unfinished tail stays hidden', () => {
  const clock = fakeClock()
  const { p, changes } = pacer(clock)
  p.update('第一句。第二句还没完', true)
  assert.equal(p.revealed, '第一句。')
  assert.equal(p.pending, true)
  p.update('第一句。第二句还没完，继续在写', true)
  assert.equal(p.revealed, '第一句。', '尾段边界没到就不出')
  clock.advance(2000)
  assert.equal(p.revealed, '第一句。', '等再久也不出没完成的段')
  assert.deepEqual(changes, ['第一句。'])
})

test('pauses between units stay within [300, 600] and depend on the previous unit length', () => {
  const clock = fakeClock()
  const { p } = pacer(clock)
  p.update('段一。\n\n', true)
  assert.equal(p.revealed, '段一。\n\n', '第一个单位立刻出')
  const long = '长'.repeat(500)
  p.update(`段一。\n\n${long}\n\n`, true)
  assert.equal(p.revealed, '段一。\n\n')
  assert.equal(clock.delays.at(-1), pausePlan('段一。\n\n'))
  assert.ok(clock.delays.at(-1) >= 300 && clock.delays.at(-1) <= 600)
  clock.advance(clock.delays.at(-1))
  assert.equal(p.revealed, `段一。\n\n${long}\n\n`)
  p.update(`段一。\n\n${long}\n\n段三。\n\n`, true)
  assert.equal(clock.delays.at(-1), 600, '很长的一段后面停顿封顶 600ms')
  clock.advance(599)
  assert.equal(p.revealed, `段一。\n\n${long}\n\n`)
  clock.advance(1)
  assert.equal(p.revealed, `段一。\n\n${long}\n\n段三。\n\n`)
  for (const d of clock.delays) assert.ok(d >= 300 && d <= 600, `pause ${d} out of range`)
  assert.equal(pausePlan(''), 300)
  assert.equal(pausePlan('x'.repeat(1000)), 600)
})

test('a unit that arrives after the pause already elapsed shows without extra waiting', () => {
  const clock = fakeClock()
  const { p } = pacer(clock)
  p.update('段一\n\n', true)
  clock.advance(1000)
  p.update('段一\n\n段二\n\n', true)
  assert.equal(clock.delays.at(-1), 0)
  clock.advance(0)
  assert.equal(p.revealed, '段一\n\n段二\n\n')
})

test('when the stream ends the rest catches up within 900ms, one unit at a time', () => {
  const clock = fakeClock()
  const { p, changes } = pacer(clock)
  const content = Array.from({ length: 12 }, (_, i) => `第 ${i + 1} 段`).join('\n\n')
  p.update(content, true)
  assert.equal(p.revealed, '第 1 段\n\n')
  p.update(content, false)
  assert.notEqual(p.revealed, content, '流一结束不是立刻全量')
  clock.advance(400)
  assert.notEqual(p.revealed, content, '追赶是分步的')
  assert.ok(changes.length > 2)
  clock.advance(500)
  assert.equal(p.revealed, content, '900ms 内追平')
  assert.equal(p.pending, false)
  assert.equal(clock.pending(), 0)
  p.update(content + '\n\n（出处更新后正文不变）', false)
  assert.equal(p.revealed, content + '\n\n（出处更新后正文不变）', '追平之后直通')
})

test('aborted or error replies show everything at once', () => {
  for (const status of ['aborted', 'error']) {
    const clock = fakeClock()
    const { p } = pacer(clock)
    p.update('段一\n\n段二', true)
    assert.equal(p.revealed, '段一\n\n')
    p.update('段一\n\n段二', false, status)
    assert.equal(p.revealed, '段一\n\n段二', `${status} → 立即全量`)
    assert.equal(clock.pending(), 0)
  }
})

test('reduced motion, hidden page, preference off and non-streaming mount pass straight through', () => {
  const base = { enabled: true, reducedMotion: false, hidden: false, streamingAtMount: true }
  assert.equal(shouldPassthrough(base), false)
  assert.equal(shouldPassthrough({ ...base, reducedMotion: true }), true)
  assert.equal(shouldPassthrough({ ...base, hidden: true }), true)
  assert.equal(shouldPassthrough({ ...base, enabled: false }), true)
  assert.equal(shouldPassthrough({ ...base, streamingAtMount: false }), true)
  const clock = fakeClock()
  const { p } = pacer(clock, { passthrough: true })
  p.update('第一句。第二句还没完', true)
  assert.equal(p.revealed, '第一句。第二句还没完')
  assert.equal(p.passthrough, true)
  assert.equal(clock.pending(), 0)
})

test('remounting mid-stream seeds what is already complete instead of replaying it', () => {
  const clock = fakeClock()
  const { p } = pacer(clock, { seed: true })
  p.update('段一\n\n段二\n\n段三未完', true)
  assert.equal(p.revealed, '段一\n\n段二\n\n')
  assert.equal(clock.pending(), 0)
})

test('dispose cancels pending timers and stops emitting', () => {
  const clock = fakeClock()
  const { p, changes } = pacer(clock)
  p.update('段一\n\n段二\n\n', true)
  assert.equal(clock.pending(), 1)
  p.dispose()
  assert.equal(clock.pending(), 0, '卸载时取消定时器')
  clock.advance(2000)
  p.update('段一\n\n段二\n\n段三\n\n', true)
  assert.deepEqual(changes, ['段一\n\n'])
  assert.equal(clock.pending(), 0)
})
