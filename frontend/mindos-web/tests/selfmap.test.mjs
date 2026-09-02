// 本体全景布局纯逻辑回归：哈希确定性、环带归属、扇区角度范围、占比。
// 运行：node --experimental-strip-types tests/selfmap.test.mjs
import assert from 'node:assert/strict'
import {
  BANDS,
  SECTOR_ORDER,
  SECTOR_SPREAD_DEG,
  annularSectorPath,
  confirmedFraction,
  hashToUnit,
  nodeAngle,
  nodeBand,
  nodeRadius,
  nodeSize,
  polar,
  sectorCenterDeg,
  sectorIndex,
  truncateLabel,
} from '../src/shared/selfmap.ts'

const NOW = Date.parse('2026-09-02T12:00:00Z')
const day = 24 * 3600 * 1000
const claim = (over = {}) => ({
  id: 'clm_a1',
  section: 'matters',
  trustState: 'confirmed',
  challenged: false,
  lastReaffirmed: new Date(NOW - 3 * day).toISOString(),
  evidence: [{}, {}],
  ...over,
})

// 1) 哈希确定、落在 [0,1)，不同 id 不同
{
  assert.equal(hashToUnit('clm_a1'), hashToUnit('clm_a1'))
  const u = hashToUnit('clm_a1')
  assert.ok(u >= 0 && u < 1)
  assert.notEqual(hashToUnit('clm_a1'), hashToUnit('clm_a2'))
}

// 2) 扇区：六个分区各占 60°，中心角在 -60,0,60,120,180,240；未知分区回落到 0
{
  assert.equal(SECTOR_ORDER.length, 6)
  assert.deepEqual(SECTOR_ORDER.map((_, i) => sectorCenterDeg(i)), [-60, 0, 60, 120, 180, 240])
  assert.equal(sectorIndex('direction'), 5)
  assert.equal(sectorIndex('nope'), 0)
}

// 3) 节点角度落在所属扇区中心 ± SPREAD 内，且确定
{
  for (const section of SECTOR_ORDER) {
    for (let i = 0; i < 50; i += 1) {
      const c = claim({ id: `clm_${section}_${i}`, section })
      const a = nodeAngle(c)
      const center = sectorCenterDeg(sectorIndex(section))
      assert.ok(Math.abs(a - center) <= SECTOR_SPREAD_DEG + 1e-9, `${section} ${a}`)
      assert.equal(a, nodeAngle(c))
    }
  }
}

// 4) 半径按信任状态落在对应环带
{
  const fresh = nodeRadius(claim(), NOW)
  assert.ok(fresh >= BANDS.fresh[0] && fresh <= BANDS.fresh[1], `fresh ${fresh}`)
  assert.equal(nodeBand(claim(), NOW), 'fresh')
  const stale = claim({ lastReaffirmed: new Date(NOW - 90 * day).toISOString() })
  assert.equal(nodeBand(stale, NOW), 'stale')
  const rs = nodeRadius(stale, NOW)
  assert.ok(rs >= BANDS.stale[0] && rs <= BANDS.stale[1], `stale ${rs}`)
  const working = claim({ trustState: 'working' })
  const rw = nodeRadius(working, NOW)
  assert.ok(rw >= BANDS.working[0] && rw <= BANDS.working[1], `working ${rw}`)
  assert.equal(nodeRadius(claim({ trustState: 'working', challenged: true }), NOW), BANDS.challenged)
  // 无效日期不算过期
  assert.equal(nodeBand(claim({ lastReaffirmed: 'not-a-date' }), NOW), 'fresh')
}

// 5) 极坐标：-90° 在正上方，0° 在正右
{
  const top = polar(-90, 100)
  assert.ok(Math.abs(top.x - 360) < 1e-9 && Math.abs(top.y - 260) < 1e-9)
  const right = polar(0, 100)
  assert.ok(Math.abs(right.x - 460) < 1e-9 && Math.abs(right.y - 360) < 1e-9)
  assert.match(annularSectorPath(-90, -30, 60, 330), /^M .* A 330 330 .* L .* A 60 60 .* Z$/)
}

// 6) 节点大小 5–9.5，标签截断，占比
{
  assert.equal(nodeSize({ evidence: [] }), 5)
  assert.equal(nodeSize({ evidence: [1, 2, 3, 4, 5] }), 9.5)
  assert.equal(truncateLabel('远川科技有限责任公司'), '远川科技有限责任…')
  assert.equal(truncateLabel('远川科技有限公司'), '远川科技有限公司')
  assert.equal(truncateLabel('  '), '')
  assert.equal(confirmedFraction(3, 1), 0.75)
  assert.equal(confirmedFraction(0, 0), 0)
}

console.log('selfmap: 6 groups OK')
