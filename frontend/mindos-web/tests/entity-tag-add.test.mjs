// P14-04 实体「作为标签添加」回归测试（node --experimental-strip-types 运行）。
//
// 核心回归（对应 Review P1#2）：
// 1. 仅点击某实体时，setMaterialTags 只以该实体名调用一次（add 语义），
//    不得顺带写入其它实体，也不得触碰候选标签的确认状态；
// 2. 请求前固定资料 ID；请求返回时用户已切换到其它资料 → 不写回、不提示；
// 3. 实体已存在于正式标签 → 直接忽略（按钮显示「已添加」）；
// 4. 同一时刻只允许一个实体添加请求在途。
//
// 运行：node --experimental-strip-types tests/entity-tag-add.test.mjs
import assert from 'node:assert/strict'
import { createEntityTagAdder } from '../src/composables/useEntityTagAdd.ts'

const entity = (entityId, name) => ({ entityId, name })

function deferred() {
  let resolve
  const promise = new Promise((res) => {
    resolve = res
  })
  return { promise, resolve }
}

// 构造 adder 上下文：detail 用可变对象模拟（materialId/tags 可被调用方改）。
// calls 记录 setMaterialTags 的调用参数；confirmedCandidates 模拟候选确认状态，
// 断言实体添加不会影响它。
function makeContext(initialDetail) {
  const calls = []
  const state = {
    detail: initialDetail,
    busy: '',
    applied: [],
    errors: [],
    confirmedCandidates: new Set(),
  }
  const adder = createEntityTagAdder({
    getDetail: () => state.detail,
    isBusy: () => state.busy !== '',
    setBusyEntityId: (entityId) => {
      state.busy = entityId
    },
    setMaterialTags: (materialId, tags, action) => {
      calls.push({ materialId, tags, action })
      return Promise.resolve({ tags: [...(state.detail ? state.detail.tags : []), ...tags] })
    },
    applyTags: (materialId, tags) => {
      state.applied.push({ materialId, tags })
      if (state.detail && state.detail.materialId === materialId) {
        state.detail.tags = tags
      }
    },
    onError: (message) => {
      state.errors.push(message)
    },
  })
  return { adder, state, calls }
}

async function testOnlyClickedEntityIsWritten() {
  const { adder, state, calls } = makeContext({ materialId: 'M1', tags: ['已有标签'] })

  // 点击「张三」，其后又点击「李四」：只写各自实体，互不干扰
  await adder(entity('entity:person:张三', '张三'))
  await adder(entity('entity:person:李四', '李四'))

  assert.equal(calls.length, 2, '两次点击应各发起一次 add')
  assert.deepEqual(calls[0], { materialId: 'M1', tags: ['张三'], action: 'add' }, '第一次仅写入张三')
  assert.deepEqual(calls[1], { materialId: 'M1', tags: ['李四'], action: 'add' }, '第二次仅写入李四')
  assert.deepEqual(state.detail.tags, ['已有标签', '张三', '李四'], '正式标签只追加被点击实体')
  assert.deepEqual(state.errors, [], '不应出现错误')
}

async function testAddDoesNotTouchCandidateConfirmation() {
  const { adder, state, calls } = makeContext({ materialId: 'M1', tags: [] })

  // 模拟候选标签确认状态：确认了 S1，实体添加不得改变或重置它
  state.confirmedCandidates.add('S1')
  await adder(entity('entity:person:张三', '张三'))

  assert.equal(calls.length, 1, '仅一次 add 请求')
  assert.ok(state.confirmedCandidates.has('S1'), '候选标签确认状态不受影响')
}

async function testSwitchedMaterialBeforeReturnIsIgnored() {
  const d = deferred()

  const state2 = { detail: { materialId: 'M1', tags: [] }, applied: [], errors: [] }
  const adder2 = createEntityTagAdder({
    getDetail: () => state2.detail,
    isBusy: () => false,
    setBusyEntityId: () => {},
    setMaterialTags: () => d.promise,
    applyTags: (materialId, tags) => {
      state2.applied.push({ materialId, tags })
    },
    onError: (message) => {
      state2.errors.push(message)
    },
  })

  const pending = adder2(entity('entity:person:张三', '张三'))

  // 请求未返回前，用户已切换到资料 M2
  state2.detail = { materialId: 'M2', tags: [] }
  d.resolve({ tags: ['张三'] })
  await pending

  assert.deepEqual(state2.applied, [], '返回后资料已切换 → 不得写回 M2')
  assert.deepEqual(state2.errors, [], '资料已切换 → 不得误提示 M1 的错误')
  assert.equal(state2.detail.materialId, 'M2', 'M2 详情不得被改动')
}

async function testExistingTagIsIgnored() {
  const { adder, state, calls } = makeContext({ materialId: 'M1', tags: ['张三'] })

  const ok = await adder(entity('entity:person:张三', '张三'))
  assert.equal(ok, false, '已在正式标签中的实体应直接忽略')
  assert.equal(calls.length, 0, '不应发起任何请求')
  assert.deepEqual(state.detail.tags, ['张三'])
}

async function testSingleInFlightGuard() {
  const d = deferred()

  const state2 = { detail: { materialId: 'M1', tags: [] }, busy: '' }
  const adder2 = createEntityTagAdder({
    getDetail: () => state2.detail,
    isBusy: () => state2.busy !== '',
    setBusyEntityId: (id) => {
      state2.busy = id
    },
    setMaterialTags: () => d.promise,
    applyTags: () => {},
    onError: () => {},
  })

  const first = adder2(entity('entity:person:张三', '张三'))
  const second = adder2(entity('entity:person:李四', '李四'))
  const secondOk = await second
  d.resolve({ tags: ['张三'] })
  void first

  assert.equal(secondOk, false, '在途期间再次点击应被忽略')
}

async function run() {
  await testOnlyClickedEntityIsWritten()
  await testAddDoesNotTouchCandidateConfirmation()
  await testSwitchedMaterialBeforeReturnIsIgnored()
  await testExistingTagIsIgnored()
  await testSingleInFlightGuard()
  console.log('entity-tag-add: 5 tests OK')
}

run().catch((err) => {
  console.error(err)
  process.exit(1)
})