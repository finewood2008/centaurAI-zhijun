// 阶段 3（WP N）BLE Adapter 骨架合同测试。
//
// 覆盖：GATT v2 合同常量与候选过滤、Command 分片/重组（含异常帧拒绝）、
// ClaimCoordinator 未绑定 fail-closed、BLE Adapter 的 Gate/acceptAllDevices/
// transport 依赖注入与 chooser handoff 候选收口。
//
// 运行：node --test ble-skeleton.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const {
  GATT_V2_SERVICE_UUID,
  GATT_V2_CHARACTERISTICS,
  COMMAND_MAX_FRAME_PAYLOAD,
  BLE_EVENTS,
  isCandidateSupported,
} = require('./ble-contracts.js')
const { frameCommand, assembleFrames, crc8Hex } = require('./command-framing.js')
const { ClaimCoordinator } = require('./claim-coordinator.js')
const { BleAdapter } = require('./ble-adapter.js')

test('GATT v2 合同常量完整', () => {
  assert.match(GATT_V2_SERVICE_UUID, /^[0-9a-f-]{36}$/i)
  for (const [name, uuid] of Object.entries(GATT_V2_CHARACTERISTICS)) {
    assert.equal(typeof uuid, 'string')
    assert.ok(uuid.length > 0, `characteristic ${name} 为空`)
  }
  assert.ok(COMMAND_MAX_FRAME_PAYLOAD > 0)
  assert.ok(BLE_EVENTS.status.length > 0)
})

test('isCandidateSupported：仅接受服务 UUID 匹配且声明支持的候选', () => {
  assert.equal(isCandidateSupported({ isSupported: true, uuids: [GATT_V2_SERVICE_UUID] }), true)
  assert.equal(isCandidateSupported({ isSupported: true, uuids: [GATT_V2_SERVICE_UUID.toUpperCase()] }), true)
  assert.equal(isCandidateSupported({ isSupported: true, uuids: ['0000ffff-0000-1000-8000-00805f9b34fb'] }), false)
  assert.equal(isCandidateSupported({ isSupported: true, uuids: [] }), false)
  assert.equal(isCandidateSupported({ isSupported: false, uuids: [GATT_V2_SERVICE_UUID] }), false)
  assert.equal(isCandidateSupported(null), false)
})

test('frameCommand：分片含 seq/total/crc 且往返一致', () => {
  const payload = 'aabbccddeeff00112233445566778899'
  const frames = frameCommand(payload, { chunkSize: 8 })
  assert.ok(frames.length >= 2)
  for (const frame of frames) {
    assert.equal(typeof frame.seq, 'number')
    assert.equal(frame.total, frames.length)
    assert.equal(frame.crc, crc8Hex(frame.payloadHex))
  }
  assert.equal(assembleFrames(frames), payload)
  assert.equal(assembleFrames([...frames].reverse()), payload)
})

test('frameCommand：非法载荷拒绝', () => {
  assert.throws(() => frameCommand(''), /非空偶数长度 hex/)
  assert.throws(() => frameCommand('abc'), /非空偶数长度 hex/)
  assert.throws(() => frameCommand('zz'), /合法 hex/)
  assert.throws(() => frameCommand('00'.repeat(200), { chunkSize: 8, maxTotal: 4 }), /最大分片数/)
})

test('assembleFrames：异常帧拒绝（不产出半帧）', () => {
  const frames = frameCommand('aabbccddeeff', { chunkSize: 4 })
  assert.throws(() => assembleFrames([]), /帧列表为空/)
  assert.throws(() => assembleFrames(frames.slice(1)), /帧缺失/)
  const corrupted = JSON.parse(JSON.stringify(frames))
  corrupted[0].payloadHex = 'ffff'
  assert.throws(() => assembleFrames(corrupted), /CRC 校验失败/)
  const badTotal = JSON.parse(JSON.stringify(frames))
  badTotal[1].total = 99
  assert.throws(() => assembleFrames(badTotal), /元数据不一致/)
  const badSeq = JSON.parse(JSON.stringify(frames))
  badSeq[0].seq = 5
  assert.throws(() => assembleFrames(badSeq), /序号越界/)
})

test('ClaimCoordinator 未绑定调用一律抛错（fail-closed）', async () => {
  const coordinator = new ClaimCoordinator()
  assert.equal(coordinator.isBound, false)
  await assert.rejects(coordinator.start({}), /未接入/)
  await assert.rejects(coordinator.selectCandidate('id'), /未接入/)
  await assert.rejects(coordinator.authenticate(), /未接入/)
  await assert.rejects(coordinator.provisionWifi('ssid', 'pw'), /未接入/)
  await assert.rejects(coordinator.appProof(), /未接入/)
  await assert.rejects(coordinator.acknowledge(), /未接入/)
  await assert.rejects(coordinator.cancel('user'), /未接入/)
  await assert.rejects(coordinator.resume(), /未接入/)
  assert.throws(() => coordinator.snapshot(), /未接入/)
})

