// P14-09 关联加载防串台回归测试（node --experimental-strip-types 运行）。
//
// 核心回归：知识卡片 A 的关联请求延迟返回、用户已切换到卡片 B 时，
// A 的结果不得覆盖 B 的关联推荐（会话门 + 路由目标二次校验 + 新请求清空旧结果）。
//
// 注意：load(id) 在 fetch 返回前挂起。涉及 deferred 的在途请求必须“不 await，
// 保留在途”，待对应 deferred resolve 后再统一 await，否则测试会卡在首个 await。
//
// 运行：node --experimental-strip-types tests/related-session.test.mjs
import assert from 'node:assert/strict'
import { createRelatedLoader } from '../src/composables/useRelatedLoader.ts'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function deferred() {
  let resolve
  const promise = new Promise((res) => {
    resolve = res
  })
  return { promise, resolve }
}

// 页面状态模拟：与 KnowledgeEditPage 的 related/relatedNote/relatedLoading 等价
function createPageState() {
  return { items: [], note: '', loading: false, onResultCount: 0 }
}

function makeLoader(state, fetch, isCurrentTarget) {
  return createRelatedLoader({
    fetch,
    isCurrentTarget,
    onResult: (result) => {
      state.items = result.items
      state.note = result.note
      state.onResultCount += 1
    },
    onEmpty: () => {
      state.items = []
      state.note = ''
    },
    onLoading: (v) => {
      state.loading = v
    },
  })
}

async function testDelayedRelatedDoesNotOverwriteNewCard() {
  // 时序：A 请求延迟（不 await，保留在途）→ 切到 B 并发起 B 请求 → 放行 A（应被丢弃）
  // → B 返回（保留 B）
  const deferreds = new Map()
  let currentTarget = 'A'
  const state = createPageState()

  const loader = makeLoader(
    state,
    (id) => {
      const d = deferred()
      deferreds.set(id, d)
      return d.promise
    },
    (id) => currentTarget === id,
  )

  const loadA = loader.load('A') // 不 await，保留在途请求
  await sleep(5)
  assert(deferreds.has('A'), 'A 的关联请求应已发出')
  assert.equal(state.loading, true, '加载中状态应开启')
  assert.deepEqual(state.items, [], '新请求开启即清空旧结果')

  // 切到卡片 B：路由目标变化并发出 B 请求
  currentTarget = 'B'
  const loadB = loader.load('B') // 不 await，保留在途请求
  await sleep(5)
  assert(deferreds.has('B'), 'B 的关联请求应已发出')

  // A 的旧请求延迟返回（带 A 的数据）→ 必须被丢弃，不得写回 B
  deferreds.get('A').resolve({ items: [{ id: 'a', title: 'A关联' }], note: '' })
  await sleep(15)
  assert.deepEqual(state.items, [], 'A 的延迟结果不应写回')
  assert.equal(state.onResultCount, 0, 'A 的结果不应触发写回')

  // B 正常返回 → 只保留 B
  deferreds.get('B').resolve({ items: [{ id: 'b', title: 'B关联' }], note: '仅 1 项' })
  await Promise.all([loadA, loadB])
  assert.equal(state.onResultCount, 1)
  assert.equal(state.items.length, 1)
  assert.equal(state.items[0].id, 'b')
  assert.equal(state.note, '仅 1 项')
  assert.equal(state.loading, false, '请求结束后加载中状态应关闭')
}

async function testRoutingMismatchAlsoDiscards() {
  // 即使代次最新，但当前路由目标已不是 id（如切卡后旧 load 恰好是新代次），也不写回
  const state = createPageState()
  state.items = [{ id: 'x', title: '旧' }]
  const loader = makeLoader(
    state,
    () => Promise.resolve({ items: [{ id: 'a', title: 'A' }], note: 'n' }), // 立即完成，可 await
    () => false, // 目标永不匹配
  )
  await loader.load('A')
  assert.deepEqual(state.items, [], '目标不一致不应写回')
}

async function testNewLoadClearsStaleResultsImmediately() {
  // 新请求开启即清空旧结果，避免详情已切换、关联仍在加载时短暂显示上一张卡片的推荐
  const deferreds = new Map()
  const state = createPageState()
  state.items = [{ id: 'a', title: 'A关联' }] // 模拟上一张卡片的残留结果

  const loader = makeLoader(
    state,
    (id) => {
      const d = deferred()
      deferreds.set(id, d)
      return d.promise
    },
    () => true,
  )

  // 不 await（模拟在途）：load 同步进入后应立刻清空旧结果
  const pending = loader.load('B')
  assert.deepEqual(state.items, [], '新请求开启应立即清空旧结果')
  assert.equal(state.loading, true)
  deferreds.get('B').resolve({ items: [{ id: 'b', title: 'B关联' }], note: '' })
  await pending
  assert.equal(state.items[0].id, 'b')
}

async function testInFlightInvalidatedOnUnmount() {
  // 组件卸载后 invalidate：在途结果全部失效，不得写回
  const deferreds = new Map()
  const state = createPageState()
  const loader = makeLoader(
    state,
    (id) => {
      const d = deferred()
      deferreds.set(id, d)
      return d.promise
    },
    () => true,
  )

  const loadA = loader.load('A') // 不 await，保留在途请求
  await sleep(5)
  assert(deferreds.has('A'), 'A 的请求应已发出')
  loader.invalidate() // 模拟组件卸载
  deferreds.get('A').resolve({ items: [{ id: 'a', title: 'A关联' }], note: '' })
  await loadA
  assert.deepEqual(state.items, [], '卸载后在途结果应被丢弃')
  assert.equal(state.loading, true, '卸载后不再重置加载态（已离开页面）')
}

async function run() {
  await testDelayedRelatedDoesNotOverwriteNewCard()
  await testRoutingMismatchAlsoDiscards()
  await testNewLoadClearsStaleResultsImmediately()
  await testInFlightInvalidatedOnUnmount()
  console.log('related-session: 4 tests OK')
}

run().catch((err) => {
  console.error(err)
  process.exit(1)
})
