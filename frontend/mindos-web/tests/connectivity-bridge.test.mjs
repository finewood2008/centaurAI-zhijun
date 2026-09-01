// 阶段 2 受控票据桥合同测试（node --experimental-strip-types 运行）。
//
// 覆盖高1修复：宿主桥 getTicket() 可为异步（Electron preload 的 ipcRenderer.invoke
// 返回 Promise），api.ts 的 readConnectivityTicket()/provisionMindosSession() 必须 await，
// 否则票据模式永远读不到票据、无法交换会话。此测试注入一个「返回 Promise」的桥，
// 验证 provisionMindosSession 真实 await 到票据并完成交换、会话凭证落到内存。
//
// 运行：node --experimental-strip-types tests/connectivity-bridge.test.mjs
import assert from 'node:assert/strict'
import {
  provisionMindosSession,
  setMindosSessionToken,
  getMindosSessionToken,
} from '../src/services/api.ts'

function deferred() {
  let resolve
  const promise = new Promise((res) => {
    resolve = res
  })
  return { promise, resolve }
}

// 打桩后端：/mindos/access-context 返回票据模式 + deviceId；交换接口返回 sessions 结果。
function stubBackend() {
  const exchangeCalls = []
  const accessCalls = []
  globalThis.fetch = async (url, init = {}) => {
    const path = String(url)
    if (path.endsWith('/mindos/access-context')) {
      accessCalls.push(init.method || 'GET')
      return json(200, {
        mode: 'connectivity_ticket_required',
        localDebug: false,
        deviceId: 'dev-for-test-1',
      })
    }
    if (path.endsWith('/mindos/connectivity/sessions/exchange')) {
      exchangeCalls.push(init.headers)
      return json(200, {
        sessionToken: 'session-token-123',
        sessionId: 's_1',
        deviceId: 'dev-for-test-1',
        accountId: 'acc_1',
        clientId: 'cl_1',
        epochGeneration: 1,
        expiresAt: 1700000000000,
      })
    }
    return json(404, { detail: `unexpected ${path}` })
  }
  return { exchangeCalls, accessCalls }
}

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

async function testAsyncHostBridgeIsAwaited() {
  const { exchangeCalls, accessCalls } = stubBackend()

  // 契约核心：宿主桥 getTicket 返回 Promise（等价 preload 的 ipcRenderer.invoke）。
  const ticketPromise = deferred()
  globalThis.window = {
    __MINDOS_ACCESS__: {
      getTicket: () => ticketPromise.promise,
    },
  }

  const pending = provisionMindosSession()
  // 尚未 resolve 票据：会话不得提前落地，交换不得发生（证明确实在等待异步票据）。
  assert.equal(getMindosSessionToken(), null)
  ticketPromise.resolve('ticket-x')
  const res = await pending

  assert.deepEqual(res, { deviceId: 'dev-for-test-1' })
  assert.equal(getMindosSessionToken(), 'session-token-123')
  assert.equal(accessCalls.length, 1)
  assert.equal(exchangeCalls.length, 1)
  const headers = exchangeCalls[0]
  const auth = headers && typeof headers.get === 'function' ? headers.get('authorization') : undefined
  assert.equal(auth, 'Bearer ticket-x')
}

async function testNoBridgeIsNoop() {
  stubBackend()
  globalThis.window = { __MINDOS_ACCESS__: null }
  // 票据模式要求连接，但无宿主桥：必须静默跳过，不弹错、不注入会话。
  const res = await provisionMindosSession()
  assert.equal(res, null)
  assert.equal(getMindosSessionToken(), null)
}

async function testNullTicketIsNoop() {
  stubBackend()
  globalThis.window = {
    __MINDOS_ACCESS__: {
      getTicket: async () => null, // 异步桥，但无可用票据
    },
  }
  const res = await provisionMindosSession()
  assert.equal(res, null)
  assert.equal(getMindosSessionToken(), null)
}

async function run() {
  const cases = [
    ['异步票据桥被 await 且会话凭证落内存', testAsyncHostBridgeIsAwaited],
    ['宿主桥缺失时静默跳过', testNoBridgeIsNoop],
    ['异步桥返回 null 时静默跳过', testNullTicketIsNoop],
  ]
  let passed = 0
  for (const [name, fn] of cases) {
    setMindosSessionToken(null)
    await fn()
    passed += 1
    console.log(`connectivity-bridge: ${name} OK`)
  }
  console.log(`connectivity-bridge: ${passed} tests OK`)
}

void run()