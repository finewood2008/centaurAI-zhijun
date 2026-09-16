'use strict'

const { randomUUID } = require('node:crypto')
const DISCOVERY_WAIT_MS = 30000
const SELECTION_WAIT_MS = 30000

// Product chooser lifetime, independent of the SDK's eight-second discovery
// batch. The SDK still owns the selected-device lease and encrypted transport.
function installElectronBluetoothPicker(input) {
  const channels = input.channels
  const now = () => input.clock.monotonicMs()
  const schedule = input.setTimeout || setTimeout
  const unschedule = input.clearTimeout || clearTimeout
  let active, disposed = false
  const trusted = event => event.sender === input.webContents
    && event.senderFrame === input.webContents.mainFrame
    && event.senderFrame?.url === input.documentUrl
  const send = (kind, value) => {
    if (!disposed && !input.webContents.isDestroyed?.()) input.webContents.send(channels[kind], value)
  }
  function finish(session, deviceId = '') {
    if (active !== session) return
    active = undefined
    unschedule(session.timer)
    session.candidates.clear()
    const callback = session.callback
    session.callback = undefined
    // Retire before calling Chromium, which may synchronously settle requestDevice.
    try { send('settled', { sessionId: session.sessionId, cancelled: !deviceId }) }
    catch { /* The renderer may already be gone; still settle Chromium. */ }
    finally {
      try { callback?.(deviceId) }
      catch { /* A destroyed native chooser no longer has a callback target. */ }
    }
  }
  function arm(session) {
    unschedule(session.timer)
    session.timer = schedule(() => finish(session), Math.max(0, session.deadline - now()))
  }
  function chooser(event, devices, callback) {
    event.preventDefault()
    if (disposed || input.webContents.mainFrame?.url !== input.documentUrl) {
      callback('')
      return
    }
    let session = active
    if (session && now() >= session.deadline) {
      // The newest callback represents the same pending native request.
      session.callback = callback
      finish(session)
      return
    }
    if (!session) {
      session = { sessionId: randomUUID(), sequence: 0, candidates: new Map(),
        deadline: now() + DISCOVERY_WAIT_MS, hasDiscovered: false, callback }
      active = session
      arm(session)
    }
    // Electron can provide a new callback on every list update. Calling the old
    // callback('') here cancels discovery exactly when the first device appears.
    session.callback = callback
    const providerIds = new Set()
    for (const device of Array.isArray(devices) ? devices : []) {
      if (providerIds.size >= 20) break
      if (typeof device?.deviceId !== 'string' || !device.deviceId || device.deviceId.length > 512
          || typeof device.deviceName !== 'string'
          || !/^CentaurOS-Setup-[A-Za-z0-9]{6}$/.test(device.deviceName)) continue
      providerIds.add(device.deviceId)
      const existing = [...session.candidates.values()].find(entry => entry.providerId === device.deviceId)
      if (!existing) {
        const candidateId = randomUUID()
        session.candidates.set(candidateId, { providerId: device.deviceId,
          candidate: { candidateId, transport: 'ble-gatt', advertisedName: device.deviceName } })
      }
    }
    for (const [id, entry] of session.candidates) {
      if (!providerIds.has(entry.providerId)) session.candidates.delete(id)
    }
    if (session.candidates.size && !session.hasDiscovered) {
      session.hasDiscovered = true
      session.deadline = now() + SELECTION_WAIT_MS
      arm(session)
    }
    send('candidates', { sessionId: session.sessionId, sequence: ++session.sequence,
      candidates: [...session.candidates.values()].map(entry => entry.candidate) })
  }
  function select(event, payload) {
    const session = active
    if (!trusted(event) || !session || payload?.sessionId !== session.sessionId) return
    if (now() >= session.deadline) { finish(session); return }
    const entry = session.candidates.get(payload.candidateId)
    if (entry) finish(session, entry.providerId)
  }
  function cancel(event, payload) {
    if (trusted(event) && active && payload?.sessionId === active.sessionId) finish(active)
  }
  input.webContents.on('select-bluetooth-device', chooser)
  input.ipcMain.on(channels.select, select)
  input.ipcMain.on(channels.cancel, cancel)
  return () => {
    if (disposed) return
    disposed = true
    try { if (active) finish(active) }
    finally {
      input.webContents.removeListener('select-bluetooth-device', chooser)
      input.ipcMain.removeListener(channels.select, select)
      input.ipcMain.removeListener(channels.cancel, cancel)
    }
  }
}

module.exports = { installElectronBluetoothPicker, DISCOVERY_WAIT_MS, SELECTION_WAIT_MS }
