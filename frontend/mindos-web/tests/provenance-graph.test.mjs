// 回复出处小图的数据整形回归：分组、上限、线宽、空态。
// 运行：node --experimental-strip-types tests/provenance-graph.test.mjs
import assert from 'node:assert/strict'
import { MAX_SHOWN, groups, isEmpty, lineWidth, truncateTitle } from '../src/shared/provenanceGraph.ts'

const claim = (i, over = {}) => ({ id: `clm_${i}`, content: `理解 ${i}`, section: 'matters', layer: 'self_declared', ...over })

// 1) 四组齐全、顺序固定、计数正确
{
  const g = groups({
    confirmedClaims: [claim(1), claim(2, { layer: 'observed' })],
    workingClaims: [claim(3, { layer: 'hypothesis' })],
    materials: [{ materialId: 'mat_1', title: '远川项目合同（2026 版）' }],
    retractedNotices: 2,
    charterVersion: null,
    promptChars: 100,
  })
  assert.deepEqual(g.map((x) => x.key), ['confirmed', 'working', 'materials', 'retracted'])
  assert.equal(g[0].count, 2)
  assert.equal(g[1].note, '带保留语气')
  assert.equal(g[2].items[0].materialId, 'mat_1')
  assert.equal(g[3].count, 2)
  assert.match(g[3].items[0].label, /避开 2 条/)
}

// 2) 空组省略；全空 → isEmpty
{
  const g = groups({ confirmedClaims: [], workingClaims: [claim(1)], materials: [], retractedNotices: 0, charterVersion: null, promptChars: 0 })
  assert.deepEqual(g.map((x) => x.key), ['working'])
  assert.equal(isEmpty({ confirmedClaims: [], workingClaims: [], materials: [], retractedNotices: 0, charterVersion: null, promptChars: 0 }), true)
  assert.equal(isEmpty(null), true)
}

// 3) 最多显示 8 个，其余计入 extra
{
  const many = Array.from({ length: 11 }, (_, i) => claim(i))
  const g = groups({ confirmedClaims: many, workingClaims: [], materials: [], retractedNotices: 0, charterVersion: null, promptChars: 0 })[0]
  assert.equal(g.shown.length, MAX_SHOWN)
  assert.equal(g.extra, 3)
  assert.equal(g.count, 11)
}

// 4) 线宽 1–3，随数量增长；标题截断
{
  assert.equal(lineWidth(0), 1)
  assert.equal(lineWidth(1), 1)
  assert.equal(lineWidth(3), 2)
  assert.equal(lineWidth(40), 3)
  assert.equal(truncateTitle('远川项目合同（2026 版）', 10), '远川项目合同（202…')
  assert.equal(truncateTitle('短标题'), '短标题')
}

console.log('provenance-graph: 4 groups OK')
