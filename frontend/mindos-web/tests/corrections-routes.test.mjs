// P14-12 纠错/来源跳转路径构造回归测试（node --experimental-strip-types 运行）。
//
// 覆盖：问答提醒「查看来源」按类型跳转（material / knowledge）、「查看纠错记录」
// 跳转纠错本并携带 correctionId 定位参数。
//
// 运行：node --experimental-strip-types tests/corrections-routes.test.mjs
import assert from 'node:assert/strict'
import { sourceRoute, correctionDetailRoute } from '../src/shared/routes.ts'

async function testSourceRouteByType() {
  assert.equal(sourceRoute('mindos_a1b2'), '/materials/mindos_a1b2', 'material 来源应跳转原材料')
  assert.equal(sourceRoute('knowledge_xyz'), '/knowledge/knowledge_xyz', 'knowledge 来源应跳转知识卡片')
}

async function testCorrectionDetailRoute() {
  assert.equal(
    correctionDetailRoute('corr_abc123'),
    '/corrections?correctionId=corr_abc123',
    '查看纠错记录应跳转纠错本并携带 correctionId 定位参数',
  )
}

async function run() {
  await testSourceRouteByType()
  await testCorrectionDetailRoute()
  console.log('corrections-routes: 2 tests OK')
}

run().catch((err) => {
  console.error(err)
  process.exit(1)
})