test('ClaimCoordinator 绑定后可用且禁止热替换/缺方法', async () => {
  const coordinator = new ClaimCoordinator()
  assert.throws(() => coordinator.bind({}), /缺少方法/)
  const impl = {
    start: async () => ({ ok: true }),
    selectCandidate: async () => ({}),
    authenticate: async () => ({}),
    provisionWifi: async () => ({}),
    appProof: async () => ({}),
    acknowledge: async () => ({}),
    cancel: async () => ({}),
    resume: async () => ({}),
    snapshot: () => ({ stage: 'idle' }),
  }
  coordinator.bind(impl)
  assert.equal(coordinator.isBound, true)
  assert.deepEqual(await coordinator.start({}), { ok: true })
  assert.deepEqual(coordinator.snapshot(), { stage: 'idle' })
  assert.throws(() => coordinator.bind(impl), /禁止热替换/)
})

test('BleAdapter：Gate 关闭与 acceptAllDevices 拒绝发现', async () => {
  const adapter = new BleAdapter({ discoveryEnabled: false })
  await assert.rejects(adapter.discover(), /未开放/)
  const open = new BleAdapter({ discoveryEnabled: true })
  await assert.rejects(open.discover({ acceptAllDevices: true }), /acceptAllDevices/)
})

test('BleAdapter：transport 未接入即拒绝 GATT 操作', async () => {
  const adapter = new BleAdapter({ discoveryEnabled: true })
  await assert.rejects(adapter.connect({}), /transport 未接入/)
  await assert.rejects(adapter.readDeviceInfo({}), /transport 未接入/)
  await assert.rejects(adapter.sendCommand({}, 'aabb'), /transport 未接入/)
  assert.throws(() => adapter.subscribeStatus({}, () => {}), /transport 未接入/)
})

test('BleAdapter：注入 transport 后 discover/connect/sendCommand 工作', async () => {
  const written = []
  const transport = {
    requestDevice: async ({ filters }) => {
      assert.equal(filters[0].services[0], GATT_V2_SERVICE_UUID)
      return { isSupported: true, uuids: [GATT_V2_SERVICE_UUID], deviceId: 'dev-1' }
    },
    connect: async (device) => device.deviceId,
    disconnect: async () => {},
    read: async () => 'aabb',
    write: async (server, charUuid, frame) => {
      written.push(frame)
    },
    subscribe: async () => {},
  }
  const adapter = new BleAdapter({ discoveryEnabled: true, transport })
  const device = await adapter.discover()
  assert.equal(device.deviceId, 'dev-1')
  assert.equal(await adapter.connect(device), 'dev-1')
  const frameCount = await adapter.sendCommand({}, 'aabbccddeeff', { chunkSize: 4 })
  assert.equal(frameCount, 3)
  assert.equal(written.length, 3)
  assert.equal(written[0].seq, 0)
  assert.equal(written[0].total, 3)
  await adapter.disconnect()
  assert.equal(adapter._connectedDevice, null)
})

test('BleAdapter：chooser handoff 候选收口', () => {
  const adapter = new BleAdapter({ discoveryEnabled: true })
  const handlers = {}
  const webContents = {
    on: (event, handler) => {
      handlers[event] = handler
    },
  }
  adapter.installChooserHandoff(webContents)
  assert.equal(typeof handlers['select-bluetooth-device'], 'function')
  // 无支持候选 → 拒绝
  let chosen = 'unset'
  handlers['select-bluetooth-device'](null, [{ deviceId: 'x', isSupported: true, uuids: ['0000ffff-0000-1000-8000-00805f9b34fb'] }], (id) => {
    chosen = id
  })
  assert.equal(chosen, '')
  // 单支持候选 → 自动确认（仍是用户显式选择的唯一项）
  handlers['select-bluetooth-device'](
    null,
    [
      { deviceId: 'bad', isSupported: true, uuids: ['0000ffff-0000-1000-8000-00805f9b34fb'] },
      { deviceId: 'good', isSupported: true, uuids: [GATT_V2_SERVICE_UUID] },
    ],
    (id) => {
      chosen = id
    },
  )
  assert.equal(chosen, 'good')
  // 多支持候选 → 不自动选择，交还 chooser
  handlers['select-bluetooth-device'](
    null,
    [
      { deviceId: 'a', isSupported: true, uuids: [GATT_V2_SERVICE_UUID] },
      { deviceId: 'b', isSupported: true, uuids: [GATT_V2_SERVICE_UUID] },
    ],
    (id) => {
      chosen = id
    },
  )
  assert.equal(chosen, '')
})
