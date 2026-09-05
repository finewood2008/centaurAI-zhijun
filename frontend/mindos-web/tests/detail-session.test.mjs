// P14-03 资料切换期间异步防串台回归测试（node --experimental-strip-types 运行）。
//
// 用真实 createSessionGate 模拟与组件一致的 loadDetail / reparseMaterial 判定：
// 1. 资料 A 的详情请求延迟返回、已切到资料 B → A 不得覆盖 B 的详情 / 关联 / 轮询；
// 2. 在 A 上点击“重新解析”，请求未返回前切到 B → 不得把 B 的四项分析改为 pending、不得为 B 启动轮询。
//
// 运行：node --experimental-strip-types tests/detail-session.test.mjs
import assert from 'node:assert/strict'
import { createSessionGate } from '../src/composables/sessionGate.ts'

function deferred() {
  let resolve
  const promise = new Promise((res) => {
    resolve = res
  })
  return { promise, resolve }
}

async function testDelayedDetailResponseDoesNotOverwriteNewMaterial() {
  const gate = createSessionGate()
  const applied = [] // 成功写入详情（含关联内容）的资料
  const pollsStarted = [] // 启动的摘要轮询目标
  let currentMaterial = null // 当前路由指向的资料

  // 与组件 loadDetail 一致的写入判定：请求代次最新 且 路由仍指向请求的资料
  const loadDetail = async (materialId, fetchDetail) => {
    const requestSession = gate.next()
    const result = await fetchDetail(materialId)
    if (gate.isCurrent(requestSession) && currentMaterial === materialId) {
      applied.push(materialId)
      pollsStarted.push(materialId)
    }
  }

  // 打开资料 A：详情请求延迟不返回
  const aFetch = deferred()
  const loadA = loadDetail('A', () => aFetch.promise)

  // 跳转资料 B：B 正常返回并应用
  currentMaterial = 'B'
  await loadDetail('B', () => Promise.resolve('B'))
  assert.deepEqual(applied, ['B'])
  assert.deepEqual(pollsStarted, ['B'])

  // A 的旧详情请求延迟返回 → 必须被丢弃（代次已换代 / 路由已是 B）
  aFetch.resolve('A')
  await loadA
  assert.deepEqual(applied, ['B'], 'A 的延迟详情不应覆盖 B')
  assert.deepEqual(pollsStarted, ['B'], '不应为 A 重启摘要轮询')
}

async function testRetryTargetFrozenBeforeAwait() {
  const retried = []
  const summaryReset = [] // 被置为 pending 的资料
  const pollsStarted = []
  let current = 'A'
  let resolveRetry

  // 与组件 reparseMaterial 一致：请求前固定资料 ID，返回后若已切换则直接结束
  const reparseMaterial = async () => {
    const materialId = current
    retried.push(materialId)
    await new Promise((res) => {
      resolveRetry = res
    })
    if (current !== materialId) return
    summaryReset.push(materialId)
    pollsStarted.push(materialId)
  }

  const pending = reparseMaterial() // 在 A 上点击重新解析，请求尚未返回
  current = 'B' // 跳转资料 B
  resolveRetry()
  await pending

  assert.deepEqual(retried, ['A'], '后端重新解析的应是发起时的 A')
  assert.deepEqual(summaryReset, [], '不得把 B 的摘要改为 pending')
  assert.deepEqual(pollsStarted, [], '不得为 B 启动轮询')
}

async function testDelayedRelatedResponseDoesNotOverwriteNewMaterial() {
  const gate = createSessionGate()
  const relatedApplied = []
  let detailMaterial = null // 当前详情指向的资料

  // 与组件 loadRelated 一致的写入判定：请求代次最新 且 当前详情仍是请求的资料
  const loadRelated = async (materialId, fetchRelated) => {
    const requestSession = gate.next()
    const result = await fetchRelated(materialId)
    if (gate.isCurrent(requestSession) && detailMaterial === materialId) {
      relatedApplied.push(materialId)
    }
  }

  // 资料 A：关联请求延迟不返回
  const aFetch = deferred()
  const loadA = loadRelated('A', () => aFetch.promise)

  // 切到资料 B：B 的详情已显示，B 的关联正常应用
  detailMaterial = 'B'
  await loadRelated('B', () => Promise.resolve(['B相关']))
  assert.deepEqual(relatedApplied, ['B'])

  // A 的旧关联请求延迟返回 → 不得覆盖 B 的相关内容
  aFetch.resolve(['A相关'])
  await loadA
  assert.deepEqual(relatedApplied, ['B'], 'A 的延迟关联结果不应覆盖 B')
}

async function run() {
  await testDelayedDetailResponseDoesNotOverwriteNewMaterial()
  await testDelayedRelatedResponseDoesNotOverwriteNewMaterial()
  await testRetryTargetFrozenBeforeAwait()
  console.log('detail-session: 3 tests OK')
}

run().catch((err) => {
  console.error(err)
  process.exit(1)
})
