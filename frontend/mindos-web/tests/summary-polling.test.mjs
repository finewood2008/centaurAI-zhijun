// P14-03 摘要轮询器回归测试（node --experimental-strip-types 运行）。
//
// 核心回归：资料 A 的在途摘要请求延迟返回、用户已切换到资料 B 时，
// A 的结果不得覆盖 B（session token 防串台）。
//
// 运行：node --experimental-strip-types tests/summary-polling.test.mjs
import assert from 'node:assert/strict'
import { createSummaryPoller } from '../src/composables/useSummaryPolling.ts'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function deferred() {
  let resolve
  const promise = new Promise((res) => {
    resolve = res
  })
  return { promise, resolve }
}

async function testDelayedResponseDoesNotOverwriteNewMaterial() {
  const deferreds = new Map()
  const results = []
  const poller = createSummaryPoller({
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

  // A 的旧请求延迟返回（ok）→ 必须被丢弃，不得写回 B
  deferreds.get('A').resolve({ materialId: 'A', text: 'A摘要', status: 'ok', generatedAt: null })
  await sleep(15)
  assert.deepEqual(results, [], 'A 的延迟结果不应写回')

  // B 正常返回 → 只写回 B
  deferreds.get('B').resolve({ materialId: 'B', text: 'B摘要', status: 'ok', generatedAt: null })
  await sleep(15)
  assert.equal(results.length, 1)
  assert.equal(results[0].materialId, 'B')
  assert.equal(results[0].result.text, 'B摘要')
}

async function testStopInvalidatesInFlight() {
  const deferreds = new Map()
  const results = []
  const poller = createSummaryPoller({
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
  deferreds.get('A').resolve({ materialId: 'A', text: 'A', status: 'ok', generatedAt: null })
  await sleep(15)
  assert.deepEqual(results, [], 'stop 后在途结果应被丢弃')
}

async function testTimeoutAfterPersistentPending() {
  const timeouts = []
  const poller = createSummaryPoller({
    fetch: () => Promise.resolve({ materialId: 'A', text: '', status: 'pending', generatedAt: null }),
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

async function run() {
  await testDelayedResponseDoesNotOverwriteNewMaterial()
  await testStopInvalidatesInFlight()
  await testTimeoutAfterPersistentPending()
  console.log('summary-polling: 3 tests OK')
}

run().catch((err) => {
  console.error(err)
  process.exit(1)
})
