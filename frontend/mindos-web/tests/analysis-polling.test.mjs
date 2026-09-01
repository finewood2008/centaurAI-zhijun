// P14-04 分析轮询器回归测试（node --experimental-strip-types 运行）。
//
// 核心回归：资料 A 的在途分析请求延迟返回、用户已切换到资料 B 时，
// A 的结果不得覆盖 B（session token 防串台）；轮询在「摘要、标签候选、实体、关系」
// 都离开 pending 后停止并写回结果。
//
// 运行：node --experimental-strip-types tests/analysis-polling.test.mjs
import assert from 'node:assert/strict'
import { createAnalysisPoller } from '../src/composables/useAnalysisPolling.ts'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function deferred() {
  let resolve
  const promise = new Promise((res) => {
    resolve = res
  })
  return { promise, resolve }
}

// 构造一次 /analysis 轮询响应（seq 用于给同一资料多次结果排序）
const makeFetched = (materialId, seq, tagStatus, entityStatus, relationStatus = 'ok') => ({
  materialId,
  summary: { text: '', status: 'ok', generatedAt: null },
  tagSuggestions: { status: tagStatus, items: [], generatedAt: null },
  entities: { status: entityStatus, items: [], generatedAt: null },
  relations: { status: relationStatus, items: [], generatedAt: null },
})

async function testDelayedResponseDoesNotOverwriteNewMaterial() {
  const deferreds = new Map()
  const results = []
  const poller = createAnalysisPoller({
    fetch: (materialId) => {
      const d = deferred()
      deferreds.set(materialId, d)
      return d.promise
    },
    onResult: (materialId, result) => results.push({ materialId, result }),
    onTimeout: () => {},
    intervalMs: 10,
    timeoutMs: 1000,
  })

  // 资料 A：发出请求但延迟不返回
  poller.start('A')
  await sleep(5)
  assert(deferreds.has('A'), 'A 的请求应已发出')

  // 切到资料 B：start 使 A 的 session 失效，并发出 B 请求
  poller.start('B')
  await sleep(5)
  assert(deferreds.has('B'), 'B 的请求应已发出')

  // A 的旧请求延迟返回（终态 ok）→ 必须被丢弃，不得写回 B
  deferreds.get('A').resolve(makeFetched('A', 0, 'ok', 'ok'))
  await sleep(15)
  assert.deepEqual(results, [], 'A 的延迟结果不应写回')

  // B 正常返回终态 → 只写回 B
  deferreds.get('B').resolve(makeFetched('B', 0, 'ok', 'ok'))
  await sleep(15)
  assert.equal(results.length, 1)
  assert.equal(results[0].materialId, 'B')
  assert.equal(results[0].result.tagSuggestions.status, 'ok')
  assert.equal(results[0].result.entities.status, 'ok')
}

async function testStopInvalidatesInFlight() {
  const deferreds = new Map()
  const results = []
  const poller = createAnalysisPoller({
    fetch: (materialId) => {
      const d = deferred()
      deferreds.set(materialId, d)
      return d.promise
    },
    onResult: (materialId, result) => results.push({ materialId, result }),
    onTimeout: () => {},
    intervalMs: 10,
    timeoutMs: 1000,
  })

  poller.start('A')
  await sleep(5)
  poller.stop() // 仅停止、不新开
  deferreds.get('A').resolve(makeFetched('A', 0, 'ok', 'ok'))
  await sleep(15)
  assert.deepEqual(results, [], 'stop 后在途结果应被丢弃')
}

async function testTimeoutAfterPersistentPending() {
  const timeouts = []
  const poller = createAnalysisPoller({
    fetch: () => Promise.resolve(makeFetched('A', 0, 'pending', 'pending')),
    onResult: () => {
      throw new Error('持续 pending 不应写回结果')
    },
    onTimeout: (materialId) => timeouts.push(materialId),
    intervalMs: 5,
    timeoutMs: 30,
  })

  poller.start('A')
  await sleep(80)
  assert.deepEqual(timeouts, ['A'], '等待预算耗尽应触发 onTimeout')
}

async function testStopsOnlyWhenAllOutputsLeavePending() {
  const calls = []
  const results = []
  const poller = createAnalysisPoller({
    fetch: () => {
      // 第 0 次摘要 pending，即使其余三项已完成也必须继续轮询。
      const i = calls.length
      calls.push(i)
      const fetched = makeFetched('A', i, 'ok', 'ok')
      fetched.summary.status = i === 0 ? 'pending' : 'ok'
      return Promise.resolve(fetched)
    },
    onResult: (materialId, result) => results.push({ materialId, result }),
    onTimeout: () => {},
    intervalMs: 5,
    timeoutMs: 1000,
  })

  poller.start('A')
  await sleep(50)
  assert.equal(results.length, 1, '四项产物都非 pending 后才写回')
  assert.equal(results[0].materialId, 'A')
  assert.equal(calls.length, 2, '挂起中间态后应再轮询一次')
  await sleep(20)
  assert.equal(results.length, 1, '写回终态后不应重复回调')
  assert.equal(calls.length, 2, '写回终态后不应继续轮询')
}

async function run() {
  await testDelayedResponseDoesNotOverwriteNewMaterial()
  await testStopInvalidatesInFlight()
  await testTimeoutAfterPersistentPending()
  await testStopsOnlyWhenAllOutputsLeavePending()
  console.log('analysis-polling: 4 tests OK')
}

run().catch((err) => {
  console.error(err)
  process.exit(1)
})
