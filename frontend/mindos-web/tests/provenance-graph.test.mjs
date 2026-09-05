// 回复出处小图的数据整形回归：分组、上限、线宽、空态。
// 运行：node --experimental-strip-types tests/provenance-graph.test.mjs
import assert from 'node:assert/strict'
import { MAX_SHOWN, groups, isEmpty, lineWidth, normalizeProvenance, provenanceCharterSummary, provenanceMemorySummary, truncateTitle } from '../src/shared/provenanceGraph.ts'

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

// 5) 旧回执或中断的流缺少集合字段时，展示层统一补为空集合
{
  const normalized = normalizeProvenance({ promptChars: 12 })
  assert.deepEqual(normalized.confirmedClaims, [])
  assert.deepEqual(normalized.workingClaims, [])
  assert.deepEqual(normalized.materials, [])
  assert.deepEqual(normalized.pastDecisions, [])
  assert.deepEqual(normalized.anchorClaimIds, [])
  assert.equal(normalized.retractedNotices, 0)
  assert.equal(normalized.promptChars, 12)
}

const memory = (over = {}) => ({ intent: 'conversation', directCount: 0, inheritedCount: 0, excludedCount: 0, status: 'none', searched: true, ...over })

// 6) 旧回执统一保守描述，不从已授权或旧计数推断实际提供/引用。
{
  const empty = '旧回执未记录可核验的信息提供与引用'
  assert.equal(provenanceMemorySummary({}), empty)
  assert.equal(provenanceMemorySummary({ confirmedClaims: [claim(1)] }), '旧回执记录了 1 项关联信息 · 未区分提供与明确引用')
  assert.equal(provenanceMemorySummary({ memoryContext: memory() }), empty)
  assert.equal(provenanceMemorySummary({ memoryContext: memory({ searched: false }) }), empty)
  assert.equal(provenanceMemorySummary({ memoryContext: memory({ intent: 'charter', searched: false }) }), empty)
  assert.equal(provenanceMemorySummary({
    routing: { sources: [{ kind: 'claim', authorization: { allowed: true } }] },
  }), empty, '允许外发不等于本轮实际使用')
}

// 7) 旧来源链只能说明关联，不标成重新读取或引用。
{
  assert.equal(provenanceMemorySummary({
    confirmedClaims: [claim(1)], workingClaims: [claim(2)],
    memoryContext: memory({ status: 'direct', directCount: 2 }),
  }), '旧回执记录了 2 项关联信息 · 未区分提供与明确引用')
  const inherited = { memoryContext: memory({ status: 'inherited', inheritedCount: 2 }) }
  assert.equal(provenanceMemorySummary(inherited), '旧回执保留了历史来源关联 · 不代表本轮读取或引用')
  assert.match(provenanceMemorySummary({
    confirmedClaims: [claim(1)], memoryContext: memory({ status: 'direct', directCount: 1, inheritedCount: 2 }),
  }), /旧回执记录了 1 项关联信息/)
}

// 8) 被权限或来源生命周期挡住的相关理解不等同于未检索到，也不伪装为已使用。
{
  const summary = provenanceMemorySummary({ memoryContext: memory({ status: 'restricted', excludedCount: 2 }) })
  assert.equal(summary, '旧回执记录了 2 项未纳入信息')
  assert.doesNotMatch(summary, /未检索到|参考了/)
  assert.match(provenanceMemorySummary({
    confirmedClaims: [claim(1)], memoryContext: memory({ status: 'direct', directCount: 1, excludedCount: 2 }),
  }), /旧回执记录了 1 项关联信息/)
}

// 9) 章程约定与提供/引用分开，不给旧回执追加新含义。
{
  const full = normalizeProvenance({ charterVersion: 3, memoryContext: memory({ intent: 'charter', charterChecked: true, charterComplete: true }) })
  assert.equal(provenanceCharterSummary(full), '旧回执关联人生章程第 3 版')
  assert.equal(provenanceMemorySummary(full), '旧回执保留了章程记录 · 未区分信息提供与引用')
  const partial = normalizeProvenance({ charterVersion: 3, memoryContext: memory({ intent: 'charter', charterChecked: true, charterComplete: false }) })
  assert.equal(provenanceMemorySummary(partial), '旧回执保留了章程记录 · 未区分信息提供与引用')
  assert.doesNotMatch(provenanceMemorySummary(partial), /已核对/)
  assert.equal(provenanceMemorySummary({ charterVersion: 1 }), '旧回执保留了章程记录 · 未区分信息提供与引用')
  assert.equal(provenanceMemorySummary({ memoryContext: memory({ intent: 'charter', charterChecked: true, charterComplete: true }) }), '旧回执保留了章程记录 · 未区分信息提供与引用')
  assert.equal(provenanceCharterSummary(normalizeProvenance({ charterBasis: { version: 4, clauseIds: ['c1'] } })), '遵循人生章程第 4 版 · 1 条约定')
}

// 10) 刷新时保留新的记忆回执，损坏或未知字段不制造 NaN/负数/虚假的使用说明。
{
  const expected = memory({ status: 'inherited', inheritedCount: 2, charterChecked: false, charterComplete: false })
  assert.deepEqual(normalizeProvenance({ memoryContext: expected }).memoryContext, expected)
  const normalized = normalizeProvenance({ memoryContext: memory({ directCount: -3, inheritedCount: Infinity, excludedCount: '2.9' }) })
  assert.equal(normalized.memoryContext.directCount, 0)
  assert.equal(normalized.memoryContext.inheritedCount, 0)
  assert.equal(normalized.memoryContext.excludedCount, 2)
  assert.equal(normalizeProvenance({ memoryContext: { status: 'unknown' } }).memoryContext, undefined)
  assert.equal(provenanceMemorySummary({ memoryContext: memory({ status: 'direct' }) }), '旧回执未记录可核验的信息提供与引用')
}

console.log('provenance-graph: 10 groups OK')
