'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { installElectronBluetoothPicker } = require('../provisioning/picker.cjs')

const channels = { candidates: 'candidates', settled: 'settled', select: 'select', cancel: 'cancel' }
const box = (id = 'private-device') => ({ deviceId: id, deviceName: 'CentaurOS-Setup-E2334B' })
function fixture() {
  const ipcMain = new EventEmitter(), webContents = new EventEmitter(), messages = [], callbacks = [], timers = new Map()
  let time = 0, timerId = 0
  const frame = { url: 'file:///trusted/setup.html' }
  webContents.mainFrame = frame
  webContents.send = (channel, payload) => messages.push({ channel, payload })
  const dispose = installElectronBluetoothPicker({ ipcMain, webContents, documentUrl: frame.url, channels,
    clock: { monotonicMs: () => time },
    setTimeout(fn, ms) { timers.set(++timerId, { fn, at: time + ms }); return timerId },
    clearTimeout(id) { timers.delete(id) } })
  const f = { ipcMain, webContents, messages, callbacks, timers, dispose,
    event: { sender: webContents, senderFrame: frame },
    update(devices = []) {
      const calls = []
      callbacks.push(calls)
      // Chromium emits a distinct callback function for successive snapshots.
      webContents.emit('select-bluetooth-device', { preventDefault() {} }, devices, id => calls.push(id))
    },
    advance(ms, fire = true) {
      time += ms
      if (fire) for (const [id, timer] of [...timers]) {
        if (timer.at <= time) { timers.delete(id); timer.fn() }
      }
    },
    latest() { return messages.filter(m => m.channel === channels.candidates).at(-1).payload },
    select(payload, event) {
      const snapshot = f.latest()
      ipcMain.emit(channels.select, event || f.event, payload || {
        sessionId: snapshot.sessionId, candidateId: snapshot.candidates[0]?.candidateId })
    },
  }
  return f
}

test('new callback at first discovery does not cancel the native request', () => {
  const f = fixture()
  f.update([])
  f.advance(7900)
  f.update([box()])
  f.advance(1000)
  f.update([box()])
  assert.deepEqual(f.callbacks, [[], [], []])
  f.select()
  assert.deepEqual(f.callbacks, [[], [], ['private-device']])
  assert.equal(f.messages.at(-1).payload.cancelled, false)
  assert.equal(f.timers.size, 0)
  f.select()
  f.dispose()
  assert.deepEqual(f.callbacks, [[], [], ['private-device']], 'only settles once')
})

test('first device found at 29.9 seconds gets a full 30-second selection window', () => {
  const f = fixture()
  f.update([])
  f.advance(29900)
  f.update([box()])
  f.advance(29999)
  assert.deepEqual(f.callbacks, [[], []])
  f.select()
  assert.deepEqual(f.callbacks, [[], ['private-device']])
  f.dispose()
})

test('repeated advertisements do not extend the selection deadline forever', () => {
  const f = fixture()
  f.update([box()])
  for (let i = 0; i < 5; i++) { f.advance(5000); f.update([box()]) }
  f.advance(5000)
  assert.deepEqual(f.callbacks.at(-1), [''])
  f.select()
  assert.equal(f.messages.filter(m => m.channel === 'settled').length, 1)
  f.dispose()
})

test('select checks monotonic expiry even if timer callback has not run yet', () => {
  const f = fixture()
  f.update([box()])
  f.advance(30001, false)
  f.select()
  assert.deepEqual(f.callbacks, [['']])
  assert.equal(f.messages.at(-1).payload.cancelled, true)
  f.dispose()
})

test('empty discovery is bounded and disposal removes listeners and cancels once', () => {
  const f = fixture()
  f.update([])
  f.advance(30000)
  assert.deepEqual(f.callbacks, [['']])
  f.update([box()])
  f.dispose(); f.dispose()
  assert.deepEqual(f.callbacks, [[''], ['']])
  assert.equal(f.timers.size, 0)
  assert.equal(f.webContents.listenerCount('select-bluetooth-device'), 0)
  assert.equal(f.ipcMain.listenerCount('select'), 0)
})

test('untrusted IPC, stale sessions, removed candidates and raw device IDs cannot select', () => {
  const f = fixture()
  f.update([box()])
  const first = f.latest()
  const payload = { sessionId: first.sessionId, candidateId: first.candidates[0].candidateId }
  f.select(payload, { sender: f.webContents, senderFrame: { ...f.webContents.mainFrame } })
  f.select(payload, { sender: {}, senderFrame: f.webContents.mainFrame })
  f.select({ ...payload, candidateId: 'private-device' })
  f.ipcMain.emit('cancel', { ...f.event, senderFrame: {} }, { sessionId: first.sessionId })
  assert.deepEqual(f.callbacks, [[]])
  f.update([])
  f.select(payload)
  assert.deepEqual(f.callbacks, [[], []])
  f.ipcMain.emit('cancel', f.event, { sessionId: first.sessionId })
  f.update([box()])
  f.select(payload)
  assert.deepEqual(f.callbacks.at(-1), [])
  f.select()
  assert.deepEqual(f.callbacks.at(-1), ['private-device'])
  assert.doesNotMatch(JSON.stringify(f.messages), /private-device|providerId|transportHandle|selectionLeaseId/)
  f.dispose()
})

test('unexpected devices are filtered and candidate output is capped', () => {
  const f = fixture()
  f.update([{ deviceId: 'untrusted', deviceName: 'Headphones' },
    { deviceId: 'wrong-name', deviceName: 'CentaurOS-Setup-<script>' },
    ...Array.from({ length: 30 }, (_, i) => box(`private-${i}`))])
  assert.equal(f.latest().candidates.length, 20)
  assert.equal(new Set(f.latest().candidates.map(c => c.candidateId)).size, 20)
  f.dispose()
})

test('failed renderer notification cannot prevent the native request from settling', () => {
  const f = fixture()
  f.update([box()])
  f.webContents.send = () => { throw new Error('renderer gone') }
  assert.doesNotThrow(() => f.select())
  assert.deepEqual(f.callbacks, [['private-device']])
  assert.equal(f.timers.size, 0)
  f.dispose()
})

test('throwing native teardown callback cannot retain listeners or timers', () => {
  const f = fixture()
  f.webContents.emit('select-bluetooth-device', { preventDefault() {} }, [], () => {
    throw new Error('native chooser gone')
  })
  assert.doesNotThrow(() => f.dispose())
  assert.equal(f.timers.size, 0)
  assert.equal(f.webContents.listenerCount('select-bluetooth-device'), 0)
  assert.equal(f.ipcMain.listenerCount('select'), 0)
  assert.equal(f.ipcMain.listenerCount('cancel'), 0)
})
