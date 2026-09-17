'use strict'

const path = require('node:path')
const { randomUUID } = require('node:crypto')
const { performance } = require('node:perf_hooks')
const { pathToFileURL } = require('node:url')

const PROVISIONING_URL = pathToFileURL(path.join(__dirname, 'provisioning', 'setup.html')).href
const PROVISIONING_ASSETS = new Set([
  PROVISIONING_URL,
  pathToFileURL(path.join(__dirname, 'provisioning', 'setup.css')).href,
  pathToFileURL(path.join(__dirname, 'provisioning', 'renderer.js')).href,
])

let activeWindow = null
let opening = null
let activeCleanup = null

function isProvisioningEnabled({ platform = process.platform, testBuild = false } = {}) {
  return ['darwin', 'win32'].includes(platform) || (testBuild === true && platform === 'linux')
}

function isLegacyPlaintextAllowed({ isPackaged, testBuild = false, env = process.env } = {}) {
  return testBuild === true
    || (isPackaged === false && env?.ZHIJUN_ALLOW_LEGACY_PLAINTEXT_PROVISIONING === '1')
}

function trustedContents(contents, window) {
  return Boolean(window && !window.isDestroyed() && contents === window.webContents
    && !contents.isDestroyed() && contents.getURL() === PROVISIONING_URL)
}

function trustedFrame(frame, window) {
  return Boolean(window && !window.isDestroyed() && frame === window.webContents.mainFrame
    && frame?.url === PROVISIONING_URL && window.webContents.getURL() === PROVISIONING_URL)
}

async function createProvisioningWindow({ BrowserWindow, session, dialog, parent = undefined,
  isPackaged = true, testBuild = false, env = process.env,
  ipcMain, provisioningConfig, provisioningContext,
  createBroker = options => require('./provisioning-broker.cjs').createProvisioningBroker(options),
  importPicker } = {}) {
  if (!isProvisioningEnabled({ testBuild })) throw new Error('PROVISIONING_NOT_SUPPORTED')
  if (activeWindow && !activeWindow.isDestroyed()) {
    activeWindow.show()
    activeWindow.focus()
    return activeWindow
  }
  if (opening) return opening

  opening = (async () => {
    const electron = !BrowserWindow || !session || !dialog || (!testBuild && !ipcMain) ? require('electron') : null
    const Window = BrowserWindow || electron.BrowserWindow
    const sessions = session || electron.session
    const dialogs = dialog || electron.dialog
    const ipc = ipcMain || electron?.ipcMain
    const isolatedSession = sessions.fromPartition(`zhijun-provisioning-${randomUUID()}`, { cache: false })
    const legacyAllowed = isLegacyPlaintextAllowed({ isPackaged, testBuild, env })
    const preloadArguments = []
    if (testBuild) preloadArguments.push('--zhijun-provisioning-test-build')
    if (testBuild && legacyAllowed) preloadArguments.push('--zhijun-legacy-plaintext-provisioning')
    const flowId = randomUUID()
    if (!testBuild) preloadArguments.push(`--zhijun-provisioning-flow=${flowId}`)
    const window = new Window({
      width: 780,
      height: 820,
      minWidth: 680,
      minHeight: 640,
      show: false,
      title: testBuild ? '添加 AI 盒子 · 配网测试版' : '添加 AI 盒子',
      backgroundColor: '#fffcf6',
      ...(parent && !parent.isDestroyed() ? { parent } : {}),
      webPreferences: {
        preload: path.join(__dirname, 'provisioning', testBuild ? 'preload.mjs' : 'preload.cjs'),
        session: isolatedSession,
        contextIsolation: true,
        sandbox: !testBuild,
        nodeIntegration: false,
        webSecurity: true,
        webviewTag: false,
        plugins: false,
        additionalArguments: preloadArguments,
      },
    })
    activeWindow = window

    const isTrusted = contents => trustedContents(contents, window)
    isolatedSession.setPermissionCheckHandler((contents, permission, _origin, details) =>
      isTrusted(contents) && permission === 'bluetooth' && details?.isMainFrame === true)
    isolatedSession.setPermissionRequestHandler((contents, permission, callback, details) => {
      callback(isTrusted(contents) && permission === 'bluetooth' && details?.isMainFrame === true)
    })
    isolatedSession.setBluetoothPairingHandler(async (details, callback) => {
      // Electron 37 binds pairing to a WebFrameMain, not a webContents field.
      // Manual PIN entry remains fail-closed until a dedicated PIN surface is added.
      if (!trustedFrame(details?.frame, window) || !['confirm', 'confirmPin'].includes(details?.pairingKind)) {
        callback({ confirmed: false })
        return
      }
      const pin = typeof details.pin === 'string' && /^\d{1,16}$/.test(details.pin) ? details.pin : null
      try {
        const result = await dialogs.showMessageBox(window, {
          type: 'question',
          buttons: ['取消', '配对'],
          defaultId: 0,
          cancelId: 0,
          noLink: true,
          title: '确认蓝牙配对',
          message: pin ? `请确认 AI 盒子上显示的配对码是 ${pin}` : '确认与当前选中的 AI 盒子配对？',
          detail: '只有在盒子和此窗口的信息一致时才继续。',
        })
        callback({ confirmed: result.response === 1 && trustedFrame(details.frame, window) })
      } catch { callback({ confirmed: false }) }
    })
    isolatedSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
      callback({ cancel: !PROVISIONING_ASSETS.has(details.url) })
    })

    let picker
    let broker
    let cleaned = false
    const cleanup = () => {
      if (cleaned) return
      cleaned = true
      picker?.dispose()
      void broker?.dispose('renderer_crash')
      if (activeWindow === window) activeWindow = null
      if (activeCleanup === cleanup) activeCleanup = null
    }
    activeCleanup = cleanup
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    window.webContents.on('will-navigate', (event, url) => { if (url !== PROVISIONING_URL) event.preventDefault() })
    window.webContents.on('will-attach-webview', event => event.preventDefault())
    window.webContents.on('render-process-gone', () => { if (!window.isDestroyed()) window.destroy() })
    window.once('closed', cleanup)
    try {
      const imported = await (importPicker ? importPicker()
        : import(testBuild ? '@nexusaos/device-provisioning-electron/main'
          : '@nexusaos/device-discovery-electron/main'))
      if (window.isDestroyed()) throw new Error('PROVISIONING_WINDOW_CLOSED')
      if (typeof imported.installElectronBluetoothPicker !== 'function') throw new Error('PROVISIONING_SDK_UNAVAILABLE')
      const installPicker = testBuild || importPicker ? imported.installElectronBluetoothPicker
        : require('./provisioning/picker.cjs').installElectronBluetoothPicker
      const installedPicker = installPicker({ webContents: window.webContents,
        documentUrl: PROVISIONING_URL, ipcMain: ipc,
        channels: imported.DISCOVERY_PICKER_CHANNELS,
        clock: Object.freeze({ monotonicMs: () => performance.now() }),
        timeoutMs: 30000, scanTimeoutMs: 8000 })
      picker = typeof installedPicker === 'function' ? { dispose: installedPicker } : installedPicker
      if (!testBuild) {
        if (!provisioningConfig || !provisioningContext) throw new Error('PROVISIONING_CONFIGURATION_INVALID')
        broker = createBroker({ ipcMain: ipc, window, flowId, provisioningConfig, provisioningContext })
        await broker.ready
      }
      await window.loadURL(PROVISIONING_URL)
      if (window.isDestroyed()) throw new Error('PROVISIONING_WINDOW_CLOSED')
      window.show()
      return window
    } catch (error) {
      cleanup()
      if (!window.isDestroyed()) window.destroy()
      throw error
    }
  })()
  try { return await opening } finally { opening = null }
}

function closeProvisioningWindow() {
  const window = activeWindow
  activeCleanup?.()
  if (window && !window.isDestroyed()) window.destroy()
  activeWindow = null
}

module.exports = { PROVISIONING_URL, isProvisioningEnabled, isLegacyPlaintextAllowed,
  createProvisioningWindow, closeProvisioningWindow }
